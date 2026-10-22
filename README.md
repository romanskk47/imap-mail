# IMAP Mail

Email tools for agents in Codex and Claude Code: read/search, create or edit unsent drafts, and send when explicitly requested. Python 3.10+, standard library only. No Apple Mail, paid API, background process or VPS is required.

The agent handles existing account configuration directly. A private form is available when a new secret must be entered; it is not a mandatory step for accounts that already work. Installing the plugin gives nobody access to the author's or another person's email.

## Install through your agent

> Install IMAP Mail from https://github.com/romanskk47/imap-mail in Codex or Claude Code. Reuse an existing installation and account access. Handle technical settings; ask only about missing choices. Never ask for passwords in chat. Check readiness without creating or sending a message.

Installation reference:

```sh
# Codex
codex plugin marketplace add romanskk47/imap-mail
codex plugin add imap-mail@imap-mail-plugins

# Claude Code
claude plugin marketplace add romanskk47/imap-mail
claude plugin install imap-mail@imap-mail-plugins --scope user
```

Refresh the plugin/start a new session as required by the client. The packaged [setup skill](plugins/imap-mail/skills/setup/SKILL.md) guides discovery and configuration. Each host needs its own authorized credentials and network access.

## What the agent can do

- Read/search without marking messages read; inspect MIME and attachment metadata.
- Compose replies with a configured signature and approved local attachments. Cc / Polish DW is supported; Reply-All retains other participants, excluding the account's own addresses.
- Edit draft wording or add/repair attachments through a reviewed replacement, preserving other content and keeping a private original backup.
- Enable SMTP with your explicit request using the exact account's existing credentials, without a browser or test email. TLS/login and the Sent folder are checked first.
- Send a specifically authorized message after reviewing its frozen preview. Draft/wording requests stay unsent.

Safety remains in the implementation: account identity binding, immutable previews, readback verification and same-operation recovery. Lost write responses are not blindly retried. SMTP acceptance is not proof of recipient delivery. Enabling sending is not permission to send arbitrary messages.

There are no dedicated forward, attachment-download, move/archive/trash, flag or folder-management tools in this release. These are implementation gaps, not restrictions imposed by IMAP or the skill.

## Compatibility and privacy

Local macOS/Linux, verified TLS and password/app-password IMAP; SMTP also requires authenticated TLS. OAuth-only accounts and Windows are not supported. Draft replacement requires UIDPLUS. New attachment files must be inside an approved local directory.

The optional new-account wizard stores a plaintext password file with mode0600 in a private directory, not encrypted Keychain storage. Credentials, signatures, correspondence and operation journals stay outside Git. This package is not a ChatGPT/Claude web/mobile connector or a mail sorter; cloud environments may block mail ports.

See the [plugin guide](plugins/imap-mail/README.md) for configuration, tools and recovery. Offline tests use synthetic mail only:

```sh
python3 -m unittest discover -s plugins/imap-mail/tests -v
```

[MIT](LICENSE). Publication does not imply listing or approval in an official plugin directory.
