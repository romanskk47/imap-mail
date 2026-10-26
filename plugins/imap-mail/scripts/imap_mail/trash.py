"""Reviewed, recoverable trash through UID MOVE only; never EXPUNGE or retry.

One batch belongs to one source mailbox. A durable attempt fences even a lost
MOVE response. Recovery observes source absence and exact MIME in special-use
Trash. No arbitrary destination, folder creation, SMTP or permanent deletion.
"""

from contextlib import contextmanager
import email
import email.policy
import fcntl
import json
import os
import uuid

from .core import (DraftError, PREVIEW_ID, digest, private_dir, private_read,
                   profile_identity, public, write_json)
from .flags import FlagService, _references, _now
from . import read_access


class TrashService(FlagService):
    @public
    def accounts(self):
        result = super().accounts()
        config = self._config(missing_ok=True)
        for row in result["accounts"]:
            row["trash_enabled"] = False
            if config is not None:
                try:
                    self._trash_account(config, row["account"])
                    row["trash_enabled"] = True
                except DraftError:
                    pass
        return result

    def _trash_account(self, config, account, write=True):
        entry, profile = self._account(config, account, write=False)
        if os.environ.get("MAIL_BROKER_CONFIG") or config.get("broker_config"):
            raise DraftError("BROKER_TRASH_ADAPTER_REQUIRED")
        if write and entry.get("trash_write") is not True:
            raise DraftError("TRASH_NOT_AUTHORIZED")
        return entry, profile

    @contextmanager
    def _trash_lock(self, config):
        root = private_dir(self._state(config) / "trash")
        path = root / "trash.lock"
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

    def _trash_load(self, root, preview_id):
        if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
            raise DraftError("INVALID_PREVIEW_ID")
        path = root / (preview_id + ".json")
        record = json.loads(private_read(path, 1024 * 1024))
        frozen = record.get("immutable", {})
        if (record.get("version") != 1 or frozen.get("operation") != "trash" or
                frozen.get("preview_id") != preview_id or digest(frozen) != record.get("preview_hash")):
            raise DraftError("PREVIEW_INTEGRITY_FAILED")
        return path, record

    def _trash_bound(self, config, record, write=True):
        frozen = record["immutable"]
        entry, profile = self._trash_account(config, frozen["account"], write)
        if profile_identity(profile) != frozen["identity"]:
            raise DraftError("ACCOUNT_IDENTITY_CHANGED")
        if write and digest(entry) != frozen["account_config_sha256"]:
            raise DraftError("ACCOUNT_CONFIGURATION_CHANGED")
        return profile

    def _trash_destination(self, reader):
        capabilities = {x.decode() if isinstance(x, bytes) else x for x in reader.client.capabilities}
        if "MOVE" not in capabilities:
            raise DraftError("TRASH_REQUIRES_UID_MOVE")
        folders = [row["name"] for row in reader.folders()["folders"]
                   if row.get("selectable") and "\\Trash" in row.get("flags", [])]
        if len(folders) != 1:
            raise DraftError("TRASH_DESTINATION_AMBIGUOUS")
        validity, _ = reader.select(folders[0])
        return {"mailbox": folders[0], "uidvalidity": str(validity)}

    def _trash_snapshot(self, reader, ref):
        raw, flags = self._full_raw(reader, ref["mailbox"], ref["uidvalidity"], ref["uid"])
        if any(flag.lower() in ("\\deleted", "\\draft") for flag in flags):
            raise DraftError("TRASH_UNSAFE_SOURCE_FLAGS")
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        message_id = str(msg.get("Message-ID", ""))
        if not message_id or len(message_id) > 998 or not message_id.isascii() or any(c in message_id for c in "\r\n"):
            raise DraftError("TRASH_REQUIRES_MESSAGE_ID")
        return {**ref, "content_sha256": digest(raw), "message_id": message_id,
                "subject": str(msg.get("Subject", ""))[:998], "from": str(msg.get("From", ""))[:998]}

    def _trash_pending(self, root, messages, preview_id=None):
        keys = {(m["account"], m["mailbox"], m["uidvalidity"], m["uid"]) for m in messages}
        for path in root.glob("*.json"):
            if path.stem == preview_id:
                continue
            _, other = self._trash_load(root, path.stem)
            if other.get("attempted") and other["state"] != "verified":
                if keys.intersection((m["account"], m["mailbox"], m["uidvalidity"], m["uid"])
                                     for m in other["immutable"]["messages"]):
                    raise DraftError("TRASH_SOURCE_OPERATION_UNRESOLVED")

    def _trash_result(self, record):
        return {**record["immutable"], "preview_hash": record["preview_hash"],
                "status": record["state"], "attempted": record["attempted"],
                "receipt": record.get("receipt"), "error_code": record.get("error_code"),
                "mailbox_writes": 0, "smtp_submissions": 0, "permanent_deletions": 0}

    @public
    def trash_preview(self, account, messages):
        config = self._config()
        entry, profile = self._trash_account(config, account)
        refs = _references(account, messages)
        if len({(r["mailbox"], r["uidvalidity"]) for r in refs}) != 1:
            raise DraftError("TRASH_ONE_SOURCE_MAILBOX_REQUIRED")
        with self._reader(profile) as reader:
            destination = self._trash_destination(reader)
            if refs[0]["mailbox"] == destination["mailbox"]:
                raise DraftError("ALREADY_IN_TRASH")
            sources = [self._trash_snapshot(reader, ref) for ref in refs]
        preview_id = uuid.uuid4().hex
        frozen = {"operation": "trash", "preview_id": preview_id, "account": account,
                  "identity": profile_identity(profile), "account_config_sha256": digest(entry),
                  "messages": sources, "destination": destination, "atomic": False}
        record = {"version": 1, "immutable": frozen, "preview_hash": digest(frozen),
                  "state": "prepared", "attempted": False, "created_at": _now(), "receipt": None}
        with self._trash_lock(config) as root:
            self._trash_pending(root, sources)
            self._trash_bound(self._config(), record)
            write_json(root / (preview_id + ".json"), record)
        return self._trash_result(record)

    def _trash_observe(self, path, record, profile):
        frozen = record["immutable"]
        observed = []
        try:
            with self._reader(profile) as reader:
                if self._trash_destination(reader) != frozen["destination"]:
                    raise DraftError("TRASH_DESTINATION_CHANGED")
                for source in frozen["messages"]:
                    validity, _ = reader.select(source["mailbox"])
                    if str(validity) != source["uidvalidity"]:
                        raise DraftError("UIDVALIDITY_CHANGED")
                    status, rows = reader.client.uid("SEARCH", None, "UID", source["uid"])
                    if status != "OK" or not rows or rows[0].split():
                        raise DraftError("TRASH_SOURCE_ABSENCE_NOT_CONFIRMED")
                    dest = frozen["destination"]
                    validity, _ = reader.select(dest["mailbox"])
                    if str(validity) != dest["uidvalidity"]:
                        raise DraftError("TRASH_DESTINATION_CHANGED")
                    status, rows = reader.client.uid("SEARCH", None, "HEADER", "Message-ID",
                                                    reader.client._quote(source["message_id"]))
                    if status != "OK" or not rows:
                        raise DraftError("TRASH_READBACK_FAILED")
                    candidates = rows[0].split()
                    if not 1 <= len(candidates) <= 100:
                        raise DraftError("TRASH_DESTINATION_NOT_CONFIRMED")
                    matches = []
                    for uid in candidates:
                        raw, _ = self._fetch_full_raw(reader, uid.decode())
                        if digest(raw) == source["content_sha256"]:
                            matches.append(uid.decode())
                    if len(matches) != 1:
                        raise DraftError("TRASH_DESTINATION_NOT_CONFIRMED")
                    observed.append({"account": frozen["account"], **dest, "uid": matches[0],
                                     "source_uid": source["uid"], "content_sha256": source["content_sha256"]})
            record.update(state="verified", receipt={"messages": observed, "verified_at": _now()})
            record.pop("error_code", None)
        except Exception as exc:
            record.update(state="uncertain", error_code=exc.code if isinstance(exc, DraftError) else "TRASH_READBACK_FAILED")
        write_json(path, record)
        return self._trash_result(record)

    @public
    def trash_apply(self, preview_id, preview_hash):
        config = self._config()
        with self._trash_lock(config) as root:
            path, record = self._trash_load(root, preview_id)
            if preview_hash != record["preview_hash"]:
                raise DraftError("PREVIEW_HASH_MISMATCH")
            if record["attempted"]:
                return self._trash_observe(path, record, self._trash_bound(config, record, write=False))
            profile = self._trash_bound(config, record)
            frozen = record["immutable"]
            self._trash_pending(root, frozen["messages"], preview_id)
            with self._reader(profile) as writer:
                if self._trash_destination(writer) != frozen["destination"]:
                    raise DraftError("TRASH_DESTINATION_CHANGED")
                for source in frozen["messages"]:
                    if self._trash_snapshot(writer, source) != source:
                        raise DraftError("TRASH_SOURCE_CHANGED")
                self._flags_writer(writer, frozen["messages"][0])
                self._trash_bound(self._config(), record)
                record.update(attempted=True, state="attempted", attempted_at=_now())
                write_json(path, record)
                try:
                    status, _ = writer.client.uid("MOVE", ",".join(s["uid"] for s in frozen["messages"]),
                        writer.client._quote(read_access.utf7_encode(frozen["destination"]["mailbox"])))
                    if status != "OK":
                        record["error_code"] = "TRASH_MOVE_NOT_CONFIRMED"
                except Exception:
                    record["error_code"] = "TRASH_MOVE_RESPONSE_UNCERTAIN"
                write_json(path, record)
            result = self._trash_observe(path, record, profile)
            result["mailbox_writes"] = 1
            return result

    @public
    def trash_status(self, preview_id):
        config = self._config()
        with self._trash_lock(config) as root:
            path, record = self._trash_load(root, preview_id)
            profile = self._trash_bound(config, record, write=False)
            return (self._trash_observe(path, record, profile) if record["attempted"]
                    else self._trash_result(record))
