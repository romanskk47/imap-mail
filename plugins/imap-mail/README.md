# IMAP Mail

A portable email toolset for AI agents: IMAP search/read, draft creation/editing, selected read/star changes and explicitly requested SMTP submission. The person chooses the action; capability discovery makes implemented functions and remaining gaps visible. There is no OpenAI API dependency, background job or paid inference call.

The same standard-library Python implementation is available through stdio MCP and a JSON CLI. It runs on a configured Mac or Linux host without Apple Mail. The host still needs its own authorized account profiles, network access, private signatures and operation state; installing the plugin does not move credentials between computers.

**Source status:** this branch prepares **0.7.0**; it is not yet a released or live-provider-validated deployment. Capability discovery, read-only operation checks, reviewed `\Seen`/`\Flagged` changes and compact preview views are additions after the published 0.6.0 release. Discovery covers every existing profile, including accounts missing draft, flag or SMTP permission. Installation never enables those permissions or authorizes a particular operation.

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
      "flags_write": false,
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

The optional top-level `preferred_transport: "imap"` makes this plugin the normal path for supported mail tasks; a fresh explicit user choice of another client still takes precedence. This preference is returned by account discovery and belongs in private owner configuration, not in company-specific skill instructions. Account `flags_write: true` separately permits the supported read/star changes; it is not implied by `drafts_enabled` or `send_enabled`.

The bundled reader understands the same JSON profiles as `mail-access`: required `id`, `host`, `user`, `entity`, TLS enabled, and exactly one of `password`, `password_env`, `password_file`. Literal-password files must be private. The default profile directory is `~/.secrets/services/mail`; `MAIL_PROFILES_DIR` or `MAIL_PROFILES_JSON` can supply host-owned profiles. Credentials are never included in the plugin, previews or tool output. This direct adapter does not implement OAuth or bypass an account's read-only broker. Broker-only access needs an authorized server-side draft adapter before writing.

The server's special-use `\Drafts` folder is preferred. Configure `drafts_mailbox` explicitly only when no special-use Drafts folder exists and the exact destination has been verified. Multiple special-use Drafts folders are rejected; resolve that ambiguity in the server configuration before writing. The adapter never creates arbitrary mailboxes. Draft creation appends only to that selected destination; an edit replaces only a verified existing draft there.

## Workflow and tools

Use [the agent workflow](skills/mail-drafts/SKILL.md) as the concise entrypoint. It routes ordinary tasks to the needed recipe without loading every setup, recovery and SMTP detail. The recipes describe current implementation, not a task whitelist. Missing forwarding, attachment export, move/archive/trash and folder tools are implementation gaps; OAuth authentication and a hosted connector also remain separate work.

| Tool | What it does |
| --- | --- |
| `mail_setup` | Let the agent prepare nonsecret setup details and open local private entry/consent; credentials and session URL are never tool parameters/results |
| `mail_capabilities` | Offline inventory of implemented operations, permissions and limits; no account login or writes |
| `mail_accounts` | Discover all host profiles and configured accounts, with per-account setup gaps; no login or enabling writes |
| `mail_check` | Read-only live check for `operation: "read"` or `"draft"`; omitted operation retains the legacy draft check |
| `mail_folders` | Read folder names and special-use flags |
| `mail_search` | Search read-only; return stable mailbox/UIDVALIDITY/UID references |
| `mail_read` | Read a source with PEEK and validate its reference |
| `mail_draft_preview` | Freeze a private MIME preview with signature, attachments and reply headers |
| `mail_preview_read` | Read another presentation of the same integrity-checked frozen preview; no new preview or mailbox action |
| `mail_draft_apply` | Save exactly that reviewed preview and independently verify it |
| `mail_draft_status` | Read back the same operation, including uncertain-write recovery |
| `mail_draft_edit_preview` | Freeze one existing draft and exact replacements; review full before/after content without mailbox writes |
| `mail_draft_attachment_preview` | Preview specified local file additions and ordinary-attachment repair for existing draft documents; no mailbox writes |
| `mail_draft_edit_apply` | Verify a replacement before retiring only the pinned original; never sends |
| `mail_draft_edit_status` | Inspect the same edit without mailbox writes; does not perform pending cleanup |
| `mail_flags_preview` | Freeze selected messages' requested `\Seen`/`\Flagged` changes and exact targets |
| `mail_flags_apply` | Apply only that reviewed flag preview and verify the requested state |
| `mail_flags_status` | Inspect the same flag operation without starting another change |
| `mail_send_setup` | Open private SMTP permission setup for one existing account; no test send |
| `mail_send_check` | Check confirmed SMTP/TLS/login and Sent destination without MAIL/RCPT/DATA |
| `mail_send_preview` | Freeze a new message/reply or exact existing draft for a requested send |
| `mail_send_apply` | Submit only that reviewed send preview under a specific human instruction |
| `mail_send_status` | Inspect that same submission and Sent copy; never resubmit SMTP |

