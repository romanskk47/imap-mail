# IMAP Mail 0.6.0

A portable plugin for reading IMAP mail, creating or editing **unsent drafts**, or submitting **explicitly requested messages over SMTP**. Editing retires only the exact reviewed original after verifying its replacement; there are no generic delete/move tools, read/answered flag changes to other messages or mail-sorting service. There is no OpenAI API dependency, background job or paid inference call.

The same standard-library Python implementation is available through MCP and a JSON CLI. It runs on a configured Mac or Linux host without Apple Mail. The host still needs its own authorized account profiles, network access and private signatures; installing the plugin does not move credentials between computers.

Discovery covers every existing profile, including accounts missing draft or sending setup. Installing/upgrading does not enable SMTP, grant permission for a particular message, or guess a signature. Readiness gaps stay visible per account.

## Package and installation

Requirements: Python 3.10+ with its standard library and TLS access to the chosen IMAP server. No `pip install` is required.

The root `plugin.json` and `mcp.json` use the portable plugin format. The public repository supplies the `imap-mail-plugins` marketplace for Codex and Claude Code. The `.claude-plugin/plugin.json` and `.mcp.json` provide Claude Code compatibility. No hooks or automation are installed. Depending on the host, a refreshed plugin or new chat may be needed to load changed tools; no app restart is performed by this package.

For a fresh installation from the public repository:

```sh
codex plugin marketplace add romanskk47/imap-mail
codex plugin add imap-mail@imap-mail-plugins
```

Or in Claude Code:

```sh
claude plugin marketplace add romanskk47/imap-mail
claude plugin install imap-mail@imap-mail-plugins --scope user
```

An agent should perform the installation and readiness checks, not ask the person to edit JSON or repeat commands. Inspect existing installations first and reuse the current source rather than install duplicate copies. The original author's private `shared-agents` marketplace is not needed and is not part of this distribution. Restart/reload the local client to expose newly installed tools; don't claim this also installs a web/mobile connector.

The stdio server is `python3 scripts/imap_mail_mcp.py`. Codex expands `${PLUGIN_ROOT}` in the root MCP manifest; Claude Code expands `${CLAUDE_PLUGIN_ROOT}` in its compatibility manifest. Keep machine-specific paths and credentials out of these files.

## Private configuration

### Agent-led setup: no JSON editing

After installing, tell the agent which mailbox to use. The agent inventories existing accounts, checks the exact selected account and reuses working access. You do not need to learn the tools, prepare JSON or manage account IDs. The packaged Setup skill tells the agent to resolve technical details and ask only for genuinely missing identity/provider/signature choices.

For a new account the agent calls `mail_setup` with known **nonsecret** email/account/entity/host metadata. It opens a pre-filled private, temporary form in the default browser on the host running the plugin. The browser is a credential-entry and consent surface, not the primary product interface. No password, setup token or private session URL is accepted through MCP or returned to the model. The agent resumes discovery and readiness checks after private entry.

1. Review the account and server prepared by the agent. A recognized provider can suggest a server; otherwise use settings verified from provider documentation. Technical labels remain editable in advanced options. Confirm the exact hostname before any credentials are transmitted.
2. Enter an app password locally. Choose a plain-text signature (with a live preview), or explicitly choose no signature. Attachments are disabled unless you approve a specific local directory.
3. Check access. This logs in over verified TLS and reads/examines the Drafts folder; it writes no message. Review the exact account, server and signature, then explicitly consent to enable drafts and save credentials locally.

The wizard only adds new accounts; it does not edit or delete existing profiles, signatures or draft journals. It closes after saving or expiry. A failed check is not a failed draft, and saving configuration is not proof that a draft has been appended.

Credential storage is deliberately simple and portable: a separate **plaintext `password_file`, mode 0600**, in a private directory. This is not encrypted Keychain storage; other software running as your OS user can read it. Use a revocable app password where the provider supports one and protect the host and its backups. No credentials belong in the package or chat. This version has no OAuth/XOAUTH2: accounts requiring OAuth cannot be connected by the wizard. Provider suggestions do not guarantee that an administrator permits password-based IMAP.

The human-run CLI alternative is:

```sh
python3 scripts/imap_mail.py setup
```

Keep that process running until setup finishes. `setup --no-browser` is a human-terminal fallback that displays a short-lived private local URL; do not run it in model-visible captured output. It does not create a public website. For a headless host, provision private profiles there through its own authorized administration; installing on a Mac does not provision a VPS.

