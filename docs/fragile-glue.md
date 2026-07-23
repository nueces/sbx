# Fragile glue ledger

Track deliberate use of internal APIs, generated-file assumptions, or other integration seams that should be replaced when upstream support exists.

## SmolVM local rootfs and init seams

Used by `src/sbx/image/build_debian.py`:

```python
class SbxImageBuilder(ImageBuilder):
    def _default_init_script(self) -> str:
        return self._base_init_script(custom_commands="...")

DockerRootfsBuilder(...)._build_rootfs(...)
```

Why: SmolVM 0.0.28 has no public rootfs-only operation. `build_debian_ssh_key()` injects the guest agent and downloads a kernel; `DockerRootfsBuilder.build_boot_image()` also downloads a kernel. sbx needs only the generated init plus Docker export/ext4 conversion because it builds its own kernel and uses SSH.

Fragility: `_base_init_script()` and `_build_rootfs()` are private SmolVM internals and may change. The dependency remains pinned to `smolvm==0.0.28`, and focused tests cover the call boundary.

Exit: use a public SmolVM rootfs-only API that accepts caller-owned Dockerfile/context and init commands without resolving a kernel or injecting a guest agent.

## SmolVM preset creation through private facade methods

Used by `src/sbx/smolvm_preset.py`:

```python
from smolvm.facade import _build_auto_config
channel = vm._ensure_ssh_for_env()
```

Why: SmolVM 0.0.28 has public VM lifecycle and preset application APIs, but no public operation that creates a named auto-configured VM with sbx's CPU/port-forward settings and supplies the communication channel required by `apply_preset()`. The preset API also reads `os.environ`, so sbx temporarily activates its credential-filtered environment during provisioning.

Fragility: both methods are private and may change. The SmolVM dependency remains pinned to `smolvm==0.0.28`, and all preset-specific private access is contained in `smolvm_preset.py`.

Exit: replace this module with an upstream public preset creation API that accepts explicit VM resources, mounts, port forwards, timeouts, and host credential/environment inputs; then remove this entry and the adjacent `ponytail:` comment.
