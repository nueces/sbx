"""Temporary in-process SmolVM preset creation compatibility layer."""

import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress

from smolvm import SmolVM
from smolvm.facade import _build_auto_config
from smolvm.presets import apply_preset, get_preset
from smolvm.types import PortForwardConfig

from sbx.constants import DEFAULT_BACKEND

# ponytail: private SmolVM seams; remove when upstream exposes preset creation.


@contextmanager
def _environment(values: Mapping[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def create_preset(
    preset_name: str,
    *,
    vm_name: str | None,
    guest_os: str,
    cpus: int | None,
    memory_mib: int | None,
    disk_size_mib: int | None,
    mounts: Sequence[str],
    writable_mounts: bool,
    port_forwards: Sequence[Mapping[str, object]],
    boot_timeout: float,
    install_timeout: int,
    host_env: Mapping[str, str],
) -> SmolVM:
    """Create and provision a running preset VM; the caller must close the facade."""
    preset = get_preset("claude-code" if preset_name == "claude" else preset_name)
    memory_mib = preset.default_mem_mib if memory_mib is None else memory_mib
    disk_size_mib = preset.default_disk_mib if disk_size_mib is None else disk_size_mib

    with _environment(host_env):
        config, ssh_key_path = _build_auto_config(
            vm_name=vm_name,
            name_prefix=preset_name,
            os=guest_os,
            backend=DEFAULT_BACKEND,
            memory=memory_mib,
            disk_size_mib=disk_size_mib,
            ssh_key_path=None,
        )
        updates: dict[str, object] = {
            "port_forwards": [PortForwardConfig(**value) for value in port_forwards]
        }
        if cpus is not None:
            updates["vcpu_count"] = cpus
        config = config.model_copy(update=updates)
        vm = SmolVM(
            config,
            ssh_key_path=ssh_key_path,
            mounts=list(mounts),
            writable_mounts=writable_mounts,
        )
        try:
            vm.start(boot_timeout=boot_timeout)
            vm.wait_for_ssh(timeout=boot_timeout)
            channel = vm._ensure_ssh_for_env()
            apply_preset(channel, preset, install_timeout=install_timeout)
        except BaseException:
            with suppress(Exception):
                vm.close()
            raise
    return vm
