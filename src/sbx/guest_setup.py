import base64
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from sbx import runtime
from sbx.runtime import ConfigError, ssh_command

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
VM_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
FORWARDABLE_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
SAFE_GIT_CONFIG_KEYS = (
    "user.name",
    "user.email",
    "init.defaultBranch",
    "pull.rebase",
    "push.default",
    "core.autocrlf",
    "core.eol",
)


def validate_vm_name(name: str) -> str:
    if not VM_NAME_RE.match(name):
        raise ConfigError(
            "[sbx].name must be a valid hostname: lowercase letters, digits, hyphens, "
            "1-63 chars, no leading/trailing hyphen"
        )
    return name


def validate_run_user(user: str) -> str:
    if not USERNAME_RE.match(user):
        raise ConfigError("[sbx].run_user must be a valid Linux user name")
    return user


def validate_env_names(names: list[str]) -> list[str]:
    invalid = [name for name in names if not ENV_NAME_RE.match(name)]
    if invalid:
        raise ConfigError(f"invalid env var name(s): {', '.join(invalid)}")
    return names


def sanitize_forwarded_env(env: dict[str, str], allowed: list[str]) -> dict[str, str]:
    allowed_set = set(allowed)
    for key in FORWARDABLE_ENV_VARS:
        if key not in allowed_set:
            env.pop(key, None)
    return env


def credential_free_env(temp_home: Path, *, forward_env: list[str]) -> dict[str, str]:
    env = sanitize_forwarded_env(dict(os.environ), forward_env)
    real_home = Path.home()
    real_smolvm_cache = real_home / ".smolvm"
    real_smolvm_data = Path(env.get("SMOLVM_DATA_DIR", real_home / ".local" / "state" / "smolvm"))
    temp_home.mkdir(parents=True, exist_ok=True)
    real_smolvm_cache.mkdir(parents=True, exist_ok=True)
    real_smolvm_data.mkdir(parents=True, exist_ok=True)
    (temp_home / ".smolvm").symlink_to(real_smolvm_cache, target_is_directory=True)

    fake_bin = temp_home / ".sbx-bin"
    fake_bin.mkdir()
    security = fake_bin / "security"
    security.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    security.chmod(0o755)

    env["HOME"] = str(temp_home)
    env["SMOLVM_DATA_DIR"] = str(real_smolvm_data)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def host_git_config(project_root: Path | None = None) -> str | None:
    values: dict[str, str] = {}
    for key in SAFE_GIT_CONFIG_KEYS:
        cmd = ["git", "config", "--global", "--get", key]
        if project_root is not None:
            cmd = ["git", "-C", str(project_root), "config", "--get", key]
        try:
            completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
        except FileNotFoundError:
            return None
        if completed.returncode != 0 and project_root is not None:
            completed = subprocess.run(
                ["git", "config", "--global", "--get", key],
                check=False,
                text=True,
                capture_output=True,
            )
        if completed.returncode == 0:
            value = completed.stdout.strip()
            if value and "\n" not in value:
                values[key] = value
    if not values:
        return None

    sections: dict[str, list[tuple[str, str]]] = {}
    for key, value in values.items():
        section, option = key.split(".", 1)
        sections.setdefault(section, []).append((option, value))

    lines: list[str] = []
    for section, entries in sections.items():
        lines.append(f"[{section}]")
        for option, value in entries:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'\t{option} = "{escaped}"')
        lines.append("")
    return "\n".join(lines)


