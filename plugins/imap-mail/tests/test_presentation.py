"""Compact review contracts; synthetic fixtures only, no real mail access."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import mcp
from imap_mail.presentation import compact_review
from imap_mail.core import DraftError
import test_core as fixtures


class PresentationTests(unittest.TestCase):
    def sample(self):
        text = "Complete synthetic body. " * 100
        html = "<p>Different HTML-only information.</p>"
        part = {"index": 2, "type": "text/plain", "content": text,
                "bytes": len(text), "sha256": "a" * 64}
        before = {"body_text": text, "rendered_body_text": text,
                  "rendered_body_html": html,
                  "mime_parts": [part, {"index": 3, "type": "text/html", "content": html}],
                  "attachments": [{"filename": "report.pdf", "bytes": 42, "sha256": "b" * 64}]}
        return {"status": "prepared", "preview_id": "a" * 32, "preview_hash": "b" * 64,
                "from": "owner@example.test", "to": ["recipient@example.test"], "cc": [],
                "subject": "Synthetic", **before, "before": copy.deepcopy(before),
                "signature": {"mode": "existing_draft", "preserved": True},
                "thread_headers": {"References": "<original@example.test>"},
                "replacements": [{"old_text": "old", "new_text": "new"}]}

    def test_compact_preserves_complete_plain_and_different_html_once(self):
        full = self.sample()
        original = copy.deepcopy(full)
        compact = compact_review(full)
        self.assertEqual(full, original)
        self.assertEqual(compact["body_text"], full["rendered_body_text"])
        self.assertEqual(compact["body_html"], full["rendered_body_html"])
        self.assertNotIn("rendered_body_text", compact)
        self.assertNotIn("rendered_body_html", compact)
        self.assertNotIn("content", compact["mime_parts"][0])
        self.assertNotIn("content", compact["mime_parts"][1])
        for key in ("preview_id", "preview_hash", "from", "to", "cc", "subject", "attachments", "thread_headers", "replacements"):
            self.assertEqual(full[key], compact[key])
        self.assertFalse(compact["review_completeness"]["body_truncated"])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)) * .65)

    def test_unrepresented_alternative_mime_body_is_not_hidden(self):
        full = self.sample()
        full["mime_parts"].append({"index": 8, "type": "text/html", "content": "<p>Extra body not in canonical HTML</p>"})
        compact = compact_review(full)
        self.assertEqual(compact["mime_parts"][-1]["content"], full["mime_parts"][-1]["content"])
        self.assertEqual(compact["review_completeness"]["additional_mime_body_contents_preserved"], 1)

    def test_creation_retains_html_signature_when_full_html_is_not_returned(self):
        compact = compact_review({"body_text": "Authored", "rendered_body_text": "Authored\nOwner",
                                  "signature": {"plain": "Owner", "html": '<a href="https://example.test">Owner</a>'}})
        self.assertNotIn("plain", compact["signature"])
        self.assertIn("Owner", compact["signature"]["html"])
        self.assertEqual(len(compact["signature"]["plain_sha256"]), 64)

    def test_mcp_legacy_full_default_and_compact_json_channels(self):
        full = self.sample()
        calls = []
        class Service:
            def preview(self, **kwargs):
                calls.append(kwargs)
                return copy.deepcopy(full)
        def invoke(extra):
            return mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "mail_draft_preview", "arguments": {"account": "one", "body_text": "Authored", **extra}}}, service=Service())["result"]
        legacy = invoke({})
        self.assertEqual(legacy["structuredContent"], full)
        compact = invoke({"response_view": "compact"})
        self.assertFalse(compact["isError"])
        self.assertEqual(json.loads(compact["content"][0]["text"]), compact["structuredContent"])
        self.assertEqual(calls, [{"account": "one", "body_text": "Authored"}] * 2)
        self.assertEqual(compact["structuredContent"]["preview_hash"], full["preview_hash"])


class OperationReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DraftServiceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service

    def test_read_check_needs_no_signature_draft_opt_in_or_smtp(self):
        self.fixture.entry.clear()
        self.fixture.entry["identity"] = {"email": "owner@example.test", "host": "imap.example.test", "entity": "synthetic"}
        self.fixture.json(self.fixture.config_path, self.fixture.config)
        result = self.service.check("one", operation="read")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["read_ready"])
        self.assertEqual(result["operation"], "read")
        self.assertEqual(self.fixture.server.commands, ["EXAMINE", "LOGOUT"])
        self.assertEqual(self.fixture.server.append_calls, 0)
        self.assertFalse((self.fixture.root / "state").exists())
        self.assertEqual(self.service.check("one")["status"], "needs_attention")

    def test_preview_read_preserves_id_hash_and_stored_files_without_network(self):
        created = self.fixture.preview()
        root = self.fixture.root / "state"
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        connections = self.fixture.server.connections
        result = self.service.preview_read(created["preview_id"])
        self.assertEqual(result["preview_id"], created["preview_id"])
        self.assertEqual(result["preview_hash"], created["preview_hash"])
        self.assertEqual(result["rendered_body_text"], created["rendered_body_text"])
        self.assertEqual(self.fixture.server.connections, connections)
        self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
        self.assertEqual(result["mailbox_writes"], 0)

    def test_new_quoted_printable_body_lines_follow_rfc_2045(self):
        # Mostly ASCII long lines select quoted-printable; dense Polish text
        # can legitimately select base64 instead, which is a different test.
        created = self.fixture.preview(body_text="A long ASCII sentence with spaces. " * 100)
        message = self.fixture.mime(created)
        quoted_parts = [part for part in message.walk()
                        if part.get("Content-Transfer-Encoding") == "quoted-printable"]
        self.assertTrue(quoted_parts)
        for part in quoted_parts:
            self.assertLessEqual(max(map(len, part.get_payload().splitlines())), 76)

    def test_preview_read_rejects_tampered_preview_and_account_change(self):
        created = self.fixture.preview()
        self.fixture.entry["identity"]["entity"] = "different"
        self.fixture.json(self.fixture.config_path, self.fixture.config)
        with self.assertRaises(DraftError) as error:
            self.service.preview_read(created["preview_id"])
        self.assertEqual(error.exception.code, "ACCOUNT_IDENTITY_MISMATCH")
        self.assertEqual(self.fixture.server.append_calls, 0)


if __name__ == "__main__":
    unittest.main()
