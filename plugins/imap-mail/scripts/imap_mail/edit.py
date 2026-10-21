"""Reviewed unsent-draft replacement, never SMTP or general mailbox cleanup.

IMAP bodies are immutable. Verify one replacement before retiring only the
pinned original UID. APPEND intent is durable and can never be replayed.
"""

import datetime as dt
import email
import email.encoders
from email import policy
from email.utils import make_msgid
import html
import json
import os
import re
import uuid

from . import read_access
from .core import (DraftError, MAX_BODY, MAX_RAW, STABLE_ID, _HTMLTextEquivalence,
                   digest, private_read, profile_identity, public, semantic,
                   write_json, write_new)
from .send import SendService


def replacements_validated(replacements):
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= 20:
        raise DraftError("INVALID_DRAFT_REPLACEMENTS")
    result = []
    for item in replacements:
        if not isinstance(item, dict) or set(item) != {"old_text", "new_text"}:
            raise DraftError("INVALID_DRAFT_REPLACEMENTS")
        old, new = item["old_text"], item["new_text"]
        if (not isinstance(old, str) or not old or not isinstance(new, str) or
                max(len(old), len(new)) > MAX_BODY or "\0" in old + new):
            raise DraftError("INVALID_DRAFT_REPLACEMENTS")
        old, new = (v.replace("\r\n", "\n").replace("\r", "\n") for v in (old, new))
        if old == new:
            raise DraftError("DRAFT_EDIT_NO_CHANGE")
        result.append({"old_text": old, "new_text": new})
    return result


def replace_text(text, replacements, *, allow_missing=False):
    """Simultaneous, unique, non-overlapping matches against original text."""
    spans = []
    counts = []
    for item in replacements:
        found = [(m.start(), m.start() + len(item["old_text"]))
                 for m in re.finditer("(?=" + re.escape(item["old_text"]) + ")", text)]
        counts.append(len(found))
        if len(found) > 1 or (not found and not allow_missing):
            raise DraftError("DRAFT_EDIT_TEXT_NOT_UNIQUE")
        if found:
            spans.append((*found[0], item["new_text"]))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise DraftError("DRAFT_EDIT_OVERLAPPING_REPLACEMENTS")
    for start, end, new in reversed(spans):
        text = text[:start] + new + text[end:]
    return text, counts


def replace_html(source, replacements):
    try:
        parser = _HTMLTextEquivalence(source)
        parser.feed(source)
        parser.close()
        tokens = parser.value()
    except Exception:
        raise DraftError("DRAFT_EDIT_HTML_UNSUPPORTED") from None
    if tokens == [("raw", source)]:
        raise DraftError("DRAFT_EDIT_HTML_UNSUPPORTED")
    counts = [0] * len(replacements)
    output = []
    events = parser.events
    if not events or events[0][0] != 0:
        events = [(0, False)] + events
    for index, (start, is_text) in enumerate(events):
        end = events[index + 1][0] if index + 1 < len(events) else len(source)
        original = source[start:end]
        if not is_text:
            output.append(original)
            continue
        value = html.unescape(original)
        changed, found = replace_text(value, replacements, allow_missing=True)
        counts = [a + b for a, b in zip(counts, found)]
        # Only affected text nodes are re-escaped. Untouched text/markup,
        # signature formatting and attribute spelling remain byte-for-byte.
        output.append(html.escape(changed, quote=False) if any(found) else original)
    if counts != [1] * len(replacements):
        raise DraftError("DRAFT_EDIT_HTML_TEXT_NOT_UNIQUE_OR_SPLIT")
    return "".join(output)


def flags_normalized(flags):
    return {f.casefold() for f in flags if f.casefold() != "\\recent"}


def flags_argument(flags):
    # Never reflect arbitrary server bytes into a command. No server-only
    # Recent, Deleted, wildcard or delimiter can become part of APPEND flags.
    kept = [f for f in flags if f.casefold() != "\\recent"]
    if any(not re.fullmatch(r"\\?[A-Za-z0-9_.$-]+", f) for f in kept):
        raise DraftError("UNSUPPORTED_DRAFT_FLAGS")
    if "\\draft" not in flags_normalized(kept) or "\\deleted" in flags_normalized(kept):
        raise DraftError("DRAFT_FLAG_NOT_CONFIRMED")
    return "(" + " ".join(kept) + ")"


