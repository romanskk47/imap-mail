"""Offline existing-draft replacement tests. No credentials or live mail."""

import concurrent.futures
import email
from email import policy
from email.message import EmailMessage
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import digest
from imap_mail.edit import EditService
import test_send as send_fixtures


class EditReader(send_fixtures.SendReader):
    """Model EXAMINE vs SELECT and narrowly scoped UIDPLUS mutations."""

    def __init__(self, server, profile):
        super().__init__(server, profile)
        self.is_readonly = True

    def select(self, mailbox, readonly=None):
        if readonly is None:
            self.is_readonly = True
            return super().select(mailbox)
        if readonly is not False or mailbox != '"Drafts"':
            raise AssertionError("Only explicit writable Drafts selection is allowed")
        self.server.commands.append("SELECT_RW")
        self.server.write_selections += 1
        self.mailbox = "Drafts"
        self.is_readonly = self.server.force_readonly
        if self.server.before_write_select:
            self.server.before_write_select()
        return ("NO" if self.server.select_refused else "OK"), [b"selected"]

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [self.server.validity[self.mailbox].encode()]
        return super().response(name)

    def capability(self):
        self.server.commands.append("CAPABILITY")
        return self.server.capability_status, [self.server.capabilities]

    def uid(self, command, *args):
        if command == "SEARCH" and args[-2:-1] == ("UID",):
            self.server.commands.append("SEARCH_UID")
            uid = str(args[-1])
            if not re.fullmatch(r"[1-9][0-9]*", uid):
                raise AssertionError("SEARCH must pin one UID")
            return "OK", [uid.encode() if uid in self.server.messages[self.mailbox] else b""]
        if command in ("STORE", "EXPUNGE"):
            if self.is_readonly or self.mailbox != "Drafts":
                raise AssertionError("Mutation requires explicit writable Drafts selection")
            uid = str(args[0])
            if not re.fullmatch(r"[1-9][0-9]*", uid):
                raise AssertionError("Never mutate UID ranges or other messages")
            self.server.commands.append(command + ":" + uid)
            if command == "STORE":
                if tuple(args[1:]) != ("+FLAGS.SILENT", "(\\Deleted)"):
                    raise AssertionError("Only the pinned old UID may acquire Deleted")
                self.server.store_calls += 1
                self.server.store_uids.append(uid)
                if self.server.store_timeout_before:
                    raise TimeoutError("synthetic private STORE response")
                self.server.flags.setdefault(uid, ["\\Draft"]).append("\\Deleted")
                if self.server.after_store:
                    self.server.after_store()
                if self.server.store_timeout_after:
                    raise TimeoutError("synthetic private STORE response")
            else:
                self.server.expunge_calls += 1
                self.server.expunge_uids.append(uid)
                if self.server.expunge_timeout_before:
                    raise TimeoutError("synthetic private EXPUNGE response")
                if "\\deleted" not in {f.casefold() for f in self.server.flags.get(uid, [])}:
                    raise AssertionError("Cannot expunge an undeleted source")
                self.server.messages["Drafts"].pop(uid, None)
                self.server.flags.pop(uid, None)
                if self.server.after_expunge:
                    self.server.after_expunge()
                if self.server.expunge_timeout_after:
                    raise TimeoutError("synthetic private EXPUNGE response")
            return "OK", [b"done"]
        result = super().uid(command, *args)
        if command == "FETCH" and ".1>" not in args[-1] and result[1]:
            status, rows = result
            uid = str(args[0])
            prefix, raw = rows[0]
            flags = self.server.flags.get(uid, ["\\Draft"] if self.server.draft_flag else [])
            flag_bytes = ("FLAGS (" + " ".join(flags) + ")").encode()
            prefix = re.sub(rb"FLAGS \([^)]*\)", lambda _: flag_bytes, prefix)
            if self.server.after_fetch:
                self.server.after_fetch(uid)
            return status, [(prefix, raw), *rows[1:]]
        return result

    def append(self, mailbox, flags, date, raw):
        if mailbox != '"Drafts"' or not flags.startswith("(") or "\\Draft" not in flags:
            raise AssertionError("Edit APPEND is confined to confirmed Drafts")
        self.server.append_calls += 1
        self.server.commands.append("APPEND")
        if self.server.timeout_before_accept:
            raise TimeoutError("synthetic private APPEND response")
        uid = str(101 + len(self.server.messages["Drafts"]))
        while uid in self.server.messages["Drafts"]:
            uid = str(int(uid) + 1)
        if self.server.corrupt_saved:
            changed = email.message_from_bytes(raw, policy=policy.default)
            changed.get_body(("plain",)).set_content("Unauthorized changed body")
            saved = changed.as_bytes(policy=policy.SMTP)
        else:
            saved = raw
        self.server.messages["Drafts"][uid] = saved
        self.server.flags[uid] = flags[1:-1].split()
        if not self.server.missing_appenduid:
            self.mapping = b"broken mapping" if self.server.invalid_appenduid else f"9 {uid}".encode()
        if self.server.after_append:
            self.server.after_append(uid)
        if self.server.timeout_after_accept:
            raise TimeoutError("synthetic private APPEND response")
        return "OK", [b"saved"]


