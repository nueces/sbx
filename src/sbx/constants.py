import os
from pathlib import Path

DEFAULT_BACKEND = "qemu"
DEFAULT_BOOT_TIMEOUT = 30.0
SBX_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "sbx"
TUNNELS_FILE = SBX_STATE_DIR / "tunnels.json"
SESSIONS_FILE = SBX_STATE_DIR / "sessions.json"
SMOLVM_DB_PATH = Path.home() / ".local" / "state" / "smolvm" / "smolvm.db"
