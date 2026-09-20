"""Run the workload several times at one checkout and aggregate the results."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__
from .runner import SCHEMA

RUNNER = Path(__file__).with_name("runner.py")
FAST_NFRAME = 1  # peak bytes do not depend on traceback depth, so timing runs stay cheap
AGREE_REL, AGREE_ABS = 0.001, 8192  # two fast runs this close -> skip the remaining runs
MIN_COVERAGE = 0.9  # a peak snapshot must hold >= 90% of the peak, else retry with the hook
HOOK_HINT_FRACTION = 0.5  # the hook retry snapshots from 50% of the peak (<= ~35 snapshots)


class MeasureError(RuntimeError):
    pass


class SetupError(MeasureError):
    """Wrong for every commit (e.g. pytest missing): stop, don't skip commit after commit."""


@dataclass
class Settings:
    workload: str
    runs: int = 3
    nframe: int = 16
    pythonpath: list[str] | None = None
    python: str | None = None
    timeout: float = 900.0
    extra_env: dict[str, str] = field(default_factory=dict)
    cache_env: list[str] = field(default_factory=list)
    cache_inputs: list[str] = field(default_factory=list)

    def fingerprint(self) -> dict:
        d = asdict(self)
        d.pop("timeout")
        return d


def find_python(repo: Path, explicit: str | None = None) -> str:
    """The interpreter that has the project's dependencies installed."""
    if explicit:
        return explicit
    candidates = [Path(os.environ[v]) for v in ("VIRTUAL_ENV", "CONDA_PREFIX") if os.environ.get(v)]
    candidates += [repo / ".venv", repo / "venv"]
    for base in candidates:
        for rel in ("bin/python", "Scripts/python.exe", "python.exe"):
            if (base / rel).exists():
                return str(base / rel)
    return sys.executable


