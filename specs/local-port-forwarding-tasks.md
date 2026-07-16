# Local port forwarding tasks

- [x] Create `specification/local-port-forwarding` and `feature/local-port-forwarding` worktrees.
- [x] Record the agreed design.
- [x] Inspect current `sbx network auth-port` and QEMU startup plumbing.
- [x] Add a shared parser for `GUEST_PORT`, `HOST_PORT:GUEST_PORT`, and `BIND_HOST:HOST_PORT:GUEST_PORT`.
- [x] Add tests for valid and invalid forward specs.
- [x] Add `port_forwards` to `.sbx.toml` loading and generated/example config docs.
- [x] Apply configured `port_forwards` during `sbx run` and `sbx shell` VM startup using QEMU `hostfwd`/SmolVM `port_forwards`.
- [x] Add `sbx network forward [NAME] SPEC` as a foreground SSH tunnel.
- [x] Print the selected host/guest mapping and `Press Ctrl-C to stop.` before blocking.
- [x] Keep ad-hoc forwarding untracked: no `stop-forward`, no daemon, no tunnel state file.
- [x] Update README/network docs with the minimal UX.
- [x] Run focused tests and ruff.
