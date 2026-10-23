"""Draft-attachment MCP/CLI contracts; synthetic data, no mailbox or SMTP access."""

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
    def __init__(self):
        self.calls = []

    def draft_attachment_preview(self, **kwargs):
        self.calls.append(("draft_attachment_preview", kwargs))
        return {"status": "prepared", "operation": "draft_edit", "preview_id": "a" * 32,
                "preview_hash": "b" * 64}

    def draft_edit_apply(self, **kwargs):
        self.calls.append(("draft_edit_apply", kwargs))
        return {"status": "verified"}

    def draft_edit_status(self, **kwargs):
        self.calls.append(("draft_edit_status", kwargs))
        return {"status": "prepared"}


class AttachmentInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.service = RecordingService()
        self.draft = {"account": "example-work", "mailbox": "Drafts", "uidvalidity": "42", "uid": "123"}
        self.preview = {"account": "example-work", "draft": self.draft,
                        "attachments": ["/private/approved-attachments/report.pdf"],
                        "normalize_attachments": True}

    def invoke(self, arguments, name="mail_draft_attachment_preview"):
        return mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}, service=self.service)["result"]

    def test_preview_only_dispatches_exact_source_and_attachment_change(self):
        result = self.invoke(self.preview)
        self.assertFalse(result["isError"])
        self.assertEqual("draft_edit", result["structuredContent"]["operation"])
        self.assertEqual([("draft_attachment_preview", self.preview)], self.service.calls)

    def test_normalization_only_and_explicit_add_only_are_supported(self):
        minimal = {"account": "example-work", "draft": self.draft}
        for values in (minimal, {**minimal, "attachments": []},
                       {**self.preview, "normalize_attachments": False}):
            with self.subTest(values=values):
                self.assertFalse(self.invoke(values)["isError"])
                self.assertEqual(("draft_attachment_preview", values), self.service.calls[-1])

    def test_account_and_exact_reference_are_required(self):
        references = [
            {**self.draft, "account": "another-company"},
            {key: value for key, value in self.draft.items() if key != "account"},
            {**self.draft, "uid": "1:*"},
            {**self.draft, "uidvalidity": "bad"},
            {**self.draft, "mailbox": ""},
            {**self.draft, "fallback_account": "other"},
        ]
        for values in ({"account": "example-work"}, {"draft": self.draft}, {},
                       *({**self.preview, "draft": reference} for reference in references)):
            with self.subTest(values=values):
                result = self.invoke(values)
                self.assertTrue(result["isError"])
                self.assertEqual("INVALID_ARGUMENTS", result["structuredContent"]["code"])
        self.assertEqual([], self.service.calls)

    def test_invalid_attachment_path_shapes_never_reach_service(self):
        invalid = [None, {}, "report.pdf", [None], [42], [True], [""], ["x" * 4097],
                   [{"path": "/private/report.pdf"}], ["/private/report.pdf"] * 11]
        for attachments in invalid:
            with self.subTest(attachments=attachments):
                self.assertTrue(self.invoke({**self.preview, "attachments": attachments})["isError"])
        self.assertEqual([], self.service.calls)

    def test_only_boolean_normalization_is_accepted(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value):
                self.assertTrue(self.invoke({**self.preview, "normalize_attachments": value})["isError"])
        self.assertEqual([], self.service.calls)

    def test_attachment_preview_cannot_override_body_identity_or_sending(self):
        for field, value in (("to", ["other@example.test"]), ("cc", ["copy@example.test"]),
                             ("subject", "Changed"), ("body_text", "Rebuilt body"),
                             ("from_address", "other@example.test"), ("signature", "Replacement"),
                             ("reply", self.draft), ("replacements", []), ("smtp", {}),
                             ("authorization", "send"), ("send", True), ("apply", True),
                             ("password", "synthetic"), ("fallback_client", "Mail")):
            with self.subTest(field=field):
                self.assertTrue(self.invoke({**self.preview, field: value})["isError"])
        self.assertEqual([], self.service.calls)

    def test_preview_receipt_uses_existing_edit_apply_and_status_contract(self):
        result = self.invoke(self.preview)["structuredContent"]
        receipt = {key: result[key] for key in ("preview_id", "preview_hash")}
        self.assertFalse(self.invoke(receipt, "mail_draft_edit_apply")["isError"])
        self.assertFalse(self.invoke({"preview_id": result["preview_id"]}, "mail_draft_edit_status")["isError"])
        self.assertEqual([("draft_attachment_preview", self.preview), ("draft_edit_apply", receipt),
                          ("draft_edit_status", {"preview_id": result["preview_id"]})], self.service.calls)

    def test_metadata_and_notifications_do_not_allow_implicit_apply(self):
        definition = next(entry for entry in mcp.TOOLS if entry["name"] == "mail_draft_attachment_preview")
        self.assertFalse(definition["inputSchema"]["additionalProperties"])
        self.assertFalse(definition["annotations"]["readOnlyHint"])
        self.assertFalse(definition["annotations"]["destructiveHint"])
        self.assertFalse(definition["annotations"]["idempotentHint"])
        self.assertEqual({"mail_draft_edit_apply", "mail_send_apply", "mail_flags_apply"}, {
            entry["name"] for entry in mcp.TOOLS if entry["annotations"]["destructiveHint"]})
        response = mcp.handle_request({"jsonrpc": "2.0", "method": "tools/call", "params": {
            "name": "mail_draft_attachment_preview", "arguments": self.preview}}, service=self.service)
        self.assertIsNone(response)
        self.assertEqual([], self.service.calls)

    def test_initialize_and_tools_list_remain_lazy(self):
        with patch.object(mcp, "make_service", side_effect=AssertionError("Must not load profiles")):
            for method in ("initialize", "tools/list"):
                response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": method})
                self.assertIn("result", response)
        self.assertEqual([], self.service.calls)

    def test_cli_accepts_input_and_request_without_applying(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(self.preview), encoding="utf-8")
            for option in ("--input", "--request"):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cli.main(["draft-attachment-preview", option, str(path)], service=self.service)
                self.assertEqual(0, code)
                self.assertEqual("draft_edit", json.loads(output.getvalue())["operation"])
        self.assertEqual([("draft_attachment_preview", self.preview)] * 2, self.service.calls)

    def test_cli_bad_file_json_or_unsupported_fields_fail_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            invalid = Path(directory) / "invalid.json"
            invalid.write_text('{"private-mail-content":', encoding="utf-8")
            unsupported = Path(directory) / "unsupported.json"
            unsupported.write_text(json.dumps({**self.preview, "send": True}), encoding="utf-8")
            for path in (missing, invalid, unsupported):
                with self.subTest(path=path.name):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = cli.main(["draft-attachment-preview", "--input", str(path)], service=self.service)
                    self.assertEqual(1, code)
                    self.assertEqual("INVALID_ARGUMENTS", json.loads(output.getvalue())["error"]["code"])
                    self.assertNotIn("private-mail-content", output.getvalue())
        self.assertEqual([], self.service.calls)


if __name__ == "__main__":
    unittest.main()
