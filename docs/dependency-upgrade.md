# Dependency upgrades

## Purpose and inputs

Use this contributor procedure when changing runtime dependencies or distribution artifacts. Read the current `pyproject.toml`, `uv.lock`, `gateway/go.mod` and `gateway/go.sum` as the authoritative version inputs; do not substitute a historical validation environment. Python 3.12 is the supported baseline; newer Python versions are best effort.

The current checkout pins SmolVM 0.0.28, mitmproxy 12.3.1, Cryptography 50.0.2 and h2 4.4.1. The worker uses Go 1.26.5 with `GOTOOLCHAIN=local`. Tornado is constrained transitively by mitmproxy rather than directly pinned by sbx.

## Known unresolved gates

These are **dated findings**, not a newly executed audit or a claim that an installed dependency is exploitable in every gateway path:

- **Last recorded check, 2026-11-11:** mitmproxy 12.3.1 constrains Tornado to `>=6.5.0,<=6.5.10`, while GHSA-pw9p-v7r6-gx4p, GHSA-r4j5-9xpp-3985 and GHSA-pcg2-g6cj-q92c list fixes in Tornado 6.6. No approved compatible resolution is recorded here.
- **Last recorded check, 2026-11-11:** Cryptography 50.0.2 lacked a Darwin amd64 wheel. The older universal2 release had known security findings; a vulnerable downgrade or source-build fallback was not approved. Linux amd64/Darwin arm64 resolution evidence does not clear Intel macOS.
- Native macOS execution, licensing/source/notices and booted-image validation are separate from version resolution. See the [support boundary](rootless-network-gateway.md#support-and-prerequisites).

Recheck advisories, upstream constraints and target wheels for the actual proposed candidate. Do not suppress unapproved findings, force conflicting metadata, silently drop a platform or weaken CI to obtain a pass. Any other exception, mitigation or fork needs a separate explicit review.

## Experimental audit exception

The user accepts four known Tornado advisories for experimental development builds while waiting for a compatible mitmproxy update. This is accepted risk, **not a fix or a clean audit**. The CI report supplied with this decision lists:

| Advisory | Reported fixed version |
| --- | --- |
| GHSA-5cjw-f85j-cv6r — triple-slash open redirect | 6.6.1 |
| GHSA-r4j5-9xpp-3985 — chunked-body resource exhaustion | 6.6 |
| GHSA-pcg2-g6cj-q92c — folded-header CPU exhaustion | 6.6 |
| GHSA-pw9p-v7r6-gx4p — protocol-relative open redirect | 6.6 |

From the repository root on the host, run the same policy as CI:

```bash
python .github/scripts/audit_dependencies.py
```

The script runs `uv audit --locked`. Only when the project version exactly matches `x.y.z.devN` and **every locked Tornado version is 6.5.10**, it warns and passes these four explicit `--ignore` IDs. Other findings, audit-service failures and command errors still fail CI. Stable versions, release candidates and changed Tornado versions receive no exceptions. Unsupported or corrupt metadata cannot turn an audit failure into success.

A stable candidate must pass plain `uv audit --locked` without ignores. Remove the exception when mitmproxy supports a Tornado version fixing all four findings (the supplied report requires at least 6.6.1). `--ignore-until-fixed` does not express this policy: fixes already exist in Tornado, but mitmproxy's dependency constraint blocks them. Dependency pins, wheel-only checks, runtime security controls and other release gates are unchanged.

## Upgrade procedure

From the repository root on the host:

1. Review release notes and the [integration seams](fragile-glue.md), then update only the intended source locks. SmolVM upgrades require explicit compatibility/facade review, not just a lock refresh.
2. Rebuild all three workers and synchronize all four mediator assets with `gateway/scripts/build_release_artifacts.sh` when their sources/toolchain change. Follow [development validation](development.md#gateway-validation).
3. Build a wheel and clean-install its locked dependencies **wheel-only** for each supported target. Execute installed Linux artifacts; record Darwin resolution/build separately from native execution. Verify source/wheel/installed bytes and executable modes.
4. Run `python .github/scripts/audit_dependencies.py`, Python/mediator/Go/race tests and affected explicit-TCG functional/security integrations. Stable candidates must also pass the unexcepted `uv audit --locked`. Record actual commands, versions, accepted exceptions and failures privately; do not describe an excepted audit as clean or turn unavailable gates into passes.
5. Repeat required resource/performance/fault/native-platform acceptance when the dependency change affects those paths. Preserve durable admission, destination/TLS identity, no replay and confinement.
6. Review bundled/transitive licenses, source/notices and image/agent redistribution. Run the image-input checker from [development](development.md#packaging-and-integration-seams) when those pins change.
7. Run documentation example, lint and diff checks. Keep support labels and installation instructions aligned with the candidate actually validated.

Do not add compilers, runtime downloads, privileged networking, source-build fallbacks or a shared mitmproxy data plane as an incidental dependency upgrade. Keep unresolved gates explicit in the release decision.
