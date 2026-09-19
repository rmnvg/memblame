# memblame: "git blame for memory" (Python)

Working name: `memblame`. Change it any time.

## 0. Status (2026-09-19)

| Phase | State | Evidence |
|---|---|---|
| 0 Spike | done, folded into the CLI | |
| 1 `run` / `diff`, median, cleanup, env check | done | `tests/test_integration.py` |
| 2 `range` + cache | done, **adaptive by default** (`--all` for every commit) | cached re-run measures 0 commits |
| 3 `bisect` | done | finds planted commit in ≤ ⌈log₂ N⌉ steps |
| 4 Real repos | done: markdown-it-py, tomlkit, pyparsing | section 10 |
| 5 VS Code extension | done; VSIX builds (~400 KB) | 12 node tests + 7-step integration test in real VS Code 1.131 |
| 6 Extras | not started | section 12 |

Test suites: `pytest` (69 tests, ~140 s, order-independent under pytest-randomly; 94 % line
coverage including the runner subprocesses),
`ruff check src tests`, `cd vscode-ext && npm test` (12), `npm run test:integration`
(7 steps in a real VS Code; set
`VSCODE_EXECUTABLE="/Applications/Visual Studio Code.app/Contents/MacOS/Code"`).
CI (`.github/workflows/ci.yml`): Linux/macOS/Windows × Python 3.9/3.12/3.14 (all green,
including Windows), extension unit tests + VSIX build, and the real-VS Code integration test
under xvfb on Linux. Supported Pythons: 3.9–3.14.

## 1. One-paragraph summary

A tool that runs a developer's own Python workload (a pytest test, a script or a function)
at git commits, measures memory at each one, and reports **which commit made memory grow and
which function is responsible**, mapped back to the changed lines in the git diff. The core
is a standalone, stdlib-only CLI. The VS Code extension is a thin UI that bundles that CLI
and talks to it through JSON.

## 2. Problem statement

### 2.1 The pain
A Python service, data pipeline, ML job or library suddenly uses more RAM. Nobody notices at
the time of the commit. Weeks later it shows up as an OOM kill, a bigger cloud bill, or a
slow container. The question is then **"Which commit did this, and what in that commit?"**
Answering it by hand (checkout, profile, write down, bisect, compare profiles, cross-check
the diff) takes hours and is usually skipped.

### 2.2 Who has this problem
Service and worker developers, data/ML engineers, library maintainers, and anyone who has
seen "it worked last month" on a memory-limited container.

### 2.3 Existing tools (checked around Sept 2026; re-verify before publishing)
- Single-run profilers (Scalene, memray, memory_profiler and the VS Code extensions built on
  them) show one run of one version.
- Cross-commit tracking: CPython Memory Insights (CPython only), `asv` (RSS-based peak
  metric), pytest-benchmem (no cross-commit history).
- **Re-checked 2026-09-19:** CodSpeed (hosted CI service) now tracks heap allocations per
  benchmark with differential flame graphs between base and head commits; pytest-memray's
  `fail-on-increase` fails a test that allocates more than its last successful run. Both are
  CI-forward: they only know commits that ran after you set them up.
- What memblame still does that they don't: works **retroactively on any past commit**
  (range/bisect over existing history, no prior setup), **local and free** (no service, no
  account, stdlib only, Windows/macOS/Linux), maps growth to the **git diff hunk** with a
  direct/indirect verdict, and lives in the editor ("Memory vs HEAD" before you commit).
- Do NOT claim "first" or "only tool that compares commits". Claim: *"Find the commit and the
  function that made your Python code use more memory — retroactively, locally, in your
  editor."*

## 3. Goals and non-goals

Goals: reproducible peak and retained memory per commit; function-level diff blame; range
with cache; bisect; honest noise handling; Windows, macOS and Linux (stdlib only).

Non-goals for now: our own profiler, other languages, a cloud service, per-commit dependency
installs, CPU profiling.

## 4. How measurement works (as built)

