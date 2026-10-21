"""Independent offline checks for ordinary-attachment repair invariants."""

import email
from email import policy
from email.message import EmailMessage
import unittest

import test_edit as fixture
from imap_mail.attachments import AttachmentService, _normalize_html
from imap_mail.core import DraftError, digest


class AttachmentAdversarialTests(unittest.TestCase):
    setUp = fixture.EditTests.setUp
    json = fixture.EditTests.json
    save_config = fixture.EditTests.save_config
    error = fixture.EditTests.error
    record = fixture.EditTests.record
    draft = fixture.EditTests.draft
    apply = fixture.EditTests.apply
    status = fixture.EditTests.status
    raw = fixture.EditTests.raw
    assert_no_smtp = fixture.EditTests.assert_no_smtp

    def make_service(self):
        return AttachmentService(config_path=self.config_path,
                                 reader_factory=self.server.reader,
                                 smtp_factory=self.server.smtp)

    def message(self, fragment=None):
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "Owner <owner@example.test>"
        message["To"] = "Recipient <recipient@example.test>"
        message["Cc"] = "copy@example.test"
        message["Subject"] = "Attachment repair"
        message["Message-ID"] = "<attachment-draft@example.test>"
        message["In-Reply-To"] = "<source@example.test>"
        message["References"] = "<first@example.test> <source@example.test>"
        message["Date"] = "Wed, 21 Oct 2026 10:00:00 +0000"
        message.set_content("Authored text\nExisting signature\nQuoted prior message")
        placeholder = '<object type="application/x-apple-msg-attachment" data="cid:document"></object>'
        message.add_alternative('<p>Authored text</p><b>Existing signature</b>'
                                '<img src="cid:logo"><blockquote>Quoted prior message</blockquote>' +
                                (placeholder if fragment is None else fragment), subtype="html")
        related = message.get_payload()[1]
        related.make_related()
        document = EmailMessage(policy=policy.SMTP)
        document.set_content(b"\x00 exact document\r\n\xff", maintype="application", subtype="pdf")
        document.add_header("Content-Disposition", "attachment", filename="ledger.pdf")
        document["Content-ID"] = "<document>"
        related.attach(document)
        logo = EmailMessage(policy=policy.SMTP)
        logo.set_content(b"\x00 exact logo\r\n\xff", maintype="image", subtype="png")
        logo.add_header("Content-Disposition", "inline", filename="signature-logo.png")
        logo["Content-ID"] = "<logo>"
        related.attach(logo)
        return message

    def preview(self, message=None, attachments=None, **options):
        source = self.draft((message or self.message()).as_bytes(policy=policy.SMTP))
        return self.service.draft_attachment_preview("one", source, attachments, **options)

    def assert_stopped_without_writes(self):
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.store_calls, 0)
        self.assertEqual(self.server.expunge_calls, 0)
        self.assert_no_smtp()

    def test_inline_filename_image_is_never_promoted_or_detached(self):
        original = self.message()
        preview = self.preview(original)
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        logo = next(part for part in saved.walk() if part.get_filename() == "signature-logo.png")
        document = next(part for part in saved.iter_parts() if part.get_filename() == "ledger.pdf")
        self.assertEqual(logo.get_content_disposition(), "inline")
        self.assertEqual(str(logo["Content-ID"]), "<logo>")
        self.assertEqual(logo.get_payload(decode=True), b"\x00 exact logo\r\n\xff")
        self.assertNotIn(logo, list(saved.iter_parts()))
        self.assertEqual(document.get_content_disposition(), "attachment")
        self.assertIsNone(document["Content-ID"])
        self.assertEqual(document.get_payload(decode=True), b"\x00 exact document\r\n\xff")
        self.assertIn('src="cid:logo"', saved.get_body(("html",)).get_content())
        self.assertEqual(preview["top_level_attachments"][0]["sha256"], digest(document.get_payload(decode=True)))
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assert_no_smtp()

    def test_duplicate_document_and_image_cid_stops_before_journaling(self):
        message = self.message()
        image = next(part for part in message.walk() if part.get_content_maintype() == "image")
        image.replace_header("Content-ID", "<document>")
        self.error("DRAFT_ATTACHMENT_CONTENT_ID_AMBIGUOUS", lambda: self.preview(message))
        self.assert_stopped_without_writes()

    def test_authored_document_link_is_not_broken_by_cid_stripping(self):
        fragment = ('<object type="application/x-apple-msg-attachment" data="cid:document"></object>'
                    '<a href="cid:document">Read my document</a>')
        self.error("DRAFT_ATTACHMENT_HTML_REFERENCE_UNSUPPORTED", lambda: self.preview(self.message(fragment)))
        self.assert_stopped_without_writes()

    def test_nonempty_object_fallback_is_never_removed(self):
        fragment = '<object type="application/x-apple-msg-attachment" data="cid:document">Important authored note</object>'
        self.error("DRAFT_ATTACHMENT_HTML_NONEMPTY_PLACEHOLDER", lambda: self.preview(self.message(fragment)))
        self.assert_stopped_without_writes()

    def test_unknown_routing_header_is_not_hidden_by_review_adapter(self):
        message = self.message()
        message["Resent-To"] = "unapproved@example.test"
        self.error("UNSUPPORTED_DRAFT_HEADERS", lambda: self.preview(message))
        self.assert_stopped_without_writes()

    def test_benign_apple_headers_are_preserved_but_send_guard_stays_strict(self):
        message = self.message()
        message["X-Apple-Notify-Thread"] = "NO"
        message["X-Apple-Message-Smime-Encrypt"] = "NO"
        preview = self.preview(message)
        saved = email.message_from_bytes(self.raw(preview), policy=policy.default)
        self.assertEqual(str(saved["X-Apple-Notify-Thread"]), "NO")
        self.assertEqual(str(saved["X-Apple-Message-Smime-Encrypt"]), "NO")
        self.error("UNSUPPORTED_DRAFT_HEADERS", lambda: self.service._existing_mime(self.raw(preview), self.entry))
        self.assert_stopped_without_writes()

    def test_html_structural_content_inside_empty_looking_object_is_not_discarded(self):
        prefix = '<object type="application/x-apple-msg-attachment" data="cid:document">'
        for structural in ("</div>", "<!DOCTYPE html>", "<?important authored?>"):
            with self.subTest(structural=structural):
                with self.assertRaises(DraftError):
                    _normalize_html(prefix + structural + '</object>', {"document"}, {"ledger.pdf"}, {"document"}, 0)

    def test_duplicate_mime_part_metadata_is_rejected_before_normalization(self):
        for name, value in (("Content-ID", "<second-document>"),
                            ("Content-Disposition", 'inline; filename="different.pdf"'),
                            ("Content-Type", "application/octet-stream"),
                            ("Content-Transfer-Encoding", "quoted-printable")):
            with self.subTest(header=name):
                message = self.message()
                document = next(part for part in message.walk() if part.get_filename() == "ledger.pdf")
                # _headers is deliberate: EmailMessage's normal API prohibits
                # certain duplicates, but malformed incoming bytes can have them.
                document._headers.append((name, value))
                with self.assertRaises(DraftError):
                    self.preview(message)
                self.assert_stopped_without_writes()


if __name__ == "__main__":
    unittest.main()
