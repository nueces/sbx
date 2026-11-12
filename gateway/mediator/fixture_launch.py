"""Test-only launcher: local protocol fixtures may resolve to loopback.

Production launch.py never imports this module or enables this exception.
"""

from __future__ import annotations

import sys
from pathlib import Path

production_launcher = Path(sys.argv.pop(1)).resolve(strict=True)
sys.path.insert(0, str(production_launcher.parent))

import launch  # noqa: E402

production_command = launch.command


def fixture_command(confdir: Path, *args, **kwargs):
    addon = confdir / "fixture_addon.py"
    production_addon = production_launcher.with_name("sbx_gateway_mediator.py")
    addon.write_text(
        "import ipaddress, runpy, sys\n"
        f"sys.path.insert(0, {str(production_launcher.parent)!r})\n"
        f"namespace = runpy.run_path({str(production_addon)!r})\n"
        "addons = namespace['addons']\n"
        "environment = addons[0].server_connect.__func__.__globals__\n"
        "production_check = environment['_destination_allowed']\n"
        "def fixture_check(address):\n"
        "    return ipaddress.ip_address(address).is_loopback or production_check(address)\n"
        "environment['_destination_allowed'] = fixture_check\n"
    )
    command = production_command(confdir, *args, **kwargs)
    command[command.index("-s") + 1] = str(addon)
    return command


launch.command = fixture_command
launch.main()
