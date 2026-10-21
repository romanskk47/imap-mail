"""Send-vs-draft routing, consent fields and metadata; no real mail access."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import cli, mcp


class RecordingService:
    config_path = "/private/synthetic-settings.json"

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(**kwargs):
            self.calls.append((name, kwargs))
            return {"status": "prepared", "method": name, "arguments": kwargs}
        return record


class SendInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.service = RecordingService()

    def invoke(self, name, arguments):
        return mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}, service=self.service)["result"]

    def test_send_apply_requires_explicit_send_marker_and_exact_fields(self):
        common = {"preview_id": "a" * 32, "preview_hash": "b" * 64}
        for extra in ({}, {"authorization": "draft"}, {"authorization": True},
                      {"authorization": "send", "password": "synthetic-secret"}):
            with self.subTest(extra=extra):
                response = self.invoke("mail_send_apply", {**common, **extra})
                self.assertTrue(response["isError"])
        self.assertEqual([], self.service.calls)
        response = self.invoke("mail_send_apply", {**common, "authorization": "send"})
        self.assertFalse(response["isError"])
        self.assertEqual("send_apply", self.service.calls[-1][0])

    def test_existing_draft_reference_is_account_bound_and_exclusive(self):
        draft = {"account": "one", "mailbox": "Drafts", "uidvalidity": "42", "uid": "123"}
        response = self.invoke("mail_send_preview", {"account": "one", "draft": draft})
        self.assertFalse(response["isError"])
        self.service.calls.clear()
        for values in ({"account": "two", "draft": draft},
                       {"account": "one", "draft": draft, "body_text": "replacement"},
                       {"account": "one", "draft": draft, "to": ["other@example.test"]},
                       {"account": "one"}):
            with self.subTest(values=values):
                self.assertTrue(self.invoke("mail_send_preview", values)["isError"])
        self.assertEqual([], self.service.calls)

    def test_send_reply_keeps_exact_source_account(self):
        reply = {"account": "other", "mailbox": "INBOX", "uidvalidity": "42", "uid": "123"}
        self.assertTrue(self.invoke("mail_send_preview", {
            "account": "selected", "body_text": "Thanks", "reply": reply})["isError"])
        self.assertEqual([], self.service.calls)

    def test_draft_operations_do_not_dispatch_sending(self):
        values = {"account": "one", "body_text": "Do not send", "to": ["recipient@example.test"], "subject": "Draft"}
        self.assertFalse(self.invoke("mail_draft_preview", values)["isError"])
        self.assertEqual([("preview", values)], self.service.calls)
        self.assertTrue(self.invoke("mail_draft_apply", {
            "preview_id": "a" * 32, "preview_hash": "b" * 64, "authorization": "send"})["isError"])

    def test_readiness_and_status_are_not_send_operations(self):
        for name, arguments, method in (
            ("mail_send_check", {"account": "one"}, "send_check"),
            ("mail_send_status", {"preview_id": "a" * 32}, "send_status"),
        ):
            self.assertFalse(self.invoke(name, arguments)["isError"])
            self.assertEqual(method, self.service.calls[-1][0])
            self.assertTrue(next(t for t in mcp.TOOLS if t["name"] == name)["annotations"]["readOnlyHint"])

    def test_send_setup_only_accepts_nonsecret_proposals(self):
        for field in ("password", "token", "smtp_username", "host_confirmed", "send_enabled"):
            self.assertTrue(self.invoke("mail_send_setup", {"account": "one", field: "synthetic"})["isError"])
        proposals = {"account": "one", "host": "smtp.example.test", "port": 465,
                     "security": "ssl", "sent_copy": "append"}
        safe = {"status": "browser_opened", "mailbox_writes": 0}
        with patch("imap_mail.send_setup.start_send_setup", return_value=safe) as start:
            self.assertEqual(safe, self.invoke("mail_send_setup", proposals)["structuredContent"])
        start.assert_called_once_with(config_path=self.service.config_path, **proposals)

    def test_notifications_cannot_send(self):
        result = mcp.handle_request({"jsonrpc": "2.0", "method": "tools/call", "params": {
            "name": "mail_send_apply", "arguments": {"preview_id": "a" * 32,
                "preview_hash": "b" * 64, "authorization": "send"}}}, service=self.service)
        self.assertIsNone(result)
        self.assertEqual([], self.service.calls)

    def test_cli_send_contract_preserves_marker(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cli.main(["send-apply", "a" * 32, "b" * 64, "--authorization", "send"], service=self.service)
        self.assertEqual(0, result)
        self.assertEqual("send_apply", self.service.calls[-1][0])
        self.assertEqual("send", self.service.calls[-1][1]["authorization"])

    def test_cli_send_preview_remains_a_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps({"account": "one", "body_text": "hello",
                                       "to": ["recipient@example.test"], "subject": "Synthetic"}))
            with contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(["send-preview", "--request", str(path)], service=self.service)
        self.assertEqual(0, result)
        self.assertEqual("send_preview", self.service.calls[-1][0])


if __name__ == "__main__":
    unittest.main()
