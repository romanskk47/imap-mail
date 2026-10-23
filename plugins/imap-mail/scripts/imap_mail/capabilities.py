"""Offline inventory of implemented workflows, not a server capability probe."""

from __future__ import annotations

from .core import DraftError, public


MAX_ACCOUNT_ROWS = 10


def _account_view(row):
    """Project only public discovery fields; never return a whole config row."""
    if not isinstance(row, dict) or not isinstance(row.get("account"), str):
        raise DraftError("INVALID_ACCOUNT_INVENTORY")
    identity = {key: row[key] for key in ("email", "host", "entity")
                if isinstance(row.get(key), str)}
    return {
        "account": row["account"],
        "identity": identity,
        "configured": row.get("configured") is True,
        "read_ready": row.get("read_ready") is True,
        "drafts_ready": row.get("drafts_ready") is True,
        "send_ready": row.get("send_ready") is True,
        "drafts_enabled": row.get("drafts_enabled") is True,
        "send_enabled": row.get("send_enabled") is True,
        "flags_enabled": row.get("flags_enabled") is True,
        "signature_configured": row.get("signature_configured") is True,
        "issues": list(row.get("issues", [])),
        "send_issues": list(row.get("send_issues", [])),
        "connection": "not_checked",
    }


@public
def capabilities(service, account=None):
    """Describe this client's implementation and host configuration without login.

    ``accounts`` already owns safe host discovery and local readiness validation.
    Do not re-read credentials or call live checks here. A selected account is
    matched exactly, including case; there is no preferred-account fallback.
    """
    if account is not None and not isinstance(account, str):
        raise DraftError("ACCOUNT_NOT_CONFIGURED")
    discovery = service.accounts()
    rows = discovery.get("accounts")
    if not isinstance(rows, list):
        raise DraftError("INVALID_ACCOUNT_INVENTORY")
    if account is not None:
        rows = [row for row in rows if isinstance(row, dict) and row.get("account") == account]
        if len(rows) != 1:
            raise DraftError("ACCOUNT_NOT_CONFIGURED")
    total = len(rows)
    return {
        "schema_version": 1,
        "connection": "not_checked",
        "server_capabilities": "not_checked",
        "mailbox_writes": 0,
        "smtp_submissions": 0,
        "transports": {
            "implemented": ["local_mcp_stdio", "local_cli"],
            "authenticated_remote_mcp": "not_implemented",
            "cloud_connector": "not_implemented",
            "private_host": "Runs on the configured host with its own secrets and network permissions. An SSH/stdio launcher can be configured separately; this inventory does not verify remote deployment or client connectivity.",
        },
        "implemented": {
            "account_discovery": ["mail_accounts"],
            "local_account_setup": ["mail_setup", "mail_send_setup"],
            "existing_account_smtp_configuration": ["mail_send_configure"],
            "live_readiness_checks": ["mail_check", "mail_send_check"],
            "folder_list": ["mail_folders"],
            "search": ["mail_search"],
            "read_mime_and_attachment_manifest": ["mail_read"],
            "compose_and_reply_draft": ["mail_draft_preview", "mail_draft_apply", "mail_draft_status"],
            "precise_draft_text_edit": ["mail_draft_edit_preview", "mail_draft_edit_apply", "mail_draft_edit_status"],
            "add_or_normalize_draft_attachments": ["mail_draft_attachment_preview", "mail_draft_edit_apply", "mail_draft_edit_status"],
            "smtp_send": ["mail_send_preview", "mail_send_apply", "mail_send_status"],
            "message_flags": ["mail_flags_preview", "mail_flags_apply", "mail_flags_status"],
        },
        "permissions": {
            "message_flags": {"configuration_key": "flags_write", "enabled_field": "flags_enabled",
                              "default": "disabled", "automatically_enabled": False},
        },
        "local_configuration": {
            "meaning": "Readiness below is local configuration only, not live login, server permission, provider compatibility or a successful write.",
            "accounts": [_account_view(row) for row in rows[:MAX_ACCOUNT_ROWS]],
            "accounts_total": total,
            "accounts_truncated": total > MAX_ACCOUNT_ROWS,
            "more": "Use account=<exact ID> to inspect one account." if total > MAX_ACCOUNT_ROWS else None,
        },
        "workflows": {
            "read": ["mail_folders", "mail_search", "mail_read"],
            "draft": ["mail_check", "mail_draft_preview", "mail_draft_apply"],
            "reply": ["mail_read", "mail_draft_preview", "mail_draft_apply"],
            "edit": ["mail_read", "mail_draft_edit_preview", "mail_draft_edit_apply"],
            "attachments": ["mail_read", "mail_draft_attachment_preview", "mail_draft_edit_apply"],
            "send": ["mail_send_check", "mail_send_preview", "mail_send_apply"],
            "flags": ["mail_flags_preview", "mail_flags_apply"],
            "recovery": {
                "draft": "mail_draft_status",
                "edit_or_attachments": "mail_draft_edit_status",
                "send": "mail_send_status",
                "flags": "mail_flags_status",
            },
        },
        "workflow_notes": [
            "Reuse a verified exact account and stable mailbox/UIDVALIDITY/UID references; never fall back to another identity.",
            "Read-only work does not require drafts, a signature or SMTP. Reply uses the same-account reply reference in the preview.",
            "Review immutable preview ID/hash before apply. A draft request does not authorize send; sending requires an explicit human request.",
            "Unavailable local permissions are setup gaps, not missing implementations. Discovery never enables drafts or SMTP.",
            "On an uncertain write, inspect its same preview ID with the recovery tool; do not create a new preview to retry.",
            "mail_read returns file metadata/hashes, not downloaded files or interpreted attachment contents. A verified MIME save does not prove an iPhone editor can load it.",
            "Flag changes use explicit seen/flagged target values and exact message references. The service verifies content and flags; a verified receipt needs no redundant full-message response to the agent.",
        ],
        "not_implemented": [
            "forward", "attachment_download", "move", "archive",
            "trash", "folder_create_rename_delete", "oauth_mail_account_authentication",
        ],
        "unknown_or_missing_operation": {
            "meaning": "An implementation gap is not a forbidden user request.",
            "next_action": "State the exact gap and offer a separately authorized adapter or implementation; do not invent tools, run generic IMAP commands or claim success.",
        },
    }
