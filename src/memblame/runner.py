"""Measure one workload run. Executed as a standalone script in the *project's* interpreter.

    <project-python> runner.py <spec.json>

This file must stay stdlib-only and must not import the rest of memblame: the project's
interpreter usually does not have memblame installed. The parent process imports it as
`memblame.runner` to reuse the AST helpers.

Spec (JSON):
    workload   "call:pkg.mod:func" | "script:path [args]" | "pytest:<node ids...>"
    root       absolute path of the checkout being measured
    pythonpath list of root-relative dirs to put first on sys.path (default: auto)
    nframe     tracemalloc traceback depth
    hints      {unit name: peak bytes from a previous run} -> enables peak attribution
    attribute  false -> numbers only (no snapshots), for cheap timing runs
    peak_mode  "poll" (cheap background thread, misses sub-millisecond peaks) or "hook"
               (profile hook on every return: exact, but 10x+ slower on call-heavy code)
    out        path to write the result JSON to
"""

from __future__ import annotations

import ast
import gc
import json
import os
import shlex
import sys
import threading
import time
import traceback
import tracemalloc
from collections import defaultdict

SCHEMA = 1
HINT_FRACTION = 0.9  # start snapshotting once memory reaches 90% of the known peak
SNAPSHOT_STEP = 1.02  # ...then only on a new high 2% above the last snapshot (~6 snapshots)
POLL_INTERVAL = 0.0005  # seconds between polls in "poll" peak mode
MIN_FUNC_BYTES = 1024
MAX_LINES = 30
SKIP_DIRS = {".venv", "venv", "env", "site-packages", ".tox", ".nox", "node_modules", ".git"}
NOT_PROJECT_MODULES = {"tests", "test", "conftest", "setup", "docs", "examples", "noxfile"}


# --------------------------------------------------------------------------- AST scopes


