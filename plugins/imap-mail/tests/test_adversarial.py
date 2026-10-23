"""Independent boundary tests. All mail, accounts and credentials here are fake."""
import email
import email.policy
import copy
from email.message import EmailMessage
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from imap_mail.core import DraftError, DraftService, semantic


class FakeMailboxServer:
    def __init__(self):
        self.validity = {"INBOX": "17", "Drafts": "23"}
        self.messages = {"INBOX": {}, "Drafts": {}}
        self.append_count = 0
        self.connection_count = 0
        self.fetch_count = 0
        self.lose_append_reply = False
        self.fail_append_before_accept = False
        self.append_uidvalidity_override = None
        self.draft_marked_deleted = False
        self.timeout_at_append = None
        self.next_uid = 101

    def reader(self, profile):
        self.connection_count += 1
        return FakeReader(self, profile)


class FakeReader:
    def __init__(self, server, profile):
        self.server = server
        self.profile = profile
        self.client = self
        self.sock = FakeSocket()
        self.mailbox = None
        self.append_uid = None

    @staticmethod
    def _quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def unquote(value):
        if isinstance(value, bytes):
            value = value.decode()
        return re.sub(r"\\(.)", r"\1", value[1:-1]) if value.startswith('"') else value

    def select(self, mailbox):
        self.mailbox = mailbox
        return self.server.validity[mailbox], len(self.server.messages[mailbox])

    def folders(self):
        return {"folders": [{"name": "Drafts", "kind": "drafts", "selectable": True}]}

    def close(self):
        pass

    def response(self, key):
        return key, [self.append_uid] if self.append_uid else [None]

    def uid(self, command, *args):
        if command.upper() == "SEARCH":
            query = self.unquote(args[-1])
            found = []
            for uid, raw in self.server.messages[self.mailbox].items():
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                if query in str(msg.get("Message-ID", "")):
                    found.append(str(uid))
            return "OK", [" ".join(found).encode()]
        if command.upper() == "FETCH":
            self.server.fetch_count += 1
            uid = int(args[0])
            raw = self.server.messages[self.mailbox].get(uid)
            if raw is None:
                return "OK", []
            flags = b"\\Draft" if self.mailbox == "Drafts" else b"\\Seen"
            if self.mailbox == "Drafts" and self.server.draft_marked_deleted:
                flags += b" \\Deleted"
            size = len(raw)
            partial = re.search(r"<(\d+)\.(\d+)>", str(args[-1]))
            if partial:
                offset, maximum = map(int, partial.groups())
                raw = raw[offset:offset + maximum]
            body_offset = f"<{partial[1]}>" if partial else ""
            prefix = f"1 (UID {uid} RFC822.SIZE {size} FLAGS (".encode() + flags + f") BODY[]{body_offset} {{{len(raw)}}}".encode()
            return "OK", [(prefix, raw), b")"]
        raise AssertionError(f"Unexpected mutating or unbounded command: {command}")

    def append(self, mailbox, flags, date_time, raw):
        mailbox = self.unquote(mailbox)
        if mailbox != "Drafts" or "\\Draft" not in flags:
            raise AssertionError("APPEND escaped the verified Drafts mailbox")
        self.server.append_count += 1
        self.server.timeout_at_append = self.sock.timeout
        if self.server.fail_append_before_accept:
            self.server.fail_append_before_accept = False
            raise TimeoutError("PRIVATE-TEST-SENTINEL: server detail must not be persisted")
        uid = self.server.next_uid
        self.server.next_uid += 1
        self.server.messages[mailbox][uid] = raw
        self.append_uid = f"{self.server.append_uidvalidity_override or self.server.validity[mailbox]} {uid}".encode()
        if self.server.lose_append_reply:
            self.server.lose_append_reply = False
            raise TimeoutError("Connection broke after the server accepted the message")
        return "OK", [b"[APPENDUID " + self.append_uid + b"] Saved"]


class FakeSocket:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value


class DraftFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile_dir = self.root / "profiles"
        self.profile_dir.mkdir()
        self.profile_path = self.profile_dir / "example.json"
        self.profile = {"id": "example", "host": "imap.example.test", "user": "owner@example.test", "entity": "example", "password": "fake-only"}
        self.write_json(self.profile_path, self.profile)
        self.attachments = self.root / "allowed"
        self.attachments.mkdir()
        self.config_path = self.root / "settings.json"
        self.config = {"version": 1, "profiles_dir": str(self.profile_dir), "state_dir": str(self.root / "state"), "accounts": {"example": {"identity": {"email": "owner@example.test", "host": "imap.example.test", "entity": "example"}, "from_addresses": ["owner@example.test"], "default_from": "owner@example.test", "drafts_enabled": True, "signature": {"none": True}, "attachment_roots": [str(self.attachments)]}}}
        self.write_json(self.config_path, self.config)
        self.server = FakeMailboxServer()
        self.service = self.make_service()

    @staticmethod
    def write_json(path, data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def make_service(self):
        return DraftService(config_path=str(self.config_path), reader_factory=self.server.reader)

    def preview(self, **kwargs):
        return self.service.preview(account="example", body_text="Please review this test draft.", to=["recipient@example.test"], subject="Independent draft test", **kwargs)

    @staticmethod
    def apply(service, preview):
        return service.apply(preview["preview_id"], preview["preview_hash"])


class AdversarialDraftTests(DraftFixture):
    def test_header_injection_is_rejected_before_append(self):
        with self.assertRaises(Exception):
            self.service.preview(account="example", body_text="Body", to=["recipient@example.test\r\nBcc: leak@example.test"], subject="Test")
        self.assertEqual(self.server.append_count, 0)

    def test_attachment_symlink_cannot_escape_allowed_root(self):
        private = self.root / "not-allowed.txt"
        private.write_text("Do not attach")
        link = self.attachments / "looks-safe.txt"
        link.symlink_to(private)
        with self.assertRaises(Exception):
            self.preview(attachments=[str(link)])
        self.assertEqual(self.server.append_count, 0)

    def test_identity_reassignment_invalidates_preview(self):
        preview = self.preview()
        self.profile["host"] = "other-entity.example.test"
        self.write_json(self.profile_path, self.profile)
        with self.assertRaises(Exception):
            self.apply(self.make_service(), preview)
        self.assertEqual(self.server.append_count, 0)

    def test_lost_append_response_never_replays_append(self):
        preview = self.preview()
        self.server.lose_append_reply = True
        try:
            self.apply(self.service, preview)
        except Exception:
            pass
        self.assertEqual(self.server.append_count, 1)
        for _ in range(2):
            try:
                self.apply(self.make_service(), preview)
            except Exception:
                pass
            self.service.status(preview["preview_id"])
        self.assertEqual(self.server.append_count, 1)

    def test_unresolved_append_attempt_never_replays_append(self):
        preview = self.preview()
        self.server.fail_append_before_accept = True
        initial = self.apply(self.service, preview)
        self.assertEqual(initial["status"], "uncertain")
        for _ in range(2):
            self.assertEqual(self.apply(self.make_service(), preview)["status"], "uncertain")
            self.assertEqual(self.service.status(preview["preview_id"])["status"], "uncertain")
        self.assertEqual(self.server.messages["Drafts"], {})
        self.assertEqual(self.server.append_count, 1)

    def test_append_timeout_is_bounded_and_error_metadata_has_no_exception_text(self):
        preview = self.preview()
        self.server.fail_append_before_accept = True
        result = self.apply(self.service, preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.server.timeout_at_append, 60)
        record_text = (self.root / "state" / (preview["preview_id"] + ".json")).read_text()
        record = json.loads(record_text)
        self.assertEqual(record["append_error_type"], "TimeoutError")
        self.assertEqual(record["append_error_code"], "APPEND_RESPONSE_UNCERTAIN")
        self.assertNotIn("PRIVATE-TEST-SENTINEL", record_text)
        self.assertNotIn("PRIVATE-TEST-SENTINEL", json.dumps(result))
        self.assertNotIn("append_error_type", result)
        self.assertEqual(self.apply(self.make_service(), preview)["status"], "uncertain")
        self.assertEqual(self.service.status(preview["preview_id"])["status"], "uncertain")
        self.assertEqual(self.server.append_count, 1)

    def test_preview_mime_tamper_is_rejected(self):
        preview = self.preview()
        candidates = list((self.root / "state").rglob("*.eml"))
        self.assertEqual(len(candidates), 1)
        candidates[0].write_bytes(candidates[0].read_bytes() + b"\r\nInjected modification\r\n")
        with self.assertRaises(Exception):
            self.apply(self.service, preview)
        self.assertEqual(self.server.append_count, 0)

    def add_source(self):
        msg = EmailMessage(policy=email.policy.SMTP)
        msg["From"] = "Customer <customer@example.test>"
        msg["To"] = "owner@example.test"
        msg["Subject"] = "Existing conversation"
        msg["Date"] = "Wed, 21 Oct 2026 09:00:00 +0000"
        msg["Message-ID"] = "<source@example.test>"
        msg.set_content("Existing conversation body.")
        self.server.messages["INBOX"][7] = msg.as_bytes()
        return {"mailbox": "INBOX", "uidvalidity": "17", "uid": "7"}

    def test_changed_source_uidvalidity_blocks_append(self):
        source = self.add_source()
        preview = self.service.preview(account="example", body_text="A reply.", reply=source)
        self.server.validity["INBOX"] = "18"
        with self.assertRaises(Exception):
            self.apply(self.service, preview)
        self.assertEqual(self.server.append_count, 0)

    def test_replaced_source_content_blocks_append(self):
        source = self.add_source()
        preview = self.service.preview(account="example", body_text="A reply.", reply=source)
        self.server.messages["INBOX"][7] = self.server.messages["INBOX"][7].replace(b"Existing conversation body.", b"A different message body.")
        with self.assertRaises(Exception):
            self.apply(self.service, preview)
        self.assertEqual(self.server.append_count, 0)

    def test_human_edit_invalidates_previously_verified_draft(self):
        preview = self.preview()
        saved = self.apply(self.service, preview)
        self.assertEqual(saved["status"], "verified")
        uid = next(iter(self.server.messages["Drafts"]))
        self.server.messages["Drafts"][uid] = self.server.messages["Drafts"][uid].replace(
            b"Please review this test draft.", b"A human edited this draft."
        )
        self.assertNotEqual(self.service.status(preview["preview_id"])["status"], "verified")
        self.assertNotEqual(self.apply(self.service, preview)["status"], "verified")
        self.assertEqual(self.server.append_count, 1)

    def test_human_deletion_invalidates_previously_verified_draft(self):
        preview = self.preview()
        saved = self.apply(self.service, preview)
        self.assertEqual(saved["status"], "verified")
        self.server.messages["Drafts"].clear()
        self.assertNotEqual(self.service.status(preview["preview_id"])["status"], "verified")
        self.assertNotEqual(self.apply(self.service, preview)["status"], "verified")
        self.assertEqual(self.server.append_count, 1)

    def test_human_delete_flag_invalidates_previously_verified_draft(self):
        preview = self.preview()
        self.assertEqual(self.apply(self.service, preview)["status"], "verified")
        self.server.draft_marked_deleted = True
        self.assertNotEqual(self.service.status(preview["preview_id"])["status"], "verified")
        self.assertEqual(self.server.append_count, 1)

    def test_appenduid_other_uidvalidity_is_not_verified(self):
        preview = self.preview()
        self.server.append_uidvalidity_override = "999"
        self.assertNotEqual(self.apply(self.service, preview)["status"], "verified")
        self.assertNotEqual(self.service.status(preview["preview_id"])["status"], "verified")
        self.assertNotEqual(self.apply(self.service, preview)["status"], "verified")
        self.assertEqual(self.server.append_count, 1)

    def test_recovered_readback_clears_historical_error(self):
        preview = self.preview()
        self.assertEqual(self.apply(self.service, preview)["status"], "verified")
        saved = dict(self.server.messages["Drafts"])
        self.server.messages["Drafts"].clear()
        self.assertEqual(self.service.status(preview["preview_id"])["status"], "uncertain")
        self.server.messages["Drafts"].update(saved)
        recovered = self.service.status(preview["preview_id"])
        self.assertEqual(recovered["status"], "verified")
        self.assertNotIn("error_code", recovered)
        self.assertEqual(self.server.append_count, 1)


class MultiAccountBoundaryTests(DraftFixture):
    def add_second(self, configured=True):
        profile = {"id": "second", "host": "other-imap.example.test", "user": "other@example.test", "entity": "other", "password": "fake-only"}
        self.write_json(self.profile_dir / "second.json", profile)
        if configured:
            entry = copy.deepcopy(self.config["accounts"]["example"])
            entry.update(identity={"email": profile["user"], "host": profile["host"], "entity": profile["entity"]},
                         from_addresses=[profile["user"]], default_from=profile["user"])
            self.config["accounts"]["second"] = entry
        self.write_json(self.config_path, self.config)
        return profile

    def rows(self):
        return {row["account"]: row for row in self.service.accounts()["accounts"]}

    def test_discovered_profile_does_not_gain_draft_authority(self):
        self.add_second(configured=False)
        rows = self.rows()
        self.assertEqual(set(rows), {"example", "second"})
        self.assertTrue(rows["example"]["drafts_ready"])
        self.assertFalse(rows["second"]["configured"])
        self.assertFalse(rows["second"]["drafts_enabled"])
        self.assertFalse(rows["second"]["drafts_ready"])
        self.assertEqual(rows["second"]["connection"], "not_checked")
        with self.assertRaises(DraftError) as check_error:
            self.service.check("second")
        self.assertEqual(check_error.exception.code, "ACCOUNT_NOT_CONFIGURED")
        with self.assertRaises(DraftError) as preview_error:
            self.service.preview(account="second", body_text="A body.", to=["recipient@example.test"], subject="Test")
        self.assertEqual(preview_error.exception.code, "ACCOUNT_NOT_CONFIGURED")
        self.assertEqual(self.server.connection_count, 0)
        self.assertEqual(self.server.append_count, 0)

    def test_one_bad_account_configuration_does_not_hide_ready_account(self):
        self.add_second()
        for invalid in ([], {"identity": []}, {"identity": {"email": "other@example.test", "host": "wrong.example.test", "entity": "other"}}):
            with self.subTest(invalid=invalid):
                self.config["accounts"]["second"] = invalid
                self.write_json(self.config_path, self.config)
                rows = self.rows()
                self.assertEqual(set(rows), {"example", "second"})
                self.assertEqual(rows["example"]["status"], "ready")
                self.assertTrue(rows["example"]["drafts_ready"])
                self.assertEqual(rows["second"]["status"], "needs_attention")
                self.assertFalse(rows["second"]["drafts_ready"])
                self.assertTrue(rows["second"]["issues"])
        self.assertEqual(self.server.connection_count, 0)

    def test_malformed_host_profile_is_a_visible_safe_discovery_failure(self):
        broken = self.profile_dir / "malformed.json"
        broken.write_text('{"PRIVATE-TEST-SENTINEL": invalid json')
        broken.chmod(0o600)
        discovery = self.service.accounts()
        self.assertEqual(discovery["status"], "needs_attention")
        self.assertEqual(discovery["profile_discovery_issues"], ["INVALID_PROFILE_JSON"])
        self.assertTrue(all(not row["read_ready"] for row in discovery["accounts"]))
        self.assertNotIn("PRIVATE-TEST-SENTINEL", json.dumps(discovery))
        checked = self.service.check("example")
        self.assertEqual(checked["status"], "needs_attention")
        self.assertEqual(checked["error_code"], "PROFILE_DISCOVERY_INCOMPLETE")
        self.assertNotIn("PRIVATE-TEST-SENTINEL", json.dumps(checked))
        self.assertFalse(checked["connection"])
        self.assertEqual(checked["mailbox_writes"], 0)
        self.assertEqual(self.server.connection_count, 0)
        self.assertEqual(self.server.append_count, 0)

    def test_check_connects_only_selected_account_and_never_appends(self):
        self.add_second()
        second_server = FakeMailboxServer()
        connections = []

        def route(profile):
            connections.append(profile["id"])
            return {"example": self.server, "second": second_server}[profile["id"]].reader(profile)

        service = DraftService(config_path=str(self.config_path), reader_factory=route)
        checked = service.check("second")
        self.assertEqual(checked["status"], "ready")
        self.assertTrue(checked["connection"])
        self.assertEqual(checked["identity"]["email"], "other@example.test")
        self.assertEqual(checked["mailbox_writes"], 0)
        self.assertEqual(connections, ["second"])
        self.assertEqual(self.server.append_count + second_server.append_count, 0)
        self.assertFalse((self.root / "state").exists())

    def test_failed_account_check_never_falls_back_to_another_account(self):
        self.add_second()
        connections = []

        def fail_selected(profile):
            connections.append(profile["id"])
            if profile["id"] == "second":
                raise TimeoutError("PRIVATE-TEST-SENTINEL: second account unavailable")
            return self.server.reader(profile)

        service = DraftService(config_path=str(self.config_path), reader_factory=fail_selected)
        checked = service.check("second")
        self.assertEqual(checked["status"], "needs_attention")
        self.assertFalse(checked["connection"])
        self.assertEqual(checked["mailbox_writes"], 0)
        self.assertEqual(connections, ["second"])
        self.assertNotIn("PRIVATE-TEST-SENTINEL", json.dumps(checked))
        self.assertEqual(self.server.append_count, 0)

    def test_no_signature_is_allowed_only_when_explicitly_configured(self):
        self.add_second()
        for value in (None, {}):
            with self.subTest(signature=value):
                self.config["accounts"]["second"]["signature"] = value
                self.write_json(self.config_path, self.config)
                self.assertFalse(self.rows()["second"]["drafts_ready"])
                checked = self.service.check("second")
                self.assertEqual(checked["status"], "needs_attention")
        self.config["accounts"]["second"]["signature"] = {"none": True}
        self.write_json(self.config_path, self.config)
        self.assertTrue(self.rows()["second"]["drafts_ready"])
        self.assertEqual(self.service.check("second")["signature_mode"], "none")
        self.assertEqual(self.server.append_count, 0)

    def test_preferred_transport_does_not_select_or_authorize_an_account(self):
        self.add_second(configured=False)
        self.config["preferred_transport"] = "imap"
        self.write_json(self.config_path, self.config)
        discovered = self.service.accounts()
        self.assertEqual(discovered["preferred_transport"], "imap")
        self.assertFalse(next(row for row in discovered["accounts"] if row["account"] == "second")["drafts_ready"])
        with self.assertRaises(DraftError):
            self.service.check("missing-account")
        self.assertEqual(self.server.connection_count, 0)

    def test_reply_account_prevents_same_uid_tuple_from_crossing_accounts(self):
        self.add_second()
        second_server = FakeMailboxServer()
        for server, suffix in ((self.server, "first"), (second_server, "second")):
            message = EmailMessage(policy=email.policy.SMTP)
            message["From"] = f"{suffix}-customer@example.test"
            message["To"] = "owner@example.test" if suffix == "first" else "other@example.test"
            message["Subject"] = f"{suffix} account conversation"
            message["Message-ID"] = f"<{suffix}-source@example.test>"
            message.set_content(f"The {suffix} account source body.")
            server.messages["INBOX"][7] = message.as_bytes()

        def route(profile):
            return {"example": self.server, "second": second_server}[profile["id"]].reader(profile)

        service = DraftService(config_path=str(self.config_path), reader_factory=route)
        source = {"account": "example", "mailbox": "INBOX", "uidvalidity": "17", "uid": "7"}
        with self.assertRaises(DraftError):
            service.preview(account="second", body_text="Reply.", reply=source)
        self.assertEqual(self.server.fetch_count + second_server.fetch_count, 0)
        source["account"] = "second"
        preview = service.preview(account="second", body_text="Reply.", reply=source)
        self.assertEqual(preview["account"], "second")
        self.assertEqual(preview["from"], "other@example.test")
        self.assertEqual(preview["reply"]["account"], "second")
        self.assertEqual(preview["reply"]["message_id"], "<second-source@example.test>")
        self.assertEqual(self.server.fetch_count, 0)
        self.assertEqual(second_server.fetch_count, 1)
        self.assertEqual(self.server.append_count + second_server.append_count, 0)


class HtmlNormalizationTests(unittest.TestCase):
    META = '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'

    def setUp(self):
        self.message = EmailMessage(policy=email.policy.SMTP)
        self.message["From"] = "owner@example.test"
        self.message["To"] = "recipient@example.test"
        self.message["Subject"] = "HTML compatibility test"
        self.message["Date"] = "Wed, 21 Oct 2026 09:00:00 +0000"
        self.message["Message-ID"] = "<html@example.test>"
        self.message.set_content("A plain body.")
        self.message.add_alternative('<div>A body. <a href="https://example.test/a">Link</a></div>', subtype="html")
        self.raw = self.message.as_bytes()

    def with_html(self, transform):
        changed = email.message_from_bytes(self.raw, policy=email.policy.default)
        html_part = changed.get_body(preferencelist=("html",))
        html_part.set_content(transform(html_part.get_content()), subtype="html", charset="utf-8")
        return changed.as_bytes()

    def test_exact_leading_utf8_charset_meta_is_harmless(self):
        changed = self.with_html(lambda body: self.META + body)
        self.assertEqual(semantic(self.raw), semantic(changed))

    def test_refresh_meta_is_not_ignored(self):
        changed = self.with_html(lambda body: '<meta http-equiv="refresh" content="0; url=https://other.example.test">' + body)
        self.assertNotEqual(semantic(self.raw), semantic(changed))

    def test_charset_meta_with_extra_attributes_is_not_ignored(self):
        changed = self.with_html(lambda body: self.META[:-1] + ' onclick="doSomething()">' + body)
        self.assertNotEqual(semantic(self.raw), semantic(changed))

    def test_charset_meta_does_not_hide_changed_body_or_link(self):
        for old, new in (("A body.", "An altered body."), ("https://example.test/a", "https://other.example.test/a")):
            with self.subTest(old=old):
                changed = self.with_html(lambda body: self.META + body.replace(old, new))
                self.assertNotEqual(semantic(self.raw), semantic(changed))

    def test_embedded_or_appended_charset_meta_is_not_ignored(self):
        for transform in (lambda body: body + self.META, lambda body: body.replace("<div>", "<div>" + self.META)):
            self.assertNotEqual(semantic(self.raw), semantic(self.with_html(transform)))

    def test_html_attachment_is_compared_as_exact_file_bytes(self):
        self.message.add_attachment(b"<div>Attached HTML file.</div>", maintype="text", subtype="html", filename="attached.html")
        original = self.message.as_bytes()
        changed = email.message_from_bytes(original, policy=email.policy.default)
        attachment = list(changed.iter_attachments())[0]
        attachment.set_payload(self.META.encode() + attachment.get_payload(decode=True))
        if attachment["Content-Transfer-Encoding"]:
            del attachment["Content-Transfer-Encoding"]
        self.assertNotEqual(semantic(original), semantic(changed.as_bytes()))


if __name__ == "__main__":
    unittest.main()
