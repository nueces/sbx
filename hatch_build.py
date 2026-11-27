from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version, build_data
        # Check only: editable builds run against the source tree, and hatchling already
        # copies the execute bit into the wheel.
        directory = Path(self.root) / "src/sbx/gateway_runtime/bin"
        workers = list(directory.glob("sbx-gateway-*"))
        if not workers:
            raise RuntimeError(
                f"no gateway worker found in {directory}; run "
                "gateway/scripts/build_release_artifacts.sh or restore it with git checkout"
            )
        for worker in workers:
            if not worker.stat().st_mode & 0o100:
                raise RuntimeError(
                    f"{worker} is not executable; run gateway/scripts/build_release_artifacts.sh "
                    "or restore it with git checkout"
                )
