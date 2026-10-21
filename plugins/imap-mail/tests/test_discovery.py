"""Offline discovery and one-account readiness checks with synthetic profiles."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError, DraftService
from test_core import Server


class AccountDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles_dir = self.root / "profiles"
        self.profiles_dir.mkdir(mode=0o700)
        self.config_path = self.root / "settings.json"
        self.profiles = {}
        self.entries = {}
        for number in range(1, 8):
            account = f"account-{number}"
            email_address = f"owner-{number}@example.test"
            profile = {"id": account, "host": f"imap-{number}.example.test",
                       "user": email_address, "entity": f"entity-{number}",
                       "password_env": f"SYNTHETIC_UNUSED_PASSWORD_{number}"}
            self.profiles[account] = profile
            self.write_json(self.profiles_dir / (account + ".json"), profile)
            self.entries[account] = {
                "identity": {"email": email_address, "host": profile["host"], "entity": profile["entity"]},
                "from_addresses": [email_address], "default_from": email_address,
                "drafts_enabled": True, "signature": {"none": True}, "attachment_roots": []}
        self.config = {"version": 1, "profiles_dir": str(self.profiles_dir),
                       "state_dir": str(self.root / "state"), "accounts": copy.deepcopy(self.entries)}
        self.write_json(self.config_path, self.config)
        self.server = Server()
        self.connected = []

        def factory(profile):
            self.connected.append(profile["id"])
            return self.server.reader(profile)

        self.service = DraftService(config_path=self.config_path, reader_factory=factory)

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def save_config(self):
        self.write_json(self.config_path, self.config)

    def rows(self, response):
        return {row["account"]: row for row in response["accounts"]}

    def test_all_seven_profiles_are_discovered_without_network_or_credential_output(self):
        response = self.service.accounts()
        self.assertEqual(response["status"], "ready")
        self.assertEqual(response["discovered_accounts"], 7)
        self.assertEqual(response["configured_accounts"], 7)
        self.assertEqual(set(self.rows(response)), set(self.profiles))
        self.assertTrue(all(row["read_ready"] and row["drafts_ready"] for row in response["accounts"]))
        self.assertTrue(all(row["connection"] == "not_checked" for row in response["accounts"]))
        self.assertEqual(self.server.connections, 0)
        self.assertNotIn("password", json.dumps(response).casefold())
        self.assertNotIn("SYNTHETIC_UNUSED", json.dumps(response))
        self.assertFalse((self.root / "state").exists())

    def test_missing_configuration_still_discovers_profiles_without_enabling_writes(self):
        service = DraftService(config_path=self.root / "absent.json", reader_factory=self.server.reader)
        with patch("imap_mail.core.read_access.profiles", return_value=self.profiles):
            response = service.accounts()
        self.assertEqual(response["status"], "setup_needed")
        self.assertEqual(response["configured_accounts"], 0)
        self.assertEqual(len(response["accounts"]), 7)
        for row in response["accounts"]:
            self.assertFalse(row["configured"])
            self.assertFalse(row["drafts_enabled"])
            self.assertFalse(row["drafts_ready"])
            self.assertEqual(row["from_addresses"], [])
            self.assertIsNone(row["default_from"])
            self.assertEqual(row["issues"], ["ACCOUNT_NOT_CONFIGURED"])
        self.assertEqual(self.server.connections, 0)

    def test_empty_accounts_configuration_discovers_profiles_but_stays_setup_needed(self):
        self.config["accounts"] = {}
        self.save_config()
        response = self.service.accounts()
        self.assertEqual(response["status"], "setup_needed")
        self.assertEqual(len(response["accounts"]), 7)
        self.assertTrue(all(row["status"] == "setup_needed" for row in response["accounts"]))

    def test_identity_mismatch_or_malformed_entry_does_not_hide_other_accounts(self):
        self.config["accounts"]["account-2"]["identity"]["entity"] = "wrong-entity"
        self.config["accounts"]["account-3"] = "invalid configuration"
        self.save_config()
        response = self.service.accounts()
        rows = self.rows(response)
        self.assertEqual(response["status"], "needs_attention")
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows["account-2"]["issues"], ["ACCOUNT_IDENTITY_MISMATCH"])
        self.assertEqual(rows["account-3"]["issues"], ["INVALID_ACCOUNT_CONFIGURATION"])
        self.assertEqual(rows["account-1"]["status"], "ready")
        self.assertFalse(rows["account-2"]["read_ready"])
        self.assertFalse(rows["account-3"]["drafts_ready"])
        self.assertEqual(self.server.connections, 0)

    def test_unconfigured_and_missing_profiles_have_individual_actionable_status(self):
        del self.config["accounts"]["account-7"]
        self.config["accounts"]["missing-profile"] = copy.deepcopy(self.entries["account-1"])
        self.save_config()
        rows = self.rows(self.service.accounts())
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows["account-7"]["issues"], ["ACCOUNT_NOT_CONFIGURED"])
        self.assertFalse(rows["account-7"]["drafts_enabled"])
        self.assertEqual(rows["missing-profile"]["issues"], ["ACCOUNT_PROFILE_UNAVAILABLE"])

    def test_missing_signature_files_are_isolated_and_do_not_invent_no_signature(self):
        self.config["accounts"]["account-2"]["signature"] = {"plain": str(self.root / "absent-signature.txt")}
        del self.config["accounts"]["account-3"]["signature"]
        self.save_config()
        rows = self.rows(self.service.accounts())
        self.assertEqual(rows["account-2"]["issues"], ["SIGNATURE_FILES_UNAVAILABLE"])
        self.assertEqual(rows["account-3"]["issues"], ["SIGNATURE_CONFIGURATION_REQUIRED"])
        self.assertTrue(rows["account-2"]["read_ready"])
        self.assertFalse(rows["account-2"]["drafts_ready"])
        self.assertTrue(rows["account-1"]["drafts_ready"])

    def test_preferred_transport_is_only_reported_when_explicitly_configured(self):
        self.assertNotIn("preferred_transport", self.service.accounts())
        self.config["preferred_transport"] = "imap"
        self.save_config()
        self.assertEqual(self.service.accounts()["preferred_transport"], "imap")

    def test_check_logs_into_only_the_requested_account_and_cannot_mutate_mail(self):
        result = self.service.check("account-5")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["connection"])
        self.assertEqual(result["identity"]["email"], "owner-5@example.test")
        self.assertEqual(result["identity"]["entity"], "entity-5")
        self.assertEqual(result["drafts_mailbox"], "Drafts")
        self.assertEqual(result["uidvalidity"], "9")
        self.assertEqual(result["signature_mode"], "none")
        self.assertEqual(result["signature_inline_count"], 0)
        self.assertEqual(result["mailbox_writes"], 0)
        self.assertEqual(self.connected, ["account-5"])
        self.assertEqual(self.server.append_calls, 0)
        self.assertEqual(self.server.commands, ["EXAMINE", "LOGOUT"])
        self.assertFalse((self.root / "state").exists())

    def test_check_validates_signature_and_inline_assets_before_connecting(self):
        plain = self.root / "signature.txt"
        plain.write_text("Account 5 approved signature", encoding="utf-8")
        rich = self.root / "signature.html"
        rich.write_text('<b>Account 5</b><img src="cid:logo-five">', encoding="utf-8")
        image = self.root / "logo-five.png"
        image.write_bytes(b"synthetic inline image")
        self.config["accounts"]["account-5"]["signature"] = {
            "plain": str(plain), "html": str(rich), "inline": {"logo-five": str(image)}}
        self.save_config()
        result = self.service.check("account-5")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["signature_mode"], "configured")
        self.assertEqual(result["signature_inline_count"], 1)
        self.connected.clear()
        image.unlink()
        result = self.service.check("account-5")
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["error_code"], "SIGNATURE_FILES_UNAVAILABLE")
        self.assertFalse(result["connection"])
        self.assertEqual(self.connected, [])

    def test_check_returns_account_local_errors_without_connecting_to_another_account(self):
        self.config["accounts"]["account-2"]["identity"]["host"] = "wrong-host.example.test"
        self.save_config()
        failed = self.service.check("account-2")
        self.assertEqual(failed["error_code"], "ACCOUNT_IDENTITY_MISMATCH")
        self.assertFalse(failed["connection"])
        self.assertEqual(self.connected, [])
        self.assertEqual(self.service.check("account-1")["status"], "ready")
        self.assertEqual(self.connected, ["account-1"])

    def test_check_unknown_account_is_consistently_rejected_before_login(self):
        with self.assertRaises(DraftError) as raised:
            self.service.check("unknown")
        self.assertEqual(raised.exception.code, "ACCOUNT_NOT_CONFIGURED")
        self.assertEqual(self.connected, [])

    def test_check_transport_failures_are_sanitized(self):
        def failed_reader(_profile):
            raise ConnectionError("Synthetic error with private server response")

        service = DraftService(config_path=self.config_path, reader_factory=failed_reader)
        result = service.check("account-1")
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["error_code"], "CONNECTION_CHECK_FAILED")
        self.assertFalse(result["connection"])
        self.assertNotIn("private server response", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