def check_interpreter(python: str) -> None:
    """Fail fast (not per commit) if the project interpreter cannot run the runner."""
    try:
        proc = subprocess.run([python, "-c", "import sys; print(*sys.version_info[:2])"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MeasureError(f"cannot run project interpreter {python}: "
                           f"{getattr(exc, 'strerror', None) or exc}. Pass --python with your "
                           "project's interpreter.") from None
    try:
        version = tuple(int(x) for x in proc.stdout.split())
    except ValueError:
        version = ()
    if proc.returncode != 0 or len(version) != 2:
        raise MeasureError(f"{python} does not look like a Python interpreter: "
                           f"{(proc.stderr or proc.stdout).strip()[-300:]}")
    if version < (3, 9):
        raise MeasureError(f"{python} is Python {version[0]}.{version[1]}; memblame needs 3.9+")


def environment_fingerprint(python: str) -> str:
    """Hash of interpreter version + installed distributions (cache invalidation)."""
    code = (
        "import sys, json, importlib.metadata as m\n"
        "d = sorted(f\"{x.metadata['Name']}=={x.version}\" for x in m.distributions())\n"
        "print(json.dumps([sys.version, d]))"
    )
    try:
        proc = subprocess.run([python, "-c", code], capture_output=True, text=True)
    except OSError as exc:
        raise MeasureError(f"cannot run project interpreter {python}: {exc.strerror or exc}. "
                           "Pass --python with your project's interpreter.") from None
    if proc.returncode != 0:
        raise MeasureError(f"cannot run project interpreter {python}: {proc.stderr.strip()}")
    return hashlib.sha256(proc.stdout.encode()).hexdigest()[:16]


def _run_once(python: str, root: Path, s: Settings, nframe: int, hints: dict | None,
              attribute: bool, peak_mode: str = "poll", hint_fraction: float = 0.9) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="mb-run-"))
    try:
        spec_path, out_path = tmp / "spec.json", tmp / "out.json"
        kind, _, target = s.workload.partition(":")
        spec = {
            "workload": s.workload,
            "argv": split_args(target) if kind in ("script", "pytest") else [],
            "root": str(root),
            "pythonpath": s.pythonpath,
            "nframe": nframe,
            "hints": hints,
            "attribute": attribute,
            "peak_mode": peak_mode,
            "hint_fraction": hint_fraction,
            "out": str(out_path),
        }
        spec_path.write_text(repr(spec), encoding="utf-8")  # read by eval: see runner.read_spec
        env = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
               **s.extra_env}
        returncode, stdout, stderr = _launch(python, spec_path, root, env, tmp, s.timeout)
        if not out_path.exists():
            tail = (stderr or stdout)[-3000:]
            raise MeasureError(f"runner crashed (exit {returncode}):\n{tail}")
        result = json.loads(out_path.read_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # a lingering child may still hold a log open
    result["output_tail"] = (stdout + stderr)[-1500:]
    if "setup_error" in result:
        raise SetupError(result["setup_error"])
    if "fatal" in result:
        raise MeasureError(result["fatal"])
    if result.get("schema") != SCHEMA:
        raise MeasureError("runner schema mismatch")
    return result


LOG_TAIL_BYTES = 8192


def _launch(python: str, spec_path: Path, root: Path, env: dict, tmp: Path,
            timeout: float) -> tuple[int, str, str]:
    """Run the runner in its own process group and wait for *it* (not for pipe EOF).

    Output goes to files: a workload can print without ever blocking on a full pipe, and a
    descendant that outlives the runner (or escapes its process group) cannot keep us
    waiting for an end-of-file that never comes. stdin is closed, so a workload that calls
    input() fails fast instead of waiting on the user's terminal.
    """
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    out_log, err_log = tmp / "stdout.log", tmp / "stderr.log"
    proc = None
    try:
        with open(out_log, "wb") as out_fh, open(err_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [python, str(RUNNER), str(spec_path)], cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh,
                start_new_session=os.name != "nt", creationflags=creationflags,
            )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(proc)
                raise MeasureError(f"workload timed out after {timeout:.0f}s") from None
    except OSError as exc:
        raise MeasureError(f"cannot run project interpreter {python}: "
                           f"{exc.strerror or exc}") from None
    except BaseException:  # Ctrl-C / SIGTERM from the editor: leave nothing running
        if proc is not None:
            _terminate_process_tree(proc)
        raise
    _terminate_process_tree(proc, leftovers_only=True)  # background processes it left behind
    return proc.returncode, _log_tail(out_log), _log_tail(err_log)


def _log_tail(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _terminate_process_tree(proc: subprocess.Popen, leftovers_only: bool = False) -> None:
    """Best-effort termination of a runner and every process it launched.

    With `leftovers_only` the runner has already exited normally; only processes still in
    its process group (background children of the workload) are removed. On Windows a
    finished parent no longer identifies its children, so nothing can be done then.
    """
    if os.name == "nt":
        if leftovers_only:
            return
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, check=False)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    if not leftovers_only:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    # The group leader may exit before a stubborn descendant. Kill the group once more.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if not leftovers_only:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def split_args(text: str) -> list[str]:
    """Split workload arguments like the platform's shell.

    POSIX rules treat backslashes as escapes, which would turn a Windows path such as
    C:\\bench\\run.py into C:benchrun.py; on Windows preserve backslashes while still
    joining adjacent quoted fragments (including quoted pytest parameter IDs).
    """
    if os.name != "nt":
        return shlex.split(text)
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def normalize_workload(repo: Path, workload: str) -> str:
    """Pin absolute repo-local scripts to each checkout; external scripts stay fixed."""
    kind, _, target = workload.partition(":")
    if kind != "script":
        return workload
    argv = split_args(target)
    if not argv or not Path(argv[0]).is_absolute():
        return workload
    try:
        rel = Path(argv[0]).resolve().relative_to(repo.resolve())
    except ValueError:
        return workload
    argv[0] = rel.as_posix()
    return "script:" + " ".join(shlex.quote(arg) for arg in argv)


def _outcomes(run: dict) -> dict[str, str]:
    return {u["name"]: u["outcome"] for u in run["units"]}


def _stats(samples: list[int]) -> dict:
    return {
        "median": int(statistics.median(samples)),
        "min": min(samples),
        "max": max(samples),
        "samples": samples,
    }


def _agree(runs: list[dict]) -> bool:
    """Two runs agree when every unit's numbers match within 0.1% (or 8 KB)."""
    a, b = runs[-2], runs[-1]
    if (_outcomes(a) != _outcomes(b) or a.get("exit_code") != b.get("exit_code")
            or a["env_problems"] or b["env_problems"]):
        return False
    ua = {u["name"]: u for u in a["units"]}
    for u in b["units"]:
        v = ua.get(u["name"])
        if v is None:
            return False
        for key in ("peak_bytes", "end_bytes"):
            if abs(u[key] - v[key]) > max(AGREE_REL * max(u[key], v[key]), AGREE_ABS):
                return False
    return len(ua) == len(b["units"])


def measure(python: str, root: Path, s: Settings, attribute: bool = True) -> dict:
    """Measure one checkout.

    Numbers (median, spread) come from up to `runs` identical fast runs (traceback depth 1,
    no snapshots). tracemalloc byte counts are nearly deterministic, so we stop after two
    runs that agree. Attribution (which function holds the memory) needs a much slower
    deep run and is only done when asked, see `add_attribution`.
    """
    fast: list[dict] = []
    while len(fast) < max(s.runs, 1):
        fast.append(_run_once(python, root, s, FAST_NFRAME, None, attribute=False))
        if (_outcomes(fast[-1]) != _outcomes(fast[0])
                or fast[-1].get("exit_code") != fast[0].get("exit_code")):
            raise MeasureError("inconsistent workload across runs: unit names, outcomes or exit "
                               "codes changed; measurements were not combined")
        if len(fast) >= 2 and _agree(fast):
            break
    units = {}
    warnings: list[str] = []
    for name in _unit_names(fast):
        same = _units(fast, name)
        last = same[-1]
        units[name] = {
            "outcome": last["outcome"],
            "error": last.get("error"),
            "peak": _stats([r["peak_bytes"] for r in same]),
            "end": _stats([r["end_bytes"] for r in same]),
            "duration_s": last["duration_s"],
            "at_peak": None,
            "retained": None,
        }
        if last["outcome"] != "passed":
            warnings.append(f"{name}: workload {last['outcome']}{_hint(last, python)}")
    if not units:
        tail = fast[0].get("output_tail", "").strip().splitlines()[-3:]
        raise MeasureError("workload produced no measurements (pytest exit code "
                           f"{fast[0].get('exit_code')}): {' | '.join(tail)}")
    # Collection errors and interrupted pytest runs may leave some units behind.
    if any(r.get("exit_code", 0) not in (0, 1) for r in fast):
        raise MeasureError(f"workload did not complete (pytest exit code {fast[0]['exit_code']})")
    first = fast[0]
    env_problems = sorted({p for r in fast for p in r["env_problems"]})
    result = {
        "schema": SCHEMA,
        "tool_version": __version__,
        "python": first["python"],
        "executable": first["executable"],
        "platform": first["platform"],
        "runs": len(fast),
        "nframe": s.nframe,
        "env_problems": env_problems,
        "valid": not env_problems,
        "units": units,
        "functions": {},
        "attributed": False,
        "warnings": warnings,
    }
    if attribute and result["valid"]:
        add_attribution(python, root, s, result)
    return result


def add_attribution(python: str, root: Path, s: Settings, result: dict) -> dict:
    """Deep run(s): full traceback depth plus a snapshot taken near the known peak.

    First with a cheap polling thread. Peaks that live for less than a poll interval (e.g.
    a temporary built and freed inside one C call) are missed, which shows as low coverage;
    only then re-run those units with the exact but much slower profile hook. The deep runs
    shift allocation timing slightly, so their numbers are not mixed into the samples.
    """
    hints = {name: u["peak"]["max"] for name, u in result["units"].items()}
    deep = _run_once(python, root, s, s.nframe, hints, attribute=True, peak_mode="poll")
    _validate_attribution(result, deep)
    # Includes workloads too quick for the poller to see at all (cheap to hook anyway).
    missed = {u["name"] for u in deep["units"]
              if ((u["at_peak"] or {}).get("coverage") or 0) < MIN_COVERAGE}
    if missed:
        # Exact hook, snapshotting from half the peak: when the true peak is a temporary
        # inside one C call (never observable), we still get the largest observable state.
        exact = _run_once(python, root, s, s.nframe, hints, attribute=True, peak_mode="hook",
                          hint_fraction=HOOK_HINT_FRACTION)
        _validate_attribution(result, exact)
    _merge_attribution(result, deep, set(hints))
    if missed:
        _merge_attribution(result, exact, missed, keep_better=True)
    result["functions"] = {**deep["functions"], **(exact["functions"] if missed else {})}
    result["attributed"] = True
    return result


def _validate_attribution(result: dict, run: dict) -> None:
    expected = {name: u["outcome"] for name, u in result["units"].items()}
    if run["env_problems"]:
        raise MeasureError("attribution imported project modules outside the checkout: "
                           + "; ".join(run["env_problems"]))
    if _outcomes(run) != expected or run.get("exit_code", 0) not in (0, 1):
        raise MeasureError("attribution workload differs from measured runs; unit names, "
                           "outcomes or exit codes changed")


def _merge_attribution(result: dict, run: dict, names: set[str], keep_better: bool = False,
                       ) -> None:
    for u in run["units"]:
        target = result["units"].get(u["name"])
        if target is None or u["name"] not in names:
            continue
        old_cov = (target["at_peak"] or {}).get("coverage") or 0
        new_cov = (u["at_peak"] or {}).get("coverage") or 0
        if not keep_better or new_cov > old_cov:
            target["at_peak"] = u["at_peak"]
        if not keep_better or target["retained"] is None:
            target["retained"] = u["retained"]


def _hint(unit: dict, python: str) -> str:
    """Turn the most common setup mistake into a readable hint."""
    error = unit.get("error") or ""
    last = next((ln.strip() for ln in reversed(error.splitlines()) if ln.strip()), "")
    if "ModuleNotFoundError" in error or "No module named" in error:
        missing = error.rsplit("No module named", 1)[-1].strip().splitlines()[0]
        return (f" (No module named {missing}; the interpreter used was {python}. Pass --python "
                "with the interpreter that has your project's dependencies)")
    return f": {last[:200]}" if last else ""


def _unit_names(runs: list[dict]) -> list[str]:
    names: list[str] = []
    for r in runs:
        for u in r["units"]:
            if u["name"] not in names:
                names.append(u["name"])
    return names


def _units(runs: list[dict], name: str) -> list[dict]:
    return [u for r in runs for u in r["units"] if u["name"] == name]


# --------------------------------------------------------------------------- cache


def engine_hash() -> str:
    """Hash of memblame's own measuring code: upgrading memblame invalidates old results."""
    h = hashlib.sha256()
    for name in ("runner.py", "measure.py"):
        h.update(RUNNER.with_name(name).read_bytes())
    return h.hexdigest()[:12]


def external_script_hash(repo: Path, workload: str) -> str:
    """Content hash of a `script:` file that lives outside the repository.

    Files inside the repo are pinned by the commit SHA; a benchmark kept elsewhere is not,
    so editing it must invalidate cached measurements.
    """
    kind, _, target = workload.partition(":")
    if kind != "script" or not target.strip():
        return ""
    path = Path(split_args(target)[0])
    if not path.is_absolute():
        return ""
    try:
        path.resolve().relative_to(repo.resolve())
        return ""
    except ValueError:
        pass
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def workload_inputs_hash(repo: Path, settings: Settings) -> str:
    """Hash user-declared inputs that are not represented by a commit SHA.

    The effective value of each declared environment variable is included without writing
    the value to disk. Files may be absolute or relative to the repository; directories are
    hashed recursively so generated datasets can be declared as one input.
    """
    h = hashlib.sha256()
    env = {**os.environ, **settings.extra_env}
    for name in sorted(set(settings.cache_env)):
        h.update(b"env\0")
        h.update(name.encode("utf-8", errors="surrogateescape"))
        h.update(b"\0")
        value = env.get(name)
        h.update(b"missing" if value is None else value.encode("utf-8", errors="surrogateescape"))
        h.update(b"\0")

    for declared in sorted(set(settings.cache_inputs)):
        path = Path(declared).expanduser()
        if not path.is_absolute():
            path = repo / path
        h.update(b"path\0")
        h.update(declared.encode("utf-8", errors="surrogateescape"))
        h.update(b"\0")
        try:
            if path.is_dir():
                files = sorted(p for p in path.rglob("*") if p.is_file())
                for child in files:
                    h.update(child.relative_to(path).as_posix().encode("utf-8"))
                    h.update(b"\0")
                    h.update(child.read_bytes())
                    h.update(b"\0")
            else:
                h.update(path.read_bytes())
        except OSError as exc:
            h.update(f"unreadable:{type(exc).__name__}:{getattr(exc, 'errno', None)}".encode())
        h.update(b"\0")
    return h.hexdigest()[:20]


class Cache:
    """Measurements keyed by commit + everything that could change the numbers."""

    def __init__(self, repo: Path, python: str, settings: Settings, enabled: bool = True):
        self.dir = repo / ".memblame" / "cache"
        self.enabled = enabled
        self._salt = ""
        if enabled:
            self._salt = json.dumps(
                [settings.fingerprint(), environment_fingerprint(python), engine_hash(), SCHEMA,
                 external_script_hash(repo, settings.workload),
                 workload_inputs_hash(repo, settings)],
                sort_keys=True,
            )

    def _path(self, sha: str) -> Path:
        key = hashlib.sha256(f"{sha}|{self._salt}".encode()).hexdigest()[:24]
        return self.dir / f"{sha[:12]}-{key}.json"

    def get(self, sha: str) -> dict | None:
        if not self.enabled or sha == "WORKTREE":
            return None
        p = self._path(sha)
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def put(self, sha: str, result: dict) -> None:
        if not self.enabled or sha == "WORKTREE":
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.dir.parent / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.dir,
                prefix=f".{sha[:12]}-", suffix=".tmp", delete=False,
            ) as fh:
                json.dump(result, fh)
                tmp = Path(fh.name)
            os.replace(tmp, self._path(sha))
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
