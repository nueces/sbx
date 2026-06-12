# UV package auth mediation without guest credentials

## Goal

Allow commands such as `uv sync` to install packages from authenticated Python package indexes, especially AWS CodeArtifact, inside an `sbx` VM without storing real package credentials, AWS credentials, or `uv` auth tokens in the guest.

The preferred design keeps the project's package index URL unchanged. The guest should still use the original configured index URL, while the host supplies authentication at request time.

## Non-goals

- Do not copy host AWS credentials, CodeArtifact tokens, `uv` credentials, or keyring entries into the VM.
- Do not require changing `UV_INDEX_URL` / `UV_EXTRA_INDEX_URL` to point at a local reverse proxy.
- Do not build a generic unrestricted web proxy.
- Do not support arbitrary credential formats in the first implementation.
- Do not weaken the existing `copy_host_credentials = false` default.

## Prior art

### Current SmolVM behavior used by `sbx`

SmolVM presets usually solve authentication by copying host credential files and selected environment variables into the guest. This works for convenience but does not satisfy this feature's security goal.

`sbx` already avoids this by default when `copy_host_credentials = false`: it applies SmolVM presets with a credential-free temporary `HOME`, then selectively installs safe configuration such as Git identity.

SmolVM provides useful port-forwarding and SSH primitives, but it does not currently provide host-side HTTP credential injection for package indexes.

### Gondolin model

Gondolin uses host-side HTTP/TLS mediation:

1. Guest sends normal HTTP(S) traffic to the original destination.
2. Host intercepts and parses the request.
3. Host applies policy and optional hooks.
4. Host substitutes placeholder secrets with real host secrets only for allowed destinations.
5. Host sends the upstream request.

The real secret never appears in the guest. This feature should borrow that model, but implement the smallest package-index-specific subset needed by `sbx`.

## Scope decisions

Initial implementation target:

- Keep `UV_INDEX_URL` unchanged.
- Use session-scoped `HTTPS_PROXY` and `HTTP_PROXY` for the minimal useful v1. This is a cooperative mechanism: tools that honor proxy environment variables are mediated; processes that ignore them can bypass the mediator.
- Use Gondolin-style placeholder credentials in the guest. The guest may contain a fake high-entropy `uv` credential, but never the real token.
- Implement one generic Basic auth replacement rule for v1, initially backed by an AWS CodeArtifact token provider.
- Support one repository/index for v1.
- Automatically install the sbx-managed mitmproxy CA certificate into the guest when the session proxy is enabled, including package-auth proxy mode and standalone proxy mode.
- Use hostname/path-scoped auth mediation: the configured package host/path gets MITM and auth replacement; unrelated HTTP/HTTPS may flow through the proxy because of proxy env vars, but must never receive injected auth. V1 makes unrelated proxy traffic policy configurable per scheme.
- Integrate first with `sbx run` / `sbx shell`; later add standalone `sbx network package-auth` and `sbx network close-package-auth` commands.
- Target credential non-exposure for cooperative package tools in v1, not package-host bypass prevention or full Gondolin-style network egress control.

Future architecture note:

The v1 credential non-exposure feature should be structured as a narrow instance of a future host-side network mediation system. Later, when there is time, we may expand this into Gondolin-level network policy: broader HTTP/TLS mediation, allowlists, request/response hooks, multiple secret types, stronger egress controls, and multiple package/provider rules. The v1 implementation should avoid hardcoding CodeArtifact or `uv` assumptions so deeply that this future expansion becomes difficult.

The SSH remote-forward tunnel is the minimal v1 transport for exposing the host mediator to the guest. A later architecture phase may replace it with a custom host↔guest stream bridge, a vsock-based bridge, or another first-class control-plane transport to avoid depending on SSH port forwarding.

A useful long-term abstraction is:

```text
traffic interceptor
  -> request classifier
  -> auth/policy rule matcher
  -> secret/token provider
  -> request rewriter
  -> upstream forwarder
```

For v1, this abstraction has one rule:

```text
match configured host + path prefix
replace Basic auth placeholder password
real credential provider = AWS CodeArtifact token provider
```

## Recommended design

Use session-scoped proxy environment variables, a constrained host-side `mitmproxy` mediator, and placeholder credentials.