### Existing profiles and advanced signatures

Configuration: `MAIL_DRAFTS_CONFIG`, or `~/.config/imap-mail/settings.json`. State: `MAIL_DRAFTS_STATE_DIR`, the configured `state_dir`, or `~/.local/state/imap-mail`. CLI `--config` and `--state-dir` override those defaults. Settings and signatures belong outside Git; keep directories private and credential/config files mode `0600`.

Example settings, using placeholders rather than any real account:

```json
{
  "version": 1,
  "preferred_transport": "imap",
  "profiles_dir": "/private/credentials/mail",
  "state_dir": "/private/state/imap-mail",
  "accounts": {
    "example-work": {
      "identity": {
        "email": "me@example.invalid",
        "host": "imap.example.invalid",
        "entity": "example-company"
      },
      "from_addresses": ["me@example.invalid"],
      "default_from": "me@example.invalid",
      "drafts_enabled": true,
      "signature": {
        "plain": "/private/signatures/example-work.txt",
        "html": "/private/signatures/example-work.html",
        "inline": {
          "signature-logo": "/private/signatures/logo.png"
        }
      },
      "attachment_roots": ["/private/approved-attachments"]
    }
  }
}
```

Use `{"none": true}` instead of the `signature` file object only when an intentional signature-free draft has been approved. A signature's HTML can refer to its inline asset as `src="cid:signature-logo"`. Supply approved local HTML/plain content; this plugin does not extract desktop signatures or guess a company footer. It inserts the configured signature once, above quoted reply text. Do not put another copy in `body_text`.

Each account key must match a host-owned profile ID. Profile `user`, `host` and `entity` must match the pinned identity. Only explicitly allowed From addresses are accepted. To enable another identity or company, configure it independently rather than changing the meaning of an existing account ID.

The optional top-level `preferred_transport: "imap"` makes this plugin the normal path for mail reads, searches and unsent drafts; a fresh explicit user choice of Apple Mail or another client still takes precedence. This preference is returned by account discovery and belongs in private owner configuration, not in company-specific skill instructions.

The bundled reader understands the same JSON profiles as `mail-access`: required `id`, `host`, `user`, `entity`, TLS enabled, and exactly one of `password`, `password_env`, `password_file`. Literal-password files must be private. The default profile directory is `~/.secrets/services/mail`; `MAIL_PROFILES_DIR` or `MAIL_PROFILES_JSON` can supply host-owned profiles. Credentials are never included in the plugin, previews or tool output. This direct adapter does not implement OAuth or bypass an account's read-only broker. Broker-only access needs an authorized server-side draft adapter before writing.

The server's special-use `\Drafts` folder is preferred. Configure `drafts_mailbox` explicitly only when no special-use Drafts folder exists and the exact destination has been verified. Multiple special-use Drafts folders are rejected; resolve that ambiguity in the server configuration before writing. The adapter never creates arbitrary mailboxes. Draft creation appends only to that selected destination; an edit replaces only a verified existing draft there.

## Workflow and tools

| Tool | What it does |
| --- | --- |
| `mail_setup` | Let the agent prepare nonsecret setup details and open local private entry/consent; credentials and session URL are never tool parameters/results |
| `mail_accounts` | Discover all host profiles and configured accounts, with per-account setup gaps; no login or enabling writes |
| `mail_check` | Read-only live readiness check for one exact account; never creates a test draft |
| `mail_folders` | Read folder names and special-use flags |
| `mail_search` | Search read-only; return stable mailbox/UIDVALIDITY/UID references |
| `mail_read` | Read a source with PEEK and validate its reference |
| `mail_draft_preview` | Freeze a private MIME preview with signature, attachments and reply headers |
| `mail_draft_apply` | Save exactly that reviewed preview and independently verify it |
| `mail_draft_status` | Read back the same operation, including uncertain-write recovery |
| `mail_draft_edit_preview` | Freeze one existing draft and exact replacements; review full before/after content without mailbox writes |
| `mail_draft_attachment_preview` | Preview specified local file additions and ordinary-attachment repair for existing draft documents; no mailbox writes |
| `mail_draft_edit_apply` | Verify a replacement before retiring only the pinned original; never sends |
| `mail_draft_edit_status` | Inspect the same edit without mailbox writes; does not perform pending cleanup |
| `mail_send_setup` | Open private SMTP permission setup for one existing account; no test send |
| `mail_send_check` | Check confirmed SMTP/TLS/login and Sent destination without MAIL/RCPT/DATA |
| `mail_send_preview` | Freeze a new message/reply or exact existing draft for a requested send |
| `mail_send_apply` | Submit only that reviewed send preview under a specific human instruction |
| `mail_send_status` | Inspect that same submission and Sent copy; never resubmit SMTP |