class EditServer(send_fixtures.SendServer):
    def __init__(self):
        super().__init__()
        self.flags = {}
        self.capabilities = b"IMAP4rev1 UIDPLUS"
        self.capability_status = "OK"
        self.force_readonly = self.select_refused = False
        self.store_timeout_before = self.store_timeout_after = False
        self.expunge_timeout_before = self.expunge_timeout_after = False
        self.store_calls = self.expunge_calls = self.write_selections = 0
        self.store_uids = []
        self.expunge_uids = []
        self.after_append = self.after_store = self.after_expunge = None
        self.before_write_select = self.after_fetch = None

    def reader(self, profile):
        self.connections += 1
        return EditReader(self, profile)


class EditTests(unittest.TestCase):
    # Reuse only fixture helpers, not the SMTP suite's test methods.
    json = send_fixtures.SendTests.json
    save_config = send_fixtures.SendTests.save_config
    error = send_fixtures.SendTests.error
    record = send_fixtures.SendTests.record

    def setUp(self):
        send_fixtures.SendTests.setUp(self)
        self.server = EditServer()
        self.service = self.make_service()

    def make_service(self):
        return EditService(config_path=self.config_path, reader_factory=self.server.reader,
                           smtp_factory=self.server.smtp)

    def draft(self, raw=None, flags=None):
        result = send_fixtures.SendTests.draft(self, raw)
        self.server.flags[result["uid"]] = flags if flags is not None else ["\\Draft"]
        return result

    def preview(self, draft=None, replacements=None):
        return self.service.draft_edit_preview("one", draft or self.draft(), replacements or [
            {"old_text": "Existing body", "new_text": "Corrected body"}])

    def apply(self, preview):
        return self.service.draft_edit_apply(preview["preview_id"], preview["preview_hash"])

    def status(self, preview):
        return self.service.draft_edit_status(preview["preview_id"])

    def raw(self, preview):
        return (self.root / "state" / (preview["preview_id"] + ".eml")).read_bytes()

    def message(self, body="Existing body", html=None, attachment=True):
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "Owner <owner@example.test>"
        message["To"] = "Recipient <recipient@example.test>"
        message["Cc"] = "copy@example.test"
        message["Subject"] = "Existing reviewed draft"
        message["Message-ID"] = "<existing-draft@example.test>"
        message["In-Reply-To"] = "<earlier@example.test>"
        message["References"] = "<first@example.test> <earlier@example.test>"
        message["Date"] = "Wed, 21 Oct 2026 10:00:00 +0000"
        message.set_content(body)
        if html is not None:
            message.add_alternative(html, subtype="html")
        if attachment:
            message.add_attachment(b"Existing body\r\nexact attachment bytes", maintype="application",
                                   subtype="octet-stream", filename="evidence.bin")
        return message

    def assert_no_smtp(self):
        self.assertEqual(self.server.smtp_connections, 0)
        self.assertEqual(self.server.data_calls, 0)
        self.assertEqual(self.server.smtp_commands, [])

    def test_preview_is_readonly_full_review_and_private_recoverable_backup(self):
        draft = self.draft()
        original = self.server.messages["Drafts"]["13"]
        preview = self.preview(draft)
        self.assertEqual(preview["status"], "prepared")
        self.assertEqual(preview["operation"], "draft_edit")
        self.assertEqual(preview["mailbox_writes"], 0)
        self.assertIn("Existing body", preview["before"]["rendered_body_text"])
        self.assertIn("Corrected body", preview["rendered_body_text"])
        self.assertIn("Corrected body", preview["rendered_body_html"])
        self.assertEqual(preview["before"]["attachments"], preview["attachments"])
        backup = self.root / "state" / preview["original_backup"]
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.write_selections, 0)
        self.assert_no_smtp()

    def test_success_leaves_one_active_copy_preserving_all_other_content(self):
        message = self.message("Existing body\n\nExisting signature\n> Quoted prior message",
                               "<p>Existing body</p><b>Existing signature</b><blockquote>Quoted prior message</blockquote>")
        draft = self.draft(message.as_bytes(), ["\\Draft", "\\Seen", "\\Flagged", "project_1"])
        preview = self.preview(draft)
        result = self.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["mailbox_writes"], 3)
        self.assertNotIn("13", self.server.messages["Drafts"])
        uid = result["receipt"]["uid"]
        self.assertEqual(list(self.server.messages["Drafts"]), [uid])
        saved = email.message_from_bytes(self.server.messages["Drafts"][uid], policy=policy.default)
        for name in ("From", "To", "Cc", "Subject", "In-Reply-To", "References", "Date"):
            self.assertEqual(str(saved[name]), str(message[name]))
        self.assertNotEqual(str(saved["Message-ID"]), str(message["Message-ID"]))
        self.assertIn("Existing signature", saved.get_body(("plain",)).get_content())
        self.assertIn("Quoted prior message", saved.get_body(("html",)).get_content())
        self.assertEqual(list(saved.iter_attachments())[0].get_payload(decode=True),
                         list(message.iter_attachments())[0].get_payload(decode=True))
        self.assertEqual(set(self.server.flags[uid]), {"\\Draft", "\\Seen", "\\Flagged", "project_1"})
        self.assertEqual(self.server.store_uids, ["13"])
        self.assertEqual(self.server.expunge_uids, ["13"])
        self.assertGreaterEqual(self.server.write_selections, 1)
        self.assert_no_smtp()

    def test_success_does_not_expunge_unrelated_already_deleted_messages(self):
        draft = self.draft()
        self.server.messages["Drafts"]["88"] = self.message("Unrelated draft").as_bytes()
        self.server.flags["88"] = ["\\Draft", "\\Deleted"]
        preview = self.preview(draft)
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertIn("88", self.server.messages["Drafts"])
        self.assertEqual(self.server.expunge_uids, ["13"])

    def test_verified_apply_and_status_never_append_or_mutate_again(self):
        preview = self.preview()
        self.assertEqual(self.apply(preview)["status"], "verified")
        counts = self.server.append_calls, self.server.store_calls, self.server.expunge_calls
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.status(preview)["status"], "verified")
        self.assertEqual((self.server.append_calls, self.server.store_calls, self.server.expunge_calls), counts)
        self.assert_no_smtp()

    def test_uidplus_missing_or_failed_capability_blocks_before_any_write(self):
        for capabilities, status, code in ((b"IMAP4rev1", "OK", "DRAFT_EDIT_REQUIRES_UIDPLUS"),
                                            (b"UIDPLUS", "NO", "DRAFT_EDIT_CAPABILITY_CHECK_FAILED")):
            with self.subTest(capabilities=capabilities, status=status):
                self.server.capabilities, self.server.capability_status = capabilities, status
                self.error(code, self.preview)
        self.assertEqual(self.server.append_calls, 0)
        self.assert_no_smtp()

    def test_capability_lost_between_preview_apply_blocks_before_append(self):
        preview = self.preview()
        self.server.capabilities = b"IMAP4rev1"
        self.error("DRAFT_EDIT_REQUIRES_UIDPLUS", lambda: self.apply(preview))
        self.assertFalse(self.record(preview)["append_attempted"])
        self.assertEqual(self.server.append_calls, 0)

    def test_append_timeout_before_accept_never_replays_or_retires_original(self):
        preview = self.preview()
        self.server.timeout_before_accept = True
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.server.timeout_before_accept = False
        self.assertEqual(self.status(preview)["status"], "uncertain")
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.server.append_calls, 1)
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(list(self.server.messages["Drafts"]), ["13"])

    def test_lost_append_reply_after_accept_is_reconciled_once_without_duplicate(self):
        preview = self.preview()
        self.server.timeout_after_accept = True
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)
        self.assertEqual(len(self.server.messages["Drafts"]), 1)
        self.assert_no_smtp()

    def test_append_without_appenduid_can_be_reconciled_by_unique_message_id(self):
        preview = self.preview()
        self.server.missing_appenduid = True
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(len(self.server.messages["Drafts"]), 1)

    def test_invalid_appenduid_never_retires_original_or_reappends(self):
        preview = self.preview()
        self.server.invalid_appenduid = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["error_code"], "APPENDUID_INVALID")
        self.assertEqual(self.server.store_calls, 0)
        self.assertIn("13", self.server.messages["Drafts"])
        self.apply(preview)
        self.assertEqual(self.server.append_calls, 1)

    def test_status_observes_cleanup_pending_but_never_retires_source(self):
        preview = self.preview()
        self.server.store_timeout_before = True
        self.assertEqual(self.apply(preview)["status"], "cleanup_pending")
        counts = self.server.append_calls, self.server.store_calls, self.server.expunge_calls
        self.server.store_timeout_before = False
        self.assertEqual(self.status(preview)["status"], "cleanup_pending")
        self.assertEqual((self.server.append_calls, self.server.store_calls, self.server.expunge_calls), counts)
        self.assertIn("13", self.server.messages["Drafts"])
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)

    def test_store_reply_lost_after_accept_resumes_targeted_expunge_only(self):
        preview = self.preview()
        self.server.store_timeout_after = True
        self.assertEqual(self.apply(preview)["status"], "cleanup_pending")
        self.assertEqual(self.server.flags["13"], ["\\Draft", "\\Deleted"])
        self.assertEqual(self.server.expunge_calls, 0)
        self.server.store_timeout_after = False
        self.assertEqual(self.status(preview)["status"], "cleanup_pending")
        self.assertEqual(self.server.expunge_calls, 0)
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.store_calls, 1)
        self.assertEqual(self.server.append_calls, 1)

    def test_expunge_reply_lost_before_accept_is_recoverable_only_by_apply(self):
        preview = self.preview()
        self.server.expunge_timeout_before = True
        self.assertEqual(self.apply(preview)["status"], "cleanup_pending")
        self.server.expunge_timeout_before = False
        self.assertEqual(self.status(preview)["status"], "cleanup_pending")
        self.assertEqual(self.server.expunge_calls, 1)
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)
        self.assertEqual(self.server.store_calls, 1)
        self.assertEqual(self.server.expunge_calls, 2)

    def test_expunge_reply_lost_after_accept_is_verified_without_replaying(self):
        preview = self.preview()
        self.server.expunge_timeout_after = True
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.status(preview)["status"], "verified")
        self.assertEqual(self.server.expunge_calls, 1)

    def test_source_body_changed_after_review_blocks_before_append(self):
        preview = self.preview()
        self.server.messages["Drafts"]["13"] = self.message("Human edited body").as_bytes()
        self.error("DRAFT_EDIT_SOURCE_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_source_flags_changed_after_review_block_before_append(self):
        preview = self.preview()
        self.server.flags["13"].append("\\Seen")
        self.error("DRAFT_EDIT_SOURCE_FLAGS_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_source_missing_after_review_blocks_before_append(self):
        preview = self.preview()
        self.server.messages["Drafts"].pop("13")
        self.error("DRAFT_EDIT_SOURCE_DISAPPEARED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_source_changed_after_replacement_upload_is_preserved(self):
        preview = self.preview()
        changed = self.message("Human edited body after upload").as_bytes()
        self.server.after_append = lambda uid: self.server.messages["Drafts"].__setitem__("13", changed)
        result = self.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["error_code"], "DRAFT_EDIT_SOURCE_CHANGED")
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.messages["Drafts"]["13"], changed)

    def test_replacement_changed_before_retirement_preserves_original(self):
        preview = self.preview()
        self.server.corrupt_saved = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["error_code"], "DRAFT_EDIT_REPLACEMENT_CHANGED")
        self.assertEqual(self.server.store_calls, 0)
        self.assertIn("13", self.server.messages["Drafts"])

    def test_replacement_changed_after_mark_deleted_is_not_expunged(self):
        preview = self.preview()
        def change_replacement():
            uid = next(uid for uid in self.server.messages["Drafts"] if uid != "13")
            changed = email.message_from_bytes(self.server.messages["Drafts"][uid], policy=policy.default)
            changed.get_body(("plain",)).set_content("Human changed replacement")
            self.server.messages["Drafts"][uid] = changed.as_bytes(policy=policy.SMTP)
        self.server.after_store = change_replacement
        result = self.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.server.expunge_calls, 0)
        self.assertIn("13", self.server.messages["Drafts"])
        self.assertTrue((self.root / "state" / preview["original_backup"]).exists())

    def test_writable_selection_refused_never_marks_or_expunge_source(self):
        preview = self.preview()
        self.server.select_refused = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "cleanup_pending")
        self.assertEqual(result["cleanup_error_code"], "DRAFT_EDIT_WRITE_SELECTION_FAILED")
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)
        self.assertIn("13", self.server.messages["Drafts"])

    def test_hash_config_profile_and_backup_integrity_fences(self):
        preview = self.preview()
        self.error("PREVIEW_HASH_MISMATCH", lambda: self.service.draft_edit_apply(preview["preview_id"], "0" * 64))
        self.entry["default_from"] = "alias@example.test"
        self.save_config()
        self.error("ACCOUNT_CONFIGURATION_CHANGED", lambda: self.apply(preview))
        self.entry["default_from"] = "owner@example.test"
        self.save_config()
        self.profile["entity"] = "different-company"
        self.json(self.profiles / "one.json", self.profile)
        self.error("ACCOUNT_IDENTITY_MISMATCH", lambda: self.apply(preview))
        self.profile["entity"] = "synthetic"
        self.json(self.profiles / "one.json", self.profile)
        (self.root / "state" / preview["original_backup"]).write_bytes(b"tampered backup")
        self.error("DRAFT_EDIT_BACKUP_INTEGRITY_FAILED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_operation_fences_prevent_using_edit_as_create_or_send(self):
        preview = self.preview()
        for action in (lambda: self.service.apply(preview["preview_id"], preview["preview_hash"]),
                       lambda: self.service.status(preview["preview_id"])):
            self.error("PREVIEW_OPERATION_MISMATCH", action)
        for action in (lambda: self.service.send_apply(preview["preview_id"], preview["preview_hash"], authorization="send"),
                       lambda: self.service.send_status(preview["preview_id"])):
            self.error("SEND_PREVIEW_REQUIRED", action)
        created = self.service.preview("one", "Separate new draft", to=["recipient@example.test"], subject="New draft")
        self.error("PREVIEW_OPERATION_MISMATCH", lambda: self.apply(created))
        self.error("PREVIEW_OPERATION_MISMATCH", lambda: self.status(created))
        self.assertEqual(self.server.append_calls, 0)
        self.assert_no_smtp()

    def test_cross_account_reference_and_non_draft_folder_are_rejected(self):
        source = self.draft()
        self.error("DRAFT_ACCOUNT_MISMATCH", lambda: self.service.draft_edit_preview("one", {**source, "account": "other"}, [
            {"old_text": "Existing body", "new_text": "Corrected body"}]))
        self.error("DRAFTS_FOLDER_CHANGED", lambda: self.preview({**source, "mailbox": "INBOX"}))
        self.assertEqual(self.server.append_calls, 0)

    def test_second_preview_after_uncertain_append_cannot_duplicate_same_source(self):
        source = self.draft()
        preview = self.preview(source)
        self.server.timeout_before_accept = True
        self.apply(preview)
        self.error("DRAFT_SOURCE_EDIT_ALREADY_ATTEMPTED", lambda: self.preview(source))
        self.assertEqual(self.server.append_calls, 1)

    def test_staged_replacement_cannot_be_sent_before_edit_cleanup(self):
        preview = self.preview()
        self.server.store_timeout_before = True
        result = self.apply(preview)
        source = {key: result["receipt"][key] for key in ("account", "mailbox", "uidvalidity", "uid")}
        self.error("DRAFT_SOURCE_EDIT_ALREADY_ATTEMPTED", lambda: self.service.send_preview("one", draft=source))
        self.assert_no_smtp()

    def test_source_with_attempted_smtp_send_cannot_be_edited(self):
        source = self.draft()
        sent = self.service.send_preview("one", draft=source)
        self.server.data_timeout = True
        self.service.send_apply(sent["preview_id"], sent["preview_hash"], authorization="send")
        self.error("DRAFT_SOURCE_SEND_ALREADY_ATTEMPTED", lambda: self.preview(source))
        self.assertEqual(self.server.data_calls, 1)
        self.assertEqual(self.server.append_calls, 0)

    def test_replacements_are_simultaneous_and_not_recursive(self):
        source = self.draft(self.message("Existing body and Second sentence", attachment=False).as_bytes())
        preview = self.preview(source, [{"old_text": "Existing body", "new_text": "Second sentence"},
                                        {"old_text": "Second sentence", "new_text": "Third sentence"}])
        self.assertIn("Second sentence and Third sentence", preview["rendered_body_text"])

    def test_ambiguous_missing_overlapping_and_unchanged_text_are_rejected(self):
        cases = [("Existing body Existing body", [{"old_text": "Existing body", "new_text": "Corrected body"}], "DRAFT_EDIT_TEXT_NOT_UNIQUE"),
                 ("Existing body", [{"old_text": "Not present", "new_text": "Corrected body"}], "DRAFT_EDIT_TEXT_NOT_UNIQUE"),
                 ("Existing body", [{"old_text": "Existing body", "new_text": "Corrected body"}, {"old_text": "body", "new_text": "sentence"}], "DRAFT_EDIT_OVERLAPPING_REPLACEMENTS"),
                 ("Existing body", [{"old_text": "Existing body", "new_text": "Existing body"}], "DRAFT_EDIT_NO_CHANGE")]
        for body, replacements, code in cases:
            with self.subTest(code=code):
                source = self.draft(self.message(body, attachment=False).as_bytes())
                self.error(code, lambda: self.preview(source, replacements))
        self.assertEqual(self.server.append_calls, 0)

    def test_html_text_replacement_escapes_new_markup_and_handles_entities(self):
        source = self.draft(self.message("Existing body & signature", "<p>Existing body &amp; signature</p>", False).as_bytes())
        preview = self.preview(source, [{"old_text": "Existing body & signature", "new_text": "<script>unsafe()</script> & safe"}])
        self.assertIn("&lt;script&gt;unsafe()&lt;/script&gt; &amp; safe", preview["rendered_body_html"])
        self.assertNotIn("<script>", preview["rendered_body_html"])
        self.assertIn("<script>unsafe()</script> & safe", preview["rendered_body_text"])

    def test_html_attributes_scripts_or_split_nodes_are_not_text_edit_targets(self):
        for html in ('<p title="Existing body">Other text</p>',
                     '<script>Existing body</script><p>Other text</p>',
                     '<p>Existing <b>body</b></p>'):
            with self.subTest(html=html):
                source = self.draft(self.message("Existing body", html, False).as_bytes())
                self.error("DRAFT_EDIT_HTML_TEXT_NOT_UNIQUE_OR_SPLIT", lambda: self.preview(source))
        self.assertEqual(self.server.append_calls, 0)

    def test_every_nonempty_body_alternative_must_contain_reviewed_text(self):
        source = self.draft(self.message("Existing body", "<p>Different HTML content</p>", False).as_bytes())
        self.error("DRAFT_EDIT_HTML_TEXT_NOT_UNIQUE_OR_SPLIT", lambda: self.preview(source))
        self.assertEqual(self.server.append_calls, 0)

    def test_empty_plain_alternative_does_not_block_real_html_body(self):
        source = self.draft(self.message("", "<p>Existing body</p><b>Existing signature</b>", False).as_bytes())
        preview = self.preview(source)
        message = email.message_from_bytes(self.raw(preview), policy=policy.default)
        self.assertEqual(message.get_body(("plain",)).get_content().strip(), "")
        self.assertIn("Corrected body", message.get_body(("html",)).get_content())
        self.assertEqual(self.apply(preview)["status"], "verified")

    def test_text_attachment_matching_old_text_is_preserved_byte_for_byte(self):
        message = self.message("Existing body", attachment=False)
        data = "Existing body\r\nNiezmieniony załącznik".encode()
        message.add_attachment(data, maintype="text", subtype="plain", filename="evidence.txt")
        preview = self.preview(self.draft(message.as_bytes()))
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        self.assertEqual(list(saved.iter_attachments())[0].get_payload(decode=True), data)
        self.assertEqual(preview["attachments"][0]["sha256"], digest(data))

    def test_edit_does_not_require_or_enable_smtp_opt_in(self):
        self.entry["send_enabled"] = False
        self.entry.pop("smtp")
        self.save_config()
        preview = self.preview()
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertFalse(json.loads(self.config_path.read_text())["accounts"]["one"]["send_enabled"])
        self.assert_no_smtp()

    def test_concurrent_apply_serializes_to_one_append_and_one_retirement(self):
        preview = self.preview()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.apply(preview), range(2)))
        self.assertEqual([r["status"] for r in results], ["verified", "verified"])
        self.assertEqual(self.server.append_calls, 1)
        self.assertEqual(self.server.store_calls, 1)
        self.assertEqual(self.server.expunge_calls, 1)
        self.assertEqual(len(self.server.messages["Drafts"]), 1)
        self.assert_no_smtp()

    def test_write_selection_that_remains_readonly_is_not_used_for_retirement(self):
        preview = self.preview()
        self.server.force_readonly = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "cleanup_pending")
        self.assertEqual(result["cleanup_error_code"], "DRAFT_EDIT_WRITE_SELECTION_FAILED")
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)

    def test_source_changed_during_readwrite_selection_is_not_retired(self):
        preview = self.preview()
        changed = self.message("Human change during writable selection").as_bytes()
        self.server.before_write_select = lambda: self.server.messages["Drafts"].__setitem__("13", changed)
        result = self.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.messages["Drafts"]["13"], changed)

    def test_configuration_changed_after_upload_never_retires_source(self):
        preview = self.preview()
        def change_config(uid):
            self.entry["default_from"] = "alias@example.test"
            self.save_config()
        self.server.after_append = change_config
        result = self.apply(preview)
        self.assertNotEqual(result["status"], "verified")
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)
        self.assertIn("13", self.server.messages["Drafts"])

    def test_uidvalidity_changed_after_review_never_appends_to_new_mailbox_generation(self):
        preview = self.preview()
        self.server.validity["Drafts"] = "10"
        self.error("DRAFTS_FOLDER_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_only_active_unsent_draft_flags_are_allowed_for_preview(self):
        for flags in ([], ["\\Seen"], ["\\Draft", "\\Deleted"]):
            with self.subTest(flags=flags):
                source = self.draft(flags=flags)
                self.error("DRAFT_FLAG_NOT_CONFIRMED", lambda: self.preview(source))
        self.assertEqual(self.server.append_calls, 0)

    def test_flag_command_injection_is_rejected_before_preview_is_saved(self):
        for flag in ("\\*", 'quote"', "unsafe;flag", "name[bracket"):
            with self.subTest(flag=flag):
                source = self.draft(flags=["\\Draft", flag])
                self.error("UNSUPPORTED_DRAFT_FLAGS", lambda: self.preview(source))
        self.assertEqual(self.server.append_calls, 0)

    def test_server_only_recent_flag_is_not_copied_to_replacement(self):
        source = self.draft(flags=["\\Draft", "\\Seen", "\\Recent"])
        preview = self.preview(source)
        result = self.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.server.flags[result["receipt"]["uid"]], ["\\Draft", "\\Seen"])

    def test_inline_cid_image_payload_and_metadata_are_preserved(self):
        message = self.message("Existing body\nExisting signature", "<p>Existing body</p><img src=\"cid:signature-logo\">", False)
        html_part = message.get_body(("html",))
        html_part.add_related(b"exact synthetic image bytes", maintype="image", subtype="png", cid="<signature-logo>",
                              disposition="inline", filename="logo.png")
        preview = self.preview(self.draft(message.as_bytes()))
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        before_image = next(p for p in message.walk() if p.get_content_type() == "image/png")
        image = next(p for p in saved.walk() if p.get_content_type() == "image/png")
        self.assertEqual(image.get_payload(decode=True), before_image.get_payload(decode=True))
        self.assertEqual(str(image["Content-ID"]), "<signature-logo>")
        self.assertEqual(image.get_content_disposition(), "inline")
        self.assertEqual(image.get_filename(), "logo.png")
        self.assertIn('src="cid:signature-logo"', saved.get_body(("html",)).get_content())
        self.assertEqual(self.apply(preview)["status"], "verified")

    def test_preview_mime_tampering_blocks_before_any_write(self):
        preview = self.preview()
        path = self.root / "state" / (preview["preview_id"] + ".eml")
        path.write_bytes(path.read_bytes() + b"tampered MIME")
        self.error("PREVIEW_INTEGRITY_FAILED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_new_edit_of_verified_replacement_remains_possible_and_unsent(self):
        first = self.preview()
        result = self.apply(first)
        source = {key: result["receipt"][key] for key in ("account", "mailbox", "uidvalidity", "uid")}
        second = self.preview(source, [{"old_text": "Corrected body", "new_text": "Second correction"}])
        self.assertEqual(self.apply(second)["status"], "verified")
        self.assertEqual(len(self.server.messages["Drafts"]), 1)
        self.assertEqual(self.server.append_calls, 2)
        self.assert_no_smtp()

    def test_overlapping_occurrences_of_one_search_string_are_ambiguous(self):
        source = self.draft(self.message("aaaa", attachment=False).as_bytes())
        self.error("DRAFT_EDIT_TEXT_NOT_UNIQUE", lambda: self.preview(source, [{"old_text": "aaa", "new_text": "b"}]))
        self.assertEqual(self.server.append_calls, 0)


if __name__ == "__main__":
    unittest.main()
