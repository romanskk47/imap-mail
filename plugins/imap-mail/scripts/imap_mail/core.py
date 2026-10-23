"""Prepare and verify server-side drafts. APPEND is the only remote mutation.

The read adapter stays read-only. No SMTP, flag changes, deletion, or mailbox
creation is provided. An uncertain APPEND is reconciled, never repeated.
"""

from contextlib import contextmanager
import datetime as dt
import email
from email import policy
from email.headerregistry import HeaderRegistry
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import fcntl
import functools
import hashlib
import html
from html.parser import HTMLParser
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import uuid

from . import read_access


MAX_RAW = 16 * 1024 * 1024
MAX_BODY = 200_000
MAX_SIGNATURE = 256 * 1024
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT = 10 * 1024 * 1024
MAX_ATTACHMENT_TOTAL = 12 * 1024 * 1024
MESSAGE_ID = re.compile(r"<[A-Za-z0-9.!#$%&'*+\-/=?^_`{|}~]+@[A-Za-z0-9.-]+>")
STABLE_ID = re.compile(r"[1-9][0-9]{0,18}")
PREVIEW_ID = re.compile(r"[a-f0-9]{32}")
HTML_UTF8_META = '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'


class DraftError(Exception):
    """Public errors contain fixed codes, never server replies or mail content."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def public(method):
    @functools.wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except DraftError:
            raise
        except Exception:
            raise DraftError("OPERATION_FAILED") from None
    return guarded


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def header(value, maximum=4000):
    if not isinstance(value, str) or len(value) > maximum or any(
        ord(c) < 32 or ord(c) == 127 for c in value
    ):
        raise DraftError("INVALID_HEADER")
    return value


def addresses(value):
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or len(values) > 100:
        raise DraftError("INVALID_RECIPIENTS")
    result = []
    seen = set()
    for item in values:
        item = header(item)
        parsed = HeaderRegistry()("To", item)
        if parsed.defects or not parsed.addresses:
            raise DraftError("INVALID_RECIPIENTS")
        for address in parsed.addresses:
            if not address.username or not address.domain:
                raise DraftError("INVALID_RECIPIENTS")
            key = address.addr_spec.casefold()
            if key not in seen:
                result.append({"address": address.addr_spec,
                               "formatted": str(address)})
                seen.add(key)
    if len(result) > 100:
        raise DraftError("INVALID_RECIPIENTS")
    return result


def bare_address(value):
    parsed = addresses(value)
    if len(parsed) != 1 or parsed[0]["address"] != value:
        raise DraftError("INVALID_FROM_ADDRESS")
    return value


def private_dir(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise DraftError("UNSAFE_STATE_PATH")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise DraftError("STATE_PERMISSIONS_REQUIRE_700")
    return path


def private_read(path, maximum=MAX_RAW):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise DraftError("PRIVATE_FILE_PERMISSIONS_REQUIRE_600")
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise DraftError("FILE_TOO_LARGE")
    return value


def write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".draft-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def profile_identity(profile):
    return {"email": profile["user"], "host": profile["host"],
            "entity": profile["entity"], "port": profile.get("port", 993),
            "tls": profile.get("tls", True)}


def text_part(message):
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        value = part.get_content()
        if not isinstance(value, str) or len(value) > MAX_RAW:
            raise DraftError("SOURCE_CONTENT_UNAVAILABLE")
        if part.get_content_type() == "text/html":
            value = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", "", value)
            value = html.unescape(re.sub(r"<[^>]*>", " ", value))
        return value.replace("\r\n", "\n").replace("\r", "\n")
    except DraftError:
        raise
    except Exception:
        raise DraftError("SOURCE_CONTENT_UNAVAILABLE") from None


class _HTMLTextEquivalence(HTMLParser):
    """Locate text nodes without reconstructing or normalizing any markup."""

    RAW_TEXT_TAGS = {"script", "style", "xmp", "iframe", "noembed", "noframes", "plaintext"}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_offsets = [0] + [match.end() for match in re.finditer("\n", source)]
        self.events = []
        self.raw_text_tag = None
        self.exact_source = False

    def _record(self, text=False):
        line, column = self.getpos()
        self.events.append((self.line_offsets[line - 1] + column, text))

    def handle_starttag(self, tag, attrs):
        self._record()
        if self.raw_text_tag is None and tag in self.RAW_TEXT_TAGS:
            self.raw_text_tag = tag

    def handle_startendtag(self, tag, attrs):
        self._record()
        # HTML ignores the self-closing slash on these non-void elements, but
        # HTMLParser does not enter raw-text mode for this spelling.
        if tag in self.RAW_TEXT_TAGS:
            self.exact_source = True

    def handle_endtag(self, tag):
        self._record()
        if tag == self.raw_text_tag and tag != "plaintext":
            self.raw_text_tag = None

    def handle_data(self, data):
        self._record(text=self.raw_text_tag is None)

    def _markup(self, *args):
        self._record()

    handle_comment = handle_decl = handle_pi = unknown_decl = _markup
    handle_entityref = handle_charref = _markup

    def value(self):
        if self.exact_source:
            return [("raw", self.source)]
        tokens = []
        events = self.events
        if not events or events[0][0] != 0:
            events = [(0, False)] + events
        for index, (start, is_text) in enumerate(events):
            end = events[index + 1][0] if index + 1 < len(events) else len(self.source)
            if end <= start:
                return [("raw", self.source)]
            raw = self.source[start:end]
            # A permissively parsed or incomplete construct may be reported as
            # data. Keep the entire source exact when its boundary is unclear.
            if is_text and "<" in raw:
                return [("raw", self.source)]
            kind, value = ("text", html.unescape(raw)) if is_text else ("raw", raw)
            if is_text and tokens and tokens[-1][0] == "text":
                tokens[-1] = ("text", tokens[-1][1] + value)
            else:
                tokens.append((kind, value))
        return tokens


def _html_semantic_value(text):
    try:
        parser = _HTMLTextEquivalence(text)
        parser.feed(text)
        parser.close()
        return parser.value()
    except Exception:
        # Malformed declarations and other parser failures must not discard or
        # normalize content that cannot be identified as an ordinary text node.
        return [("raw", text)]


def semantic(raw):
    """Compare MIME meaning, allowing transfer encoding and CRLF normalization."""
    message = email.message_from_bytes(raw, policy=policy.default)
    names = ("From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To", "References")
    headers = {name: [str(v) for v in message.get_all(name, [])] for name in names}

    def part_value(part):
        value = {"type": part.get_content_type(),
                 "disposition": part.get_content_disposition(),
                 "filename": part.get_filename(), "cid": str(part.get("Content-ID", ""))}
        if part.is_multipart():
            value["parts"] = [part_value(child) for child in part.iter_parts()]
        else:
            payload = part.get_payload(decode=True) or b""
            # Text attachments are arbitrary file bytes, not necessarily text in
            # an advertised charset (for example a UTF-8 .txt without charset).
            # Normalize only authored inline body parts with a known charset.
            if (part.get_content_maintype() == "text" and
                    part.get_content_disposition() is None and part.get_content_charset()):
                charset = part.get_content_charset()
                text = payload.decode(charset).replace("\r\n", "\n").replace("\r", "\n")
                # Observed provider behavior: prepend this one harmless charset
                # declaration to UTF-8 HTML fragments. Strip at most one exact
                # leading declaration; retain all other markup exactly.
                if (part.get_content_type() == "text/html" and
                        charset.lower().replace("_", "-") in ("utf-8", "utf8") and
                        text.startswith(HTML_UTF8_META)):
                    text = text[len(HTML_UTF8_META):]
                # Providers also rewrite character-reference spellings in text
                # nodes. Attributes, tags, comments and raw text stay exact;
                # escaped tags remain text tokens, distinct from actual markup.
                payload = _html_semantic_value(text) if part.get_content_type() == "text/html" else text.encode("utf-8")
            value["sha256"] = digest(payload)
        return value

    return {"headers": headers, "mime": part_value(message)}


class DraftService:
    def __init__(self, config_path=None, state_dir=None, reader_factory=None):
        self.config_path = Path(config_path or os.environ.get("MAIL_DRAFTS_CONFIG", "~/.config/imap-mail/settings.json")).expanduser()
        self.state_override = state_dir or os.environ.get("MAIL_DRAFTS_STATE_DIR")
        self.reader_factory = reader_factory or read_access.Reader

    def _config(self, missing_ok=False):
        if not self.config_path.exists():
            if missing_ok:
                return None
            raise DraftError("SETUP_NEEDED")
        config = json.loads(private_read(self.config_path, 1024 * 1024))
        if not isinstance(config, dict) or config.get("version") != 1 or not isinstance(config.get("accounts"), dict):
            raise DraftError("INVALID_CONFIGURATION")
        return config

    def _resolve(self, value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.config_path.parent / path

    def _state(self, config):
        return private_dir(self._resolve(self.state_override or config.get("state_dir") or "~/.local/state/imap-mail"))

    @contextmanager
    def _lock(self, config):
        root = self._state(config)
        path = root / "drafts.lock"
        if path.is_symlink():
            raise DraftError("UNSAFE_STATE_PATH")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise DraftError("PRIVATE_FILE_PERMISSIONS_REQUIRE_600")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield root
        finally:
            os.close(fd)

    def _profiles(self, config):
        try:
            profiles_dir = config.get("profiles_dir")
            return read_access.profiles(str(self._resolve(profiles_dir)) if profiles_dir else None)
        except Exception:
            raise DraftError("PROFILE_DISCOVERY_FAILED") from None

    def _account(self, config, account, write=False, profiles=None):
        if not isinstance(account, str) or account not in config["accounts"]:
            raise DraftError("ACCOUNT_NOT_CONFIGURED")
        entry = config["accounts"][account]
        if not isinstance(entry, dict):
            raise DraftError("INVALID_ACCOUNT_CONFIGURATION")
        profiles = self._profiles(config) if profiles is None else profiles
        if account not in profiles:
            raise DraftError("ACCOUNT_PROFILE_UNAVAILABLE")
        profile = profiles[account]
        expected = entry.get("identity", {})
        if not isinstance(expected, dict):
            raise DraftError("INVALID_ACCOUNT_CONFIGURATION")
        identity = profile_identity(profile)
        if any(expected.get(k) != identity[k] for k in ("email", "host", "entity")):
            raise DraftError("ACCOUNT_IDENTITY_MISMATCH")
        if profile.get("enabled") is False:
            raise DraftError("ACCOUNT_DISABLED")
        if write:
            if os.environ.get("MAIL_BROKER_CONFIG") or config.get("broker_config"):
                raise DraftError("BROKER_DRAFT_ADAPTER_REQUIRED")
            if entry.get("drafts_enabled") is not True:
                raise DraftError("DRAFTS_NOT_AUTHORIZED")
            aliases = entry.get("from_addresses")
            if not isinstance(aliases, list) or not aliases:
                raise DraftError("FROM_ALLOWLIST_REQUIRED")
            for address in aliases:
                bare_address(address)
            if entry.get("default_from") not in aliases:
                raise DraftError("DEFAULT_FROM_NOT_ALLOWED")
            if not isinstance(entry.get("signature"), dict):
                raise DraftError("SIGNATURE_CONFIGURATION_REQUIRED")
        return entry, profile

    @contextmanager
    def _reader(self, profile):
        reader = self.reader_factory(profile)
        try:
            yield reader
        finally:
            reader.close()

    @public
    def accounts(self):
        config = self._config(missing_ok=True)
        missing_config = config is None
        config = config or {"version": 1, "accounts": {}}
        profiles = self._profiles(config)
        result = []
        for account in sorted(set(profiles) | set(config["accounts"])):
            configured = account in config["accounts"]
            row = {"account": account, "configured": configured,
                   "read_ready": False, "drafts_ready": False,
                   "drafts_enabled": False, "from_addresses": [],
                   "default_from": None, "signature_configured": False,
                   "connection": "not_checked", "issues": []}
            profile = profiles.get(account)
            if profile:
                # These are host-profile identifiers, never credential fields.
                row.update(profile_identity(profile))
            if not configured:
                row.update(status="setup_needed", issues=["ACCOUNT_NOT_CONFIGURED"])
                result.append(row)
                continue
            try:
                entry, _ = self._account(config, account, profiles=profiles)
                row["read_ready"] = True
                row["drafts_enabled"] = entry.get("drafts_enabled") is True
                row["from_addresses"] = entry.get("from_addresses", [])
                row["default_from"] = entry.get("default_from")
                row["signature_configured"] = isinstance(entry.get("signature"), dict)
                self._account(config, account, write=True, profiles=profiles)
                try:
                    self._signature(entry)
                except DraftError:
                    raise
                except Exception:
                    raise DraftError("SIGNATURE_FILES_UNAVAILABLE") from None
                row.update(status="ready", drafts_ready=True)
            except Exception as exc:
                code = exc.code if isinstance(exc, DraftError) else "ACCOUNT_CONFIGURATION_INVALID"
                row.update(status="needs_attention", issues=[code])
            result.append(row)
        needs_setup = missing_config or not config["accounts"] or not result
        status = "setup_needed" if needs_setup else "ready" if all(r["status"] == "ready" for r in result) else "needs_attention"
        response = {"status": status, "accounts": result, "mailbox_writes": 0,
                    "discovered_accounts": len(profiles), "configured_accounts": len(config["accounts"])}
        if config.get("preferred_transport") == "imap":
            response["preferred_transport"] = "imap"
        return response

    @public
    def check(self, account, operation="draft"):
        """Verify one configured account and draft setup, without mailbox writes.

        Readiness is based on TLS login, folder discovery and EXAMINE only. The
        server's permission to APPEND is confirmed only by an authorized draft.
        """
        if operation not in ("read", "draft"):
            raise DraftError("INVALID_CHECK_OPERATION")
        config = self._config()
        if not isinstance(account, str) or account not in config["accounts"]:
            raise DraftError("ACCOUNT_NOT_CONFIGURED")
        result = {"account": account, "status": "needs_attention", "connection": False,
                  "mailbox_writes": 0}
        try:
            if operation == "read":
                _, profile = self._account(config, account)
                result["identity"] = profile_identity(profile)
                with self._reader(profile) as reader:
                    validity, _ = reader.select("INBOX")
                result.update(status="ready", connection=True, operation="read",
                              read_ready=True, mailbox="INBOX", uidvalidity=validity,
                              issues=[])
                return result
            entry, profile = self._account(config, account, write=True)
            result["identity"] = profile_identity(profile)
            try:
                signature, assets = self._signature(entry)
            except DraftError:
                raise
            except Exception:
                raise DraftError("SIGNATURE_FILES_UNAVAILABLE") from None
            with self._reader(profile) as reader:
                result["connection"] = True
                folder, validity = self._drafts(reader, entry)
            result.update(status="ready", drafts_mailbox=folder, uidvalidity=validity,
                          signature_mode=signature["mode"], signature_inline_count=len(assets),
                          issues=[])
        except Exception as exc:
            if isinstance(exc, DraftError):
                code = exc.code
            elif isinstance(exc, read_access.MailError):
                code = "ACCOUNT_ACCESS_UNAVAILABLE"
            else:
                code = "CONNECTION_CHECK_FAILED"
            result.update(error_code=code, issues=[code])
        return result

    @public
    def preview_read(self, preview_id):
        """Read the same integrity-checked preview, never prepare or apply one."""
        config = self._config()
        with self._lock(config) as root:
            _, record, _ = self._load(root, preview_id)
            operation = record["immutable"].get("operation", "draft")
            if operation == "send":
                self._send_prebound(config, record)
            elif operation == "draft_edit":
                # Include the original MIME integrity check, not just the new MIME.
                self._edit_load(root, preview_id)
                self._bound(config, record)
            elif operation == "draft":
                self._bound(config, record)
            else:
                raise DraftError("PREVIEW_OPERATION_MISMATCH")
            return {"status": record["state"], "preview_id": preview_id,
                    "preview_hash": record["preview_hash"],
                    **record["immutable"]["review"],
                    "mailbox_writes": 0, "smtp_submissions": 0}

    @public
    def folders(self, account):
        config = self._config()
        _, profile = self._account(config, account)
        with self._reader(profile) as reader:
            return reader.folders()

    @public
    def search(self, account, mailbox, **kwargs):
        config = self._config()
        _, profile = self._account(config, account)
        with self._reader(profile) as reader:
            return reader.search(mailbox, **kwargs)

    @public
    def read(self, account, mailbox, uidvalidity, uid):
        config = self._config()
        _, profile = self._account(config, account)
        with self._reader(profile) as reader:
            return reader.read(mailbox, uidvalidity, uid)

    def _drafts(self, reader, entry):
        folders = reader.folders()["folders"]
        selectable = [f for f in folders if f.get("selectable", True)]
        special = [f["name"] for f in selectable if f.get("kind") == "drafts" or "\\Drafts" in f.get("flags", [])]
        if len(special) > 1:
            raise DraftError("DRAFTS_FOLDER_AMBIGUOUS")
        override = entry.get("drafts_mailbox")
        if override is not None:
            header(override)
            if len([f for f in selectable if f["name"] == override]) != 1:
                raise DraftError("DRAFTS_FOLDER_UNAVAILABLE")
            if special and special[0] != override:
                raise DraftError("DRAFTS_FOLDER_CONFLICT")
        name = special[0] if special else override
        if not name:
            raise DraftError("DRAFTS_FOLDER_UNAVAILABLE")
        header(name)
        validity, _ = reader.select(name)
        return name, str(validity)

    def _raw(self, reader, mailbox, uidvalidity, uid):
        header(mailbox)
        if not STABLE_ID.fullmatch(str(uidvalidity)) or not STABLE_ID.fullmatch(str(uid)):
            raise DraftError("INVALID_MESSAGE_REFERENCE")
        actual, _ = reader.select(mailbox)
        if str(actual) != str(uidvalidity):
            raise DraftError("UIDVALIDITY_CHANGED")
        status, rows = reader.client.uid("FETCH", str(uid), f"(UID RFC822.SIZE FLAGS BODY.PEEK[]<0.{MAX_RAW + 1}>)")
        literals = [row for row in rows if isinstance(row, tuple)] if status == "OK" else []
        if len(literals) != 1:
            raise DraftError("MESSAGE_UNAVAILABLE")
        prefix, raw = literals[0]
        match = re.search(rb"\bUID (\d+)\b", prefix)
        size = re.search(rb"RFC822.SIZE (\d+)", prefix)
        flags = re.search(rb"FLAGS \(([^)]*)\)", prefix)
        if not match or match[1].decode() != str(uid) or not size or flags is None:
            raise DraftError("FETCH_IDENTITY_MISMATCH")
        if not isinstance(raw, bytes) or len(raw) > MAX_RAW:
            raise DraftError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
        if int(size[1]) > len(raw):
            # Some providers retain a stale RFC822.SIZE after MIME conversion.
            # Confirm EOF without accepting an incomplete literal or trusting size.
            probe_status, probe_rows = reader.client.uid("FETCH", str(uid), f"(UID BODY.PEEK[]<{len(raw)}.1>)")
            empty = probe_status == "OK" and any(
                (isinstance(row, tuple) and row[1] == b"" and
                 re.search(rb"\bUID " + str(uid).encode() + rb"\b", row[0])) or
                (isinstance(row, bytes) and b"BODY[]<" in row and b'""' in row and
                 re.search(rb"\bUID " + str(uid).encode() + rb"\b", row))
                for row in probe_rows
            )
            if not empty:
                raise DraftError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
        return raw, flags[1].decode("ascii").split()

    def _source(self, reader, reply, account):
        required = {"mailbox", "uidvalidity", "uid"}
        if not isinstance(reply, dict) or set(reply) not in (required, required | {"account"}):
            raise DraftError("INVALID_REPLY_REFERENCE")
        if reply.get("account", account) != account:
            raise DraftError("REPLY_ACCOUNT_MISMATCH")
        raw, _ = self._raw(reader, **{k: reply[k] for k in required})
        message = email.message_from_bytes(raw, policy=policy.default)
        for name in ("Message-ID", "From", "Reply-To", "To", "Cc", "Subject", "Date", "References", "In-Reply-To"):
            if len(message.get_all(name, [])) > 1:
                raise DraftError("AMBIGUOUS_SOURCE_HEADERS")
        message_id = str(message.get("Message-ID", ""))
        if not MESSAGE_ID.fullmatch(message_id):
            raise DraftError("SOURCE_MESSAGE_ID_REQUIRED")
        return message, {**{k: str(reply[k]) for k in required}, "account": account,
                         "content_sha256": digest(raw), "message_id": message_id}

    def _file(self, value, maximum):
        path = self._resolve(value).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > maximum:
            raise DraftError("FILE_TOO_LARGE_OR_UNAVAILABLE")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise DraftError("FILE_TOO_LARGE_OR_UNAVAILABLE")
        return path, data

    def _signature(self, entry):
        config = entry["signature"]
        if config.get("none") is True:
            if set(config) != {"none"}:
                raise DraftError("INVALID_SIGNATURE_CONFIGURATION")
            return {"mode": "none", "plain": "", "html": "", "inline": []}, []
        if not config.get("plain"):
            raise DraftError("SIGNATURE_PLAIN_REQUIRED")
        _, plain = self._file(config["plain"], MAX_SIGNATURE)
        plain = plain.decode("utf-8").strip("\r\n")
        if not plain or "\0" in plain:
            raise DraftError("INVALID_SIGNATURE")
        if config.get("html"):
            _, rich = self._file(config["html"], MAX_SIGNATURE)
            rich = rich.decode("utf-8").strip()
        else:
            rich = html.escape(plain).replace("\n", "<br>\n")
        inline = config.get("inline", {})
        if not isinstance(inline, dict) or len(inline) > MAX_ATTACHMENTS:
            raise DraftError("INVALID_SIGNATURE_CONFIGURATION")
        assets = []
        for cid, value in inline.items():
            if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,160}", cid):
                raise DraftError("INVALID_CONTENT_ID")
            path, data = self._file(value, 2 * 1024 * 1024)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if not content_type.startswith("image/") or f"cid:{cid}" not in rich:
                raise DraftError("INVALID_SIGNATURE_IMAGE")
            assets.append({"cid": cid, "filename": path.name, "type": content_type,
                           "data": data})
        summaries = [{k: a[k] for k in ("cid", "filename", "type")} | {"bytes": len(a["data"]), "sha256": digest(a["data"])} for a in assets]
        return {"mode": "configured", "plain": plain, "html": rich, "inline": summaries}, assets

    def _attachments(self, entry, requested):
        requested = [] if requested is None else requested
        if not isinstance(requested, list) or len(requested) > MAX_ATTACHMENTS:
            raise DraftError("ATTACHMENT_LIMIT")
        roots = [self._resolve(p).resolve(strict=True) for p in entry.get("attachment_roots", [])]
        results = []
        total = 0
        for value in requested:
            if not isinstance(value, str):
                raise DraftError("INVALID_ATTACHMENT")
            path = self._resolve(value).resolve(strict=True)
            if not any(path != root and path.is_relative_to(root) for root in roots):
                raise DraftError("ATTACHMENT_OUTSIDE_ALLOWED_ROOTS")
            path, data = self._file(str(path), MAX_ATTACHMENT)
            total += len(data)
            if total > MAX_ATTACHMENT_TOTAL:
                raise DraftError("ATTACHMENT_LIMIT")
            header(path.name)
            results.append({"path": str(path), "filename": path.name, "bytes": len(data),
                            "sha256": digest(data), "type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                            "data": data})
        return results

    @public
    def preview(self, account, body_text, from_address=None, to=None, cc=None,
                subject=None, attachments=None, reply=None, reply_all=False):
        config = self._config()
        entry, profile = self._account(config, account, write=True)
        if not isinstance(body_text, str) or not body_text.strip() or len(body_text) > MAX_BODY or "\0" in body_text:
            raise DraftError("INVALID_BODY")
        if not isinstance(reply_all, bool) or (reply_all and reply is None):
            raise DraftError("INVALID_REPLY_MODE")
        if isinstance(reply, dict) and reply.get("account", account) != account:
            raise DraftError("REPLY_ACCOUNT_MISMATCH")
        selected_from = bare_address(from_address or entry["default_from"])
        if selected_from not in entry["from_addresses"]:
            raise DraftError("FROM_NOT_ALLOWED")
        signature, inline = self._signature(entry)
        files = self._attachments(entry, attachments)
        source = None
        quoted = ""
        source_message = None
        with self._reader(profile) as reader:
            drafts_mailbox, drafts_validity = self._drafts(reader, entry)
            if reply is not None:
                source_message, source = self._source(reader, reply, account)
        if source_message is not None:
            own = {address.casefold() for address in entry["from_addresses"]}
            original_sender = addresses(str(source_message.get("Reply-To") or source_message.get("From", "")))
            if not original_sender:
                raise DraftError("SOURCE_SENDER_REQUIRED")
            derived_to = original_sender
            derived_cc = []
            if reply_all:
                derived_to += addresses(str(source_message.get("To", ""))) if source_message.get("To") else []
                derived_cc = addresses(str(source_message.get("Cc", ""))) if source_message.get("Cc") else []
            derived_to = [a for a in derived_to if a["address"].casefold() not in own]
            derived_cc = [a for a in derived_cc if a["address"].casefold() not in own]
            # Explicit recipients are reviewable overrides, never inferred from body text.
            to = [a["formatted"] for a in derived_to] if to is None else to
            cc = [a["formatted"] for a in derived_cc] if cc is None else cc
            original_subject = header(str(source_message.get("Subject", "")))
            if subject is None:
                subject = original_subject if re.match(r"(?i)^re\s*:", original_subject) else "Re: " + original_subject
            quote_headers = [f"{name}: {str(source_message[name])}" for name in ("Date", "From", "To", "Cc", "Subject") if source_message.get(name)]
            quoted = "\n".join(["--- Original message ---", *quote_headers, "", text_part(source_message)])
        recipients = addresses(to)
        copies = addresses(cc)
        seen = {a["address"].casefold() for a in recipients}
        copies = [a for a in copies if a["address"].casefold() not in seen]
        if not recipients:
            raise DraftError("RECIPIENT_REQUIRED")
        subject = header(subject) if subject is not None else ""
        if not subject.strip():
            raise DraftError("SUBJECT_REQUIRED")
        authored = body_text.replace("\r\n", "\n").replace("\r", "\n").rstrip()
        plain_signature = signature["plain"]
        # The adapter owns the new signature; a caller may already have included it.
        if plain_signature:
            while authored.endswith(plain_signature):
                authored = authored[:-len(plain_signature)].rstrip()
        signed_plain = authored + ("\n\n" + plain_signature if plain_signature else "")
        signed_html = '<div style="white-space:pre-wrap">' + html.escape(authored) + "</div>"
        if signature["html"]:
            signed_html += '<div class="imap-mail-signature">' + signature["html"] + "</div>"
        if quoted:
            signed_plain += "\n\n" + "\n".join("> " + line for line in quoted.splitlines())
            signed_html += '<blockquote style="white-space:pre-wrap">' + html.escape(quoted) + "</blockquote>"
        # RFC 2045 limits quoted-printable body lines to 76 characters, not
        # SMTP policy's default 78. This is standards compliance, not an
        # assertion that it resolves a particular native-client defect.
        message = EmailMessage(policy=policy.SMTP.clone(max_line_length=76))
        message["From"] = selected_from
        message["To"] = ", ".join(a["formatted"] for a in recipients)
        if copies:
            message["Cc"] = ", ".join(a["formatted"] for a in copies)
        message["Subject"] = subject
        message["Date"] = format_datetime(dt.datetime.now(dt.timezone.utc))
        message["Message-ID"] = make_msgid(domain=selected_from.rsplit("@", 1)[1])
        if source:
            message["In-Reply-To"] = source["message_id"]
            references = str(source_message.get("References", ""))
            if not references:
                references = str(source_message.get("In-Reply-To", ""))
            ids = MESSAGE_ID.findall(references)
            ids = list(dict.fromkeys([*ids, source["message_id"]]))
            if len(ids) > 100:
                raise DraftError("REFERENCES_TOO_LONG")
            message["References"] = " ".join(ids)
        message.set_content(signed_plain)
        message.add_alternative(signed_html, subtype="html")
        html_part = message.get_payload()[-1]
        for asset in inline:
            main, sub = asset["type"].split("/", 1)
            html_part.add_related(asset["data"], maintype=main, subtype=sub,
                                  cid="<" + asset["cid"] + ">", filename=asset["filename"], disposition="inline")
        for item in files:
            main, sub = item["type"].split("/", 1)
            message.add_attachment(item["data"], maintype=main, subtype=sub, filename=item["filename"])
        raw = message.as_bytes()
        if len(raw) > MAX_RAW:
            raise DraftError("DRAFT_TOO_LARGE")
        preview_id = uuid.uuid4().hex
        review = {"account": account, "identity": profile_identity(profile),
                  "from": selected_from, "to": [a["formatted"] for a in recipients],
                  "cc": [a["formatted"] for a in copies], "subject": subject,
                  "body_text": body_text, "rendered_body_text": signed_plain,
                  "signature": signature,
                  "attachments": [{k: v for k, v in item.items() if k not in ("data", "path")} for item in files],
                  "reply": source, "reply_all": reply_all, "message_id": str(message["Message-ID"]),
                  "drafts_mailbox": drafts_mailbox, "drafts_uidvalidity": drafts_validity}
        immutable = {"preview_id": preview_id, "review": review, "mime_sha256": digest(raw),
                     "account_config_sha256": digest(entry)}
        preview_hash = digest(immutable)
        record = {"version": 1, "immutable": immutable, "preview_hash": preview_hash,
                  "state": "prepared", "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "receipt": None}
        with self._lock(config) as root:
            write_new(root / (preview_id + ".eml"), raw)
            write_new(root / (preview_id + ".json"), json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return {"status": "prepared", "preview_id": preview_id, "preview_hash": preview_hash, **review, "mailbox_writes": 0}

    def _load(self, root, preview_id):
        if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
            raise DraftError("INVALID_PREVIEW_ID")
        path = root / (preview_id + ".json")
        record = json.loads(private_read(path, 4 * 1024 * 1024))
        raw = private_read(root / (preview_id + ".eml"))
        immutable = record["immutable"]
        if immutable["preview_id"] != preview_id or digest(immutable) != record["preview_hash"] or digest(raw) != immutable["mime_sha256"]:
            raise DraftError("PREVIEW_INTEGRITY_FAILED")
        return path, record, raw

    def _bound(self, config, record):
        immutable = record["immutable"]
        review = immutable["review"]
        entry, profile = self._account(config, review["account"], write=True)
        if profile_identity(profile) != review["identity"]:
            raise DraftError("ACCOUNT_IDENTITY_CHANGED")
        if digest(entry) != immutable["account_config_sha256"]:
            raise DraftError("ACCOUNT_CONFIGURATION_CHANGED")
        if review["from"] not in entry["from_addresses"]:
            raise DraftError("FROM_NOT_ALLOWED")
        return entry, profile

    def _result(self, record):
        return {"status": record["state"], "preview_id": record["immutable"]["preview_id"],
                "preview_hash": record["preview_hash"], "account": record["immutable"]["review"]["account"],
                "message_id": record["immutable"]["review"]["message_id"], "receipt": record.get("receipt"),
                "mailbox_writes": 0, "append_attempted": record["state"] != "prepared"}

    def _appenduid(self, client, data):
        _, values = client.response("APPENDUID")
        available = [v for v in (values or []) if isinstance(v, bytes) and v]
        if not available:
            available = [m[1] for row in (data or []) if isinstance(row, bytes)
                         for m in [re.search(rb"\[APPENDUID ([^]]+)\]", row)] if m]
        if not available:
            return None
        if len(available) != 1:
            raise DraftError("APPENDUID_INVALID")
        value = available[0].split(b"]", 1)[0]
        match = re.fullmatch(rb"([1-9][0-9]*) ([1-9][0-9]*)", value)
        if not match:
            raise DraftError("APPENDUID_INVALID")
        return {"uidvalidity": match[1].decode(), "uid": match[2].decode()}

    def _reconcile(self, path, record, raw, entry, profile):
        review = record["immutable"]["review"]
        try:
            if record.get("appenduid_invalid"):
                raise DraftError("APPENDUID_INVALID")
            with self._reader(profile) as check:
                folder, validity = self._drafts(check, entry)
                if folder != review["drafts_mailbox"]:
                    raise DraftError("DRAFTS_FOLDER_CHANGED")
                mapping = record.get("mapping")
                if mapping:
                    if mapping["uidvalidity"] != validity:
                        raise DraftError("DRAFTS_UIDVALIDITY_CHANGED")
                    uid = mapping["uid"]
                else:
                    status, rows = check.client.uid("SEARCH", None, "HEADER", "Message-ID", check.client._quote(review["message_id"]))
                    matches = (rows[0] or b"").split() if status == "OK" and rows else []
                    if len(matches) != 1 or not STABLE_ID.fullmatch(matches[0].decode("ascii")):
                        raise DraftError("DRAFT_NOT_FOUND_UNIQUELY")
                    uid = matches[0].decode("ascii")
                saved, flags = self._raw(check, folder, validity, uid)
                normalized_flags = {flag.casefold() for flag in flags}
                if "\\deleted" in normalized_flags:
                    raise DraftError("DRAFT_DELETED_OR_NOT_ACTIVE")
                if "\\draft" not in normalized_flags:
                    raise DraftError("DRAFT_FLAG_NOT_CONFIRMED")
                if semantic(saved) != semantic(raw):
                    raise DraftError("DRAFT_CONTENT_MISMATCH")
                record["state"] = "verified"
                record.pop("error_code", None)
                record["receipt"] = {"account": review["account"], "mailbox": folder,
                                     "uidvalidity": validity, "uid": uid,
                                     "message_id": review["message_id"], "semantic_sha256": digest(semantic(saved)),
                                     "flags": flags, "verified_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        except Exception as exc:
            record["state"] = "uncertain"
            record["error_code"] = exc.code if isinstance(exc, DraftError) else "READBACK_FAILED"
        write_json(path, record)
        result = self._result(record)
        if record.get("error_code"):
            result["error_code"] = record["error_code"]
        return result

    @public
    def apply(self, preview_id, preview_hash):
        config = self._config()
        with self._lock(config) as root:
            path, record, raw = self._load(root, preview_id)
            if record["immutable"].get("operation", "draft") != "draft":
                raise DraftError("PREVIEW_OPERATION_MISMATCH")
            if not isinstance(preview_hash, str) or preview_hash != record["preview_hash"]:
                raise DraftError("PREVIEW_HASH_MISMATCH")
            entry, profile = self._bound(config, record)
            if record["state"] in ("attempted", "uncertain", "verified"):
                return self._reconcile(path, record, raw, entry, profile)
            if record["state"] != "prepared":
                raise DraftError("INVALID_DRAFT_STATE")
            review = record["immutable"]["review"]
            with self._reader(profile) as reader:
                folder, validity = self._drafts(reader, entry)
                if folder != review["drafts_mailbox"] or validity != review["drafts_uidvalidity"]:
                    raise DraftError("DRAFTS_FOLDER_CHANGED")
                if review["reply"]:
                    source = review["reply"]
                    if source.get("account", review["account"]) != review["account"]:
                        raise DraftError("REPLY_ACCOUNT_MISMATCH")
                    current, _ = self._raw(reader, source["mailbox"], source["uidvalidity"], source["uid"])
                    if digest(current) != source["content_sha256"]:
                        raise DraftError("SOURCE_MESSAGE_CHANGED")
                # Network preflight can take time; do not use a reassigned account
                # or a changed From/folder policy for a previously reviewed MIME.
                self._bound(self._config(), record)
                record["state"] = "attempted"
                record["attempted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                write_json(path, record)  # Durable before APPEND; never reset to prepared.
                try:
                    # Uploading the allowed MIME payload may take longer than a
                    # small read. Bound this socket wait, without retrying APPEND.
                    upload_socket = getattr(reader.client, "sock", None)
                    if upload_socket is not None:
                        upload_socket.settimeout(60)
                    status, data = reader.client.append(reader.client._quote(read_access.utf7_encode(folder)), "(\\Draft)", None, raw)
                    if status != "OK":
                        raise DraftError("APPEND_NOT_CONFIRMED")
                    try:
                        record["mapping"] = self._appenduid(reader.client, data)
                    except DraftError:
                        record["appenduid_invalid"] = True
                        raise
                    write_json(path, record)
                except Exception as exc:
                    record["state"] = "uncertain"
                    record["append_error_code"] = exc.code if isinstance(exc, DraftError) else "APPEND_RESPONSE_UNCERTAIN"
                    record["append_error_type"] = type(exc).__name__
                    write_json(path, record)
            result = self._reconcile(path, record, raw, entry, profile)
            result["mailbox_writes"] = 1
            return result

    @public
    def status(self, preview_id):
        config = self._config()
        with self._lock(config) as root:
            path, record, raw = self._load(root, preview_id)
            if record["immutable"].get("operation", "draft") != "draft":
                raise DraftError("PREVIEW_OPERATION_MISMATCH")
            entry, profile = self._bound(config, record)
            if record["state"] in ("attempted", "uncertain", "verified"):
                return self._reconcile(path, record, raw, entry, profile)
            return self._result(record)
