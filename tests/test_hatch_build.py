import importlib.util
import sys
import types
from pathlib import Path

import pytest


def load_hook(monkeypatch, root):
    interface = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = type("BuildHookInterface", (), {})
    monkeypatch.setitem(sys.modules, "hatchling.builders.hooks.plugin.interface", interface)
    path = Path(__file__).resolve().parents[1] / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("hatch_build_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook = object.__new__(module.CustomBuildHook)
    hook.root = str(root)
    return hook


def workers(tmp_path, modes):
    bin_dir = tmp_path / "src/sbx/gateway_runtime/bin"
    bin_dir.mkdir(parents=True)
    paths = []
    for index, mode in enumerate(modes):
        path = bin_dir / f"sbx-gateway-{index}"
        path.write_bytes(b"worker")
        path.chmod(mode)
        paths.append(path)
    return paths


def test_hook_accepts_executable_workers_without_changing_modes(monkeypatch, tmp_path):
    paths = workers(tmp_path, [0o555, 0o755])
    (paths[0].parent / ".DS_Store").write_bytes(b"")  # stray non-binary files are ignored
    load_hook(monkeypatch, tmp_path).initialize("editable", {})
    assert [path.stat().st_mode & 0o777 for path in paths] == [0o555, 0o755]


def test_hook_rejects_non_executable_worker_without_changing_it(monkeypatch, tmp_path):
    (path,) = workers(tmp_path, [0o644])
    with pytest.raises(RuntimeError, match="not executable"):
        load_hook(monkeypatch, tmp_path).initialize("editable", {})
    assert path.stat().st_mode & 0o777 == 0o644
