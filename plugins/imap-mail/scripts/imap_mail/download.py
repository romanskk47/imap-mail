"""Original MIME attachment export to private, host-owned plugin state only."""

from contextlib import contextmanager
from dataclasses import dataclass
import email
from email import policy
import os
from pathlib import Path
import re
import stat
import uuid

from . import read_access
from .core import DraftError, MAX_ATTACHMENT, digest, public


SHA256 = re.compile(r"[0-9a-f]{64}")
# Only MIME types select a fixed suffix; sender filenames are metadata, never paths.
SUFFIXES = {"application/pdf": ".pdf", "text/csv": ".csv", "image/png": ".png",
            "image/jpeg": ".jpg", "text/plain": ".txt"}


@dataclass(frozen=True)
class MimeRead:
    message: email.message.EmailMessage
    attachments: tuple
    payloads: dict
    unsupported_parts: tuple

    @property
    def attachment_manifest_complete(self):
        return not self.unsupported_parts

    def body_part(self, subtype):
        unsupported = {item["part_index"] for item in self.unsupported_parts}
        excluded = {id(part) for index, part in enumerate(self.message.walk()) if index in unsupported}

        def find(part):
            if id(part) in excluded or part.is_attachment():
                return None
            if part.get_content_type() == "text/" + subtype:
                return part
            if part.get_content_maintype() != "multipart":
                return None
            children = list(part.iter_parts())
            if part.get_content_subtype() == "related" and children:
                start = part.get_param("start")
                root = next((child for child in children if start and child["Content-ID"] == start), children[0])
                children = [root]
            for child in children:
                candidate = find(child)
                if candidate is not None:
                    return candidate
            return None

        return find(self.message)


def parse_mime(raw):
    """Validate complete MIME and distinguish exportable leaves from opaque containers."""
    try:
        if not raw.strip():
            raise ValueError("Empty MIME")
        message = email.message_from_bytes(raw, policy=policy.default)
        files, payloads, unsupported, opaque = [], {}, [], set()
        for index, part in enumerate(message.walk()):
            for name in ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding"):
                values = part.get_all(name, [])
                if len(values) > 1 or any(getattr(value, "defects", ()) for value in values):
                    raise ValueError("Ambiguous MIME header")
            encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
            if encoding not in {"7bit", "8bit", "binary", "base64", "quoted-printable"}:
                raise ValueError("Unsupported MIME encoding")
            if part.is_multipart():
                if id(part) not in opaque and (part.get_content_maintype() == "message" or
                        part.get_content_disposition() == "attachment" or
                        part.get_filename() is not None):
                    unsupported.append({"part_index": index, "filename": part.get_filename(),
                                        "type": part.get_content_type(),
                                        "reason": "UNSUPPORTED_ATTACHMENT_CONTAINER"})
                    opaque.update(id(child) for child in part.walk())
                continue
            # Decode all leaves: Python discovers some base64 defects only here.
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                raise ValueError("Undecodable MIME payload")
            disposition, filename = part.get_content_disposition(), part.get_filename()
            cid = str(part.get("Content-ID", ""))
            if id(part) not in opaque and (disposition == "attachment" or filename is not None or cid or
                    part.get_content_maintype() != "text"):
                files.append({"part_index": index, "filename": filename,
                              "type": part.get_content_type(), "disposition": disposition,
                              "cid": cid, "bytes": len(payload), "sha256": digest(payload)})
                payloads[index] = payload
        if any(part.defects for part in message.walk()):
            raise ValueError("Defective MIME")
        return MimeRead(message, tuple(files), payloads, tuple(unsupported))
    except DraftError:
        raise
    except Exception:
        raise DraftError("MALFORMED_MESSAGE_MIME") from None