class EditService(SendService):
    def _prevent_source_replay(self, root, source, preview_id=None, message_id=None):
        super()._prevent_source_replay(root, source, preview_id, message_id)
        if not source:
            return
        for path in root.glob("*.json"):
            if path.stem == preview_id:
                continue
            _, other, _ = self._load(root, path.stem)
            immutable = other["immutable"]
            if immutable.get("operation") != "draft_edit" or not other.get("append_attempted"):
                continue
            previous = immutable["draft_source"]
            same_account = source["account"] == previous["account"]
            old_source = self._source_key(previous) == self._source_key(source)
            old_message = same_account and message_id == previous["message_id"]
            pending_new = (same_account and other["state"] != "verified" and
                           message_id == immutable["review"]["message_id"])
            if old_source or old_message or pending_new:
                raise DraftError("DRAFT_SOURCE_EDIT_ALREADY_ATTEMPTED")

    def _edit_folder(self, reader, entry, record):
        review = record["immutable"]["review"]
        folder, validity = self._drafts(reader, entry)
        if folder != review["drafts_mailbox"] or validity != review["drafts_uidvalidity"]:
            raise DraftError("DRAFTS_FOLDER_CHANGED")
        return folder, validity

    def _edit_capabilities(self, reader):
        status, rows = reader.client.capability()
        if status != "OK" or not isinstance(rows, list):
            raise DraftError("DRAFT_EDIT_CAPABILITY_CHECK_FAILED")
        capabilities = {item.upper() for row in rows if isinstance(row, bytes) for item in row.split()}
        if b"UIDPLUS" not in capabilities:
            raise DraftError("DRAFT_EDIT_REQUIRES_UIDPLUS")

    def _edit_source(self, reader, record, *, selected=False):
        source = record["immutable"]["draft_source"]
        status, rows = reader.client.uid("SEARCH", None, "UID", source["uid"])
        matches = self._edit_search_rows(status, rows)
        if not matches:
            if record.get("retire_attempted"):
                return None
            raise DraftError("DRAFT_EDIT_SOURCE_DISAPPEARED")
        if matches != [source["uid"]]:
            raise DraftError("DRAFT_EDIT_SOURCE_NOT_UNIQUE")
        raw, flags = (self._fetch_full_raw(reader, source["uid"]) if selected else
                      self._full_raw(reader, source["mailbox"], source["uidvalidity"], source["uid"]))
        if digest(raw) != source["content_sha256"]:
            raise DraftError("DRAFT_EDIT_SOURCE_CHANGED")
        actual = flags_normalized(flags)
        expected = flags_normalized(source["flags"])
        if record.get("retire_attempted"):
            actual.discard("\\deleted")
        if actual != expected:
            raise DraftError("DRAFT_EDIT_SOURCE_FLAGS_CHANGED")
        return flags

    def _edit_search_rows(self, status, rows):
        if status != "OK" or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], bytes):
            raise DraftError("DRAFT_EDIT_SEARCH_FAILED")
        values = rows[0].split()
        if any(not STABLE_ID.fullmatch(v.decode("ascii")) for v in values) or len(set(values)) != len(values):
            raise DraftError("DRAFT_EDIT_SEARCH_FAILED")
        return [v.decode("ascii") for v in values]

    def _edit_replacement(self, reader, record, raw, *, selected=False):
        review = record["immutable"]["review"]
        status, rows = reader.client.uid("SEARCH", None, "HEADER", "Message-ID", reader.client._quote(review["message_id"]))
        matches = self._edit_search_rows(status, rows)
        if len(matches) != 1:
            raise DraftError("DRAFT_EDIT_REPLACEMENT_NOT_UNIQUE")
        uid = matches[0]
        if uid == record["immutable"]["draft_source"]["uid"]:
            raise DraftError("DRAFT_EDIT_REPLACEMENT_IDENTITY_MISMATCH")
        mapping = record.get("mapping")
        if record.get("appenduid_invalid"):
            raise DraftError("APPENDUID_INVALID")
        if mapping and (mapping["uid"] != uid or mapping["uidvalidity"] != review["drafts_uidvalidity"]):
            raise DraftError("DRAFT_EDIT_REPLACEMENT_IDENTITY_MISMATCH")
        saved, flags = (self._fetch_full_raw(reader, uid) if selected else
                        self._full_raw(reader, review["drafts_mailbox"], review["drafts_uidvalidity"], uid))
        if semantic(saved) != semantic(raw):
            raise DraftError("DRAFT_EDIT_REPLACEMENT_CHANGED")
        flags_argument(flags)
        return {"account": review["account"], "mailbox": review["drafts_mailbox"],
                "uidvalidity": review["drafts_uidvalidity"], "uid": uid,
                "message_id": review["message_id"], "semantic_sha256": digest(semantic(saved)),
                "flags": flags, "verified_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    @public
    def draft_edit_preview(self, account, draft, replacements):
        config = self._config()
        entry, profile = self._account(config, account, write=True)
        required = {"account", "mailbox", "uidvalidity", "uid"}
        if not isinstance(draft, dict) or set(draft) != required:
            raise DraftError("INVALID_DRAFT_REFERENCE")
        if draft["account"] != account:
            raise DraftError("DRAFT_ACCOUNT_MISMATCH")
        changes = replacements_validated(replacements)
        with self._reader(profile) as reader:
            folder, validity = self._drafts(reader, entry)
            if draft["mailbox"] != folder or str(draft["uidvalidity"]) != validity:
                raise DraftError("DRAFTS_FOLDER_CHANGED")
            self._edit_capabilities(reader)
            original, flags = self._full_raw(reader, folder, validity, draft["uid"])
            flags_argument(flags)
        before = self._existing_mime(original, entry)
        source = {**{k: str(draft[k]) for k in required}, "content_sha256": digest(original),
                  "message_id": before["message_id"], "flags": flags}
        message = email.message_from_bytes(original, policy=policy.default)
        body_count = 0
        changed_parts = []
        expected_content = {}
        for index, part in enumerate(message.walk()):
            if (part.is_multipart() or part.get_content_disposition() == "attachment" or
                    part.get_filename() is not None or part.get_content_type() not in ("text/plain", "text/html")):
                continue
            content = part.get_content().replace("\r\n", "\n").replace("\r", "\n")
            if not content.strip():
                continue  # Some providers retain an empty plain alternative.
            body_count += 1
            revised = (replace_html(content, changes) if part.get_content_type() == "text/html" else
                       replace_text(content, changes)[0])
            subtype = part.get_content_subtype()
            # Replace only the transfer encoding and content bytes; keep CID,
            # disposition, filename absence and all other part metadata.
            if part.get("Content-Transfer-Encoding"):
                del part["Content-Transfer-Encoding"]
            part.set_payload(revised.encode("utf-8"))
            part.set_param("charset", "utf-8", header="Content-Type")
            email.encoders.encode_quopri(part)
            changed_parts.append({"index": index, "type": "text/" + subtype})
            expected_content[index] = revised
        if not body_count:
            raise DraftError("DRAFT_EDIT_BODY_UNAVAILABLE")
        message.replace_header("Message-ID", make_msgid(domain=before["from"].rsplit("@", 1)[1]))
        raw = message.as_bytes(policy=policy.SMTP)
        if len(raw) > MAX_RAW:
            raise DraftError("DRAFT_TOO_LARGE")
        after = self._existing_mime(raw, entry)
        if before["attachments"] != after["attachments"]:
            raise DraftError("DRAFT_EDIT_ATTACHMENTS_CHANGED")
        changed_indices = {item["index"] for item in changed_parts}
        if len(before["mime_parts"]) != len(after["mime_parts"]):
            raise DraftError("DRAFT_EDIT_MIME_STRUCTURE_CHANGED")
        for old_part, new_part in zip(before["mime_parts"], after["mime_parts"]):
            ignore = {"bytes", "sha256", "content"} if old_part["index"] in changed_indices else set()
            if ({k: v for k, v in old_part.items() if k not in ignore} !=
                    {k: v for k, v in new_part.items() if k not in ignore}):
                raise DraftError("DRAFT_EDIT_UNREQUESTED_PART_CHANGED")
            if old_part["index"] in changed_indices:
                actual_content = new_part.get("content", "").replace("\r\n", "\n").replace("\r", "\n")
                if actual_content != expected_content[old_part["index"]]:
                    raise DraftError("DRAFT_EDIT_BODY_SERIALIZATION_CHANGED")
        headers_before = [(k.casefold(), str(v)) for k, v in email.message_from_bytes(original, policy=policy.default).items()
                          if k.casefold() != "message-id"]
        headers_after = [(k.casefold(), str(v)) for k, v in message.items() if k.casefold() != "message-id"]
        # A single-part text body legitimately changes charset/transfer encoding.
        excluded = {"content-type", "content-transfer-encoding"} if not message.is_multipart() else set()
        if [p for p in headers_before if p[0] not in excluded] != [p for p in headers_after if p[0] not in excluded]:
            raise DraftError("DRAFT_EDIT_HEADERS_CHANGED")
        preview_id = uuid.uuid4().hex
        review = {"account": account, "identity": profile_identity(profile), **after,
                  "operation": "draft_edit", "draft_source": source, "before": before,
                  "replacements": changes, "changed_parts": changed_parts,
                  "thread_headers": {name: str(message.get(name, ""))
                                     for name in ("In-Reply-To", "References", "Date")},
                  "drafts_mailbox": folder, "drafts_uidvalidity": validity,
                  "replacement_method": "verified_append_then_uid_expunge",
                  "original_backup": preview_id + ".original.eml"}
        immutable = {"preview_id": preview_id, "operation": "draft_edit", "review": review,
                     "draft_source": source, "mime_sha256": digest(raw),
                     "original_sha256": digest(original), "account_config_sha256": digest(entry)}
        record = {"version": 1, "immutable": immutable, "preview_hash": digest(immutable),
                  "state": "prepared", "append_attempted": False, "retire_attempted": False,
                  "expunge_attempted": False, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "receipt": None}
        if len(json.dumps(record, ensure_ascii=False).encode("utf-8")) > 4 * 1024 * 1024:
            raise DraftError("DRAFT_EDIT_REVIEW_TOO_LARGE")
        with self._lock(config) as root:
            self._prevent_source_replay(root, source, message_id=source["message_id"])
            self._bound(self._config(), record)
            write_new(root / (preview_id + ".original.eml"), original)
            write_new(root / (preview_id + ".eml"), raw)
            write_json(root / (preview_id + ".json"), record)
        return {"status": "prepared", "preview_id": preview_id, "preview_hash": record["preview_hash"],
                **review, "mailbox_writes": 0, "smtp_submissions": 0}

    def _edit_load(self, root, preview_id):
        path, record, raw = self._load(root, preview_id)
        if record["immutable"].get("operation") != "draft_edit":
            raise DraftError("PREVIEW_OPERATION_MISMATCH")
        original = private_read(root / (preview_id + ".original.eml"))
        if digest(original) != record["immutable"]["original_sha256"]:
            raise DraftError("DRAFT_EDIT_BACKUP_INTEGRITY_FAILED")
        return path, record, raw

    def _edit_result(self, record):
        review = record["immutable"]["review"]
        result = {"status": record["state"], "preview_id": record["immutable"]["preview_id"],
                  "preview_hash": record["preview_hash"], "account": review["account"],
                  "subject": review["subject"], "receipt": record.get("receipt"),
                  "append_attempted": record.get("append_attempted", False),
                  "retire_attempted": record.get("retire_attempted", False),
                  "original_backup": review["original_backup"], "mailbox_writes": 0, "smtp_submissions": 0}
        if record.get("error_code"):
            result["error_code"] = record["error_code"]
        if record["state"] != "verified":
            for key in ("append_error_code", "cleanup_error_code"):
                if record.get(key):
                    result[key] = record[key]
        return result

    def _edit_observe(self, path, record, raw, entry, profile):
        try:
            with self._reader(profile) as reader:
                self._edit_folder(reader, entry, record)
                receipt = self._edit_replacement(reader, record, raw)
                source_flags = self._edit_source(reader, record)
            record["receipt"] = receipt
            record["state"] = "verified" if source_flags is None else "cleanup_pending"
            record.pop("error_code", None)
        except Exception as exc:
            code = exc.code if isinstance(exc, DraftError) else "DRAFT_EDIT_READBACK_FAILED"
            record["state"] = "conflict" if code in {
                "DRAFT_EDIT_SOURCE_DISAPPEARED", "DRAFT_EDIT_SOURCE_CHANGED", "DRAFT_EDIT_SOURCE_FLAGS_CHANGED",
                "DRAFT_EDIT_REPLACEMENT_CHANGED", "DRAFTS_FOLDER_CHANGED"} else "uncertain"
            record["error_code"] = code
        write_json(path, record)
        return self._edit_result(record)

    def _edit_writer(self, reader, entry, record):
        folder, validity = self._edit_folder(reader, entry, record)
        self._edit_capabilities(reader)
        status, _ = reader.client.select(reader.client._quote(read_access.utf7_encode(folder)), readonly=False)
        _, values = reader.client.response("UIDVALIDITY")
        values = [v for v in (values or []) if isinstance(v, bytes)]
        if status != "OK" or values != [validity.encode()]:
            raise DraftError("DRAFT_EDIT_WRITE_SELECTION_FAILED")
        if getattr(reader.client, "is_readonly", False):
            raise DraftError("DRAFT_EDIT_WRITE_SELECTION_FAILED")

    @public
    def draft_edit_apply(self, preview_id, preview_hash):
        config = self._config()
        writes = 0
        with self._lock(config) as root:
            path, record, raw = self._edit_load(root, preview_id)
            if not isinstance(preview_hash, str) or preview_hash != record["preview_hash"]:
                raise DraftError("PREVIEW_HASH_MISMATCH")
            entry, profile = self._bound(config, record)
            source = record["immutable"]["draft_source"]
            self._prevent_source_replay(root, source, preview_id, source["message_id"])
            if not record["append_attempted"]:
                with self._reader(profile) as reader:
                    folder, _ = self._edit_folder(reader, entry, record)
                    self._edit_capabilities(reader)
                    self._edit_source(reader, record)
                    self._bound(self._config(), record)
                    record.update(state="append_attempted", append_attempted=True)
                    write_json(path, record)  # Never reset, even if upload never reached the server.
                    writes += 1
                    try:
                        sock = getattr(reader.client, "sock", None)
                        if sock is not None:
                            sock.settimeout(60)
                        status, data = reader.client.append(reader.client._quote(read_access.utf7_encode(folder)),
                            flags_argument(source["flags"]), None, raw)
                        if status != "OK":
                            raise DraftError("APPEND_NOT_CONFIRMED")
                        try:
                            record["mapping"] = self._appenduid(reader.client, data)
                        except DraftError:
                            record["appenduid_invalid"] = True
                            raise
                        write_json(path, record)
                    except Exception as exc:
                        record["append_error_code"] = exc.code if isinstance(exc, DraftError) else "APPEND_RESPONSE_UNCERTAIN"
                        write_json(path, record)
            self._edit_observe(path, record, raw, entry, profile)
            if record["state"] != "cleanup_pending":
                result = self._edit_result(record)
                result["mailbox_writes"] = writes
                return result
            try:
                self._bound(self._config(), record)
                with self._reader(profile) as writer:
                    self._edit_writer(writer, entry, record)
                    # These reads keep the explicit READ-WRITE selection intact.
                    self._edit_replacement(writer, record, raw, selected=True)
                    source_flags = self._edit_source(writer, record, selected=True)
                    self._bound(self._config(), record)
                    if source_flags is not None:
                        if "\\deleted" not in flags_normalized(source_flags):
                            record.update(state="retire_attempted", retire_attempted=True)
                            write_json(path, record)
                            writes += 1
                            status, _ = writer.client.uid("STORE", source["uid"], "+FLAGS.SILENT", "(\\Deleted)")
                            if status != "OK":
                                raise DraftError("DRAFT_EDIT_RETIRE_NOT_CONFIRMED")
                        # Independently recheck both UIDs before permanent retirement.
                        self._edit_replacement(writer, record, raw, selected=True)
                        flags = self._edit_source(writer, record, selected=True)
                        self._bound(self._config(), record)
                        if flags is not None:
                            if "\\deleted" not in flags_normalized(flags):
                                raise DraftError("DRAFT_EDIT_RETIRE_NOT_CONFIRMED")
                            record.update(state="expunge_attempted", expunge_attempted=True)
                            write_json(path, record)
                            writes += 1
                            status, _ = writer.client.uid("EXPUNGE", source["uid"])
                            if status != "OK":
                                raise DraftError("DRAFT_EDIT_EXPUNGE_NOT_CONFIRMED")
            except Exception as exc:
                record["cleanup_error_code"] = exc.code if isinstance(exc, DraftError) else "DRAFT_EDIT_RETIRE_RESPONSE_UNCERTAIN"
                write_json(path, record)
            result = self._edit_observe(path, record, raw, entry, profile)
            if record.get("cleanup_error_code") and result["status"] != "verified":
                result["cleanup_error_code"] = record["cleanup_error_code"]
            result["mailbox_writes"] = writes
            return result

    @public
    def draft_edit_status(self, preview_id):
        config = self._config()
        with self._lock(config) as root:
            path, record, raw = self._edit_load(root, preview_id)
            entry, profile = self._bound(config, record)
            if record["append_attempted"]:
                return self._edit_observe(path, record, raw, entry, profile)
            return self._edit_result(record)
