"""Independent onboarding boundary tests, using only synthetic accounts/secrets."""
import concurrent.futures
import copy
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from imap_mail import setup
from imap_mail.core import DraftService


CANARY = "FAKE-ONBOARDING-PASSWORD-DO-NOT-ECHO"


class FakeReader:
    def __init__(self, owner, profile):
        self.owner = owner
        self.profile = profile
        self.owner.connections += 1

    def folders(self):
        self.owner.commands.append("LIST")
        return {"folders": [{"name": "INBOX", "kind": None, "selectable": True},
                            {"name": "Drafts", "kind": "drafts", "selectable": True}]}

    def select(self, mailbox):
        if mailbox not in ("INBOX", "Drafts"):
            raise AssertionError("Unknown synthetic mailbox")
        self.owner.commands.append("EXAMINE")
        return "12", 0

    def close(self):
        self.owner.commands.append("LOGOUT")

    def __getattr__(self, name):
        raise AssertionError("Onboarding attempted unsupported mail operation: " + name)


class SetupBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.allowed = self.root / "attachments"
        self.allowed.mkdir(mode=0o700)
        self.config_path = self.private / "settings.json"
        environment = mock.patch.dict(os.environ, {"HOME": str(self.root), "MAIL_PROFILES_DIR": str(self.private / "profiles")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.connections = 0
        self.commands = []
        self.session = self.new_session()
        self.payload = {"account": "new-account", "email": "new@example.test",
                        "entity": "synthetic-company", "host": "imap.example.test",
                        "port": 993, "tls": True, "password": CANARY,
                        "host_confirmed": True, "signature_text": "New Owner\nExample Company",
                        "attachment_root": str(self.allowed)}

    def new_session(self):
        session = setup.SetupSession(config_path=str(self.config_path), reader_factory=lambda profile: FakeReader(self, profile))
        self.addCleanup(session.close)
        return session

    def check(self, **changes):
        payload = copy.deepcopy(self.payload)
        payload.update(changes)
        return self.session.check(payload)

    def assert_private(self, result):
        text = json.dumps(result)
        self.assertNotIn(CANARY, text)
        self.assertNotIn("password.secret", text)

    def test_fresh_account_has_no_remote_write_and_no_secret_in_public_results(self):
        checked = self.check()
        self.assertEqual(checked["status"], "checked")
        self.assertEqual(checked["mailbox_writes"], 0)
        self.assert_private(checked)
        saved = self.session.save(checked["check_id"])
        self.assert_private(saved)
        self.assertEqual(saved["mailbox_writes"], 0)
        self.assertTrue(set(self.commands) <= {"LIST", "EXAMINE", "LOGOUT"})
        discovered = DraftService(config_path=str(self.config_path)).accounts()
        self.assertTrue(next(row for row in discovered["accounts"] if row["account"] == "new-account")["drafts_ready"])
        settings = json.loads(self.config_path.read_text())
        profiles_dir = Path(settings["profiles_dir"]).expanduser()
        if not profiles_dir.is_absolute():
            profiles_dir = self.private / profiles_dir
        profile = json.loads((profiles_dir / "new-account.json").read_text())
        self.assertNotIn("password", profile)
        password_path = Path(profile["password_file"])
        self.assertEqual(password_path.read_text().rstrip("\r\n"), CANARY)
        self.assertEqual(password_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_malformed_fields_are_rejected_before_login(self):
        cases = [{"account": "../escape"}, {"email": "new@example.test\r\nBcc: attacker@example.test"},
                 {"host": "imap.example.test\nother"}, {"host_confirmed": False},
                 {"tls": False}, {"port": True}, {"port": -1},
                 {"signature_none": True}, {"unexpected_secret_copy": CANARY}]
        for changes in cases:
            with self.subTest(changes=set(changes)):
                session = self.new_session()
                payload = dict(self.payload, **changes)
                with self.assertRaises(Exception):
                    session.check(payload)
        self.assertEqual(self.connections, 0)
        self.assertFalse(self.config_path.exists())

    def test_attachment_roots_and_symlink_parent_are_not_accepted(self):
        alias = self.root / "attachment-alias"
        alias.symlink_to(self.allowed, target_is_directory=True)
        for candidate in ("/", str(Path.home()), str(self.private), str(self.private / "accounts"), str(alias), str(self.root / "missing")):
            with self.subTest(candidate=Path(candidate).name):
                session = self.new_session()
                with self.assertRaises(Exception):
                    session.check(dict(self.payload, attachment_root=candidate))
        self.assertEqual(self.connections, 0)
        private_alias = self.root / "private-alias"
        private_alias.symlink_to(self.private, target_is_directory=True)
        with self.assertRaises(Exception):
            setup.SetupSession(config_path=str(private_alias / "settings.json"), reader_factory=lambda profile: FakeReader(self, profile))

    def test_check_limit_is_three_and_closed_sessions_cannot_save(self):
        for _ in range(3):
            self.check()
        with self.assertRaises(Exception):
            self.check()
        self.assertEqual(self.connections, 3)
        session = self.new_session()
        checked = session.check(self.payload)
        session.close()
        with self.assertRaises(Exception):
            session.save(checked["check_id"])
        self.assertFalse(self.config_path.exists())

    def test_concurrent_collision_does_not_overwrite_existing_identity(self):
        other = self.new_session()
        first = self.check()
        second = other.check(dict(self.payload, password=CANARY + "-SECOND"))

        def save(pair):
            session, checked = pair
            try:
                return "saved", session.save(checked["check_id"])
            except Exception:
                return "rejected", None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ((self.session, first), (other, second))))
        self.assertEqual(sorted(state for state, _ in results), ["rejected", "saved"])
        settings = json.loads(self.config_path.read_text())
        self.assertEqual(list(settings["accounts"]), ["new-account"])
        self.assertEqual(len(list(self.private.rglob("password.secret"))), 1)
        self.assertTrue(set(self.commands) <= {"LIST", "EXAMINE", "LOGOUT"})

    def existing_configuration(self):
        profiles = self.private / "profiles"
        profiles.mkdir(mode=0o700)
        profile = {"id": "existing", "host": "old-imap.example.test", "user": "old@example.test",
                   "entity": "old-company", "password_env": "FAKE_UNUSED_OLD_PASSWORD"}
        profile_path = profiles / "existing.json"
        profile_path.write_text(json.dumps(profile))
        profile_path.chmod(0o600)
        journal = self.root / "journal"
        journal.mkdir(mode=0o700)
        uncertain = journal / "existing-uncertain.json"
        uncertain.write_bytes(b'{"state":"uncertain","append_attempted":true}\n')
        uncertain.chmod(0o600)
        config = {"version": 1, "profiles_dir": str(profiles), "state_dir": str(journal),
                  "preferred_transport": "imap", "owner_extension": {"keep": [1, 2, 3]},
                  "accounts": {"existing": {"identity": {"email": "old@example.test", "host": "old-imap.example.test", "entity": "old-company"},
                                              "from_addresses": ["old@example.test"], "default_from": "old@example.test",
                                              "drafts_enabled": True, "signature": {"none": True}, "attachment_roots": []}}}
        self.config_path.write_text(json.dumps(config))
        self.config_path.chmod(0o600)
        return config, uncertain

    def test_save_preserves_existing_accounts_settings_and_uncertain_journal(self):
        original, uncertain = self.existing_configuration()
        before = uncertain.read_bytes()
        checked = self.check()
        self.session.save(checked["check_id"])
        after = json.loads(self.config_path.read_text())
        for field in ("version", "profiles_dir", "state_dir", "preferred_transport", "owner_extension"):
            self.assertEqual(after[field], original[field])
        self.assertEqual(after["accounts"]["existing"], original["accounts"]["existing"])
        self.assertEqual(uncertain.read_bytes(), before)
        self.assertEqual(set(after["accounts"]), {"existing", "new-account"})

    def test_commit_failure_rolls_back_only_new_files_and_consumes_check(self):
        _, uncertain = self.existing_configuration()
        settings_before = self.config_path.read_bytes()
        journal_before = uncertain.read_bytes()
        checked = self.check()
        original_replace = setup.os.replace

        def fail_settings_commit(source, destination):
            if Path(destination) == self.config_path:
                raise OSError(CANARY)
            return original_replace(source, destination)

        with mock.patch.object(setup.os, "replace", side_effect=fail_settings_commit):
            with self.assertRaises(setup.SetupError) as saved_error:
                self.session.save(checked["check_id"])
        self.assertNotIn(CANARY, str(saved_error.exception))
        self.assertEqual(self.config_path.read_bytes(), settings_before)
        self.assertEqual(uncertain.read_bytes(), journal_before)
        self.assertFalse((self.private / "profiles" / "new-account.json").exists())
        self.assertFalse((self.private / "accounts" / "new-account").exists())
        self.assertEqual(list(self.private.glob(".setup-config-*")), [])
        with self.assertRaises(setup.SetupError):
            self.session.save(checked["check_id"])

    def test_external_credentials_cannot_become_attachment_roots(self):
        vault = self.root / "vault"
        vault.mkdir(mode=0o700)
        profiles = vault / "profiles"
        profiles.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {"MAIL_PROFILES_DIR": str(profiles)}):
            for candidate in (vault, profiles):
                with self.subTest(root=candidate.name):
                    session = self.new_session()
                    with self.assertRaises(setup.SetupError):
                        session.check(dict(self.payload, attachment_root=str(candidate)))
        self.assertEqual(self.connections, 0)

    def test_prefill_normalizes_only_supplied_public_context_without_guessing(self):
        normalized = setup.validate_prefill({"email": "owner+tag@Example.test", "account": "Known_Account",
                                             "entity": "  Confirmed legal entity  ", "host": "IMAP.EXAMPLE.TEST"})
        self.assertEqual(normalized, {"email": "owner+tag@Example.test", "account": "Known_Account",
                                      "entity": "Confirmed legal entity", "host": "imap.example.test"})
        self.assertEqual(setup.validate_prefill({"email": "owner@gmail.com"}), {"email": "owner@gmail.com"})
        self.assertEqual(setup.validate_prefill(None), {})
        self.assertNotIn("host_confirmed", normalized)
        self.assertEqual(self.connections, 0)
        self.assertFalse(self.config_path.exists())

    def test_prefill_refuses_credentials_consent_and_unknown_fields(self):
        for forbidden in ("password", "password_file", "password_env", "token", "session_url", "host_confirmed", "storage_consent", "signature_text", "attachment_root", "unknown"):
            with self.subTest(field=forbidden):
                with self.assertRaises(setup.SetupError) as error:
                    setup.validate_prefill({"email": "owner@example.test", forbidden: CANARY})
                self.assertNotIn(CANARY, str(error.exception))
        for malformed in ({"email": "owner@example.test\r\nBcc: attacker@example.test"}, {"account": "../escape"},
                          {"entity": "   "}, {"host": "https://imap.example.test/"}, {"host": "127.0.0.1"}):
            with self.subTest(fields=list(malformed)):
                with self.assertRaises(setup.SetupError):
                    setup.validate_prefill(malformed)
        self.assertEqual(self.connections, 0)


