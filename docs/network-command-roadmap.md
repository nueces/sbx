# Networking commands

Use these host-side helpers with an installed version that supports the [gateway](rootless-network-gateway.md). Names default to `[sbx].name` from the nearest project configuration unless supplied explicitly. Start with [sandbox usage](ergonomics.md) if the VM does not exist yet.

## Networking modes

- Without `[network]`, the supported QEMU backend normally uses SLIRP/user networking.
- Adding `[network]` enables the rootless gateway and default-deny outbound policy. Only explicitly configured destinations are allowed; startup failure never falls back to SLIRP.
- Host-to-guest TCP forwarding and OAuth callback tunnels are not outbound authorization rules. Opening a callback tunnel neither authenticates an agent nor supplies a token. See [callback profiles](auth-ports.md).

Keep reproducible gateway destinations and ingress in [policy configuration](rootless-network-gateway.md#configuration-example). Runtime helpers below are operational commands, not a replacement for that policy.

## Foreground forwarding

From the host, forward ports to a running VM until Ctrl-C:

```bash
sbx network forward 3000
sbx network forward 8080:3000
sbx network forward --name the-quest 3000 8080:80
```

The accepted forms are:

```text
GUEST_PORT                   host 127.0.0.1:GUEST_PORT -> guest 127.0.0.1:GUEST_PORT
HOST_PORT:GUEST_PORT          host 127.0.0.1:HOST_PORT -> guest 127.0.0.1:GUEST_PORT
BIND_HOST:HOST_PORT:GUEST_PORT host BIND_HOST:HOST_PORT -> guest 127.0.0.1:GUEST_PORT
```

Binding a public host address exposes the forwarded service beyond localhost. Do so only deliberately, with appropriate service authentication. Ctrl-C closes forwards owned by this foreground command.

Configured forwarding fragment:

```toml
[sbx]
port_forwards = ["3000", "8080:3000"]
```

Configured forwards are applied at VM start; changing the list requires stop/start. Gateway mode normalizes legacy `port_forwards` into ingress rules.

## Status

On the host:

```bash
sbx network status
sbx network status the-quest
sbx network status --json
sbx network status --host-port=53692
```

Status reports VM/network information, callback ownership and gateway status when applicable. Do not assume a fixed guest IP or SSH port: use the running VM's output. A busy/untracked callback port is not owned by sbx and is not proof that authentication will work. See [tunnel ownership and replacement](auth-ports.md#persistent-manual-forwarding).

## Gateway UI

For normal traffic-viewer access, enable the UI in your existing `[network]` table (configuration fragment):

```toml
[network]
mitmweb = true
```

Apply the setting and request the login URL from the host:

```bash
sbx stop
sbx create
sbx network ui
# Or select an explicit sandbox:
sbx network ui the-quest
```

Success prints one authenticated URL and exits 0. Open it manually in your host browser. **The URL contains a login token:** do not share it or redirect it into shared logs. The explicit command prints it even when stdout is redirected; routine status/diagnostics never do.

The command is read-only: it does not enable the UI, start/restart the sandbox, open a browser or probe HTTP. It uses the running gateway even if requested configuration differs. A stopped gateway needs a start; a recovering gateway must become ready. Missing UI files mean disabled **or unavailable**, not proof of either: check the setting, stop/start explicitly, and use diagnostics if it remains unavailable. Operational failures leave stdout empty and exit 1; missing name or invalid config exits 2.

Recovery changes the port/token. Rerun `sbx network ui` for the current URL; replacement detected during lookup fails rather than knowingly returning an old URL. A later replacement can still invalidate a successful lookup. The private `web-auth.json` remains an internal implementation detail. See the [UI reference](rootless-network-gateway.md#optional-read-only-mitmweb-ui) for authentication, read-only enforcement and sensitive metadata.

## Gateway diagnostics

```bash
sbx network diagnostics
sbx network diagnostics the-quest
```

Diagnostics expose redacted gateway configuration/state, failure details and private audit/UI-authentication file paths. They do not print provider values or the UI login token. A stopped or disabled gateway is distinct from a ready one. Review all output before sharing it; raw audit files can contain secrets in URLs/query strings. Follow the [audit contract](rootless-network-gateway.md#host-audit-log-and-guest-denials) and [safe rollback](rootless-network-gateway.md#rollback-checklist), not an automatic retry of an unknown admitted request.

## Persistent callback tunnels

```bash
sbx network auth-port
sbx network auth-port --auth-port=anthropic
sbx network auth-port the-quest --auth-port=openai --replace
sbx network close-auth-port
```

Manual callback tunnels persist independently of attached sessions. Closing them affects sbx-tracked tunnels only, not arbitrary host listeners. Profile defaults, numeric overrides, automatic session forwarding and cleanup rules live in the [OAuth callback reference](auth-ports.md), rather than being duplicated here.
