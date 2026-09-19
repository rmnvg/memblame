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


def _run_once(python: str, root: Path, s: Settings, nframe: int, hints: dict | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="mb-run-") as tmp:
        spec_path, out_path = Path(tmp, "spec.json"), Path(tmp, "out.json")
        spec = {
            "workload": s.workload,
            "root": str(root),
            "pythonpath": s.pythonpath,
            "nframe": nframe,
            "hints": hints,
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
    result["stdout_tail"] = (proc.stdout or "")[-2000:]
    return result


def _stats(samples: list[int]) -> dict:
    return {
        "median": int(statistics.median(samples)),
        "min": min(samples),
        "max": max(samples),
        "samples": samples,
    }


def measure(python: str, root: Path, s: Settings) -> dict:
    """Measure one checkout: `runs` identical fast runs, then one attribution run.

    The numbers (median, spread) come only from the fast runs. The attribution run uses the
    full traceback depth and the peak hook, primed with the peak seen in the fast runs; the
    hook shifts allocation timing slightly (measured ~2% on the fixture), so its own peak is
    not mixed into the samples.
    """
    fast = [_run_once(python, root, s, FAST_NFRAME, None) for _ in range(max(s.runs, 1))]
    hints = {
        name: max(r["peak_bytes"] for r in _units(fast, name)) for name in _unit_names(fast)
    }
    deep = _run_once(python, root, s, s.nframe, hints)

    units = {}
    warnings: list[str] = []
    for u in deep["units"]:
        name = u["name"]
        same = _units(fast, name) or [u]
        at_peak, retained = u["at_peak"], u["retained"]
        units[name] = {
            "outcome": u["outcome"],
            "error": u.get("error"),
            "peak": _stats([r["peak_bytes"] for r in same]),
            "end": _stats([r["end_bytes"] for r in same]),
            "duration_s": u["duration_s"],
            "at_peak": at_peak,
            "retained": retained,
        }
        for label, summary in (("peak", at_peak), ("retained", retained)):
            if summary and summary["total"] > 1_000_000 and summary["truncated"] > 0.05 * summary[
                "total"
            ]:
                warnings.append(
                    f"{name}: {summary['truncated'] / summary['total']:.0%} of {label} memory has "
                    f"no project frame within {s.nframe} frames; try --nframe {s.nframe * 2}"
                )
        if u["outcome"] != "passed":
            warnings.append(f"{name}: workload {u['outcome']}")
    return {
        "schema": SCHEMA,
        "tool_version": __version__,
        "python": deep["python"],
        "executable": deep["executable"],
        "platform": deep["platform"],
        "runs": len(fast),
        "nframe": s.nframe,
        "env_problems": deep["env_problems"],
        "valid": not deep["env_problems"],
        "units": units,
        "functions": deep["functions"],
        "warnings": warnings,
    }


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