class FakeWebSession:
    def __init__(self, config_path=None):
        self.calls = []
        self.closed = False
        self.explode = False

    def suggest(self, email):
        self.calls.append("suggest")
        return {"status": "manual_required"}

    def check(self, payload):
        self.calls.append("check")
        self.last_check = dict(payload)
        if self.explode:
            raise ValueError(CANARY)
        return {"status": "checked", "check_id": "a" * 32, "mailbox_writes": 0}

    def save(self, check_id):
        self.calls.append("save")
        return {"status": "saved", "account": "example", "mailbox_writes": 0}

    def close(self):
        self.closed = True


class SetupHttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        from imap_mail import setup_web
        self.web = setup_web
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        environment = mock.patch.dict(os.environ, {"HOME": str(root), "MAIL_PROFILES_DIR": str(root / "profiles")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.session = FakeWebSession()
        self.handle = self.web._launch(config_path=str(root / "settings.json"), session_factory=lambda config_path=None: self.session,
                                      prefill={"email": "new@example.test", "account": "new-account", "entity": "Example Company", "host": "IMAP.EXAMPLE.TEST"})
        self.addCleanup(self.handle.close)
        self.original_token = self.handle.token
        self.host = self.handle.origin.removeprefix("http://")
        self.headers = {"Host": self.host, "Origin": self.handle.origin,
                        "X-Setup-Token": self.handle.token, "Content-Type": "application/json"}

    def post(self, path, payload, changes=None):
        headers = dict(self.headers)
        for key, value in (changes or {}).items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        connection = http.client.HTTPConnection(self.host, timeout=2)
        self.addCleanup(connection.close)
        connection.request("POST", path, body=raw, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()

    def test_exact_host_origin_and_private_token_are_all_required(self):
        for changes in ({"Host": "attacker.example.test"}, {"Origin": "https://attacker.example.test"},
                        {"Origin": None}, {"X-Setup-Token": None}, {"X-Setup-Token": "incorrect"}):
            with self.subTest(changes=list(changes)):
                code, body = self.post("/api/suggest", {"email": "new@example.test"}, changes)
                self.assertNotEqual(code, 200)
                self.assertFalse(self.original_token.encode() in body, "Private bootstrap token returned in HTTP response")
        self.assertEqual(self.session.calls, [])
        code, _ = self.post("/api/suggest", {"email": "new@example.test"})
        self.assertEqual(code, 200)
        self.assertEqual(self.session.calls, ["suggest"])

    def test_json_body_and_content_type_are_bounded(self):
        for raw, changes in ((b"x" * 65537, {}), (b"not-json", {}), (b"[]", {}),
                             (b"{}", {"Content-Type": "text/plain"}),
                             (b"{}", {"Transfer-Encoding": "chunked"})):
            with self.subTest(length=len(raw), changes=list(changes)):
                code, body = self.post("/api/check", raw, changes)
                self.assertNotEqual(code, 200)
                self.assertNotIn(CANARY.encode(), body)
        self.assertEqual(self.session.calls, [])

    def test_save_requires_explicit_storage_consent(self):
        for payload in ({"check_id": "a" * 32}, {"check_id": "a" * 32, "storage_consent": False}):
            code, _ = self.post("/api/save", payload)
            self.assertNotEqual(code, 200)
        self.assertEqual(self.session.calls, [])
        code, body = self.post("/api/save", {"check_id": "a" * 32, "storage_consent": True})
        self.assertEqual(code, 200)
        self.assertFalse(self.original_token.encode() in body, "Private bootstrap token returned in HTTP response")
        self.assertEqual(self.session.calls, ["save"])

    def test_unexpected_backend_exception_never_echoes_secret(self):
        self.session.explode = True
        payload = {"account": "example", "email": "new@example.test", "entity": "example", "host": "imap.example.test",
                   "password": CANARY, "host_confirmed": True, "signature_none": True, "agent_host_confirmation": "imap.example.test"}
        code, body = self.post("/api/check", payload)
        self.assertNotEqual(code, 200)
        self.assertNotIn(CANARY.encode(), body)
        self.assertFalse(self.original_token.encode() in body, "Private bootstrap token returned in HTTP response")
        self.assertEqual(self.session.calls, ["check"])

    def test_http_check_limit_stops_before_fourth_backend_attempt(self):
        for _ in range(3):
            code, _ = self.post("/api/check", {"host": "imap.example.test", "agent_host_confirmation": "imap.example.test"})
            self.assertEqual(code, 200)
        code, _ = self.post("/api/check", {"host": "imap.example.test", "agent_host_confirmation": "imap.example.test"})
        self.assertEqual(code, 429)
        self.assertEqual(self.session.calls, ["check"] * 3)

    def test_duplicate_host_or_origin_headers_are_rejected(self):
        for duplicate in ("Host", "Origin", "X-Setup-Token", "Content-Length"):
            with self.subTest(header=duplicate):
                connection = http.client.HTTPConnection(self.host, timeout=2)
                self.addCleanup(connection.close)
                connection.putrequest("POST", "/api/suggest", skip_host=True)
                body = b'{"email":"new@example.test"}'
                values = {**self.headers, "Content-Length": str(len(body))}
                for key, value in values.items():
                    connection.putheader(key, value)
                    if key == duplicate:
                        connection.putheader(key, value)
                connection.endheaders(body)
                response = connection.getresponse()
                self.assertNotEqual(response.status, 200)
                response.read()
        self.assertEqual(self.session.calls, [])

    def test_public_browser_launcher_returns_neither_url_nor_token(self):
        handles = []
        original_launch = self.web._launch

        def launch(*args, **kwargs):
            handle = original_launch(*args, **kwargs)
            handles.append(handle)
            self.addCleanup(handle.close)
            return handle

        with mock.patch.object(self.web, "_launch", side_effect=launch), mock.patch.object(self.web.webbrowser, "open", return_value=True):
            result = self.web.start_setup(config_path=str(Path(self.temp.name).resolve() / "another-settings.json"), session_factory=lambda config_path=None: FakeWebSession(),
                                         prefill={"email": "known@example.test", "entity": "Known Company"})
        self.assertEqual(result["status"], "browser_opened")
        self.assertEqual(len(result["setup_id"]), 32)
        self.assertIn("credential", result["needs_human"])
        rendered = json.dumps(result)
        self.assertNotIn("http://", rendered)
        self.assertNotIn("127.0.0.1", rendered)
        self.assertNotIn("token", rendered)
        self.assertFalse(handles[0].token in rendered, "Private bootstrap token returned to model")

    def test_prefill_rejection_happens_before_browser_backend_or_socket(self):
        factory = mock.Mock()
        with mock.patch.object(self.web, "_QuietServer") as server, mock.patch.object(self.web.webbrowser, "open") as browser:
            for field in ("password", "token", "host_confirmed", "storage_consent", "unknown"):
                with self.subTest(field=field):
                    with self.assertRaises(setup.SetupError):
                        self.web.start_setup(session_factory=factory, prefill={field: CANARY})
            factory.assert_not_called()
            server.assert_not_called()
            browser.assert_not_called()

    def test_context_is_authenticated_nonsecret_and_cannot_be_modified(self):
        code, _ = self.post("/api/context", {}, {"X-Setup-Token": None})
        self.assertNotEqual(code, 200)
        code, body = self.post("/api/context", {"host_confirmed": True, "password": CANARY})
        self.assertNotEqual(code, 200)
        self.assertNotIn(CANARY.encode(), body)
        code, body = self.post("/api/context", {})
        self.assertEqual(code, 200)
        result = json.loads(body)
        self.assertEqual(result["prefill"], {"email": "new@example.test", "account": "new-account", "entity": "Example Company", "host": "imap.example.test"})
        self.assertEqual(result["host_source"], "agent_suggestion")
        self.assertTrue(result["require_host_retype"])
        self.assertNotIn("host_confirmed", result["prefill"])
        self.assertFalse(self.original_token.encode() in body, "Private bootstrap token returned in context")
        self.assertEqual(self.session.calls, [])

    def test_unknown_agent_host_cannot_skip_retype_by_changing_host(self):
        payload = {"email": "new@example.test", "host": "imap.example.test"}
        for changes in ({}, {"agent_host_confirmation": "wrong.example.test"},
                        {"email": "owner@gmail.com", "host": "imap.gmail.com"}):
            with self.subTest(fields=list(changes)):
                code, body = self.post("/api/check", {**payload, **changes})
                self.assertNotEqual(code, 200)
                self.assertEqual(json.loads(body)["error_code"], "HOST_RETYPE_REQUIRED")
        self.assertEqual(self.session.calls, [])
        code, _ = self.post("/api/check", {**payload, "agent_host_confirmation": payload["host"]})
        self.assertEqual(code, 200)
        self.assertEqual(self.session.calls, ["check"])
        self.assertNotIn("agent_host_confirmation", self.session.last_check)

    def test_exact_known_provider_has_no_additional_retype_but_no_login(self):
        session = FakeWebSession()
        handle = self.web._launch(session_factory=lambda config_path=None: session,
                                  prefill={"email": "owner@gmail.com", "host": "IMAP.GMAIL.COM"})
        self.addCleanup(handle.close)
        connection = http.client.HTTPConnection(handle.host, timeout=2)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/context", body=b"{}", headers={"Host": handle.host, "Origin": handle.origin,
                           "X-Setup-Token": handle.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        context = json.loads(response.read())
        self.assertEqual(context["host_source"], "known_provider")
        self.assertFalse(context["require_host_retype"])
        self.assertNotIn("host_confirmed", context["prefill"])
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
