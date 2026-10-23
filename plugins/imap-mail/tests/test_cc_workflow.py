"""Offline DW/Cc workflows: review, reply participants, MIME and SMTP envelope.

Every account, message and transport in this module is synthetic. The reusable
fixture injects fake IMAP and SMTP implementations; no real connection is made.
"""

import email
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses
import unittest
from unittest.mock import patch

import test_send as fixtures
from imap_mail.core import DraftService


def recipients(values):
    """Compare mailbox identities, independently of display names and case."""
    if isinstance(values, str):
        values = [values]
    return [address.casefold() for _, address in getaddresses(values)]


class CcWorkflowTests(unittest.TestCase):
    def setUp(self):
        network_guard = patch("socket.create_connection", side_effect=AssertionError(
            "DW/Cc regressions must not open a network connection"))
        network_guard.start()
        self.addCleanup(network_guard.stop)
        self.fixture = fixtures.SendTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def mime(self, preview):
        path = self.fixture.root / "state" / (preview["preview_id"] + ".eml")
        return email.message_from_bytes(path.read_bytes(), policy=policy.default)

    def envelope(self):
        return [row[1].casefold() for row in self.fixture.server.smtp_commands
                if row[0] == "RCPT"]

    def source(self, *, reply_to=True):
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "Customer <customer@example.test>"
        if reply_to:
            message["Reply-To"] = "Support <support@example.test>"
        message["To"] = ("Owner <OWNER@example.test>, Colleague <colleague@example.test>, "
                         "Support <support@example.test>")
        message["Cc"] = ("Alias <ALIAS@example.test>, Copy <copy@example.test>, "
                         "Colleague again <COLLEAGUE@example.test>, "
                         "Copy duplicate <COPY@example.test>, Owner <owner@example.test>")
        message["Subject"] = "Shared customer conversation"
        message["Message-ID"] = "<cc-source@example.test>"
        message["References"] = "<cc-earlier@example.test>"
        message.set_content("Original shared message")
        self.fixture.server.messages["INBOX"]["13"] = message.as_bytes()
        return {"account": "one", "mailbox": "INBOX", "uidvalidity": "7", "uid": "13"}

    def test_new_draft_preserves_cc_in_review_mime_and_verified_saved_copy(self):
        fixture = self.fixture
        service = DraftService(config_path=fixture.config_path,
                               reader_factory=fixture.server.reader)
        preview = service.preview(
            "one", "Synthetic draft with DW", subject="DW draft",
            to=["Recipient <recipient@example.test>"],
            cc=["Copy <copy@example.test>", "Other copy <other@example.test>"])
        expected_copies = ["copy@example.test", "other@example.test"]
        self.assertEqual(recipients(preview["cc"]), expected_copies)
        self.assertEqual(recipients(self.mime(preview).get_all("Cc", [])), expected_copies)
        self.assertEqual(fixture.server.smtp_connections, 0)
        self.assertEqual(fixture.server.append_calls, 0)

        result = service.apply(preview["preview_id"], preview["preview_hash"])
        self.assertEqual(result["status"], "verified")
        saved = email.message_from_bytes(
            fixture.server.messages["Drafts"][result["receipt"]["uid"]], policy=policy.default)
        self.assertEqual(recipients(saved.get_all("Cc", [])), expected_copies)
        self.assertEqual(fixture.server.smtp_connections, 0)

    def test_new_send_freezes_cc_and_addresses_both_to_and_cc_in_smtp(self):
        fixture = self.fixture
        preview = fixture.preview(cc=["Copy <copy@example.test>", "Other <other@example.test>"])
        expected_to = ["recipient@example.test"]
        expected_cc = ["copy@example.test", "other@example.test"]
        self.assertEqual(recipients(preview["to"]), expected_to)
        self.assertEqual(recipients(preview["cc"]), expected_cc)
        self.assertEqual(recipients(self.mime(preview).get_all("Cc", [])), expected_cc)
        self.assertEqual(recipients(fixture.record(preview)["immutable"]["review"]["cc"]), expected_cc)
        self.assertEqual(fixture.server.smtp_connections, 0)

        # Changing the returned view does not change the already frozen message.
        preview["cc"] = ["unreviewed@example.test"]
        result = fixture.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(self.envelope(), expected_to + expected_cc)
        submitted = email.message_from_bytes(fixture.server.submitted[0], policy=policy.default)
        self.assertEqual(recipients(submitted.get_all("To", [])), expected_to)
        self.assertEqual(recipients(submitted.get_all("Cc", [])), expected_cc)
        self.assertEqual(fixture.server.data_calls, 1)

    def test_reply_all_keeps_to_and_cc_roles_excludes_own_aliases_and_deduplicates(self):
        fixture = self.fixture
        preview = fixture.preview(to=None, subject=None, reply=self.source(), reply_all=True)
        expected_to = ["support@example.test", "colleague@example.test"]
        expected_cc = ["copy@example.test"]
        self.assertEqual(recipients(preview["to"]), expected_to)
        self.assertEqual(recipients(preview["cc"]), expected_cc)
        message = self.mime(preview)
        self.assertEqual(recipients(message.get_all("To", [])), expected_to)
        self.assertEqual(recipients(message.get_all("Cc", [])), expected_cc)
        self.assertEqual(str(message["In-Reply-To"]), "<cc-source@example.test>")
        self.assertEqual(str(message["References"]), "<cc-earlier@example.test> <cc-source@example.test>")
        fixture.apply(preview)
        self.assertEqual(self.envelope(), expected_to + expected_cc)
        self.assertEqual(len(self.envelope()), len(set(self.envelope())))
        self.assertNotIn("owner@example.test", self.envelope())
        self.assertNotIn("alias@example.test", self.envelope())
        self.assertNotIn("customer@example.test", self.envelope())

    def test_reply_all_without_reply_to_uses_from_and_other_participants(self):
        fixture = self.fixture
        preview = fixture.preview(to=None, subject=None, reply=self.source(reply_to=False), reply_all=True)
        self.assertEqual(recipients(preview["to"]), [
            "customer@example.test", "colleague@example.test", "support@example.test"])
        self.assertEqual(recipients(preview["cc"]), ["copy@example.test"])

    def test_reply_all_can_add_dw_with_explicit_complete_cc_list(self):
        fixture = self.fixture
        # Explicit cc replaces the derived list; the agent includes existing DW
        # recipients along with the newly requested person to preserve both.
        preview = fixture.preview(
            to=None, subject=None, reply=self.source(), reply_all=True,
            cc=["Copy <copy@example.test>", "Anna <anna@example.test>"])
        expected_to = ["support@example.test", "colleague@example.test"]
        expected_cc = ["copy@example.test", "anna@example.test"]
        self.assertEqual(recipients(preview["to"]), expected_to)
        self.assertEqual(recipients(preview["cc"]), expected_cc)
        self.assertEqual(recipients(self.mime(preview).get_all("Cc", [])), expected_cc)
        fixture.apply(preview)
        self.assertEqual(self.envelope(), expected_to + expected_cc)
        self.assertNotIn("owner@example.test", self.envelope())
        self.assertNotIn("alias@example.test", self.envelope())

    def test_normal_reply_addresses_reply_to_only_and_does_not_invent_cc(self):
        fixture = self.fixture
        preview = fixture.preview(to=None, subject=None, reply=self.source())
        self.assertEqual(recipients(preview["to"]), ["support@example.test"])
        self.assertEqual(preview["cc"], [])
        self.assertIsNone(self.mime(preview)["Cc"])
        fixture.apply(preview)
        self.assertEqual(self.envelope(), ["support@example.test"])

    def test_normal_reply_without_reply_to_addresses_from_only(self):
        fixture = self.fixture
        preview = fixture.preview(to=None, subject=None, reply=self.source(reply_to=False))
        self.assertEqual(recipients(preview["to"]), ["customer@example.test"])
        self.assertEqual(preview["cc"], [])
        self.assertIsNone(self.mime(preview)["Cc"])

    def test_saved_draft_send_retains_cc_mime_and_smtp_envelope(self):
        fixture = self.fixture
        source = fixture.draft()
        original = fixture.server.messages["Drafts"]["13"]
        preview = fixture.service.send_preview("one", draft=source)
        self.assertTrue(preview["preserved_existing_draft"])
        self.assertEqual(recipients(preview["to"]), ["recipient@example.test"])
        self.assertEqual(recipients(preview["cc"]), ["copy@example.test"])
        self.assertEqual(recipients(self.mime(preview).get_all("Cc", [])), ["copy@example.test"])
        self.assertEqual(fixture.server.smtp_connections, 0)
        result = fixture.apply(preview)
        self.assertEqual(result["status"], "submission_accepted")
        self.assertEqual(self.envelope(), ["recipient@example.test", "copy@example.test"])
        self.assertEqual(fixture.server.submitted, [original])
        self.assertEqual(fixture.server.messages["Drafts"]["13"], original)


if __name__ == "__main__":
    unittest.main()
