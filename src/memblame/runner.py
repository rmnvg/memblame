"""Measure one workload run. Executed as a standalone script in the *project's* interpreter.

    <project-python> runner.py <spec.json>

This file must stay stdlib-only and must not import the rest of memblame: the project's
interpreter usually does not have memblame installed. The parent process imports it as
`memblame.runner` to reuse the AST helpers.

Spec (a Python literal, read with eval so that no parser module has to be imported):
    workload   "call:pkg.mod:func" | "script:path [args]" | "pytest:<node ids...>"
    argv       the script/pytest arguments, already split by the parent
    root       absolute path of the checkout being measured
    pythonpath list of root-relative dirs to put first on sys.path (default: auto)
    nframe     tracemalloc traceback depth
    hints      {unit name: peak bytes from a previous run} -> enables peak attribution
    attribute  false -> numbers only (no snapshots), for cheap timing runs
    hint_fraction  start snapshotting at this fraction of the hinted peak (default 0.9)
    peak_mode  "poll" (cheap background thread, misses sub-millisecond peaks) or "hook"
               (profile hook on every return: exact, but 10x+ slower on call-heavy code)
    out        path to write the result JSON to
"""

from __future__ import annotations

# Only modules a bare interpreter has already loaded (or builtins) are imported up front:
# anything the runner imports before tracing starts is "free" for the workload, which would
# hide import-time memory (e.g. a commit adding `import dataclasses` pulls in `inspect`).
# Everything else is imported lazily, after tracing stops or only in attribution runs.
import _tracemalloc as _tm  # the C core of tracemalloc: no pickle/linecache/re imports
import gc
import os
import sys
import time

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
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: list[tuple[int, int, int, str]] = []

    def visit(node, prefix: str) -> None:
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
        # normcase: Windows paths are case-insensitive ("C:\\Users" vs "c:\\users")
        self.prefix = os.path.normcase(self.root + os.sep)
        self.exclude = {os.path.normcase(os.path.realpath(f)) for f in exclude_files}
        self._file_cache: dict[str, str | None] = {}
        self._scope_cache: dict[str, list] = {}
        self._frame_cache: dict[tuple[str, int], tuple | None] = {}
        self.functions: dict[str, dict] = {}  # id -> {file, qualname, line, start, end}

    def project_rel(self, filename: str) -> str | None:
        if filename in self._file_cache:
            return self._file_cache[filename]
        rel = None
        # "<frozen ...>", "<string>" etc. are not files; realpath would resolve them to cwd.
        real = os.path.normcase(os.path.realpath(filename)) if os.path.isabs(filename) else ""
        if real.startswith(self.prefix) and real not in self.exclude:
            parts = os.path.realpath(filename)[len(self.prefix):].split(os.sep)
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
                import tokenize

                try:
                    with tokenize.open(filename) as fh:  # honours "# -*- coding: ... -*-"
                        self._scope_cache[rel] = scopes_from_source(fh.read())
                except (OSError, SyntaxError, UnicodeDecodeError):
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

    def summarize(self, traces: list, reference_bytes: int) -> dict:
        """Aggregate a snapshot into per-function self/cumulative bytes and top lines."""
        from collections import defaultdict

        self_bytes: dict[str, int] = defaultdict(int)
        cum_bytes: dict[str, int] = defaultdict(int)
        line_bytes: dict[tuple[str, int], int] = defaultdict(int)
        total = unattributed = truncated = 0
        for frames, size, total_nframe in _grouped_traces(traces):
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


def _grouped_traces(traces: list):
    """Yield (frames most-recent-first, total size, total_nframe) per distinct traceback.

    `traces` is the raw list from `_tracemalloc._get_traces()` (what `take_snapshot()` wraps):
    tuples `(domain, size, frames, total_nframe)`. The C side shares one frames tuple per
    distinct traceback, so grouping by identity is ~20x faster than
    `Snapshot.statistics("traceback")`, which builds objects per trace.
    """
    groups: dict[int, list] = {}
    for trace in traces:
        frames, size = trace[2], trace[1]
        g = groups.get(id(frames))
        if g is None:
            groups[id(frames)] = [frames, size, trace[3] if len(trace) > 3 else None]
        else:
            g[1] += size
    for frames, size, total_nframe in groups.values():
        yield frames, size, total_nframe


# --------------------------------------------------------------------------- measuring


