# IMAP Mail

Connect an AI agent directly to your existing email account through IMAP and SMTP. Search and read mail, prepare or edit drafts, send a specific message when requested, and change read/star status without automating Apple Mail or another desktop mail client.

The agent does the technical work. Your request determines the action: a wording question stays a preview, a draft request stays unsent, and an explicit send request submits the reviewed message. Installing the plugin does not grant access to anyone else's mailbox or enable account permissions.

**Source status:** this branch prepares **0.7.0**. Capability discovery, operation-specific read checks, reviewed read/star changes and compact previews are new here; the published **0.6.0** release does not include them. Source changes and offline tests are not a release, live provider validation or an iPhone compatibility claim.

## What it can do today

| Task | Current implementation |
| --- | --- |
| Discover accounts and available functions | Offline capability inventory and per-account configuration gaps; no login or permission changes |
| Check access, list folders, search and read | Read-only IMAP, stable message references, full MIME and attachment hash verification up to 16 MiB |
| Compose or reply | Reviewed draft or send preview; configured signature, chosen attachments and reply headers |
| Edit an existing unsent draft | Exact text replacements; preserve signature, thread and files; verify replacement before retiring the pinned original |
| Add or repair draft attachments | Add approved local files or promote document parts to ordinary attachments, preserving their bytes |
| Mark selected messages read/unread or starred/unstarred | Reviewed, verified changes to `\Seen` and `\Flagged`; separately enabled per account |
| Send a specified message or saved draft | Opt-in SMTP, frozen review, durable submission journal and separate Sent-copy verification |
| Download files, forward, move/archive/trash, manage folders | Not implemented as dedicated tools yet |
| Connect OAuth-only providers | Not implemented; current authentication is password/app-password over verified TLS |
| Use from ordinary ChatGPT/Claude web or mobile chats | Not yet a hosted connector; current package is a local stdio MCP server and CLI |

These are implementation states, not a whitelist of what an AI may help with. `mail_capabilities` gives the agent a cheap inventory, and account checks distinguish unsupported functions from missing configuration or a connection problem. Missing ordinary mail functions should be added to the plugin rather than described as restrictions imposed by IMAP or by the skill.

## Give this to your agent

> Install IMAP Mail from https://github.com/romanskk47/imap-mail in my Codex or Claude Code. Reuse an existing installation if present. Read the packaged setup instructions, discover available functions and accounts, and guide me through connecting my own authorized mailbox. Handle the technical setup; ask only for missing choices. Use private credential entry, never passwords in chat. Check the selected operation without creating or sending a test message.

Po polsku:

> Zainstaluj IMAP Mail z https://github.com/romanskk47/imap-mail w moim Codex albo Claude Code. Jeśli jest już zainstalowany, wykorzystaj obecną instalację. Przeczytaj instrukcję konfiguracji, sprawdź dostępne funkcje i konta, a następnie przeprowadź mnie przez podłączenie mojego uprawnionego konta. Sam zajmij się techniczną konfiguracją, pytaj tylko o brakujące wybory. Hasło wpiszę prywatnie, nie na czacie. Sprawdź dostęp dla wybranej czynności bez tworzenia ani wysyłania wiadomości testowej.

## Installation reference for agents

Inspect existing installations and use the client's plugin commands:

```sh
codex plugin marketplace add romanskk47/imap-mail
codex plugin add imap-mail@imap-mail-plugins
```

```sh
claude plugin marketplace add romanskk47/imap-mail
claude plugin install imap-mail@imap-mail-plugins --scope user
```

Reload the client or start a new session as needed, then follow the bundled [Setup workflow](plugins/imap-mail/skills/setup/SKILL.md). The public marketplace is `imap-mail-plugins`; no private marketplace is needed. GitHub publication does not mean official-directory approval.

## Independent of a mail app, not independent of a host

The Python implementation already talks to mail servers directly. Apple Mail does not need to be installed or running. The same code can run on an authorized Mac or Linux host, but that host must own the credentials, configuration, signatures, approved attachment files and private operation journals.

The current package uses **stdio MCP or a JSON CLI**. Installing it on a laptop does not install a cloud connector or configure a VPS. A production remote service still needs authenticated remote MCP, per-user/account isolation, private credential provisioning, durable state and attachment upload/download handles instead of laptop-only paths. OAuth provider integration and remote setup are also separate work. See the [deployment boundary](plugins/imap-mail/README.md#local-host-and-cloud-deployment) before planning a hosted installation.

## Compatibility and verification

- Python **3.10+**, standard library only, **macOS or Linux**. Windows is not supported by the POSIX journal locking implementation.
- Verified TLS for IMAP and optional SMTP. Provider and administrator authentication policies still apply.
- New-account setup uses a private local form. Credentials are stored in a private **plaintext password file (0600)**, not encrypted Keychain storage; use revocable app passwords where available.
- Draft editing needs `UIDPLUS`. New local attachments need an approved directory. Existing HTML/CID signatures can be configured privately; desktop signatures are not imported automatically.
- Server MIME/hash verification proves the stored files and reviewed message, not every client's composer behavior. An iPhone Mail case can open the attachments in a sent message and webmail but fail to import them into a draft/forward composer. Its root cause and fix are not established; attachment normalization is not a proven cure.
- SMTP acceptance, a verified Sent copy and delivery to the recipient are different outcomes. An uncertain submission is inspected through its existing journal, never blindly resubmitted.

Offline tests use synthetic data and do not contact real mailboxes:

```sh
python3 -m unittest discover -s plugins/imap-mail/tests -v
```

See the [plugin guide](plugins/imap-mail/README.md) for configuration and CLI contracts, and the [agent workflow](plugins/imap-mail/skills/mail-drafts/SKILL.md) for concise task routing and recovery recipes.

## License

[MIT](LICENSE). Only generic code, synthetic tests and documentation belong in this repository. Credentials, account profiles, signatures, operation journals and private correspondence stay outside Git.
