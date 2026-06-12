# Package auth / session proxy implementation tasks

This document breaks the package-auth plan into sequential, testable implementation tasks. Mark each task as done during implementation by changing `[ ]` to `[x]`.

Source design: [`package-auth-implementation-plan.md`](./package-auth-implementation-plan.md).

## Review summary

No blocker inconsistency was found in the current plan. The remaining open questions are implementation details and are captured below as early validation tasks.

The v1 target is:

- session-scoped `HTTP_PROXY` / `HTTPS_PROXY`;
- host `mitmdump` explicit proxy with an `sbx` addon;
- SSH remote-forward tunnel: guest loopback proxy port -> host loopback mitmdump port;
- sbx-managed mitmproxy CA installed in the guest when the session proxy is enabled;
- package auth replacement only for configured HTTPS package host/path and exact Basic placeholder;
- standalone session proxy mode via `[sbx.network.proxy].enabled = true`;
- no network-level bypass prevention in v1.

## Task 0 — Validation spike: uv, mitmproxy, CA, tunnel

- [ ] Done

### Objective

Validate the external behaviors that the implementation depends on before changing CLI behavior.

### Scope

- Confirm exact `uv` named-index env vars for username/password.
- Confirm uv's documented name normalization rule for `UV_INDEX_<NORMALIZED_NAME>_USERNAME` and `UV_INDEX_<NORMALIZED_NAME>_PASSWORD`: uppercase the index name and replace every non-alphanumeric character with `_`.
- Test representative names: `private`, `private-index`, `private_index`, and `private.index`.
- Confirm `uv sync` honors session `HTTPS_PROXY` without changing `UV_INDEX_URL`.
- Confirm mitmproxy explicit proxy mode exposes enough request data to the addon before upstream forwarding.
- Confirm guest CA install path used by target guest images makes `uv` trust mitmproxy certificates.
- Confirm SSH remote forwarding can listen on guest loopback and forward to host loopback.
- Verify CodeArtifact simple metadata, wheel, and sdist downloads all remain under `/pypi/<repository>/`.
- Record any redirects and whether they preserve the same host/path prefix.

### Out of scope

- Permanent CLI/config changes.
- Full lifecycle management.

### Tests / evidence

- Add a short validation note to `docs/package-auth-implementation-plan.md` or a companion note with exact commands and results.
- Add focused tests for any helper functions introduced during the spike, if retained.
- Manual proof command demonstrates `uv` sends `Authorization: Basic ...placeholder...` through mitmproxy.

## Task 1 — Add dependencies and package entry points

- [ ] Done

### Objective

Make `mitmproxy` available as a normal `sbx` dependency and define where the addon code lives.

### Scope

- Add `mitmproxy` as a non-optional dependency in `pyproject.toml`.
- Decide whether the addon is invoked as a generated file, module path, or script file.
- Add a preflight helper that verifies `mitmdump` can be launched.

### Out of scope

- Starting real mitmdump from `sbx run/shell`.

### Tests

- Unit test dependency/preflight helper with a fake command runner:
  - success when `mitmdump --version` returns 0;
  - clear error when executable is missing or exits nonzero.
- Existing CLI tests still pass with the new dependency.

## Task 2 — Config parsing and validation

- [ ] Done

### Objective

Parse and validate `[sbx.package_auth]`, `[sbx.package_auth.credential]`, `[sbx.network.proxy]`, and `[sbx.network.proxy.hosts]`.

### Scope

- Add config model/helper functions for:
  - `package_auth.enabled`;
  - package host, `path_prefix`, Basic username, required `uv_index_name`;
  - credential provider: `aws-codeartifact` and `command`;
  - network proxy enabled/default semantics;
  - `http`/`https` allow/block defaults;
  - origin-style host overrides: `scheme://host[:port]`.
- Enforce package-auth semantics:
  - package auth enabled + omitted network proxy enabled => enabled;
  - package auth enabled + network proxy enabled false => config error;
  - package auth requires `uv_index_name`;
  - one package rule only in v1.

### Out of scope

- Runtime process startup.

### Tests

- Config default tests:
  - package auth implies proxy enabled;
  - `http` defaults to `block`, `https` defaults to `allow`;
  - `uv_index_name` is required for package auth.
- Config error tests:
  - invalid action;
  - invalid origin key with path;
  - package auth with proxy explicitly disabled;
  - missing credential provider fields.
- Config success tests for `aws-codeartifact` and `command` providers.
- For `aws-codeartifact`, validate that `path_prefix` equals `/pypi/<repository>/` in v1.
- For `command`, validate that `path_prefix` is a non-root absolute path prefix ending in `/`, with no `..`, query, or fragment.

