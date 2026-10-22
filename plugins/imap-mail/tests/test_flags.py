"""Offline Seen/Flagged tests with exact synthetic IMAP identities only."""

import concurrent.futures
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import DraftError, digest
from imap_mail.flags import FlagService
from imap_mail.read_access import utf7_decode


class FlagsServer:
    def __init__(self):
        self.validity = {"INBOX": "7", "Archive": "9"}
        self.messages = {"INBOX": {"13": b"Subject: received message\r\n\r\nPrivate contents\r\n",
                                    "14": b"Subject: another\r\n\r\nOther contents\r\n"},
                         "Archive": {"15": b"Subject: old\r\n\r\nArchived contents\r\n"}}
        self.flags = {("INBOX", "13"): [], ("INBOX", "14"): [], ("Archive", "15"): []}
        self.commands = []
        self.readers = 0
        self.stores = 0
        self.write_selections = 0
        self.timeout_before = self.timeout_after = self.refuse_store = False
        self.ignore_store = self.refuse_select = self.force_readonly = False
        self.fail_reads = False
        self.before_select = self.after_store = self.after_fetch = None
        self.before_store = None

    def reader(self, profile):
        self.readers += 1
        return FlagsReader(self, profile)


class FlagsReader:
    def __init__(self, server, profile):
        self.server, self.profile = server, profile
        self.client = self
        self.mailbox = None
        self.is_readonly = True

    @staticmethod
    def _quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def close(self):
        self.server.commands.append(("LOGOUT",))

    def select(self, mailbox, readonly=None):
        if readonly is None:
            self.server.commands.append(("EXAMINE", mailbox))
            self.mailbox, self.is_readonly = mailbox, True
            return self.server.validity[mailbox], len(self.server.messages[mailbox])
        if readonly is not False:
            raise AssertionError("Unexpected selection")
        name = utf7_decode(re.sub(r"\\(.)", r"\1", mailbox[1:-1]))
        self.server.commands.append(("SELECT_RW", name))
        self.server.write_selections += 1
        self.mailbox = name
        self.is_readonly = self.server.force_readonly
        if self.server.before_select:
            self.server.before_select(self)
        return ("NO" if self.server.refuse_select else "OK"), [b"selected"]

    def response(self, name):
        if name != "UIDVALIDITY":
            raise AssertionError("Only UIDVALIDITY is requested")
        return name, [self.server.validity[self.mailbox].encode()]

    def uid(self, command, *args):
        self.server.commands.append((command, self.mailbox, *args))
        uid = str(args[0])
        if not re.fullmatch(r"[1-9][0-9]*", uid):
            raise AssertionError("Only one exact UID, never wildcard or set")
        key = (self.mailbox, uid)
        if command == "FETCH":
            if args[1:] != ("(UID RFC822.SIZE FLAGS BODY.PEEK[])",):
                raise AssertionError("Only a full PEEK fetch")
            if self.server.fail_reads:
                raise TimeoutError("private-secret-server-error")
            raw = self.server.messages[self.mailbox].get(uid)
            if raw is None:
                return "OK", []
            flags = " ".join(self.server.flags[key])
            result = "OK", [(f"1 (UID {uid} RFC822.SIZE {len(raw)} FLAGS ({flags}) BODY[] {{{len(raw)}}}".encode(), raw), b")"]
            if self.server.after_fetch:
                self.server.after_fetch(self)
            return result
        if command != "STORE" or self.is_readonly:
            raise AssertionError("Only explicit RW STORE is authorized")
        self.server.stores += 1
        if self.server.before_store:
            self.server.before_store(self, args)
        modifier, argument = args[1:]
        if modifier not in ("+FLAGS.SILENT", "-FLAGS.SILENT"):
            raise AssertionError("Never replace all flags")
        names = argument[1:-1].split()
        if any(name not in ("\\Seen", "\\Flagged") for name in names) or not names:
            raise AssertionError("Only Seen and Flagged may change")
        if self.server.timeout_before:
            raise TimeoutError("private-secret-server-error")
        if self.server.refuse_store:
            return "NO", [b"private-secret-server-error"]
        if not self.server.ignore_store:
            existing = self.server.flags[key]
            if modifier == "+FLAGS.SILENT":
                existing.extend(flag for flag in names if flag.casefold() not in {v.casefold() for v in existing})
            else:
                self.server.flags[key] = [flag for flag in existing if flag.casefold() not in {v.casefold() for v in names}]
        if self.server.after_store:
            self.server.after_store(self)
        if self.server.timeout_after:
            raise TimeoutError("private-secret-server-error")
        return "OK", [b"updated"]


class FlagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir(mode=0o700)
        self.profile = {"id": "one", "host": "imap.example.test", "user": "owner@example.test",
                        "entity": "synthetic", "password_env": "DO_NOT_USE_SYNTHETIC_SECRET"}
        self.json(self.profiles / "one.json", self.profile)
        # Deliberately no From allowlist/signature/SMTP/drafts authorization.
        self.entry = {"identity": {"email": "owner@example.test", "host": "imap.example.test", "entity": "synthetic"},
                      "flags_write": True}
        self.config = {"version": 1, "profiles_dir": str(self.profiles), "state_dir": str(self.root / "state"),
                       "accounts": {"one": self.entry}}
        self.config_path = self.root / "settings.json"
        self.save_config()
        self.server = FlagsServer()
        self.service = FlagService(config_path=self.config_path, reader_factory=self.server.reader,
                                   smtp_factory=lambda *a, **k: self.fail("Flags must never construct SMTP"))
        self.ref = {"account": "one", "mailbox": "INBOX", "uidvalidity": "7", "uid": "13"}

    def json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def save_config(self):
        self.json(self.config_path, self.config)

    def preview(self, messages=None, **kwargs):
        return self.service.flags_preview("one", [self.ref] if messages is None else messages,
                                          **({"seen": True} if not kwargs else kwargs))

    def apply(self, preview):
        return self.service.flags_apply(preview["preview_id"], preview["preview_hash"])

    def status(self, preview):
        return self.service.flags_status(preview["preview_id"])

    def record(self, preview):
        return json.loads((self.root / "state" / "flags" / (preview["preview_id"] + ".json")).read_text())

    def error(self, code, call):
        with self.assertRaises(DraftError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_preview_is_readonly_private_and_independent_of_authoring_setup(self):
        preview = self.preview()
        self.assertEqual(preview["status"], "prepared")
        self.assertEqual(preview["messages"][0]["before"], {"seen": False})
        self.assertEqual(preview["messages"][0]["content_sha256"], digest(self.server.messages["INBOX"]["13"]))
        self.assertEqual(preview["concurrency_protection"], "preflight_only")
        self.assertFalse(preview["atomic"])
        self.assertEqual(self.server.stores, 0)
        self.assertEqual(self.server.write_selections, 0)
        self.assertEqual(list((self.root / "state").glob("*.json")), [])
        folder = self.root / "state" / "flags"
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        for path in folder.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Private contents", json.dumps(preview))
        self.assertEqual(self.status(preview)["status"], "prepared")
        self.assertEqual(self.server.stores, 0)

    def test_add_seen_and_remove_flagged_preserves_custom_recent_draft_deleted(self):
        original = ["\\Flagged", "\\Draft", "\\Recent", "\\Deleted", "$Label1", "project_1"]
        self.server.flags[("INBOX", "13")] = list(original)
        preview = self.preview(seen=True, flagged=False)
        before = self.server.readers
        result = self.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["mailbox_writes"], 2)
        self.assertGreaterEqual(self.server.readers - before, 3)
        self.assertEqual(set(self.server.flags[("INBOX", "13")]), set(original) - {"\\Flagged"} | {"\\Seen"})
        commands = [row for row in self.server.commands if row[0] == "STORE"]
        self.assertEqual([row[3:] for row in commands], [("+FLAGS.SILENT", "(\\Seen)"), ("-FLAGS.SILENT", "(\\Flagged)")])
        for command in self.server.commands:
            self.assertNotIn(command[0], ("APPEND", "CLOSE", "EXPUNGE", "SMTP"))

    def test_batch_across_mailboxes_preserves_unrequested_flag(self):
        self.server.flags[("INBOX", "13")] = ["\\Flagged"]
        archive = {"account": "one", "mailbox": "Archive", "uidvalidity": "9", "uid": "15"}
        result = self.apply(self.preview([self.ref, archive]))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(result["receipt"]["messages"]), 2)
        self.assertEqual(self.server.flags[("INBOX", "13")], ["\\Flagged", "\\Seen"])
        self.assertEqual(self.server.flags[("Archive", "15")], ["\\Seen"])

    def test_noop_has_no_store_and_verified_only_by_independent_readback(self):
        self.server.flags[("INBOX", "13")] = ["\\Seen"]
        result = self.apply(self.preview())
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["mailbox_writes"], 0)
        self.assertEqual(self.server.stores, 0)
        self.assertFalse(result["attempted"])

    def test_same_preview_applied_concurrently_never_repeats_store(self):
        preview = self.preview()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.apply(preview), range(2)))
        self.assertEqual([result["status"] for result in results], ["verified", "verified"])
        self.assertEqual(self.server.stores, 1)
        self.assertEqual(sum(result["mailbox_writes"] for result in results), 1)

    def test_requires_independent_flags_authorization(self):
        self.entry.update(drafts_enabled=True, send_enabled=True)
        self.entry.pop("flags_write")
        self.save_config()
        self.error("FLAGS_NOT_AUTHORIZED", self.preview)
        self.assertEqual(self.server.readers, 0)
        for value in (False, 1, "true", None):
            self.entry["flags_write"] = value
            self.save_config()
            self.error("FLAGS_NOT_AUTHORIZED", self.preview)

    def test_ref_and_target_validation_before_connection(self):
        for messages, code in (([], "INVALID_FLAGS_MESSAGES"), ([self.ref] * 101, "INVALID_FLAGS_MESSAGES"),
                               ([self.ref, dict(self.ref)], "DUPLICATE_MESSAGE_REFERENCE"),
                               ([{**self.ref, "account": "two"}], "FLAGS_ACCOUNT_MISMATCH"),
                               ([{**self.ref, "uid": "13:*"}], "INVALID_MESSAGE_REFERENCE"),
                               ([{**self.ref, "uid": True}], "INVALID_MESSAGE_REFERENCE"),
                               ([{**self.ref, "uidvalidity": "4294967296"}], "INVALID_MESSAGE_REFERENCE"),
                               ([{**self.ref, "mailbox": "INBOX\r\nSTORE"}], "INVALID_HEADER"),
                               ([{**self.ref, "extra": "x"}], "INVALID_MESSAGE_REFERENCE")):
            with self.subTest(messages=messages):
                self.error(code, lambda: self.preview(messages))
        self.error("FLAGS_CHANGE_REQUIRED", lambda: self.preview(seen=None, flagged=None))
        for value in (1, 0, "true", [], {}):
            self.error("INVALID_FLAGS_VALUES", lambda: self.preview(seen=value))
        self.assertEqual(self.server.readers, 0)

    def test_uid_int_and_string_duplicate_are_same_identity(self):
        self.error("DUPLICATE_MESSAGE_REFERENCE", lambda: self.preview([self.ref, {**self.ref, "uid": 13}]))
        self.error("DUPLICATE_MESSAGE_REFERENCE", lambda: self.preview([self.ref, {**self.ref, "mailbox": "inbox"}]))

    def test_uidvalidity_changed_before_apply_has_no_store(self):
        preview = self.preview()
        self.server.validity["INBOX"] = "8"
        self.error("UIDVALIDITY_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.stores, 0)

    def test_content_or_requested_flag_changed_before_apply_has_no_store(self):
        preview = self.preview()
        original = self.server.messages["INBOX"]["13"]
        self.server.messages["INBOX"]["13"] = original + b" changed"
        self.error("FLAGS_MESSAGE_CONTENT_CHANGED", lambda: self.apply(preview))
        self.server.messages["INBOX"]["13"] = original
        self.server.flags[("INBOX", "13")] = ["\\Seen"]
        self.error("FLAGS_TARGET_PRECONDITION_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.stores, 0)

    def test_unrequested_flags_can_change_and_are_preserved(self):
        preview = self.preview()
        self.server.flags[("INBOX", "13")] = ["\\Flagged", "$new_custom"]
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.flags[("INBOX", "13")], ["\\Flagged", "$new_custom", "\\Seen"])

    def test_apply_rechecks_config_and_profile_identity(self):
        preview = self.preview()
        self.entry["flags_write"] = False
        self.save_config()
        self.error("FLAGS_NOT_AUTHORIZED", lambda: self.apply(preview))
        self.entry["flags_write"] = True
        self.entry["unrelated_setting"] = "changed"
        self.save_config()
        self.error("ACCOUNT_CONFIGURATION_CHANGED", lambda: self.apply(preview))
        self.entry.pop("unrelated_setting")
        self.save_config()
        self.profile["port"] = 1993
        self.json(self.profiles / "one.json", self.profile)
        self.error("ACCOUNT_IDENTITY_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.stores, 0)

    def test_rw_select_rechecks_uidvalidity_and_readonly(self):
        preview = self.preview()
        self.server.before_select = lambda _: self.server.validity.update(INBOX="8")
        self.error("UIDVALIDITY_CHANGED", lambda: self.apply(preview))
        self.server.validity["INBOX"] = "7"
        self.server.before_select = None
        self.server.force_readonly = True
        self.error("FLAGS_WRITE_SELECTION_FAILED", lambda: self.apply(preview))
        self.assertEqual(self.server.stores, 0)

    def test_durable_intent_exists_before_store_then_lost_ack_is_verified(self):
        preview = self.preview()
        def check_intent(_reader, _args):
            record = self.record(preview)
            self.assertTrue(record["attempted"])
            self.assertEqual(record["state"], "attempted")
            self.assertEqual(len(record["store_attempts"]), 1)
        self.server.before_store = check_intent
        self.server.timeout_after = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["store_error_code"], "FLAGS_STORE_RESPONSE_UNCERTAIN")
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertEqual(self.status(preview)["status"], "verified")
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.stores, 1)

    def test_timeout_before_store_never_retries_even_if_status_unchanged(self):
        preview = self.preview()
        self.server.timeout_before = True
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.server.timeout_before = False
        for _ in range(2):
            self.assertEqual(self.status(preview)["status"], "uncertain")
            self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.server.stores, 1)
        self.error("FLAGS_SOURCE_OPERATION_UNRESOLVED", self.preview)

    def test_partial_batch_stops_on_first_uncertainty_no_resume(self):
        second = {**self.ref, "uid": "14"}
        preview = self.preview([self.ref, second])
        self.server.timeout_after = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.server.flags[("INBOX", "13")], ["\\Seen"])
        self.assertEqual(self.server.flags[("INBOX", "14")], [])
        self.server.timeout_after = False
        self.apply(preview)
        self.status(preview)
        self.assertEqual(self.server.stores, 1)

    def test_independent_readback_does_not_trust_tagged_ok(self):
        preview = self.preview()
        self.server.ignore_store = True
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.server.stores, 1)
        self.apply(preview)
        self.assertEqual(self.server.stores, 1)

    def test_later_human_change_is_conflict_not_undone_by_status_or_apply(self):
        preview = self.preview()
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.server.flags[("INBOX", "13")] = []
        self.assertEqual(self.status(preview)["status"], "conflict")
        self.assertEqual(self.apply(preview)["status"], "conflict")
        self.assertEqual(self.server.stores, 1)
        self.assertEqual(self.server.flags[("INBOX", "13")], [])
        # A completed old operation does not indefinitely prevent a newly
        # reviewed human request based on the new current flag state.
        new_preview = self.preview(flagged=True)
        self.assertEqual(self.apply(new_preview)["status"], "verified")
        self.assertEqual(self.server.flags[("INBOX", "13")], ["\\Flagged"])

    def test_intervening_human_change_between_add_remove_stops_batch(self):
        self.server.flags[("INBOX", "13")] = ["\\Flagged"]
        preview = self.preview(seen=True, flagged=False)
        self.server.after_store = lambda _: self.server.flags[("INBOX", "13")].remove("\\Seen")
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["store_error_code"], "FLAGS_TARGET_PRECONDITION_CHANGED")
        self.assertEqual(self.server.stores, 1)
        self.assertEqual(self.server.flags[("INBOX", "13")], ["\\Flagged"])

    def test_missing_message_after_store_is_conflict_and_no_replay(self):
        preview = self.preview()
        self.server.after_store = lambda _: self.server.messages["INBOX"].pop("13")
        result = self.apply(preview)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["error_code"], "FLAGS_MESSAGE_DISAPPEARED")
        self.apply(preview)
        self.assertEqual(self.server.stores, 1)

    def test_config_revoked_during_batch_prevents_further_writes(self):
        preview = self.preview([self.ref, {**self.ref, "uid": "14"}])
        def revoke(_):
            self.entry["flags_write"] = False
            self.save_config()
        self.server.after_store = revoke
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["store_error_code"], "FLAGS_NOT_AUTHORIZED")
        self.assertEqual(self.server.stores, 1)
        status = self.status(preview)
        self.assertEqual(status["status"], "uncertain")
        self.assertTrue(status["configuration_changed"])
        self.assertEqual(self.apply(preview)["status"], "uncertain")
        self.assertEqual(self.server.stores, 1)

    def test_config_revoked_during_selected_preflight_prevents_first_store(self):
        preview = self.preview()
        def revoke(reader):
            if not reader.is_readonly:
                self.entry["flags_write"] = False
                self.save_config()
        self.server.after_fetch = revoke
        self.error("FLAGS_NOT_AUTHORIZED", lambda: self.apply(preview))
        self.assertEqual(self.server.stores, 0)
        self.assertFalse(self.record(preview)["attempted"])
        status = self.status(preview)
        self.assertEqual(status["status"], "prepared")
        self.assertTrue(status["configuration_changed"])
        self.error("FLAGS_NOT_AUTHORIZED", lambda: self.apply(preview))
        self.assertEqual(self.server.stores, 0)

    def test_bad_preview_hash_and_tampered_immutable_are_rejected(self):
        preview = self.preview()
        self.error("PREVIEW_HASH_MISMATCH", lambda: self.service.flags_apply(preview["preview_id"], "bad"))
        path = self.root / "state" / "flags" / (preview["preview_id"] + ".json")
        record = self.record(preview)
        record["immutable"]["desired"]["seen"] = False
        self.json(path, record)
        self.error("PREVIEW_INTEGRITY_FAILED", lambda: self.status(preview))
        self.assertEqual(self.server.stores, 0)

    def test_broker_grant_is_not_an_imap_write_grant(self):
        with patch.dict(os.environ, {"MAIL_BROKER_CONFIG": "/synthetic/private"}):
            self.error("BROKER_FLAGS_ADAPTER_REQUIRED", self.preview)
        self.assertEqual(self.server.readers, 0)

    def test_accounts_exposes_only_verified_flags_configuration_without_authoring(self):
        result = self.service.accounts()
        row = result["accounts"][0]
        self.assertTrue(row["flags_enabled"])
        self.assertTrue(row["read_ready"])
        self.assertFalse(row["drafts_ready"])
        self.assertEqual(row["flags_connection"], "not_checked")
        self.assertEqual(row["flags_issues"], [])
        self.assertEqual(self.server.readers, 0)
        self.assertNotIn("password", json.dumps(result))
        self.entry["flags_write"] = False
        self.save_config()
        self.assertFalse(self.service.accounts()["accounts"][0]["flags_enabled"])
        self.assertFalse(self.entry["flags_write"])

    def test_status_after_write_revocation_still_checks_identity_and_content(self):
        preview = self.preview()
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.entry["flags_write"] = False
        self.save_config()
        status = self.status(preview)
        self.assertEqual(status["status"], "verified")
        self.assertTrue(status["configuration_changed"])
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.server.messages["INBOX"]["13"] += b" changed"
        self.assertEqual(self.status(preview)["status"], "conflict")
        self.assertEqual(self.server.stores, 1)
        self.profile["port"] = 1993
        self.json(self.profiles / "one.json", self.profile)
        self.error("ACCOUNT_IDENTITY_CHANGED", lambda: self.status(preview))

    def test_accounts_never_enables_flags_for_identity_mismatch(self):
        self.profile["entity"] = "another_entity"
        self.json(self.profiles / "one.json", self.profile)
        row = self.service.accounts()["accounts"][0]
        self.assertFalse(row["flags_enabled"])
        self.assertIn("ACCOUNT_IDENTITY_MISMATCH", row["flags_issues"])
        self.assertEqual(self.server.readers, 0)

    def test_preflights_entire_batch_before_any_store(self):
        second = {**self.ref, "uid": "14"}
        preview = self.preview([self.ref, second])
        self.server.messages["INBOX"]["14"] += b" changed"
        self.error("FLAGS_MESSAGE_CONTENT_CHANGED", lambda: self.apply(preview))
        self.assertEqual(self.server.stores, 0)
        self.assertEqual(self.server.write_selections, 0)

    def test_refused_store_is_not_retried_and_server_reply_stays_private(self):
        preview = self.preview()
        self.server.refuse_store = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["store_error_code"], "FLAGS_STORE_NOT_CONFIRMED")
        self.assertNotIn("private-secret", json.dumps(result))
        self.server.refuse_store = False
        self.apply(preview)
        self.assertEqual(self.server.stores, 1)

    def test_readback_timeout_remains_uncertain_and_status_only_observes(self):
        preview = self.preview()
        self.server.after_store = lambda _: setattr(self.server, "fail_reads", True)
        result = self.apply(preview)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["error_code"], "FLAGS_READBACK_FAILED")
        self.assertNotIn("private-secret", json.dumps(result))
        self.server.fail_reads = False
        self.assertEqual(self.status(preview)["status"], "verified")
        self.assertEqual(self.server.stores, 1)

    def test_hundred_unique_references_are_supported(self):
        messages = []
        for uid in range(1, 101):
            value = str(uid)
            self.server.messages["INBOX"][value] = f"Subject: {uid}\r\n\r\nmessage".encode()
            self.server.flags[("INBOX", value)] = []
            messages.append({**self.ref, "uid": value})
        result = self.apply(self.preview(messages, flagged=True))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.server.stores, 100)
        self.assertEqual(len(result["receipt"]["messages"]), 100)

    def test_non_ascii_mailbox_uses_modified_utf7_and_quoted_literal_name(self):
        name = 'Żółte "foldery"'
        self.server.messages[name] = {"13": b"Subject: old\r\n\r\narchived"}
        self.server.validity[name] = "11"
        self.server.flags[(name, "13")] = []
        preview = self.preview([{**self.ref, "mailbox": name, "uidvalidity": "11"}])
        self.assertEqual(self.apply(preview)["status"], "verified")
        self.assertEqual(self.server.flags[(name, "13")], ["\\Seen"])


if __name__ == "__main__":
    unittest.main()