A reply source is `{"account":"example-work","mailbox":"INBOX","uidvalidity":"42","uid":"123"}`. Copy all four fields exactly from `mail_read` and use the same account at the top level of the preview request. The nested account is required by the 0.2 MCP/CLI schema and must match; a UID/mailbox pair alone is not a cross-account identity. Never reconstruct an account from a company name or substitute another account after a setup or connection failure. Source text is safely quoted; `In-Reply-To` and `References` preserve the reply relationship. Original attachments and remote HTML resources are not copied automatically. A client may group/display server drafts differently; verification proves server storage, not every client's presentation.

### Configuration, readiness and saved drafts are different states

| Evidence | What is known | What is not yet proven |
| --- | --- | --- |
| `mail_accounts` | All discovered account IDs, identity/configuration gaps and local `read_ready`/`drafts_ready` validation, including configured signature files | `connection: not_checked`: current login and the server's Drafts destination have not been checked |
| `mail_check(account)` with `status: ready` | Pinned identity, From allowlist, signature files, live login and Drafts folder checked for that account; `mailbox_writes: 0` | No APPEND has been tested and no server draft exists from this check |
| `mail_draft_apply` with `status: verified` | The reviewed draft was saved and independently read back on the server | Every mail client's visual presentation, grouping and future human edits |

If `mail_check` returns `needs_attention`, inspect its sanitized `error_code` and `issues`; do not claim the account is ready or redirect the message through a different identity. A diagnostic response can successfully return a gap, so inspect the JSON `status`, not just the CLI exit code. Profiles missing private draft settings remain visible in discovery and are not automatically granted From aliases, signatures or write access.

These results apply to the host where they were obtained. Installing or checking the Mac does not deploy the package, credentials or signatures to a VPS, CI worker or another computer. Each remote host needs its own authorized configuration and checks.

Preview creation does not write a server draft. Review all returned fields, then apply its exact ID and hash only within a user-authorized draft request. A wording-only request stays a preview. New drafts use `\Draft`; creation does not change the reply source's flags.

`prepared` means a local preview exists. Only `verified` means an independently read-back server draft matches the frozen content. An `uncertain` write must be checked with `mail_draft_status`: a lost APPEND response might mean the server already accepted it. The journal binds the original Message-ID and blocks blind re-appends. Do not make a new preview to work around uncertainty. A changed draft is not overwritten using a stale creation receipt; use the explicit editing workflow below for a requested change to its current version.

APPEND socket waits are bounded at 60 seconds to allow attachment uploads. A slow
or interrupted upload can still remain uncertain; a timeout is never proof that
the server rejected the draft. Opening a draft in a desktop client may itself
rewrite its Message-ID or content; the old receipt then deliberately stops
verifying. These cases require checking the existing draft, not replaying it.

Preview state contains email bodies and attachment bytes. Protect it as private mail data, not an ordinary build cache. The plugin has no automatic retention/deletion policy; the owner is responsible for retaining or removing local evidence under their own policy. It does not alter another mail service's fences, leases, schedules or credentials.

## Editing an existing unsent draft

Use this workflow when the person asks to change an existing draft, not to send it. Select the current message in the verified Drafts folder and pass its exact account, mailbox, UIDVALIDITY and UID to `mail_draft_edit_preview`, with the same top-level account and a nonempty `replacements` list. Each item has literal `old_text` and `new_text`; an empty new text deletes just the matched text. Up to 20 replacements are allowed, subject to the request-size limit. Matching is conservative: ordinary plain text and HTML text nodes are supported, while ambiguous matches, unsupported MIME or changes requiring alteration of markup are rejected.

The preview stores an immutable private original MIME backup and proposed replacement, but writes no mailbox message. Review the complete before/after bodies, account, recipients, subject, signature, thread headers and attachment hashes. An edit does not add the configured signature again, rebuild attachments from local paths or accept replacement recipients. Use its exact ID/hash with `mail_draft_edit_apply` only after that review and under the person's edit instruction.

