# memblame — "git blame for memory" (Python)

Working name: `memblame`. Change it any time.

## 1. One-paragraph summary

A tool that runs a developer's own Python workload (a pytest test, a script, or a function) at many git commits, measures memory at each commit, and reports **which commit made memory grow and which function is responsible**, mapped back to the lines that changed in the git diff. Core is a standalone CLI. A VS Code extension is a thin UI on top, added later.

## 2. Problem statement

### 2.1 The pain
A Python service, data pipeline, ML job or library suddenly uses more RAM. Nobody notices at the time of the commit. Weeks later it shows up as an OOM kill, a bigger cloud bill, or a slow container. The question then is: **"Which commit did this, and what in that commit?"**

Today the developer answers it by hand:
1. Guess a range of commits.
2. `git checkout` an old commit, run the workload under some profiler, write down the number.
3. Repeat for a dozen commits, binary-search by hand.
4. Open the two profiles side by side and guess which function grew.
5. Cross-check with `git diff` to find the responsible change.

This takes hours, is error-prone (noise, wrong environment, forgetting to reset state), and is usually skipped, so memory regressions ship.

### 2.2 Who has this problem
- Developers of long-running services and workers (memory creeps up between releases).
- Data / ML engineers (a pandas/numpy change doubles a DataFrame's footprint).
- Library maintainers with memory constraints.
- Anyone who has seen "it worked last month" on a memory-limited container.

### 2.3 Why existing tools do not solve it (checked around Sept 2026; re-verify before publishing)
- **Single-run profilers.** Scalene, memray, memory_profiler and the VS Code extensions built on them (for example a Scalene extension and a py-spy/memray "Flamegraph" extension) show memory for **one run of one version**. They do not compare across commits.
- **Cross-commit tracking exists but not for your own repo in your editor.** CPython Memory Insights tracks memory across CPython's own commits using community workers. `asv` (airspeed velocity) tracks benchmarks across commits but its peak-memory metric is RSS-based, which per pytest-benchmem's docs misses allocator-level detail that memray sees. pytest-benchmem explicitly does not do cross-commit history and points to asv/CodSpeed.
- **Gap we are targeting:** local, developer-side, per-commit Python memory comparison with **function-level attribution mapped to the git diff**, plus an editor UI. I did not find a tool doing exactly this. That is not proof of "first", so do NOT claim "first ever". Claim: *"per-commit Python memory regression tracking with function-level blame, inside your editor."*

### 2.4 Success looks like
Given a repo, a workload and a commit range, the tool prints something like:

```
Peak memory by commit (median of 3 runs)
  a1b2c3  412 MB  +38 MB  <-- regression   Rahul  "add caching layer"
  9f8e7d  374 MB   +1 MB
Top change a1b2c3 vs 9f8e7d:
  +38 MB  pkg/parse.py:41  load_rows()   (inside changed hunk @@ -38,6 +38,9 @@)
```

## 3. Goals and non-goals

**Goals (MVP)**
- Measure peak memory of a workload at any commit, reproducibly.
- Compare two commits with function-level attribution and map it to changed hunks.
- Run over a commit range with caching; find the first commit that crosses a threshold (bisect).
- Be honest about noise (multiple runs, variance shown).
- Works on Windows, macOS and Linux (core uses only stdlib `tracemalloc`).

**Non-goals (for now)**
- Writing our own profiler. We wrap `tracemalloc` (and later optionally memray).
- Other languages.
- A cloud service or dashboard.
- Changing dependencies per commit (MVP assumes one fixed environment; only the project code changes).
- CPU profiling.

## 4. How measurement works (design decisions)

**Workload.** Three supported forms, all run inside a fresh subprocess so state never leaks between commits:
- `pytest:<node id>`, for example `pytest:tests/test_big.py::test_load`
- `script:<path> [args]`
- `call:<module>:<function>`

**Checkout.** For each commit: `git worktree add --detach <tmp> <sha>`, run, then `git worktree remove --force`. Always clean up, including on failure. Never touch the user's working tree.

**Runner (`runner.py`).** In the subprocess: set `cwd` and `sys.path` to the worktree, call `tracemalloc.start(nframe=25)`, run the workload, and emit JSON.

**Metrics recorded per run**
- `peak_bytes`: from `tracemalloc.get_traced_memory()` (exact for traced allocations).
- `top_at_peak`: allocation sites near the peak. tracemalloc does not tell you *when* the peak happened, so use a background polling thread that watches the current traced size and takes a throttled snapshot whenever a new high-water mark is reached; keep the snapshot from the highest point. This is an approximation; document it and measure its overhead.
- `retained_at_end`: snapshot statistics after the workload (cheap, deterministic, secondary).

**Attribution.** Take each allocation's traceback and pick the **most recent frame that lies inside the project** (filter by path prefix = worktree). Map `file:line` to the enclosing function using `ast` (function start line to `end_lineno`). Aggregate bytes per function.

**Noise.** Run each commit `N` times (default 3), take the median, and store min/max. A change is "significant" only if it exceeds a noise band (start with `max(spread, 1% of peak)` times a factor of 2; tune on real data).

**Diff mapping.** `git diff -U0 A B -- '*.py'`, parse `@@ -a,b +c,d @@` hunk headers, and check whether the attributed function's line range at B intersects the new-side hunk range. If not, flag the result as **indirect** (memory grew in function X but the change was in a caller or config) and list the changed functions nearby.

**Cache.** Key = commit SHA + workload hash + Python version. Store JSON under `.memblame/cache/`.

**Bisect.** Given `--good`, `--bad` and a threshold, binary-search over `git rev-list --first-parent`. Memory is not always monotonic, so verify the found commit against its parent and fall back to a linear scan on inconsistency.

**Environment pitfall (important).** If the project is installed editable (`pip install -e .`), imports may resolve to the main checkout instead of the worktree. After each run verify that the project's modules have `__file__` inside the worktree; if not, mark the commit result `invalid_environment` instead of reporting wrong numbers. Support a `pythonpath` setting for `src/` layouts.

## 5. Repo layout

```
memblame/
  pyproject.toml
  README.md
  PROJECT.md              <- this file
  src/memblame/
    cli.py                # commands: run, range, diff, bisect
    runner.py             # subprocess entry: start tracemalloc, run workload, emit JSON
    worktree.py           # create/cleanup git worktrees
    measure.py            # N runs, median, noise band, peak polling thread
    attribute.py          # traceback -> project frame -> enclosing function (ast)
    gitdiff.py            # parse git diff -U0 hunks, map to functions
    cache.py
    report.py             # terminal table + JSON output
  tests/
    fixture_repo.py       # generates a small git repo with a planted regression
    test_*.py
  examples/
  vscode-ext/             # Phase 5, separate package, talks to CLI via JSON
```

Config file `memblame.toml` (optional): `workload`, `runs`, `pythonpath`, `project_paths`, `threshold`.

## 6. Step-by-step build plan

Each phase has an acceptance test. Do not start a phase until the previous one passes.

### Phase 0 — Smallest working spike (1 script)
Take two commit SHAs and a workload. Run it at both commits (worktrees), print peak memory for each and the top functions by growth.
**Done when:** on the fixture repo (see section 7) it names the right function.

### Phase 1 — Real CLI for one commit and a diff
- `memblame run --rev <sha>` prints the JSON result.
- `memblame diff A B` prints function-level delta and hunk mapping.
- Implement the worktree cleanup, environment-validity check, and 3-run median.
**Done when:** planted-regression test passes and a no-regression fixture reports no significant change.

### Phase 2 — Range and cache
- `memblame range main~20..main` runs every commit (first-parent), with caching, and prints the timeline.
**Done when:** a second run of the same range is near-instant and results are identical.

### Phase 3 — Bisect
- `memblame bisect --good X --bad Y --threshold 200MB`.
**Done when:** it finds the planted commit in about log2(N) measurements.

### Phase 4 — Validate on a real open-source repo
- Pick a public Python project with deterministic tests and a known memory-related issue or a commit that plausibly increased memory. Run the tool. Record real numbers, variance and false positives.
- Fix whatever breaks (imports, editable installs, slow workloads).
**Done when:** you have at least one real, explainable finding, or an honest write-up of why the tool could not find one.

### Phase 5 — VS Code extension (thin UI)
- Commands: "MemBlame: Analyze range", "MemBlame: Find regression".
- Webview timeline (simple SVG or Chart.js): x = commits, y = peak MB; click a point to show commit info, top functions and the diff hunk; click a function to open the file.
- CodeLens above functions: "+38 MB vs main".
- The extension only calls the CLI with `--json`. No shared code with the engine.
**Done when:** the demo GIF flow works end to end.

### Phase 6 — Optional extras (only after the above)
- memray backend for native allocations (Linux/macOS only).
- Uncommitted working tree vs `HEAD` comparison.
- GitHub Action that fails a PR if memory grows past a threshold.
- Optional Claude explanation of "why this hunk allocates more".
- MCP wrapper exposing `find_memory_regression`.

## 7. Testing strategy

- **Planted-regression fixture.** `fixture_repo.py` builds a temporary git repo with about 10 commits. One commit makes a function hold a large list (tens of MB). Assert the tool reports exactly that commit and that function. All tests run against this generated repo, never against a real repo.
- **Negative fixture.** No memory change across commits, so no commit is flagged (guards against false positives from noise).
- **Noise test.** Repeat measurements of the same commit and assert the variance stays inside the noise band.
- **Environment test.** An editable-install scenario where imports would resolve outside the worktree must produce `invalid_environment`.
- **Cross-platform.** Use `pathlib`, avoid shell-only features, keep temp paths short (Windows path-length limits with worktrees).

## 8. Assumptions to verify early (write a small test for each)

1. Ordering of frames in `tracemalloc.Traceback` (docs say oldest to most recent since Python 3.7; confirm and pick the correct "most recent project frame").
2. Overhead and accuracy of the polling-thread "peak snapshot" approach; measure on a real workload.
3. `tracemalloc` misses allocations from C code that calls `malloc` directly; confirm with a numpy example and document the limit.
4. `git worktree` behavior on Windows (locking, cleanup, long paths).
5. That pytest's own import/collection overhead is constant across commits (so it cancels in deltas).

## 9. Risks and honest limits (put these in the README)

- `tracemalloc` sees Python-allocator memory and tracemalloc-aware C allocations, not all native memory. Backend with memray later.
- Tracing slows the workload; use small, deterministic workloads.
- Different dependency versions across commits make results unreliable; MVP assumes a fixed environment.
- Non-deterministic tests produce noisy numbers; require a deterministic workload and show variance.
- "Indirect" attributions (memory grew in one function because a caller changed) are flagged, not guessed.

## 10. Demo and positioning

- Demo asset: a 20-second GIF: timeline chart, spike at one commit, click, function and hunk highlighted.
- LinkedIn story: run it on a real repo and show one real finding with numbers and variance.
- Claim: *"Per-commit Python memory regression tracking with function-level blame, in your editor."* Do not claim "first ever".

## 11. Working agreement for Claude Code

- Read this file first. Build **one phase at a time**, and stop after each phase to show the acceptance test passing.
- Write the fixture repo and the failing test before the feature.
- Keep the core dependency-free (stdlib only). Ask before adding any dependency.
- Keep the engine independent of VS Code; the JSON output schema is the contract, so version it (`"schema": 1`).
- Do not add features outside the current phase. If something in section 8 turns out false, stop and tell me before redesigning.
- Prefer small, readable functions and type hints; run `ruff` and `pytest` before saying a phase is done.

### Suggested prompts to paste into Claude Code, in order

1. "Read PROJECT.md. Implement `tests/fixture_repo.py` that generates a temp git repo with a planted memory regression and a no-regression variant. No tool code yet. Show me the generated history."
2. "Implement Phase 0 as a single script that measures two commits using git worktrees and tracemalloc, and make it pass on the fixture. Verify assumption 1 in section 8 with a small test."
3. "Turn the spike into the CLI (Phase 1): `run` and `diff`, 3-run median, worktree cleanup, and the invalid-environment check with its test."
4. "Implement Phase 2 (range + cache) and Phase 3 (bisect) with tests on the fixture."
5. "Help me run the tool on a real open-source repo (Phase 4). Log every failure and fix it, then summarize findings with variance."
6. "Build the VS Code extension (Phase 5) as a thin UI over the CLI's `--json` output."