def parse_managed_env_script(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export ") and "=" in line:
            key, raw_value = line[len("export ") :].split("=", 1)
            with suppress(ValueError):
                env[key] = (shlex.split(raw_value) or [""])[0]
    return env


def sync_forwarded_env_direct_ssh(
    vm_id: str,
    values: dict[str, str],
    missing: list[str],
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> None:
    from smolvm.env import ENV_FILE, build_env_script

    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    ssh_cmd = ssh(vm_id)
    completed = run_capture([*ssh_cmd, f"cat {shlex.quote(ENV_FILE)} 2>/dev/null || true"])
    if completed is None:
        raise ConfigError("failed to sync environment: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to read environment: {stderr}")

    env = parse_managed_env_script(completed.stdout)
    env.update(values)
    for name in missing:
        env.pop(name, None)

    encoded = base64.b64encode(build_env_script(env).encode("utf-8")).decode("ascii")
    write_script = f"""
set -eu
_t=$(mktemp /tmp/.smolvm_env.XXXXXXXXXX)
trap 'rm -f "$_t"' EXIT
printf %s {shlex.quote(encoded)} | base64 -d > "$_t"
chmod 0644 "$_t"
mv "$_t" {shlex.quote(ENV_FILE)}
"""
    completed = run_capture([*ssh_cmd, "bash -lc " + shlex.quote(write_script)])
    if completed is None:
        raise ConfigError("failed to sync environment: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to write environment: {stderr}")


def sync_forwarded_env(
    vm_id: str,
    names: list[str],
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    if not names:
        return
    values = {name: os.environ[name] for name in names if name in os.environ}
    missing = [name for name in names if name not in os.environ]

    from smolvm.facade import SmolVM

    probe = SmolVM.from_id(vm_id)
    try:
        info = getattr(probe, "_info", None)
        comm_channel = getattr(getattr(info, "config", None), "comm_channel", None)
    finally:
        probe.close()

    if comm_channel is None:
        sync_forwarded_env_direct_ssh(vm_id, values, missing, run_capture=run_capture, ssh=ssh)
        return

    vm = SmolVM.from_id(vm_id)
    try:
        if values:
            vm.set_env_vars(values)
        if missing:
            vm.unset_env_vars(missing)
    finally:
        vm.close()


def host_timezone() -> str:
    zoneinfo = Path("/usr/share/zoneinfo")
    try:
        target = Path("/etc/localtime").resolve()
        return target.relative_to(zoneinfo).as_posix()
    except (OSError, ValueError):
        pass
    try:
        timezone = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        return "UTC"
    return timezone or "UTC"


def sync_guest_clock(
    vm_id: str,
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    timezone = host_timezone()
    script = f"""
set -eu
zone={shlex.quote(timezone)}
if [ -f "/usr/share/zoneinfo/$zone" ]; then
  ln -sf "/usr/share/zoneinfo/$zone" /etc/localtime
  printf '%s\n' "$zone" > /etc/timezone
fi
date -u -s {shlex.quote(timestamp)}
"""
    cmd = ssh(vm_id)
    cmd.append("bash -lc " + shlex.quote(script))
    completed = run_capture(cmd)
    if completed is None:
        raise ConfigError("failed to sync VM clock: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to sync VM clock: {stderr}")


def set_hostname(
    vm_id: str,
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    hostname = validate_vm_name(vm_id)
    script = r"""
set -eu
hostname "$1"
printf '%s\n' "$1" > /etc/hostname
if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
  sed -i "s/^127\\.0\\.1\\.1.*/127.0.1.1 $1/" /etc/hosts
else
  printf '127.0.1.1 %s\n' "$1" >> /etc/hosts
fi
"""
    cmd = ssh(vm_id)
    cmd.append(
        "bash -s -- " + shlex.quote(hostname) + " <<'SBX_HOSTNAME'\n" + script + "SBX_HOSTNAME"
    )
    completed = run_capture(cmd)
    if completed is None:
        raise ConfigError("failed to set VM hostname: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to set VM hostname: {stderr}")


def install_git_config(
    vm_id: str,
    user: str | None,
    git_config_text: str | None,
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    if not git_config_text:
        return
    if user is None:
        home = "/root"
        owner = "root:root"
    else:
        quoted_user = shlex.quote(user)
        home = f"/home/{quoted_user}"
        owner = f"{quoted_user}:{quoted_user}"

    encoded = base64.b64encode(git_config_text.encode("utf-8")).decode("ascii")
    script = f"""
set -eu
install -d {shlex.quote(home)}
printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(home)}/.gitconfig
chown {owner} {shlex.quote(home)}/.gitconfig
chmod 600 {shlex.quote(home)}/.gitconfig
"""
    cmd = ssh(vm_id)
    cmd.append("bash -lc " + shlex.quote(script))
    completed = run_capture(cmd)
    if completed is None:
        raise ConfigError("failed to install git config: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to install git config: {stderr}")


def prepare_run_user(
    vm_id: str,
    user: str,
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    quoted_user = shlex.quote(user)
    home = f"/home/{quoted_user}"
    script = f"""
set -eu
host="$(hostname)"
if [ -n "$host" ] && ! getent hosts "$host" >/dev/null 2>&1; then
  printf '127.0.1.1 %s\n' "$host" >> /etc/hosts
fi
if ! id -u {quoted_user} >/dev/null 2>&1; then
  useradd -m -s /bin/bash {quoted_user}
fi
install -d -o {quoted_user} -g {quoted_user} {home}
for p in .ssh .pi .codex .claude .claude.json; do
  if [ -e /root/$p ]; then
    rm -rf {home}/$p
    cp -a /root/$p {home}/$p
  fi
done
chown -R {quoted_user}:{quoted_user} {home}
"""
    cmd = ssh(vm_id)
    cmd.append("bash -lc " + shlex.quote(script))
    completed = run_capture(cmd)
    if completed is None:
        raise ConfigError(f"failed to prepare run user {user!r}: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to prepare run user {user!r}: {stderr}")


def attach(
    vm_id: str,
    launch_command: str,
    user: str | None = None,
    cwd: str | None = None,
    *,
    run: Callable[[list[str]], int] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> int:
    from smolvm.env import ENV_FILE

    run = run or runtime.run
    ssh = ssh or ssh_command
    cmd = ssh(vm_id)
    cd_prefix = f"cd {shlex.quote(cwd)} || exit; " if cwd is not None else ""
    remote = (
        f"{cd_prefix}[ -r {ENV_FILE} ] && . {ENV_FILE}; "
        'export PATH="$HOME/.local/bin:$PATH"; '
        f"exec {launch_command}"
    )
    if user is not None:
        remote = f"sudo -iu {shlex.quote(user)} bash -lc " + shlex.quote(remote)
    cmd.insert(-1, "-t")
    cmd.append(remote)
    return run(cmd)
