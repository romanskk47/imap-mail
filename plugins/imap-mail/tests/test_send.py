"""Offline SMTP submission tests. Synthetic accounts; no real connections."""

import concurrent.futures
import email
from email import policy
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError, DraftService, digest
from imap_mail import send as send_module
from imap_mail.send import SendService, smtp_factory
from test_core import Server, FakeReader


class SendReader(FakeReader):
    def uid(self, command, *args):
        if command == "FETCH" and ".1>" in args[-1]:
            uid = str(args[0])
            if self.server.eof_unconfirmed:
                return "OK", []
            offset = int(re.search(r"<([0-9]+)\.1>", args[-1])[1])
            if self.server.bad_probe_offset:
                offset += 1
            rows = [(f"1 (UID {uid} BODY[]<{offset}> {{0}}".encode(), b""), b")"]
            if self.server.extra_probe_literal:
                rows.insert(1, (f"1 (UID {uid} BODY[]<{offset}> {{1}}".encode(), b"x"))
            return "OK", rows
        result = super().uid(command, *args)
        if command == "FETCH" and args[-1].endswith("BODY.PEEK[])") and result[1]:
            status, rows = result
            prefix, raw = rows[0]
            prefix += f" {self.server.full_body_marker} {{{len(raw)}}}".encode()
            if self.server.stale_full_size:
                prefix = prefix.replace(f"RFC822.SIZE {len(raw)}".encode(), b"RFC822.SIZE 1")
            if self.server.short_full_fetch:
                raw = raw[:-3]
            return status, [(prefix, raw), b")"]
        return result

    def append(self, mailbox, flags, date, raw):
        if mailbox == '"Drafts"':
            return super().append(mailbox, flags, date, raw)
        if mailbox != '"Sent"' or flags != "(\\Seen)":
            raise AssertionError("Not an authorized Sent copy")
        self.server.append_calls += 1
        self.server.commands.append("APPEND_SENT")
        if self.server.copy_timeout_before:
            raise TimeoutError("private copy failure")
        uid = str(201 + len(self.server.messages["Sent"]))
        self.server.messages["Sent"][uid] = raw
        self.mapping = f"11 {uid}".encode()
        if self.server.copy_timeout_after:
            raise TimeoutError("private copy failure")
        return "OK", [b"saved"]


class SMTP:
    def __init__(self, server):
        self.server = server
        self.sock = self
        self.esmtp_features = {}

    def settimeout(self, timeout):
        self.server.smtp_commands.append(("timeout", timeout))

    def login(self, username, password):
        self.server.smtp_commands.append(("login", username))
        if password != "synthetic-password":
            raise AssertionError("Credential must come from pinned profile")
        if self.server.auth_error:
            raise smtplib.SMTPAuthenticationError(535, b"private-secret-server-response")
        return 235, b"private reply"

    def has_extn(self, name):
        return name.casefold() == "8bitmime" and self.server.eightbitmime

    def mail(self, sender, options=None):
        self.server.smtp_commands.append(("MAIL", sender, options or []))
        return (550 if self.server.sender_refused else 250), b"private reply"

    def rcpt(self, recipient):
        self.server.smtp_commands.append(("RCPT", recipient))
        if self.server.after_rcpt:
            self.server.after_rcpt()
        return (550 if recipient in self.server.refused else 250), b"private reply"

    def rset(self):
        self.server.smtp_commands.append(("RSET",))
        return 250, b"private reply"

    def data(self, raw):
        self.server.smtp_commands.append(("DATA",))
        self.server.data_calls += 1
        self.server.submitted.append(raw)
        if self.server.data_hook:
            self.server.data_hook()
        if self.server.auto_copy:
            self.server.messages["Sent"][str(401 + len(self.server.messages["Sent"]))] = raw
        if self.server.data_timeout:
            raise TimeoutError("private-secret-server-response")
        return self.server.data_code, b"private-secret-server-response"

    def quit(self):
        self.server.smtp_commands.append(("QUIT",))
        if self.server.quit_error:
            raise TimeoutError("private quit reply")
        return 221, b"done"

    def close(self):
        self.server.smtp_commands.append(("close",))


