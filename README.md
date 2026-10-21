# IMAP Mail

Read your existing email and save reviewed, **unsent drafts** from Codex or Claude Code. The agent handles setup; you enter credentials only in a private form opened locally in your browser.

This plugin has no send operation or SMTP implementation. It does not delete, move, sort or mark original messages read. It supports signatures, local attachments and replies in the original conversation, with independent server readback and protection against duplicate saves.

## Give this to your agent

> Install IMAP Mail from https://github.com/romanskk47/imap-mail in my Codex or Claude Code. Check for an existing installation first and reuse it if present. Read the packaged setup instructions and guide me through connecting my own authorized mailbox. Handle the technical setup yourself; ask only for missing choices. Never request passwords in chat: use the private local setup form. Check readiness without creating or sending a message.

Po polsku:

> Zainstaluj IMAP Mail z https://github.com/romanskk47/imap-mail w moim Codex albo Claude Code. Najpierw sprawdź, czy już jest zainstalowany, i nie twórz duplikatu. Przeczytaj instrukcję konfiguracji pluginu i przeprowadź mnie przez podłączenie mojego uprawnionego konta pocztowego. Sam zajmij się techniczną konfiguracją, pytaj tylko o brakujące wybory. Nie proś o hasło w czacie — użyj prywatnego lokalnego formularza. Sprawdź gotowość bez tworzenia ani wysyłania wiadomości.

Every person connects their own mailbox. Installing this repository does not grant access to the author's or anyone else's email, signatures or passwords.

## Compatibility and limits

- Local **Codex or Claude Code**, on **macOS or Linux**, with **Python 3.10+**. Only Python's standard library is needed.
- A mailbox allowing password/app-password IMAP access over verified TLS. **OAuth-only accounts are not supported.** A provider's policy may disable password-based IMAP.
- Setup stores credentials in a private local **plaintext password file (0600)**, not encrypted Keychain storage. Use a revocable app password where available; protect the computer and backups.
- Setup supports new accounts, plain-text signatures or an explicit no-signature choice. Existing HTML/CID signatures can be configured privately; desktop signatures are not imported automatically.
- Attachments require an explicitly approved local directory. Nothing is sent: you review and click Send in your usual mail client.
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