class Meter:
    """Measures one unit (a whole call/script, or one pytest test)."""

    def __init__(self, nframe: int, attributor: Attributor, hints: dict[str, int],
                 attribute: bool = True, peak_mode: str = "poll",
                 hint_fraction: float = HINT_FRACTION):
        self.hint_fraction = hint_fraction
        self.nframe = nframe
        self.attribute = attribute
        self.peak_mode = peak_mode
        self._poller = None
        self._stop_poll = None
        self.attr = attributor
        self.hints = hints
        self.units: list[dict] = []
        self._name = ""
        self._t0 = 0.0
        self._best = 0
        self._snapshot: list | None = None  # raw traces at (near) the peak
        self._threshold = 0

    def _check(self) -> None:
        current = _tm.get_traced_memory()[0]
        if current >= self._threshold and current > self._best * SNAPSHOT_STEP:
            self._best = current
            self._snapshot = _tm._get_traces()

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
        _tm.start(self.nframe)
        if hint:  # attribution runs only; their numbers are not samples
            import threading

            self._threshold = int(hint * self.hint_fraction)
            if self.peak_mode == "hook":
                threading.setprofile(self._hook)
                sys.setprofile(self._hook)
            else:
                self._stop_poll = threading.Event()
                self._switch = sys.getswitchinterval()
                sys.setswitchinterval(POLL_INTERVAL)  # let the poller get the GIL often
                self._poller = threading.Thread(target=self._poll, daemon=True)
                self._poller.start()
        self._t0 = time.perf_counter()

    def stop(self, outcome: str, error: str | BaseException | None = None) -> None:
        duration = time.perf_counter() - self._t0
        sys.setprofile(None)
        if "threading" in sys.modules:
            sys.modules["threading"].setprofile(None)
        if self._poller is not None:
            self._stop_poll.set()
            self._poller.join()
            self._poller = None
            sys.setswitchinterval(self._switch)
        peak = _tm.get_traced_memory()[1]
        gc.collect()
        end_bytes = _tm.get_traced_memory()[0]
        end_snapshot = _tm._get_traces() if self.attribute else None
        _tm.stop()  # before summarizing: analysis under tracing is ~20x slower
        unit = {
            "name": self._name,
            "outcome": outcome,
            "peak_bytes": peak,
            "end_bytes": end_bytes,
            "duration_s": round(duration, 4),
            "retained": (self.attr.summarize(end_snapshot, end_bytes)
                         if end_snapshot is not None else None),
            "at_peak": self.attr.summarize(self._snapshot, peak) if self._snapshot else None,
        }
        if error:
            text = error if isinstance(error, str) else _format_exc(error)
            unit["error"] = text[-4000:]
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
    """Importable module names provided by the checkout, including namespace packages."""
    names: set[str] = set()
    seen: set[str] = set()
    for d in dirs:
        key = os.path.normcase(os.path.realpath(d))
        if key in seen:  # "." / the repo root / "src" often name the same directory
            continue
        seen.add(key)
        for base, subdirs, files in os.walk(d):
            if "pyvenv.cfg" in files:  # a virtualenv with any name: not project code
                subdirs[:] = []
                continue
            subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS]
            rel = os.path.relpath(base, d)
            parts = [] if rel == "." else rel.split(os.sep)
            if any(not part.isidentifier() for part in parts):
                subdirs[:] = []
                continue
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                stem = filename[:-3]
                module_parts = parts if stem == "__init__" else [*parts, stem]
                if module_parts and all(part.isidentifier() for part in module_parts):
                    names.add(".".join(module_parts))
    return {name for name in names if name.split(".")[0] not in NOT_PROJECT_MODULES}


def check_environment(root: str, dirs: list[str]) -> list[str]:
    """Project modules that were imported from outside the checkout (e.g. editable installs)."""
    prefix = os.path.normcase(os.path.realpath(root) + os.sep)
    # Also scan root and src/ even if not configured: a wrong pythonpath is exactly the case
    # where the project gets imported from somewhere else.
    wanted = _project_modules([*dirs, root, os.path.join(root, "src")])
    problems = []
    for name, mod in list(sys.modules.items()):
        if name not in wanted:
            continue
        f = getattr(mod, "__file__", None)
        if f and not os.path.normcase(os.path.realpath(f)).startswith(prefix):
            problems.append(f"{name} imported from {f}")
    return sorted(problems)


def _run_call(target: str, meter: Meter) -> None:
    module_name, _, func_name = target.partition(":")
    if not func_name:
        raise ValueError("call workload must look like call:package.module:function")
    meter.start("workload")
    try:
        __import__(module_name)  # builtin: importlib would preload extra modules
        func = getattr(sys.modules[module_name], func_name)
        result = func()
        if hasattr(result, "__await__"):  # async def workload
            import asyncio

            asyncio.run(_await(result))
        # A return value is workload output, not memory that outlived the workload. Match
        # script workloads, whose __main__ globals are cleared before retained is sampled.
        result = None
    except BaseException as exc:  # noqa: BLE001 - report every failure, keep measuring
        meter.stop("error", exc)
        return
    meter.stop("passed")


