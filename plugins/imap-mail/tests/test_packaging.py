"""Portable package shape; no local profiles or real network access."""

import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_manifests_agree_and_onboarding_is_bundled(self):
        portable = json.loads((ROOT / "plugin.json").read_text())
        claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(portable["name"], claude["name"])
        self.assertEqual(portable["version"], claude["version"])
        path = portable["extensions"]["com.openai"]["onboardingSkill"]
        self.assertTrue(path.startswith("./skills/"))
        self.assertTrue((ROOT / path).is_file())
        self.assertTrue((ROOT / "mcp.json").is_file())
        self.assertTrue((ROOT / ".mcp.json").is_file())
        entries = sorted((ROOT / "skills").rglob("SKILL.md"))
        self.assertEqual([ROOT / "skills/mail/SKILL.md"], entries)
        self.assertEqual(entries[0].resolve(), (ROOT / path).resolve())
        for document in (ROOT / "skills/mail").rglob("*.md"):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                resource = (document.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(resource.is_relative_to(ROOT), str(resource))
                self.assertTrue(resource.is_file(), f"{document}: {target}")

    def test_clean_install_has_no_machine_profiles_and_handshakes(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            package = staging / "plugin"
            shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc"))
            home = staging / "new-person"
            home.mkdir(mode=0o700)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("MAIL_") and key != "PYTHONPATH"}
            env["HOME"] = str(home)
            requests = [
                {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                 "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                            "clientInfo": {"name": "packaging-test", "version": "1"}}},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            ]
            result = subprocess.run([sys.executable, str(package / "scripts/imap_mail_mcp.py")],
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True, text=True, env=env, check=True)
            self.assertEqual("", result.stderr)
            initialization, listing = map(json.loads, result.stdout.splitlines())
            manifest = json.loads((package / "plugin.json").read_text())
            self.assertEqual(manifest["version"], initialization["result"]["serverInfo"]["version"])
            tools = listing["result"]["tools"]
            self.assertIn("mail_setup", {entry["name"] for entry in tools})
            accounts = subprocess.run([sys.executable, str(package / "scripts/imap_mail.py"), "accounts"],
                capture_output=True, text=True, env=env, check=True)
            data = json.loads(accounts.stdout)
            self.assertEqual([], data["accounts"])
            self.assertEqual(0, data["mailbox_writes"])
            self.assertNotIn("password", accounts.stdout.lower())
            self.assertFalse((home / ".config/imap-mail/settings.json").exists())
            for path in package.rglob("*"):
                self.assertFalse(path.is_symlink(), str(path))
                self.assertNotIn(path.name, {"settings.json", "secrets", "credentials", ".env"})


    def test_clean_runtime_reuses_its_injected_profiles_without_exporting_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "private.json"
            config = {"version": 1, "accounts": {"work": {
                "identity": {"email": "owner@example.test", "host": "imap.example.test", "entity": "owner"},
                "drafts_enabled": False,
            }}}
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            profile = {"id": "work", "host": "imap.example.test", "user": "owner@example.test",
                       "entity": "owner", "password_env": "TEST_MAIL_SECRET"}
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("MAIL_") and key != "PYTHONPATH"}
            env.update(MAIL_DRAFTS_CONFIG=str(config_path), MAIL_PROFILES_JSON=json.dumps(profile),
                       TEST_MAIL_SECRET="SYNTHETIC-HOST-SECRET-DO-NOT-EXPORT")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/imap_mail.py"), "capabilities"],
                capture_output=True, text=True, env=env, check=True)
            data = json.loads(result.stdout)
            rows = data["local_configuration"]["accounts"]
            self.assertEqual(["work"], [row["account"] for row in rows])
            self.assertTrue(rows[0]["read_ready"])
            self.assertFalse(rows[0]["drafts_ready"])
            self.assertEqual("not_checked", rows[0]["connection"])
            self.assertEqual(0, data["mailbox_writes"])
            self.assertEqual(config, json.loads(config_path.read_text()))
            self.assertNotIn(env["TEST_MAIL_SECRET"], result.stdout + result.stderr)
            self.assertNotIn("TEST_MAIL_SECRET", result.stdout + result.stderr)
            self.assertEqual({"private.json"}, {path.name for path in root.iterdir()})


if __name__ == "__main__":
    unittest.main()