Foundational v1 routing choice:

- Do not change `UV_INDEX_URL`.
- Start host `mitmdump` on a localhost random/session-scoped port.
- Expose the host `mitmdump` listener to the selected VM/session through an `sbx`-managed SSH remote-forward tunnel.
- Set both `HTTPS_PROXY` and `HTTP_PROXY` only in the attached `sbx run` / `sbx shell` session so cooperative tools use the mediator.
- Own `NO_PROXY` for the attached session instead of preserving user values. Use a known safe default such as `localhost,127.0.0.1,::1` so the configured package host cannot accidentally bypass the mediator.
- Do not enforce network-level bypass prevention in v1. If a process ignores the proxy environment, it can connect directly to the package host without mediation.
- Defer firewall bypass prevention, transparent no-proxy routing, `/etc/hosts` override, and Gondolin-style userspace network mediation to later phases.

Selected implementation components:

- Proxy path: session-scoped `HTTPS_PROXY` and `HTTP_PROXY` pointing at the guest side of an `sbx`-managed SSH remote-forward tunnel to host `mitmdump`.
- TLS/HTTP mediator: `mitmproxy` / `mitmdump` running on the host with an `sbx` addon.
- Dependency model: `mitmproxy` is a normal, non-optional Python dependency of `sbx` so package auth works out of the box when `sbx` is installed. `sbx` should still validate that `mitmdump` can be launched and emit a clear diagnostic if the environment is broken.
- CA/cert handling: prefer `mitmproxy`'s mature CA/TLS machinery, with `sbx` managing where CA material is stored and which CA certificate is installed into the guest whenever the session proxy is enabled.
- Auth rewrite logic: small `sbx` mitmproxy addon that validates host/path, checks the Basic auth placeholder, obtains the host-side token, and rewrites the Authorization header. Replacement fails closed unless the request contains the exact expected Basic auth placeholder.
- Credential source: use host AWS CLI for the built-in CodeArtifact provider; also support an advanced host command provider that prints the Basic auth password/token to stdout.

Rationale for `mitmproxy`:

- It is a mature open-source HTTPS MITM stack.
- It handles TLS, certificate generation, HTTP parsing, streaming, and many protocol edge cases that would be risky to reimplement in `sbx`.
- The `sbx`-specific surface can remain small: lifecycle management, guest routing, token acquisition, and a restrictive addon.

Required `mitmproxy` constraints:

- Run only a headless `mitmdump`-style process; no web UI.
- Bind to localhost on a random/session-scoped port.
- Expose only to the selected VM/session through the managed SSH remote-forward tunnel.
- Disable or avoid verbose flow logging that could include Authorization headers.
- Enforce host/path policy in the addon; do not rely on mitmproxy defaults for security.
- Use an `sbx`-controlled mitmproxy config directory, not the user's default mitmproxy state.
- Clean up the process and tunnel on session exit.

The VM keeps using the original package index URL, for example:

```text
https://DOMAIN-OWNER.d.codeartifact.REGION.amazonaws.com/pypi/REPOSITORY/simple/
```

Inside the VM, `uv` is configured with a fake placeholder credential for that index. For CodeArtifact, the username is normally `aws`, and the password is a high-entropy placeholder generated by `sbx`.

When `uv sync` runs:

```text
uv in VM
  -> original CodeArtifact URL
  -> uv honors session HTTPS_PROXY and connects to the guest-side proxy endpoint
  -> sbx-managed tunnel carries the proxy connection to host mitmdump
  -> host mediator handles HTTPS proxy CONNECT and decrypts request using the sbx-managed mitmproxy CA
  -> host verifies destination matches configured package host/path
  -> host replaces Basic auth placeholder password with fresh CodeArtifact token
  -> host forwards request to real CodeArtifact upstream
  -> response streams back to uv

If a guest process ignores `HTTPS_PROXY` and connects directly to the configured package host, v1 does not intercept or block that direct path. Enforced bypass prevention is deferred to a later phase.
```

The real CodeArtifact token stays on the host. The guest only sees a placeholder token.

Session mode behavior:

