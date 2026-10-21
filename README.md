# IMAP Mail

Read your existing email, save reviewed **unsent drafts**, or **send a specific message when you explicitly ask** from Codex or Claude Code. The agent handles setup; credentials and account permissions stay private on your computer.

The action follows your request: "prepare a draft" stays unsent, "what should I reply?" stays a preview, and "send this message to X" uses a separate reviewed SMTP submission. Sending is disabled per account until you enable it through private setup. The plugin does not delete, move, sort or mark original messages read. Signatures, attachments and reply headers are preserved.

SMTP acceptance does not prove delivery. A lost submission response is reported as uncertain and is never blindly retried; a copy in Sent is tracked separately. See the [sending workflow](plugins/imap-mail/README.md#sending-only-when-requested).

## Give this to your agent

> Install IMAP Mail from https://github.com/romanskk47/imap-mail in my Codex or Claude Code. Check for an existing installation first and reuse it if present. Read the packaged setup instructions and guide me through connecting my own authorized mailbox. Handle the technical setup yourself; ask only for missing choices. Never request passwords in chat: use the private local setup form. Check readiness without creating or sending a message.

Po polsku:

> Zainstaluj IMAP Mail z https://github.com/romanskk47/imap-mail w moim Codex albo Claude Code. Najpierw sprawdź, czy już jest zainstalowany, i nie twórz duplikatu. Przeczytaj instrukcję konfiguracji pluginu i przeprowadź mnie przez podłączenie mojego uprawnionego konta pocztowego. Sam zajmij się techniczną konfiguracją, pytaj tylko o brakujące wybory. Nie proś o hasło w czacie — użyj prywatnego lokalnego formularza. Sprawdź gotowość bez tworzenia ani wysyłania wiadomości.

Every person connects their own mailbox. Installing this repository does not grant access to the author's or anyone else's email, signatures or passwords.

## Compatibility and limits

- Local **Codex or Claude Code**, on **macOS or Linux**, with **Python 3.10+**. Only Python's standard library is needed.
- A mailbox allowing password/app-password IMAP access over verified TLS. Optional sending also needs authenticated SMTP over implicit TLS or STARTTLS. **OAuth-only accounts are not supported.** Provider or administrator policies may disable password-based access.
- Setup stores credentials in a private local **plaintext password file (0600)**, not encrypted Keychain storage. Use a revocable app password where available; protect the computer and backups.
- Setup supports new accounts, plain-text signatures or an explicit no-signature choice. Existing HTML/CID signatures can be configured privately; desktop signatures are not imported automatically.
- New local attachments require an explicitly approved directory. Existing server drafts can be frozen for a send-specific preview without rebuilding their signature or attachments; sending does not remove the old draft.
- This is **not a ChatGPT/Claude web or mobile connector**, Windows package, cloud service or background mail sorter. A remote host needs its own authorized configuration.

## Installation reference for agents

Use the installed client's official plugin commands. Inspect existing marketplaces/installations before adding another source.

Codex:

```sh
codex plugin marketplace add romanskk47/imap-mail
codex plugin add imap-mail@imap-mail-plugins
```

Claude Code:

```sh
claude plugin marketplace add romanskk47/imap-mail
claude plugin install imap-mail@imap-mail-plugins --scope user
```

Reload/restart the client or open a new session as required to expose the tools. Then follow the bundled [Setup skill](plugins/imap-mail/skills/setup/SKILL.md): inventory accounts, reuse working access, run a read-only readiness check, or open private setup for a new account. Do not create a test draft unless the person explicitly requests one.

When the person requests sending, run `mail_send_check` for that exact account. If SMTP is not enabled, resolve the provider's documented submission settings and call `mail_send_setup`. The local form requires the person to type the exact SMTP host and approve reuse of that account's credential and sending permission. Setup checks login and the Sent folder only; it never sends a test email. Never ask for a password in chat or silently enable sending on all discovered accounts.

The marketplace name is `imap-mail-plugins`; the plugin name is `imap-mail`. Both public marketplace catalogs point only to `plugins/imap-mail`. No private marketplace is required. Publication here does not imply listing or approval in an official plugin directory.

## Documentation and verification

See the [full plugin guide](plugins/imap-mail/README.md) for configuration, signatures, MCP/CLI tools, uncertain-write recovery and security boundaries.

Offline tests use synthetic data and do not connect to real mailboxes:

```sh
python3 -m unittest discover -s plugins/imap-mail/tests -v
```

Tests are not proof of compatibility with every provider or mail client's presentation. `mail_check` verifies live readiness without appending a message; a verified draft receipt proves server storage of that specific reviewed draft.

## License

[MIT](LICENSE). This repository contains generic plugin code, synthetic tests and documentation only. Runtime credentials, account profiles, signatures, draft state and private correspondence must stay outside Git.