def _private(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DraftError("UNSAFE_DOWNLOAD_DIRECTORY")


@contextmanager
def _downloads(state):
    """Walk with pinned directory descriptors; never follow symlink ancestors."""
    path = Path(state).absolute()
    if ".." in path.parts:
        raise DraftError("UNSAFE_DOWNLOAD_DIRECTORY")
    fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        for name in path.parts[1:]:
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _private(fd)
        try:
            os.mkdir("downloads", mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        child = os.open("downloads", flags, dir_fd=fd)
        os.close(fd)
        fd = child
        _private(fd)
        yield path / "downloads", fd
    except OSError:
        raise DraftError("UNSAFE_DOWNLOAD_DIRECTORY") from None
    finally:
        if fd is not None:
            os.close(fd)


def save_payload(state, payload, mime_type):
    """Create, sync and verify one new file. No supplied name or path is used."""
    with _downloads(state) as (downloads, parent):
        export_id = uuid.uuid4().hex
        # Fail closed on an improbable collision; never reuse an existing folder.
        try:
            os.mkdir(export_id, mode=0o700, dir_fd=parent)
        except OSError:
            raise DraftError("ATTACHMENT_SAVE_FAILED") from None
        directory = None
        created = False
        filename = "attachment" + SUFFIXES.get(mime_type, ".bin")
        try:
            directory = os.open(export_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            _private(directory)
            fd = os.open(filename, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            created = True
            with os.fdopen(fd, "w+b") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                stored = stream.read(MAX_ATTACHMENT + 1)
                if stored != payload or digest(stored) != digest(payload):
                    raise DraftError("ATTACHMENT_SAVE_VERIFICATION_FAILED")
            os.fsync(directory)
            os.fsync(parent)
            return str(downloads / export_id / filename)
        except Exception:
            # Only our newly created output is eligible for cleanup.
            if directory is not None and created:
                os.unlink(filename, dir_fd=directory)
            os.rmdir(export_id, dir_fd=parent)
            raise DraftError("ATTACHMENT_SAVE_FAILED") from None
        finally:
            if directory is not None:
                os.close(directory)


@public
def attachment_save(service, account, message, part_index, expected_sha256):
    required = {"account", "mailbox", "uidvalidity", "uid", "content_sha256"}
    if not isinstance(message, dict) or set(message) != required:
        raise DraftError("INVALID_MESSAGE_REFERENCE")
    if message["account"] != account:
        raise DraftError("CROSS_ACCOUNT_SOURCE")
    if (type(part_index) is not int or part_index < 0 or
            not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256) or
            not isinstance(message["content_sha256"], str) or not SHA256.fullmatch(message["content_sha256"])):
        raise DraftError("INVALID_ATTACHMENT_REFERENCE")
    config = service._config()
    _, profile = service._account(config, account, write=False)
    with service._reader(profile) as reader:
        raw, _ = service._full_raw(reader, message["mailbox"], message["uidvalidity"], message["uid"])
    if digest(raw) != message["content_sha256"]:
        raise DraftError("MESSAGE_CONTENT_CHANGED")
    mime = parse_mime(raw)
    if any(item["part_index"] == part_index for item in mime.unsupported_parts):
        raise DraftError("UNSUPPORTED_ATTACHMENT_CONTAINER")
    detail = next((item for item in mime.attachments if item["part_index"] == part_index), None)
    if detail is None:
        raise DraftError("ATTACHMENT_PART_UNAVAILABLE")
    if detail["sha256"] != expected_sha256:
        raise DraftError("ATTACHMENT_HASH_MISMATCH")
    payload = mime.payloads[part_index]
    if len(payload) > MAX_ATTACHMENT:
        raise DraftError("ATTACHMENT_TOO_LARGE")
    state = service._resolve(service.state_override or config.get("state_dir") or "~/.local/state/imap-mail")
    path = save_payload(state, payload, detail["type"])
    return {"status": "verified", **read_access.identity(profile), "message": message,
            "part_index": part_index, "original_filename": detail["filename"],
            "type": detail["type"], "bytes": len(payload), "sha256": digest(payload),
            "path": path, "mailbox_writes": 0, "smtp_submissions": 0,
            "contentTrust": "untrusted email attachment"}
