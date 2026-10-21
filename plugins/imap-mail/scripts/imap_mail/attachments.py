"""Reviewed attachment repair for an existing unsent draft, never SMTP.

Bodies on IMAP are immutable.  This module prepares a complete replacement;
EditService verifies it before retiring only the pinned old UID.  Ordinary
documents are promoted out of alternative/related trees without changing their
decoded bytes or any authored text, inline image, recipient, or thread header.
"""

import copy
import datetime as dt
import email
import email.encoders
from email import policy
from email.message import EmailMessage
from email.utils import make_msgid
from html.parser import HTMLParser
import json
import re
from urllib.parse import unquote
import uuid

from .core import (DraftError, MAX_RAW, MAX_ATTACHMENT, MAX_ATTACHMENT_TOTAL,
                   MAX_ATTACHMENTS, digest, header, profile_identity, public,
                   write_json, write_new)
from .edit import EditService, flags_argument


def _document(part):
    return (not part.is_multipart() and part.get_filename() is not None and
            part.get_content_maintype() != "image")


def _cid(value):
    value = unquote(value).strip()
    if value.casefold().startswith("cid:"):
        value = value[4:]
    value = value.strip("<>")
    if not value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise DraftError("DRAFT_ATTACHMENT_INVALID_CONTENT_ID")
    return value


class _AttachmentHTML(HTMLParser):
    """Locate only empty, explicitly identified attachment presentation nodes.

    Do not reserialize HTML: untouched markup/text must remain exact.  Unknown
    links, nonempty objects, and references hidden in other attributes fail
    closed instead of deleting possible authored text.
    """

    def __init__(self, source, document_cids, document_names, all_cids, orphan_limit):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0] + [match.end() for match in re.finditer("\n", source)]
        self.document_cids = document_cids
        self.document_names = document_names
        self.all_cids = all_cids
        self.orphan_limit = orphan_limit
        self.orphans = 0
        self.objects = []
        self.spans = []
        self.removed = []
        self.references = []

    def _offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def _attrs(self, attrs):
        values = dict(attrs)
        if len(values) != len(attrs):
            raise DraftError("DRAFT_ATTACHMENT_HTML_AMBIGUOUS")
        for value in values.values():
            for match in re.finditer(r"(?i)cid:([^\s\"'<>);]+)", value or ""):
                self.references.append((_cid(match.group(1)), self._offset()))
        return values

    def _candidate(self, attrs):
        if (attrs.get("type") or "").casefold() != "application/x-apple-msg-attachment":
            return None
        data = attrs.get("data") or ""
        linked = _cid(data) if data.casefold().startswith("cid:") else None
        declared = [attrs[key] for key in ("apple-msg-attachment-filename", "filename", "name", "data-filename")
                    if attrs.get(key)]
        if len(set(declared)) > 1:
            raise DraftError("DRAFT_ATTACHMENT_HTML_AMBIGUOUS")
        name = declared[0] if declared else None
        if name and name not in self.document_names:
            if linked in self.document_cids:
                raise DraftError("DRAFT_ATTACHMENT_HTML_AMBIGUOUS")
            return None
        if linked in self.document_cids or (name in self.document_names and linked is None):
            return {"cid": linked, "filename": name, "orphan": False}
        # Some Apple drafts have an empty placeholder for a file that was never
        # actually saved.  Removal is bounded by explicitly supplied new files;
        # an existing image CID can never be mistaken for an orphan document.
        if linked and linked not in self.all_cids and self.orphan_limit:
            return {"cid": linked, "filename": name, "orphan": True}
        return None

    def _remove(self, frame, end):
        candidate = frame["candidate"]
        if candidate is None:
            return
        if frame["content"]:
            raise DraftError("DRAFT_ATTACHMENT_HTML_NONEMPTY_PLACEHOLDER")
        if candidate["orphan"]:
            self.orphans += 1
            if self.orphans > self.orphan_limit:
                raise DraftError("DRAFT_ATTACHMENT_ORPHAN_PLACEHOLDER_AMBIGUOUS")
        self.spans.append((frame["start"], end))
        self.removed.append(candidate)

    def handle_starttag(self, tag, attrs):
        for frame in self.objects:
            frame["content"] = True
        values = self._attrs(attrs)
        if tag == "object":
            self.objects.append({"start": self._offset(), "candidate": self._candidate(values), "content": False})

    def handle_startendtag(self, tag, attrs):
        for frame in self.objects:
            frame["content"] = True
        values = self._attrs(attrs)
        if tag == "object":
            self._remove({"start": self._offset(), "candidate": self._candidate(values), "content": False},
                         self._offset() + len(self.get_starttag_text()))

    def handle_endtag(self, tag):
        if tag == "object" and self.objects:
            frame = self.objects.pop()
            end = self.source.find(">", self._offset())
            if end < 0:
                raise DraftError("DRAFT_ATTACHMENT_HTML_AMBIGUOUS")
            self._remove(frame, end + 1)
        elif self.objects:
            for frame in self.objects:
                frame["content"] = True

    def handle_data(self, data):
        if data.strip():
            for frame in self.objects:
                frame["content"] = True

    def handle_entityref(self, name):
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        self.handle_data("&#" + name + ";")

    def handle_comment(self, value):
        self.handle_data("<!--" + value + "-->")

    def handle_pi(self, value):
        self.handle_data("<?" + value + ">")

    def handle_decl(self, value):
        self.handle_data("<!" + value + ">")

    def unknown_decl(self, value):
        self.handle_data("<![" + value + "]>")

    def result(self):
        if any(frame["candidate"] is not None for frame in self.objects):
            raise DraftError("DRAFT_ATTACHMENT_HTML_AMBIGUOUS")
        self.spans.sort()
        if any(left[1] > right[0] for left, right in zip(self.spans, self.spans[1:])):
            raise DraftError("DRAFT_ATTACHMENT_HTML_AMBIGUOUS")
        for cid, offset in self.references:
            if cid in self.document_cids and not any(start <= offset < end for start, end in self.spans):
                raise DraftError("DRAFT_ATTACHMENT_HTML_REFERENCE_UNSUPPORTED")
        result = self.source
        for start, end in reversed(self.spans):
            result = result[:start] + result[end:]
        return result, self.removed


