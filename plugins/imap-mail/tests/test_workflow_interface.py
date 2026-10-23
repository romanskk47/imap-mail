"""MCP/CLI routing contracts for 0.7 workflows; no real profiles or mail access."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import cli, mcp


PREVIEW_ID = "a" * 32
PREVIEW_HASH = "b" * 64
REFERENCE = {"account": "one", "mailbox": "INBOX", "uidvalidity": "7", "uid": "13"}


class RecordingService:
    def __init__(self):
        self.calls = []
        self.review = {"status": "prepared", "preview_id": PREVIEW_ID,
                       "preview_hash": PREVIEW_HASH, "account": "one",
                       "body_text": "Authored", "rendered_body_text": "Authored\nOwner",
                       "rendered_body_html": "<p>Authored</p><p>Owner</p>",
                       "attachments": [{"filename": "report.pdf", "bytes": 12, "sha256": "c" * 64}],
                       "mailbox_writes": 0, "smtp_submissions": 0}

    def accounts(self):
        self.calls.append(("accounts", {}))
        return {"accounts": [{"account": name, "configured": True, "read_ready": True,
                              "drafts_ready": False, "flags_enabled": name == "one",
                              "email": name + "@example.test", "entity": name,
                              "host": "imap.example.test", "issues": []}
                             for name in ("one", "two")]}

    def __getattr__(self, name):
        allowed = {"check", "preview", "draft_edit_preview", "draft_attachment_preview",
                   "send_preview", "preview_read", "flags_preview", "flags_apply", "flags_status"}
        if name not in allowed:
            raise AssertionError("Unexpected service method: " + name)

        def call(**kwargs):
            self.calls.append((name, copy.deepcopy(kwargs)))
            return copy.deepcopy(self.review)
        return call


class WorkflowMCPTests(unittest.TestCase):
    def setUp(self):
        self.service = RecordingService()

    def invoke(self, name, arguments):
        return mcp.handle_request({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}},
                                 service=self.service)["result"]

    def assert_rejected(self, name, arguments):
        self.service.calls.clear()
        result = self.invoke(name, arguments)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(self.service.calls, [])

    def test_capabilities_routes_optional_exact_account_without_live_check(self):
        for arguments, expected in (({}, ["one", "two"]), ({"account": "two"}, ["two"])):
            with self.subTest(arguments=arguments):
                self.service.calls.clear()
                result = self.invoke("mail_capabilities", arguments)
                self.assertFalse(result["isError"])
                rows = result["structuredContent"]["local_configuration"]["accounts"]
                self.assertEqual([row["account"] for row in rows], expected)
                self.assertEqual(self.service.calls, [("accounts", {})])

    def test_read_check_passes_explicit_operation_and_preserves_legacy_omission(self):
        for arguments in ({"account": "one"}, {"account": "one", "operation": "read"},
                          {"account": "one", "operation": "draft"}):
            with self.subTest(arguments=arguments):
                self.service.calls.clear()
                self.assertFalse(self.invoke("mail_check", arguments)["isError"])
                self.assertEqual(self.service.calls, [("check", arguments)])

    def test_capabilities_unknown_exact_account_never_falls_back(self):
        result = self.invoke("mail_capabilities", {"account": "missing"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "ACCOUNT_NOT_CONFIGURED")
        self.assertEqual(self.service.calls, [("accounts", {})])

    def test_compact_view_is_presentation_only_for_every_review_tool(self):
        cases = [("mail_draft_preview", "preview", {"account": "one", "body_text": "Authored"}),
                 ("mail_draft_edit_preview", "draft_edit_preview",
                  {"account": "one", "draft": REFERENCE,
                   "replacements": [{"old_text": "old", "new_text": "new"}]}),
                 ("mail_draft_attachment_preview", "draft_attachment_preview",
                  {"account": "one", "draft": REFERENCE}),
                 ("mail_send_preview", "send_preview", {"account": "one", "draft": REFERENCE}),
                 ("mail_preview_read", "preview_read", {"preview_id": PREVIEW_ID})]
        for tool, method, payload in cases:
            with self.subTest(tool=tool):
                self.service.calls.clear()
                arguments = {**payload, "response_view": "compact"}
                original = copy.deepcopy(arguments)
                result = self.invoke(tool, arguments)
                self.assertFalse(result["isError"])
                self.assertEqual(self.service.calls, [(method, payload)])
                self.assertEqual(arguments, original)
                compact = result["structuredContent"]
                self.assertEqual(compact["body_text"], self.service.review["rendered_body_text"])
                self.assertEqual(compact["body_html"], self.service.review["rendered_body_html"])
                self.assertEqual(compact["preview_hash"], PREVIEW_HASH)
                self.assertEqual(compact["attachments"], self.service.review["attachments"])
                self.assertEqual(json.loads(result["content"][0]["text"]), compact)

    def test_preview_read_full_default_returns_legacy_fields(self):
        for extra in ({}, {"response_view": "full"}):
            self.service.calls.clear()
            result = self.invoke("mail_preview_read", {"preview_id": PREVIEW_ID, **extra})
            self.assertEqual(result["structuredContent"], self.service.review)
            self.assertEqual(self.service.calls, [("preview_read", {"preview_id": PREVIEW_ID})])

    def test_flags_dispatches_exact_references_targets_and_operation_receipt(self):
        cases = [("mail_flags_preview", "flags_preview",
                  {"account": "one", "messages": [REFERENCE], "seen": False, "flagged": True}),
                 ("mail_flags_apply", "flags_apply",
                  {"preview_id": PREVIEW_ID, "preview_hash": PREVIEW_HASH}),
                 ("mail_flags_status", "flags_status", {"preview_id": PREVIEW_ID})]
        for tool, method, arguments in cases:
            with self.subTest(tool=tool):
                self.service.calls.clear()
                self.assertFalse(self.invoke(tool, arguments)["isError"])
                self.assertEqual(self.service.calls, [(method, arguments)])

    def test_new_read_tools_reject_unknown_or_extraneous_arguments_before_dispatch(self):
        cases = [("mail_capabilities", {"account": "one", "password": "never-read"}),
                 ("mail_capabilities", {"account": None}),
                 ("mail_check", {"account": "one", "operation": "send"}),
                 ("mail_check", {"account": "one", "operation": True}),
                 ("mail_check", {"account": "one", "operation": "read", "write": True}),
                 ("mail_preview_read", {"preview_id": PREVIEW_ID, "response_view": "summary"}),
                 ("mail_preview_read", {"preview_id": PREVIEW_ID, "account": "two"}),
                 ("mail_preview_read", {"preview_id": PREVIEW_ID, "apply": True}),
                 ("mail_preview_unknown", {})]
        for tool, arguments in cases:
            with self.subTest(tool=tool, arguments=arguments):
                self.assert_rejected(tool, arguments)

    def test_flag_tools_reject_missing_targets_arbitrary_flags_and_cross_account_refs(self):
        base = {"account": "one", "messages": [REFERENCE]}
        cases = [("mail_flags_preview", base),
                 ("mail_flags_preview", {**base, "messages": [], "seen": True}),
                 ("mail_flags_preview", {**base, "seen": None}),
                 ("mail_flags_preview", {**base, "seen": 1}),
                 ("mail_flags_preview", {**base, "seen": True, "flags": ["\\Deleted"]}),
                 ("mail_flags_preview", {**base, "seen": True, "response_view": "compact"}),
                 ("mail_flags_preview", {**base, "seen": True,
                                         "messages": [{**REFERENCE, "account": "two"}]}),
                 ("mail_flags_preview", {**base, "seen": True,
                                         "messages": [{**REFERENCE, "extra": "ignored?"}]}),
                 ("mail_flags_apply", {"preview_id": PREVIEW_ID, "preview_hash": PREVIEW_HASH, "seen": True}),
                 ("mail_flags_apply", {"preview_id": PREVIEW_ID, "preview_hash": "bad"}),
                 ("mail_flags_status", {"preview_id": "not-an-id"}),
                 ("mail_flags_status", {"preview_id": PREVIEW_ID, "preview_hash": PREVIEW_HASH})]
        for tool, arguments in cases:
            with self.subTest(tool=tool, arguments=arguments):
                self.assert_rejected(tool, arguments)

    def test_notification_cannot_invoke_flags_apply(self):
        result = mcp.handle_request({"jsonrpc": "2.0", "method": "tools/call",
                                    "params": {"name": "mail_flags_apply", "arguments": {
                                        "preview_id": PREVIEW_ID, "preview_hash": PREVIEW_HASH}}},
                                   service=self.service)
        self.assertIsNone(result)
        self.assertEqual(self.service.calls, [])


class WorkflowCLITests(unittest.TestCase):
    def setUp(self):
        self.service = RecordingService()

    def invoke(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(arguments, service=self.service)
        return code, json.loads(output.getvalue())

    def test_capabilities_and_operation_read_route_through_same_service(self):
        for arguments, expected in ((["capabilities"], [("accounts", {})]),
                                    (["capabilities", "--account", "two"], [("accounts", {})]),
                                    (["check", "one", "--operation", "read"],
                                     [("check", {"account": "one", "operation": "read"})]),
                                    (["check", "one"], [("check", {"account": "one"})])):
            with self.subTest(arguments=arguments):
                self.service.calls.clear()
                code, _ = self.invoke(arguments)
                self.assertEqual(code, 0)
                self.assertEqual(self.service.calls, expected)

    def test_preview_read_compact_never_passes_presentation_to_service(self):
        code, result = self.invoke(["preview-read", PREVIEW_ID, "--response-view", "compact"])
        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("preview_read", {"preview_id": PREVIEW_ID})])
        self.assertEqual(result["body_text"], self.service.review["rendered_body_text"])
        self.assertEqual(result["preview_hash"], PREVIEW_HASH)

    def test_capabilities_unknown_cli_account_reports_gap_without_fallback(self):
        code, result = self.invoke(["capabilities", "--account", "missing"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "ACCOUNT_NOT_CONFIGURED")
        self.assertEqual(self.service.calls, [("accounts", {})])

    def test_preview_read_legacy_omission_and_explicit_full_keep_same_output(self):
        for extra in ([], ["--response-view", "full"]):
            self.service.calls.clear()
            code, result = self.invoke(["preview-read", PREVIEW_ID, *extra])
            self.assertEqual(code, 0)
            self.assertEqual(result, self.service.review)
            self.assertEqual(self.service.calls, [("preview_read", {"preview_id": PREVIEW_ID})])

    def test_flags_preview_json_aliases_and_apply_status_route_exact_arguments(self):
        payload = {"account": "one", "messages": [REFERENCE], "seen": False, "flagged": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            for alias in ("--input", "--request"):
                self.service.calls.clear()
                code, _ = self.invoke(["flags-preview", alias, str(path)])
                self.assertEqual(code, 0)
                self.assertEqual(self.service.calls, [("flags_preview", payload)])
        for command, method, args in (("flags-apply", "flags_apply", {"preview_id": PREVIEW_ID, "preview_hash": PREVIEW_HASH}),
                                      ("flags-status", "flags_status", {"preview_id": PREVIEW_ID})):
            self.service.calls.clear()
            code, _ = self.invoke([command, *args.values()])
            self.assertEqual(code, 0)
            self.assertEqual(self.service.calls, [(method, args)])

    def test_compose_json_compact_view_is_stripped_and_conflicting_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            payload = {"account": "one", "body_text": "Authored", "response_view": "compact"}
            path.write_text(json.dumps(payload), encoding="utf-8")
            code, result = self.invoke(["draft-preview", "--input", str(path), "--response-view", "compact"])
            self.assertEqual(code, 0)
            self.assertEqual(result["response_view"], "compact")
            self.assertEqual(self.service.calls, [("preview", {"account": "one", "body_text": "Authored"})])
            self.service.calls.clear()
            code, result = self.invoke(["draft-preview", "--input", str(path), "--response-view", "full"])
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS")
            self.assertEqual(self.service.calls, [])

    def test_flags_json_rejects_unsupported_arguments_before_service_dispatch(self):
        payload = {"account": "one", "messages": [REFERENCE], "seen": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            for extra in ({"response_view": "compact"}, {"authorization": "send"}, {"flags": ["\\Deleted"]}):
                with self.subTest(extra=extra):
                    self.service.calls.clear()
                    path.write_text(json.dumps({**payload, **extra}), encoding="utf-8")
                    code, result = self.invoke(["flags-preview", "--input", str(path)])
                    self.assertEqual(code, 1)
                    self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS")
                    self.assertEqual(self.service.calls, [])

    def test_unknown_cli_options_and_invalid_choices_never_dispatch(self):
        for arguments in (["capabilities", "--apply"], ["check", "one", "--operation", "flags"],
                          ["preview-read", PREVIEW_ID, "--response-view", "summary"],
                          ["flags-status", PREVIEW_ID, "--apply"],
                          ["flags-apply", PREVIEW_ID, PREVIEW_HASH, "--seen"]):
            with self.subTest(arguments=arguments):
                self.service.calls.clear()
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main(arguments, service=self.service)
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(self.service.calls, [])


if __name__ == "__main__":
    unittest.main()