## Task 3 — Proxy policy classifier

- [ ] Done

### Objective

Implement the pure policy decision logic used by the mitmproxy addon.

### Scope

Create a pure function that classifies a request by scheme, host, port, and path into one of:

- `replace` for configured package host + HTTPS + path prefix;
- `block` for configured package host + HTTPS wrong path;
- `block` for configured package host over HTTP;
- `allow` / `block` from `[sbx.network.proxy.hosts]` exact origin override;
- `allow` / `block` from scheme default.

Package-host rules must always take priority over general proxy rules.

### Out of scope

- Actual header mutation.
- mitmproxy API integration.

### Tests

- Unit tests for priority order.
- Exact origin matching tests:
  - default ports normalize;
  - host comparison is case-insensitive;
  - explicit ports work;
  - paths in origin config are rejected by config validation.
- Tests that broad `https = "allow"` does not override package-host wrong-path block.

## Task 4 — Placeholder generation and uv/proxy env construction

- [ ] Done

### Objective

Generate per-session placeholders and construct session-scoped environment variables.

### Scope

- Generate high-entropy placeholder per VM/session.
- Build proxy env:
  - `HTTPS_PROXY=http://127.0.0.1:<guest_proxy_port>`;
  - `HTTP_PROXY=http://127.0.0.1:<guest_proxy_port>`;
  - owned `NO_PROXY=localhost,127.0.0.1,::1` or final validated equivalent.
- Build uv env:
  - `UV_INDEX_<NORMALIZED_NAME>_USERNAME=<username>`;
  - `UV_INDEX_<NORMALIZED_NAME>_PASSWORD=<placeholder>`;
  - `UV_KEYRING_PROVIDER=disabled`.
- Implement uv index env-var normalization according to uv's documented rule: uppercase and replace non-alphanumeric characters with `_`.
- Validate `uv_index_name` contains only ASCII alphanumeric characters, `.`, `_`, and `-`.
- Ensure env is process-scoped and not written to guest profiles/files.

### Out of scope

- Starting sessions.

### Tests

- Placeholder entropy/format test without asserting exact value.
- Env construction tests:
  - correct proxy URLs;
  - `NO_PROXY` does not preserve hostile existing values;
  - uv index name normalization matches uv's documented rule and validation spike results;
  - `private`, `private-index`, `private_index`, and `private.index` normalize as expected;
  - package-auth env includes `UV_KEYRING_PROVIDER=disabled`;
  - no real credential value appears in env.

## Task 5 — Credential providers

- [ ] Done

### Objective

Implement host-side credential providers for replacement tokens/passwords.

### Scope

- `aws-codeartifact` provider using host AWS CLI.
- `command` provider using argv-array command config.
- Trim stdout, reject empty output, never log stdout.
- In-memory token cache.
- CodeArtifact expiry-aware refresh using AWS CLI JSON output that includes `authorizationToken` and `expiration`; do not use the token-only `--query authorizationToken --output text` shape for the built-in provider.
- `command` provider runs on demand in v1 and is not cached unless a future config field adds an explicit TTL.

### Out of scope

- `boto3`/AWS SDK provider.
- Multiple credentials/rules.

### Tests

- Fake command runner tests:
  - returns trimmed token;
  - rejects empty stdout;
  - handles nonzero exit with redacted diagnostic;
  - AWS CLI command includes expected domain/owner/region/profile arguments;
  - AWS CLI command requests JSON output and does not use token-only query output.
- Cache tests:
  - CodeArtifact token is reused before expiry;
  - CodeArtifact token refreshes when expired;
  - command provider is invoked on demand and does not require a TTL config.
- Secret safety tests verify token does not appear in exception/log strings.

## Task 6 — Mitmproxy addon request handling

- [ ] Done

### Objective

Implement the addon that enforces policy and performs safe Basic auth replacement.

### Scope

- Load per-session addon config from a state file or generated module config.
- For `replace` action:
  - require exact `Authorization: Basic base64(username:placeholder)`;
  - replace with `Basic base64(username:real_token)`;
  - block missing/incorrect Basic auth;
  - block Bearer or other auth schemes.
- For `allow` action:
  - forward unchanged and never inject package auth.
- For `block` action:
  - return clear proxy response.
- Redact or suppress Authorization logging.

### Out of scope

- Real mitmdump process lifecycle.

### Tests

- Unit tests using mitmproxy test utilities or small adapter fakes:
  - exact placeholder is replaced;
  - wrong Basic password blocks;
  - missing Authorization blocks for package path;
  - Bearer auth blocks for package path;
  - unrelated allow leaves Authorization unchanged;
  - unrelated block returns configured status/message;
  - package host HTTP blocks.
