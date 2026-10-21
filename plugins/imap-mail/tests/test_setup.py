"""Fresh-account setup tests: fake TLS reader and temporary private files only."""

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
from imap_mail import setup
from imap_mail.core import DraftService


CANARY = "SYNTHETIC-PASSWORD-MUST-ONLY-APPEAR-IN-SECRET-FILE"


class Reader:
    def __init__(self, owner, profile):
        self.owner = owner
        self.profile = profile
        owner.logins += 1
        owner.used_hosts.append(profile["host"])
        if owner.login_failure:
            raise RuntimeError(CANARY)
        if profile["password"] != CANARY:
            raise AssertionError("Wrong synthetic credential")

    def folders(self):
        self.owner.commands.append("LIST")
        return {"folders": self.owner.folders}

    def select(self, mailbox):
        self.owner.commands.append("EXAMINE")
        self.owner.examined.append(mailbox)
        return "41", 0

    def close(self):
        self.owner.commands.append("LOGOUT")

    def __getattr__(self, name):
        raise AssertionError("Forbidden onboarding operation")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.config_path = self.private / "settings.json"
        self.profiles = self.root / "profiles"
        self.attachments = self.root / "attachments"
        self.attachments.mkdir(mode=0o700)
        self.environment = patch.dict(os.environ, {"MAIL_PROFILES_DIR": str(self.profiles)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in ("MAIL_PROFILES_JSON", "MAIL_BROKER_CONFIG"):
            os.environ.pop(key, None)
        self.logins = 0
        self.login_failure = False
        self.commands = []
        self.used_hosts = []
        self.examined = []
        self.folders = [{"name": "Drafts", "kind": "drafts", "selectable": True}]
        self.payload = {"account": "new-account", "email": "new@example.test", "entity": "Example owner",
                        "host": "imap.example.test", "port": 993, "tls": True,
                        "password": CANARY, "host_confirmed": True,
                        "signature_text": "Owner & Company\n<Approved signature>",
                        "attachment_root": str(self.attachments)}
        self.session = self.make_session()

    def make_session(self):
        session = setup.SetupSession(config_path=self.config_path, reader_factory=lambda profile: Reader(self, profile))
        self.addCleanup(session.close)
        return session

    def error(self, code, call):
        with self.assertRaises(setup.SetupError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(CANARY, str(caught.exception))

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def check(self, **overrides):
        payload = copy.deepcopy(self.payload)
        payload.update(overrides)
        return self.session.check(payload)

    def existing_config(self):
        self.profiles.mkdir(mode=0o700)
        old_secret = self.root / "existing-password.secret"
        old_secret.write_text("unchanged synthetic older credential")
        old_secret.chmod(0o600)
        old_profile = {"id": "existing", "user": "old@example.test", "host": "old.example.test",
                       "entity": "Other owner", "password_file": str(old_secret)}
        self.write_json(self.profiles / "existing.json", old_profile)
        settings = {"version": 1, "profiles_dir": str(self.profiles), "state_dir": "unchanged-state",
                    "preferred_transport": "imap", "unknown_future_setting": {"x": [1, "ąę", None]},
                    "accounts": {"existing": {"identity": {"email": "old@example.test", "host": "old.example.test", "entity": "Other owner"},
                                               "signature": {"none": True}, "drafts_enabled": False,
                                               "unknown_account_key": [False, {"nested": "preserve"}]}}}
        self.write_json(self.config_path, settings)
        return settings

    def test_provider_suggestions_are_static_and_never_confirm_a_host(self):
        for email, expected in (("user@gmail.com", "imap.gmail.com"), ("user@icloud.com", "imap.mail.me.com"), ("user@yahoo.com", "imap.mail.yahoo.com")):
            with self.subTest(email=email):
                suggestion = self.session.suggest(email)
                self.assertEqual(suggestion["host"], expected)
                self.assertEqual(suggestion["status"], "suggested")
                self.assertFalse(suggestion["host_confirmed"])
                self.assertEqual(suggestion["port"], 993)
        self.assertEqual(self.session.suggest("user@outlook.com")["status"], "manual")
        self.assertNotIn("host", self.session.suggest("user@unknown.example.test"))
        self.assertEqual(self.logins, 0)
        self.assertFalse(self.config_path.exists())

    def test_agent_prefill_only_accepts_optional_safe_metadata_without_io(self):
        values = {"email": "owner@example.test", "account": "agent-chosen-account",
                  "entity": "  Confirmed owner  ", "host": "IMAP.EXAMPLE.TEST"}
        with patch("imap_mail.setup._walk", side_effect=AssertionError("Filesystem access")), \
                patch("imap_mail.setup.read_access.Reader", side_effect=AssertionError("Network access")), \
                patch("imap_mail.setup.os.open", side_effect=AssertionError("Storage access")):
            result = setup.validate_prefill(values)
            self.assertEqual(result, {"email": "owner@example.test", "account": "agent-chosen-account",
                                      "entity": "Confirmed owner", "host": "imap.example.test"})
            self.assertEqual(setup.validate_prefill({"email": "owner@example.test"}), {"email": "owner@example.test"})
            self.assertEqual(setup.validate_prefill({}), {})
            self.assertEqual(setup.validate_prefill(None), {})
        self.assertEqual(values["entity"], "  Confirmed owner  ")
        self.assertNotIn("host_confirmed", result)
        self.assertEqual(self.logins, 0)

    def test_known_provider_host_matches_exact_domains_without_io(self):
        with patch("imap_mail.setup._walk", side_effect=AssertionError("Filesystem access")), \
                patch("imap_mail.setup.read_access.Reader", side_effect=AssertionError("Network access")):
            for email, expected in (("user@GMAIL.COM", "imap.gmail.com"),
                                    ("user@googlemail.com", "imap.gmail.com"),
                                    ("user@icloud.com", "imap.mail.me.com"),
                                    ("user@me.com", "imap.mail.me.com"),
                                    ("user@mac.com", "imap.mail.me.com"),
                                    ("user@yahoo.com", "imap.mail.yahoo.com"),
                                    ("user@ymail.com", "imap.mail.yahoo.com"),
                                    ("user@sub.gmail.com", None),
                                    ("user@gmail.com.attacker.example.test", None),
                                    ("user@outlook.com", None), (None, None)):
                with self.subTest(email=email):
                    self.assertEqual(setup.known_provider_host(email), expected)
        self.assertIsNone(setup.known_provider_host("not-an-address"))

    def test_agent_prefill_rejects_password_consent_and_unknown_fields(self):
        for value in ({"password": CANARY}, {"host_confirmed": True}, {"signature_text": "Example"},
                      {"port": 993}, {"unexpected": "value"}, [], "not an object"):
            with self.subTest(keys=list(value) if isinstance(value, dict) else type(value).__name__):
                self.error("INVALID_SETUP_PREFILL", lambda: setup.validate_prefill(value))

    def test_agent_prefill_validates_every_provided_field_strictly(self):
        for value, code in (({"email": "not-an-address"}, "INVALID_EMAIL"),
                            ({"email": "Owner <owner@example.test>"}, "INVALID_EMAIL"),
                            ({"email": "owner@example.test\nOther"}, "INVALID_EMAIL"),
                            ({"account": "../escape"}, "INVALID_ACCOUNT_ID"),
                            ({"account": "a" * 81}, "INVALID_ACCOUNT_ID"),
                            ({"entity": " "}, "INVALID_ENTITY"),
                            ({"entity": "x" * 121}, "INVALID_ENTITY"),
                            ({"entity": "Owner\r\nOther"}, "INVALID_ENTITY"),
                            ({"host": "127.0.0.1"}, "INVALID_IMAP_HOST"),
                            ({"host": "https://imap.example.test/"}, "INVALID_IMAP_HOST"),
                            ({"host": "imap.local"}, "INVALID_IMAP_HOST"),
                            ({"host": None}, "INVALID_IMAP_HOST")):
            with self.subTest(keys=list(value), code=code):
                self.error(code, lambda: setup.validate_prefill(value))

    def test_check_is_read_only_and_keeps_secrets_only_in_memory(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            checked = self.check()
        self.assertEqual(checked["status"], "checked")
        self.assertEqual(checked["account"], "new-account")
        self.assertEqual(checked["drafts_mailbox"], "Drafts")
        self.assertEqual(checked["uidvalidity"], "41")
        self.assertEqual(checked["expires_in_seconds"], 900)
        self.assertEqual(checked["signature_mode"], "configured")
        self.assertEqual(checked["mailbox_writes"], 0)
        self.assertEqual(self.commands, ["LIST", "EXAMINE", "LOGOUT"])
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.profiles.exists())
        self.assertNotIn(CANARY, json.dumps(checked))
        self.assertEqual(captured.getvalue(), "")

    def test_save_writes_separate_private_secret_and_escaped_signature(self):
        checked = self.check()
        saved = self.session.save(checked["check_id"])
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(saved["mailbox_writes"], 0)
        self.assertEqual(self.logins, 1)
        self.assertNotIn(CANARY, json.dumps(saved))
        profile = json.loads((self.profiles / "new-account.json").read_text())
        self.assertNotIn("password", profile)
        self.assertEqual(profile["user"], self.payload["email"])
        password_file = Path(profile["password_file"])
        self.assertEqual(password_file.read_text(), CANARY)
        config = json.loads(self.config_path.read_text())
        entry = config["accounts"]["new-account"]
        self.assertEqual(entry["from_addresses"], [self.payload["email"]])
        self.assertEqual(entry["attachment_roots"], [str(self.attachments)])
        self.assertTrue(entry["drafts_enabled"])
        rich = Path(entry["signature"]["html"]).read_text()
        self.assertEqual(rich, "<div>Owner &amp; Company<br>\n&lt;Approved signature&gt;</div>")
        for file in self.root.rglob("*"):
            if file.is_file():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                if file != password_file:
                    self.assertNotIn(CANARY.encode(), file.read_bytes())
        for directory in (self.private, self.profiles, password_file.parent, password_file.parent.parent):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        discovered = DraftService(config_path=self.config_path).accounts()
        self.assertTrue(discovered["accounts"][0]["drafts_ready"])
        self.assertEqual(self.session._candidates, {})
        self.error("CHECK_EXPIRED_OR_UNAVAILABLE", lambda: self.session.save(checked["check_id"]))

    def test_explicit_no_signature_does_not_create_signature_files(self):
        checked = self.check(signature_text="", signature_none=True)
        self.session.save(checked["check_id"])
        entry = json.loads(self.config_path.read_text())["accounts"]["new-account"]
        self.assertEqual(entry["signature"], {"none": True})
        self.assertFalse(list((self.private / "accounts" / "new-account").glob("signature.*")))

    def test_save_preserves_every_existing_key_account_and_file(self):
        original = self.existing_config()
        old_profile = self.profiles / "existing.json"
        old_secret = self.root / "existing-password.secret"
        sentinel = self.root / "uncertain-journal.json"
        sentinel.write_bytes(b'{"state":"uncertain","do_not_retry":true}')
        sentinel.chmod(0o600)
        unchanged = {path: path.read_bytes() for path in (old_profile, old_secret, sentinel)}
        checked = self.check()
        self.session.save(checked["check_id"])
        updated = json.loads(self.config_path.read_text())
        for key, value in original.items():
            self.assertEqual(updated[key]["existing"] if key == "accounts" else updated[key], value["existing"] if key == "accounts" else value)
        for path, value in unchanged.items():
            self.assertEqual(path.read_bytes(), value)

    def test_existing_account_or_duplicate_email_is_rejected_before_login(self):
        self.existing_config()
        self.error("ACCOUNT_ALREADY_EXISTS", lambda: self.check(account="existing"))
        self.error("EMAIL_ALREADY_CONFIGURED", lambda: self.check(email="OLD@example.test"))
        self.assertEqual(self.logins, 0)

    def test_collision_created_after_check_is_rechecked_under_save_lock(self):
        checked = self.check()
        self.profiles.mkdir(mode=0o700)
        existing = {"id": "new-account", "user": "other@example.test", "host": "other.example.test", "entity": "Other", "password_env": "SYNTHETIC_UNUSED"}
        self.write_json(self.profiles / "new-account.json", existing)
        original = (self.profiles / "new-account.json").read_bytes()
        self.error("ACCOUNT_ALREADY_EXISTS", lambda: self.session.save(checked["check_id"]))
        self.assertEqual((self.profiles / "new-account.json").read_bytes(), original)
        self.assertFalse(self.config_path.exists())
        self.assertFalse((self.private / "accounts" / "new-account").exists())
        self.assertEqual(self.session._candidates, {})

    def test_existing_settings_changed_between_check_and_save_are_preserved(self):
        checked = self.check()
        settings = {"version": 1, "profiles_dir": str(self.profiles), "accounts": {}, "new_unrelated_key": ["preserve"]}
        self.write_json(self.config_path, settings)
        self.session.save(checked["check_id"])
        self.assertEqual(json.loads(self.config_path.read_text())["new_unrelated_key"], ["preserve"])

    def test_commit_failure_rolls_back_only_new_files_and_preserves_old_bytes(self):
        self.existing_config()
        original = self.config_path.read_bytes()
        old_profile = (self.profiles / "existing.json").read_bytes()
        checked = self.check()
        with patch.object(self.session, "_commit_config", side_effect=OSError(CANARY)):
            self.error("SETUP_OPERATION_FAILED", lambda: self.session.save(checked["check_id"]))
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual((self.profiles / "existing.json").read_bytes(), old_profile)
        self.assertFalse((self.profiles / "new-account.json").exists())
        self.assertFalse((self.private / "accounts" / "new-account").exists())
        self.assertFalse(list(self.root.rglob(".setup-config-*")))
        self.assertEqual(self.session._candidates, {})

    def test_failed_new_file_fsync_also_removes_partial_secret(self):
        checked = self.check()
        with patch("imap_mail.setup.os.fsync", side_effect=OSError(CANARY)):
            self.error("SETUP_OPERATION_FAILED", lambda: self.session.save(checked["check_id"]))
        self.assertFalse(self.config_path.exists())
        self.assertFalse((self.profiles / "new-account.json").exists())
        self.assertFalse(list(self.private.rglob("password.secret")))

    def test_malformed_or_permissive_configuration_is_not_repaired_implicitly(self):
        for raw in (b"not-json", b'{"version":1,"accounts":[],"password":"ignored"}', b'{"version":1,"version":1,"accounts":{}}'):
            with self.subTest(raw=raw[:10]):
                self.config_path.write_bytes(raw)
                self.config_path.chmod(0o600)
                session = self.make_session()
                self.error("INVALID_SETUP_CONFIGURATION", lambda: session.check(self.payload))
                self.assertEqual(self.config_path.read_bytes(), raw)
        self.write_json(self.config_path, {"version": 1, "accounts": {}})
        self.config_path.chmod(0o644)
        self.error("PRIVATE_FILE_PERMISSIONS_REQUIRE_600", lambda: self.check())
        self.assertEqual(self.logins, 0)

    def test_symlink_configuration_parent_or_profile_directory_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.private, target_is_directory=True)
        self.error("UNSAFE_SETUP_PATH", lambda: setup.SetupSession(config_path=alias / "settings.json"))
        self.profiles.symlink_to(self.attachments, target_is_directory=True)
        self.error("UNSAFE_SETUP_PATH", self.check)
        self.assertEqual(self.logins, 0)

    def test_unsafe_hosts_and_unconfirmed_server_never_receive_a_password(self):
        for host in ("localhost", "127.0.0.1", "[::1]", "imap.local", "imap.internal", "https://imap.example.test/", "user@imap.example.test", "imap.example.test\r\nOther"):
            with self.subTest(host=host):
                session = self.make_session()
                self.error("INVALID_IMAP_HOST", lambda: session.check(dict(self.payload, host=host)))
        self.error("HOST_CONFIRMATION_REQUIRED", lambda: self.check(host_confirmed=False))
        self.assertEqual(self.logins, 0)

    def test_bounds_password_header_injection_and_signature_choice_fail_closed(self):
        cases = [({"account": "../escape"}, "INVALID_ACCOUNT_ID"),
                 ({"email": "new@example.test\nOther"}, "INVALID_EMAIL"),
                 ({"entity": "Owner\r\nOther"}, "INVALID_ENTITY"),
                 ({"password": CANARY + "\n"}, "INVALID_PASSWORD"),
                 ({"port": True}, "TLS_993_REQUIRED"), ({"tls": False}, "TLS_993_REQUIRED"),
                 ({"signature_text": ""}, "SIGNATURE_CHOICE_REQUIRED"),
                 ({"signature_none": True}, "INVALID_SIGNATURE_CHOICE"),
                 ({"unexpected": "value"}, "INVALID_SETUP_INPUT")]
        for overrides, code in cases:
            with self.subTest(keys=list(overrides)):
                session = self.make_session()
                self.error(code, lambda: session.check(dict(self.payload, **overrides)))
        self.assertEqual(self.logins, 0)

    def test_attachment_roots_cannot_expose_configuration_or_credential_storage(self):
        self.profiles.mkdir(mode=0o700)
        for root in ("/", str(Path.home()), str(self.private), str(self.root), str(self.profiles), str(self.profiles / "subdirectory")):
            with self.subTest(root=Path(root).name):
                session = self.make_session()
                self.error("ATTACHMENT_ROOT_TOO_BROAD", lambda: session.check(dict(self.payload, attachment_root=root)))
        alias = self.root / "attachments-alias"
        alias.symlink_to(self.attachments, target_is_directory=True)
        self.error("UNSAFE_SETUP_PATH", lambda: self.check(attachment_root=str(alias)))
        self.assertEqual(self.logins, 0)

    def test_read_only_broker_and_injected_profiles_are_never_bypassed(self):
        with patch.dict(os.environ, {"MAIL_BROKER_CONFIG": "/synthetic/broker.json"}):
            self.error("BROKER_SETUP_NOT_SUPPORTED", self.check)
        with patch.dict(os.environ, {"MAIL_PROFILES_JSON": "[]"}):
            self.error("INJECTED_PROFILES_SETUP_NOT_SUPPORTED", self.check)
        self.write_json(self.config_path, {"version": 1, "accounts": {}, "broker_config": "/synthetic/broker.json"})
        self.error("BROKER_SETUP_NOT_SUPPORTED", self.check)
        self.assertEqual(self.logins, 0)

    def test_drafts_ambiguity_cannot_be_overridden_and_manual_folder_must_exist(self):
        self.folders.append({"name": "Other drafts", "kind": "drafts", "selectable": True})
        self.error("DRAFTS_FOLDER_AMBIGUOUS", lambda: self.check(drafts_mailbox="Drafts"))
        self.folders = [{"name": "Unmarked drafts", "selectable": True}]
        checked = self.check(drafts_mailbox="Unmarked drafts")
        self.assertEqual(checked["drafts_mailbox"], "Unmarked drafts")
        self.error("DRAFTS_FOLDER_UNAVAILABLE", lambda: self.check(drafts_mailbox="Missing drafts"))

    def test_failed_checks_also_count_toward_the_three_attempt_limit(self):
        for _ in range(3):
            self.error("HOST_CONFIRMATION_REQUIRED", lambda: self.check(host_confirmed=False))
        self.error("CHECK_ATTEMPT_LIMIT", self.check)
        self.assertEqual(self.logins, 0)

    def test_candidate_expires_and_close_erases_pending_credential_reference(self):
        with patch("imap_mail.setup.time.monotonic", return_value=100):
            checked = self.check()
        with patch("imap_mail.setup.time.monotonic", return_value=1001):
            self.error("CHECK_EXPIRED_OR_UNAVAILABLE", lambda: self.session.save(checked["check_id"]))
        self.assertEqual(self.session._candidates, {})
        checked = self.check()
        self.session.close()
        self.assertEqual(self.session._candidates, {})
        self.error("SETUP_SESSION_CLOSED", lambda: self.session.save(checked["check_id"]))
        self.assertFalse(self.config_path.exists())

    def test_payload_mutation_after_check_cannot_change_the_checked_account(self):
        checked = self.session.check(self.payload)
        self.payload["email"] = "different@example.test"
        saved = self.session.save(checked["check_id"])
        self.assertEqual(saved["email"], "new@example.test")

    def test_login_error_never_exposes_the_exception_or_keeps_a_candidate(self):
        self.login_failure = True
        self.error("SETUP_CONNECTION_CHECK_FAILED", self.check)
        self.assertEqual(self.session._candidates, {})
        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()
