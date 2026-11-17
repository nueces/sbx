# Linux Go race validation — 2026-11-16

**Result: PASS. The missing-compiler/race-testing blocker is resolved on this Linux host. Overall status remains NOT MERGE READY.**

Scope: install and document the host C toolchain, then run the complete ordinary and race-enabled gateway Go suites. No production source, test, CI, dependency-lock, release-binary or confinement changes were needed. No commit was made.

## Results

Host: Debian GNU/Linux **13.7**, Linux amd64; commands ran as **UID 1000**. Only apt administration used sudo. Pinned Go: **1.26.5**. Compiler: **gcc (Debian 14.2.0-19) 14.2.0**.

| Check | Result |
|---|---|
| C compiler, headers and libc link/run check | PASS; temporary executable removed |
| Ordinary `CGO_ENABLED=0 go test -count=1 -timeout=10m -json ./...` | PASS, all **7 packages** |
| Race `CGO_ENABLED=1 CC=/usr/bin/gcc go test -race -count=1 -timeout=10m -json ./...` | PASS, all **7 packages**, no race reports |
| Test completeness | Identical **105 passing events** per run: 66 top-level test/fuzz functions and 39 nested subtests/seed cases; zero failures/skips |
| Hardening child | PASS with `no_new_privs,seccomp,landlock,rlimits` active |
| Workspace integrity | Baseline source/module/worker hashes preserved; only this report and merge-readiness documentation changed |

Package results were checked against `go list ./...`, not inferred solely from the command exit code. Both runs used `-count=1`, a dedicated campaign build cache, `GOTOOLCHAIN=local`, no inherited `GOFLAGS`, isolated HOME and an allowlisted environment. Each command had a 30-minute outer timeout in addition to its ten-minute per-package timeout; neither timed out.

The Linux confinement test already builds its child with **`CGO_ENABLED=0`** because `AllThreadsSyscall` deliberately rejects cgo binaries. That behavior was preserved. The race detector instruments the applicable parent/package code, **not the pure-Go confinement child**. Passing seed cases also does not constitute a sustained fuzz campaign or prove absence of races in unexercised executions.

## Environment issue reproduced and resolved

After the compiler was installed, initial ordinary and race builds both failed before tests ran:

```text
link: mapping output file failed: invalid argument
```

A named-file probe reproduced writable `mmap` failure (`EINVAL`) in the workspace filesystem and success in `/tmp`. Go's build/test temporaries were therefore moved to an owned, mode-0700 directory under `/tmp` using per-command `TMPDIR` and `GOTMPDIR`. The cache and retained evidence remained in the workspace. No linker workaround, test exclusion, security relaxation or application change was introduced.

The original failing logs and the probe are retained alongside the successful reruns. The local temporary directory was removed after validation; future runs must recreate it.

## Installed OS dependencies

Apt metadata was refreshed, the installation simulated and reviewed, then the exact direct versions installed:

```bash
sudo apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
  gcc=4:14.2.0-1 libc6-dev=2.41-12+deb13u4
```

- **Direct:** `gcc` provides the compiler/link driver required by cgo/race builds; `libc6-dev` provides standard C headers and libc development linking files.
- **Actual transaction:** **34 newly installed packages: 2 direct + 32 transitive; 0 upgrades; 0 removals.** Only `gcc` and `libc6-dev` were added to apt's manual selections.
- No `build-essential`, distribution upgrade or autoremove was used. The installed development packages remain available for subsequent tests.
- These are **host validation dependencies**, not new wheel dependencies or guest-image packages. Existing release-worker builds explicitly use `CGO_ENABLED=0`; packaged workers were not rebuilt or changed.

All added packages are listed below, grouped only where exact versions match. The `:amd64` suffix is retained where reported by dpkg; `linux-libc-dev` is architecture `all`, and the remaining packages are amd64.

| Installed packages | Exact version |
|---|---|
| `binutils`, `binutils-common:amd64`, `binutils-x86-64-linux-gnu`, `libbinutils:amd64`, `libctf-nobfd0:amd64`, `libctf0:amd64`, `libgprofng0:amd64`, `libsframe1:amd64` | `2.44-3` |
| `cpp`, `cpp-x86-64-linux-gnu`, `gcc`, `gcc-x86-64-linux-gnu` | `4:14.2.0-1` |
| `cpp-14`, `cpp-14-x86-64-linux-gnu`, `gcc-14`, `gcc-14-x86-64-linux-gnu`, `libasan8:amd64`, `libcc1-0:amd64`, `libgcc-14-dev:amd64`, `libgomp1:amd64`, `libhwasan0:amd64`, `libitm1:amd64`, `liblsan0:amd64`, `libquadmath0:amd64`, `libtsan2:amd64`, `libubsan1:amd64` | `14.2.0-19` |
| `libc-dev-bin`, `libc6-dev:amd64` | `2.41-12+deb13u4` |
| `libcrypt-dev:amd64` | `1:4.4.38-1` |
| `libisl23:amd64` | `0.27-1` |
| `libmpc3:amd64` | `1.3.1-1+b3` |
| `libmpfr6:amd64` | `4.2.2-1` |
| `linux-libc-dev` | `6.12.117-1` |
| `rpcsvc-proto` | `1.4.3-1` |

Before/after complete package inventories, manual selections, simulation and install output, and a machine-readable direct/transitive delta are retained. The headers package does not replace the running kernel.

## Reproduction

From `feature/rootless-network-gateway/gateway`, use the pinned Go and native temporary storage. For example:

```bash
work=$(mktemp -d /tmp/sbx-go-race.XXXXXX)
mkdir -p "$work/home" "$work/tmp" "$work/cache"
export HOME="$work/home" TMPDIR="$work/tmp" GOTMPDIR="$work/tmp" GOCACHE="$work/cache"
export GOPATH=/tmp/sbx-gwa/go-path GOTOOLCHAIN=local GOWORK=off GOFLAGS=

CGO_ENABLED=0 /tmp/sbx-gwa/tools/go/bin/go test -count=1 -timeout=10m -json ./...
CGO_ENABLED=1 CC=/usr/bin/gcc GORACE='halt_on_error=1 exitcode=66' \
  /tmp/sbx-gwa/tools/go/bin/go test -race -count=1 -timeout=10m -json ./...
```

The actual campaign used a minimal allowlisted environment and retained complete stdout/stderr plus exit codes, without a pipeline masking failures. Its exact commands and environment snapshots are in the evidence below. Remove only the newly created `$work` directory after a reproduction run.

## Evidence and remaining scope

Ignored campaign directory:

```text
local/gateway-validation/20261116T165553Z-go-race/
```

Key receipts:

- `baseline.json`, `baseline.patch`, `git-status-before.log`, `go-tool.sha256`.
- `packages-before.log`, `packages-after.log`, `packages-final.log` and their manual-selection snapshots.
- `apt-update.log`, `apt-plan.log`, `apt-install.log`, `gcc-version.log`, `dependencies.json`.
- `compiler-check.c`, `compiler-build.log`, `compiler-run.log`.
- Initial `ordinary.log` / `race.log`, `mmap-probe.json`, successful `ordinary-final.log` / `race-final.log`.
- Per-run environment and `go env` snapshots, `package-list.log`, `test-summary.json`, `commands.jsonl` and final integrity/cleanup receipts.

This closes Linux Go race validation only. Performance measurements, complete presets, final VM/fault/stability campaigns, native macOS, third-party dependency security/distribution and separate licensing work remain outside this slice. Python/mediator suites and wheel builds were not repeated because no application or test code changed.
