# Pi package / Node version issue

## Context

While testing `sbx` with a VM named `pi-sbx`, the Pi agent launched inside the VM reported paths and update metadata from the old package namespace:

```text
/usr/lib/node_modules/@mariozechner/pi-coding-agent/docs/providers.md
/usr/lib/node_modules/@mariozechner/pi-coding-agent/docs/models.md

Update Available
New version 0.78.1 is available. Run pi update
Changelog: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/CHANGELOG.md
```

Expected/current package namespace appears to be:

```text
@earendil-works/pi-coding-agent
```

Observed npm metadata:

```text
@mariozechner/pi-coding-agent       0.73.1  repository: badlogic/pi-mono
@earendil-works/pi-coding-agent    0.78.1  repository: earendil-works/pi
```

## Reproduction notes

1. Start/create the VM through `sbx`.
2. Attach to it with Pi.
3. In a second terminal, enter the VM:

   ```bash
   sbx shell pi-sbx
   ```

4. Close the first terminal where `sbx run pi-sbx` was running.
5. In the second terminal, inside the VM, force-install the new package:

   ```bash
   npm install -g @earendil-works/pi-coding-agent --force
   ```

6. Run Pi again inside the VM:

   ```bash
   pi
   ```

7. Pi reports a Node version incompatibility:

   ```text
   Update Requires Newer Node
   New version 0.78.1 is available. Pi 0.75.0 and newer require Node >= 22.19.0.
   Current Node is v20.20.2. Update Node, then run pi update again.
   Changelog: https://github.com/earendil-works/pi-mono/blob/main/packages/coding-agent/CHANGELOG.md
   ```

## Analysis

There are two related but separate issues.

### 1. SmolVM Pi preset/prebuilt image is stale

The VM initially has Pi 0.73.1 from:

```text
@mariozechner/pi-coding-agent
```

That package points to the older GitHub organization/account:

```text
badlogic/pi-mono
```

`sbx` currently delegates agent installation to SmolVM:

```bash
smolvm pi start
```

So this stale package likely comes from SmolVM's Pi preset or prebuilt Pi image, not directly from `sbx`.

### 2. New Pi requires newer Node than the VM provides

The VM currently has:

```text
Node v20.20.2
```

But Pi >= 0.75.0 requires:

```text
Node >= 22.19.0
```

So simply forcing the newer package is insufficient unless the VM also upgrades Node.

SmolVM's Pi preset likely uses a Node 20 bootstrap. In the SmolVM source observed earlier, the Pi preset used a Node 20 setup script (`NODE20_BOOTSTRAP`). That explains why the VM has Node 20.

## Consequences for `sbx`

If `sbx` adds a naive post-start step like:

```bash
npm install -g @earendil-works/pi-coding-agent --force
```

then the install may succeed, but Pi will still warn/fail because Node is too old.

Any `sbx` fix that forces the current Pi package should also ensure Node >= 22.19.0 first.

## Possible solutions

### Option A: Upstream SmolVM fix

Best long-term fix:

- Update SmolVM's Pi preset/prebuilt image to install Node 22+.
- Update package from:

  ```text
  @mariozechner/pi-coding-agent
  ```

  to:

  ```text
  @earendil-works/pi-coding-agent
  ```

This keeps `sbx` simple.

### Option B: `sbx` post-start repair step

`sbx` could optionally run a post-start repair inside the VM before attaching:

1. Detect Node version.
2. Install/upgrade Node to >= 22.19.0 if needed.
3. Install the current Pi package:

   ```bash
   npm install -g @earendil-works/pi-coding-agent --force
   ```

Potential config:

```toml
[sbx]
upgrade_pi = true
pi_package = "@earendil-works/pi-coding-agent"
node_min_version = "22.19.0"
```

Potential CLI flag:

```bash
sbx run pi-sbx --upgrade-pi
sbx run pi-sbx --no-upgrade-pi
```

Open questions:

- Should this be enabled by default?
- How expensive is Node 22 install on every fresh VM?
- Can we cache a repaired image/snapshot instead?
- Which Node installation method should be used in Ubuntu guests? NodeSource, npm/n, fnm, or distro packages?

### Option C: Custom `sbx` image/preset

Build or maintain an `sbx`-specific SmolVM preset/image with:

- Node >= 22.19.0
- `@earendil-works/pi-coding-agent`
- Any desired `sbx` security defaults

This avoids per-start repair time but adds image maintenance.

## Suggested next step

Do not implement a package upgrade blindly yet.

First decide whether `sbx` should:

1. rely on upstream SmolVM to update the preset, or
2. own a compatibility repair step, or
3. own a full image/preset.

If we implement the repair step, it should be explicit/configurable and should upgrade Node before upgrading Pi.
