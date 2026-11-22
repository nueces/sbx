# Documentation

These pages describe the current source checkout. Start with [installation and the first sandbox](../README.md); an older tagged installation may not contain unreleased features. Commands do not imply that native macOS, dependency security or every release gate has passed.

## Using sbx

- [Sandbox lifecycle, project context and recovery](ergonomics.md)
- [Build and run the agent-neutral image](build-local-debian-pi-image.md)
- [Organize a working area and Pi state](project-organization.md)
- [Forward selected environment variables](environment-forwarding.md)
- [Forward safe Git configuration](git-config-forwarding.md)
- [OAuth callback profiles and tunnel ownership](auth-ports.md)

## Networking and security

- [Networking commands: forwarding, status and diagnostics](network-command-roadmap.md)
- [Gateway policy, direct IPv4, TLS, audit and recovery](rootless-network-gateway.md)
- [Host-side credential providers and bindings](network-credentials.md)
- [Gateway process resource limits and their reasons](rootless-network-gateway-limits.md)
- [Isolated operator smoke and evidence preservation](rootless-network-gateway-manual-validation.md)
- [Canonical host/guest component diagram](rootless-network-gateway-components.mmd)

## Contributing and architecture

- [Contributor setup, tests and documentation conventions](development.md)
- [Dependency upgrade and release checks](dependency-upgrade.md)
- [Gateway implementation and specialized stability checks](../gateway/README.md)
- [Current local-image format, runtime and disk behavior](current-local-image-usage.md)
- [QEMU backend and networking rationale](qemu-default.md)
- [SSH versus the upstream guest control plane](smolvm-guest-control-plane.md)
- [Private integration seams and their exit conditions](fragile-glue.md)

Maintained guides describe supported behavior and known limits. Historical campaigns, local evidence, proposals and backlog notes are not a substitute for current instructions or release clearance and are not part of this index.