- Test that real token is not present in addon logs/errors.

## Task 7 — Mitmdump process command and state layout

- [ ] Done

### Objective

Create the host-side runtime layout and command construction for mitmdump.

### Scope

- Shared sbx-managed mitmproxy config directory:
  - `~/.local/state/sbx/package-auth/mitmproxy/`.
- Per-session runtime directory:
  - `~/.local/state/sbx/package-auth/sessions/<vm-id>/`.
- Generate addon config with placeholder, package rule, credential provider config reference, and proxy ACL.
- Start `mitmdump` bound to `127.0.0.1:<host_port>`.
- Disable web UI and avoid verbose logging.
- Track PID, host port, guest port, config paths.
- Write state/config files with owner-only permissions where possible; never write real tokens to disk.

### Out of scope

- SSH tunnel startup.
- CLI integration.

### Tests

- Command construction tests:
  - uses sbx-controlled confdir;
  - binds localhost only;
  - includes addon script/config;
  - does not include token/placeholder in command args when avoidable.
- State file tests:
  - writes expected session metadata;
  - stale/corrupt state handled with clear errors.

## Task 8 — SSH remote-forward tunnel management

- [ ] Done

### Objective

Expose host mitmdump to the guest through SSH remote forwarding.

### Scope

- Allocate guest proxy port by probing guest loopback for a free port in an sbx-reserved high-port range, then using `ExitOnForwardFailure=yes` so races fail fast and retry with another port.
- Start SSH remote forward:
  - guest `127.0.0.1:<guest_proxy_port>` -> host `127.0.0.1:<host_mitmdump_port>`.
- Ensure remote bind is loopback-only.
- Track tunnel PID and ports.
- Detect readiness from guest side.
- Cleanup on session exit.

### Out of scope

- Custom host↔guest stream bridge.
- Vsock transport.

### Tests

- SSH command construction tests with fake `_ssh_command`:
  - uses `-R 127.0.0.1:<guest>:127.0.0.1:<host>`;
  - includes `ExitOnForwardFailure=yes`;
  - binds remote loopback only.
- Port allocation tests avoid known busy ports and retry on simulated remote-forward bind failure.
- Cleanup tests kill tracked process group and remove state.
- Readiness tests with fake listener/probe behavior.

## Task 9 — Guest CA installation

- [ ] Done

### Objective

Install the sbx-managed mitmproxy CA certificate into the guest when the session proxy is enabled.

### Scope

- Locate mitmproxy CA cert in sbx-controlled confdir.
- Copy CA cert to guest.
- Install into system trust store for target guest images.
- For Debian-like guests, copy to `/usr/local/share/ca-certificates/sbx-mitmproxy-ca.crt` and run `update-ca-certificates`.
- Support root and configured `run_user` attach modes.
- Re-run idempotently.

### Out of scope

- CA removal on every session exit; v1 leaves the installed CA certificate in place.
- Non-Linux guests.

### Tests

- Script construction tests:
  - copies to expected path;
  - runs trust update command for supported distro;
  - idempotent if cert already present.
- Error tests for missing CA cert and failed trust update.
- If possible, focused integration test against a lightweight fake SSH runner.

## Task 10 — Session env injection for shell/run

- [ ] Done

### Objective

Inject proxy and uv placeholder env vars into the correct attached process environments.

### Scope

- `sbx shell` receives proxy env in standalone and package-auth modes.
- `sbx run` launched agent receives proxy env in standalone and package-auth modes.
- uv placeholder env vars and `UV_KEYRING_PROVIDER=disabled` are only set in package-auth mode.
- Existing selected user env forwarding still works.
- Env is not persisted in guest files.

### Out of scope

- Forcing agents to preserve env if they sanitize it internally.

### Tests

- CLI/unit tests with fake attach command:
  - shell command includes proxy env;
  - run/agent command includes proxy env;
  - package-auth mode includes uv env;
  - standalone proxy mode does not include uv env or `UV_KEYRING_PROVIDER`;
  - `NO_PROXY` is owned/safe;
  - no token is present in env.

## Task 11 — Lifecycle integration for `sbx run` / `sbx shell`

- [ ] Done

### Objective

Wire proxy/package-auth startup and cleanup into existing `run` and `shell` workflows.

### Scope

- Determine active mode from config/CLI:
  - package auth;
  - standalone proxy;
  - neither.
- Start in order:
  1. mitmdump;
  2. SSH remote-forward tunnel;
  3. guest CA install;
  4. attach shell/agent with env.
- Cleanup session-owned mitmdump/tunnel on exit.
- Preserve existing auth-port behavior.

### Out of scope

- Standalone `sbx network proxy` commands.

