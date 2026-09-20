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
- A commit whose subject contains a `\x1e` byte no longer breaks every command with a bare
  `not enough values to unpack`. Commit metadata was split on `\x1e`, which a subject may
  legitimately contain; records are now separated by newline, the one byte git guarantees
  is absent (it joins a multi-line subject with spaces and strips newlines from an ident).
  Not `str.splitlines()`, which also breaks on `\x1c`-`\x1e`, `\x85` and U+2028/9.
- A Python file too deeply nested for `ast.parse` (generated code: a chain of thousands of
  `+`) raised `RecursionError` out of `scopes_from_source` and aborted the whole analysis
  *after* both commits had been measured. Unparsable now means "no scopes in this file",
  exactly as a syntax error already did.
- `--cache-input` pointing at a device or FIFO hung for ever with nothing printed, since
  the read never reaches EOF. Only regular files are read now; a declared directory already
  filtered these out.
- Source lines that read like diff headers no longer break blame. Inside a hunk every line
  carries a `-`/`+` prefix, so a deleted `-- "note` arrives as `--- "note`: memblame read it
  as a file header, which aborted the whole analysis with a `SyntaxError` traceback on the
  unterminated quote, and (with a matching `++ ` line) blamed later hunks on a path that
  does not exist. File headers are now only read between `diff --git` and the first `@@`,
  and an unparsable quoted name is used verbatim instead of raising.
- A half-written workload (`call:`, `call:mod`, `script:`, or one with unbalanced quotes)
  is rejected before any checkout or subprocess. It used to fail inside the runner and
  reach the user as a truncated Python traceback reported as an unmeasurable commit. A bare
  `pytest:` still means the whole suite.
- The HTML report's range chart divided by zero when every measured value was 0. Not
  reachable through the CLI (even a no-op workload measures ~260 KB), but `artifact.render`
  takes any schema-1 document, and the extension's chart already guarded this case.

### Added

- `py.typed`, so type checkers use the annotated public API (`PublicResult`), and the
  package now actually type-checks: `mypy` is clean and runs in CI, so the annotations
  shipped under that marker stay true. The fixes were real, not silencing — `Meter`'s
  poller attributes were annotated `None` but hold a `Thread`/`Event`, `_result_status`
  returns the contract's `Literal`, and the renderers take `Mapping[str, Any]`, which says
  what they already did: read, never mutate. `runner.py` gains no runtime import from this
  (verified: it still loads zero modules beyond a bare interpreter's).
- A `toml` extra: `pip install "memblame[toml]"` makes `memblame.toml` and
  `[tool.memblame]` work on Python 3.9/3.10, where the standard library has no TOML
  parser. The core stays dependency-free and the without-it path still explains itself.
- The extension renders a `run` result as a measurement table; it used to fall through to
  a raw JSON dump.
- Two things that used to need a person on Windows are now tested. Cancelling is covered by
  a test that cancels a real run mid-measurement and checks the worktree is gone, and the
  Python extension's API glue is a pure function (`selectedFromPythonApi`) exercised against
  every shape it returns, including a missing API, an unresolvable environment and a
  rejecting `resolveEnvironment` -- all of which mean "no selection", leaving the CLI's own
  interpreter discovery in charge rather than failing the command.
- `memblame.contract.ErrorResult` / `error_output()`: the `{"kind": "error"}` document that
  `--json` prints on failure is now part of the typed schema-1 contract.

### Internal

- Removed an unreachable measurement-failure fallback in the CLI.
- `vscode-ext/src/contract.ts` mirrors the added fields; `steps` is typed as the step list
  (`range`) or the step count (`bisect`) it actually is, instead of always a number.
- The release workflow checks the tag against `vscode-ext/package.json` as well as the
  package version, so a tag cannot publish a mismatched extension.
- `bundle-python.js` copies `py.typed` into the bundled engine.
- The extension's integration harness and the screenshot script remove their temp
  directories; each run used to leave one behind (the harness, a fixture repo plus a VS Code
  user-data dir, tens of MB).
- README: the GitHub job-summary recipe appends with `>>` instead of `-o
  "$GITHUB_STEP_SUMMARY"`, which replaced whatever an earlier step in the job had written.

## 0.1.0

- First release: `run`, `diff`, `range` and `bisect` over git history, with function-level
  attribution, an on-disk cache, Markdown/HTML reports and a schema-1 JSON contract.
