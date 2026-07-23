# Gateway development

Use Go 1.26.5 with `GOTOOLCHAIN=local` and uv 0.11.30 with CPython 3.12.13.

```sh
GOTOOLCHAIN=local go mod verify
GOTOOLCHAIN=local go test ./...
uvx --from uv==0.11.30 uv sync --directory mediator --locked --no-build --python 3.12.13
uvx --from uv==0.11.30 uv audit --directory mediator --locked
uvx --from uv==0.11.30 uv run --directory mediator --locked python -c \
  'from importlib.metadata import distributions; print("\\n".join(sorted("{}=={}: {}".format(d.metadata["Name"], d.version, d.metadata.get("License-Expression") or d.metadata.get("License") or "UNKNOWN") for d in distributions())))'
```
