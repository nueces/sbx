# Fragile glue ledger

Track deliberate use of internal APIs, generated-file assumptions, or other integration seams that should be replaced when upstream support exists.

## SmolVM init injection via protected methods

Used for Docker-capable images:

```python
class SbxDockerImageBuilder(ImageBuilder):
    def _default_init_script(self) -> str:
        return self._base_init_script(custom_commands="...")
```

Why: SmolVM has an internal `custom_commands` hook in `_base_init_script()`, but `build_debian_ssh_key()` does not expose it publicly. This lets sbx start rootless Docker at VM boot without SSH post-start orchestration, wrapping Pi, or patching `rootfs.ext4`.

Fragility: `_default_init_script()` and `_base_init_script()` are protected SmolVM internals and may change.

Exit: replace with a public SmolVM boot-hook API, e.g. `/etc/smolvm/boot.d/*.sh` or a public `custom_commands`/`boot_hooks` parameter.

## SmolVM preset creation through private facade methods

Used by `src/sbx/smolvm_preset.py`:

```python
from smolvm.facade import _build_auto_config
channel = vm._ensure_ssh_for_env()
```

Why: SmolVM 0.0.28 has public VM lifecycle and preset application APIs, but no public operation that creates a named auto-configured VM with sbx's CPU/port-forward settings and supplies the communication channel required by `apply_preset()`. The preset API also reads `os.environ`, so sbx temporarily activates its credential-filtered environment during provisioning.

Fragility: both methods are private and may change. The SmolVM dependency remains pinned to `smolvm==0.0.28`, and all preset-specific private access is contained in `smolvm_preset.py`.

Exit: replace this module with an upstream public preset creation API that accepts explicit VM resources, mounts, port forwards, timeouts, and host credential/environment inputs; then remove this entry and the adjacent `ponytail:` comment.