def scopes_from_source(source: str) -> list[tuple[int, int, int, str]]:
    """Return (def_line, first_line, end_line, qualname) for each function and class.

    `def_line` is the line of the `def`/`class` keyword (frames inside the body never point
    above it); `first_line` includes decorators and is used for diff-hunk matching.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: list[tuple[int, int, int, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                first = min([d.lineno for d in child.decorator_list] + [child.lineno])
                out.append((child.lineno, first, child.end_lineno or child.lineno, name))
                inner = "<locals>." if not isinstance(child, ast.ClassDef) else ""
                visit(child, f"{name}.{inner}")
            else:
                visit(child, prefix)

    visit(tree, "")
    out.sort()
    return out


def innermost_scope(scopes: list[tuple[int, int, int, str]], line: int):
    """The deepest scope whose def..end range contains `line`, or None (module level)."""
    best = None
    for scope in scopes:
        if scope[0] > line:
            break
        if scope[0] <= line <= scope[2]:
            best = scope  # later starts are nested deeper
    return best


# --------------------------------------------------------------------------- attribution


class Attributor:
    """Maps tracemalloc frames to project functions."""

    def __init__(self, root: str, exclude_files: set[str]):
        self.root = os.path.realpath(root)
        self.prefix = self.root + os.sep
        self.exclude = {os.path.realpath(f) for f in exclude_files}
        self._file_cache: dict[str, str | None] = {}
        self._scope_cache: dict[str, list] = {}
        self._frame_cache: dict[tuple[str, int], tuple | None] = {}
        self.functions: dict[str, dict] = {}  # id -> {file, qualname, line, start, end}

    def project_rel(self, filename: str) -> str | None:
        if filename in self._file_cache:
            return self._file_cache[filename]
        rel = None
        # "<frozen ...>", "<string>" etc. are not files; realpath would resolve them to cwd.
        real = os.path.realpath(filename) if os.path.isabs(filename) else ""
        if real.startswith(self.prefix) and real not in self.exclude:
            parts = real[len(self.prefix) :].split(os.sep)
            if not SKIP_DIRS.intersection(parts):
                rel = "/".join(parts)
        self._file_cache[filename] = rel
        return rel

    def frame(self, filename: str, lineno: int):
        """(function id, rel path) for a project frame, else None."""
        key = (filename, lineno)
        if key in self._frame_cache:
            return self._frame_cache[key]
        rel = self.project_rel(filename)
        result = None
        if rel is not None:
            if rel not in self._scope_cache:
                try:
                    with open(filename, encoding="utf-8") as fh:
                        self._scope_cache[rel] = scopes_from_source(fh.read())
                except OSError:
                    self._scope_cache[rel] = []
            scope = innermost_scope(self._scope_cache[rel], lineno)
            qualname = scope[3] if scope else "<module>"
            fid = f"{rel}::{qualname}"
            info = self.functions.get(fid)
            if info is None:
                self.functions[fid] = {
                    "file": rel,
                    "qualname": qualname,
                    "line": scope[0] if scope else 1,
                    "start": scope[1] if scope else 1,
                    "end": scope[2] if scope else 1,
                }
                if scope:  # a property getter and setter share one qualname: cover both
                    same = [o for o in self._scope_cache[rel] if o[3] == qualname]
                    self.functions[fid]["start"] = min(o[1] for o in same)
                    self.functions[fid]["end"] = max(o[2] for o in same)
            result = (fid, rel)
        self._frame_cache[key] = result
        return result

    def summarize(self, snapshot: tracemalloc.Snapshot, reference_bytes: int) -> dict:
        """Aggregate a snapshot into per-function self/cumulative bytes and top lines."""
        self_bytes: dict[str, int] = defaultdict(int)
        cum_bytes: dict[str, int] = defaultdict(int)
        line_bytes: dict[tuple[str, int], int] = defaultdict(int)
        total = unattributed = truncated = 0
        for frames, size, total_nframe in _grouped_traces(snapshot):
            total += size
            seen: set[str] = set()
            first = True
            for filename, lineno in frames:  # most recent first
                hit = self.frame(filename, lineno)
                if hit is None:
                    continue
                fid, rel = hit
                if first:
                    self_bytes[fid] += size
                    line_bytes[(rel, lineno)] += size
                    first = False
                if fid not in seen:
                    cum_bytes[fid] += size
                    seen.add(fid)
            if first:
                unattributed += size
                if total_nframe is not None and total_nframe > len(frames):
                    truncated += size
        functions = [
            {"id": fid, "self": self_bytes.get(fid, 0), "cumulative": cum}
            for fid, cum in cum_bytes.items()
            if cum >= MIN_FUNC_BYTES
        ]
        functions.sort(key=lambda f: -f["cumulative"])
        lines = sorted(line_bytes.items(), key=lambda kv: -kv[1])[:MAX_LINES]
        return {
            "total": total,
            "coverage": round(total / reference_bytes, 4) if reference_bytes else None,
            "unattributed": unattributed,
            "truncated": truncated,
            "functions": functions,
            "lines": [{"file": f, "line": ln, "bytes": b} for (f, ln), b in lines],
        }


def _grouped_traces(snapshot: tracemalloc.Snapshot):
    """Yield (frames most-recent-first, total size, total_nframe) per distinct traceback.

    Fast path: the raw trace tuples `(domain, size, frames, total_nframe)` behind
    `snapshot.traces` share one frames tuple per distinct traceback, so grouping by identity
    is ~20x faster than `snapshot.statistics("traceback")`, which builds objects per trace.
    Falls back to the public API if the private layout ever changes.
    """
    raw = getattr(snapshot.traces, "_traces", None)
    if isinstance(raw, list) and (not raw or (isinstance(raw[0], tuple) and len(raw[0]) == 4)):
        groups: dict[int, list] = {}
        for _domain, size, frames, total_nframe in raw:
            g = groups.get(id(frames))
            if g is None:
                groups[id(frames)] = [frames, size, total_nframe]
            else:
                g[1] += size
        for frames, size, total_nframe in groups.values():
            yield frames, size, total_nframe
        return
    for stat in snapshot.statistics("traceback"):
        frames = tuple((f.filename, f.lineno) for f in reversed(stat.traceback))
        yield frames, stat.size, getattr(stat.traceback, "total_nframe", None)


# --------------------------------------------------------------------------- measuring


class Meter:
    """Measures one unit (a whole call/script, or one pytest test)."""

    def __init__(self, nframe: int, attributor: Attributor, hints: dict[str, int],
                 attribute: bool = True, peak_mode: str = "poll"):
        self.nframe = nframe
        self.attribute = attribute
        self.peak_mode = peak_mode
        self._poller: threading.Thread | None = None
        self._stop_poll = threading.Event()
        self.attr = attributor
        self.hints = hints
        self.units: list[dict] = []
        self._name = ""
        self._t0 = 0.0
        self._best = 0
        self._snapshot: tracemalloc.Snapshot | None = None
        self._threshold = 0

    def _check(self) -> None:
        current = tracemalloc.get_traced_memory()[0]
        if current >= self._threshold and current > self._best * SNAPSHOT_STEP:
            self._best = current
            self._snapshot = tracemalloc.take_snapshot()

    def _hook(self, frame, event, arg):  # sys.setprofile callback
        if event == "return" or event == "c_return":
            self._check()

    def _poll(self) -> None:
        while not self._stop_poll.wait(POLL_INTERVAL):
            self._check()

    def start(self, name: str) -> None:
        self._name = name
        self._best = 0
        self._snapshot = None
        hint = self.hints.get(name)
        gc.collect()
        tracemalloc.start(self.nframe)
        if hint:
            self._threshold = int(hint * HINT_FRACTION)
            if self.peak_mode == "hook":
                threading.setprofile(self._hook)
                sys.setprofile(self._hook)
            else:
                self._stop_poll.clear()
                self._switch = sys.getswitchinterval()
                sys.setswitchinterval(POLL_INTERVAL)  # let the poller get the GIL often
                self._poller = threading.Thread(target=self._poll, daemon=True)
                self._poller.start()
        self._t0 = time.perf_counter()

    def stop(self, outcome: str, error: str | None = None) -> None:
        duration = time.perf_counter() - self._t0
        sys.setprofile(None)
        threading.setprofile(None)  # type: ignore[arg-type]
        if self._poller is not None:
            self._stop_poll.set()
            self._poller.join()
            self._poller = None
            sys.setswitchinterval(self._switch)
        peak = tracemalloc.get_traced_memory()[1]
        gc.collect()
        end_bytes = tracemalloc.get_traced_memory()[0]
        end_snapshot = tracemalloc.take_snapshot() if self.attribute else None
        tracemalloc.stop()  # before summarizing: analysis under tracing is ~20x slower
        unit = {
            "name": self._name,
            "outcome": outcome,
            "peak_bytes": peak,
            "end_bytes": end_bytes,
            "duration_s": round(duration, 4),
            "retained": self.attr.summarize(end_snapshot, end_bytes) if end_snapshot else None,
            "at_peak": self.attr.summarize(self._snapshot, peak) if self._snapshot else None,
        }
        if error:
            unit["error"] = error[-4000:]
        self.units.append(unit)


def _setup_paths(root: str, pythonpath: list[str] | None) -> list[str]:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != script_dir]
    if pythonpath is None:
        pythonpath = ["src", "."] if os.path.isdir(os.path.join(root, "src")) else ["."]
    dirs = [os.path.normpath(os.path.join(root, p)) for p in pythonpath]
    for d in reversed(dirs):
        sys.path.insert(0, d)
    os.chdir(root)
    return dirs


def _project_modules(dirs: list[str]) -> set[str]:
    names = set()
    for d in dirs:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for e in entries:
            full = os.path.join(d, e)
            if e.endswith(".py"):
                names.add(e[:-3])
            elif os.path.isfile(os.path.join(full, "__init__.py")):
                names.add(e)
    return {n for n in names if n.isidentifier()} - NOT_PROJECT_MODULES


def check_environment(root: str, dirs: list[str]) -> list[str]:
    """Project modules that were imported from outside the checkout (e.g. editable installs)."""
    prefix = os.path.realpath(root) + os.sep
    # Also scan root and src/ even if not configured: a wrong pythonpath is exactly the case
    # where the project gets imported from somewhere else.
    wanted = _project_modules([*dirs, root, os.path.join(root, "src")])
    problems = []
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] not in wanted:
            continue
        f = getattr(mod, "__file__", None)
        if f and not os.path.realpath(f).startswith(prefix):
            problems.append(f"{name} imported from {f}")
    return sorted(problems)


def _run_call(target: str, meter: Meter) -> None:
    import importlib

    module_name, _, func_name = target.partition(":")
    if not func_name:
        raise ValueError("call workload must look like call:package.module:function")
    meter.start("workload")
    try:
        func = getattr(importlib.import_module(module_name), func_name)
        func()
    except BaseException as exc:  # noqa: BLE001 - report every failure, keep measuring
        meter.stop("error", _format_exc(exc))
        return
    meter.stop("passed")


def _run_script(target: str, meter: Meter) -> None:
    import runpy

    argv = shlex.split(target)
    sys.argv = argv
    meter.start("workload")
    try:
        runpy.run_path(argv[0], run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            meter.stop("failed", f"SystemExit({exc.code!r})")
            return
    except BaseException as exc:  # noqa: BLE001
        meter.stop("error", _format_exc(exc))
        return
    meter.stop("passed")


def _run_pytest(target: str, meter: Meter) -> int:
    import pytest

    class Plugin:
        def __init__(self) -> None:
            self.outcome = "passed"
            self.longrepr: str | None = None

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            self.outcome, self.longrepr = "passed", None
            meter.start(item.nodeid)
            yield
            meter.stop(self.outcome, self.longrepr)

        def pytest_runtest_logreport(self, report):
            if report.failed:
                self.outcome = "failed" if report.when == "call" else "error"
                self.longrepr = str(report.longrepr)
            elif report.skipped and self.outcome == "passed":
                self.outcome = "skipped"

    args = shlex.split(target) + ["-q", "-p", "no:cacheprovider", "--no-header"]
    return int(pytest.main(args, plugins=[Plugin()]))


def _format_exc(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def run(spec: dict) -> dict:
    root = os.path.realpath(spec["root"])
    dirs = _setup_paths(root, spec.get("pythonpath"))
    attributor = Attributor(root, exclude_files={__file__})
    meter = Meter(int(spec.get("nframe", 16)), attributor, spec.get("hints") or {},
                  attribute=spec.get("attribute", True),
                  peak_mode=spec.get("peak_mode", "poll"))
    kind, _, target = spec["workload"].partition(":")
    exit_code = 0
    if kind == "call":
        _run_call(target, meter)
    elif kind == "script":
        _run_script(target, meter)
    elif kind == "pytest":
        exit_code = _run_pytest(target, meter)
    else:
        raise ValueError(f"unknown workload kind {kind!r}; use call:, script: or pytest:")
    for fid, info in attributor.functions.items():
        info["id"] = fid
    return {
        "schema": SCHEMA,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sys.platform,
        "nframe": meter.nframe,
        "hinted": bool(spec.get("hints")),
        "peak_mode": meter.peak_mode,
        "env_problems": check_environment(root, dirs),
        "exit_code": exit_code,
        "units": meter.units,
        "functions": attributor.functions,
    }


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    try:
        result = run(spec)
    except BaseException as exc:  # noqa: BLE001 - the parent needs a readable failure
        result = {"schema": SCHEMA, "fatal": _format_exc(exc)}
    with open(spec["out"], "w", encoding="utf-8") as fh:
        json.dump(result, fh)


if __name__ == "__main__":
    main()
