# Changelog

The VS Code extension keeps its own log in [vscode-ext/CHANGELOG.md](vscode-ext/CHANGELOG.md).

## Unreleased

### Fixed

- Adaptive `range` no longer reports `[k/N]` progress against the whole range; only
  `--all` knows its total, so the editor's progress bar no longer stalls part-way.
- `diff` against the working tree no longer aborts, after measuring, on an untracked `*.py`
  that cannot be read (for example a dangling symlink).
- `--json -o PATH` now writes the error document to `PATH` too, instead of leaving the file
  missing when a command fails.
- Cache-key construction (`environment_fingerprint`) now times out like `check_interpreter`
  instead of hanging on a wedged interpreter.

### Added

- `py.typed`, so type checkers use the annotated public API (`PublicResult`).
- `memblame.contract.ErrorResult` / `error_output()`: the `{"kind": "error"}` document that
  `--json` prints on failure is now part of the typed schema-1 contract.

### Internal

- Removed an unreachable measurement-failure fallback in the CLI.

## 0.1.0

- First release: `run`, `diff`, `range` and `bisect` over git history, with function-level
  attribution, an on-disk cache, Markdown/HTML reports and a schema-1 JSON contract.
