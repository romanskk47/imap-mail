# Draft creation, editing and recovery

Read this for a draft save/edit, attachment change or an unresolved draft write. References below always include the verified `account`, `mailbox`, `uidvalidity` and `uid`.

## Compose or reply once

Run draft readiness for the exact account. `mail_draft_preview` accepts new-message `to`/`subject` or a verified `reply` reference, `body_text`, optional `cc`, `from_address`, `reply_all` and approved local attachment paths. New text excludes the configured signature and source quote; those are added once by the plugin. Reply-All is not the default. Original source attachments and remote HTML resources are not copied automatically.

Request `response_view: "compact"`, inspect the complete review and use its exact ID/hash with `mail_draft_apply`. A draft request permits that one unsent save; wording-only requests do not. The operation appends only to the account's verified Drafts folder and does not mark the source read or answered.

`prepared` means a private preview exists. `verified` means the reviewed message was independently read back from the server. If apply is `uncertain`, call `mail_draft_status` with the same preview ID. A lost APPEND response may mean the draft already exists; the journal binds its Message-ID and blocks blind re-appends. A timeout is not proof of rejection. Do not make another preview to bypass the journal.

## Edit the current stored draft

Read its current version in the selected account's verified Drafts folder. Pass the exact reference and matching top-level `account` to `mail_draft_edit_preview`, with literal changes:

```json
{
  "account": "example-work",
  "draft": {"account": "example-work", "mailbox": "Drafts", "uidvalidity": "42", "uid": "456"},
  "replacements": [{"old_text": "The previous sentence.", "new_text": "The corrected sentence."}],
  "response_view": "compact"
}
```

Up to 20 exact replacements are supported in plain text and ordinary HTML text nodes. An empty `new_text` removes the matched text. Ambiguous matches, unsupported MIME and changes requiring markup alteration stop rather than silently rebuilding the message. This text edit does not change recipients, regenerate the signature or reload attachments from local paths.

Review complete before/after plain and HTML content, account, sender, recipients, subject, signature, thread and file hashes. Use the edit preview's ID/hash with `mail_draft_edit_apply`; `mail_draft_apply` is a different operation.

IMAP cannot replace a stored body in place. The editor keeps a private original MIME backup, appends the reviewed replacement, verifies it, then marks and expunges only the pinned original UID. `UIDPLUS` is required before writing; no global EXPUNGE is used. The replacement gets a new UID and Message-ID. Only `verified` proves the replacement matches and the original is absent.

Avoid simultaneous editing/sending of that draft in another client. Replacement is not atomic across clients; a check cannot eliminate a race after its last observation. A human-edited version must not be overwritten using a stale creation receipt.

### Interrupted edit

For `cleanup_pending`, `uncertain` or `conflict`, inspect `mail_draft_edit_status` on the same operation. Status is read-only and does not retire anything. A temporary second copy can remain after interruption. The same apply may finish pending targeted cleanup after fresh checks, but does not repeat APPEND. A concurrent source change requires inspecting the current state, not starting a fresh replacement or switching mail clients to get around it.

If sending reports `DRAFT_SOURCE_EDIT_ALREADY_ATTEMPTED`, recover the known edit to `verified` first, then use its returned current reference for the requested send. Do not reconstruct another compose message to bypass the unresolved edit.

## Add or repair attachments

Use `mail_draft_attachment_preview` with the current exact draft reference. Optional `attachments` lists up to ten approved local paths; absent intended files are not permission to substitute another document. Paths must be under configured approved attachment roots.

`normalize_attachments: true` (the default) promotes existing document files nested inside HTML-related MIME to ordinary top-level attachments. It preserves authored text, signature/CID images, existing file bytes, recipients and thread. Supported empty Apple attachment placeholders can be removed conservatively. This is a structural repair, not arbitrary attachment removal or a guarantee of client compatibility.

Review before/after content, file names, MIME positions and hashes. A file's bytes existing somewhere inside HTML is not the same as an ordinary attachment. The result is `operation: draft_edit`: apply/recover it through the same `mail_draft_edit_apply` and `mail_draft_edit_status` workflow above, never by creating another draft.

## Private evidence

Preview state and original backups contain mail content and attachment bytes. Protect them as correspondence, not build cache. The plugin has no automatic restore, retention or deletion policy. Opening a draft in a client can rewrite its Message-ID/content; inspect the current draft when an old receipt no longer verifies it.