| Mode | Starts proxy | Installs CA | Sets `HTTP_PROXY` / `HTTPS_PROXY` | Sets uv placeholder vars | Rewrites package auth |
| --- | --- | --- | --- | --- | --- |
| Package auth (`[sbx.package_auth].enabled = true`) | yes | yes | yes | yes | yes |
| Standalone proxy (`[sbx.network.proxy].enabled = true`, package auth disabled) | yes | yes | yes | no | no |
| Neither enabled | no | no | no | no | no |

## Why placeholder credentials instead of fully transparent injection?

Fully transparent injection means the host adds an `Authorization` header to matching requests even if the guest sent no auth. That is convenient, but it grants any guest process authenticated access to the configured package repository simply by making requests to the matching host/path.

Placeholder credentials provide a clearer authorization signal:

- the guest must intentionally use the configured placeholder credential;
- the host only replaces that exact high-entropy placeholder;
- requests that do not contain the placeholder are not authenticated;
- accidental authentication of unrelated traffic is less likely.

A later phase can add fully transparent injection if needed.

## User-facing behavior

A possible configuration shape:

```toml
[sbx.package_auth]
enabled = true
mode = "basic-placeholder-mitm"

# Basic auth replacement rule. One rule/index for v1.
host = "my-domain-123456789012.d.codeartifact.us-east-1.amazonaws.com"
# For CodeArtifact, use the repository root prefix rather than only /simple/
# so both package metadata and artifact downloads remain in scope.
# For provider = "aws-codeartifact", v1 expects this to match /pypi/<repository>/.
path_prefix = "/pypi/my-repo/"
username = "aws"

# V1 always configures uv placeholder auth using session-scoped named-index
# uv env vars. uv_index_name is required.
uv_index_name = "private"

[sbx.package_auth.credential]
provider = "aws-codeartifact"
domain = "my-domain"
domain_owner = "123456789012"
region = "us-east-1"
repository = "my-repo"

# Optional AWS profile. If omitted, use the host default credential chain.
profile = "default"

# Session proxy policy. Package-auth replacement has priority over these
# defaults/host rules. When package auth is enabled, omitted enabled defaults
# to true; explicitly setting enabled = false is a configuration error.
# When package auth is disabled, set enabled = true to start a standalone
# session proxy without package credential replacement.
[sbx.network.proxy]
http = "block"
https = "allow"

# Optional origin-specific overrides for unrelated proxied traffic.
# Keys are exact origins: scheme://host[:port]. Values are allow or block.
[sbx.network.proxy.hosts]
"https://api.anthropic.com" = "allow"
"https://api.openai.com" = "allow"
```

Advanced command-token configuration:

```toml
[sbx.package_auth.credential]
provider = "command"
command = [
  "aws",
  "codeartifact",
  "get-authorization-token",
  "--domain", "my-domain",
  "--domain-owner", "123456789012",
  "--region", "us-east-1",
  "--query", "authorizationToken",
  "--output", "text",
]
```

Possible CLI overrides:

```bash
sbx run --package-auth
sbx shell --package-auth
sbx run --network-proxy
sbx shell --network-proxy
sbx network package-auth NAME
sbx network close-package-auth NAME
sbx network proxy NAME
sbx network close-proxy NAME
sbx network status NAME
```

Command naming decision for v1: config-driven `sbx run` / `sbx shell` integration is required first. CLI overrides such as `--package-auth` and `--network-proxy`, plus standalone `sbx network package-auth` / `proxy` commands, are optional follow-ups; if added, prefer `package-auth` over `auth-proxy` because existing `auth-port` means OAuth callback forwarding.

## Host components

### 1. Credential provider

The host credential provider obtains and refreshes the Basic auth password/token that replaces the guest placeholder.

Built-in v1 provider:

- `provider = "aws-codeartifact"` shells out to the host AWS CLI:
  ```bash
  aws codeartifact get-authorization-token ... --output json
  ```