IMAP cannot change a stored message body in place. This workflow appends the reviewed replacement, independently verifies it, then marks and expunges **only the pinned original UID**. It requires `UIDPLUS`; unsupported servers stop before the first mailbox write. No global EXPUNGE is used. These steps are not atomic across other mail clients: avoid editing or sending the same draft in another client while the operation runs. The edited draft therefore has a new UID and Message-ID. After a verified edit, use the returned current reference for future edits or an explicitly requested send, not the retired reference.

Only `status: verified` means the replacement matches and the original is absent. `cleanup_pending` means the operation is not finished; `uncertain` does not prove rejection, and `conflict` requires inspecting the current drafts. A temporary second copy can remain after interruption. Check `mail_draft_edit_status` for the **same** preview ID; it only reads remote state and never retires anything. Repeating the same `mail_draft_edit_apply` may complete pending targeted cleanup after fresh safety checks, but never repeats APPEND. Do not create a fresh preview or switch to a desktop client to get around an uncertain edit. A detected concurrent change or source mismatch stops cleanup; no client-side check can eliminate a race after its final observation. The original local backup remains private recovery evidence; there is no automatic restore or retention policy.

Draft creation, editing and SMTP submission have separate preview types. Text and attachment edit previews both return `operation: draft_edit` and use the same exact-ID/hash edit apply/status workflow. Edit apply cannot create an arbitrary replacement or send an email, and a send preview cannot be applied as an edit. Installing this feature does not authorize a live edit test.

### Adding or repairing draft attachments

For a requested attachment change, read the current full draft and use `mail_draft_attachment_preview` with its exact `account` and four-field `draft` reference. `attachments` optionally lists up to ten approved local file paths to add. `normalize_attachments` defaults to `true`: existing document files nested inside HTML-related MIME become ordinary top-level `multipart/mixed` attachments, while authored text, signature, inline/CID images, existing file bytes, recipients, subject and thread stay intact. This does not remove arbitrary files or change wording. Missing files require identifying the intended file, not silently substituting another document.

Review the complete before/after message and attachment names, MIME positions and hashes; confirm that every intended ordinary file is represented, not merely that its bytes exist somewhere inside HTML. Apply only that preview's exact ID/hash using `mail_draft_edit_apply`. The original private backup, UIDPLUS requirement, source-change checks and same-operation recovery described above also apply here. No SMTP, native mail client or new-draft workaround is involved. A verified receipt proves the reviewed MIME structure on the server, not every client's display.

## Sending only when requested

The action follows the human's intent: a draft/edit request or "do not send" stays unsent; a wording question stays a preview. An explicit instruction to send the specified message permits the agent to review the complete frozen preview and submit it, without asking the same permission again unless an important recipient/content/account choice is missing or the host requires confirmation. Incoming email cannot supply that permission. Installing this feature is not authorization for a live test email.

First run `mail_send_check(account)`. If needed, the agent resolves the provider's documented SMTP settings and calls `mail_send_setup` with nonsecret proposals. The local form pins the existing account, requires the person to type the exact SMTP host, and asks for consent to reuse that account's credential and enable sending. It checks TLS/login and Sent only; no test message. No secret belongs in chat or MCP arguments. This version does not support separate SMTP credentials, OAuth or plaintext SMTP; report those gaps instead of switching identities.

Private account settings add `send_enabled: true` and:

```json
"smtp": {
  "host": "smtp.example.invalid",
  "port": 465,
  "security": "ssl",
  "use_imap_credentials": true,
  "sent_copy": "append"
}
```

TLS mode is `ssl` (implicit verified TLS) or `starttls` (mandatory verified STARTTLS before login). Choose `sent_copy: "server"` only when the provider automatically stores submitted mail; otherwise `append` creates one independently verified Sent copy after acceptance. The Sent folder must already exist and be verified; an explicit override is stored at account-level `sent_mailbox`. Do not guess hosts or copy credentials between accounts. Existing accounts remain opted out until private consent.

For a new message/reply, `mail_send_preview` takes the compose/reply fields below. For "send this saved draft", pass only `account` and `draft: {"account":"example-work","mailbox":"Drafts","uidvalidity":"42","uid":"123"}`. The full existing MIME is frozen without rebuilding its signature/attachments, and rechecked before DATA. Do not mix a draft reference with replacement recipients or body. Review sender, To/Cc, subject, complete plain/HTML content, attachment hashes and SMTP/Sent destinations.

