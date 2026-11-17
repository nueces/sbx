"""Temporary in-process SmolVM preset creation compatibility layer."""

import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import replace
from pathlib import Path

from smolvm import SmolVM, VMConfig
from smolvm.facade import _resolve_vm_name
from smolvm.images.manager import resolve_image_dir
from smolvm.presets import Preset, apply_preset, get_preset
from smolvm.types import PortForwardConfig
from smolvm.utils import ensure_ssh_key

from sbx import guest_setup, lifecycle_warnings, vm_metadata
from sbx.constants import DEFAULT_BACKEND
from sbx.image.build_debian import BOOT_ARGS
from sbx.runtime import ConfigError

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


def _build_base_config(
    *,
    vm_name: str | None,
    name_prefix: str,
    memory: int,
    disk_size_mib: int | None,
    default_disk_size_mib: int,
) -> tuple[VMConfig, str]:
    image_dir = (resolve_image_dir() / "sbx").resolve()
    if not image_dir.is_dir():
        raise ConfigError(
            f"prepared sbx base image not found: {image_dir}; run 'sbx image build' first"
        )
    manifest = lifecycle_warnings.local_image_manifest(image_dir)
    paths = {}
    for field in ("kernel", "rootfs", "initrd"):
        if field == "initrd" and manifest.get(field) is None:
            paths["initrd_path"] = None
            continue
        path = lifecycle_warnings.manifest_path(image_dir, manifest, field)
        if not path.is_file():
            raise ConfigError(f"image {field} not found: {path}")
        paths[f"{field}_path"] = path.resolve(strict=True)
    boot_args = manifest.get("boot_args", BOOT_ARGS)
    if not isinstance(boot_args, str):
        raise ConfigError("image manifest field 'boot_args' must be a string")
    rootfs = paths["rootfs_path"]
    image_size = lifecycle_warnings.rootfs_size_mib(rootfs)
    if image_size is None:
        raise ConfigError(f"cannot determine image rootfs size: {rootfs}")
    if disk_size_mib is None:
        disk_size_mib = max(default_disk_size_mib, image_size)
    if disk_size_mib < image_size:
        raise ConfigError(lifecycle_warnings.local_image_disk_size_error(disk_size_mib, image_size))
    private_key, public_key = ensure_ssh_key()
    config = VMConfig(
        vm_id=_resolve_vm_name(vm_name, prefix=name_prefix),
        vcpu_count=1,
        memory=memory,
        **paths,
        boot_args=boot_args,
        backend=DEFAULT_BACKEND,
        guest_os="ubuntu",  # SmolVM's Debian-compatible Linux guest type.
        comm_channel="ssh",
        ssh_capable=True,
        ssh_public_key=public_key.read_text(encoding="utf-8").strip(),
        disk_size_mib=disk_size_mib,
        grow_filesystem=disk_size_mib > image_size and rootfs.suffix.lower() != ".qcow2",
    )
    return config, str(private_key.resolve(strict=True))


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
    start_vm: Callable[[SmolVM], None] | None = None,
    before_provision: Callable[[str], None] | None = None,
    run_user: str = "root",
    env_values: Mapping[str, str | None] | None = None,
    operation: vm_metadata.Lifecycle | None = None,
) -> SmolVM:
    """Create and provision a running preset VM; the caller must close the facade."""
    if guest_os != "debian":
        raise ConfigError(
            "presets use the prepared Debian sbx base image; remove [sbx].os or set it to 'debian'"
        )
    preset = (
        Preset(name="opencode", summary="OpenCode", install_script="")
        if preset_name == "opencode"
        else get_preset("claude-code" if preset_name == "claude" else preset_name)
    )
    memory_mib = preset.default_mem_mib if memory_mib is None else memory_mib

    with _environment(host_env), ExitStack() as cleanup:
        operation = operation or cleanup.enter_context(vm_metadata.Lifecycle())
        config, ssh_key_path = _build_base_config(
            vm_name=vm_name,
            name_prefix=preset_name,
            memory=memory_mib,
            disk_size_mib=disk_size_mib,
            default_disk_size_mib=preset.default_disk_mib,
        )
        updates: dict[str, object] = {
            "port_forwards": [PortForwardConfig(**value) for value in port_forwards],
            "comm_channel": "ssh",
            "ssh_capable": True,
        }
        # Cache and key paths must outlive the temporary credential-free HOME.
        for name in ("kernel_path", "rootfs_path", "initrd_path"):
            path = getattr(config, name)
            if path is not None:
                updates[name] = path.resolve(strict=True)
        ssh_key_path = str(Path(ssh_key_path).resolve(strict=True))
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
            operation.lock(str(vm.vm_id))
            if before_provision is not None:
                before_provision(str(vm.vm_id))
            if start_vm is None:
                guest_setup.start_vm(str(vm.vm_id), boot_timeout=boot_timeout, status="created")
            else:
                start_vm(vm)
            vm.refresh()
            with guest_setup.bootstrap_session(
                str(vm.vm_id), run_user, env_values=env_values, timeout=boot_timeout
            ) as guest:
                guest_setup.prepare_boot(
                    str(vm.vm_id), run_user, env_values or {}, guest=guest, mounts=False
                )
                # Copy initial root configuration before shares can cover its destinations.
                # Boot sync owns env selection; upstream must not re-inject host values.
                if preset_name == "opencode":
                    # The pinned SDK has no OpenCode preset; use our existing installer.
                    guest_setup.install_runtime_tools(
                        str(vm.vm_id), run_user, [preset_name], install_timeout, guest=guest
                    )
                else:
                    # The prepared base already supplies Node and the OS prerequisites.
                    apply_preset(
                        guest,
                        replace(preset, host_env_vars=(), setup_script=""),
                        install_timeout=install_timeout,
                    )
                    if preset_name == "claude":
                        guest_setup.install_runtime_tools(
                            str(vm.vm_id), run_user, [preset_name], install_timeout, guest=guest
                        )
                guest_setup.mount_workspaces(str(vm.vm_id), run_user, guest=guest)
        except BaseException:
            with suppress(Exception):
                vm.close()
            raise
    return vm