- The built-in provider should not use `--query authorizationToken --output text`, because it needs both `authorizationToken` and `expiration` for safe in-memory caching and refresh.
- The host AWS CLI must be installed and authenticated for the selected profile/region.
- `sbx` should preflight this when package auth is enabled and report a clear recovery message if unavailable.
- For `provider = "aws-codeartifact"`, the recommended and expected v1 `path_prefix` is `/pypi/<repository>/`, not only `/pypi/<repository>/simple/`, because package file downloads may use repository paths outside `/simple/`.
- For `provider = "aws-codeartifact"`, v1 should validate that `path_prefix` equals `/pypi/<repository>/`. If current or future CodeArtifact behavior requires a different prefix, support that through an explicit advanced override rather than silently widening the auth scope.
- For `provider = "command"`, `path_prefix` cannot be derived by `sbx`; it is treated as an explicit advanced trust boundary. V1 requires it to be a non-root absolute path prefix ending in `/`, with no `..`, query, or fragment. Users should choose the narrowest repository root prefix that covers metadata and package artifacts.

Advanced v1 provider:

- `provider = "command"` runs a configured host command that prints the Basic auth password/token to stdout.
- Prefer argv-array command config over shell strings to avoid quoting and shell-injection issues:
  ```toml
  [sbx.package_auth.credential]
  provider = "command"
  command = [
    "aws",
    "codeartifact",
    "get-authorization-token",
    "--domain", "my-domain",
    "--domain-owner", "123456789012",
    "--region", "us-east-1",
    "--query", "authorizationToken",
    "--output", "text",
  ]
  ```
- The command runs on the host, not in the VM.
- Command stdout is trimmed, held in memory only, never logged, and rejected if empty.
- Command stderr may be included in diagnostics only after redacting obvious secret material.

The credential provider should cache tokens in memory only. For CodeArtifact, parse AWS CLI JSON output that includes both `authorizationToken` and `expiration`, then refresh before expiry. For `provider = "command"`, v1 runs the command on demand and does not cache unless a later config field adds an explicit TTL.

### 2. MITM proxy / HTTP mediator

The host process accepts tunneled explicit-proxy traffic from the guest, handles HTTPS `CONNECT`, terminates TLS for mediated requests, applies request policy, optionally substitutes package placeholder credentials, and forwards upstream.

V1 supports two session proxy modes:

- Package-auth mode: enabled by `[sbx.package_auth].enabled = true`; starts the session proxy, injects proxy env vars, applies network proxy policy, and enables package credential replacement.
- Standalone proxy mode: enabled by `[sbx.network.proxy].enabled = true` when package auth is disabled; starts the session proxy, injects proxy env vars, and applies network proxy policy without package credential replacement.

Minimum v1 requirements:

- Run `mitmdump` in regular explicit proxy mode.
- Apply proxy policy in this priority order:
  1. Configured package host + HTTPS + configured `path_prefix`: action `replace`, requiring exact Basic auth placeholder.
  2. Configured package host + HTTPS + wrong path: block.
  3. Configured package host + HTTP: block.
  4. `[sbx.network.proxy.hosts]` exact origin override: allow/block.
  5. `[sbx.network.proxy]` scheme default: allow/block.
- Package-host rules always take priority over general network proxy allow/block rules.
- Support HTTPS requests to the configured package index host.
- Support HTTP/1.1 sufficiently for `uv` package metadata and wheel/sdist downloads.
- Preserve methods, paths, headers, and streaming response bodies.
- Replace only Basic auth password matching the generated placeholder.
- For configured host + configured path prefix: MITM and placeholder replacement.
- For configured host + wrong path: fail closed for v1.
- For unrelated HTTP/HTTPS traffic that passes through the proxy because of session proxy env vars: follow `[sbx.network.proxy]` and `[sbx.network.proxy.hosts]`.
  - `enabled = true | false`; when package auth is enabled, omitted means `true`, while explicit `false` is a configuration error.
  - `http = "allow" | "block"`; default `block`.
  - `https = "allow" | "block"`; default `allow`.
  - `hosts` keys are origin-style entries such as `"https://api.anthropic.com" = "allow"`.
  - `allow` means allow normal mitmproxy explicit-proxy behavior but never inject auth. For v1, allowed HTTPS traffic uses normal mitmproxy behavior and may be TLS-intercepted after the sbx-managed CA is installed; Authorization headers and flow bodies must not be logged. A later hardening/privacy phase may add TLS passthrough for non-package hosts.
  - `block` means return a clear proxy error for matching traffic. Use HTTP `403 Forbidden` with a short secret-free message where mitmproxy can generate an HTTP response, including CONNECT/origin blocks and post-MITM package path/auth blocks.
