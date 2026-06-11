# Draft: Per-mount read-only/write-through support

## Context

`sbx` currently supports mounting host directories into SmolVM sandboxes through:

- `[sbx].mount` / `--mount`
- `[sbx].project_path` / `--project-path`
- `[sbx].writable_mounts` / `--writable-mounts`

The current behavior is limited because `--writable-mounts` is global: if enabled, every mount is writable to the host. This makes it hard to express the common desired setup:

- project directory: writable/write-through
- selected host directories: read-only or guest-local overlay

Example desired configuration:

```toml
[sbx]
project_path = "."              # writable to host
readonly_mount = ["~/.ssh:/host-ssh"]
mount = ["~/docs:/docs"]         # possibly read-only by default
```

## SmolVM behavior observed

SmolVM already has internal per-mount support through `WorkspaceMount.writable`.

Relevant installed package files inspected:

- `smolvm/types.py`
- `smolvm/facade.py`
- `smolvm/cli/main.py`

`WorkspaceMount` documents this behavior:

- `writable=False`:
  - host directory is exposed read-only through QEMU virtio-9p
  - guest gets a writable overlay
  - guest writes do not affect the host
- `writable=True`:
  - host directory is mounted write-through
  - guest writes are visible on the host

Relevant SmolVM parser:

```py
def _parse_mount_specs(specs: list[str], *, writable: bool = False) -> list[WorkspaceMount]:
    ...
    mounts.append(
        WorkspaceMount(host_path=Path(host_str), guest_path=guest_path, writable=writable)
    )
```

Current SmolVM CLI only exposes global control:

```bash
--mount HOST_PATH[:GUEST_PATH]
--writable-mounts
```

So the SmolVM Python API supports per-mount RO/RW, but the CLI path used by normal `sbx run` does not expose it yet.

## Implementation options

### Option A: Extend SmolVM CLI with explicit flags

Preferred CLI shape:

```bash
smolvm pi start --mount-ro ~/.ssh:/host-ssh
smolvm pi start --mount-rw ./project:/workspace
```

Possible semantics:

- `--mount`: existing behavior, affected by `--writable-mounts` for compatibility
- `--mount-ro`: always `WorkspaceMount(writable=False)`
- `--mount-rw`: always `WorkspaceMount(writable=True)`
- `--writable-mounts`: legacy/global flag for existing `--mount` entries only

This avoids ambiguous suffix parsing and is easier to keep compatible.

Estimated difficulty: small/medium.

Likely SmolVM changes:

1. Add CLI args in `smolvm/cli/main.py` wherever `--mount` and `--writable-mounts` are registered.
2. Add a helper to combine mount groups:

   ```py
   readonly = _parse_mount_specs(args.mount_ro or [], writable=False)
   readwrite = _parse_mount_specs(args.mount_rw or [], writable=True)
   legacy = _parse_mount_specs(args.mounts or [], writable=args.writable_mounts)
   workspace_mounts = [*legacy, *readonly, *readwrite]
   ```

3. Pass `workspace_mounts` through `VMConfig`, or adjust facade handling so the combined per-mount list is used.
4. Add tests for mixed RO/RW mounts.
5. Update SmolVM docs/help text.

### Option B: Add suffix syntax to `--mount`

Example:

```bash
smolvm pi start --mount ./project:/workspace:rw
smolvm pi start --mount ~/.ssh:/host-ssh:ro
```

This is compact, but parsing is trickier because SmolVM currently splits mount specs on the last colon to preserve future Windows path compatibility:

```py
host_str, guest_path = spec.rsplit(":", 1)
```

Adding `:ro` / `:rw` would require careful parsing and escaping rules. This is likely more error-prone than `--mount-ro` / `--mount-rw`.

Estimated difficulty: medium.

### Option C: Make `sbx` bypass SmolVM CLI and use Python API directly

`sbx` could construct `WorkspaceMount` objects directly and use SmolVM's Python API for all normal starts.

This would allow per-mount control without changing SmolVM CLI, but it is more invasive for `sbx` because the normal code path currently shells out to preset commands such as:

```bash
smolvm pi start ...
```

Estimated difficulty: medium/high compared to extending SmolVM CLI.

## Suggested future `sbx` interface

After SmolVM CLI supports per-mount control, `sbx` can expose:

```bash
sbx run --mount-ro ~/.ssh:/host-ssh
sbx run --mount-rw ./project:/workspace
```

Config example:

```toml
[sbx]
project_path = "."              # remains writable by default
mount_ro = ["~/.ssh:/host-ssh"]
mount_rw = ["~/scratch:/scratch"]
mount = ["~/docs:/docs"]
```

Possible semantics:

- `project_path`: same-path mount, writable/write-through, used as working directory
- `mount_ro`: guest can read and optionally write to overlay, but host is protected
- `mount_rw`: guest writes propagate to host
- `mount`: legacy mount list, affected by `writable_mounts` for compatibility

## Recommendation

Implement Option A in SmolVM first: add `--mount-ro` and `--mount-rw` CLI flags.

Then update `sbx` to pass through separate mount groups. This keeps compatibility with existing `--mount` / `--writable-mounts` behavior and avoids ambiguous `:ro` / `:rw` parsing.
