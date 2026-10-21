"""SMTP opt-in is offline-tested: synthetic profiles, fake checker, loopback UI."""

import copy
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import send_setup, setup_web


CANARY = "SYNTHETIC-IMAP-CREDENTIAL-NEVER-RETURN"


class SendSetupTests(unittest.TestCase):
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
        self.entry = {"identity": {"email": self.profile["user"], "host": self.profile["host"], "entity": self.profile["entity"]},
                      "drafts_enabled": True, "drafts_mailbox": "Private drafts", "signature": {"none": True},
                      "attachment_roots": ["unchanged-path"], "default_from": self.profile["user"],
                      "from_addresses": [self.profile["user"]], "unknown_account_setting": [1, {"keep": True}]}
        self.config = {"version": 1, "profiles_dir": str(self.profiles), "state_dir": "unchanged-state",
                       "preferred_transport": "imap", "unknown_global_setting": {"keep": [1, False]},
                       "accounts": {"work": self.entry, "unrelated": {"data": "unchanged"}}}
        self.write_json(self.config_path, self.config)
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in ("MAIL_BROKER_CONFIG", "MAIL_PROFILES_JSON"):
            os.environ.pop(key, None)
        self.calls = []
        self.session = self.make_session()
        self.payload = {"host": "smtp.example.test", "port": 465, "security": "ssl", "sent_copy": "server",
                        "host_confirmed": True, "host_confirmation": "smtp.example.test",
                        "credential_reuse_consent": True, "enable_sending_consent": True}

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def checker(self, account, smtp):
        self.calls.append((account, smtp))
        result = {"status": "ready", "messages_sent": 0}
        if smtp["sent_copy"] == "append":
            result["sent_mailbox"] = smtp.get("sent_mailbox", "Sent")
        return result

    def make_session(self, checker=None):
        session = send_setup.SendSetupSession(self.config_path, "work", checker=checker or self.checker)
        self.addCleanup(session.close)
        return session

    def error(self, code, fn):
        with self.assertRaises(Exception) as caught:
            fn()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))
        self.assertNotIn(CANARY, str(caught.exception))

    def check(self, **overrides):
        payload = {**self.payload, **overrides}
        return self.session.check(payload)

    def test_context_has_only_pinned_identity_without_any_password_access_or_host_guess(self):
        with patch("imap_mail.read_access.password", side_effect=AssertionError("No credential read")):
            value = self.session.context()
        self.assertEqual({"account": "work", "email": "owner@example.test", "entity": "Example owner"}, value)
        self.assertNotIn(CANARY, json.dumps(value))
        self.assertNotIn("smtp", value)
        self.assertEqual([], self.calls)
        self.error("SMTP_HOST_REQUIRED", lambda: self.session.suggest("owner@example.test"))

    def test_only_existing_exact_account_can_open(self):
        self.error("ACCOUNT_NOT_CONFIGURED", lambda: send_setup.SendSetupSession(self.config_path, "missing", self.checker))
        changed = copy.deepcopy(self.config)
        changed["accounts"]["work"]["identity"]["entity"] = "Wrong legal owner"
        self.write_json(self.config_path, changed)
        self.error("ACCOUNT_IDENTITY_MISMATCH", lambda: send_setup.SendSetupSession(self.config_path, "work", self.checker))
        self.assertEqual([], self.calls)

    def test_every_host_needs_exact_retype_even_familiar_smtp_provider(self):
        for changes in ({"host_confirmation": None}, {"host_confirmation": "SMTP.EXAMPLE.TEST"},
                        {"host_confirmed": False}, {"host": "smtp.gmail.com", "host_confirmation": "smtp.example.test"}):
            with self.subTest(changes=changes):
                self.error("HOST_RETYPE_REQUIRED", lambda: self.check(**changes))
        self.assertEqual([], self.calls)
        checked = self.check(host="smtp.gmail.com", host_confirmation="smtp.gmail.com")
        self.assertEqual("checked", checked["status"])
        self.assertEqual("smtp.gmail.com", self.calls[0][1]["host"])

    def test_reuse_and_send_permission_are_both_separate_required_booleans(self):
        for key, code in (("credential_reuse_consent", "CREDENTIAL_REUSE_CONSENT_REQUIRED"),
                          ("enable_sending_consent", "SEND_ENABLE_CONSENT_REQUIRED")):
            for value in (False, None, 1, "true"):
                with self.subTest(key=key, value=value):
                    self.error(code, lambda: self.check(**{key: value}))
        self.assertEqual([], self.calls)

    def test_rejects_secret_identity_and_profile_overrides_before_checker(self):
        for key in ("password", "token", "account", "email", "user", "identity", "signature", "password_file", "from"):
            with self.subTest(key=key):
                self.error("INVALID_SEND_SETUP_INPUT", lambda: self.check(**{key: CANARY}))
        self.assertEqual([], self.calls)

    def test_rejects_unencrypted_or_invalid_smtp_settings(self):
        cases = [({"security": "plain"}, "SMTP_TLS_REQUIRED"), ({"security": None}, "SMTP_CONFIGURATION_REQUIRED"),
                 ({"port": True}, "INVALID_SMTP_PORT"), ({"port": 0}, "INVALID_SMTP_PORT"),
                 ({"port": 65536}, "INVALID_SMTP_PORT"), ({"host": "127.0.0.1"}, "INVALID_SMTP_HOST"),
                 ({"host": "smtp.example.test\nATTACK"}, "INVALID_SMTP_HOST"),
                 ({"host": "http://smtp.example.test"}, "INVALID_SMTP_HOST"),
                 ({"sent_copy": "guess"}, "INVALID_SENT_COPY_MODE")]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                self.error(code, lambda: self.check(**overrides))
        self.assertEqual([], self.calls)

    def test_check_does_not_write_or_enable_send_and_allowlists_output(self):
        before = self.config_path.read_bytes()
        checked = self.check()
        self.assertEqual("checked", checked["status"])
        self.assertEqual(0, checked["messages_sent"])
        self.assertEqual(0, checked["mailbox_writes"])
        self.assertNotIn(CANARY, json.dumps(checked))
        self.assertEqual(before, self.config_path.read_bytes())
        self.assertTrue(self.calls[0][1]["use_imap_credentials"])

    def test_save_changes_only_sending_fields_and_keeps_private_permissions(self):
        before_profile = (self.profiles / "work.json").read_bytes()
        checked = self.check()
        saved = self.session.save(checked["check_id"])
        expected = copy.deepcopy(self.config)
        expected["accounts"]["work"].update(send_enabled=True, smtp=checked["smtp"])
        self.assertEqual(expected, json.loads(self.config_path.read_text()))
        self.assertEqual(before_profile, (self.profiles / "work.json").read_bytes())
        self.assertEqual(0o600, self.config_path.stat().st_mode & 0o777)
        self.assertEqual("saved", saved["status"])
        self.assertEqual(0, saved["messages_sent"])
        self.error("CHECK_EXPIRED_OR_UNAVAILABLE", lambda: self.session.save(checked["check_id"]))

    def test_append_sent_copy_uses_only_verified_existing_mailbox(self):
        checked = self.check(sent_copy="append")
        self.assertEqual("Sent", checked["sent_mailbox"])
        self.assertNotIn("sent_mailbox", checked["smtp"])
        self.session.save(checked["check_id"])
        self.assertEqual("Sent", json.loads(self.config_path.read_text())["accounts"]["work"]["sent_mailbox"])
        no_folder = self.make_session(lambda account, smtp: {"status": "ready"})
        self.error("SENT_FOLDER_UNAVAILABLE", lambda: no_folder.check({**self.payload, "sent_copy": "append"}))
        self.error("INVALID_SEND_SETUP_INPUT", lambda: self.check(sent_mailbox="Sent"))

    def test_failures_are_fixed_codes_no_secret_or_checker_output_leak(self):
        def failure(account, smtp):
            raise RuntimeError(CANARY)
        session = self.make_session(failure)
        self.error("SMTP_CONNECTION_CHECK_FAILED", lambda: session.check(self.payload))
        session = self.make_session(lambda account, smtp: {"status": "needs_attention", "error_code": CANARY})
        self.error("SMTP_CONNECTION_CHECK_FAILED", lambda: session.check(self.payload))
        session = self.make_session(lambda account, smtp: {"status": "ready", "password": CANARY, "arbitrary": CANARY})
        self.assertNotIn(CANARY, json.dumps(session.check(self.payload)))

    def test_three_checks_limit_and_new_check_invalidates_older_candidate(self):
        first = self.check()
        second = self.check()
        self.error("CHECK_EXPIRED_OR_UNAVAILABLE", lambda: self.session.save(first["check_id"]))
        # Invalid save clears staged checks rather than selecting another one.
        self.error("CHECK_EXPIRED_OR_UNAVAILABLE", lambda: self.session.save(second["check_id"]))
        third = self.check()
        self.error("CHECK_ATTEMPT_LIMIT", self.check)
        self.assertEqual(3, len(self.calls))
        self.error("CHECK_EXPIRED_OR_UNAVAILABLE", lambda: self.session.save(third["check_id"]))

    def test_expired_check_and_cancel_never_save(self):
        checked = self.check()
        self.session._candidates[checked["check_id"]]["created"] -= 901
        self.error("CHECK_EXPIRED_OR_UNAVAILABLE", lambda: self.session.save(checked["check_id"]))
        self.session.close()
        self.error("SETUP_SESSION_CLOSED", self.check)
        self.assertNotIn("send_enabled", json.loads(self.config_path.read_text())["accounts"]["work"])

    def test_same_account_or_profile_changes_after_check_fail_closed(self):
        for which in ("entry", "profile"):
            with self.subTest(which=which):
                self.write_json(self.config_path, self.config)
                self.write_json(self.profiles / "work.json", self.profile)
                session = self.make_session()
                checked = session.check(self.payload)
                if which == "entry":
                    updated = copy.deepcopy(self.config)
                    updated["accounts"]["work"]["signature"] = {"none": False}
                    self.write_json(self.config_path, updated)
                else:
                    self.write_json(self.profiles / "work.json", {**self.profile, "password": "Changed synthetic credential"})
                self.error("SETUP_CONFIGURATION_CHANGED", lambda: session.save(checked["check_id"]))

    def test_unrelated_concurrent_changes_survive_save(self):
        checked = self.check()
        updated = copy.deepcopy(self.config)
        updated["accounts"]["unrelated"]["newer_setting"] = "keep this"
        updated["new_global_key"] = [1, 2, 3]
        self.write_json(self.config_path, updated)
        self.session.save(checked["check_id"])
        current = json.loads(self.config_path.read_text())
        self.assertEqual(updated["accounts"]["unrelated"], current["accounts"]["unrelated"])
        self.assertEqual([1, 2, 3], current["new_global_key"])

    def test_config_symlink_or_public_permissions_rejected(self):
        self.config_path.chmod(0o644)
        self.error("PRIVATE_FILE_PERMISSIONS_REQUIRE_600", self.check)
        self.config_path.chmod(0o600)
        target = self.private / "real.json"
        self.config_path.rename(target)
        self.config_path.symlink_to(target)
        self.error("UNSAFE_SETUP_PATH", self.check)
        self.assertEqual([], self.calls)

    def test_real_send_service_accepts_saved_schema_for_both_sent_copy_modes(self):
        from imap_mail.send import SendService
        import ssl

        operations = []

        class Reader:
            def __init__(self, profile):
                self.profile = profile

            def folders(self):
                operations.append("LIST")
                return {"folders": [{"name": "Sent", "kind": "sent", "flags": ["\\Sent"], "selectable": True}]}

            def select(self, mailbox):
                operations.append(("EXAMINE", mailbox))
                return "7", 0

            def close(self):
                operations.append("IMAP LOGOUT")

        class SMTP:
            def login(self, username, password):
                if username != "owner@example.test" or password != CANARY:
                    raise AssertionError("Identity changed")
                operations.append("SMTP AUTH")

            def quit(self):
                operations.append("SMTP QUIT")

            def __getattr__(self, name):
                raise AssertionError("SMTP check tried a submission operation")

        def factory(settings, context, timeout):
            self.assertTrue(context.check_hostname)
            self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
            self.assertEqual("owner@example.test", settings["username"])
            operations.append(("TLS", settings["host"], settings["security"]))
            return SMTP()

        for mode in ("server", "append"):
            with self.subTest(sent_copy=mode):
                self.write_json(self.config_path, self.config)
                service = SendService(self.config_path, reader_factory=Reader, smtp_factory=factory)
                session = self.make_session(checker=service.check_configuration)
                checked = session.check({**self.payload, "sent_copy": mode})
                self.assertEqual("checked", checked["status"])
                session.save(checked["check_id"])
                active = service.send_check("work")
                self.assertEqual("ready", active["status"], active)
                self.assertEqual(0, active["smtp_submissions"])
                self.assertEqual(0, active["mailbox_writes"])
                saved_entry = json.loads(self.config_path.read_text())["accounts"]["work"]
                self.assertEqual({"host", "port", "security", "use_imap_credentials", "sent_copy"}, set(saved_entry["smtp"]))
                if mode == "append":
                    self.assertEqual("Sent", saved_entry["sent_mailbox"])
                self.assertEqual(self.entry["signature"], saved_entry["signature"])
                self.assertEqual(self.entry["drafts_mailbox"], saved_entry["drafts_mailbox"])
        self.assertEqual(4, operations.count("SMTP AUTH"))
        self.assertEqual(4, operations.count("SMTP QUIT"))


