# Configure email for the agent

Reuse a fresh `mail_capabilities` result or use `mail_accounts` for full discovery. Reuse exact-account access. Check the requested operation with `mail_check(operation: "read")`, `mail_check(operation: "draft")`, or `mail_send_check`. Ask only for genuinely missing identity/provider/signature choices.

## Enable SMTP on an existing account

The user must explicitly request enabling sending for the named account(s). Resolve host/port/TLS/Sent-copy behavior from authorized settings or trusted provider documentation, not the email domain. Run `mail_send_configure(account, host, port, security, sent_copy, authorization: "enable_sending")`. It reuses only that account's credential, checks verified TLS/login/Sent, then saves atomically. No MAIL/RCPT/DATA or test email. Establish user intent from the conversation; the marker is not proof.

Use `sent_copy: "server"` when the provider saves sent mail, otherwise `"append"`; do not guess and duplicate. Recheck with `mail_send_check`. This authorizes no message. Do not change identity, copy credentials across accounts or bypass broker policy. Separate SMTP credentials/OAuth are unsupported.

## Missing credentials / new account

Prepare verified nonsecret metadata for `mail_setup`; never invent entity/signature. Private entry is for missing secrets or user preference. `mail_send_setup` remains an optional SMTP form.

Never request passwords in chat or expose them in tool arguments/logs. Do not capture the private form/session URL. It stores a plaintext password file (0600), not Keychain encryption; the user approves that storage, host and signature privately. After entry, resume `mail_accounts` and `mail_check`. Setup performs no test draft or send and does not deploy a remote service. See [the guide](../../../README.md) for private configuration and headless provisioning.

## Host-owned configuration

Profiles come from `MAIL_PROFILES_JSON`, `MAIL_PROFILES_DIR`, or `~/.secrets/services/mail`. Each profile references exactly one `password`, `password_env`, or `password_file`. Settings come from `MAIL_DRAFTS_CONFIG` or `~/.config/imap-mail/settings.json`; preserve that host's existing signatures and journal. Prefer existing injected secrets/private files. Code installation from GitHub does not copy credentials or grant mail-port access to another cloud/VPS runtime. Verify the exact account from the runtime that will use it.
