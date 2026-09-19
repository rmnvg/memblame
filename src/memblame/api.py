"""High-level operations. Every function returns a JSON-serialisable dict (schema 1)."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

from . import blame, git
from .measure import Cache, Settings, find_python, measure
from .runner import SCHEMA

Progress = Callable[[str], None]


def _stderr(msg: str) -> None:
    print(f"memblame: {msg}", file=sys.stderr, flush=True)


class Session:
    """Shared state for one command: interpreter, cache and a reusable worktree."""

    def __init__(self, repo: Path, settings: Settings, use_cache: bool = True,
                 progress: Progress = _stderr):
        self.repo = git.repo_root(repo)
        self.settings = settings
        self.python = find_python(self.repo, settings.python)
        self.cache = Cache(self.repo, self.python, settings, enabled=use_cache)
        self.progress = progress
        self.measured = 0  # fresh (uncached) measurements, for tests and bisect stats
        self._pool = git.WorktreePool(self.repo)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def header(self, kind: str) -> dict:
        return {"schema": SCHEMA, "kind": kind, "repo": str(self.repo),
                "workload": self.settings.workload, "python": self.python}

    def result(self, rev: str, label: str = "") -> tuple[git.Commit, dict]:
        sha = git.resolve(self.repo, rev)
        commit = git.commit_info(self.repo, sha)
        cached = self.cache.get(sha)
        if cached is not None:
            self.progress(f"{label}{commit.short} cached")
            return commit, cached
        self.progress(f"{label}measuring {commit.short} {commit.subject[:50]!r}")
        root = self._pool.checkout(sha)
        res = measure(self.python, root, self.settings)
        self.measured += 1
        if res["valid"]:
            self.cache.put(sha, res)
        return commit, res


def run(session: Session, rev: str = git.WORKTREE) -> dict:
    commit, res = session.result(rev)
    return {**session.header("run"), "commit": commit.to_json(), "result": res}


def diff(session: Session, base: str, head: str = git.WORKTREE) -> dict:
    a_commit, a = session.result(base, "[1/2] ")
    b_commit, b = session.result(head, "[2/2] ")
    out = {**session.header("diff"), "base": a_commit.to_json(), "head": b_commit.to_json()}
    out["valid"] = a["valid"] and b["valid"]
    out["warnings"] = _warnings(a_commit, a) + _warnings(b_commit, b)
    if out["valid"]:
        out.update(blame.compare(session.repo, a, b, a_commit.sha, b_commit.sha))
        out["results"] = {"base": _brief(a), "head": _brief(b)}
    return out


def range_(session: Session, base: str, head: str) -> dict:
    shas = git.first_parent_range(session.repo, base, head)
    points, steps, warnings = [], [], []
    prev: tuple[git.Commit, dict] | None = None
    for i, sha in enumerate(shas, 1):
        commit, res = session.result(sha, f"[{i}/{len(shas)}] ")
        warnings += _warnings(commit, res)
        points.append({"commit": commit.to_json(), "valid": res["valid"], **_brief(res)})
        if prev is not None and prev[1]["valid"] and res["valid"]:
            cmp = blame.compare(session.repo, prev[1], res, prev[0].sha, commit.sha)
            steps.append({"base": prev[0].sha, "head": commit.sha,
                          "findings": cmp["findings"]})
        prev = (commit, res)
    findings = [
        {**f, "commit": s["head"], "parent": s["base"]} for s in steps for f in s["findings"]
    ]
    findings.sort(key=lambda f: -abs(f["delta"]))
    return {**session.header("range"), "points": points, "steps": steps,
            "findings": findings, "warnings": warnings}


_SIZE_RE = re.compile(r"^\s*([+]?)\s*([\d.]+)\s*(%|[kmg]i?b|b)?\s*$", re.IGNORECASE)
_MULT = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "kib": 2**10, "mib": 2**20,
         "gib": 2**30}


def parse_threshold(text: str, good_value: int) -> int:
    """'200MB' (absolute), '+20MB' or '+10%' (relative to the good commit)."""
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"bad threshold {text!r}; examples: 200MB, +20MB, +10%")
    plus, num, unit = m.group(1), float(m.group(2)), (m.group(3) or "b").lower()
    if unit == "%":
        return int(good_value * (1 + num / 100))
    value = int(num * _MULT[unit])
    return good_value + value if plus else value


def _pick_target(good: dict, bad: dict, unit: str | None, metric: str | None):
    best = None
    for name, b_unit in bad["units"].items():
        if unit and name != unit:
            continue
        a_unit = good["units"].get(name)
        if not a_unit:
            continue
        for m in [metric] if metric else list(blame.METRICS):
            key = blame.METRICS[m][0]
            delta = b_unit[key]["median"] - a_unit[key]["median"]
            band = blame.noise_band(a_unit[key], b_unit[key])
            score = delta / max(a_unit[key]["median"], blame.MIN_BAND)
            if (delta > band or unit or metric) and (best is None or score > best[0]):
                best = (score, name, m)
    return best


def bisect(session: Session, good: str, bad: str, threshold: str | None = None,
           unit: str | None = None, metric: str | None = None) -> dict:
    shas = git.first_parent_range(session.repo, good, bad)
    if len(shas) < 2:
        raise ValueError("good must be an ancestor of bad (on the first-parent chain)")
    out = session.header("bisect")
    g_commit, g = session.result(shas[0], "good ")
    b_commit, b = session.result(shas[-1], "bad ")
    out.update(good=g_commit.to_json(), bad=b_commit.to_json(), candidates=len(shas) - 2)
    target = _pick_target(g, b, unit, metric)
    if target is None:
        return {**out, "status": "no_regression",
                "message": "bad is not significantly worse than good for any unit/metric"}
    _, unit_name, metric_name = target
    key = blame.METRICS[metric_name][0]

    def value(res: dict) -> int | None:
        u = res["units"].get(unit_name)
        return u[key]["median"] if u and res["valid"] else None

    good_v, bad_v = value(g), value(b)
    if threshold:
        limit = parse_threshold(threshold, good_v)
    else:
        limit = good_v + blame.noise_band(g["units"][unit_name][key], b["units"][unit_name][key])
    out.update(unit=unit_name, metric=metric_name, threshold=limit)
    if bad_v <= limit:
        return {**out, "status": "no_regression",
                "message": f"bad ({bad_v} B) does not exceed the threshold ({limit} B)"}

    measured = {0: (g_commit, g), len(shas) - 1: (b_commit, b)}
    lo, hi = 0, len(shas) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        measured[mid] = session.result(shas[mid], f"step {len(measured) - 1} ")
        v = value(measured[mid][1])
        if v is None:
            raise RuntimeError(f"cannot measure {shas[mid][:9]}: invalid environment")
        if v > limit:
            hi = mid
        else:
            lo = mid
    parent, culprit = measured[lo], measured[hi]
    trail = [{"commit": measured[i][0].to_json(), "value": value(measured[i][1]),
              "bad": value(measured[i][1]) > limit} for i in sorted(measured)]
    flags = [t["bad"] for t in trail]
    monotonic = flags == sorted(flags)  # all good commits come before all bad ones
    cmp = blame.compare(session.repo, parent[1], culprit[1], parent[0].sha, culprit[0].sha)
    out.update(
        status="found", culprit=culprit[0].to_json(), parent=parent[0].to_json(),
        measurements=trail, steps=len(measured) - 2, monotonic=monotonic, **cmp,
    )
    if not monotonic:
        out.setdefault("warnings", []).append(
            "memory is not monotonic in this range; the culprit is *a* transition past the "
            "threshold, not necessarily the first. Run `memblame range` to see all commits."
        )
    return out


def _brief(res: dict) -> dict:
    """Per-unit numbers without attribution detail (for timelines)."""
    return {"units": {
        name: {"outcome": u["outcome"], "peak": u["peak"], "end": u["end"],
               "top": _top(u)}
        for name, u in res["units"].items()
    }}


def _top(unit: dict) -> list[dict]:
    summary = unit.get("at_peak") or unit.get("retained")
    if not summary:
        return []
    return sorted(summary["functions"], key=lambda f: -f["self"])[:5]


def _warnings(commit: git.Commit, res: dict) -> list[str]:
    out = [f"{commit.short}: {w}" for w in res.get("warnings", [])]
    if not res["valid"]:
        out.append(
            f"{commit.short}: INVALID ENVIRONMENT - project modules were imported from outside "
            f"the checkout ({'; '.join(res['env_problems'][:3])}). Set --pythonpath (e.g. src) "
            "or uninstall the editable install."
        )
    return out
