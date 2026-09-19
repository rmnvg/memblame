# memblame: git blame for memory

Find **the commit and the function** that made your Python code use more memory.

memblame runs your own workload (a pytest test, a script or a function) at several git
commits, measures memory with `tracemalloc`, and maps any growth to the function and the
diff hunk that caused it.

```
$ memblame range main~9..main -w call:shop.app:run
workload  (median of runs; ▲ = significant change; measured 7 of 10 commits)
  commit         peak      Δpeak    retained       Δret  author / subject
  da9eedf     29.5 MB                11.4 KB             Asha Rao       initial pipeline
  ...
  326798b     57.7 MB  +28.2 MB▲     12.3 KB   +0.1 KB   Rahul Mehta    include raw payload in rows
  7427306     57.7 MB   +0.4 KB      57.7 MB  +57.7 MB▲  Chen Wu        cache summarize results

Findings:
  7427306  retained +57.7 MB  "cache summarize results"
      direct: shop/report.py:7 summarize()  +57.7 MB
        changed in shop/report.py @@ -6,1 +8,4 @@
        memory allocated at shop/parse.py:8  57.7 MB
  326798b  peak +28.2 MB  "include raw payload in rows"
      direct: shop/parse.py:4 load_rows()  +28.6 MB
        changed in shop/parse.py @@ -7,1 +7,2 @@
        hot line shop/parse.py:8  57.3 MB
```

There is also a **VS Code extension** (in `vscode-ext/`) that shows a timeline, CodeLens
above the blamed function, and a one-click "Memory vs HEAD" on every pytest test.