**Workloads** (fresh subprocess of the *project's* interpreter per run):
`pytest:<node ids>` (each test is its own *unit*, and only the test's own setup, call and
teardown is traced), `script:<path> [args]` (path may be absolute and outside the repo,
which keeps it identical across commits), `call:<module>:<function>`.

**Checkout.** One reusable detached worktree per command (`git worktree add` once, then
`checkout --force` + `clean -fdx`), removed in `finally`. SIGTERM becomes `SystemExit`, so
cancelling from the editor still cleans up. Uncommitted work = pseudo-revision `WORKTREE`
(the repo itself; never cached).

**Runner** (`runner.py`, standalone, stdlib-only, run by path; it must not import memblame).
It sets `sys.path`: removes its own dir, prepends the pythonpath dirs (auto: `src` + `.` if
`src/` exists). Results go to a JSON file, not stdout, because workloads print.

**Two kinds of runs** (the key design change, driven by the measurements in section 8):
1. *Fast runs*: `tracemalloc` with 1 frame, no snapshots. They give `peak_bytes` and
   `end_bytes` (after `gc.collect()`: "retained"). Up to `runs` (default 3), stopping early
   once two runs agree within 0.1 % / 8 KB. Only these runs feed the median and the spread.
2. *Attribution run*: done **lazily**, only for commits around a significant change (and for
   `memblame run`). Full depth (`nframe`, default 16). A snapshot is taken near the peak known
   from the fast runs (≥ 90 %, then on each new +2 % high):
   - first by a **polling thread** (0.5 ms, switch interval lowered), which is cheap;
   - if the snapshot covers < 90 % of the peak (e.g. a temporary that lives inside one C
     call), the run is repeated with a **profile hook** on every return / C return, which is
     exact but 10×+ slower on call-heavy code.
   The attribution run's own numbers are *not* samples (it shifts allocation timing ~2 %).

**Attribution.** Traces are grouped by traceback (fast path over the raw trace tuples,
verified equal to `statistics("traceback")`, ~20× faster). This is done *after*
`tracemalloc.stop()`, since analysing under tracing is ~20× slower. For each project frame the
enclosing scope comes from `ast` (innermost def/class; decorators count for diff matching;
same-qualname scopes such as a property getter and setter are merged). *self* = bytes whose
most recent project frame is the function; *cumulative* = bytes with the function anywhere in
the stack (counted once per trace). The cumulative number is what makes retention bugs
("new cache around an unchanged loader") blame the changed caller.

**Noise band.** `max(2 × spread, 2 % of the larger median, 64 KiB)`.

**Blame.** Functions whose cumulative delta is ≥ 10 % of the unit delta are candidates. A
candidate is *changed* if its range at head intersects a new-side hunk, **or its range at base
intersects an old-side hunk** (needed for improvements where the allocating code was deleted).
Among changed candidates within 90 % of the best: prefer the most *self* bytes, i.e. the
deepest. Result: `direct`, `indirect` (nothing changed explains it; changed functions are
listed) or `unattributed` (with a note when truncated stacks hide > 5 % of the memory). Hot
lines come from head for growth and from base for memory that went away; `allocated_at` is
added when the blamed function mostly *keeps* memory allocated elsewhere.

**Adaptive range.** Measure both ends; if they differ significantly (any unit, any metric,
validity or outcome), measure the midpoint and recurse. Cost is about log₂ N per change. Known
blind spot: a change undone later within one unsplit segment (`--all` covers it).

**Bisect.** It picks the unit and metric with the largest relative growth (or `--unit` /
`--metric`) and a threshold (`200MB`, `+20MB`, `+10%`, default: the noise band). It
binary-searches the first-parent chain and reports `monotonic: false` plus a warning if the
measured points are not good…good,bad…bad.

**Cache.** `.memblame/cache/<sha>-<key>.json` (with a `.gitignore`), keyed by the settings,
interpreter version + installed distributions, the engine's source (runner + measure),
the schema, and the content of a `script:` file that lives outside the repo. Attribution
is added to the cached entry when it is computed.

**Environment check.** After the run, any imported module whose top-level name is a project
package (found in root, `src/` and the configured paths) but whose `__file__` is outside the
checkout makes the result `invalid_environment` (never cached, no findings).

**Robustness.** pytest always runs in-process, in file order, without coverage (`-n 0`,
`-p no:randomly`, `--no-cov` when those plugins exist). A unit whose outcome differs between
two commits is reported as `outcome_changed`, never as a memory change. A commit that crashes
or times out is a skipped point (range) or skipped like `git bisect skip` (bisect). If only one
side of a comparison has a peak snapshot, the other is treated as empty and the verdict notes
that the deltas are upper bounds. Stale worktrees from killed runs are removed via a pid file.

**Contract.** `--json` output has `"schema": 1`. Exit code 3 = significant increase found.

## 5. Repo layout (as built)

```
src/memblame/
  cli.py       argparse commands, config from [tool.memblame] / memblame.toml
  api.py       Session (cache, memo, worktree), run / diff / range_ / bisect
  measure.py   fast runs, lazy attribution, Cache, interpreter discovery
  runner.py    subprocess entry (stdlib only; also imported for its AST helpers)
  blame.py     noise band, ChangeMap (both diff sides), verdicts
  git.py       worktree pool, hunks, commit metadata
  report.py    terminal output
tests/
  fixture_repo.py      planted (direct + retention) and clean repos, flat or src layout
  test_units.py        section 8 assumptions + parsers/scopes
  test_integration.py  end-to-end on generated repos
vscode-ext/
  src/extension.ts     commands, interpreter resolution, webview, CodeLens, decorations
  src/cli.ts           spawn bundled engine, progress parsing       (vscode-free)
  src/render.ts        report HTML + SVG chart (VS Code theme vars) (vscode-free)
  src/workload.ts      test discovery / workload suggestions       (vscode-free)
  scripts/bundle-python.js   copies src/memblame into the VSIX
  scripts/screenshot.js      renders a report with headless Chrome (docs images)
  test/                node unit tests + real-VS Code integration test
```

## 6. Build plan: done, see the status table. Remaining work is in section 12.

## 7. Testing strategy (as built)

- `tests/test_edge_cases.py` (added in the review pass, each test verified to fail on the
  pre-fix code): xdist/pytest-cov/pytest-randomly in the project's pytest config, commits
  that break the workload (not an "improvement"; bisect skips them), a commit that times
  out, edits to an external benchmark invalidating the cache, a clean working tree measured
  once, untracked and non-ASCII/space paths, non-ancestor ranges, missing-dependency hints,
  stale worktrees from killed runs, sibling imports + latin-1 sources, async workloads.
- Planted fixture: `direct` (peak +28 MB in `load_rows`) and `retention` (retained +58 MB,
  blamed on the changed `summarize`, allocated in the unchanged `load_rows`). Tests assert the
  exact commits, metrics and functions, for `diff`, adaptive and exhaustive `range` (same
  findings), `bisect` and pytest per-test units.
- Clean fixture: no findings; adaptive range measures only the 2 ends.
- Noise: same commit twice stays under ¼ of the band.
- Environment: src layout + PYTHONPATH pointing at another checkout gives `invalid`; the
  default auto-detection gives valid.
- Real-world regressions turned into tests: a removed allocation inside a property setter
  (markdown-it-py) and a peak that lives only inside one C call (hook fallback).
- Uncommitted changes, worktree cleanup, CLI JSON and exit codes, workload errors.

## 8. Assumptions: results of the experiments

1. **Traceback order**: confirmed oldest → most recent on 3.12 and 3.14 (`tb[-1]` = allocator).
2. **Peak snapshot**: the original polling-thread plan got 98.5 % coverage but took *many*
   snapshots while memory climbed (3.9× slower). What works: prime with the fast-run peak;
   poll (tomlkit: 98.3 % coverage, 11 s) and fall back to the profile hook only if coverage
   < 90 % (hook alone: 98.0 %, 144 s). The hook is needed for C-call-only peaks (polling and
   Python-return-only hooks get 0 % there). A snapshot costs a constant ~0.7 KB of traced
   memory, however many traces exist.
3. **numpy**: the assumption was wrong in our favour. numpy reports its buffers to
   tracemalloc (an 80 MB array shows as 80 MB). Other native libraries may not.
4. **Windows worktrees**: not tested yet (no Windows machine). Code uses short temp paths,
   pathlib, no shell. Still to verify.
5. **pytest overhead**: made irrelevant: tracing starts and stops inside
   `pytest_runtest_protocol`, so collection/import is never measured.
6. **New: tracing cost depends on the frames actually captured**, not on `nframe`: 1 frame is
   ~6× slower than untraced; under pytest (≈40 frames of pytest internals) 16 frames cost 2×
   and 32 frames 5× more than 8. Deeply recursive code (pyparsing) was 6 s at 1 frame and
   36 s at 16. That is why the numbers come from 1-frame runs and attribution is lazy.
7. **New: determinism**: fast runs of the same commit differ by a few KB; across 33
   markdown-it-py commits the largest non-change was 36 KB on 7.5 MB.
8. **New: the runner must not preload modules.** Anything imported before tracing starts
   is free for the workload. The review pass briefly made the runner import `inspect`, which
   hid most of a real tomlkit regression (`import dataclasses` → `inspect`). The runner now
   uses `_tracemalloc` (the C core), `compile()` + `exec` for scripts and `__import__` for
   `call:`, and reads its spec with `eval` instead of `json`: at workload start it has
   loaded only `__future__`, `_tracemalloc` and `gc` beyond a bare interpreter (was 53
   modules). A test guards this.
9. **New: `retained` excludes the script's own globals** (cleared like `runpy` does), so it
   measures what outlives the workload: caches, module state, leaks.
10. **New: some peaks are unobservable.** On 3.14, `tuple(generator)` builds a temporary that
    roughly doubles memory inside one C call; no hook can see it. The hook fallback now
    snapshots from 50 % of the peak and keeps the best capture; verdicts say when coverage
    is partial.

## 9. Risks and honest limits (also in README)

- tracemalloc misses native allocations that bypass Python's allocator (memray backend later).
- Tracing is slow; choose small deterministic workloads. Attribution can be 10–40× slower on
  allocation-heavy or recursive code, but it only runs where a change was found.
- The environment is fixed across commits (current dependencies).
- `script:`/`call:` workloads include import-time memory (a new `import unittest` shows up,
  which is correct but can surprise); `pytest:` workloads do not.
- Adaptive range blind spot (see section 4).
- "Indirect" verdicts are flagged, not guessed.

## 10. Real-world validation (Phase 4)

Fixed benchmark scripts outside the repos (`script:/abs/path`), one venv per repo with its
deps but not the project, Python 3.12, adaptive range. Each finding was checked by hand
against the diff.

| repo, range | commits / measured | finding | verified cause |
|---|---|---|---|
| markdown-it-py `v4.0.0..HEAD` (exhaustive) | 33 / 33 | none | largest step +36 KB on 7.5 MB (a new preset); no false positives |
| markdown-it-py `v2.0.0..HEAD` | 137 / 19 | `f52249e` peak −1.6 MB (−15 %), direct `StateBase.src` | removed `tuple(ord(c) for c in src)` in the setter |
| | | `6649229` peak −4 %, retained −13 % | `Token` → dataclass |
| | | `145a484` peak −3 % (indirect) | `__slots__` on dataclasses (the saving shows up in callers) |
| tomlkit `0.11.0..HEAD` | 234 / 21 | `231370c` peak **−65 %** (60.6 → 21.1 MB), direct `Source.__init__` | stops materializing the source |
| | | `ae1b679` peak **+3.9 %**, direct `Container.__init__` | a new `dict` + `set` per `Container` (a speed/memory trade-off) |
| | | `a766d3a` retained +1.0 MB (+47 %), module level `items.py` | new `import dataclasses` (+ `inspect`) |
| pyparsing `3.1.0..HEAD` | 511 / 11 | `cd081ef` retained +1.56 MB (+22 %), hot line `testing.py:6` | `import unittest` added; `pyparsing/__init__` imports `testing`, so **every `import pyparsing` now loads unittest**, still true at HEAD |

Numbers above are from the final engine (after the review pass), all with 0 warnings.

Bugs found this way and fixed: old-side blame for improvements, property getter/setter
ranges, repeated warnings, slow hook-only peak capture, and (via the VS Code integration test)
macOS `/var` vs `/private/var` path mismatch that hid CodeLens.

Possible upstream contribution / demo story: the pyparsing `unittest` import (a lazy import
inside the function that needs it would fix it). Re-check at the current HEAD before reporting.

## 11. Demo and positioning

- Screenshots: `vscode-ext/media/report-range.png`, `report-diff.png` (regenerate with
  `node scripts/screenshot.js <json> <png>`).
- Still to make: the 20 s GIF (lens "Memory vs HEAD" → report → click function → CodeLens).
- Story: "memblame found that since pyparsing 3.3 every `import pyparsing` loads `unittest`",
  or the tomlkit −66 % commit, with numbers and noise.
- Claim: *"Per-commit Python memory regression tracking with function-level blame, in your
  editor."* No "first ever".

## 12. Next steps / publishing checklist

Must do before publishing (needs you):
1. Pick the final name and check that it is free on PyPI and the VS Code Marketplace.
2. Create a Marketplace publisher; set `publisher` and `repository.url` in
   `vscode-ext/package.json` (currently `memblame` / `OWNER`), set the copyright holder in
   `LICENSE`.
3. Push to GitHub, add CI (pytest + ruff on Linux/macOS/**Windows**, extension tests).
4. `python -m build && twine upload` for the CLI; `npx vsce publish` (and Open VSX for
   Cursor/VSCodium users) for the extension.
5. Record the GIF.

Worth doing next (in order of value):
1. Windows run of the test suite (assumption 4).
2. A GitHub Action: `memblame diff origin/main HEAD` on PRs, comment with the finding,
   fail on exit code 3.
3. Pytest-plugin-style workload: "all tests in a directory" is supported, but the report
   should rank tests by change.
4. memray backend for native memory (Linux/macOS).
5. MCP tool `find_memory_regression`; optional LLM explanation of a hunk.

## 13. Working agreement for Claude Code

- Read this file first. Keep the core stdlib-only; ask before adding dependencies.
- The JSON output schema is the contract with the extension; bump `"schema"` on breaking
  changes.
- Run `pytest`, `ruff check src tests` and `cd vscode-ext && npm test` before calling
  anything done.
- If an assumption in section 8 turns out false, stop and report before redesigning.
