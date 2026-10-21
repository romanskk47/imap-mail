"""Loopback onboarding tests with an in-memory backend; no real mail access."""

import contextlib
import http.client
from html.parser import HTMLParser
import io
import json
import re
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import setup_web


class FixedError(Exception):
    code = "AUTHENTICATION_FAILED"


class AssetTests(unittest.TestCase):
    def test_account_pattern_escapes_literal_hyphen_for_modern_browser_unicode_sets(self):
        class Inputs(HTMLParser):
            account_pattern = None

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "input" and values.get("id") == "account":
                    self.account_pattern = values["pattern"]

        document = Inputs()
        document.feed((setup_web.ASSETS / "setup.html").read_text(encoding="utf-8"))
        self.assertEqual(r"[a-zA-Z0-9_\-]{1,80}", document.account_pattern)
        self.assertIsNotNone(re.fullmatch(document.account_pattern, "work-mail_2"))
        self.assertIsNone(re.fullmatch(document.account_pattern, "work@other-account"))
        self.assertIsNone(re.fullmatch(document.account_pattern, "x" * 81))

    def test_secret_or_unknown_prefill_is_rejected_before_session_creation(self):
        for value, code in (({"password": "do-not-accept"}, "INVALID_SETUP_PREFILL"), ({"token": "do-not-accept"}, "INVALID_SETUP_PREFILL"),
                            ({"host_confirmed": True}, "INVALID_SETUP_PREFILL"), ({"unknown": "anything"}, "INVALID_SETUP_PREFILL"),
                            ({"email": "x" * 255}, "INVALID_EMAIL"), ({"account": "bad@account"}, "INVALID_ACCOUNT_ID"),
                            ({"host": "https://imap.example.invalid"}, "INVALID_IMAP_HOST"), ({"entity": 123}, "INVALID_ENTITY")):
            with self.subTest(value=value), patch.object(setup_web, "_QuietServer", side_effect=AssertionError("Must not bind")):
                factory = unittest.mock.Mock(side_effect=AssertionError("Must not create session"))
                with self.assertRaises(Exception) as caught:
                    setup_web._launch(session_factory=factory, prefill=value)
                self.assertEqual(code, caught.exception.code)
                factory.assert_not_called()


class FakeSession:
    def __init__(self, config_path=None):
        self.calls = []
        self.closed = False

    def suggest(self, email):
        self.calls.append(("suggest", email))
        return {"status": "manual", "email": email, "port": 993, "tls": True, "host_confirmed": False}

    def check(self, payload):
        self.calls.append(("check", dict(payload)))
        if payload.get("password") == "bad":
            raise FixedError("never-print-password")
        if not payload.get("host_confirmed"):
            raise setup_web.SetupWebError("HOST_CONFIRMATION_REQUIRED")
        return {"status": "checked", "check_id": "checked-operation", "account": payload.get("account"), "drafts_mailbox": "Drafts", "mailbox_writes": 0}

    def save(self, check_id):
        self.calls.append(("save", check_id))
        if check_id != "checked-operation":
            raise setup_web.SetupWebError("CHECK_NOT_FOUND")
        return {"status": "saved", "account": "test", "mailbox_writes": 0}

    def close(self):
        self.closed = True
        self.calls.clear()


class SetupWebTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.handle = setup_web._launch(session_factory=lambda config_path=None: self.session)

    def tearDown(self):
        self.handle.close()
        self.handle.worker.join(timeout=2)

    def call(self, path, payload=None, headers=None, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.handle.server.server_port, timeout=3)
        values = {"Host": self.handle.host, "Origin": self.handle.origin,
                  "X-Setup-Token": self.handle.token, "Content-Type": "application/json"}
        if headers:
            values.update(headers)
        body = json.dumps(payload).encode() if payload is not None else b"{}"
        connection.request(method, path, body=body if method == "POST" else None, headers=values)
        response = connection.getresponse()
        data = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, UnicodeError):
            pass
        return status, data, response_headers

    def test_fragment_bootstrap_is_not_part_of_request_and_assets_are_local(self):
        self.assertIn("/#token=", self.handle.url)
        self.assertEqual(43, len(self.handle.token))
        for path in ("/", "/setup.js", "/setup.css"):
            status, data, headers = self.call(path, method="GET")
            self.assertEqual(200, status)
            self.assertNotIn(self.handle.token.encode(), data)
            self.assertEqual("no-store, private", headers["Cache-Control"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_unknown_paths_and_queries_are_not_filesystem_routes(self):
        for path in ("/etc/passwd", "/../setup.py", "/%2e%2e/setup.py", "/setup.js?token=anything", "/api/state"):
            self.assertEqual(404, self.call(path, method="GET")[0])

    def test_host_origin_and_token_are_all_required(self):
        probes = [{"Host": "evil.invalid"}, {"Host": "localhost:" + str(self.handle.server.server_port)},
                  {"Origin": "https://evil.invalid"}, {"Origin": "null"}, {"X-Setup-Token": "wrong"}]
        for headers in probes:
            with self.subTest(headers=headers):
                self.assertEqual(403, self.call("/api/check", {"password": "must-not-reach"}, headers)[0])
        self.assertEqual([], self.session.calls)

    def test_context_requires_auth_and_contains_only_validated_nonsecret_prefill(self):
        self.handle.prefill = {"email": "me@example.invalid", "account": "work-mail", "entity": "Example company", "host": "imap.example.invalid"}
        self.assertEqual(403, self.call("/api/context", {}, {"X-Setup-Token": "wrong"})[0])
        status, result, _ = self.call("/api/context", {})
        self.assertEqual(200, status)
        self.assertEqual(self.handle.prefill, result["prefill"])
        self.assertNotIn("password", result["prefill"])
        self.assertNotIn("host_confirmed", result["prefill"])
        self.assertEqual([], self.session.calls)
        self.assertEqual(400, self.call("/api/context", {"email": "another@example.invalid"})[0])

    def test_launch_prefill_normalizes_metadata_without_authorizing_a_connection(self):
        extra = setup_web._launch(session_factory=FakeSession, prefill={"email": "me@example.invalid", "account": "work-mail", "entity": "Example company", "host": "IMAP.GMAIL.COM"})
        try:
            self.assertEqual("imap.gmail.com", extra.prefill["host"])
            self.assertNotIn("host_confirmed", extra.prefill)
            self.assertEqual([], extra.session.calls)
        finally:
            extra.close()
            extra.worker.join(2)

    def test_agent_custom_host_must_be_retyped_before_backend_gets_credentials(self):
        original = self.handle
        extra = setup_web._launch(session_factory=FakeSession, prefill={"email": "me@example.com", "account": "agent-account", "entity": "Example", "host": "mail.example.com"})
        self.handle = extra
        try:
            context = self.call("/api/context", {})[1]
            self.assertEqual("agent_suggestion", context["host_source"])
            self.assertTrue(context["require_host_retype"])
            payload = {"account": "agent-account", "email": "me@example.com", "host": "mail.example.com", "password": "direct-local-secret", "host_confirmed": True}
            self.assertEqual("HOST_RETYPE_REQUIRED", self.call("/api/check", payload)[1]["error_code"])
            self.assertEqual([], extra.session.calls)
            changed = {**payload, "host": "other.example.com", "agent_host_confirmation": "mail.example.com"}
            self.assertEqual("HOST_RETYPE_REQUIRED", self.call("/api/check", changed)[1]["error_code"])
            self.assertEqual([], extra.session.calls)
            checked = self.call("/api/check", {**changed, "agent_host_confirmation": "other.example.com"})
            self.assertEqual("checked", checked[1]["status"])
            self.assertNotIn("agent_host_confirmation", extra.session.calls[0][1])
        finally:
            self.handle = original
            extra.close()
            extra.worker.join(2)

    def test_known_provider_prefill_needs_no_retype_but_changed_host_does(self):
        original = self.handle
        extra = setup_web._launch(session_factory=FakeSession, prefill={"email": "me@gmail.com", "account": "known-account", "host": "imap.gmail.com"})
        self.handle = extra
        try:
            context = self.call("/api/context", {})[1]
            self.assertEqual("known_provider", context["host_source"])
            self.assertFalse(context["require_host_retype"])
            payload = {"account": "known-account", "email": "me@gmail.com", "host": "imap.gmail.com", "password": "direct-local-secret", "host_confirmed": True}
            self.assertEqual("checked", self.call("/api/check", payload)[1]["status"])
            self.assertEqual("HOST_RETYPE_REQUIRED", self.call("/api/check", {**payload, "host": "mail.example.com"})[1]["error_code"])
            self.assertEqual(1, len(extra.session.calls))
        finally:
            self.handle = original
            extra.close()
            extra.worker.join(2)

    def test_same_active_account_setup_is_not_superseded(self):
        extra = setup_web._launch(session_factory=FakeSession, prefill={"account": "already-open"})
        try:
            with self.assertRaises(setup_web.SetupWebError) as caught:
                setup_web._launch(session_factory=FakeSession, prefill={"account": "already-open"})
            self.assertEqual("SETUP_ALREADY_OPEN", caught.exception.code)
            self.assertTrue(extra.active)
            self.assertFalse(extra.session.closed)
        finally:
            extra.close()
            extra.worker.join(2)

    def test_suggest_check_and_explicit_save(self):
        self.assertEqual("manual", self.call("/api/suggest", {"email": "me@example.invalid"})[1]["status"])
        result = self.call("/api/check", {"account": "test", "password": "direct-secret", "host_confirmed": True})[1]
        self.assertEqual("checked", result["status"])
        self.assertNotIn("direct-secret", json.dumps(result))
        self.assertFalse(self.session.closed)
        self.assertEqual(400, self.call("/api/save", {"check_id": result["check_id"]})[0])
        self.assertEqual(400, self.call("/api/save", {"check_id": result["check_id"], "storage_consent": False})[0])
        saved = self.call("/api/save", {"check_id": result["check_id"], "storage_consent": True})
        self.assertEqual("saved", saved[1]["status"])
        self.assertTrue(self.handle.done.wait(1))
        self.assertTrue(self.session.closed)
        self.assertEqual("", self.handle.token)

    def test_failed_checks_are_sanitized_and_capped_at_three_attempts(self):
        for _ in range(3):
            status, data, _ = self.call("/api/check", {"password": "bad", "host_confirmed": True})
            self.assertEqual(400, status)
            self.assertEqual("AUTHENTICATION_FAILED", data["error_code"])
            self.assertNotIn("never-print-password", json.dumps(data))
        self.assertEqual(429, self.call("/api/check", {"password": "valid", "host_confirmed": True})[0])
        self.assertEqual(3, len(self.session.calls))

    def test_cancel_reaps_session_without_save(self):
        status, result, _ = self.call("/api/cancel", {})
        self.assertEqual(200, status)
        self.assertEqual("cancelled", result["status"])
        self.assertTrue(self.handle.done.wait(1))
        self.assertTrue(self.session.closed)

    def test_content_type_and_size_are_bounded(self):
        self.assertEqual(415, self.call("/api/check", {}, {"Content-Type": "text/plain"})[0])
        self.assertEqual(413, self.call("/api/check", {"padding": "x" * 65536})[0])
        self.assertEqual([], self.session.calls)

    def test_duplicate_negative_and_missing_content_length_are_rejected(self):
        for lengths in (("2", "2"), ("-2",), ()):
            connection = http.client.HTTPConnection("127.0.0.1", self.handle.server.server_port, timeout=3)
            connection.putrequest("POST", "/api/check", skip_host=True)
            for key, value in {"Host": self.handle.host, "Origin": self.handle.origin,
                               "X-Setup-Token": self.handle.token, "Content-Type": "application/json"}.items():
                connection.putheader(key, value)
            for length in lengths:
                connection.putheader("Content-Length", length)
            connection.endheaders(b"{}")
            response = connection.getresponse()
            self.assertEqual(400, response.status)
            response.read()
            connection.close()
        self.assertEqual([], self.session.calls)

    def test_public_start_returns_only_safe_status_not_bootstrap_capability(self):
        opened = []
        try:
            with patch.object(setup_web.webbrowser, "open", side_effect=lambda url, **kwargs: opened.append(url) or True):
                result = setup_web.start_setup(session_factory=FakeSession, prefill={"email": "me@example.invalid", "account": "work-mail", "entity": "Example", "host": "imap.gmail.com"})
            self.assertEqual("browser_opened", result["status"])
            self.assertRegex(result["setup_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(["credential", "host_confirmation", "storage_consent"], result["needs_human"])
            self.assertEqual(1, len(opened))
            self.assertIn("#token=", opened[0])
            self.assertNotIn("url", result)
            self.assertNotIn(opened[0], json.dumps(result))
            self.assertNotIn("token", json.dumps(result))
        finally:
            for handle in list(setup_web._REGISTRY):
                if handle is not self.handle:
                    handle.close()
                    handle.worker.join(2)

    def test_browser_failure_returns_static_help_and_reaps_session(self):
        with patch.object(setup_web.webbrowser, "open", side_effect=OSError("private-browser-details")):
            result = setup_web.start_setup(session_factory=FakeSession)
        self.assertEqual("manual_setup_required", result["status"])
        self.assertNotIn("private-browser-details", json.dumps(result))
        self.assertNotIn("127.0.0.1", json.dumps(result))

    def test_no_browser_cli_refuses_captured_stdout_before_launch(self):
        output = io.StringIO()
        with patch.object(setup_web, "_launch", side_effect=AssertionError("Must not launch")), contextlib.redirect_stdout(output):
            result = setup_web.serve_setup(open_browser=False, session_factory=FakeSession)
        self.assertEqual(1, result)
        self.assertNotIn("url", json.loads(output.getvalue()))
        self.assertNotIn("#token", output.getvalue())

    def test_expiry_closes_backend_and_invalidates_token(self):
        expiring = setup_web._launch(session_factory=FakeSession, ttl_seconds=1)
        self.assertTrue(expiring.done.wait(2))
        self.assertTrue(expiring.session.closed)
        self.assertEqual("expired", expiring.result)
        self.assertEqual("", expiring.token)
        expiring.worker.join(2)


if __name__ == "__main__":
    unittest.main()
