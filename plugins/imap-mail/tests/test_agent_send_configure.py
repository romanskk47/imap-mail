"""Agent-first SMTP setup uses existing identity and synthetic checks only."""

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
from imap_mail import cli, mcp, send_setup


CANARY = "SYNTHETIC-AGENT-CONFIGURE-CREDENTIAL-NEVER-RETURN"


class AgentSendConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir(mode=0o700)
        self.config_path = self.private / "settings.json"
        self.profile = {"id": "work", "host": "imap.example.test", "user": "owner@example.test",
                        "entity": "Example owner", "password": CANARY}
        self.write_json(self.profiles / "work.json", self.profile)
        self.entry = {"identity": {key: self.profile[key] for key in ("host", "entity")},
                      "drafts_enabled": True, "drafts_mailbox": "Private drafts", "signature": {"none": True},
                      "default_from": self.profile["user"], "from_addresses": [self.profile["user"]],
                      "attachment_roots": ["unchanged-path"], "unknown_account_setting": {"keep": True}}
        self.entry["identity"]["email"] = self.profile["user"]
        self.config = {"version": 1, "profiles_dir": str(self.profiles), "state_dir": "unchanged-state",
                       "preferred_transport": "imap", "unknown_global_setting": [1, False],
                       "accounts": {"work": self.entry, "unrelated": {"data": "unchanged"}}}
        self.write_json(self.config_path, self.config)
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in ("MAIL_BROKER_CONFIG", "MAIL_PROFILES_JSON"):
            os.environ.pop(key, None)
        self.calls = []
        # This public API must not launch the optional human/browser fallback.
        self.no_browser = patch.object(send_setup.webbrowser, "open", side_effect=AssertionError("No browser"))
        self.browser = self.no_browser.start()
        self.addCleanup(self.no_browser.stop)
        self.no_http = patch.object(send_setup.setup_web, "_QuietServer", side_effect=AssertionError("No HTTP server"))
        self.http = self.no_http.start()
        self.addCleanup(self.no_http.stop)

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def current(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def checker(self, account, smtp):
        self.calls.append((account, copy.deepcopy(smtp)))
        result = {"status": "ready", "messages_sent": 0}
        if smtp["sent_copy"] == "append":
            result["sent_mailbox"] = "Sent"
        return result

    def configure(self, **overrides):
        values = {"config_path": self.config_path, "account": "work", "host": "smtp.example.test",
                  "port": 465, "security": "ssl", "sent_copy": "server",
                  "authorization": "enable_sending", "checker": self.checker}
        values.update(overrides)
        return send_setup.configure_sending(**values)

    def error(self, code, callback):
        with self.assertRaises(Exception) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))
        self.assertNotIn(CANARY, str(caught.exception))

    def test_agent_configuration_checks_then_saves_without_browser_or_mail_write(self):
        profile_before = (self.profiles / "work.json").read_bytes()

        def checked_before_save(account, smtp):
            self.assertEqual(self.config, self.current())
            return self.checker(account, smtp)

        result = self.configure(checker=checked_before_save)
        self.assertEqual("saved", result["status"])
        self.assertTrue(result["send_enabled"])
        self.assertTrue(result["connection_checked"])
        self.assertEqual("work", result["account"])
        self.assertEqual(0, result["messages_sent"])
        self.assertEqual(0, result["mailbox_writes"])
        self.assertEqual([("work", {"host": "smtp.example.test", "port": 465, "security": "ssl",
                                    "sent_copy": "server", "use_imap_credentials": True})], self.calls)
        expected = copy.deepcopy(self.config)
        expected["accounts"]["work"].update(send_enabled=True, smtp=self.calls[0][1])
        self.assertEqual(expected, self.current())
        self.assertEqual(profile_before, (self.profiles / "work.json").read_bytes())
        self.assertEqual(0o600, self.config_path.stat().st_mode & 0o777)
        self.assertNotIn(CANARY, json.dumps(result))
        self.browser.assert_not_called()
        self.http.assert_not_called()

    def test_exact_authorization_marker_is_required_before_checker_or_save(self):
        before = self.config_path.read_bytes()
        for marker in (None, False, True, 1, "true", "enable", "ENABLE_SENDING", "enable_sending "):
            with self.subTest(marker=marker):
                self.error("SEND_ENABLE_AUTHORIZATION_REQUIRED", lambda: self.configure(authorization=marker))
        self.assertEqual([], self.calls)
        self.assertEqual(before, self.config_path.read_bytes())

    def test_confirmed_smtp_hostname_is_normalized_before_configuration(self):
        result = self.configure(host="SMTP.EXAMPLE.TEST")
        self.assertEqual("saved", result["status"])
        self.assertEqual("smtp.example.test", result["smtp"]["host"])
        self.assertEqual("smtp.example.test", self.calls[0][1]["host"])

    def test_requires_all_explicit_public_smtp_settings(self):
        before = self.config_path.read_bytes()
        for key in ("host", "port", "security", "sent_copy"):
            with self.subTest(key=key):
                self.error("SMTP_CONFIGURATION_REQUIRED", lambda: self.configure(**{key: None}))
        self.assertEqual([], self.calls)
        self.assertEqual(before, self.config_path.read_bytes())

    def test_rejects_insecure_or_invalid_settings_before_checker(self):
        cases = [({"security": "plain"}, "SMTP_TLS_REQUIRED"),
                 ({"security": "none"}, "SMTP_TLS_REQUIRED"),
                 ({"port": True}, "INVALID_SMTP_PORT"),
                 ({"port": 0}, "INVALID_SMTP_PORT"),
                 ({"port": 65536}, "INVALID_SMTP_PORT"),
                 ({"host": "127.0.0.1"}, "INVALID_SMTP_HOST"),
                 ({"host": "smtp.example.test\nATTACK"}, "INVALID_SMTP_HOST"),
                 ({"host": "https://smtp.example.test"}, "INVALID_SMTP_HOST"),
                 ({"sent_copy": "guess"}, "INVALID_SENT_COPY_MODE")]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                self.error(code, lambda: self.configure(**overrides))
        self.assertEqual([], self.calls)
        self.assertEqual(self.config, self.current())

    def test_supports_starttls_with_verified_existing_sent_folder(self):
        result = self.configure(port=587, security="starttls", sent_copy="append")
        self.assertEqual("saved", result["status"])
        saved = self.current()["accounts"]["work"]
        self.assertEqual("starttls", saved["smtp"]["security"])
        self.assertEqual("Sent", saved["sent_mailbox"])
        self.assertNotIn("sent_mailbox", saved["smtp"])
        self.assertEqual(self.entry["signature"], saved["signature"])
        self.assertEqual(self.entry["drafts_mailbox"], saved["drafts_mailbox"])

    def test_append_mode_does_not_save_without_a_verified_sent_folder(self):
        self.error("SENT_FOLDER_UNAVAILABLE", lambda: self.configure(
            sent_copy="append", checker=lambda account, smtp: {"status": "ready"}))
        self.assertEqual(self.config, self.current())

    def test_only_an_existing_exact_pinned_account_can_be_configured(self):
        self.error("ACCOUNT_NOT_CONFIGURED", lambda: self.configure(account="missing"))
        changed = copy.deepcopy(self.config)
        changed["accounts"]["work"]["identity"]["entity"] = "Other legal owner"
        self.write_json(self.config_path, changed)
        self.error("ACCOUNT_IDENTITY_MISMATCH", self.configure)
        self.assertEqual([], self.calls)
        self.assertEqual(changed, self.current())

    def test_checker_failure_never_enables_sending_or_leaks_secrets(self):
        def failure(account, smtp):
            raise RuntimeError(CANARY)

        for checker in (failure, lambda account, smtp: {"status": "needs_attention", "error_code": CANARY},
                        lambda account, smtp: None):
            with self.subTest(checker=checker):
                self.error("SMTP_CONNECTION_CHECK_FAILED", lambda: self.configure(checker=checker))
        self.assertEqual(self.config, self.current())

    def test_check_success_output_is_allowlisted(self):
        result = self.configure(checker=lambda account, smtp: {
            "status": "ready", "password": CANARY, "arbitrary_private_debug": CANARY})
        self.assertEqual("saved", result["status"])
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn(CANARY, self.config_path.read_text(encoding="utf-8"))

    def test_same_account_mutation_during_check_fails_without_overwriting(self):
        changed = copy.deepcopy(self.config)
        changed["accounts"]["work"]["signature"] = {"none": False}

        def mutate(account, smtp):
            self.write_json(self.config_path, changed)
            return self.checker(account, smtp)

        self.error("SETUP_CONFIGURATION_CHANGED", lambda: self.configure(checker=mutate))
        self.assertEqual(changed, self.current())

    def test_same_profile_mutation_during_check_fails_without_overwriting(self):
        changed = {**self.profile, "password": "Different synthetic credential"}

        def mutate(account, smtp):
            self.write_json(self.profiles / "work.json", changed)
            return self.checker(account, smtp)

        self.error("SETUP_CONFIGURATION_CHANGED", lambda: self.configure(checker=mutate))
        self.assertEqual(self.config, self.current())
        self.assertEqual(changed, json.loads((self.profiles / "work.json").read_text(encoding="utf-8")))

    def test_unrelated_concurrent_changes_survive_atomic_save(self):
        changed = copy.deepcopy(self.config)
        changed["accounts"]["unrelated"]["newer_setting"] = "keep this"
        changed["new_global_key"] = [1, 2, 3]

        def mutate(account, smtp):
            self.write_json(self.config_path, changed)
            return self.checker(account, smtp)

        result = self.configure(checker=mutate)
        changed["accounts"]["work"].update(send_enabled=True, smtp=result["smtp"])
        self.assertEqual(changed, self.current())

    def test_session_is_closed_after_success_or_failed_check(self):
        original_close = send_setup.SendSetupSession.close
        closed = []

        def close(session):
            closed.append(session)
            return original_close(session)

        with patch.object(send_setup.SendSetupSession, "close", close):
            self.configure()
            self.assertEqual(1, len(closed))
            self.assertTrue(closed[-1]._closed)
            self.assertEqual({}, closed[-1]._candidates)
            self.error("SMTP_CONNECTION_CHECK_FAILED", lambda: self.configure(
                checker=lambda account, smtp: {"status": "needs_attention"}))
            self.assertEqual(2, len(closed))
            self.assertTrue(closed[-1]._closed)
            self.assertEqual({}, closed[-1]._candidates)

    def test_publicly_readable_config_is_rejected_before_checker(self):
        self.config_path.chmod(0o644)
        self.error("PRIVATE_FILE_PERMISSIONS_REQUIRE_600", self.configure)
        self.assertEqual([], self.calls)
        self.assertEqual(self.config, self.current())

    def invoke(self, values):
        service = type("ConfigService", (), {"config_path": self.config_path})()
        return mcp.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                  "params": {"name": "mail_send_configure", "arguments": values}},
                                 service=service)["result"]

    def mcp_values(self):
        return {"account": "work", "host": "smtp.example.test", "port": 465, "security": "ssl",
                "sent_copy": "server", "authorization": "enable_sending"}

    def test_mcp_configuration_uses_exact_service_config_and_offline_checker(self):
        with patch.object(send_setup.SendSetupSession, "_default_check", return_value={"status": "ready"}) as check:
            result = self.invoke(self.mcp_values())
        self.assertFalse(result["isError"])
        self.assertEqual("saved", result["structuredContent"]["status"])
        self.assertEqual(0, result["structuredContent"]["messages_sent"])
        self.assertTrue(self.current()["accounts"]["work"]["send_enabled"])
        check.assert_called_once_with("work", {"host": "smtp.example.test", "port": 465, "security": "ssl",
                                                "sent_copy": "server", "use_imap_credentials": True})
        self.browser.assert_not_called()
        self.http.assert_not_called()

    def test_mcp_schema_requires_every_public_setting_and_authorization(self):
        values = self.mcp_values()
        schema = mcp.TOOL_SCHEMAS["mail_send_configure"]
        self.assertEqual(set(values), set(schema["properties"]))
        self.assertEqual(set(values), set(schema["required"]))
        self.assertFalse(schema["additionalProperties"])
        for omitted in values:
            with self.subTest(omitted=omitted):
                result = self.invoke({key: value for key, value in values.items() if key != omitted})
                self.assertTrue(result["isError"])
                self.assertEqual("INVALID_ARGUMENTS", result["structuredContent"]["code"])
        self.assertEqual(self.config, self.current())

    def test_mcp_rejects_secret_identity_overrides_and_wrong_authorization(self):
        cases = [{**self.mcp_values(), key: CANARY}
                 for key in ("password", "token", "user", "email", "identity", "password_file", "from")]
        cases.extend({**self.mcp_values(), "authorization": value} for value in ("send", "yes", True))
        with patch.object(send_setup, "configure_sending", side_effect=AssertionError("Rejected before runtime")) as configure:
            for values in cases:
                with self.subTest(values=values):
                    result = self.invoke(values)
                    self.assertTrue(result["isError"])
                    self.assertEqual("INVALID_ARGUMENTS", result["structuredContent"]["code"])
                    self.assertNotIn(CANARY, json.dumps(result))
            configure.assert_not_called()
        self.assertEqual(self.config, self.current())

    def test_cli_uses_same_exact_config_and_checked_atomic_save_without_forms(self):
        output = io.StringIO()
        args = ["--config", str(self.config_path), "send-configure", "work", "--host", "smtp.example.test",
                "--port", "465", "--security", "ssl", "--sent-copy", "server",
                "--authorization", "enable_sending"]
        with patch.object(send_setup.SendSetupSession, "_default_check", return_value={"status": "ready"}) as check:
            with contextlib.redirect_stdout(output):
                code = cli.main(args)
        result = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("saved", result["status"])
        self.assertTrue(result["connection_checked"])
        self.assertEqual(0, result["messages_sent"])
        self.assertEqual(0, result["mailbox_writes"])
        self.assertEqual("smtp.example.test", self.current()["accounts"]["work"]["smtp"]["host"])
        self.assertEqual(1, check.call_count)
        self.browser.assert_not_called()
        self.http.assert_not_called()

    def test_cli_connection_failure_is_sanitized_and_never_enables_sending(self):
        output = io.StringIO()
        args = ["--config", str(self.config_path), "send-configure", "work", "--host", "smtp.example.test",
                "--port", "465", "--security", "ssl", "--sent-copy", "server",
                "--authorization", "enable_sending"]
        with patch.object(send_setup.SendSetupSession, "_default_check", side_effect=RuntimeError(CANARY)):
            with contextlib.redirect_stdout(output):
                code = cli.main(args)
        self.assertEqual(1, code)
        self.assertEqual("SMTP_CONNECTION_CHECK_FAILED", json.loads(output.getvalue())["error"]["code"])
        self.assertNotIn(CANARY, output.getvalue())
        self.assertEqual(self.config, self.current())


if __name__ == "__main__":
    unittest.main()