async def _await(awaitable):
    return await awaitable


def _run_script(argv: list[str], meter: Meter) -> None:
    """Run like `python script.py`: own dir first on sys.path, __name__ == "__main__".

    compile() + exec() instead of runpy, which would preload importlib/pkgutil; compile()
    on bytes honours the file's coding declaration itself.
    """
    path = os.path.abspath(argv[0])
    sys.argv = list(argv)
    sys.path.insert(0, os.path.dirname(path))
    meter.start("workload")
    try:
        with open(path, "rb") as fh:
            code = compile(fh.read(), path, "exec")
        main = type(sys)("__main__")
        main.__file__ = path
        main.__builtins__ = __builtins__
        saved = sys.modules.get("__main__")
        sys.modules["__main__"] = main  # pickle/multiprocessing look things up here
        try:
            exec(code, main.__dict__)
        finally:
            if saved is not None:
                sys.modules["__main__"] = saved
            # The script's own globals die with the script (as with runpy / a real process);
            # "retained" must only count what outlives it: caches, module state, leaks.
            main.__dict__.clear()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            meter.stop("failed", f"SystemExit({exc.code!r})")
            return
    except BaseException as exc:  # noqa: BLE001
        meter.stop("error", exc)
        return
    meter.stop("passed")


class SetupProblem(Exception):
    """The environment cannot run this workload at any commit (not a per-commit failure)."""


def _run_pytest(argv: list[str], meter: Meter) -> int:
    try:
        import pytest
    except ImportError:
        raise SetupProblem(f"pytest is not installed in {sys.executable}; install it there or "
                           "pass --python with the interpreter your tests use") from None

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

    args = list(argv) + pytest_extra_args()
    return int(pytest.main(args, plugins=[Plugin()]))


def pytest_extra_args() -> list[str]:
    """Flags that keep measurements valid whatever the project's pytest config says.

    Tests must run in this process (xdist workers would be invisible to us), in a fixed
    order (pytest-randomly would move lazy-import costs between tests), and without
    coverage tracing (which allocates on every line and slows everything down).
    """
    args = ["-q", "-p", "no:cacheprovider", "--no-header", "-p", "no:randomly"]
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        return args
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        # Python 3.9 returns a dict and has no group= keyword; 3.10+ has .select()
        group = eps.select(group="pytest11") if hasattr(eps, "select") else eps.get("pytest11", [])
        plugins = {ep.name for ep in group}
    except Exception:  # noqa: BLE001 - metadata problems must not stop the measurement
        plugins = set()
    if "xdist.plugin" in plugins or "xdist" in plugins:
        args += ["-n", "0"]
    if "pytest_cov" in plugins:
        args += ["--no-cov"]
    return args


def _format_exc(exc: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def run(spec: dict) -> dict:
    root = os.path.realpath(spec["root"])
    dirs = _setup_paths(root, spec.get("pythonpath"))
    attributor = Attributor(root, exclude_files={__file__})
    meter = Meter(int(spec.get("nframe", 16)), attributor, spec.get("hints") or {},
                  attribute=spec.get("attribute", True),
                  peak_mode=spec.get("peak_mode", "poll"),
                  hint_fraction=spec.get("hint_fraction", HINT_FRACTION))
    kind, _, target = spec["workload"].partition(":")
    exit_code = 0
    if kind == "call":
        _run_call(target, meter)
    elif kind == "script":
        _run_script(spec["argv"], meter)
    elif kind == "pytest":
        exit_code = _run_pytest(spec["argv"], meter)
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


def read_spec(path: str) -> dict:
    """The spec is a trusted Python literal written by memblame into a private temp dir."""
    with open(path, encoding="utf-8") as fh:
        return eval(fh.read(), {"__builtins__": {}})  # noqa: S307 - no json/ast import (see top)


def main() -> None:
    spec = read_spec(sys.argv[1])
    try:
        result = run(spec)
    except SetupProblem as exc:
        result = {"schema": SCHEMA, "setup_error": str(exc)}
    except BaseException as exc:  # noqa: BLE001 - the parent needs a readable failure
        result = {"schema": SCHEMA, "fatal": _format_exc(exc)}
    import json  # only now: tracing has finished

    with open(spec["out"], "w", encoding="utf-8") as fh:
        json.dump(result, fh)


if __name__ == "__main__":
    main()
