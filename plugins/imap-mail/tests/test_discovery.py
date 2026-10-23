"""Offline discovery and one-account readiness checks with synthetic profiles."""

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError, DraftService
from imap_mail import cli, mcp, read_access
from imap_mail.flags import FlagService
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
        with patch("imap_mail.core.read_access.profile_inventory", return_value=read_access.ProfileInventory(self.profiles, {}, ())):
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

    def test_foreign_incomplete_profile_is_isolated_in_cli_and_exact_mcp_check(self):
        broken = {**self.profiles["account-2"]}
        del broken["user"]
        self.write_json(self.profiles_dir / "account-2.json", broken)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.main(["accounts"], service=self.service)
        self.assertEqual(0, exit_code)
        result = json.loads(output.getvalue())
        rows = self.rows(result)
        self.assertEqual(7, result["discovered_accounts"])
        self.assertEqual("needs_attention", result["status"])
        self.assertEqual(["INCOMPLETE_PROFILE"], rows["account-2"]["issues"])
        self.assertFalse(rows["account-2"]["read_ready"])
        self.assertTrue(rows["account-1"]["read_ready"])
        checked = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "mail_check", "arguments": {"account": "account-1", "operation": "read"}}},
            service=self.service)["result"]
        self.assertFalse(checked["isError"])
        self.assertEqual("ready", checked["structuredContent"]["status"])
        self.assertEqual(["account-1"], self.connected)
        self.assertEqual(0, self.server.append_calls)

    def test_foreign_invalid_injected_profile_keeps_exact_valid_account_usable(self):
        broken = {**self.profiles["account-2"], "tls": False}
        self.config.pop("profiles_dir")
        self.save_config()
        with patch.dict(os.environ, {"MAIL_PROFILES_JSON": json.dumps([self.profiles["account-1"], broken])}):
            rows = self.rows(self.service.accounts())
            self.assertTrue(rows["account-1"]["read_ready"])
            self.assertEqual(["TLS_REQUIRED"], rows["account-2"]["issues"])
            self.assertEqual("ready", self.service.check("account-1", operation="read")["status"])
            self.assertEqual("TLS_REQUIRED", self.service.check("account-2", operation="read")["error_code"])
        self.assertEqual(["account-1"], self.connected)

    def test_unidentifiable_profile_reports_global_issue_and_blocks_resolution(self):
        (self.profiles_dir / "broken.json").write_text('{"private": "SYNTHETIC-UNPRINTABLE', encoding="utf-8")
        result = self.service.accounts()
        self.assertEqual("needs_attention", result["status"])
        self.assertEqual(["INVALID_PROFILE_JSON"], result["profile_discovery_issues"])
        self.assertEqual(7, len(result["accounts"]))
        self.assertTrue(all(not row["read_ready"] for row in result["accounts"]))
        self.assertEqual("PROFILE_DISCOVERY_INCOMPLETE", self.service.check("account-1", operation="read")["error_code"])
        self.assertNotIn("SYNTHETIC-UNPRINTABLE", json.dumps(result))
        self.assertEqual([], self.connected)

    def test_duplicate_ids_block_only_that_identity_for_both_document_orders(self):
        self.config.pop("profiles_dir")
        self.save_config()
        valid = self.profiles["account-2"]
        invalid = {**valid, "tls": False}
        for duplicates in ([valid, invalid], [invalid, valid], [valid, valid], [valid, invalid, valid]):
            with self.subTest(duplicates=duplicates), patch.dict(os.environ, {
                    "MAIL_PROFILES_JSON": json.dumps([self.profiles["account-1"], *duplicates])}):
                rows = self.rows(self.service.accounts())
                self.assertEqual(["DUPLICATE_ACCOUNT_ID"], rows["account-2"]["issues"])
                self.assertFalse(rows["account-2"]["read_ready"])
                self.assertTrue(rows["account-1"]["read_ready"])
                self.assertEqual("DUPLICATE_ACCOUNT_ID", self.service.check("account-2", operation="read")["error_code"])
        self.assertEqual([], self.connected)

    def test_insecure_selected_profile_has_a_local_issue_without_secret_output(self):
        profile = {**self.profiles["account-2"], "password": "SYNTHETIC-DO-NOT-RETURN"}
        profile.pop("password_env")
        path = self.profiles_dir / "account-2.json"
        self.write_json(path, profile)
        path.chmod(0o644)
        result = self.service.accounts()
        rows = self.rows(result)
        self.assertEqual(["SECRET_FILE_PERMISSIONS_REQUIRE_600"], rows["account-2"]["issues"])
        self.assertTrue(rows["account-1"]["read_ready"])
        self.assertEqual("SECRET_FILE_PERMISSIONS_REQUIRE_600", self.service.check("account-2", operation="read")["error_code"])
        self.assertNotIn("SYNTHETIC-DO-NOT-RETURN", json.dumps(result))
        self.assertEqual([], self.connected)

    def test_ambiguous_json_id_cannot_select_a_valid_candidate(self):
        path = self.profiles_dir / "ambiguous.json"
        path.write_text('{"id":"account-1","id":"other","user":"SYNTHETIC-PRIVATE"}', encoding="utf-8")
        result = self.service.accounts()
        self.assertEqual(["INVALID_PROFILE_JSON"], result["profile_discovery_issues"])
        self.assertEqual("PROFILE_DISCOVERY_INCOMPLETE", self.service.check("account-1", operation="read")["error_code"])
        self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(result))
        self.assertEqual([], self.connected)

    def test_empty_or_unidentifiable_injected_data_never_falls_back_to_files(self):
        self.config.pop("profiles_dir")
        self.save_config()
        for injected, code in (("", "INVALID_PROFILE_JSON"), ("null", "INVALID_ACCOUNT_ID"),
                               ('[{"user":"SYNTHETIC-PRIVATE"}]', "INVALID_ACCOUNT_ID")):
            with self.subTest(injected=injected), patch.dict(os.environ, {
                    "MAIL_PROFILES_JSON": injected, "MAIL_PROFILES_DIR": str(self.profiles_dir)}):
                result = self.service.accounts()
                self.assertEqual(0, result["discovered_accounts"])
                self.assertEqual([code], result["profile_discovery_issues"])
                self.assertEqual("PROFILE_DISCOVERY_INCOMPLETE", self.service.check("account-1", operation="read")["error_code"])
                self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(result))
        self.assertEqual([], self.connected)

    def test_explicit_profile_directory_remains_the_selected_source(self):
        with patch.dict(os.environ, {"MAIL_PROFILES_JSON": "invalid injected source"}):
            result = self.service.accounts()
            self.assertEqual("ready", result["status"])
            self.assertEqual([], result["profile_discovery_issues"])
            self.assertEqual(7, result["discovered_accounts"])
            self.assertEqual("ready", self.service.check("account-1", operation="read")["status"])
        self.assertEqual(["account-1"], self.connected)

    def test_full_service_discovery_preserves_smtp_and_flags_isolation(self):
        for account in ("account-1", "account-2"):
            self.config["accounts"][account].update(flags_write=True, send_enabled=True,
                smtp={"host": "smtp.example.test", "port": 465, "security": "ssl",
                      "use_imap_credentials": True, "sent_copy": "server"})
        self.save_config()
        self.write_json(self.profiles_dir / "account-2.json", {**self.profiles["account-2"], "tls": False})
        service = FlagService(config_path=self.config_path, reader_factory=self.server.reader)
        result = mcp.dispatch_tool("mail_accounts", {}, service=service)
        rows = self.rows(result)
        self.assertTrue(rows["account-1"]["read_ready"])
        self.assertTrue(rows["account-1"]["send_ready"])
        self.assertTrue(rows["account-1"]["flags_enabled"])
        self.assertFalse(rows["account-2"]["read_ready"])
        self.assertFalse(rows["account-2"]["send_ready"])
        self.assertFalse(rows["account-2"]["flags_enabled"])
        self.assertEqual(["TLS_REQUIRED"], rows["account-2"]["send_issues"])
        self.assertEqual(["TLS_REQUIRED"], rows["account-2"]["flags_issues"])
        self.assertEqual(0, self.server.connections)

    def test_capabilities_exposes_global_discovery_error_without_raw_profile(self):
        (self.profiles_dir / "broken.json").write_text("SYNTHETIC-PRIVATE", encoding="utf-8")
        result = mcp.dispatch_tool("mail_capabilities", {"account": "account-1"}, service=self.service)
        configuration = result["local_configuration"]
        self.assertEqual(["INVALID_PROFILE_JSON"], configuration["profile_discovery_issues"])
        self.assertFalse(configuration["accounts"][0]["read_ready"])
        self.assertEqual(["PROFILE_DISCOVERY_INCOMPLETE"], configuration["accounts"][0]["issues"])
        self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(result))
        self.assertEqual([], self.connected)

    def test_unconfigured_invalid_id_remains_visible_with_its_own_issue(self):
        broken = {"id": "unconfigured-broken", "host": "imap.example.test", "entity": "other"}
        self.write_json(self.profiles_dir / "unconfigured.json", broken)
        rows = self.rows(self.service.accounts())
        self.assertFalse(rows["unconfigured-broken"]["configured"])
        self.assertEqual(["INCOMPLETE_PROFILE"], rows["unconfigured-broken"]["issues"])
        self.assertTrue(rows["account-1"]["read_ready"])


if __name__ == "__main__":
    unittest.main()
