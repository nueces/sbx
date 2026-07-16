# Local port forwarding design

## Goal

Let a user open a port on the host for a server running inside an `sbx` VM, similar to SSH or Kubernetes port forwarding.

## Names

User-facing feature name: **port forwarding**.

CLI command:

```bash
sbx network forward ...
```

Configuration key:

```toml
[sbx]
port_forwards = ["3000"]
```

## Address forms

Support these TCP forms:

```text
GUEST_PORT
HOST_PORT:GUEST_PORT
BIND_HOST:HOST_PORT:GUEST_PORT
```

Examples:

```bash
sbx network forward 3000
sbx network forward 8080:3000
sbx network forward 0.0.0.0:3000:3000
```

Meanings:

```text
3000                 => 127.0.0.1:3000 -> guest 127.0.0.1:3000
8080:3000            => 127.0.0.1:8080 -> guest 127.0.0.1:3000
0.0.0.0:3000:3000    => 0.0.0.0:3000 -> guest 127.0.0.1:3000
```

Only TCP is in scope.

## Ad-hoc forwarding behavior

`sbx network forward` is foreground by default and stays attached to the current terminal:

```text
Forwarding 127.0.0.1:3000 -> guest 127.0.0.1:3000
Press Ctrl-C to stop.
```

Stopping the `sbx network forward` process closes the tunnel. There is no `stop-forward`, no PID tracking, and no background mode in the first version. Users who really need background behavior can use their shell:

```bash
sbx network forward 3000 &
```

Implementation: run an SSH local forward in the foreground:

```bash
ssh -N -L 127.0.0.1:3000:127.0.0.1:3000 ...
```

## Configured forwarding behavior

Durable forwards live in `.sbx.toml`:

```toml
[sbx]
port_forwards = [
  "3000",
  "8080:3000",
  "0.0.0.0:3000:3000",
]
```

`sbx run` and `sbx shell` apply configured forwards when starting the VM.

Implementation: pass QEMU `hostfwd` entries through SmolVM startup configuration.

If the VM is already running, changed `port_forwards` do not mutate it. The user must restart the VM:

```bash
sbx stop
sbx run
```

## Out of scope

- `stop-forward` command.
- Built-in background/daemon mode.
- PID tracking for ad-hoc forwards.
- Listing ad-hoc forwards.
- UDP forwarding.
- Custom guest bind host; guest target is always `127.0.0.1`.
- Applying changed configured forwards to an already-running VM.

## Module split

Network behavior should live outside the top-level CLI module now that it owns more than one operation.

Target modules:

```text
src/sbx/constants.py  # shared dumb constants
src/sbx/config.py     # tiny shared config access/name resolution helpers
src/sbx/runtime.py    # shared process, SSH, JSON, PID helpers
src/sbx/network.py    # network command implementations and network helpers
src/sbx/cli.py        # argparse and top-level workflows
```

`cli.py` keeps argparse command layout and wires network parsers to `sbx.network` command functions.

`network.py` owns:

- `cmd_forward`
- `cmd_auth_port`
- `cmd_close_auth_port`
- `cmd_status`
- port-forward parsing and conversion for SmolVM config
- foreground SSH forwarding
- auth tunnel tracking and lifecycle
- network status formatting

Shared constants/helpers move only when needed to avoid import cycles. No full CLI modularization is in scope.
