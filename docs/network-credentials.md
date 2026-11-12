# Network credentials

> Status: implemented by rootless-network-gateway Task 32.

Network credentials let guest tools use familiar environment variables while real values remain on the host. The guest receives a high-entropy opaque placeholder. The mediator injects the real value only after an outbound request matches an explicit network policy and credential binding.

## Configuration model

Configuration has three parts:

1. `[[network.policy]]` authorizes destinations and protocol behavior.
2. `[[network.credential]]` defines how one credential is retrieved or generated.
3. `[[network.credential.bindings]]` connects that credential to one or more policy IDs.

Credential options and bindings are nested under their credential, so their relationship is visible. A credential may have multiple bindings, and one binding may reference multiple policies.

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

`hosts` is always a list, including for one host. Existing one-label wildcards such as `*.example.com` are accepted only for credential-free `http-mediated` and `tcp-forward` policies; they match neither `example.com` nor `a.b.example.com`. TLS passthrough and credential-bound policies require exact hosts. Hosts may share a policy only when mode, ports, methods, paths, hooks, and other authorization are identical. Different authorization requires separate policies. sbx normalizes each host to an internal worker rule while preserving the public policy ID used by bindings and diagnostics. Overlapping destination patterns on the same transport and port are rejected rather than resolved by hidden precedence.

Methods are normalized to uppercase. Configuration and mediation use one path-prefix canonicalizer: repeatedly percent-decode with a fixed bound, reject invalid encoding, control characters, backslashes, and dot segments, then match either the complete prefix or a slash-delimited descendant. Binding methods and path prefixes must be subsets of their referenced policies. An omitted or empty parent list allows all, so a binding may still narrow it to specific methods or paths. Credential bindings are valid only for HTTPS traffic under `http-mediated` policies because `tls-passthrough` and `tcp-forward` payloads are not inspected.

## Credentials

A credential is an array entry with these core fields:

- `id`: stable unique configuration identity.
- `environment`: guest environment variable that receives the placeholder.
- `method`: `plugin:<entry-point-name>` or `command`.
- `options`: method-specific TOML table.

Example:

```toml
[[network.credential]]
id = "github"
environment = "GH_TOKEN"
method = "plugin:onepassword"

[network.credential.options]
item = "GitHub"
field = "token"

[[network.credential.bindings]]
policies = ["github-api", "github-uploads"]
```

Each `options` and `bindings` table belongs to the most recent `[[network.credential]]` entry. The outer credential schema accepts no other fields. Actual credential values must never appear in options.

A credential without bindings is not retrieved and its placeholder is not exported to the guest.

### Placeholder security

sbx, not the provider, generates each placeholder with `secrets.token_urlsafe(32)`. It is unique per VM launch and credential and has no fixed `SBX` marker. Placeholder formats are not configurable; add a provider-specific format only if a supported client demonstrably rejects the opaque value before sending it.

The placeholder remains stable while that VM runs so worker and mediator replacement cannot invalidate its guest environment. It rotates when the VM starts with a new gateway generation. The initiating CLI transfers it to the supervisor through an inherited descriptor and, after SSH readiness, writes it through SmolVM's managed guest-environment mechanism. Guest-side persistence is intentional because the placeholder is non-secret and later shells need the same value; a non-secret guest manifest lets the next start remove obsolete credential names. Placeholder values are never written to host policy/state or logs.

The mediator tracks and redacts exact placeholder values. It substitutes only the configured authentication header, never arbitrary URLs, query values, request bodies, git commit contents, or packet payloads. A known placeholder encountered in an inspected authentication field outside its authorized binding fails closed. Streaming bodies remain unchanged and are not buffered merely to search for placeholders.

## Bindings

Bindings reference policy IDs rather than duplicate hosts and ports:

```toml
[[network.credential.bindings]]
policies = ["github-api", "github-uploads"]
```

A binding may narrow policy authorization but never widen it:

```toml
[[network.credential.bindings]]
policies = ["github-api"]
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

Fetch is lazy and returns only the changing value and expiry:

```python
provider.fetch(credential_id, options) == {
    "value": "secret-token",
    "expires_at": 1787600000,
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
{"value":"secret-token","expires_at":1787600000}
```

Executable resolution follows the host `PATH`; the child working directory is the private gateway directory. Every provider child receives only the host `HOME` plus documented `PATH` and `TZ` baseline and names explicitly listed in the common `inherit_environment` option. The core removes that option before method-specific validation. Those host values are captured by the initiating CLI and transferred to the supervisor through an inherited descriptor, never argv or persisted state. Secret inherited values are supplied only to `fetch`, not `describe`. Missing names fail closed. CodeArtifact profiles and config are discovered through host `HOME`; users of AWS environment credentials explicitly list the required AWS names. Provider helpers run in new process groups with fixed runtime, output, and concurrency limits, monitor a supervisor-lifetime descriptor, and terminate the whole provider group on timeout, output overflow or supervisor death. The 64 KiB output ceiling applies to stdout and stderr combined and is enforced while reading, not after buffering arbitrary output. Input and output are serviced concurrently so a helper cannot deadlock the parent by writing before reading its configuration. Configuration cannot enable a shell. Command methods do not silently infer destination scope or headers.

## Full example

This example uses one long-lived GitHub token and one short-lived AWS CodeArtifact token. Both real values remain on the host.

```toml
[sbx]
name = "credential-smoke"
agent = "pi"
image = "~/.smolvm/images/sbx"
run_user = "agent"
project_path = "/home/nueces/code/my-project"

[network]
default = "deny"

# GitHub API and uploads use different method authorization.
[[network.policy]]
id = "github-api"
mode = "http-mediated"
hosts = ["api.github.com"]
ports = [443]
methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]

