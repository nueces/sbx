"""Runtime patches for the pinned smolvm release.

shortcut: raises smolvm 0.0.28's VMConfig.memory cap from 16384 to 32768 MiB (upstream
Celesto #605); delete this module when migrating to celesto>=0.1.3.
"""

from annotated_types import Le
from smolvm.types import SnapshotInfo, VMConfig, VMInfo

MAX_MEMORY_MIB = 32768

_memory = VMConfig.model_fields["memory"]
_memory.metadata = [Le(MAX_MEMORY_MIB) if isinstance(m, Le) else m for m in _memory.metadata]
# VMInfo and SnapshotInfo embed VMConfig's schema, so they must be rebuilt too.
for _model in (VMConfig, VMInfo, SnapshotInfo):
    _model.model_rebuild(force=True)