A reply source is `{"account":"example-work","mailbox":"INBOX","uidvalidity":"42","uid":"123"}`. Copy all four fields exactly from `mail_read` and use the same account at the top level of the preview request. The nested account is required by the 0.2 MCP/CLI schema and must match; a UID/mailbox pair alone is not a cross-account identity. Never reconstruct an account from a company name or substitute another account after a setup or connection failure. Source text is safely quoted; `In-Reply-To` and `References` preserve the reply relationship. Original attachments and remote HTML resources are not copied automatically. A client may group/display server drafts differently; verification proves server storage, not every client's presentation.

### Configuration, readiness and saved drafts are different states

| Evidence | What is known | What is not yet proven |
| --- | --- | --- |
| `mail_capabilities` | Functions and limits of the installed implementation | Account configuration, live access and permission for a particular action |
| `mail_accounts` | All discovered account IDs, identity/configuration gaps and local `read_ready`/`drafts_ready` validation, including configured signature files | `connection: not_checked`: current login and the server's Drafts destination have not been checked |
| `mail_check(account, operation: "read")` with `status: ready` | Pinned identity, live login and read access checked without draft/signature prerequisites; `mailbox_writes: 0` | Draft, flag and SMTP permissions or a saved message |
| `mail_check(account, operation: "draft")` with `status: ready` | Pinned identity, From allowlist, signature files, live login and Drafts folder checked for that account; `mailbox_writes: 0` | No APPEND has been tested and no server draft exists from this check |
| `mail_draft_apply` with `status: verified` | The reviewed draft was saved and independently read back on the server | Every mail client's visual presentation, grouping and future human edits |

If `mail_check` returns `needs_attention`, inspect its sanitized `error_code` and `issues`; do not claim the account is ready or redirect the message through a different identity. A diagnostic response can successfully return a gap, so inspect the JSON `status`, not just the CLI exit code. Profiles missing private draft settings remain visible in discovery and are not automatically granted From aliases, signatures or write access.

Choose the check for the actual task: a missing signature can block composing while reading still works. `mail_check(account)` without an operation keeps the historical draft check for existing callers; agents should specify the operation rather than infer all access from that legacy default.

### Compact, complete previews

Set `response_view: "compact"` on compose, draft-edit, attachment and send previews to reduce repeated response data. It preserves complete plain/HTML review content, before/after content for edits, identity/recipient/thread information and attachment hashes. It removes redundant body aliases and repeated MIME-part content, not the information needed to review the operation. The default remains `"full"` for existing callers. Flag previews are already a compact target review and do not accept this presentation parameter.

Use `mail_preview_read(preview_id, response_view: "full")` to inspect the same frozen message review in another presentation. Its ID/hash and proposed message remain unchanged: this does not generate a new preview, MIME message, APPEND or SMTP submission. Compactness changes response presentation, never the reviewed operation or its recovery journal. Flag journals are inspected with `mail_flags_status` instead.