### Tests

- Focused CLI tests with fake runners:
  - package auth starts proxy before attach;
  - standalone proxy starts proxy before attach;
  - neither mode does not start proxy;
  - cleanup runs on attach exit/failure;
  - auth-port and package proxy can coexist;
  - failure to start mitmdump aborts with clear message.

## Task 12 — Network status reporting

- [ ] Done

### Objective

Expose proxy/package-auth state in `sbx network status`.

### Scope

- Show whether session proxy is inactive/active/stale.
- Show mode: standalone proxy or package auth.
- Show host/guest ports and PIDs without secrets.
- Show configured package host/path in package-auth mode.
- Detect stale PIDs and stale tunnel records.

### Out of scope

- Starting/stopping proxy from network commands.

### Tests

- Status output tests for:
  - inactive;
  - active standalone proxy;
  - active package auth;
  - stale PID;
  - corrupt state file.
- Verify no placeholder or real token appears in status.

## Task 13 — Standalone network proxy mode verification

- [ ] Done

### Objective

Verify and harden the standalone proxy behavior already wired through Task 11.

### Scope

- Verify `[sbx.network.proxy].enabled = true` works when package auth is disabled.
- Verify standalone mode starts mitmdump and tunnel through the same lifecycle path as package-auth mode.
- Verify standalone mode applies `[sbx.network.proxy]` policy.
- Verify standalone mode installs the CA and injects proxy env.
- Verify standalone mode does not generate uv placeholder env.
- Verify standalone mode does not set `UV_KEYRING_PROVIDER`.
- Verify standalone mode does not load or invoke credential providers.
- Verify standalone mode does not include package-auth replacement config.

### Out of scope

- Package auth replacement.
- Duplicating lifecycle implementation already covered by Task 11.

### Tests

- CLI tests:
  - standalone mode starts proxy and injects proxy env;
  - no uv vars or `UV_KEYRING_PROVIDER` are injected;
  - credential provider is not invoked;
  - no package-auth replacement rule is emitted;
  - `http = block` and `https = allow` policy is passed to addon config.

## Task 14 — Diagnostics and user-facing errors

- [ ] Done

### Objective

Make failures actionable and secret-safe.

### Scope

User-facing errors for:

- mitmdump unavailable/broken;
- AWS CLI missing or unauthenticated;
- credential command failure;
- invalid config;
- CA install failure;
- SSH remote-forward failure;
- uv index name missing;
- blocked package request due to missing/wrong placeholder;
- blocked proxy traffic using HTTP `403 Forbidden` with a short secret-free message where an HTTP response can be generated.

### Out of scope

- Rich troubleshooting UI.

### Tests

- Unit tests assert error messages include recovery hints.
- Tests assert real token, placeholder, and Authorization header are not included in errors.
- CLI tests for common config errors.

## Task 15 — Documentation updates

- [ ] Done

### Objective

Document the feature for users after implementation.

### Scope

- Update README config reference.
- Add package-auth usage docs.
- Explain security model:
  - real host token stays on host;
  - placeholder enters VM env;
  - cooperative proxy only;
  - no network-level bypass prevention in v1.
- Document named uv index requirement.
- Document standalone proxy mode.

### Out of scope

- Full architecture documentation for future Gondolin-style networking.

### Tests

- Documentation examples are validated by config parsing tests where possible.
- CLI help snapshot/completion tests updated for new flags if flags are added.

## Task 16 — Focused integration test with local fake package host

- [ ] Done

### Objective

Verify the full mediation path without real AWS or CodeArtifact.

### Scope

- Use a local fake upstream server representing package host behavior.
- Use mitmdump + addon with command credential provider returning a fake token.
- Send a proxied HTTPS request with placeholder Basic auth.
- Verify upstream receives real token, not placeholder.
- Verify wrong auth blocks.

### Out of scope

- Real VM startup if not practical in CI.
- Real CodeArtifact.

### Tests

- Integration test marked appropriately if it starts subprocesses.
- Must pass without external network or AWS credentials.
- Assert no real/fake token leakage in logs beyond the controlled fake upstream assertion.

## Task 17 — Final end-to-end manual checklist

- [ ] Done

### Objective

Provide a manual acceptance checklist for a real CodeArtifact repository.

### Scope

- Configure named uv index.
- Enable package auth with AWS CLI provider.
- Run `sbx shell` and `uv sync`.
- Verify no real token in guest env/files known to sbx.
- Verify `sbx network status` reports active package auth without secrets.
- Verify cleanup after session exit.

### Out of scope

- Automated real AWS test.

### Tests / evidence

- Record manual command transcript with secrets redacted.
- Add any discovered gaps as follow-up tasks.