- Package auth replacement itself is only supported for HTTPS package-index requests in v1. Plain HTTP requests to the configured package host should not receive auth injection.
- For configured package host/path, fail closed unless the request contains the exact expected Basic auth placeholder. Missing Authorization, Bearer auth, other auth schemes, or Basic auth with any password other than the placeholder are blocked rather than rewritten. This prevents silently masking real credentials stored in the guest.
- Never log real tokens or Authorization headers.

### 3. CA management

To inspect HTTPS, v1 lets mitmproxy generate and manage its CA inside an `sbx`-controlled mitmproxy config directory. `sbx` owns the location/lifecycle and installs the resulting mitmproxy CA certificate into the guest trust store.

Recommended host layout:

```text
~/.local/state/sbx/package-auth/mitmproxy/          # shared CA/config per host user
~/.local/state/sbx/package-auth/sessions/<vm-id>/  # per-VM/session runtime state
```

Key points:

- The mitmproxy CA private key stays on the host.
- The guest receives only the CA certificate.
- The CA may be shared across package-auth sessions for the same host user.
- Runtime state must be isolated per VM/session: placeholder value, mitmdump listen port, tunnel, package auth config, token cache, addon config, PID, and logs.
- Host-side state files containing placeholders, provider configuration, process metadata, or logs must be created with owner-only permissions where possible. The real token must not be written to state files.
- Guest CA installation must work for common Linux trust stores used by Python/uv.

V1 CA install decision: install the CA into the guest system trust store for supported Linux images. For Debian-like images, copy the certificate to `/usr/local/share/ca-certificates/sbx-mitmproxy-ca.crt` and run `update-ca-certificates`. Do not set `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` by default unless Phase 0 validation shows `uv` does not honor the system trust store in a target image.

### 4. Guest proxy wiring

Because `UV_INDEX_URL` must not change, v1 uses proxy environment variables rather than package index URL rewriting.

Selected foundational approach:

- Expose host `mitmdump` to the guest through an `sbx`-managed SSH remote-forward tunnel:
  ```text
  guest 127.0.0.1:<guest_proxy_port> -> host 127.0.0.1:<host_mitmdump_port>
  ```
- Set session proxy env vars to the guest side of that tunnel:
  ```bash
  HTTPS_PROXY=http://127.0.0.1:<guest_proxy_port>
  HTTP_PROXY=http://127.0.0.1:<guest_proxy_port>
  ```
- Set both `HTTPS_PROXY` and `HTTP_PROXY` only in the attached process environment.
  - `sbx shell` injects proxy variables into the shell process.
  - `sbx run` injects proxy variables into the launched agent process.
  - Child processes inherit these variables according to normal process environment behavior.
- Do not write proxy variables to shell profiles, `/etc/environment`, or other persistent guest files in v1.
- Set a conservative `NO_PROXY` for loopback destinations, for example `localhost,127.0.0.1,::1`.
- Do not preserve existing `NO_PROXY` values in v1, because broad entries such as `*`, `.amazonaws.com`, or the configured package host could bypass package-auth mediation.
- Do not modify `UV_INDEX_URL` / `UV_EXTRA_INDEX_URL`.

This keeps package index URLs unchanged while avoiding the complexity of firewall enforcement, `/etc/hosts` override, guest port 443 routing, or full transparent interception in v1.

V1 is cooperative: tools that honor proxy environment variables are mediated. Processes that ignore those environment variables can bypass the mediator. Enforced bypass prevention is deferred to a later hardening phase.

### 5. Guest uv placeholder configuration

V1 always configures `uv` placeholder auth automatically using session-scoped `uv` environment variables. This keeps the placeholder credential process-scoped like the proxy environment and avoids writing credential files or modifying project files. There is no `configure_uv` opt-out in v1; package auth requires a named uv index so `sbx` can inject placeholder credentials consistently.

Preferred v1 mechanism:

- Configure named-index `uv` environment variables in the attached process environment, for example:
  ```bash
  UV_INDEX_<NORMALIZED_NAME>_USERNAME=aws
  UV_INDEX_<NORMALIZED_NAME>_PASSWORD=<placeholder>
  ```
