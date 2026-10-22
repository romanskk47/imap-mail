---
name: setup
description: Configure email access for an agent, reusing existing exact-account credentials and using private entry only when a secret is missing.
---

# Configure email for the agent

Start with `mail_accounts`. Reuse exact-account access, not a form detour. Run `mail_check`. Ask only for genuinely missing identity/provider/signature choices.

## Enable SMTP on an existing account

The user must explicitly request enabling sending for the named account(s). Resolve host/port/TLS/Sent-copy behavior from authorized settings or trusted provider documentation, not the email domain. Run `mail_send_configure(account, host, port, security, sent_copy, authorization: "enable_sending")`. It reuses only that account's credential, checks verified TLS/login/Sent, then saves atomically. No MAIL/RCPT/DATA or test email. Establish user intent from the conversation; the marker is not proof.

Use `sent_copy: "server"` when the provider saves sent mail, otherwise `"append"`; do not guess and duplicate. Recheck with `mail_send_check`. This authorizes no message. Do not change identity, copy credentials across accounts or bypass broker policy. Separate SMTP credentials/OAuth are unsupported.

## Missing credentials / new account

Prepare verified nonsecret metadata for `mail_setup`; never invent entity/signature. Private entry is for missing secrets or user preference. `mail_send_setup` remains an optional SMTP form.

Never request passwords in chat or expose them in tool arguments/logs. Do not capture the private form/session URL. It stores a plaintext password file (0600), not Keychain encryption; the user approves that storage, host and signature privately. After entry, resume `mail_accounts` and `mail_check`. Setup performs no test draft or send and does not deploy a remote service. See [the guide](../../README.md) for private configuration and headless provisioning.
