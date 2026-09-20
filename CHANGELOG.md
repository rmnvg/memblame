# Changelog

The VS Code extension keeps its own log in [vscode-ext/CHANGELOG.md](vscode-ext/CHANGELOG.md).

## Unreleased

### Fixed

- Adaptive `range` no longer reports `[k/N]` progress against the whole range; only
  `--all` knows its total, so the editor's progress bar no longer stalls part-way.
- `diff` against the working tree no longer aborts, after measuring, on an untracked `*.py`
  that cannot be read (for example a dangling symlink).
- `--json -o PATH` now writes the error document to `PATH` on every failure path, including
  "not inside a git repository", instead of leaving the file missing.
- Cache-key construction (`environment_fingerprint`) now times out like `check_interpreter`
  instead of hanging on a wedged interpreter.
- `PublicResult` declares the fields the commands actually emit (`valid`,
  `changed_functions`, `mode`, `measured`, `steps`, `good`, `bad`, `candidates`, `unit`,
  `metric`, `threshold`, `measurements`, `monotonic`, `verified`, `culprit_range`). They
  shipped undeclared, so `py.typed` consumers saw errors on real fields. A test now fails
  if a command emits a key the contract does not declare.
- The `# type: ignore` in `contract.py` had a trailing comment inside the pragma, which made
  mypy report it as invalid rather than honouring it.
- Temp directories a killed run left behind are now reclaimed. `git worktree list` never
  mentions a run killed before `git worktree add` finished, nor the per-run scratch
  directory holding the workload's stdout/stderr — which is uncapped on disk, since only
  its tail is read back — so both used to stay in the temp directory for ever. A directory
  is only removed once the pid it recorded is gone, so concurrent runs keep their own.
- The HTML report's range chart divided by zero when every measured value was 0. Not
  reachable through the CLI (even a no-op workload measures ~260 KB), but `artifact.render`
  takes any schema-1 document, and the extension's chart already guarded this case.

### Added

- `py.typed`, so type checkers use the annotated public API (`PublicResult`).
- `memblame.contract.ErrorResult` / `error_output()`: the `{"kind": "error"}` document that
  `--json` prints on failure is now part of the typed schema-1 contract.

### Internal

- Removed an unreachable measurement-failure fallback in the CLI.
- `vscode-ext/src/contract.ts` mirrors the added fields; `steps` is typed as the step list
  (`range`) or the step count (`bisect`) it actually is, instead of always a number.
- The release workflow checks the tag against `vscode-ext/package.json` as well as the
  package version, so a tag cannot publish a mismatched extension.
- `bundle-python.js` copies `py.typed` into the bundled engine.
- The extension's integration harness removes its temp directory; each run used to leave a
  fixture repo plus a VS Code user-data dir (tens of MB) behind.
- README: the GitHub job-summary recipe appends with `>>` instead of `-o
  "$GITHUB_STEP_SUMMARY"`, which replaced whatever an earlier step in the job had written.

## 0.1.0

- First release: `run`, `diff`, `range` and `bisect` over git history, with function-level
  attribution, an on-disk cache, Markdown/HTML reports and a schema-1 JSON contract.
