"""JSON CLI for the same bounded operations exposed by MCP."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any

from .mcp import InvalidArguments, dispatch_tool, error_payload, make_service


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Read IMAP, save or edit unsent drafts, or submit explicitly authorized SMTP messages")
    result.add_argument("--config", help="Private settings JSON (or MAIL_DRAFTS_CONFIG)")
    result.add_argument("--state-dir", help="Private journal directory (or MAIL_DRAFTS_STATE_DIR)")
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Connect a new account in a private local browser; never sends")
    setup.add_argument("--no-browser", action="store_true", help="Print a private local setup URL for a human terminal; do not capture in AI logs")
    commands.add_parser("accounts")
    capabilities = commands.add_parser("capabilities", help="Offline capabilities and local account readiness")
    capabilities.add_argument("--account")
    check = commands.add_parser("check", help="Read-only live readiness; never appends a test draft")
    check.add_argument("account")
    check.add_argument("--operation", choices=["read", "draft"])
    folders = commands.add_parser("folders")
    folders.add_argument("account")
    search = commands.add_parser("search")
    search.add_argument("account")
    search.add_argument("mailbox")
    search.add_argument("--query", default="")
    search.add_argument("--field", choices=["SUBJECT", "FROM", "TO", "TEXT"], default="SUBJECT")
    search.add_argument("--since")
    search.add_argument("--before")
    search.add_argument("--before-uid", type=int)
    search.add_argument("--limit", type=int, default=20)
    read = commands.add_parser("read")
    for field in ("account", "mailbox", "uidvalidity", "uid"):
        read.add_argument(field)
    download = commands.add_parser("attachment-save", help="Save one hash-pinned attachment under private plugin state; no mailbox writes")
    download.add_argument("--input", "--request", dest="request", required=True,
                          help="JSON with account, message identity/content_sha256, part_index and expected_sha256")
    preview = commands.add_parser("draft-preview")
    preview.add_argument("--request", "--input", dest="request", required=True, help="JSON file containing exact preview arguments")
    preview.add_argument("--response-view", choices=["full", "compact"])
    apply = commands.add_parser("draft-apply")
    apply.add_argument("preview_id")
    apply.add_argument("preview_hash")
    status = commands.add_parser("draft-status")
    status.add_argument("preview_id")
    preview_read = commands.add_parser("preview-read", help="Read the same frozen preview without preparing another")
    preview_read.add_argument("preview_id")
    preview_read.add_argument("--response-view", choices=["full", "compact"])
    edit_preview = commands.add_parser("draft-edit-preview", help="Freeze exact replacements to one existing unsent draft; no mailbox writes")
    edit_preview.add_argument("--input", "--request", dest="request", required=True,
                              help="JSON file with account, exact draft reference and text replacements")
    edit_preview.add_argument("--response-view", choices=["full", "compact"])
    attachment_preview = commands.add_parser("draft-attachment-preview", help="Preview requested attachment additions or MIME repair to one existing unsent draft")
    attachment_preview.add_argument("--input", "--request", dest="request", required=True,
                                    help="JSON file with account, exact draft reference, optional attachment paths and normalization choice")
    attachment_preview.add_argument("--response-view", choices=["full", "compact"])
    edit_apply = commands.add_parser("draft-edit-apply", help="Replace the reviewed draft; retire only its pinned original after verification, never send")
    edit_apply.add_argument("preview_id")
    edit_apply.add_argument("preview_hash")
    edit_status = commands.add_parser("draft-edit-status", help="Inspect the same edit without mailbox writes or sending")
    edit_status.add_argument("preview_id")
    send_check = commands.add_parser("send-check", help="Check separately enabled SMTP without sending a message")
    send_check.add_argument("account")
    configure = commands.add_parser("send-configure", help="Configure an authorized existing account; checks SMTP without sending or opening a browser")
    configure.add_argument("account")
    configure.add_argument("--host", required=True)
    configure.add_argument("--port", type=int, required=True)
    configure.add_argument("--security", choices=["ssl", "starttls"], required=True)
    configure.add_argument("--sent-copy", choices=["server", "append"], required=True)
    configure.add_argument("--authorization", choices=["enable_sending"], required=True)
    send_preview = commands.add_parser("send-preview", help="Freeze content for an explicit send request; does not send")
    send_preview.add_argument("--request", "--input", dest="request", required=True)
    send_preview.add_argument("--response-view", choices=["full", "compact"])
    send_apply = commands.add_parser("send-apply", help="Submit the exact reviewed message only with human authorization")
    send_apply.add_argument("preview_id")
    send_apply.add_argument("preview_hash")
    send_apply.add_argument("--authorization", choices=["send"], required=True)
    send_status = commands.add_parser("send-status", help="Inspect submission status without SMTP resubmission")
    send_status.add_argument("preview_id")
    flags_preview = commands.add_parser("flags-preview", help="Preview exact read/unread or star targets")
    flags_preview.add_argument("--input", "--request", dest="request", required=True)
    flags_apply = commands.add_parser("flags-apply", help="Apply the same reviewed flag operation")
    flags_apply.add_argument("preview_id")
    flags_apply.add_argument("preview_hash")
    flags_status = commands.add_parser("flags-status", help="Inspect flag outcomes without writes")
    flags_status.add_argument("preview_id")
    return result


def request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    values = vars(args).copy()
    command = values.pop("command")
    values.pop("config")
    values.pop("state_dir")
    if command in ("attachment-save", "draft-preview", "draft-edit-preview", "draft-attachment-preview", "send-preview", "flags-preview"):
        path = Path(values["request"]).expanduser()
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise InvalidArguments("Request must be a JSON file no larger than 2 MiB")
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidArguments("Request is not readable UTF-8 JSON") from exc
        response_view = values.get("response_view")
        if response_view is not None:
            if not isinstance(body, dict) or body.get("response_view", response_view) != response_view:
                raise InvalidArguments("Conflicting preview response views")
            body["response_view"] = response_view
        return {"attachment-save": "mail_attachment_save", "draft-preview": "mail_draft_preview", "draft-edit-preview": "mail_draft_edit_preview",
                "draft-attachment-preview": "mail_draft_attachment_preview",
                "send-preview": "mail_send_preview", "flags-preview": "mail_flags_preview"}[command], body
    names = {"accounts": "mail_accounts", "capabilities": "mail_capabilities", "check": "mail_check", "folders": "mail_folders", "search": "mail_search",
             "read": "mail_read", "draft-apply": "mail_draft_apply", "draft-status": "mail_draft_status",
             "preview-read": "mail_preview_read",
             "draft-edit-apply": "mail_draft_edit_apply", "draft-edit-status": "mail_draft_edit_status",
             "send-configure": "mail_send_configure", "send-check": "mail_send_check", "send-apply": "mail_send_apply", "send-status": "mail_send_status",
             "flags-apply": "mail_flags_apply", "flags-status": "mail_flags_status"}
    return names[command], {key: value for key, value in values.items() if value is not None}


def main(argv: list[str] | None = None, *, service: Any = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "setup":
            from .setup_web import serve_setup
            return serve_setup(config_path=args.config, open_browser=not args.no_browser)
        name, values = request(args)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            current = service if service is not None else make_service(args.config, args.state_dir)
            result = dispatch_tool(name, values, service=current)
    except Exception as exc:
        print(json.dumps({"error": error_payload(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