- Normalize `uv_index_name` using uv's documented environment-variable rule: uppercase the index name and replace every non-alphanumeric character with `_`. For example, `internal-proxy`, `internal_proxy`, and `internal.proxy` all map to `INTERNAL_PROXY`.
- Validate that `uv_index_name` contains only ASCII alphanumeric characters, `.`, `_`, and `-`. If the name cannot be represented using uv's documented env-var scheme, fail fast with a clear configuration error.
- Note: different uv index names can collide after normalization, for example `private-index`, `private_index`, and `private.index` all map to `PRIVATE_INDEX`. This is not a v1 blocker because v1 supports one package-auth index, but future multi-index support must detect and reject collisions.
- Preliminary validation with `uv 0.11.20` confirmed that `private`, `private-index`, `private_index`, and `private.index` all use the documented env-var normalization and cause uv to send `Authorization: Basic base64(username:password)` for named-index requests when username/password env vars are set.

CodeArtifact path-prefix documentation note:

- AWS CodeArtifact pip documentation configures pip with an index URL of the form `https://aws:$CODEARTIFACT_AUTH_TOKEN@<domain-owner>.d.codeartifact.<region>.amazonaws.com/pypi/<repository>/simple/` and states that `aws codeartifact login --tool pip --repository <repository>` sets pip's `index-url` for that repository.
- This supports using `/pypi/<repository>/` as the package-auth trust boundary rather than only `/pypi/<repository>/simple/`.
- AWS documentation found during planning does not explicitly specify every wheel/sdist asset URL or redirect target. Therefore, v1 should keep the `/pypi/<repository>/` validation rule. If future real traffic shows artifact URLs or redirects outside `/pypi/<repository>/`, require an explicit advanced override rather than silently widening auth scope.
- Live validation against a CodeArtifact PyPI repository using `pip download` through mitmproxy observed metadata and wheel requests for a package and dependencies. All observed requests stayed on the CodeArtifact host and under `/pypi/<repository>/`; no requests to other hosts and no requests outside the prefix were observed.
- Require package-auth config to identify the `uv` index name.
- Automatic uv placeholder configuration requires the project's private package index to be a named uv index, for example:
  ```toml
  [[tool.uv.index]]
  name = "private"
  url = "https://my-domain-123456789012.d.codeartifact.us-east-1.amazonaws.com/pypi/my-repo/simple/"
  ```
- `sbx` does not mutate project uv config to create this named index in v1.
- `sbx shell` injects these variables into the shell process.
- `sbx run` injects these variables into the launched agent process.
- When package auth is enabled, `sbx` also sets `UV_KEYRING_PROVIDER=disabled` in the attached process environment. This reduces the chance that `uv` consults guest keyring credentials instead of the sbx placeholder. The proxy still fails closed if `uv` sends any Authorization header other than the exact expected placeholder.
- Child processes inherit these variables according to normal process environment behavior.
- Do not write placeholder credentials to project files, `uv` auth stores, `.netrc`, shell profiles, or system environment files in v1.
- Do not attempt to rewrite or delete guest `.netrc` or other existing guest auth sources in v1; the exact-placeholder proxy check is the safety control.

Phase 0 must validate the exact current `uv` environment variable names and behavior. If the project uses an index that cannot be targeted by named-index env vars, v1 requires the user to name the index in project uv config before enabling package auth.

## Security model

Protected:

- Host AWS credentials do not enter the guest.
- Real CodeArtifact auth token does not enter the guest.
- Real token is not persisted in guest disk, project files, or `uv` config.
- Host only substitutes placeholder credentials for configured destinations.

Not protected:

- The guest can use the authenticated repository access intentionally exposed by this feature.
- A malicious guest process may download any package allowed by the configured CodeArtifact repository unless package-name/path allowlisting is added later.
- If the guest can read the placeholder config, it can trigger substitution for allowed destinations while the proxy is active.

Required controls:

