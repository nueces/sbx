from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version, build_data
        # Check only: editable builds run against the source tree, and hatchling already
        # copies the execute bit into the wheel.
        for worker in (Path(self.root) / "src/sbx/gateway_runtime/bin").glob("sbx-gateway-*"):
            if worker.stat().st_mode & 0o111 != 0o111:
                raise RuntimeError(
                    f"{worker} is not executable; run gateway/scripts/build_release_artifacts.sh "
                    "or restore it with git checkout"
                )
