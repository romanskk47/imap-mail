# Optional private HTTP transport

This is a **server-only optional bridge**, not a replacement for the dependency-free
stdio plugin. It uses the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
`mcp==2.3.0` low-level `Server` with stateless Streamable HTTP and JSON responses.
No OpenAI/Anthropic API or inference call is involved. Installing this code does not
publish a listener, configure a cloud client, grant mailbox permissions or send mail.

## Boundary and ownership

One existing mail backend remains the credential and operation-journal owner.
The bridge starts its **fixed, administrator-configured stdio command** only when a
tool listing/call arrives. It forwards tool definitions/annotations and complete
results, including `isError`, `_meta`, content and structured content. It never
retries a tool call after dispatch. The backend must use the same durable private
state directory on every invocation; per-request temporary journals are unsafe.

Separate named bearer principals let the owner revoke one client without changing
mail passwords. **They are not tenant or per-account isolation**: every configured
principal can invoke the same owner's available backend tools/accounts. Do not
share these keys with employees or other people. Different owners need separate
backends, credentials, journals and endpoints. Existing entity/account identity
checks and draft/flag/SMTP opt-ins remain unchanged.

Only HTTPS server-to-server clients are intended. There is no public health,
OAuth, CORS or unauthenticated discovery route. Every HTTP path requires a bearer;
only `/mcp` is then routed to the SDK. Hostnames/origins are exact allowlists; an
absent Origin is accepted, browser Origins are rejected unless explicitly listed.
No query-string tokens are accepted. GET/DELETE are handed to the stateless SDK,
not converted into mailbox calls.

Request bodies are limited to 3 MiB, body ingress to 10 seconds by default,
simultaneous requests to 2, and backend work to 120 seconds. These bounds do not
make an interrupted write a failure: a call may already have been accepted by the
provider. A failed backend response yields `uncertain` plus a private bridge receipt
ID and the same-operation status tool where applicable. Never create a fresh
preview/resubmit SMTP as a transport retry. A crashed bridge can leave a `pending`
receipt, which also means **may have run**, not safely retryable.
HTTP ingress slots and actual backend-lifetime slots are bounded independently:
a disconnected request may leave the SDK handler completing its bounded work,
but cannot free that handler's backend slot early or launch unlimited processes.

The private bridge dispatch journal stores only random receipt ID, named principal,
tool name, timestamps and receipt state, not tokens, mail arguments or results.
It is diagnostic evidence, not a second submission/idempotency authority. The
existing mail operation journal remains authoritative for recovery. A received
response receipt is not proof that the client received it or that a mailbox write
was verified; inspect the tool's actual result.

`mail_setup` and `mail_send_setup` remain visible with their original descriptions,
but calling either remotely returns `local_setup_only` without launching the
backend. Those private desktop wizards cannot configure this headless host.
Administrators provision/consent remotely; never ask for credentials in chat.
Host-local attachment paths still refer to the server, not the phone/laptop.
This bridge does not add file upload/download, forwarding or provider OAuth.

## Private deployment

1. Use a dedicated unprivileged service user. Install optional requirements in a
   dedicated virtual environment; the ordinary plugin needs no new dependency:

   ```sh
   python3.12 -m venv /private/service/venv
   /private/service/venv/bin/pip install -r bridge/requirements.txt
   ```

2. An administrator privately generates separate cryptographically random
   URL-safe bearer tokens (at least 32 random bytes each). Store their **SHA-256
   hex digests only** in a host-owned configuration. The raw keys go through the
   cloud service's private secret-entry UI, never Git, chat, terminal history or
   model-visible output. Rotate/revoke by replacing one digest and restarting.
   The verifier requires URL-safe tokens of 43–128 characters; arbitrary weak
   human passwords are not appropriate.

3. Create a service-owned `0700` bridge receipt directory and private `0600`
   configuration. No symlink component or hardlinked configuration is accepted.
   Use canonical absolute paths (on macOS `/private/var`, not `/var` symlink).
   Example **nonfunctional placeholders**:

   ```json
   {
     "version": 1,
     "backend_command": ["/usr/bin/sudo", "-n", "/usr/local/libexec/imap-mail-stdio"],
     "principals": {
       "codex-cloud": "REPLACE_WITH_64_CHARACTER_SHA256_HEX",
       "claude-cloud": "REPLACE_WITH_DIFFERENT_64_CHARACTER_SHA256_HEX"
     },
     "allowed_hosts": ["mail.example.invalid"],
     "allowed_origins": [],
     "journal_dir": "/private/service/bridge-receipts",
     "max_concurrency": 2,
     "timeout_seconds": 120,
     "body_timeout_seconds": 10
   }
   ```

   This example's sudo helper must itself be an audited fixed, no-argument
   launcher, with a sudoers grant only for that exact command. Never grant generic
   shell, Python or systemd-run execution to the HTTP service. A `NoNewPrivileges`
   service sandbox prevents sudo elevation; account for that deliberately instead
   of disabling protections on an unrelated service. Backend credentials and
   transient-session cleanup belong to the helper, not incoming HTTP requests.

