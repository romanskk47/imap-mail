"""Draft-only transport tests: synthetic messages, no network or credentials."""

import email
from email import policy
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError, DraftService, digest, semantic


class Server:
    def __init__(self):
        self.messages = {"INBOX": {}, "Drafts": {}}
        self.validity = {"INBOX": "7", "Drafts": "9"}
        self.listing = [{"name": "Drafts", "kind": "drafts", "selectable": True}]
        self.append_calls = 0
        self.connections = 0
        self.commands = []
        self.timeout_after_accept = False
        self.timeout_before_accept = False
        self.missing_appenduid = False
        self.invalid_appenduid = False
        self.corrupt_saved = False
        self.draft_flag = True

    def reader(self, profile):
        self.connections += 1
        return FakeReader(self, profile)


class FakeReader:
    def __init__(self, server, profile):
        self.server = server
        self.profile = profile
        self.client = self
        self.mailbox = None
        self.mapping = None

    @staticmethod
    def _quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def close(self):
        self.server.commands.append("LOGOUT")

    def folders(self):
        return {"account": self.profile["id"], "folders": self.server.listing}

    def select(self, mailbox):
        self.server.commands.append("EXAMINE")
        self.mailbox = mailbox
        return self.server.validity[mailbox], len(self.server.messages[mailbox])

    def response(self, name):
        return name, [self.mapping] if self.mapping else [None]

    def uid(self, command, *args):
        self.server.commands.append(command)
        if command == "SEARCH":
            message_id = args[-1].strip('"')
            found = [str(uid) for uid, raw in self.server.messages[self.mailbox].items()
                     if str(email.message_from_bytes(raw, policy=policy.default).get("Message-ID")) == message_id]
            return "OK", [" ".join(found).encode()]
        if command == "FETCH":
            if "BODY.PEEK[]" not in args[-1]:
                raise AssertionError("Only bounded PEEK allowed")
            uid = str(args[0])
            raw = self.server.messages[self.mailbox].get(uid)
            if raw is None:
                return "OK", []
            flags = "\\Draft" if self.mailbox == "Drafts" and self.server.draft_flag else ""
            return "OK", [(f"1 (UID {uid} RFC822.SIZE {len(raw)} FLAGS ({flags})".encode(), raw), b")"]
        raise AssertionError("Unexpected command: " + command)

    def append(self, mailbox, flags, date, raw):
        self.server.append_calls += 1
        self.server.commands.append("APPEND")
        if flags != "(\\Draft)" or mailbox != '"Drafts"':
            raise AssertionError("Write outside confirmed Drafts")
        if self.server.timeout_before_accept:
            raise TimeoutError("Synthetic private server error")
        uid = str(101 + len(self.server.messages["Drafts"]))
        saved = raw.replace(b"Authored reply", b"Unauthorized changed body") if self.server.corrupt_saved else raw
        self.server.messages["Drafts"][uid] = saved
        if not self.server.missing_appenduid:
            self.mapping = b"broken mapping" if self.server.invalid_appenduid else f"9 {uid}".encode()
        if self.server.timeout_after_accept:
            raise TimeoutError("Synthetic private server error")
        return "OK", [b"Saved"]


class DraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir(mode=0o700)
        self.profile_path = self.profiles / "one.json"
        self.profile = {"id": "one", "host": "imap.example.test", "user": "owner@example.test",
                        "entity": "synthetic", "password_env": "IMAP_MAIL_TEST_UNUSED_PASSWORD"}
        self.json(self.profile_path, self.profile)
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.config_path = self.root / "settings.json"
        self.entry = {"identity": {"email": "owner@example.test", "host": "imap.example.test", "entity": "synthetic"},
                      "from_addresses": ["owner@example.test", "alias@example.test"],
                      "default_from": "owner@example.test", "drafts_enabled": True,
                      "signature": {"none": True}, "attachment_roots": [str(self.allowed)]}
        self.config = {"version": 1, "profiles_dir": str(self.profiles), "state_dir": str(self.root / "state"),
                       "accounts": {"one": self.entry}}
        self.json(self.config_path, self.config)
        self.server = Server()
        self.service = self.make_service()

    def make_service(self):
        return DraftService(config_path=self.config_path, reader_factory=self.server.reader)

    def json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def preview(self, **kwargs):
        options = {"account": "one", "body_text": "Authored reply", "to": ["recipient@example.test"], "subject": "Test draft"}
        options.update(kwargs)
        return self.service.preview(**options)

    def apply(self, preview):
        return self.service.apply(preview["preview_id"], preview["preview_hash"])

    def error(self, code, call):
        with self.assertRaises(DraftError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)

    def source(self, body="Original body", attachments=False):
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "Customer <customer@example.test>"
        message["Reply-To"] = "Support <support@example.test>"
        message["To"] = "owner@example.test, colleague@example.test"
        message["Cc"] = "alias@example.test, copy@example.test, colleague@example.test"
        message["Subject"] = "Existing thread"
        message["Message-ID"] = "<source@example.test>"
        message["References"] = "<earlier@example.test>"
        message["Date"] = "Wed, 21 Oct 2026 10:00:00 +0000"
        message.set_content(body)
        if attachments:
            message.add_attachment(b"original private attachment", maintype="application", subtype="octet-stream", filename="not-forwarded.bin")
        self.server.messages["INBOX"]["13"] = message.as_bytes()
        return {"mailbox": "INBOX", "uidvalidity": "7", "uid": "13"}

    def mime(self, preview):
        return email.message_from_bytes((self.root / "state" / (preview["preview_id"] + ".eml")).read_bytes(), policy=policy.default)

    def test_missing_configuration_reports_setup_needed(self):
        service = DraftService(config_path=self.root / "missing.json")
        with patch("imap_mail.core.read_access.profiles", return_value={}):
            result = service.accounts()
        self.assertEqual(result["status"], "setup_needed")
        self.assertEqual(result["accounts"], [])

    def test_account_discovery_has_no_network_and_exposes_no_password_source(self):
        result = self.service.accounts()
        self.assertEqual(result["accounts"][0]["account"], "one")
        self.assertNotIn("password", json.dumps(result))
        self.assertEqual(self.server.connections, 0)

    def test_preview_is_private_read_only_and_apply_independently_verifies(self):
        preview = self.preview()
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(preview["mailbox_writes"], 0)
        self.assertEqual((self.root / "state").stat().st_mode & 0o777, 0o700)
        for path in (self.root / "state").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = self.server.connections
        result = self.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["receipt"]["uid"], "101")
        self.assertGreaterEqual(self.server.connections - before, 2)
        self.assertEqual(self.server.append_calls, 1)
        self.assertTrue(set(self.server.commands) <= {"LOGOUT", "EXAMINE", "APPEND", "FETCH", "SEARCH"})

    def test_verified_apply_is_idempotent_and_preserves_human_edits(self):
        preview = self.preview()
        self.apply(preview)
        self.server.messages["Drafts"]["101"] = b"Human changed the draft"
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.server.messages["Drafts"]["101"], b"Human changed the draft")
        self.assertEqual(self.server.append_calls, 1)

    def test_accepted_append_with_lost_response_is_recovered_without_repeating(self):
        preview = self.preview()
        self.server.timeout_after_accept = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.make_service().apply(preview["preview_id"], preview["preview_hash"])["status"], "verified")
        self.assertEqual(self.server.append_calls, 1)

    def test_uncertain_append_not_found_is_never_repeated_even_after_restart(self):
        preview = self.preview()
        self.server.timeout_before_accept = True
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.server.timeout_before_accept = False
        service = self.make_service()
        self.assertEqual(service.apply(preview["preview_id"], preview["preview_hash"])["status"], "uncertain")
        self.assertEqual(service.status(preview["preview_id"])["status"], "uncertain")
        self.assertEqual(self.server.append_calls, 1)

    def test_missing_appenduid_uses_unique_message_id(self):
        preview = self.preview()
        self.server.missing_appenduid = True
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertIn("SEARCH", self.server.commands)

    def test_invalid_appenduid_is_not_replaced_by_guess(self):
        preview = self.preview()
        self.server.invalid_appenduid = True
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.service.status(preview["preview_id"])["status"], "uncertain")
        self.assertEqual(self.server.append_calls, 1)

    def test_changed_saved_content_or_missing_draft_flag_is_not_success(self):
        preview = self.preview()
        self.server.corrupt_saved = True
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.server.corrupt_saved = False
        self.server.draft_flag = False
        second = self.preview(subject="Second")
        self.assertEqual(self.apply(second)["status"], "uncertain")

    def test_reply_all_preserves_chain_and_excludes_all_own_aliases(self):
        source = self.source(attachments=True)
        preview = self.preview(to=None, subject=None, reply=source, reply_all=True)
        message = self.mime(preview)
        self.assertEqual(str(message["In-Reply-To"]), "<source@example.test>")
        self.assertEqual(str(message["References"]), "<earlier@example.test> <source@example.test>")
        self.assertEqual(preview["subject"], "Re: Existing thread")
        self.assertIn("support@example.test", str(message["To"]))
        self.assertIn("colleague@example.test", str(message["To"]))
        self.assertEqual(str(message["Cc"]), "copy@example.test")
        self.assertNotIn("owner@example.test", str(message["To"]))
        self.assertNotIn("alias@example.test", str(message["Cc"]))
        self.assertEqual(list(message.iter_attachments()), [])
        self.assertIn("Original body", preview["rendered_body_text"])

    def test_source_uidvalidity_and_content_drift_block_apply(self):
        source = self.source()
        preview = self.preview(to=None, subject=None, reply=source)
        self.server.validity["INBOX"] = "8"
        self.error("UIDVALIDITY_CHANGED", lambda: self.apply(preview))
        self.server.validity["INBOX"] = "7"
        self.source(body="Changed body")
        self.error("SOURCE_MESSAGE_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_explicit_reply_account_is_pinned_and_mismatch_fails_before_login(self):
        source = self.source()
        source["account"] = "another-account"
        self.error("REPLY_ACCOUNT_MISMATCH", lambda: self.preview(to=None, subject=None, reply=source))
        self.assertEqual(self.server.connections, 0)
        source["account"] = "one"
        preview = self.preview(to=None, subject=None, reply=source)
        self.assertEqual(preview["reply"]["account"], "one")
        self.assertEqual(self.apply(preview)["status"], "verified")

    def test_legacy_three_field_reply_reference_remains_accepted(self):
        preview = self.preview(to=None, subject=None, reply=self.source())
        self.assertEqual(preview["reply"]["account"], "one")
        self.assertEqual(self.apply(preview)["status"], "verified")

    def test_existing_prepared_and_uncertain_records_without_reply_account_remain_usable(self):
        def legacy_record(preview):
            path = self.root / "state" / (preview["preview_id"] + ".json")
            record = json.loads(path.read_text())
            del record["immutable"]["review"]["reply"]["account"]
            record["preview_hash"] = digest(record["immutable"])
            self.json(path, record)
            preview["preview_hash"] = record["preview_hash"]

        prepared = self.preview(to=None, subject=None, reply=self.source())
        legacy_record(prepared)
        self.assertEqual(self.apply(prepared)["status"], "verified")
        uncertain = self.preview(to=None, subject=None, reply=self.source())
        self.server.timeout_before_accept = True
        self.assertEqual(self.apply(uncertain)["status"], "uncertain")
        legacy_record(uncertain)
        raw = (self.root / "state" / (uncertain["preview_id"] + ".eml")).read_bytes()
        self.server.messages["Drafts"]["102"] = raw
        self.assertEqual(self.apply(uncertain)["status"], "verified")
        self.assertEqual(self.server.append_calls, 2)

    def test_wrong_account_entity_port_or_from_is_rejected(self):
        self.error("ACCOUNT_NOT_CONFIGURED", lambda: self.preview(account="other"))
        self.error("FROM_NOT_ALLOWED", lambda: self.preview(from_address="wrong@example.test"))
        preview = self.preview()
        self.profile["entity"] = "different"
        self.json(self.profile_path, self.profile)
        self.error("ACCOUNT_IDENTITY_MISMATCH", lambda: self.apply(preview))
        self.profile["entity"] = "synthetic"
        self.profile["port"] = 1993
        self.json(self.profile_path, self.profile)
        self.error("ACCOUNT_IDENTITY_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_configuration_drift_or_preview_hash_mismatch_blocks_apply(self):
        preview = self.preview()
        self.error("PREVIEW_HASH_MISMATCH", lambda: self.service.apply(preview["preview_id"], "a" * 64))
        self.entry["drafts_mailbox"] = "Drafts"
        self.json(self.config_path, self.config)
        self.error("ACCOUNT_CONFIGURATION_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.append_calls, 0)

    def test_unsigned_draft_requires_explicit_no_signature(self):
        del self.entry["signature"]
        self.json(self.config_path, self.config)
        self.error("SIGNATURE_CONFIGURATION_REQUIRED", self.preview)

    def test_read_only_broker_cannot_be_bypassed_by_draft_writer(self):
        with patch.dict(os.environ, {"MAIL_BROKER_CONFIG": "/synthetic/readonly-broker.json"}):
            self.error("BROKER_DRAFT_ADAPTER_REQUIRED", self.preview)
        self.assertEqual(self.server.append_calls, 0)

    def test_signature_plain_html_cid_and_explicit_attachment(self):
        plain = self.root / "signature.txt"
        plain.write_text("Owner Name\nExample company", encoding="utf-8")
        rich = self.root / "signature.html"
        rich.write_text('<b>Owner Name</b><img src="cid:logo@example.test">', encoding="utf-8")
        logo = self.root / "logo.png"
        logo.write_bytes(b"synthetic image bytes")
        attachment = self.allowed / "quote.txt"
        attachment.write_text("Explicit quoted file", encoding="utf-8")
        self.entry["signature"] = {"plain": str(plain), "html": str(rich), "inline": {"logo@example.test": str(logo)}}
        self.json(self.config_path, self.config)
        source = self.source(body="Previous signed history\nOwner Name\nExample company")
        preview = self.preview(to=None, subject=None, reply=source, body_text="Authored reply\n\nOwner Name\nExample company", attachments=[str(attachment)])
        message = self.mime(preview)
        plain_body = message.get_body(preferencelist=("plain",)).get_content()
        authored, quoted = plain_body.split("> --- Original message ---", 1)
        self.assertEqual(authored.count("Owner Name"), 1)
        self.assertIn("Owner Name", quoted)
        html_body = message.get_body(preferencelist=("html",)).get_content()
        self.assertEqual(html_body.count('class="imap-mail-signature"'), 1)
        self.assertIn("cid:logo@example.test", html_body)
        self.assertTrue(any(str(part.get("Content-ID")) == "<logo@example.test>" for part in message.walk()))
        self.assertEqual([part.get_filename() for part in message.iter_attachments()], ["quote.txt"])
        self.assertEqual(self.apply(preview)["status"], "verified")

    def test_attachment_traversal_and_symlink_escape_are_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("private")
        self.error("ATTACHMENT_OUTSIDE_ALLOWED_ROOTS", lambda: self.preview(attachments=[str(self.allowed / ".." / "outside.txt")]))
        link = self.allowed / "file.txt"
        link.symlink_to(outside)
        self.error("ATTACHMENT_OUTSIDE_ALLOWED_ROOTS", lambda: self.preview(attachments=[str(link)]))

    def test_non_ascii_text_attachment_without_charset_is_compared_as_file_bytes(self):
        attachment = self.allowed / "polish.txt"
        content = "Zażółć gęślą jaźń\r\nDokładne bajty pliku.\n".encode("utf-8")
        attachment.write_bytes(content)
        preview = self.preview(attachments=[str(attachment)])
        message = self.mime(preview)
        part = list(message.iter_attachments())[0]
        self.assertEqual(part.get_content_type(), "text/plain")
        self.assertIsNone(part.get_content_charset())
        self.assertEqual(part.get_payload(decode=True), content)
        self.assertEqual(self.apply(preview)["status"], "verified")
        raw = (self.root / "state" / (preview["preview_id"] + ".eml")).read_bytes()
        changed = email.message_from_bytes(raw, policy=policy.SMTP)
        changed_part = list(changed.iter_attachments())[0]
        changed_part.set_payload(b"changed attachment")
        changed_part.replace_header("Content-Transfer-Encoding", "7bit")
        self.assertNotEqual(semantic(raw), semantic(changed.as_bytes()))

    def test_headers_reject_injection_and_ambiguous_drafts_folder(self):
        self.error("INVALID_HEADER", lambda: self.preview(subject="Hi\r\nBcc: secret@example.test"))
        self.error("INVALID_HEADER", lambda: self.preview(to=["safe@example.test\nBcc: secret@example.test"]))
        self.server.listing.append({"name": "Other drafts", "kind": "drafts", "selectable": True})
        self.error("DRAFTS_FOLDER_AMBIGUOUS", self.preview)
        self.assertEqual(self.server.append_calls, 0)

    def test_semantic_comparison_accepts_line_endings_but_rejects_body_change(self):
        preview = self.preview()
        raw = (self.root / "state" / (preview["preview_id"] + ".eml")).read_bytes()
        self.assertEqual(semantic(raw), semantic(raw.replace(b"\r\n", b"\n")))
        self.assertNotEqual(semantic(raw), semantic(raw.replace(b"Authored reply", b"Different body")))

    def test_only_one_exact_leading_utf8_html_charset_meta_is_ignored(self):
        preview = self.preview()
        raw = (self.root / "state" / (preview["preview_id"] + ".eml")).read_bytes()
        meta = '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'

        def alter(prefix="", old=None, new=None):
            message = email.message_from_bytes(raw, policy=policy.SMTP)
            part = message.get_body(preferencelist=("html",))
            body = part.get_content()
            if old is not None:
                body = body.replace(old, new)
            part.set_content(prefix + body, subtype="html", charset="utf-8")
            return message.as_bytes()

        self.assertEqual(semantic(raw), semantic(alter(prefix=meta)))
        self.assertNotEqual(semantic(raw), semantic(alter(prefix=meta + meta)))
        self.assertNotEqual(semantic(raw), semantic(alter(prefix="\n" + meta)))
        self.assertNotEqual(semantic(raw), semantic(alter(prefix=meta, old="Authored reply", new="Changed reply")))
        self.assertNotEqual(semantic(raw), semantic(alter(prefix=meta, old="Authored reply", new='<a href="https://changed.example.test/">Authored reply</a>')))
        refresh = '<meta http-equiv="refresh" content="0;url=https://changed.example.test/">'
        self.assertNotEqual(semantic(raw), semantic(alter(prefix=refresh)))
        self.assertNotEqual(semantic(raw), semantic(alter(prefix=meta + refresh)))


if __name__ == "__main__":
    unittest.main()
