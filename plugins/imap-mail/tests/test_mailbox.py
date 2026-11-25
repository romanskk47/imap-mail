"""Offline move/trash/archive and permanent-delete tests with a synthetic IMAP server."""

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError
from imap_mail.mailbox import MailboxService
from imap_mail.read_access import utf7_decode


def raw(subject, message_id):
    return (f"Message-ID: {message_id}\r\nSubject: {subject}\r\n\r\nBody of {subject}\r\n").encode()


class MailServer:
    FOLDERS = {"INBOX": [], "Drafts": ["\\Drafts"], "Trash": ["\\Trash"],
               "Archive": ["\\Archive"], "Projects": []}

    def __init__(self, capabilities=("IMAP4rev1", "MOVE", "UIDPLUS")):
        self.capabilities = list(capabilities)
        self.validity = {name: str(index + 3) for index, name in enumerate(self.FOLDERS)}
        self.messages = {name: {} for name in self.FOLDERS}
        self.flags = {}
        self.next_uid = {name: 100 for name in self.FOLDERS}
        self.commands = []
        self.writes = []
        self.fail_on = None          # e.g. "MOVE": raise after mutating
        self.refuse = None           # e.g. "MOVE": return NO without mutating
        self.no_copyuid = False
        self.ignore = None           # e.g. "EXPUNGE": OK without effect
        self.empty_search_untagged = True  # servers may omit "* SEARCH" when empty

    def add(self, mailbox, subject, message_id, flags=()):
        uid = str(self.next_uid[mailbox])
        self.next_uid[mailbox] += 1
        self.messages[mailbox][uid] = raw(subject, message_id)
        self.flags[(mailbox, uid)] = list(flags)
        return {"account": "one", "mailbox": mailbox, "uidvalidity": self.validity[mailbox], "uid": uid}

    def reader(self, profile):
        return FakeReader(self)

    def copy(self, source, uid, target):
        new = str(self.next_uid[target])
        self.next_uid[target] += 1
        self.messages[target][new] = self.messages[source][uid]
        self.flags[(target, new)] = [f for f in self.flags[(source, uid)] if f != "\\Deleted"]
        return new


