"""Independent offline adversarial probes for reviewed draft replacement."""

import email
from email import policy
from email.message import EmailMessage
import unittest
from unittest.mock import patch

import test_edit as fixture


class EditAdversarialTests(unittest.TestCase):
    # Fixture reuse only: do not inherit/discover the main suite a second time.
    setUp = fixture.EditTests.setUp
    make_service = fixture.EditTests.make_service
    json = fixture.EditTests.json
    save_config = fixture.EditTests.save_config
    error = fixture.EditTests.error
    record = fixture.EditTests.record
    draft = fixture.EditTests.draft
    preview = fixture.EditTests.preview
    apply = fixture.EditTests.apply
    status = fixture.EditTests.status
    raw = fixture.EditTests.raw
    message = fixture.EditTests.message
    assert_no_smtp = fixture.EditTests.assert_no_smtp

    def test_plain_text_cannot_inject_parts_using_existing_mime_boundary(self):
        message = self.message("Existing body", attachment=True)
        message.set_boundary("safe-boundary")
        source = self.draft(message.as_bytes())
        injection = ("Corrected body\n--safe-boundary\nContent-Type:text/html\n\n"
                     "<script>unsafe()</script>\n--safe-boundary\n"
                     "Content-Type:text/plain\n\nRemaining text")
        self.error("DRAFT_EDIT_MIME_STRUCTURE_CHANGED", lambda: self.preview(source, [
            {"old_text": "Existing body", "new_text": injection}]))
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.store_calls, 0)
        state = self.root / "state"
        self.assertFalse(state.exists() and list(state.glob("*.json")))
        self.assert_no_smtp()

    def test_inline_image_without_filename_and_cid_survives_exactly(self):
        message = self.message("Existing body", "<p>Existing body</p><img src=\"cid:logo\">", False)
        resource = EmailMessage(policy=policy.SMTP)
        resource.set_content(b"\x00\xff image bytes\r\n", maintype="image", subtype="png")
        resource["Content-ID"] = "<logo>"
        resource["Content-Disposition"] = "inline"
        html_container = message.get_payload()[1]
        html_container.make_related()
        html_container.attach(resource)
        source = self.draft(message.as_bytes())
        preview = self.preview(source)
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        original_images = [part for part in message.walk() if part.get_content_maintype() == "image"]
        saved_images = [part for part in saved.walk() if part.get_content_maintype() == "image"]
        self.assertEqual(len(saved_images), 1)
        self.assertEqual(saved_images[0].get_payload(decode=True), original_images[0].get_payload(decode=True))
        self.assertEqual(str(saved_images[0]["Content-ID"]), "<logo>")
        self.assertEqual(saved_images[0].get_content_disposition(), "inline")
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assert_no_smtp()

    def test_inline_text_file_is_not_mistaken_for_editable_body(self):
        message = self.message("Existing body", attachment=False)
        message.add_attachment(b"Existing body\r\n", maintype="text", subtype="plain",
                               disposition="inline", filename="inline-evidence.txt")
        preview = self.preview(self.draft(message.as_bytes()))
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        file = next(part for part in saved.walk() if part.get_filename() == "inline-evidence.txt")
        self.assertEqual(file.get_payload(decode=True), b"Existing body\r\n")
        self.assertIn("Corrected body", saved.get_body(("plain",)).get_content())
        self.assert_no_smtp()

    def test_source_disappearing_after_append_is_not_claimed_as_our_cleanup(self):
        preview = self.preview()
        self.server.after_append = lambda uid: self.server.messages["Drafts"].pop("13")
        result = self.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["error_code"], "DRAFT_EDIT_SOURCE_DISAPPEARED")
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)
        self.assertFalse(self.record(preview)["retire_attempted"])
        self.assert_no_smtp()

    def test_duplicate_replacement_is_rejected_even_with_valid_appenduid(self):
        preview = self.preview()
        def duplicate(uid):
            self.server.messages["Drafts"]["999"] = self.server.messages["Drafts"][uid]
            self.server.flags["999"] = ["\\Draft"]
        self.server.after_append = duplicate
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["error_code"], "DRAFT_EDIT_REPLACEMENT_NOT_UNIQUE")
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)
        self.assertIn("13", self.server.messages["Drafts"])
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.server.append_calls, 1)

    def test_appenduid_pointing_at_original_can_never_retire_it(self):
        preview = self.preview()
        with patch.object(self.service, "_appenduid", return_value={"uidvalidity": "9", "uid": "13"}):
            result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["error_code"], "DRAFT_EDIT_REPLACEMENT_IDENTITY_MISMATCH")
        self.assertIn("13", self.server.messages["Drafts"])
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)

    def test_human_flag_change_at_writable_selection_blocks_retirement(self):
        preview = self.preview()
        self.server.before_write_select = lambda: self.server.flags["13"].append("\\Flagged")
        result = self.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["error_code"], "DRAFT_EDIT_SOURCE_FLAGS_CHANGED")
        self.assertIn("13", self.server.messages["Drafts"])
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)

    def test_replacement_losing_draft_flag_after_store_blocks_expunge(self):
        preview = self.preview()
        def remove_replacement_draft_flag():
            uid = next(uid for uid in self.server.messages["Drafts"] if uid != "13")
            self.server.flags[uid] = ["\\Seen"]
        self.server.after_store = remove_replacement_draft_flag
        result = self.apply(preview)
        self.assertNotEqual(result["status"], "verified")
        self.assertEqual(self.server.expunge_calls, 0)
        self.assertIn("13", self.server.messages["Drafts"])
        self.assertTrue((self.root / "state" / preview["original_backup"]).exists())
        self.assert_no_smtp()

    def test_send_preview_created_before_edit_cannot_send_stale_original(self):
        source = self.draft()
        send = self.service.send_preview("one", draft=source)
        edited = self.preview(source)
        self.assertEqual(self.apply(edited)["status"], "verified")
        self.error("DRAFT_SOURCE_EDIT_ALREADY_ATTEMPTED", lambda: self.service.send_apply(
            send["preview_id"], send["preview_hash"], authorization="send"))
        self.assert_no_smtp()

    def test_smtp_attempt_after_edit_preview_fences_apply_before_append(self):
        source = self.draft()
        edited = self.preview(source)
        send = self.service.send_preview("one", draft=source)
        self.server.data_timeout = True
        self.service.send_apply(send["preview_id"], send["preview_hash"], authorization="send")
        self.error("DRAFT_SOURCE_SEND_ALREADY_ATTEMPTED", lambda: self.apply(edited))
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.data_calls, 1)

    def test_second_prepared_edit_cannot_replay_source_after_first_edit(self):
        source = self.draft()
        first = self.preview(source)
        second = self.preview(source, [{"old_text": "Existing body", "new_text": "Another correction"}])
        self.assertEqual(self.apply(first)["status"], "verified")
        self.error("DRAFT_SOURCE_EDIT_ALREADY_ATTEMPTED", lambda: self.apply(second))
        self.assertEqual(self.server.append_calls, 1)
        self.assertEqual(self.server.expunge_calls, 1)

    def test_status_after_store_uncertainty_never_selects_writable(self):
        preview = self.preview()
        self.server.store_timeout_after = True
        self.assertEqual(self.apply(preview)["status"], "cleanup_pending")
        counts = (self.server.write_selections, self.server.store_calls, self.server.expunge_calls)
        for _ in range(3):
            self.assertEqual(self.status(preview)["status"], "cleanup_pending")
        self.assertEqual((self.server.write_selections, self.server.store_calls, self.server.expunge_calls), counts)
        self.assert_no_smtp()

    def test_private_backup_write_failure_prevents_all_remote_mutations(self):
        source = self.draft()
        with patch("imap_mail.edit.write_new", side_effect=OSError("synthetic private disk failure")):
            self.error("OPERATION_FAILED", lambda: self.preview(source))
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)
        self.assert_no_smtp()


if __name__ == "__main__":
    unittest.main()
