"""Existing-draft edit MCP/CLI contract tests; no credentials or network."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import cli, mcp


class RecordingService:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(**kwargs):
            self.calls.append((name, kwargs))
            return {"status": "prepared", "method": name, "arguments": kwargs}
        return record


class EditInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.service = RecordingService()
        self.draft = {"account": "example-work", "mailbox": "Drafts", "uidvalidity": "42", "uid": "123"}
        self.preview = {"account": "example-work", "draft": self.draft,
                        "replacements": [{"old_text": "Earlier sentence.", "new_text": "Corrected sentence."}]}
        self.receipt = {"preview_id": "a" * 32, "preview_hash": "b" * 64}

    def invoke(self, name, arguments):
        return mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}, service=self.service)["result"]

    def test_edit_preview_keeps_exact_account_source_and_replacements(self):
        result = self.invoke("mail_draft_edit_preview", self.preview)
        self.assertFalse(result["isError"])
        self.assertEqual([("draft_edit_preview", self.preview)], self.service.calls)

    def test_cross_account_and_incomplete_references_never_reach_service(self):
        references = [
            {**self.draft, "account": "another-company"},
            {key: value for key, value in self.draft.items() if key != "account"},
            {**self.draft, "uid": "1:*"},
            {**self.draft, "uidvalidity": "bad"},
            {**self.draft, "mailbox": ""},
            {**self.draft, "fallback_account": "other"},
        ]
        for reference in references:
            with self.subTest(reference=reference):
                result = self.invoke("mail_draft_edit_preview", {**self.preview, "draft": reference})
                self.assertTrue(result["isError"])
                self.assertEqual("INVALID_ARGUMENTS", result["structuredContent"]["code"])
        self.assertEqual([], self.service.calls)

    def test_edit_cannot_override_recipients_signature_attachments_or_sending(self):
        for field, value in (("to", ["other@example.test"]), ("subject", "New subject"),
                             ("body_text", "Rebuilt body"), ("signature", "Replacement"),
                             ("attachments", ["/private/new.pdf"]), ("reply", self.draft),
                             ("authorization", "send"), ("send", True), ("password", "synthetic")):
            with self.subTest(field=field):
                self.assertTrue(self.invoke("mail_draft_edit_preview", {**self.preview, field: value})["isError"])
        self.assertEqual([], self.service.calls)

    def test_replacements_are_nonempty_bounded_and_exactly_shaped(self):
        invalid = [None, {}, [], "Earlier sentence.",
                   [{"old_text": "Earlier sentence."}],
                   [{"new_text": "Corrected sentence."}],
                   [{"old_text": "", "new_text": "New"}],
                   [{"old_text": "x", "new_text": None}],
                   [{"old_text": "x", "new_text": "y", "all": True}],
                   [{"old_text": "x" * 200001, "new_text": "y"}],
                   [{"old_text": "x", "new_text": "y" * 200001}],
                   [{"old_text": "x", "new_text": "y"}] * 21]
        for replacements in invalid:
            with self.subTest(kind=type(replacements).__name__):
                result = self.invoke("mail_draft_edit_preview", {**self.preview, "replacements": replacements})
                self.assertTrue(result["isError"])
        self.assertEqual([], self.service.calls)

    def test_text_deletion_and_twenty_replacements_are_schema_supported(self):
        replacements = [{"old_text": f"Sentence {index}", "new_text": ""} for index in range(20)]
        values = {**self.preview, "replacements": replacements}
        self.assertFalse(self.invoke("mail_draft_edit_preview", values)["isError"])
        self.assertEqual([("draft_edit_preview", values)], self.service.calls)

    def test_edit_apply_uses_only_reviewed_id_hash_and_never_send_dispatch(self):
        result = self.invoke("mail_draft_edit_apply", self.receipt)
        self.assertFalse(result["isError"])
        self.assertEqual([("draft_edit_apply", self.receipt)], self.service.calls)
        self.service.calls.clear()
        for values in ({"preview_id": "a" * 32}, {**self.receipt, "preview_hash": "bad"},
                       {**self.receipt, "authorization": "send"}, {**self.receipt, "body_text": "Changed"},
                       {**self.receipt, "draft": self.draft}):
            with self.subTest(values=values):
                self.assertTrue(self.invoke("mail_draft_edit_apply", values)["isError"])
        self.assertEqual([], self.service.calls)

    def test_status_only_dispatches_same_edit_reference(self):
        values = {"preview_id": "a" * 32}
        self.assertFalse(self.invoke("mail_draft_edit_status", values)["isError"])
        self.assertEqual([("draft_edit_status", values)], self.service.calls)
        self.service.calls.clear()
        self.assertTrue(self.invoke("mail_draft_edit_status", {**values, "cleanup": True})["isError"])
        self.assertEqual([], self.service.calls)

    def test_tool_metadata_marks_edit_apply_as_destructive_not_status(self):
        definitions = {entry["name"]: entry for entry in mcp.TOOLS}
        preview = definitions["mail_draft_edit_preview"]["annotations"]
        apply = definitions["mail_draft_edit_apply"]["annotations"]
        status = definitions["mail_draft_edit_status"]["annotations"]
        self.assertFalse(preview["readOnlyHint"])
        self.assertFalse(preview["destructiveHint"])
        self.assertFalse(preview["idempotentHint"])
        self.assertFalse(apply["readOnlyHint"])
        self.assertTrue(apply["destructiveHint"])
        self.assertTrue(apply["idempotentHint"])
        self.assertTrue(status["readOnlyHint"])
        self.assertFalse(status["destructiveHint"])
        self.assertTrue(status["idempotentHint"])
        for name in ("mail_draft_edit_preview", "mail_draft_edit_apply", "mail_draft_edit_status"):
            self.assertFalse(definitions[name]["inputSchema"]["additionalProperties"])

    def test_notifications_cannot_preview_or_apply_edits(self):
        for name, arguments in (("mail_draft_edit_preview", self.preview), ("mail_draft_edit_apply", self.receipt)):
            response = mcp.handle_request({"jsonrpc": "2.0", "method": "tools/call", "params": {
                "name": name, "arguments": arguments}}, service=self.service)
            self.assertIsNone(response)
        self.assertEqual([], self.service.calls)

    def test_factory_selects_attachment_capable_edit_service_without_eager_profile_access(self):
        module = types.ModuleType("imap_mail.flags")
        module.FlagService = Mock(return_value=self.service)
        with patch.dict(sys.modules, {"imap_mail.flags": module}):
            self.assertIs(self.service, mcp.make_service("/private/settings.json", "/private/state"))
        module.FlagService.assert_called_once_with(config_path="/private/settings.json", state_dir="/private/state")

    def test_cli_preview_accepts_input_and_request_without_applying(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(self.preview), encoding="utf-8")
            for argument in ("--input", "--request"):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = cli.main(["draft-edit-preview", argument, str(path)], service=self.service)
                self.assertEqual(0, result)
        self.assertEqual([("draft_edit_preview", self.preview)] * 2, self.service.calls)

    def test_cli_apply_and_status_do_not_dispatch_smtp(self):
        for arguments, method, expected in (
            (["draft-edit-apply", self.receipt["preview_id"], self.receipt["preview_hash"]], "draft_edit_apply", self.receipt),
            (["draft-edit-status", self.receipt["preview_id"]], "draft_edit_status", {"preview_id": self.receipt["preview_id"]}),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(arguments, service=self.service)
            self.assertEqual(0, result)
            self.assertEqual((method, expected), self.service.calls[-1])

    def test_cli_preview_invalid_json_is_sanitized_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text('{"private-mail-content":', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = cli.main(["draft-edit-preview", "--input", str(path)], service=self.service)
        self.assertEqual(1, result)
        self.assertEqual("INVALID_ARGUMENTS", json.loads(output.getvalue())["error"]["code"])
        self.assertNotIn("private-mail-content", output.getvalue())
        self.assertEqual([], self.service.calls)

    def test_existing_preview_commands_keep_request_and_gain_input_alias(self):
        values = {"account": "example-work", "body_text": "Unsent or send-specific preview",
                  "to": ["recipient@example.test"], "subject": "Synthetic"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(values), encoding="utf-8")
            for command, method in (("draft-preview", "preview"), ("send-preview", "send_preview")):
                for argument in ("--request", "--input"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = cli.main([command, argument, str(path)], service=self.service)
                    self.assertEqual(0, result)
                    self.assertEqual((method, values), self.service.calls[-1])


if __name__ == "__main__":
    unittest.main()