4. Start the bridge with its module directory on `PYTHONPATH`:

   ```sh
   PYTHONPATH=/private/source/bridge /private/service/venv/bin/python \
     -m imap_mail_bridge.server --config /private/service/settings.json
   ```

   The default listener is `127.0.0.1:8766`; the CLI refuses non-loopback binds
   and privileged ports by default. A reverse proxy inside a private container
   network can instead use an **exact RFC1918 interface**, for example
   `--listen 172.18.0.1 --allow-private-bind`. This explicit opt-in still rejects
   wildcard, public, multicast, link-local, documentation and private IPv6 binds.
   Protect that listener with host/container-network firewall rules: RFC1918 is
   not itself authentication or proof of a trusted network. Never publish it as
   a container port. Supervise it with the host's service manager.
   Give only this service read access to its configuration and write access to
   its receipt directory. No timer/background mailbox scan is installed.

5. Use the host's existing trusted HTTPS reverse proxy, preserving its configured
   public Host. Route just the chosen endpoint to this loopback listener; never
   expose the listener or an unauthenticated upstream. TLS certificate checking
   stays enabled. If using a prefix, strip it: for example external
   `/imap-mail/mcp` must reach backend `/mcp`. Deny other paths. Disable access
   logging (or demonstrably redact Authorization); never log request bodies or
   SDK parsing exception payloads. Do not put a raw key in proxy labels/config.
   Test proxy Host rewriting before adding aliases to any allowlist.

No deployment is complete until one actual tool call succeeds from **each cloud
runtime**, not merely from a Mac, VPS, browser or local CLI. Connectivity checks
must not create/send test messages or change existing mail.

## Cloud client recipes (administrator/agent reference)

The endpoint/key permissions must be confined to the correct private owner and
cloud environment. Do not add all of a person's mail accounts to a shared company
environment just because that environment already exists. Cloud network allowlists
must permit HTTPS/443 to the exact endpoint host. Mac-installed skills/plugins and
credentials do not automatically appear in a new cloud VM.

### Codex Cloud

A private cloud environment can publish the generic client configuration and
request a personal-vault **network secret** named `IMAP_MAIL_TOKEN`. That secret's
allowed HTTPS destination must be exactly the bridge host. The VM receives its
placeholder; the network proxy substitutes it on permitted HTTPS requests during
setup and task runtime. Do not copy mail passwords to the VM.

For the environment's Codex MCP config, use a supported environment-variable field,
not `${VAR}` interpolation in an ordinary TOML header string:

```toml
[mcp_servers.imap_mail_cloud]
url = "https://mail.example.invalid/imap-mail/mcp"
bearer_token_env_var = "IMAP_MAIL_TOKEN"
startup_timeout_sec = 30
tool_timeout_sec = 150
```

Use the Codex-specific raw token, not the complete `Bearer ...` header value. If
using a raw environment value instead of a network secret, it is visible to code
in the VM and must be handled accordingly. Preserve the plugin's workflow skills
but disable its bundled local MCP server if enabled there, avoiding two toolsets.
Save, finish setup and publish are distinct; only new tasks use the new snapshot.
In a new real cloud task: list tools, `mail_accounts`, then one selected account's
`mail_check(operation="read")`. A successful readiness result proves read access,
not draft APPEND, SMTP or phone-client attachment behavior.

Official current references: [cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments),
[Codex MCP configuration](https://developers.openai.com/codex/mcp).

### Claude Code cloud sessions

Use its private cloud environment's network-secret UI to inject the complete
`Authorization: Bearer <CLAUDE_SPECIFIC_KEY>` header for the exact HTTPS host.
The application config then contains only the endpoint:

```sh
claude mcp add --scope user --transport http imap-mail-cloud \
  https://mail.example.invalid/imap-mail/mcp
```

Run this as environment setup if necessary so it survives new cloud sessions; do
not mistake a local Mac user-scope setting for a cloud configuration. Do not also
add a conflicting fixed Authorization header to `.mcp.json`. Provider network-secret
header injection into native HTTP MCP needs an end-to-end cloud test, not an
assumption from a successful curl request. If native MCP is not injected in the
current runtime, stop and report that exact gap before choosing another adapter.
Disable a duplicate local stdio plugin server there without discarding its skills.
In a new real cloud session, verify account discovery and the same selected
read-only readiness check, without creating/sending a test message.

Official current reference: [Claude cloud environments](https://code.claude.com/docs/en/cloud-environments).

Both secrets authorize the same owner's on-demand email actions, not autonomous
sending. Explicit sends still require the existing account SMTP opt-in and frozen
preview/apply workflow; setup itself authorizes no message.

## Synthetic verification

```sh
PYTHONPATH=bridge /private/service/venv/bin/python \
  -m unittest discover -s bridge/tests -v
```

Tests use a fake stdio backend, never account profiles or mail servers. They cover
SDK initialization, exact tool annotations/results, distinct client principals,
denied unauthenticated/incorrect-host/origin/query requests, size/concurrency bounds,
local-only setup and a backend crash/timeout after dispatch without retry.
Run the ordinary plugin's standard-library suite independently; HTTP dependencies
are intentionally absent from its runtime/package manifests.
