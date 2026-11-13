# OAuth callback forwarding

`sbx` forwards browser callback traffic over SSH; it does not perform authentication or integrate with Auth0. All callback forwards bind host `127.0.0.1` and target guest `127.0.0.1`.

## Profiles

| Profile | Host and guest ports |
| --- | --- |
| `openai` | 1455 |
| `anthropic` | 53692 |
| `both` | 1455 and 53692 |
| `none` | No forwarding |
| `auto` | Provider hint, otherwise selected-agent defaults |

`auto` uses `--auth-provider=openai|anthropic` or `[sbx].auth_provider` first. Without a hint, Claude selects `anthropic`, Codex selects `openai`, and Pi/OpenCode select `both`. The provider hint does not change the agent's model or read agent-native configuration. Claude/OpenAI and Codex/Anthropic hints are rejected; explicit port profiles remain available as low-level overrides.

These profiles are forwarding defaults, not universal provider callback ports. A client may use a different port or login mechanism.

## Attached sessions

```bash
sbx run --auth-port                 # explicit auto, even on later runs
sbx run --auth-port=anthropic
sbx run --auth-port=auto --auth-provider=openai
sbx run --auth-port=none
```

With no auth-port setting, forwarding opens only after an attached run successfully provisions a previously unprovisioned VM. Later attachments and reprovisioning of an already provisioned VM do not implicitly forward. Provisioning completed with `create` or `--no-attach` does not defer this automatic opportunity to a later attachment.

`create`, `run --no-attach`, `shell`, and diagnostic-root sessions do not open session tunnels, even with enabled config. A provider hint alone never enables forwarding on subsequent runs.

Session-created tunnels close on normal exit, attachment failure, startup failure, or interruption. Reused/manual tunnels are not owned by the new session and are left untouched. An original session closes its own tunnels when it exits, even if another session has reused them; use manual tunnels when forwarding must outlive a particular session.

Forwarding starts after installation, provisioning checks, and successful provisioning metadata are written. If tunnel startup fails afterward, provisioning remains complete; retry with an explicit `--auth-port`.

## Configuration and compatibility

```toml
[sbx]
auth_port = "auto"           # enable forwarding on every attached run
auth_provider = "anthropic"  # forwarding hint, not model configuration
```

CLI values override config. An omitted auth_port means first-provision-only behavior; `auth_port = "auto"` enables forwarding on every attached run, and `auth_port = "none"` disables forwarding. This intentionally changes the omitted-setting default from every-run forwarding.

Only the named string profiles are accepted, and TOML requires quotes. Other values produce a configuration error. A valid explicit CLI profile takes precedence over the config value for that invocation without changing the file.

For a single profile, numeric overrides can change either side independently:

```toml
[sbx]
auth_port = "anthropic"
auth_host_port = 53692
auth_guest_port = 6000
```

Ports must be integers in `1..65535`. Numeric overrides combined with `both` (including an `auto` that resolves to both) are rejected as ambiguous; select a single profile first.

## Persistent manual forwarding

```bash
sbx network auth-port --auth-port=both
sbx network auth-port --auth-port=anthropic --guest-port=6000
sbx network auth-port OTHER_VM --auth-port=openai --replace
sbx network status --json
sbx network status --host-port=53692
sbx network close-auth-port
```

Network commands use the configured sandbox unless given a name. Manual auth-port uses the same config, provider hint, and selected/default agent resolution. With no selector/config it uses `auto`. CLI `--host-port` and `--guest-port` override configured numeric values. Manual tunnels persist independently of attached sessions; close-auth-port closes all tracked auth tunnels for that VM.

Live matching tunnels are reused. An occupied port produces a warning without killing an untracked process or another VM's tunnel. Manual `--replace` replaces only a conflicting tracked tunnel, never other profiles or untracked processes. A warning is not proof that login will succeed: free the port or deliberately replace its tracked owner.

Tunnel state in `~/.local/state/sbx/tunnels.json` supports multiple forwards per VM. Legacy single-tunnel state is read automatically and converted on the next state mutation. State changes are locked, cleanup checks PID and ports, and `sbx doctor --fix` removes stale entries.

## Runtime installers

`runtime_install` accepts `pi`, `specify`, `claude`, `opencode`, and `codex`. Claude, OpenCode, and Codex install their documented npm packages as the runtime user under `$HOME/.local`, allowing package lifecycle scripts for native binaries and verifying executable discovery afterward. This executes third-party code as that user. Existing executables are skipped; list removal never uninstalls tools. Installation runs only during initial or explicitly requested provisioning.

Use `--agent opencode`, `--agent claude`, or `--agent codex` to select the launch command independently of installation. No additional host credential copying is introduced.