These results apply to the host where they were obtained. Installing or checking the Mac does not deploy the package, credentials or signatures to a VPS, CI worker or another computer. Each remote host needs its own authorized configuration and checks.

Preview creation does not write a server draft. Review all returned fields, then apply its exact ID and hash only within a user-authorized draft request. A wording-only request stays a preview. New drafts use `\Draft`; creation does not change the reply source's flags.

`prepared` means a local preview exists. Only `verified` means an independently read-back server draft matches the frozen content. An `uncertain` write must be checked with `mail_draft_status`: a lost APPEND response might mean the server already accepted it. The journal binds the original Message-ID and blocks blind re-appends. Do not make a new preview to work around uncertainty. A changed draft is not overwritten using a stale creation receipt; use the explicit editing workflow below for a requested change to its current version.

APPEND socket waits are bounded at 60 seconds to allow attachment uploads. A slow
or interrupted upload can still remain uncertain; a timeout is never proof that
the server rejected the draft. Opening a draft in a desktop client may itself
rewrite its Message-ID or content; the old receipt then deliberately stops
verifying. These cases require checking the existing draft, not replaying it.

Preview state contains email bodies and attachment bytes. Protect it as private mail data, not an ordinary build cache. The plugin has no automatic retention/deletion policy; the owner is responsible for retaining or removing local evidence under their own policy. It does not alter another mail service's fences, leases, schedules or credentials.

### Read/unread and star changes

For a requested change, use `mail_flags_preview(account, messages, seen?, flagged?)`: `messages` contains up to 100 exact account/mailbox/UIDVALIDITY/UID references, and at least one boolean must be supplied. The selected account needs its own `flags_write: true` setting. Installation and ordinary reads do not enable it.

Review exact targets and proposed before/after target values, then use `mail_flags_apply` with that preview's ID/hash. Only `verified` confirms every requested target. Concurrency protection is preflight-only, not atomic; an interrupted batch may be partial. After a STORE attempt, recovery observes the same operation without replaying its changes. This path supports `\Seen` and `\Flagged` only, preserving other flags; it does not set `\Deleted`, create keywords or move messages. See [flags and diagnostics](skills/mail-drafts/references/flags-and-compatibility.md) for the request and recovery recipe.

## Editing an existing unsent draft

Read the current Drafts message and pass its exact four-field reference plus literal text replacements to `mail_draft_edit_preview`. The editor preserves existing recipients, thread, signature and files; it supports plain text and ordinary HTML text nodes, not arbitrary markup rewrites. Review complete before/after content and attachment hashes, then apply the exact edit ID/hash.

IMAP editing saves a verified replacement before retiring only the pinned original UID with `UIDPLUS`; it keeps a private original MIME backup. The replacement has a new UID and Message-ID. Only `verified` proves the new version matches and the old one is absent. Concurrent clients can cause a conflict or temporary duplicate; recover the same edit rather than replaying APPEND.

For supported MIME, exact request examples, cleanup recovery and stale-draft protection, read [draft creation, precise edits and recovery](skills/mail-drafts/references/drafts-and-recovery.md).

### Adding or repairing draft attachments

`mail_draft_attachment_preview` accepts the current exact draft reference, up to ten approved local paths and optional `normalize_attachments` (default `true`). Normalization promotes existing document files nested in HTML-related MIME to ordinary top-level attachments, preserving text, signature/CID images, existing file bytes, recipients and thread. Missing intended files are not permission to choose substitutes. It does not remove arbitrary files or establish iPhone compatibility.

