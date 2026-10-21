"""Private, existing-account SMTP opt-in; no secrets enter tool arguments.

The browser authorizes reuse of one pinned IMAP credential for a separately
confirmed SMTP host. Checks cannot submit email. Saving changes only that
account's send_enabled/smtp settings, never its identity or draft configuration.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
import webbrowser

from . import setup, setup_web
from .core import DraftService


class SendSetupError(setup.SetupError):
    pass


def _snapshot(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate_proposal(account, host=None, port=None, security=None, sent_copy=None):
    """Public metadata only; no provider inference and no implied consent."""
    account = setup._text(account, 80, "INVALID_ACCOUNT_ID")
    if not setup.ACCOUNT_ID.fullmatch(account):
        raise SendSetupError("INVALID_ACCOUNT_ID")
    result = {"account": account}
    if host is not None:
        try:
            result["host"] = setup._validated_host(host)
        except setup.SetupError:
            raise SendSetupError("INVALID_SMTP_HOST") from None
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise SendSetupError("INVALID_SMTP_PORT")
        result["port"] = port
    if security is not None:
        if security not in ("ssl", "starttls"):
            raise SendSetupError("SMTP_TLS_REQUIRED")
        result["security"] = security
    if sent_copy is not None:
        if sent_copy not in ("server", "append"):
            raise SendSetupError("INVALID_SENT_COPY_MODE")
        result["sent_copy"] = sent_copy
    return result


class SendSetupSession:
    def __init__(self, config_path=None, account=None, checker=None):
        self.storage = setup.SetupSession(config_path=config_path)
        self.config_path = self.storage.config_path
        self.account = validate_proposal(account)["account"]
        self.checker = checker or self._default_check
        self._mutex = threading.RLock()
        self._closed = False
        self._attempts = 0
        self._candidates = {}
        # Fail before opening a browser when the exact existing identity is not
        # available. Profile metadata is read, never the password source here.
        self._identity()

    def _identity(self):
        config, original, _, profiles = self.storage._existing()
        service = DraftService(config_path=self.config_path)
        entry, profile = service._account(config, self.account, profiles=profiles)
        return config, original, entry, profile

    def _open(self):
        if self._closed:
            raise SendSetupError("SETUP_SESSION_CLOSED")
        now = time.monotonic()
        self._candidates = {key: item for key, item in self._candidates.items()
                            if now - item["created"] < setup.CHECK_LIFETIME}

    def close(self):
        with self._mutex:
            self._closed = True
            self._candidates.clear()
            self.storage.close()

    @setup.guarded
    def context(self):
        with self._mutex:
            self._open()
            _, _, _, profile = self._identity()
            return {"account": self.account, "email": profile["user"], "entity": profile["entity"]}

    @setup.guarded
    def suggest(self, email):
        # Unlike the IMAP new-account wizard, this flow never guesses SMTP
        # hosts from an address or an existing IMAP host.
        raise SendSetupError("SMTP_HOST_REQUIRED")

    def _payload(self, payload):
        allowed = {"host", "port", "security", "sent_copy", "host_confirmation",
                   "host_confirmed", "credential_reuse_consent", "enable_sending_consent"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise SendSetupError("INVALID_SEND_SETUP_INPUT")
        proposal = validate_proposal(self.account, payload.get("host"), payload.get("port"),
                                     payload.get("security"), payload.get("sent_copy"))
        if not all(key in proposal for key in ("host", "port", "security", "sent_copy")):
            raise SendSetupError("SMTP_CONFIGURATION_REQUIRED")
        if payload.get("host_confirmed") is not True or payload.get("host_confirmation") != proposal["host"]:
            raise SendSetupError("HOST_RETYPE_REQUIRED")
        if payload.get("credential_reuse_consent") is not True:
            raise SendSetupError("CREDENTIAL_REUSE_CONSENT_REQUIRED")
        if payload.get("enable_sending_consent") is not True:
            raise SendSetupError("SEND_ENABLE_CONSENT_REQUIRED")
        smtp = {key: proposal[key] for key in ("host", "port", "security", "sent_copy")}
        smtp["use_imap_credentials"] = True
        return smtp

    def _default_check(self, account, smtp):
        from .send import SendService
        return SendService(config_path=self.config_path).check_configuration(account, smtp)

    @setup.guarded
    def check(self, payload):
        with self._mutex:
            self._open()
            self._candidates.clear()
            if self._attempts >= setup.MAX_CHECK_ATTEMPTS:
                raise SendSetupError("CHECK_ATTEMPT_LIMIT")
            # Validate consent and the exact host before invoking anything that
            # can read a credential or make a network connection.
            smtp = self._payload(payload)
            self._attempts += 1
            _, _, entry, profile = self._identity()
            identity = {"account": self.account, "email": profile["user"], "entity": profile["entity"]}
            entry_pin, profile_pin = _snapshot(entry), _snapshot(profile)
            try:
                result = self.checker(self.account, copy.deepcopy(smtp))
            except Exception as exc:
                code = getattr(exc, "code", "SMTP_CONNECTION_CHECK_FAILED")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", code):
                    code = "SMTP_CONNECTION_CHECK_FAILED"
                raise SendSetupError(code) from None
            if not isinstance(result, dict) or result.get("status") != "ready":
                code = result.get("error_code") if isinstance(result, dict) else None
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", code):
                    code = "SMTP_CONNECTION_CHECK_FAILED"
                raise SendSetupError(code)
            # The checker may resolve the verified Sent mailbox by special-use
            # flags. Do not guess a folder and never create one.
            verified = None
            if smtp["sent_copy"] == "append":
                verified = result.get("sent_mailbox")
                if not isinstance(verified, str) or not verified:
                    raise SendSetupError("SENT_FOLDER_UNAVAILABLE")
                verified = setup._text(verified, 1024, "INVALID_SENT_MAILBOX")
            check_id = uuid.uuid4().hex
            self._candidates[check_id] = {"created": time.monotonic(), "smtp": smtp,
                                           "sent_mailbox": verified, "entry_pin": entry_pin, "profile_pin": profile_pin}
            return {"status": "checked", "check_id": check_id, **identity,
                    "smtp": copy.deepcopy(smtp), "expires_in_seconds": setup.CHECK_LIFETIME,
                    "sent_mailbox": verified, "mailbox_writes": 0, "messages_sent": 0}

    @setup.guarded
    def save(self, check_id):
        with self._mutex:
            self._open()
            candidate = self._candidates.pop(check_id, None) if isinstance(check_id, str) else None
            if candidate is None:
                self._candidates.clear()
                raise SendSetupError("CHECK_EXPIRED_OR_UNAVAILABLE")
            # Shared onboarding lock prevents races with another local setup.
            # Re-read and retain unrelated concurrent account/settings changes.
            with self.storage._lock([]):
                config, original, entry, profile = self._identity()
                if _snapshot(entry) != candidate["entry_pin"] or _snapshot(profile) != candidate["profile_pin"]:
                    raise SendSetupError("SETUP_CONFIGURATION_CHANGED")
                updated = copy.deepcopy(config)
                updated["accounts"][self.account]["send_enabled"] = True
                updated["accounts"][self.account]["smtp"] = copy.deepcopy(candidate["smtp"])
                if candidate["sent_mailbox"] is not None:
                    updated["accounts"][self.account]["sent_mailbox"] = candidate["sent_mailbox"]
                self.storage._commit_config(self.config_path, updated, original)
                try:
                    setup._sync_directory(self.config_path.parent)
                except OSError:
                    pass  # Config replace already committed; do not rewrite it.
            self._candidates.clear()
            return {"status": "saved", "account": self.account, "send_enabled": True,
                    "smtp": copy.deepcopy(candidate["smtp"]), "sent_mailbox": candidate["sent_mailbox"],
                    "mailbox_writes": 0, "messages_sent": 0}


class _SendHandler(setup_web._Handler):
    def do_GET(self):
        if not self._host_ok():
            return self._reject()
        if not self.controller.active or time.monotonic() >= self.controller.deadline:
            return self._reject(410, "SESSION_EXPIRED")
        routes = {"/": ("send-setup.html", "text/html; charset=utf-8"),
                  "/send-setup.js": ("send-setup.js", "text/javascript; charset=utf-8"),
                  "/send-setup.css": ("send-setup.css", "text/css; charset=utf-8")}
        if self.path not in routes:
            return self._reject(404, "NOT_FOUND")
        filename, content_type = routes[self.path]
        try:
            content = (setup_web.ASSETS / filename).read_bytes()
        except OSError:
            return self._reject(503, "SETUP_ASSET_UNAVAILABLE")
        self._respond(200, content, content_type)


class _SendController(setup_web._Controller):
    def __init__(self, config_path=None, session_factory=None, ttl_seconds=setup_web.TTL_SECONDS, prefill=None):
        self.setup_id = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.done = threading.Event()
        lifetime = max(1, min(ttl_seconds, setup_web.TTL_SECONDS))
        self.deadline = time.monotonic() + lifetime
        self.expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=lifetime)).isoformat()
        self.session = (session_factory or SendSetupSession)(config_path=config_path, account=prefill["account"])
        self.prefill = {**self.session.context(), **prefill}
        self.agent_host_supplied = True
        self.host_source = "explicit_smtp_confirmation"
        self.require_host_retype = True  # Every SMTP host, including familiar providers.
        self.attempts, self.active, self.result = 0, True, "waiting"
        self.server = setup_web._QuietServer(("127.0.0.1", 0), _SendHandler)
        self.server.controller = self
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.url = self.origin + "/#token=" + self.token
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.worker.start()
        threading.Thread(target=self._expiry, daemon=True).start()


def _launch_send_setup(config_path=None, account=None, host=None, port=None, security=None,
                       sent_copy=None, session_factory=None, ttl_seconds=setup_web.TTL_SECONDS):
    prefill = validate_proposal(account, host, port, security, sent_copy)
    with setup_web._REGISTRY_LOCK:
        setup_web._REGISTRY.difference_update({item for item in setup_web._REGISTRY if not item.active})
        if any(item.prefill.get("account") == account for item in setup_web._REGISTRY):
            raise SendSetupError("SETUP_ALREADY_OPEN")
        if len(setup_web._REGISTRY) >= 3:
            raise SendSetupError("TOO_MANY_SETUP_SESSIONS")
        controller = _SendController(config_path, session_factory, ttl_seconds, prefill)
        setup_web._REGISTRY.add(controller)
        return controller


def start_send_setup(config_path=None, account=None, host=None, port=None, security=None,
                     sent_copy=None, session_factory=None, open_browser=True):
    """Open a local human-only consent form; return no URL, token or secret."""
    controller = _launch_send_setup(config_path, account, host, port, security, sent_copy, session_factory)
    opened = False
    if open_browser:
        try:
            opened = bool(webbrowser.open(controller.url, new=1, autoraise=True))
        except Exception:
            pass
    result = {"status": "browser_opened" if opened else "manual_setup_required", "setup_id": controller.setup_id,
              "account": account, "needs_human": ["smtp_host_confirmation", "credential_reuse_consent", "send_enable_consent"],
              "expires_at": controller.expires_at, "expires_in_seconds": setup_web.TTL_SECONDS,
              "mailbox_writes": 0, "messages_sent": 0}
    if not opened:
        controller.close("browser_unavailable")
        result["next_step"] = "Open this setup on a machine with a local browser. Do not paste credentials into chat."
    return result
