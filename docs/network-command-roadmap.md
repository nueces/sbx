# Network command notes

`auth-port` is an expert/troubleshooting operation, not a daily top-level command.

Networking helpers live under:

```bash
sbx network ...
```

## Current commands

### `sbx network forward [NAME] SPEC...`

Forwards host TCP ports to a running sandbox in the foreground. Press Ctrl-C to stop all forwards.

```bash
sbx network forward 3000
sbx network forward 8080:3000
sbx network forward 0.0.0.0:3000:3000
sbx network forward 3000 8080:80
sbx network forward my-sbx 3000 8080:80
```

`SPEC` is one of:

```text
3000              host 127.0.0.1:3000 -> guest 127.0.0.1:3000
8080:80           host 127.0.0.1:8080 -> guest 127.0.0.1:80
0.0.0.0:8080:80   host 0.0.0.0:8080 -> guest 127.0.0.1:80
```

Configured forwards live in `.sbx.toml` and are applied when the VM starts:

```toml
[sbx]
port_forwards = ["3000", "8080:3000"]
```

### `sbx network status NAME`

Shows networking details and auth callback tunnel state for one sandbox.

Example output:

```text
Sandbox: pi-sbx
Status: running
Backend: qemu
Guest IP: 10.0.2.15
SSH Port: 2200
Auth callback: active
Auth detail: pid 12345, localhost:1455 -> guest:1455
```

If the auth callback port is listening but the process is not tracked by `sbx`, status reports:

```text
Auth callback: busy/untracked
```

### `sbx network auth-port NAME`

Opens the OAuth callback tunnel for an already-running sandbox:

```text
host localhost:1455 -> guest localhost:1455
```

The tunnel PID is tracked in:

```text
~/.local/state/sbx/tunnels.json
```

The command is idempotent when the tracked tunnel already exists.

If the host port is already listening but not tracked, `sbx` treats the port as open but does not take ownership of that process.

### `sbx network close-auth-port NAME`

Closes the tracked OAuth callback tunnel for a sandbox.

This command only closes tunnels tracked by `sbx`. It intentionally does not kill arbitrary processes listening on the same port.

## Design principle

Top-level commands should reflect normal user intent:

```bash
sbx run pi-sbx
sbx create pi-sbx
sbx shell pi-sbx
sbx ls
sbx rm pi-sbx
```

Expert operational/debug functionality should live under namespaces such as:

```bash
sbx network ...
```