- Bind host mediator to localhost.
- Expose it only to the selected VM/session through the managed tunnel.
- Set proxy environment variables only for the attached process environment; do not persist them in guest profile or system environment files.
- Own `NO_PROXY` for the attached process environment and avoid preserving user-provided entries that could bypass the configured package host.
- Document that v1 does not enforce network-level bypass prevention; direct connections that ignore proxy env vars are out of scope.
- Scope substitution to exact configured package host and path prefix.
- Replace only the generated high-entropy placeholder.
- Block configured package host/path requests that do not contain the exact expected Basic auth placeholder.
- Fail closed for configured host + wrong path in v1.
- Never inject auth outside the configured rule.
- Apply the configured network proxy policy (`[sbx.network.proxy]`) for proxied HTTP/HTTPS traffic to non-package hosts.
- Ensure package-auth host/path rules take priority over `[sbx.network.proxy.hosts]` and scheme defaults, so broad allow rules cannot override package-host wrong-path or HTTP blocking.
- When package auth is enabled, treat omitted `[sbx.network.proxy].enabled` as `true`; fail fast with a configuration error if it is explicitly `false`.
- When package auth is disabled, start a standalone session proxy only if `[sbx.network.proxy].enabled = true`; no package credential replacement occurs in that mode.
- Do not log secrets.
- Clean up mediator/tunnel/session runtime state on exit.
- Leave the installed guest CA certificate in place by default. Proxy and uv placeholder environment variables are process-scoped and disappear with the attached process; no uv placeholder files should exist to clean up.

## Implementation phases

### Phase 0: validation and design spike

Scope:

- Confirm current `uv` named-index environment variable behavior for username/password placeholder auth.
- Confirm how `uv` sends credentials for CodeArtifact indexes.
- Confirm `uv sync` honors session-scoped `HTTPS_PROXY` while keeping `UV_INDEX_URL` unchanged.
- Confirm the session-owned `NO_PROXY` value does not bypass the configured package host.
- Validate the guest-side proxy endpoint plus `sbx`-managed SSH remote-forward tunnel to host `mitmdump`.
- Confirm which CA trust path `uv` uses in the target guest image.
- Prototype manually outside `sbx` if needed.

Deliverables:

- Documented `uv` credential mechanism for placeholder Basic auth.
- Validation of session-scoped named-index `uv` environment variables as the v1 placeholder credential mechanism.
- Decision on guest CA installation method.
- Proof that proxy env + tunnel can route `uv sync` traffic to the host mediator without changing `UV_INDEX_URL`.

Exit criteria:

- A manual `uv sync` can reach an HTTPS MITM mediator while keeping the original index URL.
- The mediator can observe the Authorization header that contains the placeholder.
- The v1 limitation is documented: direct guest connections that ignore proxy env vars are not mediated.

### Phase 1: explicit host mediator prototype, manually wired

Scope:

- Add or prototype a host-side Basic auth placeholder mediator process.
- Implement AWS CodeArtifact credential provider by shelling out to AWS CLI.
- Mediator supports one configured package host/path prefix.
- Manual guest setup is acceptable: install CA, set proxy env vars for the test shell, set up tunnel, and configure uv placeholder auth.
- Use host `mitmdump` in regular explicit proxy mode with an `sbx` addon as the planned runtime mediator.

Out of scope:

- Automatic lifecycle integration with `sbx run`.
- Multiple concurrent VMs.
- Full CLI/config UX.
- Firewall bypass prevention.
- Transparent no-proxy routing.

Exit criteria:

- From inside a VM, `uv sync` succeeds against CodeArtifact without changing `UV_INDEX_URL`.
- No real CodeArtifact token appears in guest files/env/process config.
- Requests without placeholder auth are not upgraded to real auth.
- Configured host + wrong path fails closed.
- The cooperative proxy-only limitation is documented.

### Phase 2: sbx-managed lifecycle for one VM/session

Scope:

- Add `[sbx.package_auth]` config parsing.
- Add `[sbx.network.proxy]` config parsing for both package-auth proxy mode and standalone session proxy mode.
- Start/stop the host mediator from `sbx run` / `sbx shell` when package auth is enabled or standalone network proxy is enabled.
- Track mediator and tunnel state in `~/.local/state/sbx`.
- Install the sbx-managed mitmproxy CA certificate into guest when the session proxy is enabled, including standalone proxy mode.
- Install managed SSH remote-forward tunnel and session-scoped proxy environment.
- Configure guest uv placeholder auth via session-scoped named-index environment variables only when package auth is enabled.
- Apply `[sbx.network.proxy]` allow/block policy in both modes.
- Show proxy/package-auth state in `sbx network status`.