![report](https://raw.githubusercontent.com/rmnvg/memblame/main/vscode-ext/media/report-range.png)

## Install

```
pip install memblame        # or: pipx install memblame
```

No dependencies: the core is standard-library only. Python 3.9+.

## Usage

```
memblame diff                     # working tree (uncommitted changes) vs HEAD
memblame diff main feature        # two revisions
memblame range main~50..main      # timeline over a range (adaptive; --all for every commit)
memblame bisect --good v1.2 --bad HEAD --threshold +20MB
memblame run HEAD                 # one revision, with top allocating functions
```

Pick what to run with `-w` (or `workload` in `[tool.memblame]` in `pyproject.toml`):

| workload | measures |
|---|---|
| `pytest:tests/test_big.py::test_load` | one test (only the test itself: setup, call, teardown) |
| `pytest:tests/test_big.py` | each test in the file, separately |
| `script:bench/run.py --n 10` | a script; the path may be absolute (outside the repo), which keeps the workload identical at every commit |
| `call:mypkg.pipeline:main` | a function |

### Options

| option | default | meaning |
|---|---|---|
| `-w, --workload` | from config | what to run (table above) |
| `-C, --repo` | `.` | repository to analyse |
| `--python` | active venv / conda env, else `.venv`/`venv` in the repo, else the current Python | interpreter with your project's dependencies (3.9+) |
| `--pythonpath DIR` | `src` + `.` if `src/` exists, else `.` | where to import your project from; repeatable |
| `--runs N` | `3` | maximum runs per commit (stops early once two runs agree) |
| `--nframe N` | `16` | traceback depth for attribution; raise it if a verdict notes truncated stacks |
| `--timeout S` | `900` | seconds per run; a commit that takes longer is skipped |
| `--no-cache` | | ignore and don't write `.memblame/cache/` |
| `--json` | | machine-readable output (`"schema": 1`), used by the VS Code extension |
| `range --all` | | measure every commit instead of subdividing adaptively |
| `bisect --good REV` / `--bad REV` | `--bad HEAD` | the range to search |
| `bisect --threshold` | noise band | `200MB` (absolute), `+20MB` or `+10%` (relative to good) |
| `bisect --unit NAME` / `--metric peak\|retained` | the one that grew most | what to track, e.g. a pytest node id |

Exit codes: `0` no significant increase, `3` a significant memory increase was found (handy
in CI), `1` error, `2` not a git repository.

### Configuration

These keys can live in `pyproject.toml` (or in a `memblame.toml` at the repo root, which
wins). Command-line flags override them; a relative `python` path is relative to the repo root.

```toml
[tool.memblame]
workload = "pytest:tests/test_pipeline.py"
runs = 3
nframe = 16
pythonpath = ["src"]
python = ".venv/bin/python"
timeout = 600
threshold = "+10%"   # default for bisect
```

Reading config needs Python 3.11+ (or `pip install tomli` on 3.9/3.10); otherwise memblame
says so and uses the command line only.

## How it works

1. Each commit is checked out into a temporary `git worktree` (your working tree is never
   touched) and the workload runs in a fresh subprocess of **your project's interpreter**.
2. Fast runs measure **peak** and **retained** (still allocated after the run) traced memory.
   They repeat until two runs agree, and the median is reported. `tracemalloc` counts
   are nearly deterministic: on real projects run-to-run noise was a few KB.
3. A change counts only if it exceeds the noise band: `max(2 × spread, 2 % of peak, 64 KiB)`.
4. Only for commits around a significant change, an **attribution run** records
   tracebacks and snapshots memory as it approaches the known peak. It uses a cheap polling
   thread first, and an exact profile hook only if the peak was too short-lived to catch.
   Each allocation is credited to project functions: *own* bytes (allocated in the
   function) and *incl. callees* bytes.
5. The function deltas are matched against `git diff -U0` hunks, on the new side for
   growth and the old side for memory that went away.
   * **direct**: the function whose code changed accounts for the growth.
   * **indirect**: memory grew in code that did not change (the cause is a caller, data or
     config); the changed functions are listed.
6. `range` is adaptive: it measures both ends and only subdivides segments whose ends
   differ, so the work is roughly log₂(N) per change. Results are cached per commit in
   `.memblame/cache/`, keyed by the commit plus the interpreter, installed packages,
   settings and memblame version.

## Honest limits

* `tracemalloc` sees memory allocated through Python's allocators. numpy reports its
  buffers to tracemalloc, so arrays are counted. Native libraries that call `malloc` directly
  are not.
* Tracing is slow: the fast runs are several times slower than normal, and the attribution
  run can be 10–40× slower on allocation-heavy or deeply recursive code. Use small,
  deterministic workloads.
* The environment is fixed: all commits run with the dependencies currently installed. If
  your dependencies changed across the range, results may not be comparable.
* If your project is installed so that imports resolve **outside** the checked-out commit
  (for example `pip install -e .` with a `src/` layout and no `--pythonpath`), memblame
  detects it and reports `invalid environment` instead of wrong numbers.
* pytest workloads always run in-process, in file order and without coverage: memblame
  adds `-n 0` (pytest-xdist), `-p no:randomly` and `--no-cov` (pytest-cov) when those
  plugins are installed.
* A commit where the workload fails or that cannot be measured (crash, timeout) is skipped,
  never reported as a memory change. `bisect` skips it the way `git bisect skip` does.
* Adaptive `range` can miss a change that is exactly undone later within one unsplit
  segment. Use `--all` to measure every commit.
* Attribution names where memory was **allocated**. For "kept alive too long" problems it
  still points at the changed function through the *incl. callees* numbers, and it
  shows where the memory was allocated.

## Tested on real projects

Adaptive `range` runs with a fixed benchmark script, each finding checked against the diff:

| project, range | measured | finding | cause (verified in the diff) |
|---|---|---|---|
| tomlkit `0.11.0..HEAD` (233 commits) | 21 | `231370c` peak **−65 %** (60.6 → 21.1 MB), direct in `Source.__init__` | source is indexed instead of materialized |
| | | `ae1b679` peak **+3.9 %**, direct in `Container.__init__` | a new `dict` and `set` on every `Container` |
| | | `a766d3a` retained **+1.0 MB** at module level in `items.py` | new `import dataclasses` (pulls in `inspect`) |
| pyparsing `3.1.0..HEAD` (510 commits) | 11 | `cd081ef` retained **+1.56 MB (+22 %)**, hot line `pyparsing/testing.py:6` | `import unittest` added; since 3.3.0 every `import pyparsing` loads `unittest` |
| markdown-it-py `v2.0.0..HEAD` (136 commits) | 19 | `f52249e` peak **−15 %**, direct in `StateBase.src` setter | removed a per-character `tuple(ord(c) ...)` |
| | | `6649229`, `145a484` peak −4 % / −3 % | `Token` became a dataclass, then got `__slots__` |

No chore, docs or CI commit was flagged. On the markdown-it-py range, commit-to-commit
noise was under 0.05 % of the peak. See `PROJECT.md` for the full log.

## Development

```
uv venv && uv pip install -e . pytest ruff pytest-xdist pytest-cov pytest-randomly
pytest            # unit + end-to-end tests against generated git repos (~90 s)
ruff check src tests
python tests/fixture_repo.py /tmp/demo   # a repo with two planted regressions
```

## License

MIT