class FakeReader:
    def __init__(self, server):
        self.server = server
        self.client = self
        self.mailbox = None
        self.is_readonly = True
        self.untagged = {}

    @staticmethod
    def _quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def close(self):
        pass

    def capability(self):
        return "OK", [" ".join(self.server.capabilities).encode()]

    def folders(self):
        return {"account": "one", "folders": [
            {"name": name, "flags": flags, "selectable": True,
             "kind": next((f[1:].lower() for f in flags), None)} for name, flags in self.server.FOLDERS.items()]}

    def select(self, mailbox, readonly=None):
        if readonly is None:
            self.server.commands.append(("EXAMINE", mailbox))
            self.mailbox, self.is_readonly = mailbox, True
            return self.server.validity[mailbox], len(self.server.messages[mailbox])
        assert readonly is False
        name = utf7_decode(re.sub(r"\\(.)", r"\1", mailbox[1:-1]))
        self.server.commands.append(("SELECT_RW", name))
        self.mailbox, self.is_readonly = name, False
        self.untagged["UIDVALIDITY"] = [self.server.validity[name].encode()]
        return "OK", [b"selected"]

    def response(self, name):
        return name, self.untagged.pop(name, [None]) if name == "COPYUID" else self.untagged.get(name, [None])

    def uid(self, command, *args):
        server, box = self.server, self.mailbox
        server.commands.append((command, box, *args))
        if command == "FETCH":
            uid = str(args[0])
            data = server.messages[box].get(uid)
            if data is None:
                return "OK", []
            flags = " ".join(server.flags[(box, uid)])
            return "OK", [(f"1 (UID {uid} RFC822.SIZE {len(data)} FLAGS ({flags}) BODY[] {{{len(data)}}}".encode(), data), b")"]
        if command == "SEARCH":
            result = self._search(args)
            return ("OK", [None]) if server.empty_search_untagged and result[1] == [b""] else result
        return self._write(command, args)

    def _search(self, args):
        server, box = self.server, self.mailbox
        if args[1] == "UID":
            return "OK", [(" ".join(u for u in server.messages[box] if u == args[2])).encode()]
        assert args[1:3] == ("HEADER", "Message-ID")
        wanted = args[3][1:-1]
        return "OK", [(" ".join(u for u, d in server.messages[box].items()
                                if f"Message-ID: {wanted}\r\n".encode() in d)).encode()]

    def _write(self, command, args):
        server, box = self.server, self.mailbox
        if self.is_readonly:
            raise AssertionError("Mutation on a read-only selection")
        uid = str(args[0])
        assert re.fullmatch(r"[1-9][0-9]*", uid), "exact UID only"
        server.writes.append((command, box, uid))
        if server.refuse == command:
            return "NO", [b"refused"]
        result = "OK", [b"done"]
        if server.ignore != command:
            if command in ("MOVE", "COPY"):
                target = utf7_decode(re.sub(r"\\(.)", r"\1", args[1][1:-1]))
                new = server.copy(box, uid, target)
                code = f"{server.validity[target]} {uid} {new}".encode()
                if command == "MOVE":
                    del server.messages[box][uid]
                if not server.no_copyuid:
                    # imaplib exposes tagged and untagged COPYUID via response().
                    self.untagged["COPYUID"] = [code]
                result = "OK", [None]
            elif command == "STORE":
                assert args[1:] == ("+FLAGS.SILENT", "(\\Deleted)")
                server.flags[(box, uid)].append("\\Deleted")
            elif command == "EXPUNGE":
                if "\\Deleted" in server.flags[(box, uid)]:
                    del server.messages[box][uid]
            else:
                raise AssertionError(command)
        if server.fail_on == command:
            raise TimeoutError("lost acknowledgement")
        return result


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles"
        profiles.mkdir(mode=0o700)
        self.write(profiles / "one.json", {"id": "one", "host": "imap.example.test", "user": "owner@example.test",
                                           "entity": "synthetic", "password_env": "DO_NOT_USE"})
        self.entry = {"identity": {"email": "owner@example.test", "host": "imap.example.test", "entity": "synthetic"},
                      "move_write": True, "expunge_write": True}
        self.config = {"version": 1, "profiles_dir": str(profiles), "state_dir": str(self.root / "state"),
                       "accounts": {"one": self.entry}}
        self.config_path = self.root / "settings.json"
        self.save()
        self.make()

    def make(self, **kwargs):
        self.server = MailServer(**kwargs)
        self.service = MailboxService(config_path=self.config_path, reader_factory=self.server.reader,
                                      smtp_factory=lambda *a, **k: self.fail("never SMTP"))

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def save(self):
        self.write(self.config_path, self.config)

    def error(self, code, call):
        with self.assertRaises(DraftError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def move(self, refs, destination="trash", folder=None):
        preview = self.service.move_preview("one", refs, destination, folder)
        return preview, self.service.move_apply(preview["preview_id"], preview["preview_hash"])

    # ------------------------------------------------------------------ move
    def test_preview_is_read_only_and_apply_moves_to_trash_with_copyuid(self):
        ref = self.server.add("Drafts", "test draft", "<t1@example.test>", ["\\Draft"])
        preview = self.service.move_preview("one", [ref], "trash")
        self.assertEqual(([], "prepared", "Trash", "uid_move", True),
                         (self.server.writes, preview["status"], preview["destination"]["mailbox"],
                          preview["method"], preview["recoverable"]))
        result = self.service.move_apply(preview["preview_id"], preview["preview_hash"])
        self.assertEqual("verified", result["status"])
        self.assertEqual([("MOVE", "Drafts", ref["uid"])], self.server.writes)
        moved = result["receipt"]["messages"][0]["destination"]
        self.assertEqual(("Trash", "100"), (moved["mailbox"], moved["uid"]))
        self.assertNotIn(ref["uid"], self.server.messages["Drafts"])
        # Status only observes; applying again never repeats MOVE.
        self.assertEqual("verified", self.service.move_status(preview["preview_id"])["status"])
        self.assertEqual("verified", self.service.move_apply(preview["preview_id"], preview["preview_hash"])["status"])
        self.assertEqual(1, len(self.server.writes))

    def test_archive_and_named_folder_destinations(self):
        first = self.server.add("INBOX", "one", "<a1@example.test>")
        second = self.server.add("INBOX", "two", "<a2@example.test>")
        self.assertEqual("Archive", self.move([first], "archive")[1]["receipt"]["destination"]["mailbox"])
        self.assertEqual("Projects", self.move([second], "folder", "Projects")[1]["receipt"]["destination"]["mailbox"])
        self.error("MOVE_FOLDER_UNAVAILABLE", lambda: self.service.move_preview(
            "one", [self.server.add("INBOX", "x", "<x@example.test>")], "folder", "Missing"))
        self.error("INVALID_MOVE_DESTINATION", lambda: self.service.move_preview("one", [second], "trash", "Projects"))

    def test_copy_store_expunge_fallback_without_move(self):
        self.make(capabilities=("IMAP4rev1", "UIDPLUS"))
        ref = self.server.add("INBOX", "fallback", "<f@example.test>")
        preview, result = self.move([ref])
        self.assertEqual("uid_copy_store_deleted_uid_expunge", preview["method"])
        self.assertEqual("verified", result["status"])
        self.assertEqual(["COPY", "STORE", "EXPUNGE"], [w[0] for w in self.server.writes])

    def test_capabilities_required(self):
        self.make(capabilities=("IMAP4rev1",))
        self.error("MOVE_REQUIRES_MOVE_OR_UIDPLUS", lambda: self.service.move_preview(
            "one", [self.server.add("INBOX", "x", "<x@example.test>")], "trash"))
        self.make(capabilities=("IMAP4rev1", "UIDPLUS", "X-GM-EXT-1"))
        self.error("GMAIL_MOVE_REQUIRED", lambda: self.service.move_preview(
            "one", [self.server.add("INBOX", "x", "<x@example.test>")], "trash"))

    def test_lost_ack_is_verified_by_independent_readback_without_copyuid(self):
        self.server.no_copyuid = True
        self.server.fail_on = "MOVE"
        ref = self.server.add("INBOX", "lost ack", "<lost@example.test>")
        _, result = self.move([ref])
        self.assertEqual(("verified", "MOVE_RESPONSE_UNCERTAIN"), (result["status"], result.get("command_error_code")))
        self.assertEqual(1, len(self.server.writes))

    def test_duplicate_in_destination_is_ignored_when_known_before_move(self):
        self.server.no_copyuid = True
        self.server.add("Trash", "older copy", "<dup@example.test>")
        ref = self.server.add("INBOX", "dup", "<dup@example.test>")
        _, result = self.move([ref])
        self.assertEqual("verified", result["status"])

    def test_ignored_move_is_uncertain_and_never_replayed(self):
        self.server.ignore = "MOVE"
        self.server.no_copyuid = True
        ref = self.server.add("INBOX", "ignored", "<ign@example.test>")
        preview, result = self.move([ref])
        self.assertEqual(("uncertain", "MOVE_NOT_APPLIED"), (result["status"], result["error_code"]))
        self.assertEqual("uncertain", self.service.move_apply(preview["preview_id"], preview["preview_hash"])["status"])
        self.assertEqual(1, len(self.server.writes))

    def test_partial_effect_blocks_new_operations_until_resolved(self):
        self.make(capabilities=("IMAP4rev1", "UIDPLUS"))
        self.server.no_copyuid = True
        ref = self.server.add("INBOX", "partial", "<pa@example.test>")
        self.move([ref])  # COPY happened, source kept: a human must resolve the duplicate.
        self.error("MAILBOX_SOURCE_OPERATION_UNRESOLVED", lambda: self.service.move_preview("one", [ref], "archive"))

    def test_refused_command_is_not_applied_and_does_not_block_a_new_preview(self):
        self.server.refuse = "MOVE"
        ref = self.server.add("INBOX", "refused", "<r@example.test>")
        preview, result = self.move([ref])
        self.assertEqual(("uncertain", "MOVE_NOT_APPLIED", "MOVE_COMMAND_NOT_CONFIRMED"),
                         (result["status"], result["error_code"], result["command_error_code"]))
        self.assertEqual("uncertain", self.service.move_apply(preview["preview_id"], preview["preview_hash"])["status"])
        self.assertEqual(1, len(self.server.writes))
        self.server.refuse = None
        self.assertEqual("verified", self.move([ref], "archive")[1]["status"])

    def test_copy_without_copyuid_never_deletes_source(self):
        self.make(capabilities=("IMAP4rev1", "UIDPLUS"))
        self.server.no_copyuid = True
        ref = self.server.add("INBOX", "no copyuid", "<nc@example.test>")
        _, result = self.move([ref])
        self.assertEqual(["COPY"], [w[0] for w in self.server.writes])
        self.assertEqual(("uncertain", "MOVE_SOURCE_STILL_PRESENT", "MOVE_COPY_NOT_CONFIRMED"),
                         (result["status"], result["error_code"], result["command_error_code"]))
        self.assertIn(ref["uid"], self.server.messages["INBOX"])

    def test_uidvalidity_change_after_attempt_is_conflict_not_verified(self):
        self.server.fail_on = "MOVE"
        self.server.ignore = "MOVE"
        ref = self.server.add("INBOX", "renumbered", "<uv@example.test>")
        preview, _ = self.move([ref])
        self.server.validity["INBOX"] = "77"
        result = self.service.move_status(preview["preview_id"])
        self.assertEqual(("conflict", "UIDVALIDITY_CHANGED"), (result["status"], result["error_code"]))

    def test_delete_absent_without_attempted_expunge_is_not_verified(self):
        self.server.fail_on = "STORE"
        ref = self.server.add("Trash", "t", "<ne@example.test>")
        preview = self.service.delete_preview("one", [ref])
        def vanish(*_):
            del self.server.messages["Trash"][ref["uid"]]  # restored elsewhere by another client
            raise TimeoutError("lost acknowledgement")
        original = FakeReader._write
        FakeReader._write = lambda reader, command, args: vanish() if command == "STORE" else original(reader, command, args)
        self.addCleanup(setattr, FakeReader, "_write", original)
        result = self.service.delete_apply(preview["preview_id"], preview["preview_hash"])
        self.assertEqual(("uncertain", "DELETE_NOT_CONFIRMED"), (result["status"], result["error_code"]))

    def test_changed_source_before_apply_has_no_write(self):
        ref = self.server.add("INBOX", "changing", "<c@example.test>")
        preview = self.service.move_preview("one", [ref], "trash")
        self.server.flags[("INBOX", ref["uid"])].append("\\Seen")
        self.error("MOVE_SOURCE_CHANGED", lambda: self.service.move_apply(preview["preview_id"], preview["preview_hash"]))
        self.assertEqual([], self.server.writes)

    def test_permission_required_and_hash_checked(self):
        ref = self.server.add("INBOX", "perm", "<p@example.test>")
        preview = self.service.move_preview("one", [ref], "trash")
        self.error("PREVIEW_HASH_MISMATCH", lambda: self.service.move_apply(preview["preview_id"], "0" * 64))
        self.entry["move_write"] = False
        self.save()
        self.error("MOVE_NOT_AUTHORIZED", lambda: self.service.move_preview("one", [ref], "trash"))
        self.error("MOVE_NOT_AUTHORIZED", lambda: self.service.move_apply(preview["preview_id"], preview["preview_hash"]))
        self.assertEqual([], self.server.writes)
        accounts = {row["account"]: row for row in self.service.accounts()["accounts"]}
        self.assertEqual((False, True), (accounts["one"]["move_enabled"], accounts["one"]["delete_enabled"]))

    def test_source_must_differ_and_single_mailbox(self):
        trash = self.server.add("Trash", "in trash", "<t@example.test>")
        inbox = self.server.add("INBOX", "inbox", "<i@example.test>")
        self.error("MOVE_SOURCE_IS_DESTINATION", lambda: self.service.move_preview("one", [trash], "trash"))
        self.error("MOVE_SINGLE_SOURCE_MAILBOX_REQUIRED",
                   lambda: self.service.move_preview("one", [trash, inbox], "archive"))

    # ---------------------------------------------------------------- delete
    def test_permanent_delete_from_trash_keeps_backup_and_verifies(self):
        ref = self.server.add("Trash", "trashed", "<d@example.test>")
        original = self.server.messages["Trash"][ref["uid"]]
        preview = self.service.delete_preview("one", [ref])
        self.assertEqual((True, False, []), (preview["permanent"], preview["recoverable"], self.server.writes))
        backup = self.root / "state" / "mailbox" / preview["backup"][0]
        self.assertEqual(original, backup.read_bytes())
        self.assertEqual(0o600, backup.stat().st_mode & 0o777)
        result = self.service.delete_apply(preview["preview_id"], preview["preview_hash"])
        self.assertEqual("verified", result["status"])
        self.assertEqual([("STORE", "Trash", ref["uid"]), ("EXPUNGE", "Trash", ref["uid"])], self.server.writes)
        self.assertEqual("verified", self.service.delete_status(preview["preview_id"])["status"])

    def test_permanent_delete_draft_requires_draft_flag(self):
        draft = self.server.add("Drafts", "draft", "<dr@example.test>", ["\\Draft"])
        self.assertEqual("verified", self.service.delete_apply(*(lambda p: (p["preview_id"], p["preview_hash"]))(
            self.service.delete_preview("one", [draft])))["status"])
        not_draft = self.server.add("Drafts", "odd", "<nd@example.test>")
        self.error("DELETE_DRAFT_FLAG_REQUIRED", lambda: self.service.delete_preview("one", [not_draft]))

    def test_permanent_delete_refused_outside_trash_and_drafts(self):
        for mailbox in ("INBOX", "Archive", "Projects"):
            ref = self.server.add(mailbox, "keep", f"<k-{mailbox}@example.test>")
            self.error("DELETE_ONLY_FROM_TRASH_OR_DRAFTS", lambda: self.service.delete_preview("one", [ref]))
        self.assertEqual([], self.server.writes)

    def test_delete_needs_separate_permission(self):
        self.entry["expunge_write"] = False
        self.save()
        ref = self.server.add("Trash", "t", "<np@example.test>")
        self.error("DELETE_NOT_AUTHORIZED", lambda: self.service.delete_preview("one", [ref]))

    def test_ignored_expunge_is_uncertain(self):
        self.server.ignore = "EXPUNGE"
        ref = self.server.add("Trash", "t", "<ie@example.test>")
        preview = self.service.delete_preview("one", [ref])
        result = self.service.delete_apply(preview["preview_id"], preview["preview_hash"])
        self.assertEqual(("uncertain", "DELETE_EXPUNGE_NOT_CONFIRMED"), (result["status"], result["error_code"]))

    def test_tampered_backup_blocks_delete(self):
        ref = self.server.add("Trash", "t", "<tb@example.test>")
        preview = self.service.delete_preview("one", [ref])
        (self.root / "state" / "mailbox" / preview["backup"][0]).write_bytes(b"changed")
        self.error("DELETE_BACKUP_INTEGRITY_FAILED",
                   lambda: self.service.delete_apply(preview["preview_id"], preview["preview_hash"]))
        self.assertEqual([], self.server.writes)

    def test_operation_mismatch_rejected(self):
        ref = self.server.add("Trash", "t", "<om@example.test>")
        preview = self.service.delete_preview("one", [ref])
        self.error("PREVIEW_OPERATION_MISMATCH",
                   lambda: self.service.move_apply(preview["preview_id"], preview["preview_hash"]))


if __name__ == "__main__":
    unittest.main()
