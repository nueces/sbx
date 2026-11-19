from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version, build_data
        for worker in (Path(self.root) / "src/sbx/gateway_runtime/bin").iterdir():
            worker.chmod(0o755)
