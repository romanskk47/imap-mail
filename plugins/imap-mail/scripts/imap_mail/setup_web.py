"""Ephemeral loopback-only onboarding. Secrets enter directly in the browser.

The public MCP entrypoint intentionally never returns the bootstrap URL/token.
There is no SMTP, APPEND, filesystem browsing or third-party asset endpoint.
"""

from __future__ import annotations

import datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from typing import Any
import uuid
import webbrowser

from .core import DraftError

TTL_SECONDS = 15 * 60
MAX_BODY = 64 * 1024
MAX_ATTEMPTS = 3
ASSETS = Path(__file__).resolve().parents[2] / "assets"
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; font-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
_REGISTRY: set[Any] = set()
_REGISTRY_LOCK = threading.Lock()


class SetupWebError(DraftError):
    """Fixed public codes share the MCP adapter's sanitized error contract."""


def _safe_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", code) else "SETUP_OPERATION_FAILED"


def _default_factory(config_path=None):
    from .setup import SetupSession
    return SetupSession(config_path=config_path)


class _QuietServer(HTTPServer):
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        pass  # Tracebacks can include request bodies; never log them.


class _Controller:
    def __init__(self, config_path=None, session_factory=None, ttl_seconds=TTL_SECONDS, prefill=None):
        from .setup import known_provider_host, validate_prefill
        self.prefill = validate_prefill(prefill)
        self.setup_id = uuid.uuid4().hex
        self.agent_host_supplied = "host" in self.prefill
        provider_host = known_provider_host(self.prefill.get("email"))
        self.host_source = ("manual" if not self.agent_host_supplied else
                            "known_provider" if provider_host == self.prefill["host"] else "agent_suggestion")
        self.require_host_retype = self.host_source == "agent_suggestion"
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.deadline = time.monotonic() + max(1, min(ttl_seconds, TTL_SECONDS))
        self.expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(1, min(ttl_seconds, TTL_SECONDS)))).isoformat()
        self.session = (session_factory or _default_factory)(config_path=config_path)
        self.attempts = 0
        self.active = True
        self.result = "waiting"
        self.server = _QuietServer(("127.0.0.1", 0), _Handler)
        self.server.controller = self
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.url = self.origin + "/#token=" + self.token
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.worker.start()
        threading.Thread(target=self._expiry, daemon=True).start()

    def _expiry(self):
        if not self.done.wait(max(0, self.deadline - time.monotonic())):
            self.close("expired")

    def close(self, result="closed"):
        with self.lock:
            if not self.active:
                return
            self.active = False
            self.result = result
            self.token = ""
            self.prefill.clear()
            try:
                self.session.close()
            except Exception:
                pass
            self.done.set()
        # shutdown must not run on the request-serving thread.
        def stop():
            self.server.shutdown()
            self.server.server_close()
            with _REGISTRY_LOCK:
                _REGISTRY.discard(self)
        threading.Thread(target=stop, daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalSetup"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"status": "error", "error_code": "REQUEST_REJECTED"})

    @property
    def controller(self):
        return self.server.controller

    def _respond(self, code, data, content_type):
        self.close_connection = True
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except (OSError, ValueError):
            pass

    def _json(self, code, value):
        self._respond(code, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def _reject(self, code=403, reason="REQUEST_REJECTED"):
        self._json(code, {"status": "error", "error_code": reason})

    def _host_ok(self):
        return self.headers.get_all("Host", []) == [self.controller.host]

    def do_GET(self):
        if not self._host_ok():
            return self._reject()
        if not self.controller.active or time.monotonic() >= self.controller.deadline:
            return self._reject(410, "SESSION_EXPIRED")
        routes = {"/": ("setup.html", "text/html; charset=utf-8"),
                  "/setup.css": ("setup.css", "text/css; charset=utf-8"),
                  "/setup.js": ("setup.js", "text/javascript; charset=utf-8")}
        if self.path not in routes:
            return self._reject(404, "NOT_FOUND")
        filename, content_type = routes[self.path]
        try:
            content = (ASSETS / filename).read_bytes()
        except OSError:
            return self._reject(503, "SETUP_ASSET_UNAVAILABLE")
        self._respond(200, content, content_type)

    def do_POST(self):
        if not self._host_ok() or self.headers.get_all("Origin", []) != [self.controller.origin]:
            return self._reject()
        with self.controller.lock:
            if not self.controller.active or time.monotonic() >= self.controller.deadline:
                return self._reject(410, "SESSION_EXPIRED")
            tokens = self.headers.get_all("X-Setup-Token", [])
            if len(tokens) != 1 or not tokens[0].isascii() or not secrets.compare_digest(tokens[0], self.controller.token):
                return self._reject()
            if self.path not in ("/api/context", "/api/suggest", "/api/check", "/api/save", "/api/cancel"):
                return self._reject(404, "NOT_FOUND")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]):
                return self._reject(400, "INVALID_CONTENT_LENGTH")
            length = int(lengths[0])
            if not 1 <= length <= MAX_BODY:
                return self._reject(413, "REQUEST_TOO_LARGE")
            if self.headers.get_all("Content-Type", []) != ["application/json"]:
                return self._reject(415, "JSON_REQUIRED")
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self._reject(400, "INCOMPLETE_REQUEST")
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    return self._reject(400, "INVALID_REQUEST")
            except (OSError, UnicodeError, json.JSONDecodeError):
                return self._reject(400, "INVALID_REQUEST")
            try:
                if self.path == "/api/context":
                    if value:
                        return self._reject(400, "INVALID_REQUEST")
                    result = {"status": "ready", "prefill": dict(self.controller.prefill),
                              "host_source": self.controller.host_source,
                              "require_host_retype": self.controller.require_host_retype, "mailbox_writes": 0}
                elif self.path == "/api/suggest":
                    if set(value) != {"email"} or not isinstance(value["email"], str):
                        return self._reject(400, "INVALID_REQUEST")
                    result = self.controller.session.suggest(value["email"])
                elif self.path == "/api/check":
                    from .setup import known_provider_host
                    typed_host = value.pop("agent_host_confirmation", None)
                    host_retype_required = self.controller.agent_host_supplied and (
                        self.controller.require_host_retype or known_provider_host(value.get("email")) != value.get("host"))
                    if host_retype_required and (not isinstance(typed_host, str) or typed_host != value.get("host")):
                        return self._reject(400, "HOST_RETYPE_REQUIRED")
                    if self.controller.attempts >= MAX_ATTEMPTS:
                        return self._reject(429, "LOGIN_ATTEMPT_LIMIT")
                    self.controller.attempts += 1
                    result = self.controller.session.check(value)
                elif self.path == "/api/save":
                    if set(value) != {"check_id", "storage_consent"} or value["storage_consent"] is not True or not isinstance(value["check_id"], str):
                        return self._reject(400, "STORAGE_CONSENT_REQUIRED")
                    result = self.controller.session.save(value["check_id"])
                else:
                    if value:
                        return self._reject(400, "INVALID_REQUEST")
                    self._json(200, {"status": "cancelled", "mailbox_writes": 0})
                    self.controller.close("cancelled")
                    return
            except Exception as exc:
                return self._reject(400, _safe_code(exc))
            self._json(200, result)
            if self.path == "/api/save" and result.get("status") == "saved":
                self.controller.close("saved")

    def do_OPTIONS(self):
        self._reject()

    def do_HEAD(self):
        self._reject(405)