[[network.policy]]
id = "github-uploads"
mode = "http-mediated"
hosts = ["uploads.github.com"]
ports = [443]
methods = ["GET", "POST", "PUT"]

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

# Long-lived token retrieved from 1Password.
[[network.credential]]
id = "github"
environment = "GH_TOKEN"
method = "plugin:onepassword"

[network.credential.options]
item = "GitHub"
field = "token"

[[network.credential.bindings]]
policies = ["github-api", "github-uploads"]

# Short-lived token generated with the host AWS credential chain.
[[network.credential]]
id = "codeartifact"
environment = "CODEARTIFACT_AUTH_TOKEN"
method = "plugin:aws-codeartifact"

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

The GitHub plugin supplies its conventional authorization header. The CodeArtifact plugin supplies the AWS Basic-auth password descriptor. `github-downloads` is authorized but has no credential binding, so downloads receive no GitHub token. A known placeholder sent to that policy is denied.

## Retrieval and refresh

- Retrieval is lazy on the first matching authorized request.
- The per-VM supervisor/broker is the sole cache owner; the mediator retains a returned value only for the current request.
- Each credential has an independent cache and refresh lock.
- A value without expiry is cached for the supervisor session.
- A value with expiry is refreshed shortly before `expires_at`.
- Concurrent requests share one refresh operation.
- Refresh failure may use a still-valid cached value.
- Once a value expires, provider failure denies the request.
- Credentials are memory-only and discarded on supervisor shutdown.

A year-long GitHub token and a 12-hour CodeArtifact token therefore use independent providers and refresh schedules.

## Process and trust boundaries

Credential methods run in killable bounded child processes owned by the unprivileged per-VM supervisor/broker, not in the VM and not by relaxing mediator Landlock rules. Each mediator incarnation receives a private inherited socket pair using a versioned, length-bounded, request-ID-correlated protocol. Mediator requests are asynchronous so one slow provider cannot block unrelated traffic. The supervisor owns one 128-pending-request budget across all mediator incarnations, separate from the eight-provider-helper budget. Saturation returns the existing credential-unavailable response without spawning work or closing a healthy channel. Disconnects cancel queued credential-lock/helper-slot waits; waits poll cancellation at most every 100 ms and have a ten-second capacity deadline. Already-running providers retain their execution/output limits and may populate the shared cache, but never send replies to a closed channel. Broken broker framing/transport, invalid JSON or an uncorrelated late reply after a lookup timeout closes the channel, fails waiting lookups and stops the mediator for existing supervised pair recovery; it cannot leave a running mediator with a dead reply reader. Recovery does not replay HTTP requests. Only the value and expiry cross for an authorized request; validated non-secret header descriptors are part of startup policy.

Plugins and commands are explicitly selected trusted same-UID host code. sbx limits their arguments, inherited environment, working directory, descriptors, runtime, concurrency, and output, but does not claim to confine provider filesystem or network access needed by host credential stores or cloud credential chains. Raw provider stdout and stderr are never copied into diagnostics or logs.

Real values must never appear in:

- guest environment or files;
- process arguments;
- persisted policy or state;
- diagnostics or logs;
- exceptions or surfaced/persisted provider stderr;
- packet captures before authorized substitution.

## Contributor smoke

Use a temporary fake `aws` executable on `PATH` that prints JSON such as `{"authorizationToken":"fake-token","expiration":1787600000}`. Never put a real cloud token in tests, examples, logs, or fixtures. Run `pytest --no-cov tests/test_gateway_credentials.py` and the mediator suite from `docs/development.md`.

## Required validation

Tests must cover plugin description/fetch separation, command methods, text and JSON command output, literal and Basic headers, multiple credentials, multiple policy bindings, independent expiry schedules, refresh serialization, malformed and oversized output, timeout, provider failure, exact request scoping, redirects, mediator replacement, supervisor shutdown, later guest sessions, stale guest-environment cleanup, and proof that real values never reach guest-visible or persisted data. Placeholder tests must prove `secrets.token_urlsafe(32)` generation, uniqueness across credentials and VM launches, stability across data-plane recovery, rotation with a new gateway generation, absence of a fixed sbx marker, exclusion from host state/logs, fail-closed use in inspected authentication fields outside bindings, and substitution only in the configured authentication header. URL, query, and streaming-body occurrences must remain unchanged.
