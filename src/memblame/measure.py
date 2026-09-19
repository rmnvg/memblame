"""Run the workload several times at one checkout and aggregate the results."""

from __future__ import annotations

import hashlib
import json
import os
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


class MeasureError(RuntimeError):
    pass


@dataclass
class Settings:
    workload: str
    runs: int = 3
    nframe: int = 16
    pythonpath: list[str] | None = None
    python: str | None = None
    timeout: float = 900.0
    extra_env: dict[str, str] = field(default_factory=dict)

    def fingerprint(self) -> dict:
        d = asdict(self)
        d.pop("timeout")
        return d


def find_python(repo: Path, explicit: str | None = None) -> str:
    """The interpreter that has the project's dependencies installed."""
    if explicit:
        return explicit
    venv = os.environ.get("VIRTUAL_ENV")
    candidates = [Path(venv)] if venv else []
    candidates += [repo / ".venv", repo / "venv"]
    for base in candidates:
        for rel in ("bin/python", "Scripts/python.exe"):
            if (base / rel).exists():
                return str(base / rel)
    return sys.executable


def environment_fingerprint(python: str) -> str:
    """Hash of interpreter version + installed distributions (cache invalidation)."""
    code = (
        "import sys, json, importlib.metadata as m\n"
        "d = sorted(f\"{x.metadata['Name']}=={x.version}\" for x in m.distributions())\n"
        "print(json.dumps([sys.version, d]))"
    )
    proc = subprocess.run([python, "-c", code], capture_output=True, text=True)
    if proc.returncode != 0:
        raise MeasureError(f"cannot run project interpreter {python}: {proc.stderr.strip()}")
    return hashlib.sha256(proc.stdout.encode()).hexdigest()[:16]


def _run_once(python: str, root: Path, s: Settings, nframe: int, hints: dict | None,
              attribute: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="mb-run-") as tmp:
        spec_path, out_path = Path(tmp, "spec.json"), Path(tmp, "out.json")
        spec = {
            "workload": s.workload,
            "root": str(root),
            "pythonpath": s.pythonpath,
            "nframe": nframe,
            "hints": hints,
            "attribute": attribute,
            "out": str(out_path),
        }
        spec_path.write_text(json.dumps(spec))
        env = {**os.environ, "PYTHONHASHSEED": "0", **s.extra_env}
        try:
            proc = subprocess.run(
                [python, str(RUNNER), str(spec_path)], cwd=root, env=env,
                capture_output=True, text=True, errors="replace", timeout=s.timeout,
            )
        except subprocess.TimeoutExpired:
            raise MeasureError(f"workload timed out after {s.timeout:.0f}s") from None
        if not out_path.exists():
            tail = (proc.stderr or proc.stdout)[-3000:]
            raise MeasureError(f"runner crashed (exit {proc.returncode}):\n{tail}")
        result = json.loads(out_path.read_text())
    if "fatal" in result:
        raise MeasureError(result["fatal"])
    if result.get("schema") != SCHEMA:
        raise MeasureError("runner schema mismatch")
    return result


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
            warnings.append(f"{name}: workload {last['outcome']}")
    first = fast[0]
    result = {
        "schema": SCHEMA,
        "tool_version": __version__,
        "python": first["python"],
        "executable": first["executable"],
        "platform": first["platform"],
        "runs": len(fast),
        "nframe": s.nframe,
        "env_problems": first["env_problems"],
        "valid": not first["env_problems"],
        "units": units,
        "functions": {},
        "attributed": False,
        "warnings": warnings,
    }
    if attribute and result["valid"]:
        add_attribution(python, root, s, result)
    return result


def add_attribution(python: str, root: Path, s: Settings, result: dict) -> dict:
    """One deep run: full traceback depth plus the peak hook primed with the known peaks.

    The hook shifts allocation timing slightly (~2% measured on the fixture), so this run's
    own numbers are not mixed into the samples.
    """
    hints = {name: u["peak"]["max"] for name, u in result["units"].items()}
    deep = _run_once(python, root, s, s.nframe, hints, attribute=True)
    for u in deep["units"]:
        target = result["units"].get(u["name"])
        if target is None:
            continue
        target["at_peak"], target["retained"] = u["at_peak"], u["retained"]
        for label, summary in (("peak", u["at_peak"]), ("retained", u["retained"])):
            if summary and summary["total"] > 1_000_000 and summary["truncated"] > 0.05 * summary[
                "total"
            ]:
                result["warnings"].append(
                    f"{u['name']}: {summary['truncated'] / summary['total']:.0%} of {label} memory "
                    f"has no project frame within {s.nframe} frames; try --nframe {s.nframe * 2}"
                )
    result["functions"] = deep["functions"]
    result["attributed"] = True
    return result


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


class Cache:
    """Measurements keyed by commit + everything that could change the numbers."""

    def __init__(self, repo: Path, python: str, settings: Settings, enabled: bool = True):
        self.dir = repo / ".memblame" / "cache"
        self.enabled = enabled
        self._salt = ""
        if enabled:
            runner_hash = hashlib.sha256(RUNNER.read_bytes()).hexdigest()[:12]
            self._salt = json.dumps(
                [settings.fingerprint(), environment_fingerprint(python), runner_hash, SCHEMA],
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
        tmp = self._path(sha).with_suffix(".tmp")
        tmp.write_text(json.dumps(result))
        os.replace(tmp, self._path(sha))
