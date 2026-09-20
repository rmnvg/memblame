# MemBlame: git blame for memory

**Find the commit and the function that made your Python code use more memory.**

MemBlame runs one of your pytest tests, scripts or functions at several git commits,
measures memory, and points at the function and the diff hunk responsible.

![Timeline of memory by commit with two regressions blamed on functions](https://raw.githubusercontent.com/rmnvg/memblame/main/vscode-ext/media/report-range.png)

## What you can do

- **Memory vs HEAD**: a CodeLens above every pytest test compares the test's memory with
  and without your uncommitted changes. This answers "did my change make memory worse?"
  before you commit.
- **Compare two commits**, with a verdict: which function grew, whether it is the code you
  changed (*direct*) or a knock-on effect (*indirect*), and the hot lines.
- **Analyze a commit range**: a timeline of peak and retained memory. It measures the ends
  and only subdivides where memory changed, so 100 commits usually needs only a handful of
  measurements.
- **Find the commit that increased memory**: bisect with an optional threshold
  (`200MB`, `+20MB`, `+10%`).
- After a run, the blamed function gets a CodeLens (`↑ peak memory +28.2 MB`) and hot
  lines are annotated inline.

![Comparison report](https://raw.githubusercontent.com/rmnvg/memblame/main/vscode-ext/media/report-diff.png)

## Commands

Open the Command Palette (`Ctrl/Cmd+Shift+P`) and type **MemBlame**:

| Command | What it does |
|---|---|
| **MemBlame: Compare Memory: Working Tree vs HEAD** | your uncommitted changes vs the last commit (also in the editor's right-click menu, and as the "Memory vs HEAD" lens above tests) |
| **MemBlame: Compare Memory Between Two Commits…** | pick a base and a head commit (or the working tree) |
| **MemBlame: Analyze Memory Over a Commit Range…** | timeline over `BASE..HEAD` |
| **MemBlame: Find the Commit That Increased Memory (bisect)…** | pick the last good commit and an optional threshold |
| **MemBlame: Choose Workload…** | which test, script or function to run |
| **MemBlame: Show Last Report** | reopen the last report |
| **MemBlame: Clear Annotations** | remove MemBlame CodeLens and inline notes |

## Getting started

1. Open a Python project that is a git repository.
2. Select your project's interpreter with the Python extension, or set `memblame.pythonPath`.
   MemBlame runs your code with it, so your dependencies must be installed there.
3. Click **Memory vs HEAD** above a test, or run **MemBlame: Choose Workload…** and then any
   MemBlame command from the Command Palette. The chosen workload is remembered in VS Code's
   per-workspace state; the `memblame.workload` setting, when present, wins. If neither exists,
   MemBlame automatically uses `memblame.toml` or `[tool.memblame]` in `pyproject.toml` before
   prompting. Analysis results are cached in `.memblame/`.

Nothing needs to be installed with pip: the engine is bundled with the extension and uses
only the Python standard library.

## Settings

| Setting | Default | |
|---|---|---|
| `memblame.workload` | | `pytest:tests/test_x.py::test_y`, `script:path [args]` or `call:module:function` |
| `memblame.runs` | repo config, else `3` | max measured runs per commit (stops early once two runs agree) |
| `memblame.nframe` | repo config, else `16` | traceback depth for attribution |
| `memblame.pythonPath` | see below | interpreter that runs your code |
| `memblame.importPaths` | repo config, else auto | e.g. `["src"]` |
| `memblame.defaultRange` | `HEAD~20..HEAD` | |
| `memblame.testCodeLens` | `true` | show "Memory vs HEAD" above tests |

### Which value wins

The editor and the command line share one configuration, so the same repository gives the
same numbers in both. A value you never set in VS Code is **not** sent to the engine, which
then uses the repository's `memblame.toml` / `[tool.memblame]` (see the
[CLI documentation](https://github.com/rmnvg/memblame#configuration)) and finally its
built-in default. In order, highest first:

- **Interpreter:** `memblame.pythonPath` → `python` in the repository config → the
  interpreter selected in the Python extension → `.venv` / `venv` in the repository → `python3`.
- **`runs`, `nframe`, import paths, workload:** the VS Code setting, if you set one → the
  repository config → the default. The workload you pick from the lens or the palette is
  remembered per workspace and never written into your repository.

Cached measurements are reused when nothing that affects them changed. If your workload reads
environment variables or data files that are not part of a commit, list them under
`cache_env` / `cache_inputs` in the repository config so that changing them re-measures
(see the CLI documentation).

## How it works and its limits

Each committed revision is checked out into a temporary git worktree, so your current checkout
is never switched. A `WORKTREE` analysis runs the workload in the current checkout, where that
workload can still create or modify files; Python bytecode writes are disabled. Measurements
use Python's `tracemalloc`: peak memory and memory still held after the run. The median of runs
is compared against a noise band. Only commits around a real change get a slower attribution
run that maps memory to functions and to the lines in `git diff`.

- `tracemalloc` counts Python allocations and numpy arrays, but not native libraries that
  call `malloc` directly.
- Tracing slows code down (several times, more for attribution), so choose a small,
  deterministic test.
- All commits run with your currently installed dependencies.
- pytest runs in-process and in file order, without coverage, even if your pytest config
  uses pytest-xdist, pytest-randomly or pytest-cov.
- A commit where a test fails or skips, or that cannot be measured, makes the check
  incomplete rather than reporting a successful memory check. Bisect can still skip broken
  intermediate commits, but its endpoints must pass.
- If imports resolve outside the checked-out commit (for example an editable install with
  a `src/` layout), MemBlame reports **invalid environment** instead of wrong numbers. Set
  `memblame.importPaths`.

The same engine is available as a CLI (`pip install memblame`) for CI and terminals, including
portable Markdown and self-contained HTML reports via `--report md|html --output PATH`.

**Privacy:** MemBlame sends nothing anywhere. It runs locally, in trusted workspaces only.
