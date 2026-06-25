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
