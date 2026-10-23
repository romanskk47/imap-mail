# IMAP Mail

One plugin and one `mail` skill for email in Codex and Claude Code. Search and read through IMAP, compose or edit drafts, change read/star status, and send requested messages through opt-in SMTP. It runs directly on a configured Mac, Linux host or cloud runtime.

Version **0.7.2** adds hash-pinned original attachment export to the existing email workflow. The package uses Python 3.10+ and the standard library.

## Install

Ask your agent to install IMAP Mail from this repository and reuse existing account access. It handles technical configuration and asks only for missing choices. Passwords belong in the host's private secret store, never chat.

```sh
# Codex
codex plugin marketplace add romanskk47/imap-mail
codex plugin add imap-mail@imap-mail-plugins

# Claude Code
claude plugin marketplace add romanskk47/imap-mail
claude plugin install imap-mail@imap-mail-plugins --scope user
```

Reuse an existing installation instead of installing the same plugin from two marketplaces. Refresh the client/start a new session when needed. The single [mail workflow](plugins/imap-mail/skills/mail/SKILL.md) includes account setup as a reference; there is no separate setup skill to choose.

## One code source, host-owned access

GitHub distributes code and instructions. Each runtime supplies its own authorized account profiles, settings, signatures, operation journal and network access. GitHub repository secrets and a laptop installation do not automatically become cloud-task secrets.

The plugin discovers profiles from `MAIL_PROFILES_JSON`, `MAIL_PROFILES_DIR` or `~/.secrets/services/mail`; profile passwords can reference injected environment variables or private files. Settings use `MAIL_DRAFTS_CONFIG` or `~/.config/imap-mail/settings.json`. No secrets, signatures or correspondence belong in this repository.

Direct access requires the runtime to permit the provider's IMAP/SMTP ports. An HTTPS domain allowlist alone does not establish that access. A configured SSH/stdio launcher can run the same plugin on an authorized host. This release provides no hosted HTTP connector. Only a successful check from the actual runtime confirms connectivity.

Mailroom or another automatic sorter remains an independent service with its own configuration, policy, schedule and state. This plugin installs no sorter, daemon or background scan.

## Implemented actions

| Action | Behavior |
| --- | --- |
| Discover accounts/capabilities | Offline inventory; keeps account identity and readiness gaps explicit |
| Read/search/list folders | IMAP PEEK, stable references, MIME and file hashes up to 16 MiB |
| Save original attachments | Exact message and MIME part hashes; private host-local files up to 10 MiB, no flag changes |
| Compose/reply | Frozen preview, configured signature, To/Cc and selected attachments |
| Edit drafts/files | Verify a replacement before retiring the pinned original; preserve unrelated content |
| Mark read/star status | Reviewed changes to `\Seen` and `\Flagged`, enabled per account |
| Configure SMTP | Reuse the exact account's credential after explicit setup authorization; no test mail |
| Send | Explicit message request, frozen review, submission journal and separate Sent-copy verification |

Forward, move/archive/trash, folder management and OAuth provider authentication are not implemented as dedicated operations. `mail_capabilities` reports these gaps so the agent can explain the actual limitation when relevant.

A wording request stays a preview; a draft stays unsent. Only the user's send instruction authorizes submitting that message. SMTP acceptance is not proof of delivery. Uncertain writes use their existing operation journal, never blind retries.

See the [plugin guide](plugins/imap-mail/README.md) for private configuration, CLI and recovery. Windows is unsupported because journals use POSIX locking. Draft replacement requires UIDPLUS. New attachments must be in approved host-local directories. The optional new-account form stores a private plaintext password file (0600), not encrypted Keychain storage.

## Verification

Offline tests use synthetic mail and never contact real mailboxes:

```sh
python3 -m unittest discover -s plugins/imap-mail/tests -v
```

A passing suite or published version does not verify any user's account, cloud connection or mail-client display.

[MIT](LICENSE). Publication does not imply listing or approval in an official plugin directory.
