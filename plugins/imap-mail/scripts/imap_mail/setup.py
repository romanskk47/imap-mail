"""Explicit new-account setup with read-only checks and private local storage.

Passwords are accepted only in memory for a confirmed host, never returned or
logged. The sole persistence action is save(check_id), called after UI consent.
No mailbox mutation, keychain import, SMTP, OAuth or discovery network is used.
"""

from contextlib import contextmanager
import copy
import fcntl
import functools
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid

from . import read_access
from .core import DraftError, bare_address


CHECK_LIFETIME = 15 * 60
MAX_CHECK_ATTEMPTS = 3
MAX_CONFIG_BYTES = 1024 * 1024
MAX_SIGNATURE_TEXT = 20_000
ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
KNOWN_PROVIDERS = {
    "gmail.com": ("gmail", "imap.gmail.com"),
    "googlemail.com": ("gmail", "imap.gmail.com"),
    "icloud.com": ("icloud", "imap.mail.me.com"),
    "me.com": ("icloud", "imap.mail.me.com"),
    "mac.com": ("icloud", "imap.mail.me.com"),
    "yahoo.com": ("yahoo", "imap.mail.yahoo.com"),
    "ymail.com": ("yahoo", "imap.mail.yahoo.com"),
}


class SetupError(DraftError):
    """Fixed public diagnostic code; never arbitrary exception text."""


def guarded(method):
    @functools.wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except SetupError:
            raise
        except DraftError as exc:
            raise SetupError(exc.code) from None
        except Exception:
            raise SetupError("SETUP_OPERATION_FAILED") from None
    return call


def _text(value, maximum, code, multiline=False):
    if not isinstance(value, str) or not value or len(value) > maximum or any(
        (ord(c) < 32 and not (multiline and c in "\r\n\t")) or ord(c) == 127 for c in value
    ):
        raise SetupError(code)
    return value


def _validated_host(value):
    host = _text(value, 253, "INVALID_IMAP_HOST").lower()
    labels = host.split(".")
    if len(labels) < 2 or not all(HOST_LABEL.fullmatch(label) for label in labels):
        raise SetupError("INVALID_IMAP_HOST")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SetupError("INVALID_IMAP_HOST")
    if labels[-1] in {"local", "localhost", "internal", "lan", "home", "invalid"} or host.endswith(".arpa"):
        raise SetupError("INVALID_IMAP_HOST")
    return host


@guarded
def validate_prefill(prefill):
    """Validate optional agent-supplied public fields, without I/O or consent.

    The agent supplies confirmed context. This helper does not infer an account,
    guess a legal entity, accept credentials, or confirm a host for the person.
    """
    if prefill is None:
        return {}
    if not isinstance(prefill, dict) or set(prefill) - {"email", "account", "entity", "host"}:
        raise SetupError("INVALID_SETUP_PREFILL")
    result = {}
    if "email" in prefill:
        try:
            result["email"] = bare_address(_text(prefill["email"], 254, "INVALID_EMAIL"))
        except DraftError:
            raise SetupError("INVALID_EMAIL") from None
    if "account" in prefill:
        account = _text(prefill["account"], 80, "INVALID_ACCOUNT_ID")
        if not ACCOUNT_ID.fullmatch(account):
            raise SetupError("INVALID_ACCOUNT_ID")
        result["account"] = account
    if "entity" in prefill:
        entity = _text(prefill["entity"], 120, "INVALID_ENTITY").strip()
        if not entity:
            raise SetupError("INVALID_ENTITY")
        result["entity"] = entity
    if "host" in prefill:
        result["host"] = _validated_host(prefill["host"])
    return result


@guarded
def known_provider_host(email):
    """Return only an exact static domain match; never resolve or guess a host."""
    if email is None:
        return None
    try:
        email = validate_prefill({"email": email})["email"]
    except DraftError:
        return None
    provider = KNOWN_PROVIDERS.get(email.rsplit("@", 1)[1].lower())
    return provider[1] if provider else None


def _path(value, base=None):
    if not isinstance(value, (str, os.PathLike)):
        raise SetupError("UNSAFE_SETUP_PATH")
    rendered = os.fspath(value)
    _text(rendered, 4096, "UNSAFE_SETUP_PATH")
    path = Path(rendered).expanduser()
    if ".." in path.parts:
        raise SetupError("UNSAFE_SETUP_PATH")
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path


