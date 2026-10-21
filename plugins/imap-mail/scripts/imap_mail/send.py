"""Explicitly authorized TLS SMTP submission of frozen, reviewed MIME messages.

SMTP has no idempotency primitive. A durable journal is written before DATA;
after that point this adapter never repeats submission, even on lost replies.
Sent copies are a separate outcome, not evidence of delivery or acceptance.
"""

from contextlib import contextmanager
import datetime as dt
import email
from email import policy
import json
import os
import re
import smtplib
import ssl
import tempfile
import uuid

from . import read_access
from .core import (DraftError, DraftService, MESSAGE_ID, MAX_RAW, MAX_BODY, STABLE_ID,
                   addresses, bare_address, digest, header, profile_identity,
                   public, semantic, text_part, write_json, write_new)


SMTP_TIMEOUT = 30
UPLOAD_TIMEOUT = 60


class _SendComposer(DraftService):
    """Use the common MIME author without granting/depending on draft APPEND."""

    def __init__(self, sender):
        super().__init__(sender.config_path, sender.state_override, sender.reader_factory)
        self.sender = sender

    def _account(self, config, account, write=False, profiles=None):
        return self.sender._send_account(config, account, profiles)

    def _drafts(self, reader, entry):
        # The common author stores these draft-only fields; send_preview removes
        # them before exposing or binding the reviewed send operation.
        return "not_applicable_for_send", "1"


def smtp_factory(settings, context, timeout):
    """Return a TLS-protected SMTP session; never fall back to plaintext."""
    client = None
    try:
        if settings["security"] == "ssl":
            client = smtplib.SMTP_SSL(settings["host"], settings["port"],
                                      context=context, timeout=timeout)
            code, _ = client.ehlo()
            if code != 250:
                raise DraftError("SMTP_EHLO_FAILED")
        else:
            client = smtplib.SMTP(settings["host"], settings["port"], timeout=timeout)
            code, _ = client.ehlo()
            if code != 250 or not client.has_extn("starttls"):
                raise DraftError("SMTP_STARTTLS_REQUIRED")
            code, _ = client.starttls(context=context)
            if code != 220:
                raise DraftError("SMTP_STARTTLS_FAILED")
            code, _ = client.ehlo()
            if code != 250:
                raise DraftError("SMTP_EHLO_FAILED")
        return client
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        raise


