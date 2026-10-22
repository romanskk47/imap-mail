# Sending and submission recovery

Read this for a specific human-requested send or an unresolved submission. A draft/edit/wording request is not a send instruction. If the intended message and recipients are already specified and authorized, review and proceed without asking the same permission again. Ask when an important account, recipient or content choice remains missing.

## Account readiness

Use `mail_send_check(account)` for the exact selected account. SMTP must be separately enabled there; draft readiness does not grant it. Setup does not send test email.

For an SMTP gap, follow [private SMTP setup](../../setup/SKILL.md#optional-smtp-sending). Resolve provider-documented settings rather than guessing a host. The local form requires consent to the exact SMTP hostname, reuse of that account's credential and sending permission. Never place a password in chat or tool arguments. The current implementation supports authenticated password/app-password SMTP over verified implicit TLS or mandatory STARTTLS, not separate SMTP credentials, OAuth or plaintext SMTP.

## Freeze the message actually requested

Use `mail_send_preview`, not a draft preview. For new mail/replies use the compose fields described in the draft workflow. For “send this saved draft”, pass only the exact account/draft reference plus optional presentation choice:

```json
{
  "account": "example-work",
  "draft": {"account": "example-work", "mailbox": "Drafts", "uidvalidity": "42", "uid": "456"},
  "response_view": "compact"
}
```

The full existing MIME is frozen without rebuilding the signature or files and is rechecked before DATA. Existing draft and compose fields are mutually exclusive; do not silently substitute text or recipients. This implementation supports ASCII envelope addresses and To/Cc; unsupported Bcc/Resent headers on existing drafts are rejected.

Review complete content, signature, attachment hashes, sender, recipients, subject and SMTP/Sent destination. `mail_preview_read` can reopen the same frozen ID/hash if another view is needed. Then call `mail_send_apply` with that send-preview ID/hash and `authorization: "send"` under the human's sending instruction. That marker records agent intent; it does not itself prove consent. A draft preview cannot be sent or a send preview saved through draft apply.

Every recipient must be accepted before DATA; otherwise submission aborts without DATA. Existing server drafts remain after sending and are not marked answered. Their continued presence is not a reason to submit them again or remove them without authorized supported functionality.

## Interpret and recover the receipt

Standard SMTP has no general idempotency key. Message-ID alone cannot prevent duplicate delivery. The journal records intent before DATA:

| Evidence | Meaning |
| --- | --- |
| SMTP accepted | Submission was accepted by the server; recipient delivery is not proven |
| Sent copy verified | The stored copy matches; this is separate from SMTP acceptance |
| DATA response uncertain | Submission may already have succeeded; do not resend |
| Sent copy missing after accepted SMTP | Submission still succeeded; a storage problem is not a send failure |

For uncertain acceptance use `mail_send_status` on the **same preview ID**. Never make another preview/resubmit to work around uncertainty. A matching Sent copy alone cannot convert uncertain SMTP into proven acceptance or delivery. A status check never resubmits DATA.

Recipient-side delivery/bounce handling, background queues, automatic delivery retries and bulk marketing are not implemented. Report the receipt's actual evidence instead of claiming delivery.