def _walk(path):
    """Reject symlinks anywhere in a supplied path, including broken links."""
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise SetupError("UNSAFE_SETUP_PATH")
        if index < len(path.parts) - 2 and not stat.S_ISDIR(info.st_mode):
            raise SetupError("UNSAFE_SETUP_PATH")
    return path


def _private_file(path, maximum=MAX_CONFIG_BYTES):
    _walk(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SetupError("PRIVATE_FILE_PERMISSIONS_REQUIRE_600")
    if info.st_size > maximum:
        raise SetupError("SETUP_FILE_TOO_LARGE")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise SetupError("SETUP_FILE_TOO_LARGE")
    return data


def _private_directory(path, created=None):
    _walk(path)
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SetupError("PRIVATE_DIRECTORY_PERMISSIONS_REQUIRE_700")
        return
    if created is None:
        return
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        info = directory.stat()
        created.append((directory, info.st_dev, info.st_ino))


def _json(data):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SetupError("INVALID_SETUP_CONFIGURATION")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=unique_pairs)
    except SetupError:
        raise
    except Exception:
        raise SetupError("INVALID_SETUP_CONFIGURATION") from None


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _new_file(path, data, created):
    _walk(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise SetupError("ACCOUNT_FILES_EXIST") from None
    info = os.fstat(descriptor)
    position = len(created)
    created.append((path, info.st_dev, info.st_ino, None))
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    created[position] = (path, info.st_dev, info.st_ino, _digest(data))


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback(files, directories):
    # Remove only this transaction's unchanged files, never a replaced object.
    for path, device, inode, expected in reversed(files):
        try:
            info = path.lstat()
            if (stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode)
                    and (expected is None or _digest(path.read_bytes()) == expected)):
                path.unlink()
        except OSError:
            pass
    for path, device, inode in reversed(directories):
        try:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode):
                path.rmdir()
        except OSError:
            pass


