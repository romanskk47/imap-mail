"""Offline contract tests. No credentials, network or mailbox mutations."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from imap_mail import cli, mcp


class FakeService:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            return {"status": "prepared" if name == "preview" else "verified", "method": name, "arguments": kwargs}
        return call


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()

    def invoke(self, name, arguments):
        return mcp.handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}}, service=self.service)

    def test_exact_tool_allowlist_and_annotations(self):
        expected = {"mail_setup", "mail_accounts", "mail_check", "mail_folders", "mail_search", "mail_read", "mail_draft_preview", "mail_draft_apply", "mail_draft_status"}
        self.assertEqual(expected, set(mcp.TOOL_SCHEMAS))
        for definition in mcp.TOOLS:
            self.assertFalse(definition["annotations"]["destructiveHint"])
            self.assertFalse(definition["inputSchema"]["additionalProperties"])
        self.assertFalse(next(x for x in mcp.TOOLS if x["name"] == "mail_draft_apply")["annotations"]["readOnlyHint"])
        self.assertTrue(next(x for x in mcp.TOOLS if x["name"] == "mail_check")["annotations"]["readOnlyHint"])

    def test_setup_has_no_secret_fields_or_session_url_in_result(self):
        service = type("ConfigService", (), {"config_path": "/private/settings.json"})()
        safe = {"status": "browser_opened", "mailbox_writes": 0}
        with patch("imap_mail.setup_web.start_setup", return_value=safe) as launch:
            response = mcp.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "mail_setup", "arguments": {}}}, service=service)
        self.assertEqual(safe, response["result"]["structuredContent"])
        launch.assert_called_once_with(config_path="/private/settings.json", prefill={})
        self.assertEqual({"email", "account", "entity", "host"}, set(mcp.TOOL_SCHEMAS["mail_setup"]["properties"]))
        self.assertTrue(self.invoke("mail_setup", {"password": "canary-secret"})["result"]["isError"])

    def test_agent_can_prepare_nonsecret_setup_metadata(self):
        service = type("ConfigService", (), {"config_path": "/private/settings.json"})()
        values = {"email": "me@example.test", "account": "work", "entity": "example-owner", "host": "imap.example.test"}
        with patch("imap_mail.setup_web.start_setup", return_value={"status": "browser_opened", "mailbox_writes": 0}) as launch:
            response = mcp.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "mail_setup", "arguments": values}}, service=service)
        self.assertFalse(response["result"]["isError"])
        launch.assert_called_once_with(config_path="/private/settings.json", prefill=values)
        for rejected in ({"host_confirmed": True}, {"token": "secret"}, {"url": "http://private-session"}):
            self.assertTrue(self.invoke("mail_setup", rejected)["result"]["isError"])

    def test_discovery_preserves_incomplete_accounts_and_transport_preference(self):
        discovery = {"status": "needs_attention", "preferred_transport": "imap", "accounts": [
            {"account": "configured", "configured": True, "drafts_ready": True, "connection": "not_checked", "issues": []},
            {"account": "missing-signature", "configured": True, "drafts_ready": False, "connection": "not_checked", "issues": ["SIGNATURE_CONFIGURATION_REQUIRED"]},
            {"account": "profile-only", "configured": False, "drafts_ready": False, "connection": "not_checked", "issues": ["ACCOUNT_NOT_CONFIGURED"]},
        ], "mailbox_writes": 0}
        class InventoryService:
            def accounts(self):
                return discovery
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "mail_accounts", "arguments": {}}}, service=InventoryService())
        self.assertEqual(discovery, response["result"]["structuredContent"])

    def test_check_uses_exact_account_without_apply_or_fallback(self):
        result = self.invoke("mail_check", {"account": "chosen-account"})
        self.assertFalse(result["result"]["isError"])
        self.assertEqual([("check", {"account": "chosen-account"})], self.service.calls)

    def test_check_preserves_account_specific_readiness_gaps(self):
        class GapService:
            def check(self, account):
                return {"account": account, "status": "needs_attention", "connection": False,
                        "error_code": "SIGNATURE_PLAIN_REQUIRED", "issues": ["SIGNATURE_PLAIN_REQUIRED"], "mailbox_writes": 0}
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "mail_check", "arguments": {"account": "chosen-account"}}}, service=GapService())
        value = response["result"]["structuredContent"]
        self.assertEqual("chosen-account", value["account"])
        self.assertEqual("needs_attention", value["status"])
        self.assertEqual(0, value["mailbox_writes"])

    def test_handshake_does_not_read_configuration(self):
        with patch.object(mcp, "make_service", side_effect=AssertionError("Must not touch profiles")):
            response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual("2025-06-18", response["result"]["protocolVersion"])
        self.assertEqual("imap-mail", response["result"]["serverInfo"]["name"])

    def test_account_bound_read_passes_stable_reference(self):
        values = {"account": "example", "mailbox": "INBOX", "uidvalidity": "42", "uid": "123"}
        response = self.invoke("mail_read", values)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual([("read", values)], self.service.calls)

    def test_preview_preserves_reply_and_attachment_request(self):
        values = {"account": "example", "body_text": "A human reply\nwith two lines.",
                  "reply": {"account": "example", "mailbox": "INBOX", "uidvalidity": "42", "uid": "123"},
                  "attachments": ["/private/example/report.pdf"]}
        self.invoke("mail_draft_preview", values)
        self.assertEqual([("preview", values)], self.service.calls)

    def test_apply_accepts_exact_id_and_hash_not_inline_body(self):
        values = {"preview_id": "b" * 32, "preview_hash": "a" * 64}
        self.invoke("mail_draft_apply", values)
        self.assertEqual([("apply", values)], self.service.calls)
        self.service.calls.clear()
        response = self.invoke("mail_draft_apply", {**values, "body_text": "changed"})
        self.assertTrue(response["result"]["isError"])
        self.assertEqual([], self.service.calls)

    def test_invalid_inputs_never_reach_service(self):
        invalid = [
            ("mail_send", {}),
            ("mail_accounts", {"password": "secret-value"}),
            ("mail_check", {}),
            ("mail_check", {"account": "example", "apply": True}),
            ("mail_check", {"account": "example", "fallback_account": "other-account"}),
            ("mail_search", {"account": "example", "mailbox": "INBOX", "limit": True}),
            ("mail_search", {"account": "example", "mailbox": "INBOX", "limit": 101}),
            ("mail_search", {"account": "example", "mailbox": "INBOX", "field": "BODY"}),
            ("mail_read", {"account": "example", "mailbox": "INBOX", "uidvalidity": "42", "uid": "1:*"}),
            ("mail_draft_preview", {"account": "example", "body_text": "hi", "reply": {"uid": "1"}}),
            ("mail_draft_preview", {"account": "example", "body_text": "hi", "reply": {"mailbox": "INBOX", "uidvalidity": "42", "uid": "123"}}),
            ("mail_draft_preview", {"account": "example", "body_text": "hi", "reply": {"account": "another-company", "mailbox": "INBOX", "uidvalidity": "42", "uid": "123"}}),
            ("mail_draft_preview", {"account": "example", "body_text": "hi", "to": "a@example.invalid"}),
            ("mail_draft_preview", {"account": "example", "body_text": "hi", "reply_all": "false"}),
            ("mail_draft_apply", {"preview_id": "b" * 32, "preview_hash": "bad"}),
        ]
        for name, values in invalid:
            with self.subTest(name=name, values=values):
                response = self.invoke(name, values)
                self.assertTrue(response["result"]["isError"])
                self.assertEqual("INVALID_ARGUMENTS", response["result"]["structuredContent"]["code"])
        self.assertEqual([], self.service.calls)

    def test_notifications_cannot_write(self):
        result = mcp.handle_request({"jsonrpc": "2.0", "method": "tools/call", "params": {
            "name": "mail_draft_apply", "arguments": {"preview_id": "b" * 32, "preview_hash": "a" * 64}}}, service=self.service)
        self.assertIsNone(result)
        self.assertEqual([], self.service.calls)

    def test_helper_output_and_unexpected_exception_are_sanitized(self):
        class LeakyService:
            def accounts(self):
                print("secret password in accidental log")
                print("another password", file=sys.stderr)
                raise RuntimeError("credential=do-not-expose")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mail_accounts", "arguments": {}}}, service=LeakyService())
        self.assertEqual("", out.getvalue())
        self.assertEqual("", err.getvalue())
        self.assertNotIn("do-not-expose", json.dumps(response))
        self.assertEqual("INTERNAL_ERROR", response["result"]["structuredContent"]["code"])

    def test_stdio_handshake_unknown_tool_and_parse_error(self):
        payload = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "not json",
        ]) + "\n"
        proc = subprocess.run([sys.executable, str(SCRIPTS / "imap_mail_mcp.py")], input=payload,
                              text=True, capture_output=True, check=True)
        self.assertEqual("", proc.stderr)
        messages = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(3, len(messages))
        self.assertEqual(1, messages[0]["id"])
        self.assertEqual(9, len(messages[1]["result"]["tools"]))
        self.assertEqual(-32700, messages[2]["error"]["code"])


class CLITests(unittest.TestCase):
    def run_cli(self, args, service=None):
        output = io.StringIO()
        current = service or FakeService()
        with contextlib.redirect_stdout(output):
            exitcode = cli.main(args, service=current)
        return exitcode, json.loads(output.getvalue()), current

    def test_read_and_search_argument_contract(self):
        exitcode, result, service = self.run_cli(["search", "example", "INBOX", "--query", "invoice", "--since", "2026-10-01", "--limit", "5"])
        self.assertEqual(0, exitcode)
        self.assertEqual("search", result["method"])
        self.assertEqual(5, service.calls[0][1]["limit"])
        self.assertNotIn("before", service.calls[0][1])

    def test_check_command_is_read_only_and_account_bound(self):
        exitcode, result, service = self.run_cli(["check", "chosen-account"])
        self.assertEqual(0, exitcode)
        self.assertEqual("check", result["method"])
        self.assertEqual([("check", {"account": "chosen-account"})], service.calls)

    def test_setup_cli_uses_local_browser_without_model_secret_arguments(self):
        with patch("imap_mail.setup_web.serve_setup", return_value=0) as serve:
            self.assertEqual(0, cli.main(["--config", "/private/settings.json", "setup"]))
        serve.assert_called_once_with(config_path="/private/settings.json", open_browser=True)

    def test_preview_json_file_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps({"account": "example", "body_text": "hi", "to": ["test@example.invalid"], "subject": "Draft"}), encoding="utf-8")
            exitcode, result, service = self.run_cli(["draft-preview", "--request", str(path)])
        self.assertEqual(0, exitcode)
        self.assertEqual("prepared", result["status"])
        self.assertEqual("preview", service.calls[0][0])
        self.assertEqual(0, self.run_cli(["draft-status", "b" * 32])[0])

    def test_invalid_preview_json_returns_sanitized_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text('{"secret-password":', encoding="utf-8")
            exitcode, result, service = self.run_cli(["draft-preview", "--request", str(path)])
        self.assertEqual(1, exitcode)
        self.assertEqual("INVALID_ARGUMENTS", result["error"]["code"])
        self.assertNotIn("secret-password", json.dumps(result))
        self.assertEqual([], service.calls)


if __name__ == "__main__":
    unittest.main()
