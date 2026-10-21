"""Portable package shape; no local profiles or real network access."""

import json
import os
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
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            result = subprocess.run([sys.executable, str(package / "scripts/imap_mail_mcp.py")],
                input=json.dumps(request) + "\n", capture_output=True, text=True, env=env, check=True)
            self.assertEqual("", result.stderr)
            tools = json.loads(result.stdout)["result"]["tools"]
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


if __name__ == "__main__":
    unittest.main()
