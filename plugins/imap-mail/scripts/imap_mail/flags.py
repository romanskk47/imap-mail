"""Reviewed Seen/Flagged changes for exact existing IMAP message identities.

No MIME rewrite, SMTP, arbitrary flags or expunge. Preview is read-only. Before
each STORE a private intent journal is durable; an attempted batch is only ever
observed again, never resumed/replayed. Without CONDSTORE this is explicitly
preflight-only concurrency protection, not an atomic compare-and-swap or batch.
"""

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
import re
import uuid

from . import read_access
from .attachments import AttachmentService
from .core import (DraftError, PREVIEW_ID, digest, header, private_dir,
                   private_read, profile_identity, public, write_json)


_REFERENCE_KEYS = {"account", "mailbox", "uidvalidity", "uid"}
_TARGET_FLAGS = {"seen": "\\Seen", "flagged": "\\Flagged"}
_CONCURRENCY = "preflight_only"
_CONFLICTS = {"UIDVALIDITY_CHANGED", "FLAGS_MESSAGE_CONTENT_CHANGED",
              "FLAGS_TARGET_PRECONDITION_CHANGED", "FLAGS_MESSAGE_DISAPPEARED"}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _identifier(value):
    # UID and UIDVALIDITY are nonzero unsigned 32-bit integers, not UID sets.
    if (isinstance(value, bool) or not isinstance(value, (str, int)) or
            not re.fullmatch(r"[1-9][0-9]{0,9}", str(value)) or int(value) > 4294967295):
        raise DraftError("INVALID_MESSAGE_REFERENCE")
    return str(value)