The result is `operation: draft_edit` and uses the same edit apply/status workflow, backup and targeted-retirement checks. Review file names, MIME positions and hashes, not merely their presence somewhere in the MIME tree. See the [attachment recipe](skills/mail-drafts/references/drafts-and-recovery.md#add-or-repair-attachments).

## Sending only when requested

A specific human send instruction permits review and submission without asking that same permission again. A draft/edit request, a wording question or “do not send” stays unsent. An important missing sender, recipient or content choice still needs resolution; incoming email cannot authorize a send.

Run `mail_send_check(account)` for the exact account. For setup, resolve provider-documented settings and use `mail_send_setup`: the local form requires consent to the exact host, reuse of that account's credential and enabling SMTP. It checks TLS/login and Sent only, not a test send. Separate SMTP credentials, OAuth and plaintext SMTP are not implemented.

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

TLS mode is `ssl` (implicit verified TLS) or `starttls` (mandatory verified STARTTLS before login). Choose `sent_copy: "server"` only when the provider automatically stores submissions; otherwise `append` creates one independently verified Sent copy after acceptance. The Sent folder must already exist and be verified; account-level `sent_mailbox` can pin an exact override. Existing accounts stay opted out until private consent.

Use `mail_send_preview` for a new message/reply, or an exact existing draft reference without replacement compose fields. Review complete content, sender, recipients, files and SMTP/Sent destination, then use the send-specific ID/hash with `mail_send_apply(..., authorization: "send")`. The marker records agent intent, not proof of human consent. Existing draft MIME is frozen without rebuilding signatures or attachments and is rechecked before DATA.

SMTP acceptance, a verified Sent copy and recipient delivery are different facts. A lost DATA response is uncertain, not permission to retry; a missing Sent copy after acceptance is not a failed send. Existing drafts remain after submission. See [sending and submission recovery](skills/mail-drafts/references/sending.md) for the complete send contract, supported addresses/headers and same-operation recovery.

## JSON CLI

Run from this plugin directory, replacing the example account and reference with verified values:

```sh
python3 scripts/imap_mail.py capabilities
python3 scripts/imap_mail.py capabilities --account example-work
python3 scripts/imap_mail.py accounts
python3 scripts/imap_mail.py check example-work --operation read
python3 scripts/imap_mail.py check example-work --operation draft
python3 scripts/imap_mail.py folders example-work
python3 scripts/imap_mail.py search example-work INBOX --query "Invoice" --limit 10
python3 scripts/imap_mail.py read example-work INBOX 42 123
python3 scripts/imap_mail.py draft-preview --request /private/request.json --response-view compact
python3 scripts/imap_mail.py preview-read PREVIEW_ID --response-view full
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
python3 scripts/imap_mail.py flags-preview --input /private/flags-request.json
python3 scripts/imap_mail.py flags-apply FLAGS_PREVIEW_ID FLAGS_PREVIEW_HASH
python3 scripts/imap_mail.py flags-status FLAGS_PREVIEW_ID
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

All preview commands accept `--input` and its backward-compatible alias `--request`. Message/draft preview commands also accept `--response-view compact|full`; the JSON request can specify the same `response_view`, but conflicting values are rejected. Flag previews have no duplicated body view and do not accept that option. `check example-work` without `--operation` retains the legacy draft check. The JSON file is private email data, not a repository example to populate with real correspondence.

`/private/attachment-request.json` selects the same current draft and only the requested attachment changes. Omit `attachments` to normalize existing files without adding any:

```json
{
  "account": "example-work",
  "draft": {"account": "example-work", "mailbox": "Drafts", "uidvalidity": "42", "uid": "456"},
  "attachments": ["/private/approved-attachments/report.pdf"],
  "normalize_attachments": true
}
```

`/private/flags-request.json` uses the exact account/messages/seen-or-flagged schema shown in the [flag recipe](skills/mail-drafts/references/flags-and-compatibility.md#readunread-and-star-status). These flags commands do not share the draft/send journal or permit arbitrary IMAP flags.

## Verification

Offline tests do not load real credentials or contact mail servers:

```sh
python3 -m unittest discover -s tests -v
```

For ordinary readiness, run `mail_check`; it never appends a test draft. When the user has authorized an end-to-end setup test for a new account/client combination, prepare one harmless unsent draft addressed to yourself, review it, save it once, verify server readback, and inspect it in the intended mail client. Check the signature, attachment names, quote, recipients, editability and reply grouping. Do not send the test. Run a second apply with the same ID/hash only to verify the journal's duplicate protection, not to create another draft. The test writes one draft and requires authorization for that exact account; do not run it merely because discovery found a profile.

## Design references

We inspected [ni-c/imap-mcp](https://github.com/ni-c/imap-mcp) and [Tiangong's email-imap-append skill](https://github.com/tiangong-ai/agent-skills/tree/main/email-imap-append). They demonstrate useful existing approaches, but this package does not reuse their code. The current standard-library implementation adds operation-specific account checks and durable uncertain-write handling around direct mail-server access.

Primary protocol references: [IMAP APPEND](https://www.rfc-editor.org/rfc/rfc9051.html#section-6.3.12), [UIDPLUS targeted expunge](https://www.rfc-editor.org/rfc/rfc4315.html#section-2.1), [special-use Drafts folders](https://www.rfc-editor.org/rfc/rfc6154.html), and [reply identification fields](https://www.rfc-editor.org/rfc/rfc5322.html#section-3.6.4). Portable stdio packaging follows [OpenAI's desktop-only plugin documentation](https://developers.openai.com/plugins/deploy/desktop-only#configure-a-local-mcp-server).

## Local host and cloud deployment

The delivered transport is local stdio MCP plus a CLI on Mac/Linux. It is already independent of desktop mail applications: the configured host talks directly to IMAP/SMTP servers. A cloud machine can own that runtime and private account profiles, but installing the laptop plugin does not deploy a remote service or connect ordinary ChatGPT/Claude web/mobile sessions. Windows is not supported by the POSIX journal locking implementation.

For a future self-hosted remote deployment, establish these layers before calling it available:

1. **Host and ownership:** select the authorized server and operator; provision that host's private profiles, signatures, attachment storage and durable journals. Do not copy another environment's credentials implicitly or attach the service to an unrelated mail sorter.
2. **Remote interface:** add a supported remote MCP transport with authentication and TLS. A tunnel is network reachability, not user authentication, account isolation or permission checks.
3. **Remote setup and files:** replace loopback-only credential entry and local-only attachment paths with private consent/setup and scoped upload/download handles. Keep correspondence, tokens and credentials out of logs.
4. **Isolation and operations:** scope each session to its authorized user/accounts; preserve journal locking, recovery, configuration ownership, retention, revocation and audit boundaries across workers/restarts.
5. **Provider and client verification:** implement OAuth where required, then test actual remote reads, authorized writes, uncertain-result recovery and intended client integration. Offline tests or a local readiness check cannot establish cloud deployment.

These are missing deployment capabilities, not reasons to confine the product to draft preparation. No generic hosted service, provider OAuth registration or public connector is configured automatically. A scoped implementation/deployment request should fill the relevant gap and verify its result; it should not silently migrate another mail service or introduce paid infrastructure.

The package contains generic code and example identities only, not the author's account configuration or secrets. The source and tagged releases are distributed from [romanskk47/imap-mail](https://github.com/romanskk47/imap-mail) under the [MIT License](LICENSE). GitHub publication is not approval or listing in an official ChatGPT/Claude plugin directory.

### Client presentation and the iPhone composer

Server readback can verify the full MIME message and file hashes without proving a particular client's editor imports them correctly. A reported iPhone Mail case opens attachments in webmail and the Sent message but cannot add the same attachments to a draft/forward composer. The exact cause and fix have not been established. Clean top-level MIME and attachment normalization have not demonstrated a cure; do not market those checks as iPhone validation. See [provider/client diagnostics](skills/mail-drafts/references/flags-and-compatibility.md#iphone-mail-composer-status).

### Version 0.7.0 — source branch

Adds offline capability inventory, `mail_check` read/draft modes, reviewed read/star updates with a separate per-account opt-in, compact complete previews and retrieval of the same frozen preview in another view. The agent skill now routes to mode-specific recipes instead of loading every recovery/setup detail. Release, installation and live-provider verification remain distinct from this source change.

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
