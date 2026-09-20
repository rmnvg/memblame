"""Compare two measurements and blame the change on a function and a diff hunk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import git
from .runner import innermost_scope, scopes_from_source

METRICS = {
    # metric name -> (unit stats key, attribution summary key)
    "peak": ("peak", "at_peak"),
    "retained": ("end", "retained"),
}
MIN_BAND = 64 * 1024  # never call anything below 64 KiB significant
REL_BAND = 0.02  # ...or below 2% of the larger measurement
SPREAD_FACTOR = 2.0
FUNC_SHARE = 0.10  # a function must explain >= 10% of the unit change to be a candidate
TIE_SHARE = 0.90  # changed functions within 90% of the best growth are treated as tied
MAX_FUNCS = 15
NOISE_BYTES = 4096  # per-function differences below this are interpreter noise
MIN_COVERAGE = 0.9  # below this share of the peak, say that the snapshot is partial
TRUNCATED_NOTE = 0.05  # mention truncated stacks when they hide more than 5% of the memory


EMPTY_SUMMARY: dict[str, Any] = {"total": 0, "coverage": 0, "unattributed": 0, "truncated": 0,
                                 "functions": [], "lines": []}


def noise_band(a: dict, b: dict) -> int:
    spread = max(a["max"] - a["min"], b["max"] - b["min"])
    return int(max(SPREAD_FACTOR * spread, REL_BAND * max(a["median"], b["median"]), MIN_BAND))


class ChangeMap:
    """Which functions a set of diff hunks touches, on either side of the diff."""

    def __init__(self, repo: Path, base: str, head: str, hunks: list[git.Hunk]):
        self.hunks = hunks
        self.by_file: dict[str, list[git.Hunk]] = {}  # new-side path
        self.by_old_file: dict[str, list[git.Hunk]] = {}
        for h in hunks:
            self.by_file.setdefault(h.file, []).append(h)
            if h.old_file:
                self.by_old_file.setdefault(h.old_file, []).append(h)
        self._repo, self._base, self._head = repo, base, head
        self._scopes: dict[tuple[str, str], list] = {}

    def scopes(self, rel: str, rev: str | None = None) -> list:
        key = (rev or self._head, rel)
        if key not in self._scopes:
            src = git.file_at(self._repo, key[0], rel)
            self._scopes[key] = scopes_from_source(src) if src else []
        return self._scopes[key]

    def hunks_for(self, info: dict | None, side: str = "new") -> list[git.Hunk]:
        """Hunks intersecting a function's lines on one side of the diff.

        '<module>' matches edits outside any function. The old side matters for memory that
        went *down*: the code that allocated it has often been deleted.
        """
        if info is None:
            return []
        old = side == "old"
        rev = self._base if old else self._head
        out = []
        for h in (self.by_old_file if old else self.by_file).get(info["file"], []):
            lo, hi = h.old_range if old else h.new_range
            if info["qualname"] == "<module>":
                scopes = self.scopes(info["file"], rev)
                if any(innermost_scope(scopes, ln) is None for ln in range(lo, hi + 1)):
                    out.append(h)
            elif lo <= info["end"] and hi >= info["start"]:
                out.append(h)
        return out

    def changed_functions(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for rel, hunks in self.by_file.items():
            scopes = self.scopes(rel)
            for h in hunks:
                lo, hi = h.new_range
                for ln in range(lo, hi + 1):
                    sc = innermost_scope(scopes, ln)
                    name = sc[3] if sc else "<module>"
                    seen.setdefault(f"{rel}::{name}", {"file": rel, "qualname": name,
                                                       "line": sc[0] if sc else lo})
        return [{"id": k, **v} for k, v in seen.items()]


def _by_id(summary: dict | None) -> dict[str, dict]:
    return {f["id"]: f for f in summary["functions"]} if summary else {}


def compare_metric(a_unit: dict, b_unit: dict, metric: str, a_functions: dict,
                   b_functions: dict, changes: ChangeMap) -> dict:
    stat_key, summary_key = METRICS[metric]
    a, b = a_unit[stat_key], b_unit[stat_key]
    delta = b["median"] - a["median"]
    band = noise_band(a, b)
    out: dict = {
        "metric": metric,
        "base": a["median"],
        "head": b["median"],
        "delta": delta,
        "band": band,
        "significant": abs(delta) > band,
        "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
    }
    a_sum, b_sum = a_unit.get(summary_key), b_unit.get(summary_key)
    note = None
    if not (a_sum or b_sum):
        out["functions"] = []
        out["verdict"] = {"kind": "unattributed", "reason": "no attribution data (the workload "
                          "is too short-lived to snapshot at its peak)"}
        return out
    if not (a_sum and b_sum):
        # One side's peak was too short-lived to snapshot (e.g. a tiny workload whose peak is
        # inside a single C call). Treat it as empty: deltas become upper bounds.
        missing = "base" if not a_sum else "head"
        note = (f"no peak snapshot for the {missing} side (its peak was too short-lived); "
                "function deltas are upper bounds")
        a_sum, b_sum = a_sum or EMPTY_SUMMARY, b_sum or EMPTY_SUMMARY

    fa, fb = _by_id(a_sum), _by_id(b_sum)
    sign = 1 if delta >= 0 else -1
    rows = []
    for fid in set(fa) | set(fb):
        a_info, b_info = a_functions.get(fid), b_functions.get(fid)
        info = b_info or a_info or {"id": fid, "file": fid.split("::")[0],
                                    "qualname": fid.split("::")[-1], "line": 1,
                                    "start": 1, "end": 1}
        x, y = fa.get(fid, {}), fb.get(fid, {})
        cum = y.get("cumulative", 0) - x.get("cumulative", 0)
        slf = y.get("self", 0) - x.get("self", 0)
        if abs(cum) < NOISE_BYTES and abs(slf) < NOISE_BYTES:
            continue
        hunks = changes.hunks_for(b_info, "new")
        for h in changes.hunks_for(a_info, "old"):
            if h not in hunks:
                hunks.append(h)
        rows.append({
            "id": fid, "file": info["file"], "qualname": info["qualname"],
            "line": info["line"], "cum_delta": cum, "self_delta": slf,
            "base_cum": x.get("cumulative", 0), "head_cum": y.get("cumulative", 0),
            "changed": bool(hunks), "hunks": [h.to_json() for h in hunks],
        })
    rows.sort(key=lambda r: (-sign * r["cum_delta"], -sign * r["self_delta"]))
    out["functions"] = rows[:MAX_FUNCS]
    out["coverage"] = {"base": a_sum.get("coverage"), "head": b_sum.get("coverage")}
    # Where the memory lives: at head for growth, at base for memory that went away.
    where = b_sum if sign > 0 else a_sum
    out["verdict"] = _verdict(rows, delta, sign, where) if out["significant"] else {"kind": "none"}
    covs = [c for c in (a_sum.get("coverage"), b_sum.get("coverage")) if c is not None]
    low = min(covs, default=1) if metric == "peak" else 1
    if low < MIN_COVERAGE and not note:
        note = (f"the peak snapshot holds only {low:.0%} of the peak (the true peak is a "
                "temporary inside a single C call); attribution uses the largest observable state")
    if note and out["significant"]:
        out["verdict"]["note"] = note
    return out


def _verdict(rows: list[dict], delta: int, sign: int, where: dict) -> dict:
    floor = max(FUNC_SHARE * abs(delta), MIN_BAND)
    candidates = [r for r in rows if sign * r["cum_delta"] >= floor]
    if not candidates:
        return _with_note({"kind": "unattributed",
                           "reason": "no project function explains the change (native memory "
                                     "or allocations outside the project?)"}, where)
    changed = [r for r in candidates if r["changed"]]
    if changed:
        best = max(sign * r["cum_delta"] for r in changed)
        tied = [r for r in changed if sign * r["cum_delta"] >= TIE_SHARE * best]
        # Among equally-large changed functions prefer the one allocating the memory itself
        # (deepest in the stack), not the callers that merely call it.
        pick = max(tied, key=lambda r: (sign * r["self_delta"], -r["head_cum"]))
        kind = "direct"
    else:
        pick = max(candidates, key=lambda r: sign * r["self_delta"])
        kind = "indirect"
    big = [ln for ln in where["lines"] if ln["bytes"] >= 0.01 * abs(pick["cum_delta"])]
    lines = [ln for ln in big if ln["file"] == pick["file"]][:5]
    # When the blamed function only *keeps* memory that is allocated elsewhere (e.g. a new
    # cache around an unchanged loader), point at the biggest allocation sites too.
    allocated_at = [] if abs(pick["self_delta"]) >= 0.5 * abs(pick["cum_delta"]) else big[:3]
    verdict = {"kind": kind, "function": pick["id"], "file": pick["file"],
               "qualname": pick["qualname"], "line": pick["line"],
               "cum_delta": pick["cum_delta"], "self_delta": pick["self_delta"],
               "hunks": pick["hunks"], "hot_lines": lines, "allocated_at": allocated_at}
    return _with_note(verdict, where) if kind == "indirect" else verdict


def _with_note(verdict: dict, where: dict) -> dict:
    """Explain weak attribution caused by stacks deeper than the traceback limit."""
    share = where.get("truncated", 0) / max(where.get("total", 0), 1)
    if share > TRUNCATED_NOTE:
        verdict["note"] = (f"{share:.0%} of this memory had no project frame within the traceback "
                           "depth (deep library stacks); a larger --nframe may attribute it")
    return verdict


def compare(repo: Path, base: dict, head: dict, base_sha: str, head_sha: str,
            metrics: tuple[str, ...] = ("peak", "retained")) -> dict:
    """Compare two `measure()` results. `head_sha` may be git.WORKTREE."""
    changes = ChangeMap(repo, base_sha, head_sha, git.diff_hunks(repo, base_sha, head_sha))
    units = []
    for name, b_unit in head["units"].items():
        a_unit = base["units"].get(name)
        if a_unit is None:
            units.append({"name": name, "status": "new"})
            continue
        outcome = {"base": a_unit["outcome"], "head": b_unit["outcome"]}
        if outcome["base"] != outcome["head"]:
            # A test that now fails early "uses less memory"; comparing would be misleading.
            units.append({"name": name, "status": "outcome_changed", "outcome": outcome})
            continue
        units.append({
            "name": name,
            "status": "compared",
            "outcome": outcome,
            "metrics": [compare_metric(a_unit, b_unit, m, base["functions"], head["functions"],
                                       changes) for m in metrics],
        })
    for name in base["units"]:
        if name not in head["units"]:
            units.append({"name": name, "status": "removed"})
    return {
        "units": units,
        "changed_functions": changes.changed_functions(),
        "findings": findings(units),
    }


def findings(units: list[dict]) -> list[dict]:
    """Significant changes, largest first."""
    out = []
    for u in units:
        for m in u.get("metrics", []):
            if m["significant"]:
                out.append({"unit": u["name"], "metric": m["metric"], "delta": m["delta"],
                            "base": m["base"], "head": m["head"], "band": m["band"],
                            "verdict": m["verdict"], "functions": m["functions"][:8]})
    out.sort(key=lambda f: -abs(f["delta"]))
    return out
