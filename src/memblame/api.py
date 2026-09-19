"""High-level operations. Every function returns a JSON-serialisable dict (schema 1)."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

from . import blame, git
from .measure import (
    Cache,
    MeasureError,
    Settings,
    SetupError,
    add_attribution,
    check_interpreter,
    find_python,
    measure,
)
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
        check_interpreter(self.python)
        self.cache = Cache(self.repo, self.python, settings, enabled=use_cache)
        self.progress = progress
        self.measured = 0  # fresh (uncached) measurements, for tests and bisect stats
        self._pool = git.WorktreePool(self.repo)
        self._memo: dict[str, dict] = {}
        self._commits: dict[str, git.Commit] = {}

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def header(self, kind: str) -> dict:
        return {"schema": SCHEMA, "kind": kind, "repo": str(self.repo),
                "workload": self.settings.workload, "python": self.python}

    def result(self, rev: str, label: str = "", attribute: bool = False,
               ) -> tuple[git.Commit, dict]:
        """Measurement for a revision: cache, then memo, then a fresh measurement.

        attribute=False returns numbers only; attribution is added lazily (and cached) only
        for the commits where a significant change needs explaining.
        """
        sha = git.resolve(self.repo, rev)
        commit = self._commits.get(sha) or git.commit_info(self.repo, sha)
        self._commits[sha] = commit
        res = self._memo.get(sha) or self.cache.get(sha)
        if res is not None and (res.get("attributed") or not attribute or not res["valid"]):
            if sha not in self._memo:
                self.progress(f"{label}{commit.short} cached")
            self._memo[sha] = res
            return commit, res
        root = self._pool.checkout(sha)
        if res is None:
            self.progress(f"{label}measuring {commit.short} {commit.subject[:50]!r}")
            try:
                res = measure(self.python, root, self.settings, attribute=attribute)
            except SetupError:
                raise
            except MeasureError as exc:
                # A commit that cannot be measured (crash, timeout) is skipped, not fatal:
                # like `git bisect skip`. Not cached, so a retry measures it again.
                res = failed_result(str(exc))
            self.measured += 1
        else:
            self.progress(f"{label}attributing {commit.short} {commit.subject[:50]!r}")
            try:
                add_attribution(self.python, root, self.settings, res)
            except MeasureError as exc:
                res["warnings"].append(f"attribution run failed: {str(exc)[-300:]}")
                res["attributed"] = True  # keep the numbers; don't retry within this session
                self._memo[sha] = res
                return commit, res
        self._memo[sha] = res
        if res["valid"] and not res.get("error"):
            self.cache.put(sha, res)
        return commit, res


def failed_result(error: str) -> dict:
    return {"schema": SCHEMA, "valid": False, "error": error, "env_problems": [], "units": {},
            "functions": {}, "attributed": True, "warnings": [], "runs": 0}


def run(session: Session, rev: str = git.WORKTREE) -> dict:
    commit, res = session.result(rev, attribute=True)
    return {**session.header("run"), "commit": commit.to_json(), "result": res,
            "warnings": _warnings(commit, res)}


def _compare(session: Session, a: tuple[git.Commit, dict], b: tuple[git.Commit, dict],
             ) -> tuple[dict, tuple[git.Commit, dict], tuple[git.Commit, dict]]:
    """Compare two measurements, attributing both sides only if something changed."""
    cmp = blame.compare(session.repo, a[1], b[1], a[0].sha, b[0].sha)
    if cmp["findings"] and not (a[1]["attributed"] and b[1]["attributed"]):
        a = session.result(a[0].sha, "", attribute=True)
        b = session.result(b[0].sha, "", attribute=True)
        cmp = blame.compare(session.repo, a[1], b[1], a[0].sha, b[0].sha)
    return cmp, a, b


def diff(session: Session, base: str, head: str = git.WORKTREE) -> dict:
    notes = []
    if head == git.WORKTREE and not git.is_dirty(session.repo):
        # Nothing uncommitted: measuring the same code twice would only double the wait.
        head = "HEAD"
        notes.append("working tree has no uncommitted Python changes; compared HEAD with itself"
                     if git.resolve(session.repo, base) == git.resolve(session.repo, "HEAD")
                     else "working tree is clean; measured HEAD instead")
    a = session.result(base, "[1/2] ")
    b = session.result(head, "[2/2] ")
    out = {**session.header("diff"), "base": a[0].to_json(), "head": b[0].to_json(),
           "notes": notes}
    out["valid"] = a[1]["valid"] and b[1]["valid"]
    if out["valid"]:
        cmp, a, b = _compare(session, a, b)
        out.update(cmp)
        out["results"] = {"base": _brief(a[1]), "head": _brief(b[1])}
    out["warnings"] = _dedupe(_warnings(*a) + _warnings(*b))
    return out


def _differs(a: dict, b: dict) -> bool:
    """Is there any significant difference (or a change in validity/outcome) between two?"""
    if a["valid"] != b["valid"] or set(a["units"]) != set(b["units"]):
        return True
    for name, ua in a["units"].items():
        ub = b["units"][name]
        if ua["outcome"] != ub["outcome"]:
            return True
        for stat_key, _ in blame.METRICS.values():
            delta = ub[stat_key]["median"] - ua[stat_key]["median"]
            if abs(delta) > blame.noise_band(ua[stat_key], ub[stat_key]):
                return True
    return False


def range_(session: Session, base: str, head: str, exhaustive: bool = False) -> dict:
    """Memory over a first-parent commit range.

    Adaptive by default: measure both ends and only subdivide segments whose ends differ
    significantly (about log2(N) measurements per change instead of N). A change that is
    later exactly undone inside one unsplit segment is missed; use exhaustive=True to
    measure every commit.
    """
    _require_ancestor(session.repo, base, head)
    shas = git.first_parent_range(session.repo, base, head)
    n = len(shas)
    measured: dict[int, tuple[git.Commit, dict]] = {}

    def get(i: int) -> tuple[git.Commit, dict]:
        if i not in measured:
            measured[i] = session.result(shas[i], f"[{len(measured) + 1}/{n}] ")
        return measured[i]

    if exhaustive:
        for i in range(n):
            get(i)
    else:
        get(0)
        get(n - 1)
        todo = [(0, n - 1)]
        while todo:
            lo, hi = todo.pop()
            if hi - lo > 1 and _differs(get(lo)[1], get(hi)[1]):
                mid = (lo + hi) // 2
                get(mid)
                todo += [(mid, hi), (lo, mid)]

    steps = []
    order = sorted(measured)
    for lo, hi in zip(order, order[1:]):  # noqa: B905 - py3.9 has no strict=
        a, b = measured[lo], measured[hi]
        if not (a[1]["valid"] and b[1]["valid"]):
            continue
        cmp, a, b = _compare(session, a, b)
        measured[lo], measured[hi] = a, b
        steps.append({"base": a[0].sha, "head": b[0].sha, "commits": hi - lo,
                      "findings": cmp["findings"]})
    commits = git.commit_infos(session.repo, shas)
    points, warnings = [], []
    for i, commit in enumerate(commits):
        if i in measured:
            res = measured[i][1]
            warnings += _warnings(commit, res)
            points.append({"commit": commit.to_json(), "measured": True, "valid": res["valid"],
                           **_brief(res)})
        else:
            points.append({"commit": commit.to_json(), "measured": False, "valid": True,
                           "units": {}})
    findings = [
        {**f, "commit": s["head"], "parent": s["base"]} for s in steps for f in s["findings"]
    ]
    findings.sort(key=lambda f: -abs(f["delta"]))
    return {**session.header("range"), "mode": "exhaustive" if exhaustive else "adaptive",
            "measured": len(measured), "points": points, "steps": steps,
            "findings": findings, "warnings": _dedupe(warnings)}


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
    _require_ancestor(session.repo, good, bad)
    shas = git.first_parent_range(session.repo, good, bad)
    if len(shas) < 2:
        raise ValueError("good and bad are the same commit")
    out = session.header("bisect")
    g_commit, g = session.result(shas[0], "good ")
    b_commit, b = session.result(shas[-1], "bad ")
    out.update(good=g_commit.to_json(), bad=b_commit.to_json(), candidates=len(shas) - 2)
    broken = [c.short for c, r in ((g_commit, g), (b_commit, b)) if not r["valid"]]
    if broken:
        return {**out, "status": "error",
                "warnings": _warnings(g_commit, g) + _warnings(b_commit, b),
                "message": f"cannot measure {', '.join(broken)}; see warnings"}
    target = _pick_target(g, b, unit, metric)
    if target is None:
        return {**out, "status": "no_regression",
                "message": "bad is not significantly worse than good for any unit/metric"}
    _, unit_name, metric_name = target
    key = blame.METRICS[metric_name][0]

    endpoint_outcomes = {g["units"][unit_name]["outcome"], b["units"][unit_name]["outcome"]}

    def value(res: dict) -> int | None:
        """None = skip this commit: not measurable, or the workload behaved differently
        (e.g. crashed early and so used little memory) from both known endpoints."""
        u = res["units"].get(unit_name)
        if not (u and res["valid"]) or u["outcome"] not in endpoint_outcomes:
            return None
        return u[key]["median"]

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
    skipped: set[int] = set()
    lo, hi = 0, len(shas) - 1
    while True:
        # Nearest-to-the-middle commit strictly between lo and hi that is not skipped.
        order = sorted(range(lo + 1, hi), key=lambda i: abs(i - (lo + hi) / 2))
        mid = next((i for i in order if i not in skipped), None)
        if mid is None:
            break
        measured[mid] = session.result(shas[mid], f"step {len(measured) - 1} ")
        v = value(measured[mid][1])
        if v is None:
            skipped.add(mid)
        elif v > limit:
            hi = mid
        else:
            lo = mid
    parent, culprit = measured[lo], measured[hi]
    trail = [{"commit": measured[i][0].to_json(), "value": value(measured[i][1]),
              "bad": (value(measured[i][1]) or 0) > limit, "skipped": i in skipped}
             for i in sorted(measured)]
    between = [shas[i] for i in range(lo + 1, hi)]
    flags = [t["bad"] for t in trail if not t["skipped"]]
    monotonic = flags == sorted(flags)  # all good commits come before all bad ones
    cmp, parent, culprit = _compare(session, parent, culprit)
    out.update(
        status="found", culprit=culprit[0].to_json(), parent=parent[0].to_json(),
        measurements=trail, steps=len(measured) - 2, monotonic=monotonic, **cmp,
    )
    out["warnings"] = []
    if between:
        out["culprit_range"] = between + [culprit[0].sha]
        out["warnings"].append(
            f"{len(between)} commit(s) right before the culprit could not be measured (skipped); "
            f"the regression is in one of {len(between) + 1} commits ending at "
            f"{culprit[0].short}")
    if not monotonic:
        out["warnings"].append(
            "memory is not monotonic in this range; the culprit is *a* transition past the "
            "threshold, not necessarily the first. Run `memblame range` to see all commits."
        )
    return out


def _require_ancestor(repo: Path, older: str, newer: str) -> None:
    if not git.is_ancestor(repo, git.resolve(repo, older), git.resolve(repo, newer)):
        raise ValueError(f"{older} is not an ancestor of {newer}; memblame follows the "
                         "first-parent history from the older to the newer commit")


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


def _dedupe(warnings: list[str]) -> list[str]:
    """Collapse 'sha: message' lines repeated across commits into one line."""
    by_msg: dict[str, list[str]] = {}
    for w in warnings:
        short, sep, msg = w.partition(": ")
        by_msg.setdefault(msg if sep else w, []).append(short)
    out = []
    for msg, shorts in by_msg.items():
        out.append(f"{shorts[0]}: {msg}" if len(shorts) == 1 else f"{msg} ({len(shorts)} commits)")
    return out


def _warnings(commit: git.Commit, res: dict) -> list[str]:
    out = [f"{commit.short}: {w}" for w in res.get("warnings", [])]
    if res.get("error"):
        out.append(f"{commit.short}: SKIPPED - could not be measured: {res['error'][-300:]}")
    elif not res["valid"]:
        out.append(
            f"{commit.short}: INVALID ENVIRONMENT - project modules were imported from outside "
            f"the checkout ({'; '.join(res['env_problems'][:3])}). Set --pythonpath (e.g. src) "
            "or uninstall the editable install."
        )
    return out
