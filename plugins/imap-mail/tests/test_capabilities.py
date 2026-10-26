"""Offline capability inventory: no credentials, network or mailbox writes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from imap_mail.capabilities import MAX_ACCOUNT_ROWS, capabilities
from imap_mail.core import DraftError
from imap_mail import mcp


class FakeService:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def accounts(self):
        self.calls.append("accounts")
        return {"accounts": self.rows, "status": "ready", "password": "do-not-return"}

    def __getattr__(self, name):
        raise AssertionError("Inventory must not invoke " + name)


def account_row(account="work", **changes):
    return {"account": account, "email": account + "@example.test",
            "host": "imap.example.test", "entity": "example-owner",
            "configured": True, "read_ready": True, "drafts_ready": True,
            "drafts_enabled": True, "signature_configured": True,
            "send_enabled": False, "send_ready": False,
            "connection": "not_checked", "issues": [], "send_issues": [], **changes}


class CapabilitiesTests(unittest.TestCase):
    def test_offline_inventory_uses_only_existing_discovery(self):
        service = FakeService([account_row()])
        result = capabilities(service)
        self.assertEqual(["accounts"], service.calls)
        self.assertEqual(1, result["schema_version"])
        self.assertEqual("not_checked", result["connection"])
        self.assertEqual("not_checked", result["server_capabilities"])
        self.assertEqual(0, result["mailbox_writes"])
        self.assertEqual(0, result["smtp_submissions"])

    def test_configuration_readiness_is_not_live_support(self):
        service = FakeService([account_row(connection=True, send_connection=True)])
        result = capabilities(service)
        row = result["local_configuration"]["accounts"][0]
        self.assertTrue(row["read_ready"])
        self.assertTrue(row["drafts_ready"])
        self.assertEqual("not_checked", row["connection"])
        self.assertIn("not live login", result["local_configuration"]["meaning"])
        self.assertNotIn("send_connection", row)

    def test_read_only_account_without_signature_stays_read_ready(self):
        service = FakeService([account_row(
            drafts_ready=False, drafts_enabled=False, signature_configured=False,
            issues=["DRAFTS_NOT_AUTHORIZED"])])
        result = capabilities(service)
        row = result["local_configuration"]["accounts"][0]
        self.assertTrue(row["read_ready"])
        self.assertFalse(row["drafts_ready"])
        self.assertFalse(row["signature_configured"])
        self.assertIn("mail_read", result["implemented"]["read_mime_and_attachment_manifest"])
        self.assertNotIn("mail_check", result["workflows"]["read"])

    def test_smtp_opt_out_is_not_absence_of_sending_implementation(self):
        result = capabilities(FakeService([account_row()]))
        row = result["local_configuration"]["accounts"][0]
        self.assertFalse(row["send_enabled"])
        self.assertFalse(row["send_ready"])
        self.assertIn("mail_send_apply", result["implemented"]["smtp_send"])

    def test_flags_permission_is_local_explicit_opt_in_not_implementation_gap(self):
        service = FakeService([account_row(), account_row("enabled", flags_enabled=True)])
        result = capabilities(service)
        rows = result["local_configuration"]["accounts"]
        self.assertFalse(rows[0]["flags_enabled"])
        self.assertTrue(rows[1]["flags_enabled"])
        self.assertIn("mail_flags_apply", result["implemented"]["message_flags"])
        self.assertEqual({"configuration_key": "flags_write", "enabled_field": "flags_enabled",
                          "default": "disabled", "automatically_enabled": False},
                         result["permissions"]["message_flags"])
        self.assertEqual(["accounts"], service.calls)

    def test_filtered_account_is_exact_and_exposes_only_one_identity(self):
        rows = [account_row("work"), account_row("private")]
        before = copy.deepcopy(rows)
        result = capabilities(FakeService(rows), account="work")
        found = result["local_configuration"]["accounts"]
        self.assertEqual(["work"], [row["account"] for row in found])
        self.assertNotIn("private@example.test", json.dumps(result))
        self.assertEqual(before, rows)

    def test_unknown_account_never_falls_back(self):
        for selector in ("missing", "Work", "", " work "):
            with self.subTest(selector=selector):
                service = FakeService([account_row("work")])
                with self.assertRaises(DraftError) as caught:
                    capabilities(service, account=selector)
                self.assertEqual("ACCOUNT_NOT_CONFIGURED", caught.exception.code)
                self.assertEqual(["accounts"], service.calls)

    def test_nonstring_account_has_fixed_error_without_discovery(self):
        for selector in (0, True, [], {}, object()):
            with self.subTest(selector=type(selector).__name__):
                service = FakeService([account_row()])
                with self.assertRaises(DraftError) as caught:
                    capabilities(service, account=selector)
                self.assertEqual("ACCOUNT_NOT_CONFIGURED", caught.exception.code)
                self.assertEqual([], service.calls)

    def test_no_profiles_credentials_paths_or_unexpected_fields_returned(self):
        service = FakeService([account_row(
            password="canary-password", smtp={"password": "canary-smtp"},
            profile_path="/private/secrets/profile", credential_path="/private/password",
            signature={"html": "/private/signature.html"},
            from_addresses=["secret-alias@example.test"], arbitrary="canary-field")])
        encoded = json.dumps(capabilities(service))
        for secret in ("canary", "/private/", "secret-alias", "do-not-return"):
            self.assertNotIn(secret, encoded)

    def test_only_existing_tools_are_advertised(self):
        result = capabilities(FakeService([]))
        advertised = {name for tools in result["implemented"].values() for name in tools}
        for key, tools in result["workflows"].items():
            advertised.update(tools.values() if key == "recovery" else tools)
        self.assertTrue(advertised <= set(mcp.TOOL_SCHEMAS))

    def test_real_gaps_are_not_called_forbidden(self):
        result = capabilities(FakeService([]))
        self.assertEqual({"forward", "move",
                          "archive", "permanent_delete", "folder_create_rename_delete",
                          "oauth_mail_account_authentication"}, set(result["not_implemented"]))
        self.assertIn("not a forbidden", result["unknown_or_missing_operation"]["meaning"])
        transports = result["transports"]
        self.assertEqual(["local_mcp_stdio", "local_cli"], transports["implemented"])
        self.assertEqual("not_implemented", transports["authenticated_remote_mcp"])
        self.assertEqual("not_implemented", transports["cloud_connector"])

    def test_no_accounts_and_bounded_default_inventory(self):
        empty = capabilities(FakeService([]))
        self.assertEqual([], empty["local_configuration"]["accounts"])
        rows = [account_row("account-" + str(index)) for index in range(MAX_ACCOUNT_ROWS + 3)]
        result = capabilities(FakeService(rows))
        local = result["local_configuration"]
        self.assertEqual(MAX_ACCOUNT_ROWS + 3, local["accounts_total"])
        self.assertEqual(MAX_ACCOUNT_ROWS, len(local["accounts"]))
        self.assertTrue(local["accounts_truncated"])
        exact = capabilities(FakeService(rows), account="account-" + str(MAX_ACCOUNT_ROWS + 2))
        self.assertEqual(1, exact["local_configuration"]["accounts_total"])
        self.assertFalse(exact["local_configuration"]["accounts_truncated"])
        self.assertLess(len(json.dumps(capabilities(FakeService([account_row()])))), 8000)

    def test_malformed_inventory_has_fixed_public_error(self):
        for malformed in (None, {}, ["not-a-row"], [{"account": 7}]):
            service = FakeService(malformed)
            with self.subTest(malformed=malformed), self.assertRaises(DraftError) as caught:
                capabilities(service)
            self.assertEqual("INVALID_ACCOUNT_INVENTORY", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