def _normalize_html(source, document_cids, document_names, all_cids, orphan_limit):
    parser = _AttachmentHTML(source, document_cids, document_names, all_cids, orphan_limit)
    parser.feed(source)
    parser.close()
    return parser.result()


def _leaf_fingerprint(part):
    data = part.get_payload(decode=True) or b""
    if part.get_content_type() in ("text/plain", "text/html") and not part.get_filename():
        payload = part.get_content().replace("\r\n", "\n").replace("\r", "\n")
    else:
        payload = digest(data)
    return {"type": part.get_content_type(), "filename": part.get_filename(),
            "disposition": part.get_content_disposition(), "cid": str(part.get("Content-ID", "")),
            "payload": payload}


def _headers(message):
    excluded = {"message-id", "mime-version", "content-type", "content-transfer-encoding", "content-disposition"}
    return [(name.casefold(), str(value)) for name, value in message.items() if name.casefold() not in excluded]


class AttachmentService(EditService):
    def _attachment_review(self, message, entry):
        """Narrow review adapter, without loosening the SMTP source validator."""
        if any(part.defects for part in message.walk()):
            raise DraftError("MALFORMED_DRAFT_MIME")
        for part in message.walk():
            if any(len(part.get_all(name, [])) > 1 for name in
                   ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding", "Content-ID")):
                raise DraftError("DRAFT_ATTACHMENT_MIME_HEADERS_AMBIGUOUS")
        adjusted = copy.deepcopy(message)
        for name in ("X-Apple-Notify-Thread", "X-Apple-Message-Smime-Encrypt"):
            values = adjusted.get_all(name, [])
            if len(values) > 1 or any(str(value).upper() not in ("NO", "FALSE", "0") for value in values):
                raise DraftError("UNSUPPORTED_DRAFT_HEADERS")
            if values:
                del adjusted[name]
        originals = list(message.walk())
        for part in adjusted.walk():
            if part.is_multipart() and (part.get_filename() or part.get_content_disposition() == "attachment"):
                raise DraftError("DRAFT_ATTACHMENT_CONTAINER_UNSUPPORTED")
            if _document(part) and part.get_content_disposition() != "attachment":
                filename = part.get_filename()
                if part.get("Content-Disposition"):
                    del part["Content-Disposition"]
                part.add_header("Content-Disposition", "attachment", filename=filename)
        review = self._existing_mime(adjusted.as_bytes(policy=policy.SMTP), entry)
        for part, summary in zip(originals, review["mime_parts"]):
            if not part.is_multipart():
                summary["disposition"] = part.get_content_disposition()
        for part, summary in zip([p for p in originals if not p.is_multipart() and
                                   (p.get_content_disposition() == "attachment" or p.get_filename())], review["attachments"]):
            summary["disposition"] = part.get_content_disposition()
        return review

    @public
    def draft_attachment_preview(self, account, draft, attachments=None, normalize_attachments=True):
        config = self._config()
        entry, profile = self._account(config, account, write=True)
        required = {"account", "mailbox", "uidvalidity", "uid"}
        if not isinstance(draft, dict) or set(draft) != required:
            raise DraftError("INVALID_DRAFT_REFERENCE")
        if draft["account"] != account:
            raise DraftError("DRAFT_ACCOUNT_MISMATCH")
        if not isinstance(normalize_attachments, bool):
            raise DraftError("INVALID_ATTACHMENT_NORMALIZATION")
        files = self._attachments(entry, attachments)
        with self._reader(profile) as reader:
            folder, validity = self._drafts(reader, entry)
            if draft["mailbox"] != folder or str(draft["uidvalidity"]) != validity:
                raise DraftError("DRAFTS_FOLDER_CHANGED")
            self._edit_capabilities(reader)
            original, flags = self._full_raw(reader, folder, validity, draft["uid"])
            flags_argument(flags)
        message = email.message_from_bytes(original, policy=policy.default)
        before = self._attachment_review(message, entry)
        source = {**{key: str(draft[key]) for key in required}, "content_sha256": digest(original),
                  "message_id": before["message_id"], "flags": flags}
        existing = [part for part in message.walk() if not part.is_multipart() and part.get_filename()]
        names = {part.get_filename().casefold() for part in existing}
        hashes = {digest(part.get_payload(decode=True) or b"") for part in existing}
        for file in files:
            if file["filename"].casefold() in names or file["sha256"] in hashes:
                raise DraftError("DRAFT_ATTACHMENT_DUPLICATE_FILE")
            names.add(file["filename"].casefold())
            hashes.add(file["sha256"])
        if len(existing) + len(files) > MAX_ATTACHMENTS:
            raise DraftError("ATTACHMENT_LIMIT")
        sizes = [len(part.get_payload(decode=True) or b"") for part in existing] + [file["bytes"] for file in files]
        if any(size > MAX_ATTACHMENT for size in sizes) or sum(sizes) > MAX_ATTACHMENT_TOTAL:
            raise DraftError("ATTACHMENT_LIMIT")
        documents = [part for part in message.walk() if _document(part)]
        for part in documents:
            header(part.get_filename())
        all_cids = {_cid(str(part["Content-ID"])) for part in message.walk() if part.get("Content-ID")}
        document_cids = {_cid(str(part["Content-ID"])) for part in documents if part.get("Content-ID")}
        if sum(bool(part.get("Content-ID")) for part in message.walk()) != len(all_cids):
            raise DraftError("DRAFT_ATTACHMENT_CONTENT_ID_AMBIGUOUS")
        if normalize_attachments:
            for part in message.walk():
                start = part.get_param("start") if part.get_content_type() == "multipart/related" else None
                if start and (_cid(str(start)) in document_cids or _cid(str(start)) not in all_cids):
                    raise DraftError("DRAFT_ATTACHMENT_RELATED_ROOT_UNSUPPORTED")
        document_names = {part.get_filename() for part in documents} | {file["filename"] for file in files}
        removed = []
        expected_leaves = []
        for part in message.walk():
            if part.is_multipart() or (normalize_attachments and _document(part)):
                continue
            if normalize_attachments and part.get_content_type() == "text/html" and not part.get_filename():
                content = part.get_content()
                cleaned, wrappers = _normalize_html(content, document_cids, document_names, all_cids, len(files))
                removed.extend(wrappers)
                if len([item for item in removed if item["orphan"]]) > len(files):
                    raise DraftError("DRAFT_ATTACHMENT_ORPHAN_PLACEHOLDER_AMBIGUOUS")
                if cleaned != content:
                    if part.get("Content-Transfer-Encoding"):
                        del part["Content-Transfer-Encoding"]
                    part.set_payload(cleaned.encode("utf-8"))
                    part.set_param("charset", "utf-8", header="Content-Type")
                    email.encoders.encode_quopri(part)
            expected_leaves.append(_leaf_fingerprint(part))
        direct = list(message.iter_parts()) if message.is_multipart() else []
        needs_normalization = normalize_attachments and (removed or any(
            part not in direct or message.get_content_type() != "multipart/mixed" or
            part.get_content_disposition() != "attachment" or part.get("Content-ID")
            for part in documents))
        if not files and not needs_normalization:
            raise DraftError("DRAFT_ATTACHMENT_NO_CHANGE")
        if normalize_attachments:
            def detach(parent):
                if parent.is_multipart():
                    kept = []
                    for child in parent.iter_parts():
                        if _document(child):
                            continue
                        detach(child)
                        if child.is_multipart() and not list(child.iter_parts()):
                            raise DraftError("DRAFT_ATTACHMENT_EMPTY_BODY_CONTAINER")
                        kept.append(child)
                    parent.set_payload(kept)
            detach(message)
        if not any(not part.is_multipart() and not part.get_filename() and
                   part.get_content_type() in ("text/plain", "text/html") for part in message.walk()):
            raise DraftError("DRAFT_EDIT_BODY_UNAVAILABLE")
        if message.get_content_type() != "multipart/mixed":
            message.make_mixed()
        promoted = []
        if normalize_attachments:
            for part in documents:
                filename = part.get_filename()
                if part.get("Content-Disposition"):
                    del part["Content-Disposition"]
                part.add_header("Content-Disposition", "attachment", filename=filename)
                if part.get("Content-ID"):
                    del part["Content-ID"]
                message.attach(part)
                promoted.append({"filename": filename, "type": part.get_content_type(),
                                 "bytes": len(part.get_payload(decode=True) or b""),
                                 "sha256": digest(part.get_payload(decode=True) or b"")})
        for file in files:
            maintype, subtype = file["type"].split("/", 1)
            message.add_attachment(file["data"], maintype=maintype, subtype=subtype, filename=file["filename"])
        message.replace_header("Message-ID", make_msgid(domain=before["from"].rsplit("@", 1)[1]))
        raw = message.as_bytes(policy=policy.SMTP)
        if len(raw) > MAX_RAW:
            raise DraftError("DRAFT_TOO_LARGE")
        saved = email.message_from_bytes(raw, policy=policy.default)
        after = self._attachment_review(saved, entry)
        original_message = email.message_from_bytes(original, policy=policy.default)
        if _headers(original_message) != _headers(saved):
            raise DraftError("DRAFT_EDIT_HEADERS_CHANGED")
        added_parts = list(saved.iter_parts())[-len(files):] if files else []
        retained = [_leaf_fingerprint(part) for part in saved.walk() if not part.is_multipart() and
                    not (normalize_attachments and _document(part)) and
                    not any(part is added for added in added_parts)]
        if expected_leaves != retained:
            raise DraftError("DRAFT_EDIT_UNREQUESTED_PART_CHANGED")
        expected_files = [(part.get_filename(), part.get_content_type(), digest(part.get_payload(decode=True) or b""))
                          for part in original_message.walk() if not part.is_multipart() and
                          (part.get_content_disposition() == "attachment" or part.get_filename())]
        expected_files += [(file["filename"], file["type"], file["sha256"]) for file in files]
        actual_files = [(item["filename"], item["type"], item["sha256"]) for item in after["attachments"]]
        if sorted(expected_files, key=repr) != sorted(actual_files, key=repr):
            raise DraftError("DRAFT_EDIT_ATTACHMENTS_CHANGED")
        top_level = [{"filename": part.get_filename(), "type": part.get_content_type(),
                      "bytes": len(part.get_payload(decode=True) or b""),
                      "sha256": digest(part.get_payload(decode=True) or b""),
                      "disposition": part.get_content_disposition(), "cid": str(part.get("Content-ID", ""))}
                     for part in saved.iter_parts() if not part.is_multipart() and part.get_filename()]
        if normalize_attachments:
            required_docs = [item for item in after["attachments"] if not item["type"].startswith("image/")]
            if any(item["disposition"] != "attachment" or item["cid"] for item in required_docs) or any(
                    not any(item["filename"] == top["filename"] and item["sha256"] == top["sha256"]
                            for top in top_level) for item in required_docs):
                raise DraftError("DRAFT_ATTACHMENT_TOP_LEVEL_CHECK_FAILED")
        preview_id = uuid.uuid4().hex
        review = {"account": account, "identity": profile_identity(profile), **after,
                  "operation": "draft_edit", "edit_kind": "attachments", "draft_source": source, "before": before,
                  "added_attachments": [{key: file[key] for key in ("filename", "type", "bytes", "sha256")} for file in files],
                  "promoted_attachments": promoted, "removed_attachment_placeholders": removed,
                  "top_level_attachments": top_level, "normalize_attachments": normalize_attachments,
                  "invariants": {"recipients_preserved": True, "headers_preserved": True,
                                 "authored_text_and_signature_preserved": True, "existing_file_bytes_preserved": True,
                                 "inline_images_preserved": True},
                  "thread_headers": {name: str(saved.get(name, "")) for name in ("In-Reply-To", "References", "Date")},
                  "drafts_mailbox": folder, "drafts_uidvalidity": validity,
                  "replacement_method": "verified_append_then_uid_expunge",
                  "original_backup": preview_id + ".original.eml"}
        immutable = {"preview_id": preview_id, "operation": "draft_edit", "review": review,
                     "draft_source": source, "mime_sha256": digest(raw), "original_sha256": digest(original),
                     "account_config_sha256": digest(entry)}
        record = {"version": 1, "immutable": immutable, "preview_hash": digest(immutable), "state": "prepared",
                  "append_attempted": False, "retire_attempted": False, "expunge_attempted": False,
                  "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "receipt": None}
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
