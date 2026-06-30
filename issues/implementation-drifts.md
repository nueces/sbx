# Implementation drift findings

## Findings requiring implementation/documentation correction

### README install example pins an older tag

Current implementation version is `0.2.1`:

```text
pyproject.toml: version = "0.2.1"
src/sbx/__init__.py: __version__ = "0.2.1"
```

`webpage/main/index.html` also shows `v0.2.1` in both release markers.

`main/README.md` still shows:

```bash
uv tool install git+https://github.com/nueces/sbx.git@v0.2.0
```

Recommended correction: update README to `v0.2.1`, or make the release bump flow update README too if the README should always show the latest released tag.

## Spec drift corrected in this branch

### Docker install spec had obsolete SmolVM install guidance

`specs/docker-install-design.md` still instructed users to install SmolVM separately:

```bash
uv tool install 'smolvm==0.0.19'
```

Current implementation pins SmolVM as the `sbx` package dependency (`smolvm==0.0.24.post2`), so users should install `sbx` and let the dependency resolve normally.

The spec was updated to remove the separate SmolVM install command.
