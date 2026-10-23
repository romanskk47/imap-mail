"""Synthetic MIME interpretation and pre-upgrade journal regressions."""

import email
import copy
import json
from email import policy
from email.message import EmailMessage
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError, digest, semantic, write_json
import test_edit as fixture_module
import test_core as draft_fixtures
import test_send as send_fixtures


class MimeMeaningTests(unittest.TestCase):
    def test_interpretation_parameters_are_not_discarded(self):
        for name, first, second in (("start", "<first>", "<second>"),
                                    ("type", "text/plain", "text/html"),
                                    ("start-info", "first", "second"),
                                    ("protocol", "application/first", "application/second"),
                                    ("format", "fixed", "flowed"),
                                    ("delsp", "no", "yes"),
                                    ("x-display-mode", "one", "two")):
            with self.subTest(parameter=name):
                message = EmailMessage(policy=policy.SMTP)
                message.set_content("First text\nSecond text")
                message.set_param(name, first)
                original = message.as_bytes()
                message.set_param(name, second)
                self.assertNotEqual(semantic(original), semantic(message.as_bytes()))

    def test_boundary_parameter_order_and_equivalent_charset_preserve_meaning(self):
        message = EmailMessage(policy=policy.SMTP)
        message.set_content("Equal ASCII text", charset="utf-8")
        utf8 = message.as_bytes()
        message.set_content("Equal ASCII text", charset="iso-8859-1")
        self.assertEqual(semantic(utf8), semantic(message.as_bytes()))
        message.make_related()
        message.set_param("start", "<first>")
        message.set_param("type", "text/plain")
        message.get_payload()[0]["Content-ID"] = "<first>"
        original = message.as_bytes()
        message.set_boundary("different-valid-boundary")
        self.assertEqual(semantic(original), semantic(message.as_bytes()))
        header = str(message["Content-Type"])
        message.replace_header("Content-Type", 'multipart/related; type="text/plain"; start="<first>"; boundary="different-valid-boundary"')
        self.assertNotEqual(header, str(message["Content-Type"]))
        self.assertEqual(semantic(original), semantic(message.as_bytes()))

    def test_duplicate_parameters_and_malformed_mime_are_not_verifiable(self):
        for raw in (
            b'',
            b'Content-Type: text/plain; charset=utf-8; format=fixed; format=flowed\r\n\r\nText\r\n',
            b'Content-Type: multipart/mixed; boundary=missing\r\n\r\nNot a multipart\r\n',
            b'Content-Type: text/plain\r\nContent-Transfer-Encoding: x-unknown\r\n\r\nText\r\n',
            b'Content-Type: text/plain; charset=x-unknown\r\n\r\nText\r\n',
        ):
            with self.subTest(raw=raw), self.assertRaises(DraftError):
                semantic(raw)


class DraftMeaningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.EditTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def related_preview(self):
        fixture = self.fixture
        message = fixture.message("Existing body primary", attachment=False)
        message.make_related()
        message.get_payload()[0]["Content-ID"] = "<primary@example.test>"
        message.add_related("Existing body alternate", subtype="plain", cid="<alternate@example.test>")
        message.set_param("start", "<primary@example.test>")
        message.set_param("type", "text/plain")
        draft = fixture.draft(message.as_bytes())
        original = fixture.server.messages["Drafts"][draft["uid"]]
        return draft, original, fixture.preview(draft)

    def switch_stored_root(self, uid):
        fixture = self.fixture
        message = email.message_from_bytes(fixture.server.messages["Drafts"][uid], policy=policy.default)
        message.set_param("start", "<alternate@example.test>")
        fixture.server.messages["Drafts"][uid] = message.as_bytes(policy=policy.SMTP)

    def test_changed_related_root_never_retires_original(self):
        fixture = self.fixture
        draft, original, preview = self.related_preview()
        before = fixture.record(preview)
        fixture.server.after_append = self.switch_stored_root
        result = fixture.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["error_code"], "DRAFT_EDIT_REPLACEMENT_CHANGED")
        self.assertEqual(fixture.server.messages["Drafts"][draft["uid"]], original)
        self.assertEqual(fixture.server.store_calls, 0)
        self.assertEqual(fixture.server.expunge_calls, 0)
        self.assertEqual(fixture.record(preview)["immutable"], before["immutable"])
        self.assertEqual(fixture.record(preview)["preview_hash"], before["preview_hash"])
        self.assertEqual(fixture.apply(preview)["status"], "conflict")
        self.assertEqual(fixture.server.append_calls, 1)

    def test_changed_type_format_and_delsp_conflict_before_retirement(self):
        for name, before, after in (("type", "text/plain", "text/html"),
                                    ("format", "fixed", "flowed"), ("delsp", "no", "yes")):
            with self.subTest(parameter=name):
                fixture = fixture_module.EditTests(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                message = fixture.message(attachment=False)
                if name == "type":
                    message.make_related()
                message.set_param(name, before)
                draft = fixture.draft(message.as_bytes())
                original = fixture.server.messages["Drafts"][draft["uid"]]
                preview = fixture.preview(draft)

                def change_parameter(uid):
                    stored = email.message_from_bytes(fixture.server.messages["Drafts"][uid], policy=policy.default)
                    stored.set_param(name, after)
                    fixture.server.messages["Drafts"][uid] = stored.as_bytes(policy=policy.SMTP)

                fixture.server.after_append = change_parameter
                self.assertEqual(fixture.apply(preview)["status"], "conflict")
                self.assertEqual(fixture.server.messages["Drafts"][draft["uid"]], original)
                self.assertEqual(fixture.server.store_calls, 0)
                self.assertEqual(fixture.server.expunge_calls, 0)

    def test_completed_legacy_edit_reports_new_conflict_without_restoring_or_retiring_again(self):
        fixture = self.fixture
        draft, _, preview = self.related_preview()
        result = fixture.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertNotIn(draft["uid"], fixture.server.messages["Drafts"])
        record = fixture.record(preview)
        self.switch_stored_root(result["receipt"]["uid"])
        messages = copy.deepcopy(fixture.server.messages)
        writes = (fixture.server.append_calls, fixture.server.store_calls, fixture.server.expunge_calls)
        self.assertEqual(fixture.status(preview)["status"], "conflict")
        self.assertEqual(fixture.apply(preview)["status"], "conflict")
        self.assertEqual(fixture.server.messages, messages)
        self.assertEqual((fixture.server.append_calls, fixture.server.store_calls, fixture.server.expunge_calls), writes)
        self.assertEqual(fixture.record(preview)["immutable"], record["immutable"])

    def test_legacy_cleanup_receipt_does_not_authorize_changed_root_retirement(self):
        for state in ("append_attempted", "uncertain", "cleanup_pending", "verified"):
            with self.subTest(state=state):
                self.fixture = fixture_module.EditTests(methodName="runTest")
                self.fixture.setUp()
                self.addCleanup(self.fixture.doCleanups)
                fixture = self.fixture
                draft, original, preview = self.related_preview()
                fixture.server.store_timeout_before = True
                fixture.apply(preview)
                fixture.server.store_timeout_before = False
                record = fixture.record(preview)
                immutable, preview_hash = record["immutable"], record["preview_hash"]
                replacement = record["receipt"]["uid"]
                record["state"] = state
                record["receipt"]["semantic_sha256"] = "0" * 64
                path = fixture.root / "state" / (preview["preview_id"] + ".json")
                write_json(path, record)
                self.switch_stored_root(replacement)
                writes = (fixture.server.append_calls, fixture.server.store_calls, fixture.server.expunge_calls)
                self.assertEqual(fixture.status(preview)["status"], "conflict")
                self.assertEqual(fixture.apply(preview)["status"], "conflict")
                self.assertEqual((fixture.server.append_calls, fixture.server.store_calls, fixture.server.expunge_calls), writes)
                self.assertEqual(fixture.server.messages["Drafts"][draft["uid"]], original)
                self.assertEqual(fixture.record(preview)["immutable"], immutable)
                self.assertEqual(fixture.record(preview)["preview_hash"], preview_hash)


class LegacyMimeTests(unittest.TestCase):
    def fixture(self, kind):
        fixture_class = {"draft": draft_fixtures.DraftServiceTests, "edit": fixture_module.EditTests,
                         "send": send_fixtures.SendTests}[kind]
        fixture = fixture_class(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def legacy_snapshot(self, fixture):
        preview = fixture.preview()
        path = fixture.root / "state" / (preview["preview_id"] + ".json")
        raw_path = path.with_suffix(".eml")
        raw = raw_path.read_bytes().replace(b"Content-Type: text/plain; charset=\"utf-8\"",
            b'Content-Type: text/plain; charset="utf-8"; format=fixed; format=flowed', 1)
        self.assertIn(b"format=fixed; format=flowed", raw)
        record = json.loads(path.read_text())
        record["immutable"]["mime_sha256"] = digest(raw)
        record["preview_hash"] = digest(record["immutable"])
        raw_path.write_bytes(raw)
        write_json(path, record)
        preview["preview_hash"] = record["preview_hash"]
        return preview, path, copy.deepcopy(record), raw

    def test_prepared_legacy_draft_edit_and_send_reject_ambiguous_mime_before_first_write(self):
        for kind in ("draft", "edit", "send"):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind)
                preview, path, record, raw = self.legacy_snapshot(fixture)
                for _ in range(2):
                    with self.assertRaises(DraftError) as caught:
                        fixture.apply(preview)
                    self.assertEqual(caught.exception.code, "MALFORMED_MESSAGE_MIME")
                self.assertEqual(json.loads(path.read_text()), record)
                self.assertEqual(path.with_suffix(".eml").read_bytes(), raw)
                self.assertEqual(fixture.server.append_calls, 0)
                if kind == "edit":
                    self.assertEqual(fixture.server.store_calls, 0)
                    self.assertEqual(fixture.server.expunge_calls, 0)
                    self.assertFalse(record["append_attempted"])
                if kind == "send":
                    self.assertEqual(fixture.server.smtp_commands, [])
                    self.assertEqual(fixture.server.smtp_connections, 0)
                    self.assertFalse(record["smtp_attempted"])

    def test_attempted_legacy_draft_and_send_never_return_to_prepared(self):
        for kind, states in (("draft", ("attempted", "uncertain", "verified")),
                             ("send", ("attempted", "uncertain", "submission_accepted"))):
            for state in states:
                with self.subTest(kind=kind, state=state):
                    fixture = self.fixture(kind)
                    preview, path, record, raw = self.legacy_snapshot(fixture)
                    record["state"] = state
                    record["attempted_at"] = "2026-10-20T10:00:00+00:00"
                    if kind == "send":
                        record["smtp_attempted"] = True
                    write_json(path, record)
                    for _ in range(2):
                        result = fixture.apply(preview)
                        self.assertNotEqual(result["status"], "prepared")
                    observed = json.loads(path.read_text())
                    self.assertEqual(observed["immutable"], record["immutable"])
                    self.assertEqual(observed["preview_hash"], record["preview_hash"])
                    self.assertEqual(observed["attempted_at"], record["attempted_at"])
                    self.assertEqual(path.with_suffix(".eml").read_bytes(), raw)
                    self.assertEqual(fixture.server.append_calls, 0)
                    if kind == "send":
                        self.assertTrue(observed["smtp_attempted"])
                        self.assertEqual(fixture.server.smtp_commands, [])


if __name__ == "__main__":
    unittest.main()
