# Flags and provider/client diagnostics

## Read/unread and star status

`mail_flags_preview` prepares only `\Seen` and `\Flagged` changes on up to 100 selected, verified messages; it writes nothing to the mailbox. Use matching account references copied from reads/searches and at least one explicit boolean:

```json
{
  "account": "example-work",
  "messages": [{"account": "example-work", "mailbox": "INBOX", "uidvalidity": "42", "uid": "123"}],
  "seen": false,
  "flagged": true
}
```

The account must have `flags_write: true` in its private settings. Discovery, reading and installing this feature do not enable it. If the person requested the flag action but the setting is disabled, explain that concrete configuration gap and resolve only the authorized account permission; do not substitute a draft permission or silently enable every account.

Review exact targets, observed target booleans and desired values, then apply its exact ID/hash with `mail_flags_apply`. Only `verified` confirms every requested target. The service preserves all other flags and verifies content identity. Its concurrency protection is preflight-only, not an atomic compare-and-swap or batch: another client can still race a check, and an interrupted batch can be partial.

Inspect `mail_flags_status` for the same operation after interruption or uncertainty. Once STORE was attempted, the operation is observed/reconciled, never resumed or replayed; a later human flag change must not be undone as an automatic retry. Do not create a new preview to bypass an unresolved operation. Flag journals use their own status tool, not `mail_preview_read`.

This path does not support `\Deleted`, arbitrary keywords, moving messages or folder changes. Those are implementation gaps, not restrictions on what the person may request.

## Know which layer failed

Account inventory is local configuration; `mail_check(operation: "read")` tests current read access, while `operation: "draft"` also validates draft identity/signature/destination. Use the check relevant to the task. A missing draft signature does not make a mailbox unreadable. Provider or administrator password policies can still block login even when an account is configured.

`mail_read` uses PEEK and validates UIDVALIDITY. Full MIME is verified up to 16 MiB; its text view is separately capped at 60,000 characters. Check `mime_complete`, `attachment_manifest_complete` and text truncation independently. `attachments_read: false` means attachment documents have not been interpreted. Attached messages (`message/rfc822`/`.eml`) and multipart attachment files are reported as unsupported, not silently omitted from a complete manifest.

Readback must not assume a provider's stored RFC822.SIZE is exact. The reader's full-body response and EOF probe address stale size metadata, but do not prove arbitrary server data is correct. Report reproducible protocol evidence rather than attributing every failed attachment to MIME formatting.

## iPhone Mail composer status

A reported case opens the files in webmail and in the iPhone's Sent message, yet iPhone Mail cannot import them into a draft/forward composer. Full-message and individual-part hashes can be correct while that composer still fails. This distinguishes file availability from the client's attachment-transfer path; it does **not** establish the exact iOS/provider cause.

Ordinary top-level attachments, a clean recreated draft, and server hash checks have not demonstrated a cure for that case. Do not repeat normalization as a confirmed fix or claim the plugin is client-compatible because offline tests pass. Do not advise saving/sending a partial message after “create anyway”.

Diagnose with scoped comparisons: exact source message, opening the file versus adding it to a composer, manual insertion of a saved test file, provider/client versions, and protocol logs when available. Preserve working account settings and correspondence. A destructive account reset, public issue containing private mail, another live test message or a provider configuration change requires its own appropriate scope; none follows merely from finding a client bug.
