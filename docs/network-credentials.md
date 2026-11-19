# Network credentials

Network credentials let guest tools use familiar environment variables without receiving the real credential. The guest receives a high-entropy opaque placeholder. The host gateway substitutes the real value only in an authorized HTTPS authentication header sent to a configured service.

Start with the [full GitHub and CodeArtifact example](#full-example), then the [guest commands](#using-the-credentials-in-the-guest). For general networking, ingress, diagnostics, and auditing, see the [gateway guide](rootless-network-gateway.md).

## Supported today

| Capability | Support / example |
| --- | --- |
| Bearer tokens, token authentication, custom API-key headers | Exact [header templates](#bindings), such as `Authorization: Bearer {placeholder}` or `X-API-Key: {placeholder}`. |
| HTTP Basic authentication | Replace the password while validating the username; the shipped [CodeArtifact provider](#full-example) supplies this descriptor. |
| Credential sources | Host commands returning [text](#text-output) or [JSON with expiry](#json-output), the shipped AWS CodeArtifact plugin, or explicitly installed trusted [plugins](#plugin-methods). No 1Password plugin ships with sbx; use the `op` command example. |
| One credential, multiple policies | List policy IDs in one binding; use [separate bindings for different headers](#one-credential-different-headers). |
| Multiple credentials on one request | [Separate credentials on distinct header names](#two-credentials-on-one-request); every matching binding must be satisfied. |
| Narrower credential scope | Restrict a binding's methods and paths within its policies' permissions. Credential-bound policies require exact DNS hosts and HTTPS. |
| Direct IPv4 networking without host credentials | Explicit [IP policies](#direct-ipv4-policies) for raw TCP or mediated HTTP/HTTPS; DNS policies do not grant direct access to their resolved IPs. |
| Fetch timing | [`fetch = "lazy"` (default) or `"startup"`](#fetching-at-startup-for-authentication). Startup fetch failure aborts startup. |
| Cache and refresh | [Per-credential in-memory caching and expiry refresh](#retrieval-and-refresh); the cache survives worker/mediator recovery, not a new supervisor session. |

**Not supported:** host-side HTTP request signing (HMAC, AWS SigV4), arbitrary body/query substitution, automatic authentication-header insertion, host-managed mTLS client keys, or credential injection into IP-literal policies, plain HTTP, TLS passthrough, or raw TCP. Terminal password prompts are not supported; [startup fetching](#fetching-at-startup-for-authentication) can use compatible host GUI authentication or an existing host sign-in.

`fetch` controls **when to retrieve a value**, not how to authenticate a request. Signing with a placeholder and replacing it later cannot produce a valid request signature. A request-aware host signer would be a separate feature; credential-provider plugins only retrieve values. An already issued bearer JWT can be injected like any other token; this does not sign the surrounding HTTP request. The gateway's internal HMAC worker-readiness proof authenticates its own components and is not an outbound API signer.

## How a request works

This is the successful path for a credential-bound HTTPS request. Invalid policy, scope, or placeholder headers deny the request rather than cause a secret lookup. Provider failure without a valid cached value prevents forwarding; audit failure stops mediation. See [audit guarantees and unknown outcomes](rootless-network-gateway.md#host-audit-log-and-guest-denials) before retrying a failed operation.

```mermaid
sequenceDiagram
    box Guest VM
        participant App as Guest tool
    end
    box Host
        participant Gateway as Gateway worker / mediator
        participant Broker as Supervisor credential broker
        participant Provider as Host provider helper
        participant Audit as Supervisor audit log
    end
    participant API as Authorized remote API

    Note over Gateway,Provider: Setup validates policy, descriptions, and bindings
    opt fetch = startup, before networking starts
        Broker->>Provider: Fetch configured bound credential
        Provider-->>Broker: Value and expiry, cached in memory
    end
    Note over App: Guest environment receives only an opaque placeholder
    App->>Gateway: HTTPS request with placeholder in expected header
    Gateway->>Gateway: Validate destination, policy, binding, and header
    Gateway->>Broker: Look up credential ID
    opt Cache missing or refresh due
        Broker->>Provider: Fetch credential
        Provider-->>Broker: Value and expiry
    end
    Broker-->>Gateway: Credential value for this request
    Gateway->>Gateway: Revalidate scope and substitute header
    Gateway->>Audit: Append admission and wait for durable ACK
    Audit-->>Gateway: ACK after flush and sync
    Gateway->>API: Forward request with real authentication header
    API-->>Gateway: Response
    Gateway-->>App: Response
    Gateway->>Audit: Terminal outcome, asynchronous
```

In short: **guest placeholder → policy and binding checks → host cache/provider → header substitution → durable admission ACK → remote API**. Startup fetch populates the same cache earlier; it does not bypass request checks. Descriptions retrieve no credential, and unbound credentials are never fetched.

Only bind a credential to services you trust: the authorized service receives the real value and could reflect it in a response. sbx does not make a malicious authorized service safe. Never put real credentials in config, command arguments, URLs, or query strings; private audit/UI metadata can retain sensitive URL content.

## Configuration model

Configuration has three parts:

1. `[[network.policy]]` authorizes destinations and protocol behavior.
2. `[[network.credential]]` defines how one credential is retrieved or generated.
3. `[[network.credential.bindings]]` connects that credential to one or more policy IDs.

Credential options and bindings are nested under their credential, so their relationship is visible. A credential may have multiple bindings, and one binding may reference multiple policies. In the full example, the credential ID and the referenced policy IDs are deliberately different:

```mermaid
flowchart LR
    C["Credential: github<br/>host op command"] --> B["Its binding<br/>Authorization: Bearer placeholder"]
    B -->|policies includes| P1["Policy: github-api<br/>api.github.com:443"]
    B -->|policies includes| P2["Policy: github-uploads<br/>uploads.github.com:443"]
```

## Network policies

All destinations use one policy table with an explicit `mode` discriminator:

```toml
[[network.policy]]
id = "example-api"
mode = "http-mediated"
hosts = ["api.example.com"]
ports = [443]
methods = ["GET", "POST"]
path_prefixes = ["/v1/"]
```

Opaque TLS and raw TCP use the same policy shape:

```toml
# Any TLS-wrapped application protocol, authorized through exact SNI.
[[network.policy]]
id = "private-ldaps"
mode = "tls-passthrough"
hosts = ["ldap.example.com"]
ports = [636]

# Raw TCP without HTTP or TLS inspection.
[[network.policy]]
id = "postgres"
mode = "tcp-forward"
hosts = ["db.example.com"]
ports = [5432]
```

Supported modes:

- `http-mediated`: terminate and inspect HTTP/HTTPS; supports `methods`, `path_prefixes`, and an ordered unique `hooks` list containing only current built-in approved names. Empty methods or path prefixes mean all.
- `tls-passthrough`: preserve end-to-end TLS for any TLS-wrapped application protocol and authorize using exact SNI; no header inspection or credential injection.
- `tcp-forward`: forward raw TCP.

`allow_ranges` is valid only for `tls-passthrough` and `tcp-forward`; each normalized CIDR explicitly permits an otherwise forbidden host-resolution result. Mediated HTTP rejects it and always rejects forbidden resolution ranges.

`hosts` is always a list, including for one host. Existing one-label wildcards such as `*.example.com` are accepted only for credential-free `http-mediated` and `tcp-forward` policies; they match neither `example.com` nor `a.b.example.com`. TLS passthrough and credential-bound policies require exact DNS hosts. Only `tcp-forward` and credential-free `http-mediated` policies may select IPv4 literals. Hosts may share a policy only when mode, ports, methods, paths, hooks, and other authorization are identical. Different authorization requires separate policies. sbx normalizes each host to an internal worker rule while preserving the public policy ID used by bindings and diagnostics. Overlapping destination patterns on the same transport and port are rejected rather than resolved by hidden precedence.

Methods are normalized to uppercase. Configuration and mediation use one path-prefix canonicalizer: repeatedly percent-decode with a fixed bound, reject invalid encoding, control characters, backslashes, and dot segments, then match either the complete prefix or a slash-delimited descendant. Binding methods and path prefixes must be subsets of their referenced policies. An omitted or empty parent list allows all, so a binding may still narrow it to specific methods or paths. Credential bindings are valid only for HTTPS traffic under DNS-host `http-mediated` policies because `tls-passthrough` and `tcp-forward` payloads are not inspected.

## Direct IPv4 policies

An IPv4 literal in `hosts` is the opt-in; there is no global direct-IP switch. Only the listed address and ports become reachable. A hostname policy does not grant direct access to its resolved addresses, and an `allow_ranges` CIDR does not grant access to other IPs inside that CIDR.

Private TCP access needs both an exact IP and an explicit address-safety exception:

```toml
[[network.policy]]
id = "database-ip"
mode = "tcp-forward"
hosts = ["10.20.30.40"]
ports = [5432]
allow_ranges = ["10.20.30.40/32"]
```

Raw TCP may carry TLS, but the gateway does not inspect it, inject host credentials, or verify its service certificate. The guest application must do its own TLS verification. Raw TCP is outside the durable HTTP-admission audit guarantee.

For HTTP inspection, use `http-mediated`. This illustrative address must be replaced with the public IPv4 of your own service; it makes no claim about the service currently at that address:

```toml
[[network.policy]]
id = "public-ip-api"
mode = "http-mediated"
hosts = ["93.184.216.34"]
ports = [443]
methods = ["GET", "HEAD"]
path_prefixes = ["/v1/"]
```

Mediated HTTP/HTTPS remains public-unicast-only: private addresses and `allow_ranges` are not supported for this mode. For HTTPS, the upstream must have a trusted certificate with the exact IP as an **IP-address SAN**, not a DNS-name SAN containing numeric text or just a matching Common Name. The gateway automatically puts the IP SAN in its guest-facing leaf, using the existing per-VM CA. Normal IP clients omit SNI; DNS-name HTTPS still requires matching SNI. `Host`/HTTP2 authority must match the policy IP and effective port. Do not disable certificate verification or use a DNS Host header to select a virtual host through an IP policy.

```text
DNS policy: guest -> synthetic address -> DNS identity checks -> validated upstream IP
IP policy:  guest -> configured IPv4  -> exact IP/port checks -> that same upstream IP
```

Both mediated paths still require durable admission before forwarding HTTP. Literal connections do not perform DNS or consume synthetic DNS mappings, and worker replacement restores their exact registrations. They do not select DNS-bound credentials or widen redirect permissions. Unknown IPs/ports, IPv6 literals, numeric aliases such as integer/hex/short IPv4, CIDR hosts, and IP-based `tls-passthrough` are rejected or denied. Loopback, link-local, the guest/gateway subnet, synthetic pool, unspecified, multicast and reserved non-unicast addresses cannot be enabled as direct destinations, even with a broad `allow_ranges`.

Upgrade the installed package and stop/start the VM to activate support; an old worker without matching direct-IP readiness support fails clearly. The isolated real-VM regression uses controlled test origins, not the illustrative public service above. Native macOS runtime remains unverified.

## Credentials

A credential is an array entry with these core fields:

- `id`: stable unique configuration identity.
- `environment`: guest environment variable that receives the placeholder.
- `method`: `plugin:<entry-point-name>` or `command`.
- `options`: method-specific TOML table.
- `fetch`: optional string, `"lazy"` (default) or `"startup"`; fetch on first authorized use or before networking starts.

Example:

```toml
[[network.credential]]
id = "github"
environment = "GH_TOKEN"
method = "command"
fetch = "startup"
options = { argv = ["op", "read", "op://Engineering/GitHub/token"], format = "text" }

[[network.credential.bindings]]
policies = ["github-api", "github-uploads"]
header = "Authorization: Bearer {placeholder}"
```

The two policies are defined in the [full example](#full-example). This uses the host's installed `op` CLI, not a separately installed 1Password plugin.

Each `options` and `bindings` table belongs to the most recent `[[network.credential]]` entry. The outer credential schema accepts no other fields. Actual credential values must never appear in options.

A credential without bindings is not retrieved and its placeholder is not exported to the guest, even with `fetch = "startup"`.

### Placeholder security

sbx, not the provider, generates each placeholder with `secrets.token_urlsafe(32)`. It is unique per VM launch and credential and has no fixed `SBX` marker. Placeholder formats are not configurable; add a provider-specific format only if a supported client demonstrably rejects the opaque value before sending it.

The placeholder remains stable while that VM runs so worker and mediator replacement cannot invalidate its guest environment. It rotates when the VM starts with a new gateway generation. The initiating CLI transfers it to the supervisor through an inherited descriptor and, after SSH readiness, writes it through SmolVM's managed guest-environment mechanism. Guest-side persistence is intentional because the placeholder is non-secret and later shells need the same value; a non-secret guest manifest lets the next start remove obsolete credential names. Placeholder values are never written to host policy/state or logs.

The mediator tracks and redacts exact placeholder values. It substitutes only the configured authentication header, never arbitrary URLs, query values, request bodies, git commit contents, or packet payloads. A known placeholder encountered in an inspected authentication field outside its authorized binding fails closed. Streaming bodies remain unchanged and are not buffered merely to search for placeholders.

## Bindings

**`bindings.policies` refers to `network.policy.id`, not to a credential ID.** Each binding belongs to the most recent `[[network.credential]]` entry. The credential and policy IDs do not need to match. For example, credential `github` in the full example binds to policies `github-api` and `github-uploads`.

The policy permits the destination; the binding permits that credential's substitution within the policy. It does not grant new network access or add a missing header. The guest must send exactly the expected header containing its placeholder.

Bindings reference policy IDs rather than duplicate hosts and ports. All policies in one binding share its header and optional method/path restrictions. The following binding snippets are alternatives for the GitHub binding in the full example; replace that binding rather than append overlapping copies:

```toml
[[network.credential.bindings]]
policies = ["github-api", "github-uploads"]
header = "Authorization: Bearer {placeholder}"
```

A binding may narrow policy authorization but never widen it:

```toml
[[network.credential.bindings]]
policies = ["github-api"]
header = "Authorization: Bearer {placeholder}"
methods = ["GET"]
path_prefixes = ["/user/"]
```

The plugin may provide a conventional default header. `header` is needed only when overriding that default or when the method has no default:

```toml
[[network.credential.bindings]]
policies = ["github-api"]
header = "Authorization: token {placeholder}"
```

The selected default or override must contain exactly one structural placeholder and pass strict header-name, value, size, and CRLF validation. Header names compare case-insensitively. A matching binding requires exactly one instance with the exact expected value; missing, duplicate, or altered fields deny. Multiple credentials may match one request only on distinct header names, and then every matching binding is required and fetched independently. Overlapping policy/header/method/path scopes are rejected during configuration. Plugins cannot define or widen destination hosts, ports, methods, or paths.

If a request carries a known placeholder in an inspected authentication field under a policy not listed in that credential's bindings, the mediator denies it rather than forwarding or silently stripping it. Cross-host redirects are reauthorized and never retain an injected credential.

### One credential, different headers

Define `service-api` and `service-legacy` as HTTPS `http-mediated` policies for your own services first. This fragment uses one host-provided value with two different header formats; it does not convert a token into credentials for an unrelated service. `get-service-key` is your own host command, not shipped by sbx.

```toml
[[network.credential]]
id = "shared-key"
environment = "SERVICE_KEY"
method = "command"
fetch = "lazy"
options = { argv = ["get-service-key"], format = "text" }

[[network.credential.bindings]]
policies = ["service-api"]
header = "Authorization: Bearer {placeholder}"

[[network.credential.bindings]]
policies = ["service-legacy"]
header = "X-API-Key: {placeholder}"
```

The guest sends `Authorization: Bearer $SERVICE_KEY` to the first service, or `X-API-Key: $SERVICE_KEY` to the second. The same opaque `SERVICE_KEY` placeholder is used in either header; the real value remains in the host cache until substitution.

### Two credentials on one request

For an API requiring both a bearer token and a separate API key, use distinct credential/environment IDs and distinct header names. This is an alternative fragment for your existing `service-api` policy, not an addition to the overlapping binding above. Both host helper commands are supplied by you.

```toml
[[network.credential]]
id = "service-token"
environment = "SERVICE_TOKEN"
method = "command"
options = { argv = ["get-service-token"], format = "text" }

[[network.credential.bindings]]
policies = ["service-api"]
header = "Authorization: Bearer {placeholder}"

[[network.credential]]
id = "service-api-key"
environment = "SERVICE_API_KEY"
method = "command"
options = { argv = ["get-service-api-key"], format = "text" }

[[network.credential.bindings]]
policies = ["service-api"]
header = "X-API-Key: {placeholder}"
```

The guest must send both placeholder headers. Failure to satisfy either binding denies the request; neither credential grants access outside the policy.

## Plugin methods

Plugins register under the `sbx.network.auth.methods` entry-point group:

```toml
[project.entry-points."sbx.network.auth.methods"]
# Registered by the sbx package.
aws-codeartifact = "sbx.gateway_credentials:aws_codeartifact"
# Example separately installed provider.
onepassword = "company_sbx_auth:credential"
```

sbx ships only the CodeArtifact method. It loads exactly the explicitly configured entry-point name. Missing or duplicate names fail closed. A misspelled plugin name must not fall back to executing a command.

An entry point exposes separate non-secret description and secret-fetch operations. Core schema validation happens in the initiating CLI. The new supervisor then runs description through a bounded provider helper and reports readiness only after description succeeds; failure occurs before mediator, worker, or QEMU startup. Both operations receive the credential ID and strictly validated method options. The core consumes the common `inherit_environment` option before plugin validation. Description retrieves no credential:

```python
provider.describe(credential_id, options) == {
    "header": {
        "name": "Authorization",
        "format": "template",
        "value": "Bearer {placeholder}",
    }
}
```

Fetch is lazy by default (or performed at startup when opted in) and returns only the changing value and expiry:

```python
provider.fetch(credential_id, options) == {
    "value": "secret-token",
    "expires_at": 1893456000,
}
```

`expires_at` is a Unix timestamp or `None`. `header` is an optional non-secret default injection descriptor from `describe`; command methods have no default, so their bindings require `header`. Supported descriptors include a validated literal template and Basic authentication, needed for CodeArtifact:

```python
{
    "name": "Authorization",
    "format": "basic-password",
    "username": "aws",
}
```

For Basic authentication, the mediator validates the decoded username and placeholder, replaces only the password, and re-encodes the header. Destination authorization remains in sbx, not in the plugin.

Plugins are trusted host code installed in the sbx environment. Their description and fetch operations must reject unknown options. sbx independently validates and bounds every result; description cannot return a credential value, and fetch cannot change destination scope or injection metadata.

## Command methods

Commands use an argv array and never a shell string.

### Fetching at startup for authentication

Set `fetch = "startup"` directly on the credential, not inside `options`. Omit it or use `fetch = "lazy"` to wait for the first authorized use. Only these two string values are accepted; this is not an array of triggers.

```toml
[[network.credential]]
id = "github"
environment = "GH_TOKEN"
method = "command"
fetch = "startup"
options = { argv = ["op", "read", "op://Engineering/GitHub/token"], format = "text" }

[[network.credential.bindings]]
policies = ["github-api"]
header = "Authorization: Bearer {placeholder}"
```

This uses the previously configured `github-api` policy. Startup fetches opted-in bound credentials sequentially, after validating descriptions and bindings but before starting the mediator, worker, or QEMU. Each fetch has a 60-second provider execution budget, including command execution; plugins may impose shorter internal limits. The startup wait includes these budgets without extending ordinary request/provider deadlines. Failure aborts startup with a redacted error identifying the credential; sbx does not continue lazily after a failed startup fetch.

Provider stdin is reserved for its protocol and commands receive no interactive terminal. For 1Password, use supported desktop-app/GUI authentication or sign in on the host beforehand; this option cannot service a terminal password prompt. Required host environment names still need explicit `options.inherit_environment` entries.

The result uses the same memory-only supervisor cache and expiry rules as lazy fetches. Data-plane recovery keeps that cache; a new gateway session fetches again. Text output has no expiry, so changing the vault value does not trigger automatic refresh. Changing this option for an already running VM requires stop/start.

### Text output

```toml
[[network.credential]]
id = "github"
environment = "GH_TOKEN"
method = "command"

[network.credential.options]
argv = ["op", "read", "op://Engineering/GitHub/token"]
format = "text"

[[network.credential.bindings]]
policies = ["github-api"]
header = "Authorization: token {placeholder}"
```

For `format = "text"`, stdout contains only the token:

```text
ghp_exampleToken123
```

One trailing newline is stripped. Empty output, multiple lines, CR, NUL, embedded newlines, oversized output, timeout, or nonzero exit status fails closed. stderr is diagnostic input only and is never interpreted as a token.

### JSON output

```toml
[[network.credential]]
id = "custom"
environment = "CUSTOM_TOKEN"
method = "command"

[network.credential.options]
argv = ["/home/nueces/bin/get-custom-token", "--json"]
format = "json"

[[network.credential.bindings]]
policies = ["custom-api"]
header = "Authorization: Bearer {placeholder}"
```

Expected stdout:

```json
{"value":"secret-token","expires_at":1893456000}
```

Executable resolution follows the host `PATH`; the child working directory is the private gateway directory. Every provider child receives only the host `HOME` plus documented `PATH` and `TZ` baseline and names explicitly listed in the common `inherit_environment` option. The core removes that option before method-specific validation. Those host values are captured by the initiating CLI and transferred to the supervisor through an inherited descriptor, never argv or persisted state. Secret inherited values are supplied only to `fetch`, not `describe`. Missing names fail closed. CodeArtifact profiles and config are discovered through host `HOME`; users of AWS environment credentials explicitly list the required AWS names. Provider helpers run in new process groups with fixed runtime, output, and concurrency limits, monitor a supervisor-lifetime descriptor, and terminate the whole provider group on timeout, output overflow or supervisor death. The 64 KiB output ceiling applies to stdout and stderr combined and is enforced while reading, not after buffering arbitrary output. Input and output are serviced concurrently so a helper cannot deadlock the parent by writing before reading its configuration. Configuration cannot enable a shell. Command methods do not silently infer destination scope or headers.

## Full example

Merge this network section into your existing `.sbx.toml`, replacing any existing network tables rather than duplicating them. Replace the vault reference, AWS account/domain/region, repository, and profile with your own. Install `op` and the AWS CLI on the host and configure their authentication first; the AWS CodeArtifact provider itself ships with sbx.

GitHub uses startup fetch to allow host authentication before networking starts. CodeArtifact demonstrates lazy fetch and automatic expiry refresh. Use `fetch = "startup"` there too if you want its initial retrieval to gate startup (provider-specific internal timeouts still apply).

```toml
[network]
default = "deny"

# GitHub API and uploads use different method authorization.
[[network.policy]]
id = "github-api"
mode = "http-mediated"
hosts = ["api.github.com"]
ports = [443]
methods = ["GET", "POST"]

[[network.policy]]
id = "github-uploads"
mode = "http-mediated"
hosts = ["uploads.github.com"]
ports = [443]
methods = ["POST"]

# Public downloads are allowed without GH_TOKEN.
[[network.policy]]
id = "github-downloads"
mode = "http-mediated"
hosts = ["objects.githubusercontent.com"]
ports = [443]
methods = ["GET", "HEAD"]

# Exact CodeArtifact repository scope.
[[network.policy]]
id = "codeartifact-python"
mode = "http-mediated"
hosts = ["my-domain-123456789012.d.codeartifact.us-east-1.amazonaws.com"]
ports = [443]
methods = ["GET", "HEAD"]
path_prefixes = ["/pypi/my-repo/"]

# One token shared by two policies, using the same header.
[[network.credential]]
id = "github"
environment = "GH_TOKEN"
method = "command"
fetch = "startup"
options = { argv = ["op", "read", "op://Engineering/GitHub/token"], format = "text" }

[[network.credential.bindings]]
policies = ["github-api", "github-uploads"]
header = "Authorization: Bearer {placeholder}"

# Short-lived token generated with the host AWS credential chain.
[[network.credential]]
id = "codeartifact"
environment = "CODEARTIFACT_AUTH_TOKEN"
method = "plugin:aws-codeartifact"
fetch = "lazy"

[network.credential.options]
domain = "my-domain"
domain_owner = "123456789012"
region = "us-east-1"
profile = "default"

[[network.credential.bindings]]
policies = ["codeartifact-python"]
```

Expected guest environment:

```text
GH_TOKEN=<opaque random value>
CODEARTIFACT_AUTH_TOKEN=<opaque random value>
```

The GitHub binding explicitly supplies its bearer-header template. The CodeArtifact plugin supplies the Basic-auth password descriptor with username `aws`, so no `header` override is needed. The host AWS CLI authenticates to AWS to obtain that repository token; sbx does not perform SigV4 signing of guest requests or give AWS access keys to the guest.

`github-downloads` is authorized but has no credential binding, so downloads receive no GitHub token. A known placeholder sent to that policy is denied. This is GitHub API/upload access and CodeArtifact PyPI download access, not arbitrary GitHub browsing/cloning, all package registries, or all AWS APIs. Additional download/redirect hosts require their own policies. Applying changes to a running VM requires stop/start.

### Using the credentials in the guest

Run these **inside the VM**, where sbx has installed the placeholders in the guest environment:

```bash
# curl does not read GH_TOKEN automatically; send the expected placeholder header.
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${GH_TOKEN}" https://api.github.com/user

# pip sends HTTP Basic with username aws and the placeholder as its password.
python -m pip install --index-url \
  "https://aws:${CODEARTIFACT_AUTH_TOKEN}@my-domain-123456789012.d.codeartifact.us-east-1.amazonaws.com/pypi/my-repo/simple/" \
  PACKAGE_NAME
```

Replace `PACKAGE_NAME` with a package in your repository. The URL above contains the **guest placeholder**, not the real token. Do not substitute a host token or use verbose credential/header dumps when sharing diagnostics. The gateway validates the Basic username and placeholder, then replaces the password on the host. Clients that reject opaque token formats before making a request may not work; a correctly scoped `curl` request is a simpler first check.

### AWS environment credentials instead of a profile

To use temporary AWS environment credentials, replace the CodeArtifact options table in the full example with this one. Export all listed names on the host before starting sbx; do not put their values in TOML. Omit `AWS_SESSION_TOKEN` from the list when using credentials that do not have one.

```toml
[network.credential.options]
domain = "my-domain"
domain_owner = "123456789012"
region = "us-east-1"
inherit_environment = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
```

Only these explicitly named host values are passed to secret-fetch helpers, not to the guest or the provider's non-secret description operation. Missing listed host values fail closed. Provider environment values are captured at startup; changing the host shell later does not update a running supervisor.

## Retrieval and refresh

- Retrieval is lazy on the first matching authorized request unless the credential sets `fetch = "startup"`.
- The per-VM supervisor/broker is the sole cache owner; the mediator retains a returned value only for the current request.
- Each credential has an independent cache and refresh lock.
- A value without expiry is cached for the supervisor session.
- A value with expiry is refreshed on the next authorized use when it is within 60 seconds of `expires_at` (or already expired); there is no background refresh timer.
- Concurrent requests share one refresh operation.
- Refresh failure may use a still-valid cached value.
- Once a value expires, provider failure denies the request.
- Credentials are memory-only and discarded on supervisor shutdown.
- Vault changes and revocation are not proactively detected. Values without expiry require a new supervisor session to fetch again; upstream authentication errors do not trigger automatic credential refetch or HTTP replay.

A year-long GitHub token and a 12-hour CodeArtifact token therefore use independent providers and refresh schedules.

## Process and trust boundaries

Credential methods run in killable bounded child processes owned by the unprivileged per-VM supervisor/broker, not in the VM and not by relaxing mediator Landlock rules. Each mediator incarnation receives a private inherited socket pair using a versioned, length-bounded, request-ID-correlated protocol. Mediator requests are asynchronous so one slow provider cannot block unrelated traffic. The supervisor owns one 128-pending-request budget across all mediator incarnations, separate from the eight-provider-helper budget. Saturation returns the existing credential-unavailable response without spawning work or closing a healthy channel. Disconnects cancel queued credential-lock/helper-slot waits; waits poll cancellation at most every 100 ms and have a ten-second capacity deadline. Already-running providers retain their execution/output limits and may populate the shared cache, but never send replies to a closed channel. Broken broker framing/transport, invalid JSON or an uncorrelated late reply after a lookup timeout closes the channel, fails waiting lookups and stops the mediator for existing supervised pair recovery; it cannot leave a running mediator with a dead reply reader. Recovery does not replay HTTP requests. Only the credential value is returned to the mediator for an authorized lookup; expiry and refresh ownership stay in the supervisor, and validated non-secret header descriptors are part of startup policy.

Plugins and commands are explicitly selected trusted same-UID host code. sbx limits their arguments, inherited environment, working directory, descriptors, runtime, concurrency, and output, but does not claim to confine provider filesystem or network access needed by host credential stores or cloud credential chains. Raw provider stdout and stderr are never copied into diagnostics or logs.

Real values must never appear in:

- guest environment or files;
- process arguments;
- persisted policy or state;
- diagnostics or logs;
- exceptions or surfaced/persisted provider stderr;
- packet captures before authorized substitution.

## Contributor smoke

Use a temporary fake `aws` executable on `PATH` that prints JSON such as `{"authorizationToken":"fake-token","expiration":1893456000}`. Never put a real cloud token in tests, examples, logs, or fixtures. Run `pytest --no-cov tests/test_gateway_credentials.py tests/test_gateway_startup_credentials.py tests/test_network_credentials_docs.py` and the mediator suite from `docs/development.md`.

## Required validation

Tests must cover plugin description/fetch separation, command methods, text and JSON command output, literal and Basic headers, multiple credentials, multiple policy bindings, independent expiry schedules, refresh serialization, malformed and oversized output, timeout, provider failure, exact request scoping, redirects, mediator replacement, supervisor shutdown, later guest sessions, stale guest-environment cleanup, and proof that real values never reach guest-visible or persisted data. Placeholder tests must prove `secrets.token_urlsafe(32)` generation, uniqueness across credentials and VM launches, stability across data-plane recovery, rotation with a new gateway generation, absence of a fixed sbx marker, exclusion from host state/logs, fail-closed use in inspected authentication fields outside bindings, and substitution only in the configured authentication header. URL, query, and streaming-body occurrences must remain unchanged.
