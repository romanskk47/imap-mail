import asyncio
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
import mcp.client.stdio as stdio

from imap_mail_bridge.server import (ConfigurationError, Journal, MAX_REQUEST_BYTES,
                                     Settings, create_app, validate_bind)

TOKEN = "s" * 43
OTHER_TOKEN = "t" * 43
FAKE = Path(__file__).with_name("fake_backend.py")
HEADERS = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
           "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}


def request(method, params=None, request_id=2):
    result = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        result["params"] = params
    return result


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        os.chmod(self.directory, 0o700)
        self.config = self.directory / "settings.json"
        self.value = {"version": 1, "backend_command": [sys.executable, str(FAKE)],
                      "principals": {"codex-cloud": hashlib.sha256(TOKEN.encode()).hexdigest()},
                      "allowed_hosts": ["mail.example.invalid"], "allowed_origins": [],
                      "journal_dir": str(self.directory)}

    def save(self):
        self.config.write_text(json.dumps(self.value))
        self.config.chmod(0o600)

    def test_private_config_loads_and_does_not_contain_raw_token(self):
        self.save()
        settings = Settings.load(str(self.config))
        self.assertEqual(settings.command, (sys.executable, str(FAKE)))
        self.assertNotIn(TOKEN, repr(settings))

    def test_permission_and_symlink_rejected(self):
        self.save()
        self.config.chmod(0o640)
        with self.assertRaises(ConfigurationError):
            Settings.load(str(self.config))
        self.config.chmod(0o600)
        link = self.directory / "linked.json"
        link.symlink_to(self.config)
        with self.assertRaises(ConfigurationError):
            Settings.load(str(link))

    def test_parent_symlink_and_hardlink_rejected(self):
        self.save()
        parent_link = self.directory / "parent"
        parent_link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            Settings.load(str(parent_link / self.config.name))
        os.link(self.config, self.directory / "hardlink.json")
        with self.assertRaises(ConfigurationError):
            Settings.load(str(self.config))

    def test_extra_fields_relative_command_wildcard_and_duplicate_principal_rejected(self):
        for change in ({"extra": "no"}, {"backend_command": ["sudo"]},
                       {"allowed_hosts": ["*.example.invalid"]},
                       {"principals": {"codex": "a" * 64, "claude": "a" * 64}},
                       {"max_concurrency": True}, {"timeout_seconds": 99999}):
            original = dict(self.value)
            self.value.update(change)
            self.save()
            with self.subTest(change=change), self.assertRaises(ConfigurationError):
                Settings.load(str(self.config))
            self.value = original

    def test_pending_receipt_survives_another_journal_instance(self):
        journal = Journal(str(self.directory))
        receipt, descriptor, record = journal.start("mail_send_apply")
        os.close(descriptor)  # Simulates losing a process after dispatch starts.
        journal.close()
        replacement = Journal(str(self.directory))
        replacement.close()
        persisted = json.loads((self.directory / (receipt + ".json")).read_text())
        self.assertEqual(persisted["status"], "pending")
        self.assertEqual(persisted, record)

    def test_bind_is_loopback_by_default_and_private_only_with_opt_in(self):
        for address in ("127.0.0.1", "::1"):
            validate_bind(address, 8766)
        for address in ("172.18.0.1", "10.0.0.1", "192.168.1.1"):
            with self.assertRaises(ConfigurationError):
                validate_bind(address, 8766)
            validate_bind(address, 8766, allow_private=True)
        for address in ("0.0.0.0", "::", "8.8.8.8", "169.254.1.1", "224.0.0.1", "fc00::1", "192.0.2.1"):
            with self.assertRaises(ConfigurationError):
                validate_bind(address, 8766, allow_private=True)
        for port in (0, 443, 65536):
            with self.assertRaises(ConfigurationError):
                validate_bind("127.0.0.1", port)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name).resolve()
        os.chmod(self.directory, 0o700)
        self.settings = Settings(
            (sys.executable, str(FAKE)),
            (("codex-cloud", hashlib.sha256(TOKEN.encode()).hexdigest()),
             ("claude-cloud", hashlib.sha256(OTHER_TOKEN.encode()).hexdigest())),
            ("mail.example.invalid",), (), str(self.directory), 2, 2, 1)
        self.app = create_app(self.settings)
        self.started = asyncio.Event()
        self.stop = asyncio.Event()
        async def lifespan_task():
            async with self.app.app.router.lifespan_context(self.app.app):
                self.started.set()
                await self.stop.wait()
        self.lifespan_task = asyncio.create_task(lifespan_task())
        await self.started.wait()
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                       base_url="https://mail.example.invalid", headers=HEADERS)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.stop.set()
        await self.lifespan_task
        self.app.app.state.backend.journal.close()
        self.tmp.cleanup()

    async def post(self, value):
        return await self.client.post("/mcp", json=value)

    async def test_initialize_is_stateless_and_does_not_start_backend(self):
        response = await self.post(request("initialize", {"protocolVersion": "2025-11-25",
            "capabilities": {}, "clientInfo": {"name": "synthetic", "version": "1"}}))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("mcp-session-id", response.headers)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_list_preserves_annotations_and_meta_without_dispatch_journal(self):
        response = await self.post(request("tools/list"))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["_meta"], {"listing": "kept"})
        tool = result["tools"][0]
        self.assertEqual(tool["annotations"], {"readOnlyHint": False, "destructiveHint": True,
                         "idempotentHint": False, "openWorldHint": True})
        self.assertEqual(tool["_meta"], {"test": "kept"})
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_call_preserves_full_result_and_named_principal(self):
        response = await self.post(request("tools/call", {"name": "mail_synthetic", "arguments": {"is_error": True}}))
        result = response.json()["result"]
        self.assertEqual(result["_meta"], {"private": "kept"})
        self.assertEqual(result["structuredContent"], {"preserved": True})
        self.assertTrue(result["isError"])
        records = list(self.directory.glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record["status"], "response_received")
        self.assertEqual(record["principal"], "codex-cloud")
        self.assertNotIn("synthetic private result", records[0].read_text())
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)

    async def test_crash_after_dispatch_is_uncertain_and_never_retried(self):
        marker = self.directory / "dispatch.count"
        response = await self.post(request("tools/call", {"name": "mail_send_apply", "arguments": {
            "crash": True, "marker_file": str(marker), "preview_id": "a" * 32}}))
        result = response.json()["result"]
        value = result["structuredContent"]
        self.assertEqual(value["status"], "uncertain")
        self.assertEqual(value["recovery_tool"], "mail_send_status")
        self.assertEqual(marker.read_text(), "one dispatch\n")
        self.assertNotIn("PRIVATE_BACKEND_SECRET", response.text)
        record = json.loads((self.directory / (value["bridge_receipt_id"] + ".json")).read_text())
        self.assertEqual(record["status"], "uncertain")

    async def test_timeout_is_uncertain_and_never_retried(self):
        marker = self.directory / "dispatch.count"
        response = await self.post(request("tools/call", {"name": "mail_draft_apply", "arguments": {
            "delay": 4, "marker_file": str(marker)}}))
        self.assertEqual(response.json()["result"]["structuredContent"]["status"], "uncertain")
        self.assertEqual(marker.read_text(), "one dispatch\n")

    async def test_client_cancel_keeps_backend_slot_until_subprocess_cleanup(self):
        backend = self.app.app.state.backend
        borrower = object()
        backend.limit.acquire_on_behalf_of_nowait(borrower)  # One of two slots.
        processes = []
        original = stdio._create_platform_compatible_process
        async def observe_process(**kwargs):
            process = await original(**kwargs)
            processes.append(process)
            return process
        marker = self.directory / "dispatch.count"
        try:
            with patch.object(stdio, "_create_platform_compatible_process", observe_process):
                task = asyncio.create_task(self.post(request("tools/call", {
                    "name": "mail_draft_apply", "arguments": {"delay": 1.5, "marker_file": str(marker)}})))
                for _ in range(100):
                    if marker.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(marker.exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.app.limit.borrowed_tokens, 0)
                self.assertEqual(backend.limit.borrowed_tokens, 2)
                self.assertIsNone(processes[0].returncode)
                response = await self.post(request("tools/call", {"name": "mail_synthetic", "arguments": {}}))
                self.assertEqual(response.json()["result"]["structuredContent"]["error_code"], "backend_busy")
                self.assertEqual(len(processes), 1)  # No extra backend after disconnect.
                for _ in range(300):
                    if backend.limit.borrowed_tokens == 1:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(backend.limit.borrowed_tokens, 1)
                self.assertIsNotNone(processes[0].returncode)
                record = json.loads(next(self.directory.glob("*.json")).read_text())
                self.assertEqual(record["status"], "uncertain")
                self.assertEqual(marker.read_text(), "one dispatch\n")
        finally:
            backend.limit.release_on_behalf_of(borrower)

    async def test_no_backend_launch_before_setup_rejection(self):
        for tool in ("mail_setup", "mail_send_setup"):
            response = await self.post(request("tools/call", {"name": tool, "arguments": {}}))
            self.assertEqual(response.json()["result"]["structuredContent"]["error_code"], "local_setup_only")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_invalid_request_is_generic_and_never_dispatched(self):
        response = await self.client.post("/mcp", content=b'{"SECRET_INVALID_REQUEST":')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("SECRET_INVALID_REQUEST", response.text)
        response = await self.post(request("tools/call", {"name": "private secret with spaces", "arguments": {}}))
        self.assertEqual(response.json()["result"]["structuredContent"]["status"], "not_dispatched")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_backend_spawn_failure_is_not_dispatched(self):
        backend = self.app.app.state.backend
        backend.settings = dataclasses.replace(self.settings, command=("/no/such/backend",))
        response = await self.post(request("tools/call", {"name": "mail_synthetic", "arguments": {}}))
        value = response.json()["result"]["structuredContent"]
        self.assertEqual(value["status"], "not_dispatched")
        self.assertEqual(value["error_code"], "backend_unavailable")
        self.assertNotIn("/no/such/backend", response.text)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_deny_missing_wrong_duplicate_auth_on_all_paths(self):
        for path in ("/mcp", "/health", "/.well-known/oauth-protected-resource", "/"):
            for headers in ({}, {"Authorization": "Bearer " + "u" * 43},
                            {"Authorization": "Bearer short"}):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                              base_url="https://mail.example.invalid") as client:
                    response = await client.post(path, headers=headers, content=b"private")
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("private", response.text)
        response = await self.client.post("/mcp", headers=[("Authorization", "Bearer " + TOKEN),
                                                          ("Authorization", "Bearer " + TOKEN)])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_host_origin_query_body_limits(self):
        for headers in ({"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}):
            response = await self.client.post("/mcp", headers=headers, json=request("tools/list"))
            self.assertEqual(response.status_code, 403)
        response = await self.client.post("/mcp?token=" + TOKEN, json=request("tools/list"))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(TOKEN, response.text)
        response = await self.client.post("/mcp", content=b"x" * (MAX_REQUEST_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_separate_claude_principal_and_parallel_limit(self):
        response = await self.client.post("/mcp", headers={"Authorization": "Bearer " + OTHER_TOKEN},
            json=request("tools/call", {"name": "mail_synthetic", "arguments": {}}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(next(self.directory.glob("*.json")).read_text())["principal"], "claude-cloud")
        borrower = object()
        self.app.limit.acquire_on_behalf_of_nowait(borrower)
        # Different borrower must take the other slot.
        ready = asyncio.Event()
        release = asyncio.Event()
        async def occupy():
            self.app.limit.acquire_nowait()
            ready.set()
            await release.wait()
            self.app.limit.release()
        task = asyncio.create_task(occupy())
        await ready.wait()
        try:
            response = await self.post(request("tools/list"))
            self.assertEqual(response.status_code, 429)
        finally:
            release.set()
            await task
            self.app.limit.release_on_behalf_of(borrower)


if __name__ == "__main__":
    unittest.main()