def _launch(config_path=None, session_factory=None, ttl_seconds=TTL_SECONDS, prefill=None):
    """Internal handle for the local browser/fixtures, never an MCP result."""
    from .setup import validate_prefill
    prefill = validate_prefill(prefill)
    with _REGISTRY_LOCK:
        _REGISTRY.difference_update({item for item in _REGISTRY if not item.active})
        account = prefill.get("account")
        if account and any(item.prefill.get("account") == account for item in _REGISTRY):
            raise SetupWebError("SETUP_ALREADY_OPEN")
        if len(_REGISTRY) >= 3:
            raise SetupWebError("TOO_MANY_SETUP_SESSIONS")
        controller = _Controller(config_path, session_factory, ttl_seconds, prefill)
        _REGISTRY.add(controller)
        return controller


def _open(controller, open_browser):
    opened = False
    if open_browser:
        try:
            opened = bool(webbrowser.open(controller.url, new=1, autoraise=True))
        except Exception:
            pass
    result = {"status": "browser_opened" if opened else "manual_setup_required",
              "setup_id": controller.setup_id,
              "needs_human": ["credential", "host_confirmation", "storage_consent"],
              "expires_at": controller.expires_at, "expires_in_seconds": TTL_SECONDS,
              "mailbox_writes": 0}
    if not opened:
        controller.close("browser_unavailable")
        result["next_step"] = "Run the local imap-mail setup command with --no-browser in your own terminal. Do not paste credentials into chat."
    return result


def start_setup(config_path=None, session_factory=None, open_browser=True, prefill=None):
    """Open a local browser and return only non-secret status to the agent."""
    controller = _launch(config_path, session_factory, prefill=prefill)
    return _open(controller, open_browser)


def serve_setup(config_path=None, session_factory=None, open_browser=True, prefill=None):
    """Human CLI mode; URL is printed only when explicitly using --no-browser."""
    if not open_browser and not sys.stdout.isatty():
        print(json.dumps({"status": "manual_setup_required", "error_code": "INTERACTIVE_TERMINAL_REQUIRED",
                          "next_step": "Run setup --no-browser directly in your own interactive terminal. Do not paste credentials into chat.",
                          "mailbox_writes": 0}, separators=(",", ":")), flush=True)
        return 1
    controller = _launch(config_path, session_factory, prefill=prefill)
    if open_browser:
        ready = _open(controller, True)
    else:
        ready = {"status": "manual_setup_ready", "url": controller.url,
                 "expires_at": controller.expires_at, "mailbox_writes": 0}
    print(json.dumps(ready, ensure_ascii=False, separators=(",", ":")), flush=True)
    try:
        controller.done.wait()
    except KeyboardInterrupt:
        controller.close("cancelled")
    return 0 if controller.result == "saved" else 1