class SetupSession:
    def __init__(self, config_path=None, reader_factory=None):
        self.config_path = _path(config_path or os.environ.get("MAIL_DRAFTS_CONFIG", "~/.config/imap-mail/settings.json"))
        _walk(self.config_path)
        self.reader_factory = reader_factory or read_access.Reader
        self._candidates = {}
        self._attempts = 0
        self._closed = False
        self._mutex = threading.RLock()

    def _clear(self):
        for candidate in self._candidates.values():
            candidate["payload"]["password"] = ""
        self._candidates.clear()

    def _expire(self):
        for check_id in list(self._candidates):
            if time.monotonic() - self._candidates[check_id]["created"] >= CHECK_LIFETIME:
                self._candidates[check_id]["payload"]["password"] = ""
                del self._candidates[check_id]

    def close(self):
        with self._mutex:
            self._clear()
            self._closed = True

    def _open(self):
        if self._closed:
            raise SetupError("SETUP_SESSION_CLOSED")
        self._expire()

    @guarded
    def suggest(self, email):
        with self._mutex:
            self._open()
            email = bare_address(_text(email, 254, "INVALID_EMAIL"))
            domain = email.rsplit("@", 1)[1].lower()
            result = {"status": "manual", "email": email, "port": 993,
                      "tls": True, "host_confirmed": False}
            host = known_provider_host(email)
            if host is not None:
                result.update(status="suggested", provider=KNOWN_PROVIDERS[domain][0], host=host)
            return result

    def _environment(self):
        if os.environ.get("MAIL_BROKER_CONFIG"):
            raise SetupError("BROKER_SETUP_NOT_SUPPORTED")
        if "MAIL_PROFILES_JSON" in os.environ:
            raise SetupError("INJECTED_PROFILES_SETUP_NOT_SUPPORTED")

    def _existing(self):
        self._environment()
        _walk(self.config_path)
        _private_directory(self.config_path.parent)
        original = _private_file(self.config_path) if self.config_path.exists() else None
        config = _json(original) if original is not None else {"version": 1, "accounts": {}}
        if (not isinstance(config, dict) or type(config.get("version")) is not int
                or config["version"] != 1 or not isinstance(config.get("accounts"), dict)):
            raise SetupError("INVALID_SETUP_CONFIGURATION")
        if config.get("broker_config"):
            raise SetupError("BROKER_SETUP_NOT_SUPPORTED")
        profiles_dir = _path(config.get("profiles_dir") or os.environ.get("MAIL_PROFILES_DIR", "~/.secrets/services/mail"), self.config_path.parent)
        _private_directory(profiles_dir)
        if profiles_dir.exists():
            for path in profiles_dir.rglob("*.json"):
                _walk(path)
                if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
                    raise SetupError("PROFILE_DISCOVERY_FAILED")
        try:
            profiles = read_access.profiles(str(profiles_dir))
        except Exception:
            raise SetupError("PROFILE_DISCOVERY_FAILED") from None
        return config, original, profiles_dir, profiles

    def _host(self, value):
        # This is an owner-confirmed hostname, not a DNS provider guess. The
        # reader still verifies the server's TLS certificate for this exact host.
        return _validated_host(value)

    def _attachment_root(self, value, profiles_dir=None, profiles=None):
        if value is None or value == "":
            return None
        root = _path(value, self.config_path.parent)
        _walk(root)
        home = Path.home().absolute()
        config_root = self.config_path.parent
        protected = [config_root]
        if profiles_dir is not None:
            protected.append(profiles_dir)
        for profile in (profiles or {}).values():
            if profile.get("password_file"):
                protected.append(_path(profile["password_file"]))
        if (root == Path(root.anchor) or root == home or any(
                root == path or root.is_relative_to(path) or path.is_relative_to(root)
                for path in protected)):
            raise SetupError("ATTACHMENT_ROOT_TOO_BROAD")
        if not root.is_dir() or root.stat().st_uid != os.getuid():
            raise SetupError("ATTACHMENT_ROOT_UNAVAILABLE")
        return str(root)

    def _payload(self, payload, profiles_dir=None, profiles=None):
        allowed = {"account", "email", "entity", "host", "port", "tls", "password", "host_confirmed",
                   "signature_text", "signature_none", "attachment_root", "drafts_mailbox"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise SetupError("INVALID_SETUP_INPUT")
        account = _text(payload.get("account"), 80, "INVALID_ACCOUNT_ID")
        if not ACCOUNT_ID.fullmatch(account):
            raise SetupError("INVALID_ACCOUNT_ID")
        email = bare_address(_text(payload.get("email"), 254, "INVALID_EMAIL"))
        entity = _text(payload.get("entity"), 120, "INVALID_ENTITY").strip()
        if not entity:
            raise SetupError("INVALID_ENTITY")
        if payload.get("host_confirmed") is not True:
            raise SetupError("HOST_CONFIRMATION_REQUIRED")
        host = self._host(payload.get("host"))
        if type(payload.get("port", 993)) is not int or payload.get("port", 993) != 993 or payload.get("tls", True) is not True:
            raise SetupError("TLS_993_REQUIRED")
        password = _text(payload.get("password"), 4096, "INVALID_PASSWORD")
        no_signature = payload.get("signature_none", False)
        signature = payload.get("signature_text", "")
        if not isinstance(no_signature, bool) or not isinstance(signature, str):
            raise SetupError("INVALID_SIGNATURE_CHOICE")
        if no_signature:
            if signature.strip():
                raise SetupError("INVALID_SIGNATURE_CHOICE")
            signature = ""
        else:
            signature = _text(signature, MAX_SIGNATURE_TEXT, "SIGNATURE_CHOICE_REQUIRED", multiline=True).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            if not signature.strip():
                raise SetupError("SIGNATURE_CHOICE_REQUIRED")
        folder = payload.get("drafts_mailbox")
        if folder is not None:
            folder = _text(folder, 1024, "INVALID_DRAFTS_MAILBOX")
        return {"account": account, "email": email, "entity": entity, "host": host,
                "port": 993, "tls": True, "password": password,
                "signature_text": signature, "signature_none": no_signature,
                "attachment_root": self._attachment_root(payload.get("attachment_root"), profiles_dir, profiles),
                "drafts_mailbox": folder}

    def _new_identity(self, payload, config, profiles_dir, profiles):
        account, email = payload["account"], payload["email"].casefold()
        if account in profiles or account in config["accounts"]:
            raise SetupError("ACCOUNT_ALREADY_EXISTS")
        if any(str(profile.get("user", "")).casefold() == email for profile in profiles.values()):
            raise SetupError("EMAIL_ALREADY_CONFIGURED")
        for entry in config["accounts"].values():
            if isinstance(entry, dict) and isinstance(entry.get("identity"), dict) and str(entry["identity"].get("email", "")).casefold() == email:
                raise SetupError("EMAIL_ALREADY_CONFIGURED")
        profile_path = profiles_dir / (account + ".json")
        account_dir = self.config_path.parent / "accounts" / account
        _walk(profile_path)
        _walk(account_dir)
        if profile_path.exists() or account_dir.exists():
            raise SetupError("ACCOUNT_FILES_EXIST")

    def _drafts(self, reader, override):
        folders = reader.folders()["folders"]
        selectable = [folder for folder in folders if folder.get("selectable", True)]
        names = [folder["name"] for folder in selectable if folder.get("kind") == "drafts" or any(str(flag).casefold() == "\\drafts" for flag in folder.get("flags", []))]
        if len(names) > 1:
            raise SetupError("DRAFTS_FOLDER_AMBIGUOUS")
        if override is not None:
            if len([folder for folder in selectable if folder["name"] == override]) != 1:
                raise SetupError("DRAFTS_FOLDER_UNAVAILABLE")
            if names and names[0] != override:
                raise SetupError("DRAFTS_FOLDER_CONFLICT")
        mailbox = names[0] if names else override
        if mailbox is None:
            raise SetupError("DRAFTS_FOLDER_UNAVAILABLE")
        _text(mailbox, 1024, "INVALID_DRAFTS_MAILBOX")
        validity, _ = reader.select(mailbox)  # Reader uses EXAMINE, never SELECT rw.
        if not re.fullmatch(r"[1-9][0-9]{0,18}", str(validity)):
            raise SetupError("UIDVALIDITY_UNAVAILABLE")
        return mailbox, str(validity)

    @guarded
    def check(self, payload):
        with self._mutex:
            self._open()
            self._clear()
            self._attempts += 1
            if self._attempts > MAX_CHECK_ATTEMPTS:
                raise SetupError("CHECK_ATTEMPT_LIMIT")
            normalized = None
            try:
                config, _, profiles_dir, profiles = self._existing()
                normalized = self._payload(payload, profiles_dir, profiles)
                self._new_identity(normalized, config, profiles_dir, profiles)
                profile = {"id": normalized["account"], "host": normalized["host"],
                           "user": normalized["email"], "entity": normalized["entity"],
                           "port": 993, "tls": True, "password": normalized["password"]}
                reader = None
                try:
                    reader = self.reader_factory(profile)
                    mailbox, validity = self._drafts(reader, normalized["drafts_mailbox"])
                except SetupError:
                    raise
                except Exception:
                    raise SetupError("SETUP_CONNECTION_CHECK_FAILED") from None
                finally:
                    profile["password"] = ""
                    if reader is not None:
                        try:
                            reader.close()
                        except Exception:
                            pass
                check_id = uuid.uuid4().hex
                summary = {"status": "checked", "check_id": check_id,
                           "expires_in_seconds": CHECK_LIFETIME,
                           "account": normalized["account"], "email": normalized["email"],
                           "host": normalized["host"], "port": 993, "tls": True,
                           "entity": normalized["entity"], "drafts_mailbox": mailbox,
                           "uidvalidity": validity,
                           "signature_mode": "none" if normalized["signature_none"] else "configured",
                           "attachment_root": normalized["attachment_root"], "mailbox_writes": 0}
                self._candidates[check_id] = {"created": time.monotonic(), "payload": normalized,
                                              "summary": summary, "profiles_dir": str(profiles_dir)}
                return copy.deepcopy(summary)
            except Exception:
                if normalized is not None:
                    normalized["password"] = ""
                self._clear()
                raise

    @contextmanager
    def _lock(self, directories):
        _private_directory(self.config_path.parent, directories)
        path = self.config_path.parent / ".setup.lock"
        _walk(path)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SetupError("PRIVATE_FILE_PERMISSIONS_REQUIRE_600")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _commit_config(self, path, config, expected_bytes):
        current = _private_file(path) if path.exists() else None
        if current != expected_bytes:
            raise SetupError("SETUP_CONFIGURATION_CHANGED")
        descriptor, temporary = tempfile.mkstemp(prefix=".setup-config-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            _walk(path)
            if expected_bytes is None and path.exists():
                raise SetupError("SETUP_CONFIGURATION_CHANGED")
            os.replace(temporary, path)  # Last commit point; every new file is durable.
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @guarded
    def save(self, check_id):
        with self._mutex:
            self._open()
            if not isinstance(check_id, str) or not re.fullmatch(r"[a-f0-9]{32}", check_id):
                self._clear()
                raise SetupError("INVALID_CHECK_ID")
            candidate = self._candidates.pop(check_id, None)
            if candidate is None:
                self._clear()
                raise SetupError("CHECK_EXPIRED_OR_UNAVAILABLE")
            payload, summary = candidate["payload"], candidate["summary"]
            files, directories = [], []
            committed = False
            try:
                with self._lock(directories):
                    config, original, profiles_dir, profiles = self._existing()
                    if str(profiles_dir) != candidate["profiles_dir"]:
                        raise SetupError("SETUP_CONFIGURATION_CHANGED")
                    self._new_identity(payload, config, profiles_dir, profiles)
                    self._attachment_root(payload["attachment_root"], profiles_dir, profiles)
                    _private_directory(profiles_dir, directories)
                    accounts_root = self.config_path.parent / "accounts"
                    _private_directory(accounts_root, directories)
                    account_dir = accounts_root / payload["account"]
                    account_dir.mkdir(mode=0o700)  # Exclusive, never reuse account material.
                    info = account_dir.stat()
                    directories.append((account_dir, info.st_dev, info.st_ino))
                    password_path = account_dir / "password.secret"
                    _new_file(password_path, payload["password"].encode("utf-8"), files)
                    if payload["signature_none"]:
                        signature = {"none": True}
                    else:
                        plain_path, html_path = account_dir / "signature.txt", account_dir / "signature.html"
                        _new_file(plain_path, payload["signature_text"].encode("utf-8"), files)
                        rich = "<div>" + html.escape(payload["signature_text"]).replace("\n", "<br>\n") + "</div>"
                        _new_file(html_path, rich.encode("utf-8"), files)
                        signature = {"plain": str(plain_path), "html": str(html_path)}
                    profile_path = profiles_dir / (payload["account"] + ".json")
                    profile = {"id": payload["account"], "host": payload["host"],
                               "port": 993, "tls": True, "user": payload["email"],
                               "entity": payload["entity"], "password_file": str(password_path)}
                    _new_file(profile_path, json.dumps(profile, ensure_ascii=False, indent=2).encode("utf-8") + b"\n", files)
                    updated = copy.deepcopy(config)
                    updated.setdefault("profiles_dir", str(profiles_dir))
                    updated["accounts"][payload["account"]] = {
                        "identity": {"email": payload["email"], "host": payload["host"], "entity": payload["entity"]},
                        "from_addresses": [payload["email"]], "default_from": payload["email"],
                        "drafts_enabled": True, "drafts_mailbox": summary["drafts_mailbox"],
                        "signature": signature,
                        "attachment_roots": [payload["attachment_root"]] if payload["attachment_root"] else []}
                    _sync_directory(account_dir)
                    _sync_directory(profiles_dir)
                    _sync_directory(accounts_root)
                    self._commit_config(self.config_path, updated, original)
                    committed = True
                    # A directory-fsync error after replace is an uncertain local
                    # durability condition, not permission to remove committed files.
                    try:
                        _sync_directory(self.config_path.parent)
                    except OSError:
                        pass
                return {"status": "saved", "account": payload["account"], "email": payload["email"],
                        "host": payload["host"], "entity": payload["entity"],
                        "drafts_mailbox": summary["drafts_mailbox"], "signature_mode": summary["signature_mode"],
                        "credential_storage": "private_password_file", "mailbox_writes": 0}
            except Exception:
                if not committed:
                    _rollback(files, directories)
                raise
            finally:
                payload["password"] = ""
                self._clear()
