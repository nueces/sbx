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

## Virtio devices on PCI

SmolVM starts Linux x86_64 guests on QEMU's `microvm` machine with virtio-mmio devices. Those devices use level-triggered interrupt lines on an interrupt controller emulated inside QEMU, and under bursts of requests a line can get stuck. The device then stops delivering interrupts and processes using it block; this was seen on shared folders.

`sbx` therefore rewrites SmolVM's `microvm` command line so the root disk, shared folders and network are PCI devices (`pcie=on`, `virtio-*-pci`). PCI devices use MSI-X interrupts, which do not go through that controller. Device names in the guest (`/dev/vda`, `eth0`) and shared-folder tags are unchanged; `/proc/interrupts` shows `PCI-MSIX` entries instead of `IO-APIC` ones.

The change applies the next time a VM starts. To compare or to work around a problem:

```bash
SBX_QEMU_VIRTIO_BUS=mmio sbx run   # keep the old virtio-mmio devices
SMOLVM_QEMU_MACHINE=q35 sbx run    # use the q35 machine instead of microvm
```

Any other value of `SBX_QEMU_VIRTIO_BUS` is rejected by every `sbx` command before a VM is started, so a typo does not change the VM's state.

A VM that is already stuck on the old devices can usually be freed without a restart by running `stat -f <shared folder>` for each shared folder from a root shell in the guest.

This is an interim patch in `sbx/smolvm_compat.py` until the pinned SmolVM/Celesto release attaches these devices to PCI itself.

## Trade-offs

QEMU/slirp may be slower than Firecracker/TAP networking. That trade-off is acceptable for now because it avoids privileged host networking requirements and lets us focus on the agent sandbox UX.

## Current constraint

`sbx` injects this backend and rejects other backend values for now:

```toml
[sbx]
backend = "qemu"
```
