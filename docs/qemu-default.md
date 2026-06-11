# Why sbx supports QEMU first

`sbx` currently supports the SmolVM `qemu` backend only.

Other SmolVM backends, such as Firecracker or libkrun, may be evaluated later after the core `sbx` ergonomics and feature set are stable.

## Goal

`sbx` is meant to run coding agents in an isolated VM while keeping host requirements and host-side privileges as small as practical.

The default path should work for local developer usage without requiring per-VM privileged network setup.

## QEMU user-mode networking

With QEMU, SmolVM can use user-mode networking/slirp.

In this mode:

- no host TAP device is required for normal VM startup;
- no nftables NAT rules are required for normal VM startup;
- no `sudo -n ip tuntap ...` call is needed per VM;
- SSH is exposed through a localhost host-forward, e.g. `127.0.0.1:2200 -> guest:22`.

This matches `sbx`'s desired initial target: local sandboxing with low host privilege requirements.

## Firecracker / TAP networking

Firecracker typically uses host TAP networking. That path can require host setup and non-interactive sudo privileges for commands such as:

```bash
sudo -n ip tuntap add ...
sudo -n nft ...
sudo -n sysctl ...
```

This can be faster and more production-like, but it is not the best first supported backend for a local developer CLI whose goal is to avoid unnecessary host privileges.

## libkrun

libkrun may eventually be interesting because it can provide lightweight VM isolation and may have a rootless-friendly model on supported hosts.

However, it needs separate evaluation for:

- availability across supported developer machines;
- SmolVM feature parity;
- project directory mounts;
- SSH reachability;
- OAuth callback forwarding;
- performance and reliability.

## Auth callback forwarding

Pi OAuth login inside the VM redirects the host browser to something like:

```text
http://localhost:1455/auth/callback?code=...
```

With QEMU user-mode networking, arbitrary guest ports are not directly reachable from the host. `sbx` therefore forwards the callback port over SSH:

```text
host localhost:1455 -> SSH tunnel -> guest localhost:1455
```

This uses the existing SmolVM SSH path and avoids additional host firewall/NAT configuration.

## Trade-offs

QEMU/slirp may be slower than Firecracker/TAP networking. That trade-off is acceptable for now because it avoids privileged host networking requirements and lets us focus on the agent sandbox UX.

## Current constraint

`sbx` injects this backend and rejects other backend values for now:

```toml
[sbx]
backend = "qemu"
```
