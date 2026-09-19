"""Compare two measurements and blame the change on a function and a diff hunk."""

from __future__ import annotations

from pathlib import Path

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


def noise_band(a: dict, b: dict) -> int:
    spread = max(a["max"] - a["min"], b["max"] - b["min"])
    return int(max(SPREAD_FACTOR * spread, REL_BAND * max(a["median"], b["median"]), MIN_BAND))


class ChangeMap:
    """Which functions (at the new revision) a set of diff hunks touches."""

    def __init__(self, repo: Path, head: str, hunks: list[git.Hunk]):
        self.hunks = hunks
        self.by_file: dict[str, list[git.Hunk]] = {}
        for h in hunks:
            self.by_file.setdefault(h.file, []).append(h)
        self._repo, self._head = repo, head
        self._scopes: dict[str, list] = {}

    def scopes(self, rel: str) -> list:
        if rel not in self._scopes:
            src = git.file_at(self._repo, self._head, rel)
            self._scopes[rel] = scopes_from_source(src) if src else []
        return self._scopes[rel]

    def hunks_for(self, info: dict) -> list[git.Hunk]:
        """Hunks intersecting a function's lines. '<module>' matches top-level edits."""
        out = []
        for h in self.by_file.get(info["file"], []):
            lo, hi = h.new_range
            if info["qualname"] == "<module>":
                scopes = self.scopes(info["file"])
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


def compare_metric(a_unit: dict, b_unit: dict, metric: str, b_functions: dict,
                   changes: ChangeMap) -> dict:
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
    if not (a_sum and b_sum):
        out["functions"], out["verdict"] = [], {"kind": "unattributed"}
        return out

    fa, fb = _by_id(a_sum), _by_id(b_sum)
    sign = 1 if delta >= 0 else -1
    rows = []
    for fid in set(fa) | set(fb):
        info = b_functions.get(fid) or {"id": fid, "file": fid.split("::")[0],
                                        "qualname": fid.split("::")[-1], "line": 1,
                                        "start": 1, "end": 1}
        x, y = fa.get(fid, {}), fb.get(fid, {})
        cum = y.get("cumulative", 0) - x.get("cumulative", 0)
        slf = y.get("self", 0) - x.get("self", 0)
        if abs(cum) < NOISE_BYTES and abs(slf) < NOISE_BYTES:
            continue
        hunks = changes.hunks_for(info) if fid in fb else []
        rows.append({
            "id": fid, "file": info["file"], "qualname": info["qualname"],
            "line": info["line"], "cum_delta": cum, "self_delta": slf,
            "base_cum": x.get("cumulative", 0), "head_cum": y.get("cumulative", 0),
            "changed": bool(hunks), "hunks": [h.to_json() for h in hunks],
        })
    rows.sort(key=lambda r: (-sign * r["cum_delta"], -sign * r["self_delta"]))
    out["functions"] = rows[:MAX_FUNCS]
    out["coverage"] = {"base": a_sum.get("coverage"), "head": b_sum.get("coverage")}
    out["verdict"] = _verdict(rows, delta, sign, b_sum) if out["significant"] else {"kind": "none"}
    return out


def _verdict(rows: list[dict], delta: int, sign: int, b_sum: dict) -> dict:
    floor = max(FUNC_SHARE * abs(delta), MIN_BAND)
    candidates = [r for r in rows if sign * r["cum_delta"] >= floor]
    if not candidates:
        return {"kind": "unattributed",
                "reason": "no project function explains the change (native memory or "
                          "allocations outside the project?)"}
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
    big = [ln for ln in b_sum["lines"] if ln["bytes"] >= 0.01 * abs(pick["cum_delta"])]
    lines = [ln for ln in big if ln["file"] == pick["file"]][:5]
    # When the blamed function only *keeps* memory that is allocated elsewhere (e.g. a new
    # cache around an unchanged loader), point at the biggest allocation sites too.
    allocated_at = [] if abs(pick["self_delta"]) >= 0.5 * abs(pick["cum_delta"]) else big[:3]
    return {"kind": kind, "function": pick["id"], "file": pick["file"],
            "qualname": pick["qualname"], "line": pick["line"], "cum_delta": pick["cum_delta"],
            "self_delta": pick["self_delta"], "hunks": pick["hunks"], "hot_lines": lines,
            "allocated_at": allocated_at}


def compare(repo: Path, base: dict, head: dict, base_sha: str, head_sha: str,
            metrics: tuple[str, ...] = ("peak", "retained")) -> dict:
    """Compare two `measure()` results. `head_sha` may be git.WORKTREE."""
    changes = ChangeMap(repo, head_sha, git.diff_hunks(repo, base_sha, head_sha))
    units = []
    for name, b_unit in head["units"].items():
        a_unit = base["units"].get(name)
        if a_unit is None:
            units.append({"name": name, "status": "new"})
            continue
        units.append({
            "name": name,
            "status": "compared",
            "outcome": {"base": a_unit["outcome"], "head": b_unit["outcome"]},
            "metrics": [compare_metric(a_unit, b_unit, m, head["functions"], changes)
                        for m in metrics],
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
