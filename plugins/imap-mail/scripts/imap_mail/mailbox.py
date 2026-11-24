"""Reviewed message moves (Trash, Archive, named folder) and permanent deletion.

Both follow the flags pattern: a read-only preview freezes exact message
identities, content hashes and the resolved destination; apply needs the same
ID/hash, writes a durable intent journal before every IMAP mutation and then
verifies the result in an independent read-only session; status only observes.
An attempted operation is never replayed. Moves are opt-in per account with
``move_write``; permanent deletion separately with ``expunge_write``. Permanent
deletion is limited to the account's Trash, or unsent drafts in Drafts, and a
private backup of every original is kept before anything is expunged.
"""

from contextlib import contextmanager
import datetime as dt
import email
from email import policy
import fcntl
import json
import os
import re
import uuid

from . import read_access
from .core import (DraftError, MESSAGE_ID, PREVIEW_ID, STABLE_ID, digest, header,
                   private_dir, private_read, profile_identity, public, semantic,
                   write_json, write_new)
from .flags import FlagService, _key, _now, _references


MAX_MESSAGES = 50
_DESTINATIONS = {"trash", "archive", "folder"}
_REFERENCE = ("account", "mailbox", "uidvalidity", "uid")
_CONFLICTS = {"UIDVALIDITY_CHANGED", "MOVE_SOURCE_CHANGED", "MOVE_DESTINATION_CHANGED",
              "DELETE_SOURCE_CHANGED", "DELETE_DESTINATION_CHANGED"}


def _capabilities(reader):
    status, rows = reader.client.capability()
    if status != "OK" or not isinstance(rows, list):
        raise DraftError("MAILBOX_CAPABILITY_CHECK_FAILED")
    return {item.upper().decode("ascii", "replace") for row in rows if isinstance(row, bytes)
            for item in row.split()}


def _uid_rows(status, rows):
    if status != "OK" or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], bytes):
        raise DraftError("MAILBOX_SEARCH_FAILED")
    values = rows[0].split()
    if any(not STABLE_ID.fullmatch(v.decode("ascii", "replace")) for v in values):
        raise DraftError("MAILBOX_SEARCH_FAILED")
    return [v.decode("ascii") for v in values]


def _copyuid(client, data, uid):
    """Parse one COPYUID mapping (RFC 4315) for exactly this source UID."""
    available = []
    try:
        _, values = client.response("COPYUID")
        available = [v for v in (values or []) if isinstance(v, bytes) and v]
    except Exception:
        available = []
    if not available:
        available = [m[1] for row in (data or []) if isinstance(row, bytes)
                     for m in [re.search(rb"\[COPYUID ([^]]+)\]", row)] if m]
    if not available:
        return None
    if len(available) != 1:
        raise DraftError("COPYUID_INVALID")
    match = re.fullmatch(rb"([1-9][0-9]*) ([1-9][0-9]*) ([1-9][0-9]*)", available[0].split(b"]", 1)[0].strip())
    if not match or match[2].decode() != uid:
        raise DraftError("COPYUID_INVALID")
    return {"uidvalidity": match[1].decode(), "uid": match[3].decode()}


def _message_id(raw):
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
        values = message.get_all("Message-ID", [])
        value = str(values[0]).strip() if len(values) == 1 else ""
    except Exception:
        return None
    return value if MESSAGE_ID.fullmatch(value) else None


def _same_content(raw, expected_sha256, expected_semantic):
    if digest(raw) == expected_sha256:
        return True
    # Some providers re-serialize stored MIME; equal MIME meaning still counts.
    try:
        return expected_semantic is not None and digest(semantic(raw)) == expected_semantic
    except Exception:
        return False


