# Release action design

The release action is part of the current project release infrastructure. Its normative design is [`project-release-design.md`](project-release-design.md).

That design includes the complete workflow:

- development package versions use `X.Y.Z.devN`,
- the manual action opens `release/vX.Y.Z` with a final `X.Y.Z` version,
- release validation permits the source version removed by the PR to be either final or `.devN`, while requiring the resulting version to match the final branch version,
- publishing repeats validation before creating tag and release `vX.Y.Z`,
- publishing then opens the website update PR and the next `X.Y.(Z+1).dev0` PR,
- protected branches are changed only through pull requests.

Publishing to a package index and publishing pre-release or build-metadata tags remain out of scope.
