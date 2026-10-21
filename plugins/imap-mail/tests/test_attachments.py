"""Offline draft attachment repair; no real accounts, credentials, or SMTP."""

import email
from email import policy
from email.message import EmailMessage
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from imap_mail.attachments import AttachmentService
from imap_mail.core import DraftError, digest
import test_edit as edit_fixtures
import test_send as send_fixtures


class AttachmentTests(unittest.TestCase):
    json = send_fixtures.SendTests.json
    save_config = send_fixtures.SendTests.save_config
    record = send_fixtures.SendTests.record
    draft = edit_fixtures.EditTests.draft
    apply = edit_fixtures.EditTests.apply
    status = edit_fixtures.EditTests.status
    raw = edit_fixtures.EditTests.raw
    assert_no_smtp = edit_fixtures.EditTests.assert_no_smtp
    message = edit_fixtures.EditTests.message

    def setUp(self):
        send_fixtures.SendTests.setUp(self)
        self.server = edit_fixtures.EditServer()
        self.service = self.make_service()

    def make_service(self):
        return AttachmentService(config_path=self.config_path, reader_factory=self.server.reader,
                                 smtp_factory=self.server.smtp)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(DraftError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def file(self, name="scan.pdf", payload=b"%PDF synthetic missing scan\r\n"):
        path = self.allowed / name
        path.write_bytes(payload)
        return str(path)

    def nested(self, extra_html="", inline_documents=False, apple_headers=True):
        """Model alternative -> related -> HTML + PDF/XLSX + signature image."""
        message = self.message(attachment=False)
        message.clear_content()
        if apple_headers:
            message["X-Apple-Notify-Thread"] = "NO"
            message["X-Apple-Message-Smime-Encrypt"] = "NO"
        message.make_alternative()
        related = EmailMessage(policy=policy.SMTP)
        related.set_content('<html><body><p>Existing body and corrected wording.</p>'
                            '<b>Existing signature</b><img src="cid:signature-image">'
                            '<blockquote>Quoted prior message</blockquote>' + extra_html +
                            '<object type="application/x-apple-msg-attachment" width="96" data="cid:missing-scan"></object>'
                            '<object type="application/x-apple-msg-attachment" width="102" data="cid:pdf-cid"></object>'
                            '<object type="application/x-apple-msg-attachment" width="102" data="cid:xlsx-cid"></object>'
                            '</body></html>', subtype="html")
        related.make_related()
        for name, content_type, cid, data in (
                ("analysis.pdf", "application/pdf", "pdf-cid", b"%PDF exact original\r\n"),
                ("analysis.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx-cid", b"PK\x03\x04 exact sheet\r\n")):
            maintype, subtype = content_type.split("/", 1)
            document = EmailMessage(policy=policy.SMTP)
            document.set_content(data, maintype=maintype, subtype=subtype)
            document.add_header("Content-Disposition", "inline" if inline_documents else "attachment", filename=name)
            document["Content-ID"] = "<" + cid + ">"
            related.attach(document)
        image = EmailMessage(policy=policy.SMTP)
        image.set_content(b"\x89PNG exact image bytes\r\n", maintype="image", subtype="png")
        image.add_header("Content-Disposition", "inline", filename="signature.png")
        image["Content-ID"] = "<signature-image>"
        related.attach(image)
        message.attach(related)
        return message

    def preview(self, draft=None, attachments=None, normalize_attachments=True):
        return self.service.draft_attachment_preview("one", draft or self.draft(self.nested().as_bytes()),
            [self.file()] if attachments is None else attachments, normalize_attachments)

    def test_nested_docs_and_missing_file_become_three_top_level_attachments(self):
        draft = self.draft(self.nested().as_bytes())
        original = self.server.messages["Drafts"]["13"]
        preview = self.preview(draft)
        self.assertEqual(preview["status"], "prepared")
        self.assertEqual(preview["operation"], "draft_edit")
        self.assertEqual(preview["edit_kind"], "attachments")
        self.assertEqual(len(preview["top_level_attachments"]), 3)
        self.assertEqual([item["filename"] for item in preview["top_level_attachments"]],
                         ["analysis.pdf", "analysis.xlsx", "scan.pdf"])
        self.assertTrue(all(item["disposition"] == "attachment" and not item["cid"]
                            for item in preview["top_level_attachments"]))
        self.assertEqual(len(preview["removed_attachment_placeholders"]), 3)
        self.assertEqual(sum(item["orphan"] for item in preview["removed_attachment_placeholders"]), 1)
        self.assertEqual(preview["mailbox_writes"], 0)
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.write_selections, 0)
        backup = self.root / "state" / preview["original_backup"]
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        self.assertEqual(saved.get_content_type(), "multipart/mixed")
        html = saved.get_body(("html",)).get_content()
        self.assertNotIn("x-apple-msg-attachment", html)
        self.assertIn('src="cid:signature-image"', html)
        self.assertIn("Existing body and corrected wording.", html)
        self.assertIn("Existing signature", html)
        self.assertIn("Quoted prior message", html)
        image = [part for part in saved.walk() if part.get_content_maintype() == "image"][0]
        self.assertEqual(image.get_payload(decode=True), b"\x89PNG exact image bytes\r\n")
        self.assertEqual(image["Content-ID"], "<signature-image>")
        self.assertEqual(image.get_content_disposition(), "inline")
        before = email.message_from_bytes(original, policy=policy.default)
        for name in ("From", "To", "Cc", "Subject", "Date", "References", "In-Reply-To",
                     "X-Apple-Notify-Thread", "X-Apple-Message-Smime-Encrypt"):
            self.assertEqual(str(before[name]), str(saved[name]))
        self.assertNotEqual(str(before["Message-ID"]), str(saved["Message-ID"]))
        before_files = {part.get_filename(): part.get_payload(decode=True) for part in before.walk() if part.get_filename()}
        after_files = {part.get_filename(): part.get_payload(decode=True) for part in saved.walk() if part.get_filename()}
        for name, data in before_files.items():
            self.assertEqual(after_files[name], data)
        self.assertEqual(after_files["scan.pdf"], Path(self.file()).read_bytes())
        self.assert_no_smtp()

    def test_apply_retires_only_one_old_draft_and_never_replays(self):
        draft = self.draft(self.nested().as_bytes(), ["\\Draft", "\\Seen", "\\Flagged"])
        self.server.messages["Drafts"]["88"] = self.message("Unrelated draft").as_bytes()
        self.server.flags["88"] = ["\\Draft", "\\Deleted"]
        preview = self.preview(draft)
        result = self.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("13", self.server.messages["Drafts"])
        self.assertIn("88", self.server.messages["Drafts"])
        self.assertEqual(self.server.store_uids, ["13"])
        self.assertEqual(self.server.expunge_uids, ["13"])
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.status(preview)["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)
        self.assert_no_smtp()

    def test_existing_inline_documents_are_supported_only_by_repair_review(self):
        message = self.nested(inline_documents=True)
        preview = self.preview(self.draft(message.as_bytes()))
        documents_before = [part for part in preview["before"]["attachments"] if part["filename"] != "signature.png"]
        self.assertTrue(all(part["disposition"] == "inline" for part in documents_before))
        self.assertTrue(all(part["disposition"] == "attachment" for part in preview["top_level_attachments"]))
        self.error("UNSUPPORTED_DRAFT_HEADERS", self.service._existing_mime, message.as_bytes(), self.entry)

    def test_ordinary_new_attachment_does_not_require_normalization(self):
        message = self.message(attachment=True)
        draft = self.draft(message.as_bytes())
        preview = self.preview(draft, normalize_attachments=False)
        self.assertEqual(len(preview["attachments"]), 2)
        self.assertEqual(preview["promoted_attachments"], [])
        self.assertEqual(preview["removed_attachment_placeholders"], [])
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assert_no_smtp()

    def test_new_image_attachment_is_not_dropped_by_invariant_check(self):
        preview = self.preview(self.draft(self.message(attachment=False).as_bytes()),
                               [self.file("extra.png", b"\x89PNG new attachment")])
        self.assertEqual(preview["top_level_attachments"][0]["filename"], "extra.png")

    def test_noop_rejected(self):
        self.error("DRAFT_ATTACHMENT_NO_CHANGE", self.preview,
                   self.draft(self.message(attachment=True).as_bytes()), [])

    def test_duplicate_new_filename_or_payload_rejected(self):
        for name, payload in (("analysis.pdf", b"different bytes"), ("other.pdf", b"%PDF exact original\r\n")):
            with self.subTest(name=name):
                self.error("DRAFT_ATTACHMENT_DUPLICATE_FILE", self.preview, attachments=[self.file(name, payload)])
        self.assertEqual(self.server.append_calls, 0)

    def test_container_attachments_rejected(self):
        message = self.message(attachment=False)
        message.add_alternative("<p>Existing body</p>", subtype="html")
        message.add_header("Content-Disposition", "attachment", filename="container.eml")
        self.error("DRAFT_ATTACHMENT_CONTAINER_UNSUPPORTED", self.preview, self.draft(message.as_bytes()))

    def test_source_config_tamper_and_uid_fences_inherited(self):
        preview = self.preview()
        self.entry["default_from"] = "alias@example.test"
        self.save_config()
        self.error("ACCOUNT_CONFIGURATION_CHANGED", self.apply, preview)
        self.assertEqual(self.server.append_calls, 0)

    def test_source_content_change_blocks_before_any_write(self):
        preview = self.preview()
        self.server.messages["Drafts"]["13"] = self.message("Human replacement").as_bytes()
        self.error("DRAFT_EDIT_SOURCE_CHANGED", self.apply, preview)
        self.assertEqual(self.server.append_calls, 0)

    def test_tampered_mime_blocks_before_any_write(self):
        preview = self.preview()
        path = self.root / "state" / (preview["preview_id"] + ".eml")
        path.write_bytes(path.read_bytes() + b"unreviewed")
        self.error("PREVIEW_INTEGRITY_FAILED", self.apply, preview)
        self.assertEqual(self.server.append_calls, 0)

    def test_lost_append_reply_reconciles_without_duplicate_or_smtp(self):
        preview = self.preview()
        self.server.timeout_after_accept = True
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)
        self.assert_no_smtp()

    def test_uidplus_required_before_preview_or_writes(self):
        self.server.capabilities = b"IMAP4rev1"
        self.error("DRAFT_EDIT_REQUIRES_UIDPLUS", self.preview)
        self.assertEqual(self.server.append_calls, 0)

    def test_extra_apple_headers_never_allow_smime_encryption_yes(self):
        message = self.nested()
        message.replace_header("X-Apple-Message-Smime-Encrypt", "YES")
        self.error("UNSUPPORTED_DRAFT_HEADERS", self.preview, self.draft(message.as_bytes()))

    def test_related_root_cannot_be_promoted_or_left_dangling(self):
        for cid in ("pdf-cid", "missing-related-root"):
            with self.subTest(cid=cid):
                message = self.nested()
                related = message.get_payload()[0]
                related.set_param("start", "<" + cid + ">")
                self.error("DRAFT_ATTACHMENT_RELATED_ROOT_UNSUPPORTED", self.preview, self.draft(message.as_bytes()))
        self.assertEqual(self.server.append_calls, 0)

    def test_related_html_root_cid_is_preserved(self):
        message = self.nested()
        related = message.get_payload()[0]
        related.get_payload()[0]["Content-ID"] = "<html-root>"
        related.set_param("start", "<html-root>")
        preview = self.preview(self.draft(message.as_bytes()))
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        related_saved = next(part for part in saved.walk() if part.get_content_type() == "multipart/related")
        self.assertEqual(related_saved.get_param("start"), "<html-root>")
        self.assertEqual(str(saved.get_body(("html",))["Content-ID"]), "<html-root>")

    def test_wrong_account_or_invalid_normalize_flag_rejected(self):
        draft = self.draft(self.nested().as_bytes())
        self.error("DRAFT_ACCOUNT_MISMATCH", self.preview, {**draft, "account": "another"})
        self.error("INVALID_ATTACHMENT_NORMALIZATION", self.preview, draft, [], "yes")


if __name__ == "__main__":
    unittest.main()