class SendService(DraftService):
    def __init__(self, config_path=None, state_dir=None, reader_factory=None,
                 smtp_factory=None):
        super().__init__(config_path, state_dir, reader_factory)
        self.smtp_factory = smtp_factory or globals()["smtp_factory"]

    def _send_account(self, config, account, profiles=None):
        entry, profile = self._account(config, account, profiles=profiles)
        if config.get("broker_config") or os.environ.get("MAIL_BROKER_CONFIG"):
            raise DraftError("BROKER_SEND_ADAPTER_REQUIRED")
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

    @public
    def accounts(self):
        result = super().accounts()
        config = self._config(missing_ok=True)
        if config is None:
            return result
        profiles = self._profiles(config)
        for row in result["accounts"]:
            row.update(send_enabled=False, send_ready=False, send_connection="not_checked", send_issues=[])
            entry = config["accounts"].get(row["account"], {})
            if not isinstance(entry, dict) or entry.get("send_enabled") is not True:
                continue
            row["send_enabled"] = True
            try:
                entry, profile = self._send_account(config, row["account"], profiles)
                self._smtp_settings(entry, profile)
                self._signature(entry)
                row["send_ready"] = True
            except Exception as exc:
                row["send_issues"] = [exc.code if isinstance(exc, DraftError) else "SEND_CONFIGURATION_INVALID"]
        return result

    def _smtp_settings(self, entry, profile):
        if entry.get("send_enabled") is not True:
            raise DraftError("SEND_NOT_AUTHORIZED")
        settings = entry.get("smtp")
        required = {"host", "port", "security", "use_imap_credentials", "sent_copy"}
        if not isinstance(settings, dict) or set(settings) != required:
            raise DraftError("INVALID_SMTP_CONFIGURATION")
        host = settings["host"]
        # No URL, whitespace, shell/protocol syntax or implicit target discovery.
        if (not isinstance(host, str) or len(host) > 253 or
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or
                any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                    for label in host.split("."))):
            raise DraftError("INVALID_SMTP_HOST")
        if type(settings["port"]) is not int or not 1 <= settings["port"] <= 65535:
            raise DraftError("INVALID_SMTP_PORT")
        if settings["security"] not in ("ssl", "starttls"):
            raise DraftError("SMTP_TLS_REQUIRED")
        if settings["use_imap_credentials"] is not True:
            raise DraftError("SMTP_CREDENTIAL_REUSE_NOT_AUTHORIZED")
        if settings["sent_copy"] not in ("server", "append"):
            raise DraftError("INVALID_SENT_COPY_POLICY")
        # Username is not configurable separately: it is pinned to this account.
        return {**settings, "username": profile["user"]}

    def _sent(self, reader, entry):
        selectable = [f for f in reader.folders()["folders"] if f.get("selectable", True)]
        special = [f["name"] for f in selectable
                   if f.get("kind") == "sent" or "\\Sent" in f.get("flags", [])]
        if len(special) != 1:
            raise DraftError("SENT_FOLDER_AMBIGUOUS" if special else "SENT_FOLDER_UNAVAILABLE")
        name = special[0]
        override = entry.get("sent_mailbox")
        if override is not None and header(override) != name:
            raise DraftError("SENT_FOLDER_CONFLICT")
        header(name)
        validity, _ = reader.select(name)
        if not STABLE_ID.fullmatch(str(validity)):
            raise DraftError("SENT_UIDVALIDITY_UNAVAILABLE")
        return name, str(validity)

    @contextmanager
    def _smtp(self, settings, profile):
        client = None
        try:
            client = self.smtp_factory(settings, ssl.create_default_context(), SMTP_TIMEOUT)
            client.login(profile["user"], read_access.password(profile))
            yield client
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass

    @public
    def send_check(self, account):
        """Login/read-only readiness only: never MAIL, RCPT, DATA or APPEND."""
        result = {"account": account, "status": "needs_attention", "connection": False,
                  "smtp_connection": False, "smtp_submissions": 0, "mailbox_writes": 0}
        try:
            config = self._config()
            entry, profile = self._send_account(config, account)
            settings = self._smtp_settings(entry, profile)
            self._signature(entry)
            with self._reader(profile) as reader:
                result["connection"] = True
                folder, validity = self._sent(reader, entry)
            with self._smtp(settings, profile):
                result["smtp_connection"] = True
            result.update(status="ready", identity=profile_identity(profile), smtp=settings,
                          sent_mailbox=folder, sent_uidvalidity=validity, issues=[])
        except Exception as exc:
            code = exc.code if isinstance(exc, DraftError) else "SEND_CHECK_FAILED"
            result.update(error_code=code, issues=[code])
        return result

    @public
    def check_configuration(self, account, smtp):
        """Validate an explicitly human-confirmed SMTP proposal without saving it.

        Internal setup API, not an MCP override: the caller must first obtain
        human confirmation of the host and credential reuse in its local form.
        """
        result = {"account": account, "status": "needs_attention", "connection": False,
                  "smtp_connection": False, "smtp_submissions": 0, "mailbox_writes": 0}
        try:
            config = self._config()
            entry, profile = self._send_account(config, account)
            proposed = {**entry, "send_enabled": True, "smtp": smtp}
            settings = self._smtp_settings(proposed, profile)
            self._signature(proposed)
            with self._reader(profile) as reader:
                result["connection"] = True
                folder, validity = self._sent(reader, proposed)
            with self._smtp(settings, profile):
                result["smtp_connection"] = True
            result.update(status="ready", identity=profile_identity(profile), smtp=settings,
                          sent_mailbox=folder, sent_uidvalidity=validity, issues=[])
        except Exception as exc:
            code = exc.code if isinstance(exc, DraftError) else "SEND_CHECK_FAILED"
            result.update(error_code=code, issues=[code])
        return result

    def _full_raw(self, reader, mailbox, uidvalidity, uid):
        """Freeze full MIME, avoiding provider corruption of partial BODY ranges."""
        header(mailbox)
        if not STABLE_ID.fullmatch(str(uidvalidity)) or not STABLE_ID.fullmatch(str(uid)):
            raise DraftError("INVALID_MESSAGE_REFERENCE")
        actual, _ = reader.select(mailbox)
        if str(actual) != str(uidvalidity):
            raise DraftError("UIDVALIDITY_CHANGED")
        status, rows = reader.client.uid("FETCH", str(uid), "(UID RFC822.SIZE FLAGS BODY.PEEK[])")
        literals = [row for row in rows if isinstance(row, tuple)] if status == "OK" else []
        if len(literals) != 1:
            raise DraftError("MESSAGE_UNAVAILABLE")
        prefix, raw = literals[0]
        match = re.search(rb"\bUID (\d+)\b", prefix)
        size = re.search(rb"RFC822.SIZE (\d+)", prefix)
        flags = re.search(rb"FLAGS \(([^)]*)\)", prefix)
        if not match or match[1].decode() != str(uid) or not size or flags is None:
            raise DraftError("FETCH_IDENTITY_MISMATCH")
        literal = re.search(rb"\bBODY\[\]\s+\{([0-9]+)\}\s*$", prefix, re.IGNORECASE)
        if (not isinstance(raw, bytes) or len(raw) > MAX_RAW or not literal or
                int(literal[1]) != len(raw)):
            raise DraftError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
        if int(size[1]) != len(raw):
            # Known providers retain pre-conversion RFC822.SIZE. A full FETCH
            # still declares the received literal size; independently prove EOF
            # rather than silently trusting either size or a truncated literal.
            status, probe_rows = reader.client.uid("FETCH", str(uid),
                f"(UID BODY.PEEK[]<{len(raw)}.1>)")
            candidates = []
            for row in probe_rows:
                uid_pattern = rb"\bUID " + str(uid).encode() + rb"\b"
                body_pattern = rb"\bBODY\[\]<" + str(len(raw)).encode() + rb">"
                if isinstance(row, tuple) and len(row) == 2:
                    valid = (row[1] == b"" and re.search(uid_pattern, row[0]) and
                             re.search(body_pattern + rb"\s+\{0\}\s*$", row[0], re.IGNORECASE))
                    candidates.append(bool(valid))
                elif isinstance(row, bytes) and re.fullmatch(rb"\s*\)*\s*", row):
                    continue
                elif isinstance(row, bytes):
                    valid = (re.search(uid_pattern, row) and
                             re.search(body_pattern + rb'\s+""\s*\)*\s*$', row, re.IGNORECASE))
                    candidates.append(bool(valid))
                else:
                    candidates.append(False)
            if status != "OK" or candidates != [True]:
                raise DraftError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
        return raw, flags[1].decode("ascii").split()

    def _existing_mime(self, raw, entry):
        message = email.message_from_bytes(raw, policy=policy.default)
        allowed = {"from", "to", "cc", "subject", "date", "message-id", "in-reply-to",
                   "references", "mime-version", "content-type", "content-transfer-encoding",
                   "content-disposition", "x-mailer", "x-uniform-type-identifier",
                   "x-universally-unique-identifier", "x-apple-windows-friendly",
                   "x-apple-auto-saved"}
        if any(name.casefold() not in allowed for name in message.keys()):
            raise DraftError("UNSUPPORTED_DRAFT_HEADERS")
        for name in allowed:
            if len(message.get_all(name, [])) > 1:
                raise DraftError("AMBIGUOUS_DRAFT_HEADERS")
        if any(part.defects for part in message.walk()):
            raise DraftError("MALFORMED_DRAFT_MIME")
        parts = []
        text_size = 0
        for index, part in enumerate(message.walk()):
            if part.is_multipart():
                if part.get_content_type() not in ("multipart/mixed", "multipart/alternative", "multipart/related"):
                    raise DraftError("UNSUPPORTED_DRAFT_MIME")
                parts.append({"index": index, "type": part.get_content_type()})
                continue
            payload = part.get_payload(decode=True) or b""
            summary = {"index": index, "type": part.get_content_type(),
                       "disposition": part.get_content_disposition(), "filename": part.get_filename(),
                       "cid": str(part.get("Content-ID", "")), "bytes": len(payload), "sha256": digest(payload)}
            if part.get_content_maintype() == "text" and part.get_content_disposition() != "attachment":
                if part.get_content_type() not in ("text/plain", "text/html"):
                    raise DraftError("UNSUPPORTED_DRAFT_MIME")
                content = part.get_content()
                text_size += len(content)
                if text_size > MAX_BODY:
                    raise DraftError("DRAFT_BODY_TOO_LARGE_FOR_REVIEW")
                summary["content"] = content
            elif part.get_content_disposition() != "attachment" and part.get_content_maintype() != "image":
                raise DraftError("UNSUPPORTED_DRAFT_MIME")
            parts.append(summary)
        sender = addresses(str(message.get("From", "")))
        if len(sender) != 1 or sender[0]["address"] not in entry["from_addresses"]:
            raise DraftError("FROM_NOT_ALLOWED")
        selected_from = bare_address(sender[0]["address"])
        recipients = addresses(str(message.get("To", "")))
        copies = addresses(str(message.get("Cc", ""))) if message.get("Cc") else []
        if not recipients:
            raise DraftError("RECIPIENT_REQUIRED")
        subject = header(str(message.get("Subject", "")))
        if not subject.strip():
            raise DraftError("SUBJECT_REQUIRED")
        message_id = str(message.get("Message-ID", ""))
        if not MESSAGE_ID.fullmatch(message_id):
            raise DraftError("DRAFT_MESSAGE_ID_REQUIRED")
        html_part = message.get_body(preferencelist=("html",))
        rendered_html = html_part.get_content() if html_part is not None else None
        files = []
        for part in message.walk():
            if not part.is_multipart() and (part.get_content_disposition() == "attachment" or part.get_filename()):
                data = part.get_payload(decode=True) or b""
                files.append({"filename": part.get_filename(), "type": part.get_content_type(),
                              "bytes": len(data), "sha256": digest(data),
                              "disposition": part.get_content_disposition(),
                              "cid": str(part.get("Content-ID", ""))})
        return {"from": selected_from, "to": [a["formatted"] for a in recipients],
                "cc": [a["formatted"] for a in copies], "subject": subject,
                "body_text": text_part(message), "rendered_body_text": text_part(message),
                "rendered_body_html": rendered_html,
                "signature": {"mode": "existing_draft", "preserved": True},
                "attachments": files, "reply": None, "reply_all": False,
                "mime_parts": parts, "message_id": message_id, "preserved_existing_draft": True}

    def _source_key(self, source):
        return tuple(source.get(k) for k in ("account", "mailbox", "uidvalidity", "uid"))

    def _prevent_source_replay(self, root, source, preview_id=None, message_id=None):
        if not source:
            return
        for path in root.glob("*.json"):
            if path.stem == preview_id:
                continue
            _, other, _ = self._load(root, path.stem)
            immutable = other["immutable"]
            previous = immutable.get("draft_source")
            same_source = previous and self._source_key(previous) == self._source_key(source)
            same_message = (message_id is not None and
                            immutable["review"]["account"] == source["account"] and
                            immutable["review"]["message_id"] == message_id)
            if (immutable.get("operation") == "send" and (same_source or same_message) and
                    (other.get("smtp_attempted") is True or other["state"] == "attempted")):
                raise DraftError("DRAFT_SOURCE_SEND_ALREADY_ATTEMPTED")

    @public
    def send_preview(self, account, body_text=None, from_address=None, to=None, cc=None,
                     subject=None, attachments=None, reply=None, reply_all=False, draft=None):
        config = self._config()
        entry, profile = self._send_account(config, account)
        settings = self._smtp_settings(entry, profile)
        source = None
        if draft is not None:
            if (any(v is not None for v in (body_text, from_address, to, cc, subject, attachments, reply))
                    or reply_all is not False):
                raise DraftError("DRAFT_AND_COMPOSE_ARE_EXCLUSIVE")
            required = {"mailbox", "uidvalidity", "uid"}
            if not isinstance(draft, dict) or set(draft) not in (required, required | {"account"}):
                raise DraftError("INVALID_DRAFT_REFERENCE")
            if draft.get("account", account) != account:
                raise DraftError("DRAFT_ACCOUNT_MISMATCH")
            with self._reader(profile) as reader:
                folder, validity = self._drafts(reader, entry)
                if draft["mailbox"] != folder or str(draft["uidvalidity"]) != validity:
                    raise DraftError("DRAFTS_FOLDER_CHANGED")
                raw, flags = self._full_raw(reader, folder, validity, draft["uid"])
                normalized = {flag.casefold() for flag in flags}
                if "\\draft" not in normalized or "\\deleted" in normalized:
                    raise DraftError("DRAFT_FLAG_NOT_CONFIRMED")
            source = {"account": account, "mailbox": folder, "uidvalidity": validity,
                      "uid": str(draft["uid"]), "content_sha256": digest(raw)}
            review = {"account": account, "identity": profile_identity(profile),
                      "drafts_mailbox": folder, "drafts_uidvalidity": validity,
                      **self._existing_mime(raw, entry)}
            preview_id = uuid.uuid4().hex
            immutable = {"preview_id": preview_id, "review": review, "mime_sha256": digest(raw),
                         "account_config_sha256": digest(entry)}
            record = {"version": 1, "immutable": immutable, "state": "prepared",
                      "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "receipt": None}
        else:
            result = _SendComposer(self).preview(account, body_text, from_address, to, cc, subject,
                                                 attachments, reply, reply_all)
            preview_id = result["preview_id"]
            record = raw = None
        with self._lock(config) as root:
            if record is None:
                path, record, raw = self._load(root, preview_id)
                entry, profile = self._send_prebound(self._config(), record)
                # Generate transport-safe new MIME before freezing it. Existing
                # drafts remain byte-for-byte unchanged and negotiate 8BITMIME.
                raw = email.message_from_bytes(raw, policy=policy.default).as_bytes(
                    policy=policy.SMTP.clone(cte_type="7bit"))
                fd, temporary = tempfile.mkstemp(prefix=".send-mime-", dir=root)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, root / (preview_id + ".eml"))
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                record["immutable"]["mime_sha256"] = digest(raw)
            else:
                path = root / (preview_id + ".json")
            self._prevent_source_replay(root, source, message_id=record["immutable"]["review"]["message_id"])
            with self._reader(profile) as reader:
                folder, validity = self._sent(reader, entry)
            immutable = record["immutable"]
            immutable.update(operation="send", smtp=settings,
                             sent={"mailbox": folder, "uidvalidity": validity}, draft_source=source)
            review = immutable["review"]
            review.pop("drafts_mailbox", None)
            review.pop("drafts_uidvalidity", None)
            review.update(operation="send", smtp=settings, sent_mailbox=folder,
                          sent_uidvalidity=validity, draft_source=source)
            record["preview_hash"] = digest(immutable)
            record["smtp_attempted"] = False
            record["sent_copy"] = {"mode": settings["sent_copy"], "status": "not_started"}
            self._send_prebound(self._config(), record)
            if source:
                write_new(root / (preview_id + ".eml"), raw)
            write_json(path, record)
        return {"status": "prepared", "preview_id": preview_id, "preview_hash": record["preview_hash"],
                **review, "smtp_submissions": 0, "mailbox_writes": 0}

    def _send_bound(self, config, record):
        immutable = record["immutable"]
        if immutable.get("operation") != "send" or immutable["review"].get("operation") != "send":
            raise DraftError("SEND_PREVIEW_REQUIRED")
        entry, profile = self._send_prebound(config, record)
        settings = self._smtp_settings(entry, profile)
        if settings != immutable["smtp"] or settings != immutable["review"]["smtp"]:
            raise DraftError("SMTP_CONFIGURATION_CHANGED")
        return entry, profile, settings

    def _send_prebound(self, config, record):
        immutable = record["immutable"]
        review = immutable["review"]
        entry, profile = self._send_account(config, review["account"])
        if profile_identity(profile) != review["identity"]:
            raise DraftError("ACCOUNT_IDENTITY_CHANGED")
        if digest(entry) != immutable["account_config_sha256"]:
            raise DraftError("ACCOUNT_CONFIGURATION_CHANGED")
        if review["from"] not in entry["from_addresses"]:
            raise DraftError("FROM_NOT_ALLOWED")
        return entry, profile

    def _verify_sources(self, reader, entry, record):
        immutable, review = record["immutable"], record["immutable"]["review"]
        folder, validity = self._sent(reader, entry)
        if {"mailbox": folder, "uidvalidity": validity} != immutable["sent"]:
            raise DraftError("SENT_FOLDER_CHANGED")
        source = immutable.get("draft_source")
        if source:
            if source["account"] != review["account"]:
                raise DraftError("DRAFT_ACCOUNT_MISMATCH")
            drafts_folder, drafts_validity = self._drafts(reader, entry)
            if source["mailbox"] != drafts_folder or source["uidvalidity"] != drafts_validity:
                raise DraftError("DRAFTS_FOLDER_CHANGED")
            raw, flags = self._full_raw(reader, source["mailbox"], source["uidvalidity"], source["uid"])
            normalized = {flag.casefold() for flag in flags}
            if "\\draft" not in normalized or "\\deleted" in normalized:
                raise DraftError("DRAFT_FLAG_NOT_CONFIRMED")
            if digest(raw) != source["content_sha256"]:
                raise DraftError("DRAFT_SOURCE_CHANGED")
        if review["reply"]:
            source = review["reply"]
            if source.get("account", review["account"]) != review["account"]:
                raise DraftError("REPLY_ACCOUNT_MISMATCH")
            raw, _ = self._raw(reader, source["mailbox"], source["uidvalidity"], source["uid"])
            if digest(raw) != source["content_sha256"]:
                raise DraftError("SOURCE_MESSAGE_CHANGED")

    def _send_result(self, record):
        result = {"status": record["state"], "operation": "send",
                  "preview_id": record["immutable"]["preview_id"], "preview_hash": record["preview_hash"],
                  "account": record["immutable"]["review"]["account"],
                  "message_id": record["immutable"]["review"]["message_id"],
                  "smtp_attempted": record.get("smtp_attempted", False),
                  "submission_accepted": record["state"] == "submission_accepted", "delivered": False,
                  "receipt": record.get("receipt"), "sent_copy": record.get("sent_copy"),
                  "smtp_submissions": 0, "mailbox_writes": 0}
        if record.get("error_code"):
            result["error_code"] = record["error_code"]
        if record.get("journal_error_code"):
            result["journal_error_code"] = record["journal_error_code"]
        return result

    def _persist_outcome(self, path, record):
        try:
            write_json(path, record)
            return True
        except Exception:
            # An already observed SMTP 250 remains acceptance even if the disk
            # fills up. The durable pre-DATA intent still forbids any resubmit.
            record["journal_error_code"] = "SEND_OUTCOME_PERSISTENCE_FAILED"
            return False

    def _safe_observe_sent(self, path, record, raw, entry, profile):
        try:
            self._observe_sent(path, record, raw, entry, profile)
        except Exception:
            record["sent_copy"].update(status="uncertain", error_code="SENT_COPY_STATUS_UNAVAILABLE")
            self._persist_outcome(path, record)

    def _observe_sent(self, path, record, raw, entry, profile):
        copy = record["sent_copy"]
        review = record["immutable"]["review"]
        try:
            if copy.get("appenduid_invalid"):
                raise DraftError("APPENDUID_INVALID")
            with self._reader(profile) as reader:
                folder, validity = self._sent(reader, entry)
                if {"mailbox": folder, "uidvalidity": validity} != record["immutable"]["sent"]:
                    raise DraftError("SENT_FOLDER_CHANGED")
                mapping = copy.get("mapping")
                if mapping:
                    if mapping["uidvalidity"] != validity:
                        raise DraftError("SENT_UIDVALIDITY_CHANGED")
                    uid = mapping["uid"]
                else:
                    status, rows = reader.client.uid("SEARCH", None, "HEADER", "Message-ID",
                                                     reader.client._quote(review["message_id"]))
                    matches = (rows[0] or b"").split() if status == "OK" and rows else []
                    if status != "OK":
                        raise DraftError("SENT_LOOKUP_FAILED")
                    if not matches:
                        raise DraftError("SENT_NOT_FOUND")
                    if len(matches) != 1 or not STABLE_ID.fullmatch(matches[0].decode("ascii")):
                        raise DraftError("SENT_NOT_FOUND_UNIQUELY")
                    uid = matches[0].decode("ascii")
                saved, flags = self._full_raw(reader, folder, validity, uid)
                normalized = {flag.casefold() for flag in flags}
                if "\\deleted" in normalized or "\\draft" in normalized:
                    raise DraftError("SENT_COPY_NOT_ACTIVE")
                if semantic(saved) != semantic(raw):
                    raise DraftError("SENT_CONTENT_MISMATCH")
                copy.update(status="verified", receipt={"account": review["account"], "mailbox": folder,
                            "uidvalidity": validity, "uid": uid, "message_id": review["message_id"],
                            "semantic_sha256": digest(semantic(saved)), "flags": flags})
                copy.pop("error_code", None)
        except Exception as exc:
            copy["status"] = "uncertain" if copy.get("append_attempted") else "not_observed"
            copy["error_code"] = exc.code if isinstance(exc, DraftError) else "SENT_READBACK_FAILED"
        write_json(path, record)

    def _copy_sent(self, path, record, raw, entry, profile):
        copy = record["sent_copy"]
        # A provider can auto-save even when local configuration expected an
        # APPEND. Never create a duplicate or repair mismatching/ambiguous copies.
        self._observe_sent(path, record, raw, entry, profile)
        if copy["status"] == "verified" or copy.get("error_code") != "SENT_NOT_FOUND":
            return
        if copy["mode"] == "append":
            try:
                with self._reader(profile) as reader:
                    folder, validity = self._sent(reader, entry)
                    if {"mailbox": folder, "uidvalidity": validity} != record["immutable"]["sent"]:
                        raise DraftError("SENT_FOLDER_CHANGED")
                    copy.update(status="attempted", append_attempted=True)
                    copy.pop("error_code", None)
                    write_json(path, record)  # Durable before one permitted Sent APPEND.
                    sock = getattr(reader.client, "sock", None)
                    if sock is not None:
                        sock.settimeout(UPLOAD_TIMEOUT)
                    status, data = reader.client.append(reader.client._quote(read_access.utf7_encode(folder)),
                                                       "(\\Seen)", None, raw)
                    if status != "OK":
                        raise DraftError("SENT_APPEND_NOT_CONFIRMED")
                    try:
                        copy["mapping"] = self._appenduid(reader.client, data)
                    except DraftError:
                        copy["appenduid_invalid"] = True
                        raise
                    write_json(path, record)
            except Exception as exc:
                copy["status"] = "uncertain"
                copy["error_code"] = exc.code if isinstance(exc, DraftError) else "SENT_APPEND_RESPONSE_UNCERTAIN"
                write_json(path, record)
        self._observe_sent(path, record, raw, entry, profile)

    def _envelope(self, record, raw):
        review = record["immutable"]["review"]
        message = email.message_from_bytes(raw, policy=policy.default)
        if message.get("Bcc") or any(name.casefold().startswith("resent-") for name in message.keys()):
            raise DraftError("UNSUPPORTED_SEND_HEADERS")
        sender = bare_address(review["from"])
        recipients = addresses(review["to"]) + addresses(review["cc"])
        envelope = list(dict.fromkeys(a["address"] for a in recipients))
        if not envelope:
            raise DraftError("RECIPIENT_REQUIRED")
        if not all(value.isascii() for value in [sender, *envelope]):
            raise DraftError("SMTPUTF8_NOT_SUPPORTED")
        if not raw.split(b"\r\n\r\n", 1)[0].isascii():
            raise DraftError("SMTPUTF8_NOT_SUPPORTED")
        return sender, envelope

    def _mail_options(self, client, raw):
        if raw.isascii():
            return []
        if not client.has_extn("8bitmime"):
            raise DraftError("SMTP_8BITMIME_REQUIRED")
        return ["BODY=8BITMIME"]

    @public
    def send_apply(self, preview_id, preview_hash, authorization=None):
        if authorization != "send":
            raise DraftError("SEND_AUTHORIZATION_REQUIRED")
        config = self._config()
        with self._lock(config) as root:
            path, record, raw = self._load(root, preview_id)
            if not isinstance(preview_hash, str) or preview_hash != record["preview_hash"]:
                raise DraftError("PREVIEW_HASH_MISMATCH")
            entry, profile, settings = self._send_bound(config, record)
            if record["state"] != "prepared":
                if record["state"] in ("attempted", "uncertain"):
                    record["state"] = "uncertain"
                if record["state"] in ("submission_accepted", "uncertain"):
                    self._safe_observe_sent(path, record, raw, entry, profile)
                return self._send_result(record)
            self._prevent_source_replay(root, record["immutable"].get("draft_source"), preview_id,
                                        record["immutable"]["review"]["message_id"])
            sender, recipients = self._envelope(record, raw)
            with self._reader(profile) as reader:
                self._verify_sources(reader, entry, record)
            with self._smtp(settings, profile) as client:
                self._send_bound(self._config(), record)
                advertised_size = getattr(client, "esmtp_features", {}).get("size", "")
                if (isinstance(advertised_size, str) and advertised_size.isdigit() and
                        len(raw) > int(advertised_size)):
                    raise DraftError("SMTP_MESSAGE_TOO_LARGE")
                # All recipients must be accepted before DATA. No partial send.
                mail_options = self._mail_options(client, raw)
                code, _ = client.mail(sender, options=mail_options) if mail_options else client.mail(sender)
                if code != 250:
                    record.update(state="not_submitted", error_code="SMTP_SENDER_REFUSED")
                    write_json(path, record)
                    return self._send_result(record)
                refused = False
                for recipient in recipients:
                    code, _ = client.rcpt(recipient)
                    refused = refused or code not in (250, 251)
                if refused:
                    try:
                        client.rset()
                    except Exception:
                        pass
                    record.update(state="not_submitted", error_code="SMTP_RECIPIENT_REFUSED")
                    write_json(path, record)
                    return self._send_result(record)
                # Recheck configuration/source after slow SMTP preflight, still before DATA.
                self._send_bound(self._config(), record)
                with self._reader(profile) as reader:
                    self._verify_sources(reader, entry, record)
                record.update(state="attempted", smtp_attempted=True,
                              attempted_at=dt.datetime.now(dt.timezone.utc).isoformat())
                write_json(path, record)
                outcome_durable = False
                try:
                    sock = getattr(client, "sock", None)
                    if sock is not None:
                        sock.settimeout(UPLOAD_TIMEOUT)
                    code, _ = client.data(raw)
                except Exception:
                    record.update(state="uncertain", error_code="SMTP_DATA_RESPONSE_UNCERTAIN")
                else:
                    if code == 250:
                        record.update(state="submission_accepted", receipt={
                            "message_id": record["immutable"]["review"]["message_id"],
                            "smtp_host": settings["host"], "recipient_count": len(recipients),
                            "accepted_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                        record.pop("error_code", None)
                    else:
                        record.update(state="submission_rejected", error_code="SMTP_DATA_REJECTED")
                # Acceptance persists before QUIT or any Sent-copy mutation.
                outcome_durable = self._persist_outcome(path, record)
            if record["state"] == "submission_accepted":
                if outcome_durable:
                    try:
                        self._copy_sent(path, record, raw, entry, profile)
                    except Exception:
                        record["sent_copy"].update(status="uncertain", error_code="SENT_COPY_STATUS_UNAVAILABLE")
                        self._persist_outcome(path, record)
                else:
                    record["sent_copy"].update(status="not_started", error_code="SEND_RECEIPT_NOT_DURABLE")
            elif record["state"] == "uncertain":
                self._safe_observe_sent(path, record, raw, entry, profile)
            result = self._send_result(record)
            result["smtp_submissions"] = 1
            result["mailbox_writes"] = int(record["sent_copy"].get("append_attempted", False))
            return result

    @public
    def send_status(self, preview_id):
        config = self._config()
        with self._lock(config) as root:
            path, record, raw = self._load(root, preview_id)
            entry, profile, _ = self._send_bound(config, record)
            if record["state"] in ("attempted", "uncertain"):
                record["state"] = "uncertain"
            if record["state"] in ("submission_accepted", "uncertain"):
                self._safe_observe_sent(path, record, raw, entry, profile)
            return self._send_result(record)