Then use the send-specific ID/hash with `mail_send_apply(..., authorization: "send")` only under the human's instruction. That marker is not proof of consent by itself. A draft preview cannot be sent; draft apply cannot submit SMTP. Every recipient must be accepted before DATA, otherwise the submission is aborted without DATA. This initial sending implementation supports ASCII envelope addresses, To/Cc, and rejects unsupported Bcc/Resent headers on existing drafts.

The durable journal records intent before DATA. Standard SMTP has no general idempotency key: Message-ID alone cannot prevent duplicates. A lost DATA response is **uncertain** and is never blindly retried. Use `mail_send_status` for the same operation, never a new preview/resubmission to work around uncertainty. Existing drafts are not deleted or marked answered; do not send them again from another client.

**SMTP acceptance, Sent-copy verification and recipient delivery are separate facts.** A missing Sent copy after accepted SMTP is not a failed send. A matching Sent copy alone cannot turn uncertain SMTP into proven acceptance/delivery. Recipient-side delivery/bounce handling, background queues, automatic delivery retry and bulk marketing are not implemented. Full-body IMAP responses and an explicit EOF probe handle providers with stale stored sizes; this relies on the server's full-body protocol response, not a claim to detect arbitrary server corruption.

## JSON CLI

Run from this plugin directory, replacing the example account and reference with verified values:

```sh
python3 scripts/imap_mail.py accounts
python3 scripts/imap_mail.py check example-work
python3 scripts/imap_mail.py folders example-work
python3 scripts/imap_mail.py search example-work INBOX --query "Invoice" --limit 10
python3 scripts/imap_mail.py read example-work INBOX 42 123
python3 scripts/imap_mail.py draft-preview --request /private/request.json
python3 scripts/imap_mail.py draft-apply PREVIEW_ID PREVIEW_HASH
python3 scripts/imap_mail.py draft-status PREVIEW_ID
python3 scripts/imap_mail.py draft-edit-preview --input /private/edit-request.json
python3 scripts/imap_mail.py draft-attachment-preview --input /private/attachment-request.json
python3 scripts/imap_mail.py draft-edit-apply EDIT_PREVIEW_ID EDIT_PREVIEW_HASH
python3 scripts/imap_mail.py draft-edit-status EDIT_PREVIEW_ID
python3 scripts/imap_mail.py send-check example-work
python3 scripts/imap_mail.py send-preview --request /private/send-request.json
python3 scripts/imap_mail.py send-apply SEND_PREVIEW_ID SEND_PREVIEW_HASH --authorization send
python3 scripts/imap_mail.py send-status SEND_PREVIEW_ID
```

`/private/request.json` has exactly the same fields as `mail_draft_preview`, for example:

```json
{
  "account": "example-work",
  "body_text": "Thank you. Please find the requested report attached.",
  "reply": {"account": "example-work", "mailbox": "INBOX", "uidvalidity": "42", "uid": "123"},
  "attachments": ["/private/approved-attachments/report.pdf"]
}
```

A new message supplies explicit `to` (an array) and `subject` instead of `reply`. Optional fields are `from_address`, `cc`, and `reply_all`; Reply-All is not the default. CLI output is JSON; errors return exit 1 with a sanitized code. Send commands are separate from draft commands and require opt-in SMTP plus the specific human instruction; a CLI marker alone does not establish consent. SMTP permission setup is provided through the persistent MCP server's private browser wizard.

`/private/edit-request.json` uses only the exact existing draft reference and requested text changes:

```json
{
  "account": "example-work",
  "draft": {"account": "example-work", "mailbox": "Drafts", "uidvalidity": "42", "uid": "456"},
  "replacements": [{"old_text": "The previous sentence.", "new_text": "The corrected sentence."}]
}
```

All preview commands accept `--input` and its backward-compatible alias `--request`. The JSON file is private email data, not a repository example to populate with real correspondence.

`/private/attachment-request.json` selects the same current draft and only the requested attachment changes. Omit `attachments` to normalize existing files without adding any:

```json
{
  "account": "example-work",
  "draft": {"account": "example-work", "mailbox": "Drafts", "uidvalidity": "42", "uid": "456"},
  "attachments": ["/private/approved-attachments/report.pdf"],
  "normalize_attachments": true
}
```

## Verification

Offline tests do not load real credentials or contact mail servers:

```sh
python3 -m unittest discover -s tests -v
```