class SendServer(Server):
    def __init__(self):
        super().__init__()
        self.messages["Sent"] = {}
        self.validity["Sent"] = "11"
        self.listing.append({"name": "Sent", "kind": "sent", "flags": ["\\Sent"], "selectable": True})
        self.smtp_commands = []
        self.smtp_connections = 0
        self.data_calls = 0
        self.submitted = []
        self.auto_copy = True
        self.auth_error = self.sender_refused = self.data_timeout = self.quit_error = False
        self.copy_timeout_before = self.copy_timeout_after = self.short_full_fetch = False
        self.stale_full_size = self.eof_unconfirmed = False
        self.bad_probe_offset = self.extra_probe_literal = False
        self.full_body_marker = "BODY[]"
        self.eightbitmime = True
        self.refused = set()
        self.data_code = 250
        self.data_hook = self.after_rcpt = None
        self.client = None

    def reader(self, profile):
        self.connections += 1
        return SendReader(self, profile)

    def smtp(self, settings, context, timeout):
        if not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise AssertionError("Certificate verification is mandatory")
        if timeout <= 0 or timeout > 60:
            raise AssertionError("Bounded timeout required")
        self.smtp_connections += 1
        self.client = SMTP(self)
        return self.client


class SendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir(mode=0o700)
        self.profile = {"id": "one", "host": "imap.example.test", "user": "owner@example.test",
                        "entity": "synthetic", "password_env": "IMAP_MAIL_TEST_SEND_PASSWORD"}
        self.json(self.profiles / "one.json", self.profile)
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.settings = {"host": "smtp.example.test", "port": 465, "security": "ssl",
                         "use_imap_credentials": True, "sent_copy": "server"}
        self.entry = {"identity": {"email": "owner@example.test", "host": "imap.example.test", "entity": "synthetic"},
                      "from_addresses": ["owner@example.test", "alias@example.test"],
                      "default_from": "owner@example.test", "drafts_enabled": True,
                      "signature": {"none": True}, "attachment_roots": [str(self.allowed)],
                      "send_enabled": True, "smtp": self.settings}
        self.config = {"version": 1, "profiles_dir": str(self.profiles), "state_dir": str(self.root / "state"),
                       "accounts": {"one": self.entry}}
        self.config_path = self.root / "settings.json"
        self.save_config()
        self.server = SendServer()
        self.service = self.make_service()
        self.password_env = patch.dict(os.environ, {"IMAP_MAIL_TEST_SEND_PASSWORD": "synthetic-password"})
        self.password_env.start()
        self.addCleanup(self.password_env.stop)

    def json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def save_config(self):
        self.json(self.config_path, self.config)

    def make_service(self):
        return SendService(config_path=self.config_path, reader_factory=self.server.reader,
                           smtp_factory=self.server.smtp)

    def preview(self, **kwargs):
        options = {"account": "one", "body_text": "Hello — preserved body", "to": ["Recipient <recipient@example.test>"],
                   "subject": "Reviewed message"}
        options.update(kwargs)
        return self.service.send_preview(**options)

    def apply(self, preview):
        return self.service.send_apply(preview["preview_id"], preview["preview_hash"], authorization="send")

    def error(self, code, call):
        with self.assertRaises(DraftError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def record(self, preview):
        return json.loads((self.root / "state" / (preview["preview_id"] + ".json")).read_text())

    def draft(self, raw=None):
        if raw is None:
            message = EmailMessage(policy=policy.SMTP)
            message["From"] = "Owner <owner@example.test>"
            message["To"] = "Recipient <recipient@example.test>"
            message["Cc"] = "copy@example.test"
            message["Subject"] = "Existing reviewed draft"
            message["Message-ID"] = "<existing-draft@example.test>"
            message["In-Reply-To"] = "<earlier@example.test>"
            message.set_content("Existing body\n\nExisting signature")
            message.add_alternative("<p>Existing body</p><b>Existing signature</b>", subtype="html")
            message.add_attachment(b"exact attachment bytes", maintype="application", subtype="octet-stream", filename="evidence.bin")
            raw = message.as_bytes()
        self.server.messages["Drafts"]["13"] = raw
        return {"account": "one", "mailbox": "Drafts", "uidvalidity": "9", "uid": "13"}

    def test_check_login_only_and_no_secrets(self):
        result = self.service.send_check("one")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["smtp"]["username"], "owner@example.test")
        self.assertEqual([row[0] for row in self.server.smtp_commands], ["login", "QUIT"])
        self.assertEqual(self.server.data_calls, 0)
        self.assertEqual(self.server.append_calls, 0)
        self.assertNotIn("password", json.dumps(result))

    def test_configuration_proposal_check_does_not_grant_or_write(self):
        self.entry.pop("send_enabled")
        self.entry.pop("smtp")
        self.save_config()
        before = self.config_path.read_bytes()
        result = self.service.check_configuration("one", self.settings)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.service.send_check("one")["error_code"], "SEND_NOT_AUTHORIZED")
        self.assertEqual(self.server.data_calls, 0)

    def test_sending_disabled_by_default_and_inventory_does_not_connect(self):
        self.entry.pop("send_enabled")
        self.save_config()
        inventory = self.service.accounts()["accounts"][0]
        self.assertFalse(inventory["send_enabled"])
        self.assertFalse(inventory["send_ready"])
        self.assertTrue(inventory["drafts_ready"])
        self.assertEqual(self.server.smtp_connections, 0)
        self.error("SEND_NOT_AUTHORIZED", self.preview)

    def test_send_readiness_and_compose_do_not_require_draft_write_permission_or_folder(self):
        self.entry["drafts_enabled"] = False
        self.server.listing = [self.server.listing[-1]]
        self.save_config()
        self.assertEqual(self.service.send_check("one")["status"], "ready")
        inventory = self.service.accounts()["accounts"][0]
        self.assertTrue(inventory["send_ready"])
        self.assertFalse(inventory["drafts_ready"])
        preview = self.preview()
        self.assertNotIn("drafts_mailbox", preview)
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")

    def test_preview_is_read_only_and_submission_exact_bytes(self):
        attachment = self.allowed / "file.bin"
        attachment.write_bytes(b"immutable bytes")
        preview = self.preview(attachments=[str(attachment)], cc=["copy@example.test"])
        expected = (self.root / "state" / (preview["preview_id"] + ".eml")).read_bytes()
        self.assertEqual(preview["operation"], "send")
        self.assertEqual(self.server.smtp_connections, 0)
        self.assertEqual(self.server.append_calls, 0)
        attachment.write_bytes(b"changed later")
        result = self.apply(preview)
        self.assertEqual(self.server.submitted, [expected])
        self.assertEqual(result["status"], "submission_accepted")
        self.assertFalse(result["delivered"])
        self.assertEqual(result["sent_copy"]["status"], "verified")
        self.assertEqual(self.record(preview)["state"], "submission_accepted")
        commands = [row[0] for row in self.server.smtp_commands]
        self.assertLess(commands.index("RCPT"), commands.index("DATA"))
        self.assertEqual(sum(name == "RCPT" for name in commands), 2)

    def test_authorization_must_be_explicit_and_exact(self):
        preview = self.preview()
        self.error("SEND_AUTHORIZATION_REQUIRED", lambda: self.service.send_apply(preview["preview_id"], preview["preview_hash"]))
        self.error("SEND_AUTHORIZATION_REQUIRED", lambda: self.service.send_apply(preview["preview_id"], preview["preview_hash"], authorization="draft"))
        self.assertEqual(self.server.smtp_connections, 0)

    def test_send_rejects_legacy_and_explicit_draft_preview(self):
        service = DraftService(config_path=self.config_path, reader_factory=self.server.reader)
        preview = service.preview("one", "Draft only", to=["recipient@example.test"], subject="Draft")
        self.error("SEND_PREVIEW_REQUIRED", lambda: self.apply(preview))
        self.assertEqual(self.server.smtp_connections, 0)

    def test_hash_mime_configuration_and_profile_drift_block_before_smtp(self):
        preview = self.preview()
        self.error("PREVIEW_HASH_MISMATCH", lambda: self.service.send_apply(preview["preview_id"], "x", "send"))
        eml = self.root / "state" / (preview["preview_id"] + ".eml")
        original = eml.read_bytes()
        eml.write_bytes(original + b"changed")
        self.error("PREVIEW_INTEGRITY_FAILED", lambda: self.apply(preview))
        eml.write_bytes(original)
        self.entry["smtp"]["host"] = "other.example.test"
        self.save_config()
        self.error("ACCOUNT_CONFIGURATION_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.smtp_connections, 0)

    def test_explicit_reuse_tls_and_port_are_mandatory(self):
        for key, value, code in [("use_imap_credentials", False, "SMTP_CREDENTIAL_REUSE_NOT_AUTHORIZED"),
                                 ("security", "plain", "SMTP_TLS_REQUIRED"),
                                 ("port", True, "INVALID_SMTP_PORT"),
                                 ("host", "smtp.example.test\r\ninjected", "INVALID_SMTP_HOST")]:
            original = self.settings[key]
            self.settings[key] = value
            self.save_config()
            self.error(code, self.preview)
            self.settings[key] = original
        self.assertEqual(self.server.smtp_connections, 0)

    def test_smtp_username_cannot_be_overridden_or_cross_account(self):
        self.settings["username"] = "another@example.test"
        self.save_config()
        self.error("INVALID_SMTP_CONFIGURATION", self.preview)
        self.assertEqual(self.server.smtp_connections, 0)

    def test_sent_folder_must_be_verified_special_use(self):
        self.entry["sent_mailbox"] = "INBOX"
        self.save_config()
        self.error("SENT_FOLDER_CONFLICT", self.preview)
        self.entry.pop("sent_mailbox")
        self.server.listing = self.server.listing[:-1]
        self.save_config()
        self.error("SENT_FOLDER_UNAVAILABLE", self.preview)

    def test_partial_recipient_refusal_sends_no_data_and_no_retry(self):
        preview = self.preview(cc=["refused@example.test"])
        self.server.refused.add("refused@example.test")
        result = self.apply(preview)
        self.assertEqual(result["status"], "not_submitted")
        self.assertEqual(result["error_code"], "SMTP_RECIPIENT_REFUSED")
        self.assertIn(("RSET",), self.server.smtp_commands)
        self.assertEqual(self.server.data_calls, 0)
        connections = self.server.smtp_connections
        self.apply(preview)
        self.assertEqual(self.server.smtp_connections, connections)

    def test_sender_refusal_never_attempts_rcpt_or_data(self):
        preview = self.preview()
        self.server.sender_refused = True
        self.assertEqual(self.apply(preview)["error_code"], "SMTP_SENDER_REFUSED")
        self.assertNotIn("RCPT", [row[0] for row in self.server.smtp_commands])
        self.assertEqual(self.server.data_calls, 0)

    def test_attempt_is_durable_before_data_and_acceptance_before_copy(self):
        preview = self.preview()
        self.server.data_hook = lambda: self.assertEqual(self.record(preview)["state"], "attempted")
        original_copy = self.service._copy_sent
        def checked_copy(*args):
            self.assertEqual(self.record(preview)["state"], "submission_accepted")
            return original_copy(*args)
        self.service._copy_sent = checked_copy
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")

    def test_lost_data_reply_stays_uncertain_even_with_verified_sent_copy(self):
        preview = self.preview()
        self.server.data_timeout = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["submission_accepted"])
        self.assertEqual(result["sent_copy"]["status"], "verified")
        self.server.data_timeout = False
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.make_service().send_status(preview["preview_id"])["status"], "uncertain")
        self.assertEqual(self.server.data_calls, 1)

    def test_lost_data_reply_without_sent_copy_never_retries(self):
        preview = self.preview()
        self.server.data_timeout = True
        self.server.auto_copy = False
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.server.data_timeout = False
        self.apply(preview)
        self.service.send_status(preview["preview_id"])
        self.assertEqual(self.server.data_calls, 1)

    def test_negative_data_reply_is_terminal_not_automatically_resubmitted(self):
        preview = self.preview()
        self.server.data_code = 554
        self.assertEqual(self.apply(preview)["status"], "submission_rejected")
        self.server.data_code = 250
        self.apply(preview)
        self.assertEqual(self.server.data_calls, 1)

    def test_accepted_quit_failure_retains_acceptance(self):
        preview = self.preview()
        self.server.quit_error = True
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")
        self.apply(preview)
        self.assertEqual(self.server.data_calls, 1)

    def test_accepted_copy_failure_does_not_become_mail_failure_or_resend(self):
        preview = self.preview()
        self.server.auto_copy = False
        result = self.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(result["sent_copy"]["status"], "not_observed")
        self.apply(preview)
        self.assertEqual(self.server.data_calls, 1)

    def test_accepted_outcome_write_failure_is_reported_as_accepted_and_never_replayed(self):
        preview = self.preview()
        original = send_module.write_json
        def fail_accepted(path, record):
            if record["state"] == "submission_accepted":
                raise OSError("synthetic disk full")
            return original(path, record)
        with patch("imap_mail.send.write_json", side_effect=fail_accepted):
            result = self.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(result["journal_error_code"], "SEND_OUTCOME_PERSISTENCE_FAILED")
        self.assertEqual(result["sent_copy"]["error_code"], "SEND_RECEIPT_NOT_DURABLE")
        self.assertEqual(self.record(preview)["state"], "attempted")
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.server.data_calls, 1)

    def test_unexpected_sent_copy_error_does_not_hide_accepted_submission(self):
        preview = self.preview()
        with patch.object(self.service, "_copy_sent", side_effect=OSError("synthetic disk full")):
            result = self.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(result["sent_copy"]["error_code"], "SENT_COPY_STATUS_UNAVAILABLE")
        self.assertEqual(self.record(preview)["state"], "submission_accepted")
        self.apply(preview)
        self.assertEqual(self.server.data_calls, 1)

    def test_intent_write_failure_blocks_data_entirely(self):
        preview = self.preview()
        with patch("imap_mail.send.write_json", side_effect=OSError("synthetic disk full")):
            self.error("OPERATION_FAILED", lambda: self.apply(preview))
        self.assertEqual(self.server.data_calls, 0)

    def test_append_sent_mode_one_copy_with_seen_and_independent_readback(self):
        self.settings["sent_copy"] = "append"
        self.save_config()
        self.server.auto_copy = False
        preview = self.preview()
        before = self.server.connections
        result = self.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(result["sent_copy"]["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)
        self.assertGreater(self.server.connections - before, 2)
        self.apply(preview)
        self.assertEqual(self.server.append_calls, 1)

    def test_append_policy_avoids_duplicate_if_provider_already_saved_copy(self):
        self.settings["sent_copy"] = "append"
        self.save_config()
        preview = self.preview()
        self.assertEqual(self.apply(preview)["sent_copy"]["status"], "verified")
        self.assertEqual(self.server.append_calls, 0)

    def test_sent_append_lost_response_never_repeated_and_submission_stays_accepted(self):
        self.settings["sent_copy"] = "append"
        self.save_config()
        self.server.auto_copy = False
        self.server.copy_timeout_before = True
        preview = self.preview()
        result = self.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(result["sent_copy"]["status"], "uncertain")
        self.server.copy_timeout_before = False
        self.apply(preview)
        self.service.send_status(preview["preview_id"])
        self.assertEqual(self.server.append_calls, 1)
        self.assertEqual(self.server.data_calls, 1)

    def test_sent_append_accepted_lost_response_can_be_observed_without_reappend(self):
        self.settings["sent_copy"] = "append"
        self.save_config()
        self.server.auto_copy = False
        self.server.copy_timeout_after = True
        preview = self.preview()
        result = self.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(result["sent_copy"]["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)

    def test_existing_draft_keeps_raw_html_signature_attachments_and_source_untouched(self):
        source = self.draft()
        expected = self.server.messages["Drafts"]["13"]
        preview = self.service.send_preview("one", draft=source)
        self.assertTrue(preview["preserved_existing_draft"])
        self.assertIn("Existing signature", preview["rendered_body_html"])
        self.assertEqual(preview["attachments"][0]["sha256"], digest(b"exact attachment bytes"))
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")
        self.assertEqual(self.server.submitted, [expected])
        self.assertEqual(self.server.messages["Drafts"]["13"], expected)
        self.assertEqual(self.server.append_calls, 0)

    def test_existing_draft_rejects_cross_account_stale_uid_and_mixed_compose(self):
        source = self.draft()
        self.error("DRAFT_AND_COMPOSE_ARE_EXCLUSIVE", lambda: self.preview(draft=source))
        source["account"] = "two"
        self.error("DRAFT_ACCOUNT_MISMATCH", lambda: self.service.send_preview("one", draft=source))
        source["account"] = "one"
        source["uidvalidity"] = "8"
        self.error("DRAFTS_FOLDER_CHANGED", lambda: self.service.send_preview("one", draft=source))

    def test_existing_draft_human_change_or_removed_draft_flag_blocks_data(self):
        source = self.draft()
        preview = self.service.send_preview("one", draft=source)
        self.server.messages["Drafts"]["13"] = self.server.messages["Drafts"]["13"].replace(b"Existing body", b"Changed body")
        self.error("DRAFT_SOURCE_CHANGED", lambda: self.apply(preview))
        self.draft()
        self.server.draft_flag = False
        self.error("DRAFT_FLAG_NOT_CONFIRMED", lambda: self.apply(preview))
        self.assertEqual(self.server.data_calls, 0)

    def test_same_draft_source_cannot_be_sent_via_a_new_or_concurrent_preview(self):
        source = self.draft()
        first = self.service.send_preview("one", draft=source)
        second = self.service.send_preview("one", draft=source)
        self.apply(first)
        self.error("DRAFT_SOURCE_SEND_ALREADY_ATTEMPTED", lambda: self.apply(second))
        self.error("DRAFT_SOURCE_SEND_ALREADY_ATTEMPTED", lambda: self.service.send_preview("one", draft=source))
        self.assertEqual(self.server.data_calls, 1)

    def test_resaved_draft_retaining_message_id_cannot_bypass_send_journal(self):
        source = self.draft()
        first = self.service.send_preview("one", draft=source)
        self.apply(first)
        self.server.messages["Drafts"]["14"] = self.server.messages["Drafts"]["13"]
        resaved = {**source, "uid": "14"}
        self.error("DRAFT_SOURCE_SEND_ALREADY_ATTEMPTED", lambda: self.service.send_preview("one", draft=resaved))
        self.assertEqual(self.server.data_calls, 1)

    def test_known_apple_mail_metadata_is_preserved_but_not_interpreted_as_routing(self):
        source = self.draft()
        metadata = (b"X-Mailer: Apple Mail (2.3826)\r\n"
                    b"X-Uniform-Type-Identifier: com.apple.mail-draft\r\n"
                    b"X-Universally-Unique-Identifier: synthetic-uuid\r\n"
                    b"X-Apple-Windows-Friendly: 1\r\n"
                    b"X-Apple-Auto-Saved: 1\r\n")
        raw = metadata + self.server.messages["Drafts"]["13"]
        self.draft(raw)
        preview = self.service.send_preview("one", draft=source)
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")
        self.assertEqual(self.server.submitted[0], raw)

    def test_existing_draft_bcc_resent_and_unsupported_headers_fail_closed(self):
        source = self.draft()
        raw = self.server.messages["Drafts"]["13"]
        for name in (b"Bcc", b"Resent-To", b"Return-Path"):
            self.draft(name + b": hidden@example.test\r\n" + raw)
            self.error("UNSUPPORTED_DRAFT_HEADERS", lambda: self.service.send_preview("one", draft=source))
        self.assertEqual(self.server.smtp_connections, 0)

    def test_incomplete_full_fetch_is_never_frozen_or_sent(self):
        source = self.draft()
        self.server.short_full_fetch = True
        self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", lambda: self.service.send_preview("one", draft=source))

    def test_nonascii_envelope_fails_before_mail_but_unicode_display_name_is_supported(self):
        preview = self.preview(to=["Unicode name <recipient@exämple.test>"])
        self.error("SMTPUTF8_NOT_SUPPORTED", lambda: self.apply(preview))
        self.assertEqual(self.server.smtp_connections, 0)
        preview = self.preview(to=["Łukasz <recipient@example.test>"])
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")

    def test_stale_rfc822_size_requires_matching_literal_and_independent_eof(self):
        source = self.draft()
        self.server.stale_full_size = True
        preview = self.service.send_preview("one", draft=source)
        self.assertTrue(preview["preserved_existing_draft"])
        self.server.eof_unconfirmed = True
        self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", lambda: self.apply(preview))
        self.assertEqual(self.server.data_calls, 0)

    def test_full_fetch_rejects_partial_or_other_section_markers(self):
        source = self.draft()
        for marker in ("BODY[]<0>", "BODY[HEADER]", "BODY[TEXT]", "BODY.PEEK[]"):
            self.server.full_body_marker = marker
            self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", lambda: self.service.send_preview("one", draft=source))

    def test_eof_probe_rejects_wrong_offset_or_contradictory_extra_literal(self):
        source = self.draft()
        self.server.stale_full_size = True
        self.server.bad_probe_offset = True
        self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", lambda: self.service.send_preview("one", draft=source))
        self.server.bad_probe_offset = False
        self.server.extra_probe_literal = True
        self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", lambda: self.service.send_preview("one", draft=source))

    def test_composed_polish_text_is_7bit_safe_without_8bitmime(self):
        self.server.eightbitmime = False
        preview = self.preview(body_text="Zażółć gęślą jaźń — treść wiadomości")
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")
        self.assertTrue(self.server.submitted[0].isascii())
        message = email.message_from_bytes(self.server.submitted[0], policy=policy.default)
        self.assertIn("Zażółć", message.get_body(preferencelist=("plain",)).get_content())

    def test_existing_8bit_body_negotiates_extension_without_modifying_mime(self):
        source = self.draft()
        message = email.message_from_bytes(self.server.messages["Drafts"]["13"], policy=policy.default)
        message.get_body(preferencelist=("plain",)).set_content("Zażółć — istniejąca treść", cte="8bit")
        raw = message.as_bytes(policy=policy.SMTP)
        self.assertFalse(raw.isascii())
        self.draft(raw)
        preview = self.service.send_preview("one", draft=source)
        self.server.eightbitmime = False
        self.error("SMTP_8BITMIME_REQUIRED", lambda: self.apply(preview))
        self.assertEqual(self.server.data_calls, 0)
        self.server.eightbitmime = True
        self.assertEqual(self.apply(preview)["status"], "submission_accepted")
        self.assertIn(("MAIL", "owner@example.test", ["BODY=8BITMIME"]), self.server.smtp_commands)
        self.assertEqual(self.server.submitted[0], raw)

    def test_second_visible_text_body_is_present_in_complete_mime_review(self):
        source = self.draft()
        message = email.message_from_bytes(self.server.messages["Drafts"]["13"], policy=policy.default)
        extra = EmailMessage(policy=policy.SMTP)
        extra.set_content("Second recipient-visible body")
        message.attach(extra)
        self.draft(message.as_bytes(policy=policy.SMTP))
        preview = self.service.send_preview("one", draft=source)
        self.assertTrue(any("Second recipient-visible body" in part.get("content", "")
                            for part in preview["mime_parts"]))

    def test_sent_uidvalidity_and_config_drift_during_preflight_block_data(self):
        preview = self.preview()
        self.server.after_rcpt = lambda: self.server.validity.update(Sent="12")
        self.error("SENT_FOLDER_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.data_calls, 0)

    def test_broker_config_blocks_smtp_not_only_drafts(self):
        with patch.dict(os.environ, {"MAIL_BROKER_CONFIG": "/not/read"}):
            self.error("BROKER_SEND_ADAPTER_REQUIRED", self.preview)
        self.assertEqual(self.server.smtp_connections, 0)

    def test_errors_and_journal_never_store_server_reply_or_password(self):
        self.server.auth_error = True
        check = self.service.send_check("one")
        self.assertEqual(check["error_code"], "SEND_CHECK_FAILED")
        self.assertNotIn("private-secret", json.dumps(check))
        self.server.auth_error = False
        preview = self.preview()
        self.server.data_timeout = True
        result = self.apply(preview)
        serialized = json.dumps(result) + json.dumps(self.record(preview))
        self.assertNotIn("private-secret", serialized)
        self.assertNotIn("synthetic-password", serialized)

    def test_concurrent_apply_has_one_data_submission(self):
        preview = self.preview()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.apply(preview), range(2)))
        self.assertEqual([result["status"] for result in results], ["submission_accepted"] * 2)
        self.assertEqual(self.server.data_calls, 1)


class SMTPFactoryTests(unittest.TestCase):
    def test_ssl_uses_default_context_bounded_timeout_and_ehlo(self):
        settings = {"host": "smtp.example.test", "port": 465, "security": "ssl"}
        context = ssl.create_default_context()
        with patch("imap_mail.send.smtplib.SMTP_SSL") as factory:
            factory.return_value.ehlo.return_value = (250, b"safe")
            smtp_factory(settings, context, 30)
            factory.assert_called_once_with("smtp.example.test", 465, context=context, timeout=30)
            factory.return_value.ehlo.assert_called_once_with()

    def test_starttls_requires_extension_and_ehlo_again_before_login(self):
        settings = {"host": "smtp.example.test", "port": 587, "security": "starttls"}
        context = ssl.create_default_context()
        with patch("imap_mail.send.smtplib.SMTP") as factory:
            client = factory.return_value
            client.ehlo.return_value = (250, b"safe")
            client.has_extn.return_value = True
            client.starttls.return_value = (220, b"safe")
            smtp_factory(settings, context, 30)
            self.assertEqual(client.ehlo.call_count, 2)
            client.starttls.assert_called_once_with(context=context)
            client.login.assert_not_called()
        with patch("imap_mail.send.smtplib.SMTP") as factory:
            client = factory.return_value
            client.ehlo.return_value = (250, b"safe")
            client.has_extn.return_value = False
            with self.assertRaises(DraftError) as caught:
                smtp_factory(settings, context, 30)
            self.assertEqual(caught.exception.code, "SMTP_STARTTLS_REQUIRED")
            client.login.assert_not_called()
            client.starttls.assert_not_called()
            client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