class MailboxService(FlagService):
    # ----------------------------------------------------------------- access
    @public
    def accounts(self):
        result = super().accounts()
        config = self._config(missing_ok=True)
        for row in result["accounts"]:
            row.update(move_enabled=False, delete_enabled=False, mailbox_issues=[])
            if config is None:
                continue
            entry = config["accounts"].get(row["account"], {})
            if not isinstance(entry, dict):
                continue
            for key, field in (("move_write", "move_enabled"), ("expunge_write", "delete_enabled")):
                if entry.get(key) is not True:
                    continue
                try:
                    self._mailbox_account(config, row["account"], key)
                    row[field] = True
                except Exception as exc:
                    row["mailbox_issues"].append(exc.code if isinstance(exc, DraftError) else "MAILBOX_CONFIGURATION_INVALID")
        return result

    def _mailbox_account(self, config, account, permission):
        entry, profile = self._account(config, account, write=False)
        if os.environ.get("MAIL_BROKER_CONFIG") or config.get("broker_config"):
            raise DraftError("BROKER_MAILBOX_ADAPTER_REQUIRED")
        if entry.get(permission) is not True:
            raise DraftError("MOVE_NOT_AUTHORIZED" if permission == "move_write" else "DELETE_NOT_AUTHORIZED")
        return entry, profile

    def _permission(self, operation):
        return "move_write" if operation == "move" else "expunge_write"

    @contextmanager
    def _mailbox_lock(self, config):
        root = private_dir(self._state(config) / "mailbox")
        path = root / "mailbox.lock"
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

    def _mailbox_load(self, root, preview_id, operation):
        if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
            raise DraftError("INVALID_PREVIEW_ID")
        record = json.loads(private_read(root / (preview_id + ".json"), 1024 * 1024))
        immutable = record.get("immutable", {})
        if (record.get("version") != 1 or immutable.get("preview_id") != preview_id or
                digest(immutable) != record.get("preview_hash")):
            raise DraftError("PREVIEW_INTEGRITY_FAILED")
        if immutable.get("operation") != operation:
            raise DraftError("PREVIEW_OPERATION_MISMATCH")
        return root / (preview_id + ".json"), record

    def _mailbox_bound(self, config, record):
        immutable = record["immutable"]
        entry, profile = self._mailbox_account(config, immutable["account"], self._permission(immutable["operation"]))
        if profile_identity(profile) != immutable["identity"]:
            raise DraftError("ACCOUNT_IDENTITY_CHANGED")
        if digest(entry) != immutable["account_config_sha256"]:
            raise DraftError("ACCOUNT_CONFIGURATION_CHANGED")
        return entry, profile

    def _mailbox_read_bound(self, config, record):
        # Revoking the write grant must not hide an attempted operation.
        immutable = record["immutable"]
        entry, profile = self._account(config, immutable["account"], write=False)
        if profile_identity(profile) != immutable["identity"]:
            raise DraftError("ACCOUNT_IDENTITY_CHANGED")
        return entry, profile

    def _mailbox_pending(self, root, messages, preview_id=None):
        keys = {_key(message) for message in messages}
        for path in root.glob("*.json"):
            if path.stem == preview_id:
                continue
            record = json.loads(private_read(path, 1024 * 1024))
            if (record.get("attempted") and record.get("state") != "verified" and
                    keys.intersection(_key(m) for m in record.get("immutable", {}).get("messages", []))):
                raise DraftError("MAILBOX_SOURCE_OPERATION_UNRESOLVED")

    # ---------------------------------------------------------------- folders
    def _special(self, reader, entry, kind):
        selectable = [f for f in reader.folders()["folders"] if f.get("selectable", True)]
        flag = "\\" + kind.capitalize()
        special = [f["name"] for f in selectable if f.get("kind") == kind or flag in f.get("flags", [])]
        if len(special) > 1:
            raise DraftError(kind.upper() + "_FOLDER_AMBIGUOUS")
        override = entry.get(kind + "_mailbox")
        if override is not None:
            header(override)
            if len([f for f in selectable if f["name"] == override]) != 1:
                raise DraftError(kind.upper() + "_FOLDER_UNAVAILABLE")
            if special and special[0] != override:
                raise DraftError(kind.upper() + "_FOLDER_CONFLICT")
        name = special[0] if special else override
        if not name:
            raise DraftError(kind.upper() + "_FOLDER_UNAVAILABLE")
        return header(name)

    def _destination(self, reader, entry, destination, folder):
        if destination not in _DESTINATIONS:
            raise DraftError("INVALID_MOVE_DESTINATION")
        if destination == "folder":
            if not isinstance(folder, str) or not folder:
                raise DraftError("INVALID_MOVE_DESTINATION")
            name = header(folder, 1024)
            selectable = [f for f in reader.folders()["folders"] if f.get("selectable", True)]
            if len([f for f in selectable if f["name"] == name]) != 1:
                raise DraftError("MOVE_FOLDER_UNAVAILABLE")
        else:
            if folder is not None:
                raise DraftError("INVALID_MOVE_DESTINATION")
            name = self._special(reader, entry, destination)
        validity, _ = reader.select(name)
        if not STABLE_ID.fullmatch(str(validity)):
            raise DraftError("MOVE_DESTINATION_UIDVALIDITY_UNAVAILABLE")
        return name, str(validity)

    # --------------------------------------------------------------- snapshot
    def _snapshot(self, reader, ref):
        try:
            raw, flags = self._full_raw(reader, ref["mailbox"], ref["uidvalidity"], ref["uid"])
        except DraftError as exc:
            if exc.code == "MESSAGE_UNAVAILABLE":
                raise DraftError("MAILBOX_MESSAGE_UNAVAILABLE") from None
            raise
        normalized = {flag.casefold() for flag in flags}
        if "\\deleted" in normalized:
            raise DraftError("MAILBOX_MESSAGE_ALREADY_DELETED")
        try:
            meaning = digest(semantic(raw))
        except Exception:
            meaning = None
        message = email.message_from_bytes(raw, policy=policy.default)
        return {**ref, "content_sha256": digest(raw), "semantic_sha256": meaning,
                "message_id": _message_id(raw), "flags": list(flags),
                "subject": str(message.get("Subject", ""))[:300], "bytes": len(raw)}, raw

    def _destination_matches(self, reader, mailbox, message_id):
        if not message_id:
            return []
        reader.select(mailbox)
        status, rows = reader.client.uid("SEARCH", None, "HEADER", "Message-ID", reader.client._quote(message_id))
        return _uid_rows(status, rows)

    # ---------------------------------------------------------------- results
    def _mailbox_result(self, record):
        immutable = record["immutable"]
        result = {"status": record["state"], "operation": immutable["operation"],
                  "preview_id": immutable["preview_id"], "preview_hash": record["preview_hash"],
                  "account": immutable["account"], "identity": immutable["identity"],
                  "messages": [{k: m[k] for k in (*_REFERENCE, "subject", "message_id", "content_sha256", "flags")}
                               for m in immutable["messages"]],
                  "attempted": record.get("attempted", False),
                  "observations": record.get("observations", []), "receipt": record.get("receipt"),
                  "mailbox_writes": 0, "smtp_submissions": 0}
        for name in ("destination", "method", "backup", "permanent", "recoverable"):
            if name in immutable:
                result[name] = immutable[name]
        for name in ("error_code", "command_error_code"):
            if record.get(name):
                result[name] = record[name]
        return result

    def _writer(self, writer, mailbox, uidvalidity, code):
        status, _ = writer.client.select(writer.client._quote(read_access.utf7_encode(mailbox)), readonly=False)
        _, values = writer.client.response("UIDVALIDITY")
        values = [value for value in (values or []) if isinstance(value, bytes)]
        if status != "OK" or getattr(writer.client, "is_readonly", False):
            raise DraftError(code)
        if values != [str(uidvalidity).encode()]:
            raise DraftError("UIDVALIDITY_CHANGED")

    def _precondition(self, writer, source, error):
        raw, flags = self._fetch_full_raw(writer, source["uid"])
        if digest(raw) != source["content_sha256"]:
            raise DraftError(error)
        current = {flag.casefold() for flag in flags if flag.casefold() != "\\recent"}
        expected = {flag.casefold() for flag in source["flags"] if flag.casefold() != "\\recent"}
        if current != expected:
            raise DraftError(error)

    def _source_state(self, reader, source):
        reader.select(source["mailbox"])
        status, rows = reader.client.uid("SEARCH", None, "UID", source["uid"])
        uids = _uid_rows(status, rows)
        if source["uid"] not in uids:
            return "absent", None
        raw, flags = self._fetch_full_raw(reader, source["uid"])
        deleted = "\\deleted" in {flag.casefold() for flag in flags}
        return ("deleted_flag" if deleted else "present"), digest(raw)

    # ------------------------------------------------------------------- move
    @public
    def move_preview(self, account, messages, destination, folder=None):
        config = self._config()
        entry, profile = self._mailbox_account(config, account, "move_write")
        references = _references(account, messages)
        if len(references) > MAX_MESSAGES:
            raise DraftError("INVALID_MAILBOX_MESSAGES")
        if len({(r["mailbox"], r["uidvalidity"]) for r in references}) != 1:
            raise DraftError("MOVE_SINGLE_SOURCE_MAILBOX_REQUIRED")
        sources = []
        with self._reader(profile) as reader:
            capabilities = _capabilities(reader)
            has_move, has_uidplus = "MOVE" in capabilities, "UIDPLUS" in capabilities
            if not has_move and not has_uidplus:
                raise DraftError("MOVE_REQUIRES_MOVE_OR_UIDPLUS")
            if not has_move and "X-GM-EXT-1" in capabilities:
                raise DraftError("GMAIL_MOVE_REQUIRED")
            target, target_validity = self._destination(reader, entry, destination, folder)
            if target == references[0]["mailbox"]:
                raise DraftError("MOVE_SOURCE_IS_DESTINATION")
            for ref in references:
                snapshot, _ = self._snapshot(reader, ref)
                if not has_uidplus and not snapshot["message_id"]:
                    # Without COPYUID only Message-ID can locate the copy.
                    raise DraftError("MOVE_IDENTITY_UNVERIFIABLE")
                snapshot["destination_existing_uids"] = self._destination_matches(
                    reader, target, snapshot["message_id"])
                sources.append(snapshot)
        preview_id = uuid.uuid4().hex
        method = "uid_move" if has_move else "uid_copy_store_deleted_uid_expunge"
        immutable = {"operation": "move", "preview_id": preview_id, "account": account,
                     "identity": profile_identity(profile), "account_config_sha256": digest(entry),
                     "messages": sources, "method": method,
                     "destination": {"kind": destination, "mailbox": target, "uidvalidity": target_validity},
                     "permanent": False, "recoverable": True}
        record = {"version": 1, "immutable": immutable, "preview_hash": digest(immutable),
                  "state": "prepared", "attempted": False, "attempts": [],
                  "created_at": _now(), "receipt": None}
        with self._mailbox_lock(config) as root:
            self._mailbox_pending(root, sources)
            self._mailbox_bound(self._config(), record)
            write_json(root / (preview_id + ".json"), record)
        return self._mailbox_result(record)

    def _observe_move(self, path, record, profile):
        immutable = record["immutable"]
        target = immutable["destination"]
        mappings = record.get("mappings", {})
        observations = []
        try:
            with self._reader(profile) as reader:
                for source in immutable["messages"]:
                    state, _ = self._source_state(reader, source)
                    validity, _ = reader.select(target["mailbox"])
                    if str(validity) != target["uidvalidity"]:
                        raise DraftError("MOVE_DESTINATION_CHANGED")
                    mapping = mappings.get(source["uid"])
                    if mapping and mapping["uidvalidity"] == target["uidvalidity"]:
                        candidates = [mapping["uid"]]
                    else:
                        known = set(source.get("destination_existing_uids", []))
                        candidates = [uid for uid in self._destination_matches(reader, target["mailbox"], source["message_id"])
                                      if uid not in known]
                    found = []
                    for uid in candidates:
                        try:
                            raw, _ = self._fetch_full_raw(reader, uid)
                        except DraftError:
                            continue
                        if _same_content(raw, source["content_sha256"], source.get("semantic_sha256")):
                            found.append(uid)
                    observations.append({**{k: source[k] for k in _REFERENCE}, "source": state,
                                         "destination_uids": found})
            complete = all(row["source"] == "absent" and len(row["destination_uids"]) == 1 for row in observations)
            if complete:
                record.update(state="verified", receipt={
                    "destination": target, "verified_at": _now(),
                    "messages": [{"source": {k: row[k] for k in _REFERENCE},
                                  "destination": {"account": immutable["account"], "mailbox": target["mailbox"],
                                                  "uidvalidity": target["uidvalidity"], "uid": row["destination_uids"][0]}}
                                 for row in observations]})
                record.pop("error_code", None)
            else:
                record["state"] = "uncertain"
                if any(row["source"] != "absent" and row["destination_uids"] for row in observations):
                    record["error_code"] = "MOVE_SOURCE_STILL_PRESENT"
                elif any(row["source"] == "absent" and not row["destination_uids"] for row in observations):
                    record["error_code"] = "MOVE_DESTINATION_NOT_CONFIRMED"
                elif any(len(row["destination_uids"]) > 1 for row in observations):
                    record["error_code"] = "MOVE_DESTINATION_NOT_UNIQUE"
                else:
                    record["error_code"] = "MOVE_NOT_CONFIRMED"
        except Exception as exc:
            code = exc.code if isinstance(exc, DraftError) else "MOVE_READBACK_FAILED"
            record["state"] = "conflict" if code in _CONFLICTS else "uncertain"
            record["error_code"] = code
        record["observations"] = observations
        write_json(path, record)
        return self._mailbox_result(record)

    @public
    def move_apply(self, preview_id, preview_hash):
        config = self._config()
        writes = 0
        with self._mailbox_lock(config) as root:
            path, record = self._mailbox_load(root, preview_id, "move")
            if not isinstance(preview_hash, str) or preview_hash != record["preview_hash"]:
                raise DraftError("PREVIEW_HASH_MISMATCH")
            immutable = record["immutable"]
            if record.get("attempted") or record["state"] != "prepared":
                _, profile = self._mailbox_read_bound(config, record)
                return self._observe_move(path, record, profile)
            entry, profile = self._mailbox_bound(config, record)
            self._mailbox_pending(root, immutable["messages"], preview_id)
            sources = immutable["messages"]
            target = immutable["destination"]
            quoted_target = None
            try:
                with self._reader(profile) as writer:
                    capabilities = _capabilities(writer)
                    if immutable["method"] == "uid_move" and "MOVE" not in capabilities:
                        raise DraftError("MOVE_CAPABILITY_CHANGED")
                    if immutable["method"] != "uid_move" and "UIDPLUS" not in capabilities:
                        raise DraftError("MOVE_CAPABILITY_CHANGED")
                    validity, _ = writer.select(target["mailbox"])
                    if str(validity) != target["uidvalidity"]:
                        raise DraftError("MOVE_DESTINATION_CHANGED")
                    self._writer(writer, sources[0]["mailbox"], sources[0]["uidvalidity"], "MOVE_WRITE_SELECTION_FAILED")
                    for source in sources:
                        self._precondition(writer, source, "MOVE_SOURCE_CHANGED")
                    self._mailbox_bound(self._config(), record)
                    quoted_target = writer.client._quote(read_access.utf7_encode(target["mailbox"]))
                    record.setdefault("mappings", {})
                    for source in sources:
                        self._precondition(writer, source, "MOVE_SOURCE_CHANGED")
                        try:
                            writer.client.response("COPYUID")  # Drop any stale code.
                        except Exception:
                            pass
                        steps = (["MOVE"] if immutable["method"] == "uid_move" else ["COPY", "STORE", "EXPUNGE"])
                        for step in steps:
                            record.update(state="attempted", attempted=True)
                            record["attempts"].append({"uid": source["uid"], "command": step, "attempted_at": _now()})
                            write_json(path, record)  # Durable before the command.
                            writes += 1
                            if step == "MOVE":
                                status, data = writer.client.uid("MOVE", source["uid"], quoted_target)
                            elif step == "COPY":
                                status, data = writer.client.uid("COPY", source["uid"], quoted_target)
                            elif step == "STORE":
                                status, data = writer.client.uid("STORE", source["uid"], "+FLAGS.SILENT", "(\\Deleted)")
                            else:
                                status, data = writer.client.uid("EXPUNGE", source["uid"])
                            if status != "OK":
                                raise DraftError("MOVE_COMMAND_NOT_CONFIRMED")
                            if step in ("MOVE", "COPY"):
                                try:
                                    mapping = _copyuid(writer.client, data, source["uid"])
                                except DraftError:
                                    mapping = None
                                if mapping:
                                    record["mappings"][source["uid"]] = mapping
                                    write_json(path, record)
            except Exception as exc:
                code = exc.code if isinstance(exc, DraftError) else "MOVE_RESPONSE_UNCERTAIN"
                if not record.get("attempted"):
                    raise DraftError(code) from None
                record["command_error_code"] = code
                write_json(path, record)
            result = self._observe_move(path, record, profile)
            result["mailbox_writes"] = writes
            return result

    @public
    def move_status(self, preview_id):
        config = self._config()
        with self._mailbox_lock(config) as root:
            path, record = self._mailbox_load(root, preview_id, "move")
            _, profile = self._mailbox_read_bound(config, record)
            if record.get("attempted") or record["state"] == "verified":
                return self._observe_move(path, record, profile)
            return self._mailbox_result(record)

    # ----------------------------------------------------------------- delete
    @public
    def delete_preview(self, account, messages):
        config = self._config()
        entry, profile = self._mailbox_account(config, account, "expunge_write")
        references = _references(account, messages)
        if len(references) > MAX_MESSAGES:
            raise DraftError("INVALID_MAILBOX_MESSAGES")
        if len({(r["mailbox"], r["uidvalidity"]) for r in references}) != 1:
            raise DraftError("DELETE_SINGLE_SOURCE_MAILBOX_REQUIRED")
        sources, originals = [], []
        with self._reader(profile) as reader:
            if "UIDPLUS" not in _capabilities(reader):
                raise DraftError("DELETE_REQUIRES_UIDPLUS")
            mailbox = references[0]["mailbox"]
            allowed = {}
            for kind in ("trash", "drafts"):
                try:
                    allowed[self._special(reader, entry, kind)] = kind
                except DraftError:
                    continue
            kind = allowed.get(mailbox)
            if kind is None:
                # Permanent deletion is never offered for Inbox, Sent, Archive
                # or ordinary folders: move to Trash first.
                raise DraftError("DELETE_ONLY_FROM_TRASH_OR_DRAFTS")
            for ref in references:
                snapshot, raw = self._snapshot(reader, ref)
                if kind == "drafts" and "\\draft" not in {f.casefold() for f in snapshot["flags"]}:
                    raise DraftError("DELETE_DRAFT_FLAG_REQUIRED")
                sources.append(snapshot)
                originals.append(raw)
        preview_id = uuid.uuid4().hex
        backups = [f"{preview_id}.{source['uid']}.original.eml" for source in sources]
        immutable = {"operation": "delete", "preview_id": preview_id, "account": account,
                     "identity": profile_identity(profile), "account_config_sha256": digest(entry),
                     "messages": sources, "source_kind": kind,
                     "method": "uid_store_deleted_uid_expunge", "permanent": True,
                     "recoverable": False, "backup": backups}
        record = {"version": 1, "immutable": immutable, "preview_hash": digest(immutable),
                  "state": "prepared", "attempted": False, "attempts": [],
                  "created_at": _now(), "receipt": None}
        with self._mailbox_lock(config) as root:
            self._mailbox_pending(root, sources)
            self._mailbox_bound(self._config(), record)
            for name, raw in zip(backups, originals):
                write_new(root / name, raw)
            write_json(root / (preview_id + ".json"), record)
        return self._mailbox_result(record)

    def _observe_delete(self, path, record, profile):
        immutable = record["immutable"]
        observations = []
        try:
            with self._reader(profile) as reader:
                for source in immutable["messages"]:
                    state, current = self._source_state(reader, source)
                    if state != "absent" and current != source["content_sha256"]:
                        raise DraftError("DELETE_SOURCE_CHANGED")
                    observations.append({**{k: source[k] for k in _REFERENCE}, "source": state})
            if all(row["source"] == "absent" for row in observations):
                record.update(state="verified", receipt={"deleted": [{k: row[k] for k in _REFERENCE} for row in observations],
                                                         "verified_at": _now(), "backup": immutable["backup"]})
                record.pop("error_code", None)
            else:
                record["state"] = "uncertain"
                record["error_code"] = ("DELETE_EXPUNGE_NOT_CONFIRMED"
                                        if any(row["source"] == "deleted_flag" for row in observations)
                                        else "DELETE_NOT_CONFIRMED")
        except Exception as exc:
            code = exc.code if isinstance(exc, DraftError) else "DELETE_READBACK_FAILED"
            record["state"] = "conflict" if code in _CONFLICTS else "uncertain"
            record["error_code"] = code
        record["observations"] = observations
        write_json(path, record)
        return self._mailbox_result(record)

    @public
    def delete_apply(self, preview_id, preview_hash):
        config = self._config()
        writes = 0
        with self._mailbox_lock(config) as root:
            path, record = self._mailbox_load(root, preview_id, "delete")
            if not isinstance(preview_hash, str) or preview_hash != record["preview_hash"]:
                raise DraftError("PREVIEW_HASH_MISMATCH")
            immutable = record["immutable"]
            if record.get("attempted") or record["state"] != "prepared":
                _, profile = self._mailbox_read_bound(config, record)
                return self._observe_delete(path, record, profile)
            _, profile = self._mailbox_bound(config, record)
            self._mailbox_pending(root, immutable["messages"], preview_id)
            for name, source in zip(immutable["backup"], immutable["messages"]):
                if digest(private_read(root / name)) != source["content_sha256"]:
                    raise DraftError("DELETE_BACKUP_INTEGRITY_FAILED")
            sources = immutable["messages"]
            try:
                with self._reader(profile) as writer:
                    if "UIDPLUS" not in _capabilities(writer):
                        raise DraftError("DELETE_REQUIRES_UIDPLUS")
                    self._writer(writer, sources[0]["mailbox"], sources[0]["uidvalidity"], "DELETE_WRITE_SELECTION_FAILED")
                    for source in sources:
                        self._precondition(writer, source, "DELETE_SOURCE_CHANGED")
                    self._mailbox_bound(self._config(), record)
                    for source in sources:
                        self._precondition(writer, source, "DELETE_SOURCE_CHANGED")
                        for step in ("STORE", "EXPUNGE"):
                            if step == "EXPUNGE":
                                # Expunge only a UID that now carries \Deleted.
                                _, flags = self._fetch_full_raw(writer, source["uid"])
                                if "\\deleted" not in {f.casefold() for f in flags}:
                                    raise DraftError("DELETE_FLAG_NOT_CONFIRMED")
                            record.update(state="attempted", attempted=True)
                            record["attempts"].append({"uid": source["uid"], "command": step, "attempted_at": _now()})
                            write_json(path, record)
                            writes += 1
                            if step == "STORE":
                                status, _ = writer.client.uid("STORE", source["uid"], "+FLAGS.SILENT", "(\\Deleted)")
                            else:
                                status, _ = writer.client.uid("EXPUNGE", source["uid"])
                            if status != "OK":
                                raise DraftError("DELETE_COMMAND_NOT_CONFIRMED")
            except Exception as exc:
                code = exc.code if isinstance(exc, DraftError) else "DELETE_RESPONSE_UNCERTAIN"
                if not record.get("attempted"):
                    raise DraftError(code) from None
                record["command_error_code"] = code
                write_json(path, record)
            result = self._observe_delete(path, record, profile)
            result["mailbox_writes"] = writes
            return result

    @public
    def delete_status(self, preview_id):
        config = self._config()
        with self._mailbox_lock(config) as root:
            path, record = self._mailbox_load(root, preview_id, "delete")
            _, profile = self._mailbox_read_bound(config, record)
            if record.get("attempted") or record["state"] == "verified":
                return self._observe_delete(path, record, profile)
            return self._mailbox_result(record)