For ordinary readiness, run `mail_check`; it never appends a test draft. When the user has authorized an end-to-end setup test for a new account/client combination, prepare one harmless unsent draft addressed to yourself, review it, save it once, verify server readback, and inspect it in the intended mail client. Check the signature, attachment names, quote, recipients, editability and reply grouping. Do not send the test. Run a second apply with the same ID/hash only to verify the journal's duplicate protection, not to create another draft. The test writes one draft and requires authorization for that exact account; do not run it merely because discovery found a profile.

## Design references

We inspected [ni-c/imap-mcp](https://github.com/ni-c/imap-mcp) and [Tiangong's email-imap-append skill](https://github.com/tiangong-ai/agent-skills/tree/main/email-imap-append). They demonstrate useful existing approaches, but this package does not reuse their code. It extends the existing standard-library reader with a narrow, separately gated draft operation and durable uncertain-write handling.

Primary protocol references: [IMAP APPEND](https://www.rfc-editor.org/rfc/rfc9051.html#section-6.3.12), [UIDPLUS targeted expunge](https://www.rfc-editor.org/rfc/rfc4315.html#section-2.1), [special-use Drafts folders](https://www.rfc-editor.org/rfc/rfc6154.html), and [reply identification fields](https://www.rfc-editor.org/rfc/rfc5322.html#section-3.6.4). Portable stdio packaging follows [OpenAI's desktop-only plugin documentation](https://developers.openai.com/plugins/deploy/desktop-only#configure-a-local-mcp-server).

## Distribution boundary

This is a local stdio package for supported Codex and Claude Code installations on Mac/Linux. It does not install a connector in ordinary ChatGPT or Claude web/mobile chats, host a multiuser service, or deploy alongside Mailroom. Those need a separately secured remote integration. Windows is not supported by the POSIX journal locking implementation.

The package contains generic code and example identities only, not the author's account configuration or secrets. The source and tagged releases are distributed from [romanskk47/imap-mail](https://github.com/romanskk47/imap-mail) under the [MIT License](LICENSE). GitHub publication is not approval or listing in an official ChatGPT/Claude plugin directory. Provider OAuth registration, web/mobile connectors and any official-directory submission remain separate work.

### Version 0.6.0

Adds reviewed attachment repair for an existing unsent draft. Approved local files can be added and existing document parts promoted to ordinary top-level attachments without rewriting authored text, recipients or signatures. Empty supported Apple attachment placeholders can be removed conservatively. A complete before/after preview, private original backup and the existing one-APPEND/targeted-retirement journal remain mandatory. No sending or general mailbox deletion is introduced.

### Version 0.5.1

Fixes ordinary message reads that previously fetched only the first 2 MiB and could prefer an empty plain alternative over real HTML. The plugin now reads verified full MIME up to 16 MiB, reports complete attachment metadata with byte counts and SHA256, and separates MIME completeness from text-view truncation. Reading does not alter flags, drafts or SMTP settings. The skill distinguishes actual missing implementations from permission to perform a requested mail task.

The text view is capped at 60,000 characters independently of the full-MIME check. Attached messages (`.eml`/`message/rfc822`) and multipart attachment files are currently reported as unsupported rather than omitted from a supposedly complete manifest. `attachments_read: false` means document contents were not interpreted. The full-reader response does not guess an unavailable IMAP internal date.

### Version 0.5.0

Adds reviewed text edits to existing unsent drafts without a desktop mail client. The editor preserves recipients, thread, signature and attachments, retains a private original MIME backup, verifies the replacement, then retires only the exact original UID using UIDPLUS. Status is remotely read-only; interrupted edits reuse the same journal and never replay APPEND. There are no general deletion tools or implicit sends.

### Version 0.4.0

Adds opt-in SMTP submission, send-specific immutable previews, existing-draft sending, no-message readiness and private SMTP permission setup. Draft/wording requests remain unsent. Accepted SMTP, uncertain submission, Sent-copy storage and unproven delivery are reported separately. Existing account permissions are not silently expanded.

### Version 0.3.1

Readback verification recognizes equivalent HTML character references in ordinary text, such as a literal apostrophe versus `&#x27;`, without treating escaped HTML tags, changed links, changed script/style content or attachment bytes as equivalent. This avoids false content-mismatch reports from providers that re-encode HTML text. It does not loosen the one-APPEND journal, human-edit protection or uncertain-write retry fence.
