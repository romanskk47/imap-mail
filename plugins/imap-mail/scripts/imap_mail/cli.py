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
    result = argparse.ArgumentParser(description="Read IMAP and save verified unsent drafts; never sends")
    result.add_argument("--config", help="Private settings JSON (or MAIL_DRAFTS_CONFIG)")
    result.add_argument("--state-dir", help="Private journal directory (or MAIL_DRAFTS_STATE_DIR)")
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Connect a new account in a private local browser; never sends")
    setup.add_argument("--no-browser", action="store_true", help="Print a private local setup URL for a human terminal; do not capture in AI logs")
    commands.add_parser("accounts")
    check = commands.add_parser("check", help="Read-only live readiness; never appends a test draft")
    check.add_argument("account")
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
    preview = commands.add_parser("draft-preview")
    preview.add_argument("--request", required=True, help="JSON file containing exact preview arguments")
    apply = commands.add_parser("draft-apply")
    apply.add_argument("preview_id")
    apply.add_argument("preview_hash")
    status = commands.add_parser("draft-status")
    status.add_argument("preview_id")
    return result


def request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    values = vars(args).copy()
    command = values.pop("command")
    values.pop("config")
    values.pop("state_dir")
    if command == "draft-preview":
        path = Path(values["request"]).expanduser()
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise InvalidArguments("Preview request must be a JSON file no larger than 2 MiB")
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidArguments("Preview request is not readable UTF-8 JSON") from exc
        return "mail_draft_preview", body
    names = {"accounts": "mail_accounts", "check": "mail_check", "folders": "mail_folders", "search": "mail_search",
             "read": "mail_read", "draft-apply": "mail_draft_apply", "draft-status": "mail_draft_status"}
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