Out of scope:

- Multiple package providers.
- Multiple package repositories in one session.
- Standalone `sbx network proxy` / `close-proxy` commands, unless added after the `run`/`shell` lifecycle path.
- Firewall bypass prevention.
- Transparent no-proxy routing.
- Full Gondolin-style network egress policy.

Exit criteria:

- User can enable package auth with config and run `sbx shell` or `sbx run`.
- User can enable standalone session proxy with `[sbx.network.proxy].enabled = true` and run `sbx shell` or `sbx run`.
- `uv sync` works without changing `UV_INDEX_URL` when package auth is enabled.
- Cleanup works when the session exits; standalone network cleanup commands may be added later.

### Phase 3: hardening and multi-session support

Scope:

- Robust token refresh and retry behavior.
- Concurrent VMs/sessions with isolated placeholders and ports.
- Better diagnostics for CA/proxy/auth failures.
- Request/path policy tightening.
- Tests for lifecycle, config validation, and failure modes.
- Secret-safe logging review.

Exit criteria:

- Multiple sandboxes can use package auth independently.
- Stale processes/tunnels are detected and cleaned up.
- Common failure states produce actionable messages.

### Phase 4: usability expansion

Scope options:

- Support additional providers, for example Artifactory, Nexus, GitHub Packages, private PyPI with static host-side secret refs.
- Support multiple indexes/repositories.
- Add package name allowlists.
- Add fully transparent injection mode for users who do not want guest placeholder uv config.
- Add transparent no-proxy routing using hosts/DNS override, guest redirect rules, or a broader network mediation layer.
- Add broader Gondolin-style network mediation and egress policy.

Exit criteria:

- The feature generalizes beyond one CodeArtifact repository without weakening the default security model.

## Decisions made

- Keep `UV_INDEX_URL` unchanged.
- Use session-scoped `HTTPS_PROXY` / `HTTP_PROXY` for the minimal useful v1.
- Do not enforce the proxy path at the network layer in v1; processes can bypass mediation by ignoring proxy environment variables.
- Use Gondolin-style placeholder Basic auth credentials in the guest.
- Implement one generic Basic auth replacement rule for v1.
- Use AWS CodeArtifact as the first credential provider.
- Support one repository/index for v1.
- Automatically install the sbx-managed mitmproxy CA certificate in the guest when the session proxy is enabled, including standalone proxy mode.
- Use host/path-scoped auth mediation: configured package host/path gets MITM and auth replacement; configured host + wrong path fails closed; auth is never injected elsewhere.
- Make unrelated proxied HTTP/HTTPS behavior configurable under `[sbx.network.proxy]`, defaulting to `http = "block"` and `https = "allow"`, with optional origin-specific host overrides.
- Add `[sbx.network.proxy].enabled`; when package auth is enabled, omitted means enabled and explicit `false` is a configuration error. When package auth is disabled, explicit `[sbx.network.proxy].enabled = true` starts a standalone session proxy without package credential replacement.
- Start with `sbx run` / `sbx shell` integration for package auth and standalone session proxy; add standalone network commands later.
- Target credential non-exposure for cooperative package tools now; defer firewall bypass prevention, transparent no-proxy routing, and full Gondolin-style egress policy to later architecture phases.
- Use constrained host-side `mitmproxy` / `mitmdump` as the planned runtime mediator, with an `sbx` addon enforcing package-auth policy.
- Add `mitmproxy` as a normal non-optional Python dependency so the feature works out of the box.
- Use host AWS CLI for the built-in CodeArtifact token provider in v1, parsing JSON output with both `authorizationToken` and `expiration`.
- Also support `provider = "command"` so advanced users can run a host command that prints the token/password to stdout; command-provider `path_prefix` is user-supplied and must pass safe non-root path-prefix validation.

## Remaining technical questions

No pre-implementation blocker questions remain. The implementation should still keep validation tests/checklists for uv behavior and CodeArtifact path scope so regressions or provider behavior changes are caught before release.
