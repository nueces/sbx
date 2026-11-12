# Agent guidance

## Coding style

- Avoid local imports inside functions. Prefer module-level imports so dependencies are visible and easier to test. If a local import seems necessary because of a circular import, treat that as a code-organization problem: surface it explicitly so we can decide how and when to do the proper refactor rather than hiding it in the implementation.
- Keep user-facing configuration small and explicit. Avoid adding multiple narrowly-scoped options when one higher-level concept is enough.

## Testing

Follow the test organization, conventions, setup, and commands in
[`docs/development.md`](docs/development.md).
