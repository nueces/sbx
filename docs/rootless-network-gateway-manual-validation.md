# Gateway operator smoke test

## Purpose and prerequisites

Exercise allowed/denied egress, ingress and stop/start through the public CLI on Linux. This is a functional smoke, not release, performance, long-soak or native macOS acceptance.

Use an installed development version containing the gateway and a prepared image. See [installation](../README.md#install), [image building](build-local-debian-pi-image.md) and the [support boundary](rootless-network-gateway.md#support-and-prerequisites). Do not use real credentials. The anonymous HTTPS check needs host internet access to `example.com`.

The [canonical component diagram](rootless-network-gateway-components.mmd) explains the host/guest boundary. Keep all host evidence and state outside the one shared project directory. Do not mount the repository, its `local/` directory, a host home, Docker socket or credential store.

## 1. Prepare isolated host state

Use a dedicated host Bash terminal and keep it open throughout the smoke. Resolve the installed command and existing image **before** changing HOME. Replace the image path if you deliberately built another image:

```bash
SBX_BIN=$(command -v sbx)
IMAGE_DIR=$(realpath "$HOME/.smolvm/images/sbx")
test -f "$IMAGE_DIR/smolvm-image.json"
SMOKE_DIR=$(mktemp -d /tmp/sbx-smoke.XXXXXX)
printf 'Private smoke root: %s\n' "$SMOKE_DIR"
PROJECT_DIR="$SMOKE_DIR/project"
EVIDENCE_DIR="$SMOKE_DIR/host-evidence"
umask 077
mkdir -m 700 "$PROJECT_DIR" "$EVIDENCE_DIR"
export HOME="$SMOKE_DIR/home"
export XDG_CONFIG_HOME="$SMOKE_DIR/config"
export XDG_STATE_HOME="$SMOKE_DIR/state"
export XDG_DATA_HOME="$SMOKE_DIR/data"
export XDG_CACHE_HOME="$SMOKE_DIR/cache"
export XDG_RUNTIME_DIR="$SMOKE_DIR/runtime"
mkdir -m 700 "$HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
"$SBX_BIN" --version | tee "$EVIDENCE_DIR/version.txt"
"$SBX_BIN" network diagnostics --help
```

**Expected:** the command is the intended feature-capable installation; the image already exists; only a new owner-private temporary tree is created. No shared image is rebuilt or removed. Runtime gateway startup needs no host sudo; building the image and automated namespace fixtures have separate prerequisites.

Create this complete smoke config. The project is the only shared directory; the sibling state/evidence directories are not mounted. Empty lists deliberately override inherited selections:

```bash
CONFIG="$PROJECT_DIR/.sbx.toml"
cat >"$CONFIG" <<EOF
[sbx]
name = "gateway-smoke"
agent = "pi"
image = "$IMAGE_DIR"
run_user = "agent"
project_path = "$PROJECT_DIR"
writable_mounts = true
stop_on_exit = false
copy_host_credentials = false
git_config = false
env = []
mount = []
port_forwards = []
runtime_install = []
auth_port = "none"

[network]
default = "deny"
credential = []

[[network.policy]]
id = "example"
mode = "http-mediated"
hosts = ["example.com"]
ports = [443]
methods = ["GET", "HEAD"]

[[network.ingress]]
id = "dev-server"
listen = "127.0.0.1:13000"
guest_port = 3000
EOF
cd "$PROJECT_DIR"
```

An agent is not installed or launched by this smoke. Port 13000 must be free; choose another unused explicit port in the config and host checks if necessary. Keep the same shell variables for later host steps. Do not reuse these test settings as a credential-bearing production configuration.

## 2. Create and inspect (host)

```bash
"$SBX_BIN" --config "$CONFIG" create
"$SBX_BIN" --config "$CONFIG" network status
"$SBX_BIN" --config "$CONFIG" network diagnostics >"$EVIDENCE_DIR/diagnostics.before-stop.txt"
"$SBX_BIN" --config "$CONFIG" shell
```

**Expected:** ready gateway, loopback ingress and no SLIRP fallback. If any command fails, stop the procedure, retain the temporary tree and follow the failure guidance below. Do not weaken policy or TLS checks to obtain a pass.

## 3. Check egress and prepare ingress (guest, runtime user)

Inside the guest shell:

```bash
resolver=$(awk '$1 == "nameserver" { print $2 }' /etc/resolv.conf)
test "$resolver" = "192.0.2.1"
getent ahostsv4 example.com | awk '{ print $1 }' | grep -Eqm1 '^198\.(18|19)\.'
curl --max-time 15 -fsSI https://example.com/
```

Check negatives in the same guest shell. Certificate verification remains enabled; these targets are not authorized by this config:

```bash
must_fail() {
  if "$@"; then
    echo "FAIL: unexpectedly allowed" >&2
    return 1
  fi
  echo "PASS: denied"
}
must_fail curl --max-time 10 -fsSI https://www.example.com/
must_fail curl --max-time 10 -fsSI https://1.1.1.1/
must_fail curl --max-time 10 -fsSI http://example.com/
must_fail curl --max-time 10 -fsS -X POST https://example.com/
```

A client failure alone does not establish why it failed. Inspect private admission/outcome records using the audit path in host diagnostics. A missing outcome means unknown execution, never permission to replay. Alternate DNS, protocol and identity negatives are covered more comprehensively by the [automated integration suite](development.md#gateway-validation).

Start an ingress server inside the guest, bound to all guest interfaces:

```bash
python3 -m http.server 3000 --bind 0.0.0.0
```

From a second host terminal, only the following loopback request is needed:

```bash
curl --max-time 5 --fail http://127.0.0.1:13000/
```

**Expected:** the host receives the guest project directory listing. Stop the guest HTTP server with Ctrl-C, then leave that shell with `exit`.

## 4. Stop and restart (original host terminal)

Collect needed live diagnostics before stopping; stop removes ephemeral runtime/authentication material. Then:

```bash
"$SBX_BIN" --config "$CONFIG" stop
! curl --max-time 2 --fail http://127.0.0.1:13000/
"$SBX_BIN" --config "$CONFIG" network diagnostics >"$EVIDENCE_DIR/diagnostics.stopped.txt"
"$SBX_BIN" --config "$CONFIG" create
"$SBX_BIN" --config "$CONFIG" network status
"$SBX_BIN" --config "$CONFIG" shell
```

Inside the guest, repeat the allowed request and resolver checks from step 3. Exit the guest shell and stop again from the original host terminal:

```bash
"$SBX_BIN" --config "$CONFIG" stop
```

**Expected:** ingress closes on stop, the existing VM disk is reused on restart, and the next gateway permits only configured traffic. Do not confuse a successful stop/start with pair-recovery acceptance; the integration suite checks authenticated replacement while QEMU stays alive.

## Optional direct IPv4 check

Use the [IPv4 examples](rootless-network-gateway.md#direct-ipv4-policies) with a service you own, then stop/start to apply policy. Check the exact allowed address/port, wrong ports and unlisted neighbors. HTTPS needs trusted IP SANs and normal guest CA verification; never use a DNS Host override or disable verification. Check origin execution counts and private audit records, not only curl's status. DNS-bound host credentials must not become available through an IP admission.

The automated direct-IP fixture uses owned isolated public-address origins, not an arbitrary third-party endpoint. It does not weaken production address checks.

## Failure, preservation and optional cleanup (host)

**Default on failure: stop if possible and retain the VM, disk and entire private temporary tree.** Do not run `remove`, `recreate`, a recursive deletion or a failed request again to manufacture evidence. If guest export is unavailable, keep the stopped disk for later recovery. Never post raw logs, state, CA keys or UI login files; URLs can contain secrets.

After collecting guest-only data and stopping all test writers, preserve the full isolated tree in a sibling archive (Linux GNU tar). The archive is outside the input tree. Comparing member contents must succeed before destructive cleanup:

```bash
ARCHIVE="$SMOKE_DIR.tar.gz"
tar -czf "$ARCHIVE" -C "$(dirname "$SMOKE_DIR")" "$(basename "$SMOKE_DIR")"
tar -dzf "$ARCHIVE" -C "$(dirname "$SMOKE_DIR")"
```

**Expected:** both commands succeed and `tar --diff` reports no differences. A failed or partial archive does not authorize deletion. This is a private local snapshot, not a portable VM restore guarantee. Record its path and keep the original tree if uncertain.

Only after verifying preservation, optionally remove the disposable VM through the installed CLI. **This deletes guest-only disk data and retained gateway generations**; review the confirmation rather than bypassing it:

```bash
"$SBX_BIN" --config "$CONFIG" remove
```

Leave the evidence/archive in place. Close the dedicated host shell to discard its temporary HOME/XDG selection. Record sbx/image/QEMU identity, platform, allowed/denied results, audit interpretation, ingress and restart results, and any failed prerequisite. Native macOS and long-duration acceptance remain unverified by this procedure.