class FakeSession:
    def __init__(self, config_path=None, account=None):
        self.account, self.calls, self.closed = account, [], False

    def context(self):
        return {"account": self.account, "email": "owner@example.test", "entity": "Example"}

    def suggest(self, email):
        raise send_setup.SendSetupError("SMTP_HOST_REQUIRED")

    def check(self, value):
        self.calls.append(copy.deepcopy(value))
        return {"status": "checked", "check_id": "a" * 32, "email": "owner@example.test", "smtp": {"host": value["host"]}}

    def save(self, check_id):
        return {"status": "saved", "account": self.account, "messages_sent": 0}

    def close(self):
        self.closed = True


class SendSetupWebTests(unittest.TestCase):
    def setUp(self):
        self.handle = send_setup._launch_send_setup(account="work", host="smtp.example.test", session_factory=FakeSession)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for handle in list(setup_web._REGISTRY):
            if isinstance(handle, send_setup._SendController):
                handle.close()
                handle.worker.join(2)

    def call(self, path, payload=None, headers=None, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.handle.server.server_port, timeout=3)
        values = {"Host": self.handle.host, "Origin": self.handle.origin,
                  "X-Setup-Token": self.handle.token, "Content-Type": "application/json"}
        values.update(headers or {})
        body = json.dumps(payload if payload is not None else {}).encode()
        connection.request(method, path, body=body if method == "POST" else None, headers=values)
        response = connection.getresponse()
        status, data, response_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, UnicodeError):
            pass
        return status, data, response_headers

    def test_reuses_exact_host_origin_token_and_csp_guards(self):
        for headers in ({"Host": "evil.test"}, {"Origin": "https://evil.test"}, {"X-Setup-Token": "wrong"}):
            self.assertEqual(403, self.call("/api/check", {"host": "smtp.example.test"}, headers)[0])
        self.assertEqual([], self.handle.session.calls)
        for path in ("/", "/send-setup.js", "/send-setup.css"):
            status, content, headers = self.call(path, method="GET")
            self.assertEqual(200, status)
            self.assertNotIn(self.handle.token.encode(), content)
            self.assertEqual("no-store, private", headers["Cache-Control"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(404, self.call("/setup.js", method="GET")[0])
        self.assertEqual(404, self.call("/../send_setup.py", method="GET")[0])

    def test_context_has_public_identity_and_always_requires_host_retype(self):
        status, value, _ = self.call("/api/context", {})
        self.assertEqual(200, status)
        self.assertTrue(value["require_host_retype"])
        self.assertEqual("smtp.example.test", value["prefill"]["host"])
        self.assertNotIn("password", json.dumps(value))
        self.assertEqual(400, self.call("/api/context", {"extra": "reject"})[0])

    def test_every_check_requires_hostname_retype_before_backend(self):
        value = {"host": "smtp.gmail.com"}
        self.assertEqual("HOST_RETYPE_REQUIRED", self.call("/api/check", value)[1]["error_code"])
        self.assertEqual("HOST_RETYPE_REQUIRED", self.call("/api/check", {**value, "agent_host_confirmation": "smtp.example.test"})[1]["error_code"])
        self.assertEqual([], self.handle.session.calls)
        self.assertEqual("checked", self.call("/api/check", {**value, "agent_host_confirmation": "smtp.gmail.com"})[1]["status"])
        self.assertNotIn("agent_host_confirmation", self.handle.session.calls[0])

    def test_explicit_local_save_wire_consent_required_and_cancel_safe(self):
        self.assertEqual(400, self.call("/api/save", {"check_id": "a" * 32})[0])
        self.assertEqual(400, self.call("/api/save", {"check_id": "a" * 32, "storage_consent": False})[0])
        self.assertEqual("saved", self.call("/api/save", {"check_id": "a" * 32, "storage_consent": True})[1]["status"])
        self.assertTrue(self.handle.done.wait(1))
        self.assertTrue(self.handle.session.closed)

    def test_public_setup_returns_no_bootstrap_url_token_or_credentials(self):
        opened = []
        with patch.object(send_setup.webbrowser, "open", side_effect=lambda url, **kwargs: opened.append(url) or True):
            result = send_setup.start_send_setup(account="other", host="smtp.example.test", port=587,
                                                  security="starttls", sent_copy="server", session_factory=FakeSession)
        self.assertEqual("browser_opened", result["status"])
        self.assertNotIn("url", result)
        self.assertNotIn("token", json.dumps(result))
        self.assertNotIn("127.0.0.1", json.dumps(result))
        self.assertEqual(0, result["messages_sent"])
        self.assertIn("#token=", opened[0])

    def test_invalid_proposal_fails_before_session_or_socket_creation(self):
        for values in ({"account": "bad account"}, {"account": "ok", "port": 0}, {"account": "ok", "security": "none"}):
            factory = Mock(side_effect=AssertionError("No session"))
            with self.assertRaises(Exception):
                send_setup._launch_send_setup(**values, session_factory=factory)
            factory.assert_not_called()

    def test_active_same_account_not_replaced_browser_failure_and_expiry_reaped(self):
        with self.assertRaises(Exception) as caught:
            send_setup._launch_send_setup(account="work", session_factory=FakeSession)
        self.assertEqual("SETUP_ALREADY_OPEN", caught.exception.code)
        self.assertTrue(self.handle.active)
        with patch.object(send_setup.webbrowser, "open", side_effect=OSError(CANARY)):
            value = send_setup.start_send_setup(account="browser-fails", session_factory=FakeSession)
        self.assertEqual("manual_setup_required", value["status"])
        self.assertNotIn(CANARY, json.dumps(value))
        expiring = send_setup._launch_send_setup(account="expires", session_factory=FakeSession, ttl_seconds=1)
        self.assertTrue(expiring.done.wait(2))
        self.assertTrue(expiring.session.closed)

    def test_ui_never_requests_secret_and_labels_explicit_send_permission(self):
        document = (setup_web.ASSETS / "send-setup.html").read_text()
        script = (setup_web.ASSETS / "send-setup.js").read_text()
        self.assertNotIn('type="password"', document)
        self.assertNotIn('name="password"', document)
        self.assertIn('id="enable-consent" type="checkbox" required', document)
        self.assertIn('id="reuse-consent" type="checkbox" required', document)
        self.assertIn("Save and enable sending", document)
        self.assertIn("Provider saves the Sent copy", document)
        self.assertIn("Plugin saves the Sent copy", document)
        self.assertIn("OAuth-only providers are not supported", document)
        self.assertIn("Settings changed. Check the connection again before saving.", script)
        self.assertIn("history.replaceState", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
