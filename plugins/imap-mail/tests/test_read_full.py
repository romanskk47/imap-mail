"""Complete read-only MIME reads; synthetic profiles and messages only."""

import copy
from email import policy
from email.message import EmailMessage
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError, digest
from imap_mail.edit import EditService
from imap_mail import send as send_module
import test_send as send_fixture


class FullReadReader(send_fixture.SendReader):
    def uid(self, command, *args):
        self.server.read_queries.append((command, args))
        return super().uid(command, *args)


class FullReadTests(unittest.TestCase):
    def setUp(self):
        # Reuse its synthetic account/server setup, not its TestCase inheritance
        # or imported TestCase name (which would duplicate the SMTP test suite).
        self.fixture = send_fixture.SendTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.server = self.fixture.server
        self.server.read_queries = []
        self.service = EditService(config_path=self.fixture.config_path,
            reader_factory=lambda profile: FullReadReader(self.server, profile),
            smtp_factory=lambda *args: self.fail("Reading must not connect to SMTP"))

    def message(self, *, plain="Read-only message", html=None, files=None, inline=False):
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "Owner <owner@example.test>"
        message["To"] = "Recipient <recipient@example.test>"
        message["Cc"] = "copy@example.test"
        message["Subject"] = "Synthetic full MIME read"
        message["Date"] = "Wed, 21 Oct 2026 10:00:00 +0000"
        message["Message-ID"] = "<full-read@example.test>"
        message.set_content(plain)
        if html is not None:
            message.add_alternative(html, subtype="html")
        if inline:
            message.get_payload()[-1].add_related(b"synthetic image bytes",
                maintype="image", subtype="png", cid="<logo@example.test>")
        for filename, data in (files or []):
            message.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
        return message

    def save(self, message):
        raw = message if isinstance(message, bytes) else message.as_bytes()
        self.server.messages["Drafts"]["13"] = raw
        return raw

    def read(self, **kwargs):
        args = {"account": "one", "mailbox": "Drafts", "uidvalidity": "9", "uid": "13"}
        args.update(kwargs)
        return self.service.read(**args)

    def error(self, code, call):
        with self.assertRaises(DraftError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def test_full_mime_over_old_two_megabyte_bound_has_three_hashed_attachments(self):
        files = [("scan.pdf", b"s" * (2 * 1024 * 1024)),
                 ("analysis.pdf", b"synthetic analysis"), ("analysis.xlsx", b"synthetic spreadsheet")]
        raw = self.save(self.message(plain="", html="<p>First corrected sentence.</p><p>Second corrected sentence.</p>",
                                     files=files))
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        result = self.read()
        self.assertEqual(result["body"], "First corrected sentence. Second corrected sentence.")
        self.assertEqual(result["body_type"], "text/html")
        self.assertTrue(result["mime_complete"])
        self.assertTrue(result["attachment_manifest_complete"])
        self.assertFalse(result["truncated"])
        self.assertFalse(result["body_truncated"])
        self.assertFalse(result["attachments_read"])
        self.assertEqual(result["raw_bytes"], len(raw))
        self.assertEqual(result["content_sha256"], digest(raw))
        self.assertEqual(result["attachments"], [name for name, _ in files])
        for index, (detail, (name, payload)) in enumerate(zip(result["attachment_details"], files), start=4):
            self.assertEqual(detail, {"part_index": index, "filename": name, "type": "application/octet-stream",
                "disposition": "attachment", "cid": "", "bytes": len(payload), "sha256": digest(payload)})

    def test_empty_or_whitespace_plain_uses_nonempty_html_and_strips_script_style(self):
        self.save(self.message(plain=" \t\n", html="<style>hidden style</style><p>A &amp; B</p><script>hidden code</script>"))
        self.assertEqual(self.read()["body"], "A & B")

    def test_nonempty_plain_remains_preferred_and_normalized(self):
        self.save(self.message(plain="Actual plain\nNext line", html="<p>Alternate rich body</p>"))
        result = self.read()
        self.assertEqual(result["body"], "Actual plain\nNext line\n")
        self.assertEqual(result["body_type"], "text/plain")

    def test_inline_cid_resource_is_in_complete_leaf_manifest(self):
        self.save(self.message(html='<p>Body</p><img src="cid:logo@example.test">', inline=True))
        result = self.read()
        self.assertEqual(len(result["attachment_details"]), 1)
        resource = result["attachment_details"][0]
        self.assertIsNone(resource["filename"])
        self.assertEqual(resource["cid"], "<logo@example.test>")
        self.assertEqual(resource["type"], "image/png")
        self.assertEqual(resource["bytes"], len(b"synthetic image bytes"))
        self.assertEqual(resource["sha256"], digest(b"synthetic image bytes"))

    def test_read_requires_no_draft_or_smtp_opt_in_signature_or_journal(self):
        self.fixture.entry.clear()
        self.fixture.entry["identity"] = {"email": "owner@example.test", "host": "imap.example.test", "entity": "synthetic"}
        self.fixture.save_config()
        raw = self.save(self.message())
        before = copy.deepcopy(self.server.messages)
        settings_before = self.fixture.config_path.read_bytes()
        result = self.read()
        self.assertEqual(result["body"], "Read-only message\n")
        self.assertEqual(result["account"], "one")
        self.assertEqual(result["entity"], "synthetic")
        self.assertEqual(result["email"], "owner@example.test")
        self.assertEqual(result["access"], "read-only")
        self.assertEqual(result["mailbox"], "Drafts")
        self.assertEqual(result["uidvalidity"], "9")
        self.assertEqual(result["uid"], "13")
        self.assertEqual(result["flags"], ["\\Draft"])
        self.assertEqual(result["To"], "Recipient <recipient@example.test>")
        self.assertEqual(result["Cc"], "copy@example.test")
        self.assertEqual(result["Date"], "Wed, 21 Oct 2026 10:00:00 +0000")
        self.assertIsNone(result["internal_date"])
        self.assertFalse(result["internal_date_available"])
        self.assertEqual(result["contentTrust"], "untrusted email")
        self.assertEqual(self.server.messages, before)
        self.assertEqual(self.server.messages["Drafts"]["13"], raw)
        self.assertEqual(self.fixture.config_path.read_bytes(), settings_before)
        self.assertFalse((self.fixture.root / "state").exists())
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.smtp_connections, 0)
        self.assertEqual(self.server.smtp_commands, [])
        self.assertEqual(self.server.commands, ["EXAMINE", "FETCH", "LOGOUT"])
        self.assertEqual(self.server.read_queries, [("FETCH", ("13", "(UID RFC822.SIZE FLAGS BODY.PEEK[])"))])

    def test_stale_stored_size_requires_exact_one_byte_eof_peek(self):
        raw = self.save(self.message(html="<p>Complete converted MIME</p>"))
        self.server.stale_full_size = True
        result = self.read()
        self.assertTrue(result["mime_complete"])
        self.assertEqual(self.server.read_queries[-1], ("FETCH", ("13", f"(UID BODY.PEEK[]<{len(raw)}.1>)")))

    def test_unconfirmed_or_contradictory_eof_never_claims_complete(self):
        self.save(self.message())
        self.server.stale_full_size = True
        for attr in ("eof_unconfirmed", "bad_probe_offset", "extra_probe_literal"):
            with self.subTest(attr=attr):
                setattr(self.server, attr, True)
                self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", self.read)
                setattr(self.server, attr, False)

    def test_body_display_limit_does_not_mean_mime_or_manifest_is_incomplete(self):
        self.save(self.message(plain="x" * 60001, files=[("whole.bin", b"complete attachment")]))
        result = self.read()
        self.assertEqual(len(result["body"]), 60000)
        self.assertEqual(result["body_characters"], 60002)
        self.assertTrue(result["body_truncated"])
        self.assertTrue(result["truncated"])
        self.assertTrue(result["mime_complete"])
        self.assertTrue(result["attachment_manifest_complete"])
        self.assertEqual(result["attachment_details"][0]["sha256"], digest(b"complete attachment"))

    def test_changed_uidvalidity_stops_before_fetch(self):
        self.save(self.message())
        self.error("UIDVALIDITY_CHANGED", lambda: self.read(uidvalidity="8"))
        self.assertEqual(self.server.commands, ["EXAMINE", "LOGOUT"])

    def test_wrong_body_literal_marker_or_short_literal_stops_before_parsing(self):
        self.save(self.message())
        self.server.short_full_fetch = True
        self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", self.read)
        self.server.short_full_fetch = False
        self.server.full_body_marker = "BODY[]<0>"
        self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", self.read)

    def test_missing_message_is_explicit_error_not_empty_body(self):
        self.error("MESSAGE_UNAVAILABLE", self.read)

    def test_invalid_stable_reference_is_rejected_before_connection_command(self):
        self.error("INVALID_MESSAGE_REFERENCE", lambda: self.read(uid="1:*"))
        self.assertEqual(self.server.commands, ["LOGOUT"])

    def test_raw_message_limit_is_explicit_error_not_partial_success(self):
        raw = self.save(self.message())
        self.assertEqual(send_module.MAX_RAW, 16 * 1024 * 1024)
        with patch.object(send_module, "MAX_RAW", len(raw) - 1):
            self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", self.read)

    def test_missing_multipart_close_boundary_is_rejected(self):
        message = self.message(files=[("complete.bin", b"payload")])
        raw = message.as_bytes()
        closing = b"--" + message.get_boundary().encode() + b"--"
        self.save(raw[:raw.index(closing)])
        self.error("MALFORMED_MESSAGE_MIME", self.read)

    def test_invalid_base64_attachment_is_rejected_after_decode(self):
        raw = self.message(files=[("invalid.bin", b"Hello")]).as_bytes()
        self.assertIn(b"SGVsbG8=", raw)
        self.save(raw.replace(b"SGVsbG8=", b"SGVsbG8"))
        self.error("MALFORMED_MESSAGE_MIME", self.read)

    def test_attached_eml_keeps_parent_readable_and_manifest_explicit(self):
        message = self.message()
        message.add_attachment(self.message(plain="Attached message body"), filename="forwarded.eml")
        self.save(message)
        result = self.read()
        self.assertEqual(result["body"], "Read-only message\n")
        self.assertTrue(result["mime_complete"])
        self.assertFalse(result["attachment_manifest_complete"])
        self.assertEqual(result["attachment_details"], [])
        self.assertEqual(result["unsupported_parts"], [{"part_index": 2, "filename": "forwarded.eml",
            "type": "message/rfc822", "reason": "UNSUPPORTED_ATTACHMENT_CONTAINER"}])

    def test_multipart_attachment_container_is_not_silently_omitted(self):
        container = self.message(html="<p>Attached rich message</p>")
        container["Content-Disposition"] = 'attachment; filename="multipart.mime"'
        message = self.message()
        message.make_mixed()
        message.attach(container)
        self.save(message)
        result = self.read()
        self.assertEqual(result["body"], "Read-only message\n")
        self.assertFalse(result["attachment_manifest_complete"])
        self.assertEqual(result["unsupported_parts"][0]["filename"], "multipart.mime")
        self.assertEqual(result["attachment_details"], [])

    def test_delivery_status_keeps_explanation_readable_with_opaque_report(self):
        raw = (b"From: postmaster@example.test\r\nTo: owner@example.test\r\n"
               b"Subject: Delivery failure\r\nMIME-Version: 1.0\r\n"
               b'Content-Type: multipart/report; report-type=delivery-status; boundary="dsn"\r\n\r\n'
               b"--dsn\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               b"Your message could not be delivered.\r\n"
               b"--dsn\r\nContent-Type: message/delivery-status\r\n\r\n"
               b"Reporting-MTA: dns; mail.example.test\r\n\r\n"
               b"Final-Recipient: rfc822; unavailable@example.test\r\n"
               b"Action: failed\r\nStatus: 5.1.1\r\n\r\n--dsn--\r\n")
        self.save(raw)
        result = self.read()
        self.assertEqual(result["body"], "Your message could not be delivered.")
        self.assertEqual(result["content_sha256"], digest(raw))
        self.assertTrue(result["mime_complete"])
        self.assertFalse(result["attachment_manifest_complete"])
        self.assertEqual(result["attachment_details"], [])
        self.assertEqual(result["unsupported_parts"], [{"part_index": 2, "filename": None,
            "type": "message/delivery-status", "reason": "UNSUPPORTED_ATTACHMENT_CONTAINER"}])

    def test_opaque_container_descendants_remain_validated(self):
        message = self.message()
        message.add_attachment(self.message(files=[("bad.bin", b"Hello")]), filename="forwarded.eml")
        raw = message.as_bytes()
        self.assertIn(b"SGVsbG8=", raw)
        self.save(raw.replace(b"SGVsbG8=", b"SGVsbG8"))
        self.error("MALFORMED_MESSAGE_MIME", self.read)

    def test_opaque_inline_or_named_container_cannot_replace_parent_body(self):
        for disposition in (None, "inline"):
            for parent_kind in ("html", "alternative", "related"):
                with self.subTest(disposition=disposition, parent_kind=parent_kind):
                    if parent_kind == "html":
                        message = self.message()
                        message.set_content("<p>Actual parent body</p>", subtype="html")
                    elif parent_kind == "alternative":
                        message = self.message(plain="Actual parent plain", html="<p>Actual parent rich</p>")
                    else:
                        message = self.message(plain="Unselected related resource")
                        message.make_related()
                        message.get_payload()[0]["Content-ID"] = "<resource@example.test>"
                        message.add_related("<p>Actual related root</p>", subtype="html", cid="<root@example.test>")
                        message.set_param("start", "<root@example.test>")
                    message.make_mixed()
                    nested = self.message(plain="Nested attachment must stay separate", html="<p>Nested rich body</p>")
                    if disposition is None:
                        nested.set_param("name", "attached.mime")
                    else:
                        nested["Content-Disposition"] = 'inline; filename="attached.mime"'
                    message.attach(nested)
                    raw = self.save(message)
                    result = self.read()
                    expected = {"html": "Actual parent body", "alternative": "Actual parent plain\n",
                                "related": "Actual related root"}[parent_kind]
                    self.assertEqual(result["body"], expected)
                    self.assertEqual(result["body_type"], "text/plain" if parent_kind == "alternative" else "text/html")
                    self.assertEqual(result["content_sha256"], digest(raw))
                    self.assertEqual(self.server.messages["Drafts"]["13"], raw)
                    self.assertFalse(result["attachment_manifest_complete"])
                    index = next(i for i, part in enumerate(message.walk()) if part is nested)
                    self.assertEqual(result["unsupported_parts"], [{"part_index": index, "filename": "attached.mime",
                        "type": "multipart/alternative", "reason": "UNSUPPORTED_ATTACHMENT_CONTAINER"}])

    def test_empty_mime_is_rejected(self):
        self.save(b"")
        self.error("MALFORMED_MESSAGE_MIME", self.read)


if __name__ == "__main__":
    unittest.main()