def _references(account, messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        raise DraftError("INVALID_FLAGS_MESSAGES")
    result, found = [], set()
    for message in messages:
        if not isinstance(message, dict) or set(message) != _REFERENCE_KEYS:
            raise DraftError("INVALID_MESSAGE_REFERENCE")
        if message["account"] != account:
            raise DraftError("FLAGS_ACCOUNT_MISMATCH")
        mailbox = header(message["mailbox"], 4000)
        if not mailbox:
            raise DraftError("INVALID_MESSAGE_REFERENCE")
        if mailbox.casefold() == "inbox":
            mailbox = "INBOX"  # The one IMAP mailbox name that is case-insensitive.
        ref = {"account": account, "mailbox": mailbox,
               "uidvalidity": _identifier(message["uidvalidity"]), "uid": _identifier(message["uid"])}
        key = _key(ref)
        if key in found:
            raise DraftError("DUPLICATE_MESSAGE_REFERENCE")
        found.add(key)
        result.append(ref)
    return result


def _key(ref):
    return tuple(ref[name] for name in ("account", "mailbox", "uidvalidity", "uid"))


def _desired(seen, flagged):
    result = {}
    for name, value in (("seen", seen), ("flagged", flagged)):
        if value is not None:
            if not isinstance(value, bool):
                raise DraftError("INVALID_FLAGS_VALUES")
            result[name] = value
    if not result:
        raise DraftError("FLAGS_CHANGE_REQUIRED")
    return result


def _target_values(flags, desired):
    # Server flags are observed, never interpolated into a mutation command.
    values = {flag.casefold() for flag in flags}
    return {name: _TARGET_FLAGS[name].casefold() in values for name in desired}


class FlagService(AttachmentService):
    @public
    def accounts(self):
        result = super().accounts()
        config = self._config(missing_ok=True)
        for row in result["accounts"]:
            row.update(flags_enabled=False, flags_connection="not_checked", flags_issues=[])
            if config is None:
                continue
            entry = config["accounts"].get(row["account"], {})
            if not isinstance(entry, dict) or entry.get("flags_write") is not True:
                continue
            try:
                self._flags_account(config, row["account"])
                row["flags_enabled"] = True
            except Exception as exc:
                row["flags_issues"] = [exc.code if isinstance(exc, DraftError) else "FLAGS_CONFIGURATION_INVALID"]
        return result

    def _flags_account(self, config, account):
        # Flags are not authoring: no From allowlist, signature or drafts grant.
        entry, profile = self._account(config, account, write=False)
        if os.environ.get("MAIL_BROKER_CONFIG") or config.get("broker_config"):
            raise DraftError("BROKER_FLAGS_ADAPTER_REQUIRED")
        if entry.get("flags_write") is not True:
            raise DraftError("FLAGS_NOT_AUTHORIZED")
        return entry, profile

    @contextmanager
    def _flags_lock(self, config):
        # Keep these records out of the existing draft/send *.json namespace.
        root = private_dir(self._state(config) / "flags")
        path = root / "flags.lock"
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

    def _flags_load(self, root, preview_id):
        if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
            raise DraftError("INVALID_PREVIEW_ID")
        path = root / (preview_id + ".json")
        record = json.loads(private_read(path, 1024 * 1024))
        immutable = record.get("immutable", {})
        if (record.get("version") != 1 or immutable.get("operation") != "flags" or
                immutable.get("preview_id") != preview_id or digest(immutable) != record.get("preview_hash")):
            raise DraftError("PREVIEW_INTEGRITY_FAILED")
        return path, record

    def _flags_bound(self, config, record):
        immutable = record["immutable"]
        entry, profile = self._flags_account(config, immutable["account"])
        if profile_identity(profile) != immutable["identity"]:
            raise DraftError("ACCOUNT_IDENTITY_CHANGED")
        if digest(entry) != immutable["account_config_sha256"]:
            raise DraftError("ACCOUNT_CONFIGURATION_CHANGED")
        return entry, profile

    def _flags_read_bound(self, config, record):
        # Revoking a write grant does not prevent observing an already attempted
        # operation. Current read access and the exact frozen identity still do.
        immutable = record["immutable"]
        entry, profile = self._account(config, immutable["account"], write=False)
        if profile_identity(profile) != immutable["identity"]:
            raise DraftError("ACCOUNT_IDENTITY_CHANGED")
        return entry, profile

    def _flags_configuration_warning(self, result, entry, record):
        if digest(entry) != record["immutable"]["account_config_sha256"]:
            result["configuration_changed"] = True
        return result

    def _flags_pending(self, root, messages, preview_id=None):
        keys = {_key(message) for message in messages}
        for path in root.glob("*.json"):
            if path.stem == preview_id:
                continue
            _, record = self._flags_load(root, path.stem)
            if (record.get("attempted") and not record.get("receipt") and
                    keys.intersection(_key(message) for message in record["immutable"]["messages"])):
                raise DraftError("FLAGS_SOURCE_OPERATION_UNRESOLVED")

    def _flags_snapshot(self, reader, ref, desired, *, selected=False):
        try:
            raw, flags = (self._fetch_full_raw(reader, ref["uid"]) if selected else
                          self._full_raw(reader, ref["mailbox"], ref["uidvalidity"], ref["uid"]))
        except DraftError as exc:
            if exc.code == "MESSAGE_UNAVAILABLE":
                raise DraftError("FLAGS_MESSAGE_DISAPPEARED") from None
            raise
        return {"content_sha256": digest(raw), "target_flags": _target_values(flags, desired)}

    def _flags_precondition(self, reader, source, desired, *, selected=False):
        current = self._flags_snapshot(reader, source, desired, selected=selected)
        if current["content_sha256"] != source["content_sha256"]:
            raise DraftError("FLAGS_MESSAGE_CONTENT_CHANGED")
        if current["target_flags"] != source["before"]:
            raise DraftError("FLAGS_TARGET_PRECONDITION_CHANGED")
        return current

    def _flags_result(self, record):
        immutable = record["immutable"]
        result = {"status": record["state"], "operation": "flags",
                  "preview_id": immutable["preview_id"], "preview_hash": record["preview_hash"],
                  "account": immutable["account"], "identity": immutable["identity"],
                  "messages": immutable["messages"], "desired": immutable["desired"],
                  "concurrency_protection": _CONCURRENCY, "atomic": False,
                  "attempted": record.get("attempted", False),
                  "store_attempts": len(record.get("store_attempts", [])),
                  "observations": record.get("observations", []), "receipt": record.get("receipt"),
                  "mailbox_writes": 0, "smtp_submissions": 0}
        for name in ("error_code", "store_error_code"):
            if record.get(name):
                result[name] = record[name]
        return result

    @public
    def flags_preview(self, account, messages, seen=None, flagged=None):
        config = self._config()
        entry, profile = self._flags_account(config, account)
        references = _references(account, messages)
        desired = _desired(seen, flagged)
        sources = []
        with self._reader(profile) as reader:
            for ref in references:
                current = self._flags_snapshot(reader, ref, desired)
                sources.append({**ref, "content_sha256": current["content_sha256"],
                                "before": current["target_flags"]})
        preview_id = uuid.uuid4().hex
        immutable = {"operation": "flags", "preview_id": preview_id, "account": account,
                     "identity": profile_identity(profile), "account_config_sha256": digest(entry),
                     "messages": sources, "desired": desired,
                     "concurrency_protection": _CONCURRENCY, "atomic": False}
        record = {"version": 1, "immutable": immutable, "preview_hash": digest(immutable),
                  "state": "prepared", "attempted": False, "store_attempts": [],
                  "created_at": _now(), "receipt": None}
        with self._flags_lock(config) as root:
            self._flags_pending(root, sources)
            self._flags_bound(self._config(), record)
            write_json(root / (preview_id + ".json"), record)
        return self._flags_result(record)

    def _flags_writer(self, reader, ref):
        status, _ = reader.client.select(
            reader.client._quote(read_access.utf7_encode(ref["mailbox"])), readonly=False)
        _, values = reader.client.response("UIDVALIDITY")
        values = [value for value in (values or []) if isinstance(value, bytes)]
        if status != "OK":
            raise DraftError("FLAGS_WRITE_SELECTION_FAILED")
        if values != [ref["uidvalidity"].encode()]:
            raise DraftError("UIDVALIDITY_CHANGED")
        if getattr(reader.client, "is_readonly", False):
            raise DraftError("FLAGS_WRITE_SELECTION_FAILED")

    def _flags_observe(self, path, record, profile):
        immutable = record["immutable"]
        observations = []
        try:
            with self._reader(profile) as reader:
                for source in immutable["messages"]:
                    current = self._flags_snapshot(reader, source, immutable["desired"])
                    if current["content_sha256"] != source["content_sha256"]:
                        raise DraftError("FLAGS_MESSAGE_CONTENT_CHANGED")
                    observations.append({**{name: source[name] for name in _REFERENCE_KEYS},
                                         "target_flags": current["target_flags"]})
            matches = all(row["target_flags"] == immutable["desired"] for row in observations)
            if matches:
                record.update(state="verified", receipt={"messages": observations, "verified_at": _now(),
                              "concurrency_protection": _CONCURRENCY, "atomic": False})
                record.pop("error_code", None)
            else:
                # Even a lost STORE reply followed by an unchanged value is not
                # authority to retry. A later human change must never be undone.
                record["state"] = "conflict" if record.get("receipt") else "uncertain"
                record["error_code"] = "FLAGS_TARGET_NOT_CONFIRMED"
        except Exception as exc:
            code = exc.code if isinstance(exc, DraftError) else "FLAGS_READBACK_FAILED"
            record["state"] = "conflict" if code in _CONFLICTS else "uncertain"
            record["error_code"] = code
        record["observations"] = observations
        write_json(path, record)
        return self._flags_result(record)

    @public
    def flags_apply(self, preview_id, preview_hash):
        config = self._config()
        writes = 0
        with self._flags_lock(config) as root:
            path, record = self._flags_load(root, preview_id)
            if not isinstance(preview_hash, str) or preview_hash != record["preview_hash"]:
                raise DraftError("PREVIEW_HASH_MISMATCH")
            immutable = record["immutable"]
            if record.get("attempted") or record["state"] == "verified":
                entry, profile = self._flags_read_bound(config, record)
                return self._flags_configuration_warning(self._flags_observe(path, record, profile), entry, record)
            _, profile = self._flags_bound(config, record)
            if record["state"] != "prepared":
                return self._flags_result(record)
            self._flags_pending(root, immutable["messages"], preview_id)
            # Check the whole batch before the first mutation. Readers EXAMINE;
            # the separate writer below explicitly SELECTs read-write.
            with self._reader(profile) as reader:
                for source in immutable["messages"]:
                    self._flags_precondition(reader, source, immutable["desired"])
            self._flags_bound(self._config(), record)
            try:
                with self._reader(profile) as writer:
                    for source in immutable["messages"]:
                        self._flags_writer(writer, source)
                        self._flags_precondition(writer, source, immutable["desired"], selected=True)
                        expected = dict(source["before"])
                        for wanted, modifier in ((True, "+FLAGS.SILENT"), (False, "-FLAGS.SILENT")):
                            names = [name for name, value in immutable["desired"].items()
                                     if value is wanted and source["before"][name] is not value]
                            if not names:
                                continue
                            # Detect intervening changes between this message's
                            # separate add/remove commands too. This narrows but
                            # cannot eliminate the declared preflight race.
                            self._flags_precondition(writer, {**source, "before": expected},
                                                     immutable["desired"], selected=True)
                            self._flags_bound(self._config(), record)
                            # Fixed allowlisted flag literals only; never reuse
                            # or replace the server's complete/custom flag set.
                            argument = "(" + " ".join(_TARGET_FLAGS[name] for name in names) + ")"
                            record.update(state="attempted", attempted=True)
                            record["store_attempts"].append({
                                **{name: source[name] for name in _REFERENCE_KEYS},
                                "modifier": modifier, "flags": names, "attempted_at": _now()})
                            write_json(path, record)  # Durable before STORE, even on timeout.
                            writes += 1
                            status, _ = writer.client.uid("STORE", source["uid"], modifier, argument)
                            if status != "OK":
                                raise DraftError("FLAGS_STORE_NOT_CONFIRMED")
                            expected.update({name: wanted for name in names})
            except Exception as exc:
                code = exc.code if isinstance(exc, DraftError) else "FLAGS_STORE_RESPONSE_UNCERTAIN"
                if not record.get("attempted"):
                    raise DraftError(code) from None
                record["store_error_code"] = code
                write_json(path, record)
            # New independent read-only session; never conclude from tagged OK.
            result = self._flags_observe(path, record, profile)
            result["mailbox_writes"] = writes
            return result

    @public
    def flags_status(self, preview_id):
        config = self._config()
        with self._flags_lock(config) as root:
            path, record = self._flags_load(root, preview_id)
            entry, profile = self._flags_read_bound(config, record)
            if record.get("attempted") or record["state"] == "verified":
                result = self._flags_observe(path, record, profile)
            else:
                result = self._flags_result(record)
            return self._flags_configuration_warning(result, entry, record)
