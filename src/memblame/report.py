"""Human-readable terminal output for the dicts produced by `memblame.api`."""

from __future__ import annotations


def mb(n: int | float, signed: bool = False) -> str:
    v = n / 1e6
    if abs(v) < 0.1 and n != 0:
        text = f"{n / 1e3:{'+' if signed else ''}.1f} KB"
    else:
        text = f"{v:{'+' if signed else ''}.1f} MB"
    return text


def _loc(v: dict) -> str:
    name = "module level" if v["qualname"] == "<module>" else f"{v['qualname']}()"
    return f"{v['file']}:{v['line']} {name}"


def _verdict_lines(v: dict, indent: str) -> list[str]:
    kind = v.get("kind")
    if kind in ("direct", "indirect"):
        lines = [f"{indent}{kind}: {_loc(v)}  {mb(v['cum_delta'], True)}"]
        for h in v.get("hunks", [])[:3]:
            lines.append(f"{indent}  changed in {h['file']} {h['header']}")
        if kind == "indirect":
            lines.append(f"{indent}  (this function did not change; its memory grew because of "
                         "a change elsewhere - see changed functions)")
        for hl in v.get("hot_lines", [])[:3]:
            lines.append(f"{indent}  hot line {hl['file']}:{hl['line']}  {mb(hl['bytes'])}")
        for hl in v.get("allocated_at", [])[:3]:
            lines.append(f"{indent}  memory allocated at {hl['file']}:{hl['line']}  "
                         f"{mb(hl['bytes'])}")
        if v.get("note"):
            lines.append(f"{indent}  note: {v['note']}")
        return lines
    if kind == "unattributed":
        out = [f"{indent}unattributed: {v.get('reason', 'no attribution data')}"]
        return out + ([f"{indent}  note: {v['note']}"] if v.get("note") else [])
    return []


def _warnings(d: dict) -> list[str]:
    return [f"warning: {w}" for w in d.get("warnings", [])]


def _status_line(d: dict) -> str | None:
    status = d.get("measurement_status", "complete")
    if status == "complete":
        return None
    return ("ERROR: measurement unavailable; no memory-regression conclusion"
            if status == "error"
            else "INCOMPLETE: one or more workloads did not pass; no regression conclusion")


def format_run(d: dict) -> str:
    c, r = d["commit"], d["result"]
    if not r["valid"]:
        return "\n".join([f"{c['short']}  {c['subject']}   (measurement unavailable)",
                          *_warnings(d)])
    out = [f"{c['short']}  {c['subject']}   (python {r['python']}, {r['runs']} runs)"]
    if line := _status_line(d):
        out.append(line)
    out.append(f"  {'unit':44} {'peak':>10} {'spread':>9} {'retained':>10}  outcome")
    for name, u in r["units"].items():
        spread = u["peak"]["max"] - u["peak"]["min"]
        out.append(f"  {name[-44:]:44} {mb(u['peak']['median']):>10} {mb(spread):>9} "
                   f"{mb(u['end']['median']):>10}  {u['outcome']}")
        summary = u.get("at_peak") or u.get("retained")
        if summary:
            for f in sorted(summary["functions"], key=lambda f: -f["self"])[:3]:
                if f["self"] > 0:
                    out.append(f"      {mb(f['self']):>10}  {f['id']}")
    return "\n".join(out + _warnings(d))


def format_diff(d: dict) -> str:
    a, b = d["base"], d["head"]
    out = [f"{a['short']} -> {b['short']}   {b['subject']}"]
    if not d["valid"]:
        return "\n".join(out + _warnings(d))
    if line := _status_line(d):
        out.append(line)
    out += [f"  note: {n}" for n in d.get("notes", [])]
    if not d["units"]:
        out.append("  no units were measured (see warnings)")
    for u in d["units"]:
        if u["status"] == "outcome_changed":
            o = u["outcome"]
            out.append(f"  {u['name']}: outcome changed {o['base']} -> {o['head']}; "
                       "memory not compared")
            continue
        if u["status"] != "compared":
            out.append(f"  {u['name']}: {u['status']} (only measured on one side)")
            continue
        out.append(f"  {u['name']}")
        for m in u["metrics"]:
            tag = ("REGRESSION" if m["direction"] == "up" else "improvement") if m[
                "significant"] else "no significant change"
            out.append(f"    {m['metric']:9} {mb(m['base']):>9} -> {mb(m['head']):>9}  "
                       f"{mb(m['delta'], True):>10}  (noise ±{mb(m['band'])})  {tag}")
            if m["significant"]:
                out += _verdict_lines(m["verdict"], "      ")
                for f in m["functions"][:5]:
                    flag = "  [changed]" if f["changed"] else ""
                    out.append(f"        {mb(f['cum_delta'], True):>10} cum "
                               f"{mb(f['self_delta'], True):>10} self  {f['id']}{flag}")
    changed = d.get("changed_functions", [])
    if changed:
        out.append("  changed functions: " + ", ".join(c["id"] for c in changed[:8]))
    return "\n".join(out + _warnings(d))


def format_range(d: dict) -> str:
    points = d["points"]
    units: list[str] = []
    for p in points:
        for name in p.get("units", {}):
            if name not in units:
                units.append(name)
    flagged = {(f["commit"], f["unit"], f["metric"]) for f in d["findings"]}
    interesting = [u for u in units if any(k[1] == u for k in flagged)] or units[:1]
    out = []
    for unit in interesting[:3]:
        mode = (f"; measured {d.get('measured', len(points))} of {len(points)} commits"
                if d.get("mode") == "adaptive" else "")
        out.append(f"{unit}  (median of runs; ▲ = significant change{mode})")
        out.append(f"  {'commit':9} {'peak':>9} {'Δpeak':>10}   {'retained':>9} {'Δret':>10}  "
                   "author / subject")
        prev = None
        skipped: list[dict] = []
        for p in points:
            u = p.get("units", {}).get(unit)
            c = p["commit"]
            if not p.get("measured", True):
                skipped.append(c)
                continue
            if skipped:
                out.append(f"  {'':9} {'·':>9}   {len(skipped)} commit(s) not measured: no "
                           "significant change across them")
                skipped = []
            if not u:
                out.append(f"  {c['short']:9} {'-':>9}")
                continue
            pk, en = u["peak"]["median"], u["end"]["median"]
            dp = mb(pk - prev[0], True) if prev else ""
            de = mb(en - prev[1], True) if prev else ""
            mp = "▲" if (c["sha"], unit, "peak") in flagged else " "
            me = "▲" if (c["sha"], unit, "retained") in flagged else " "
            bad = "" if p["valid"] else "  INVALID ENV"
            out.append(f"  {c['short']:9} {mb(pk):>9} {dp:>9}{mp}   {mb(en):>9} {de:>9}{me}  "
                       f"{c['author'][:14]:14} {c['subject'][:40]}{bad}")
            prev = (pk, en)
        out.append("")
    if len(units) > len(interesting[:3]):
        out.append(f"({len(units) - len(interesting[:3])} other units without findings)")
    if d.get("measurement_status", "complete") == "complete":
        out += _findings(d["findings"], points)
    elif d["findings"]:
        out.append(f"INCOMPLETE: {_incomplete_note(d)}")
        out += _findings(d["findings"], points)
    else:
        out.append("Measurement incomplete; no memory-regression conclusion.")
    return "\n".join(out + _warnings(d))


def _incomplete_note(d: dict) -> str:
    n = d.get("incomplete_commits", 0)
    which = f"{n} commit(s)" if n else "some commits"
    return (f"{which} could not be measured or did not pass. The findings below are between "
            "commits that were measured; more may hide in the gaps, so this is not an all-clear.")


def _findings(findings: list[dict], points: list[dict]) -> list[str]:
    if not findings:
        return ["No significant memory changes."]
    short = {p["commit"]["sha"]: p["commit"] for p in points}
    out = ["Findings:"]
    for f in findings:
        c = short.get(f["commit"], {"short": f["commit"][:7], "subject": ""})
        out.append(f"  {c['short']}  {f['metric']} {mb(f['delta'], True)}  in {f['unit']}  "
                   f"\"{c['subject'][:50]}\"")
        out += _verdict_lines(f["verdict"], "      ")
    return out


def format_bisect(d: dict) -> str:
    if d["status"] != "found":
        return "\n".join([f"bisect: {d['message']}"] + _warnings(d))
    c = d["culprit"]
    label = "First verified crossing" if d.get("verified") else "Threshold crossing"
    out = [f"{label}: {c['short']}  {c['author']}  \"{c['subject']}\"",
           f"  {d['unit']} {d['metric']} threshold {mb(d['threshold'])}; "
           f"{d['steps']} bisect steps for {d['candidates']} candidate commits"]
    if line := _status_line(d):
        out.append(line)
    for t in d["measurements"]:
        mark = "skip" if t.get("skipped") else "bad " if t["bad"] else "good"
        value = "-" if t["value"] is None else mb(t["value"])
        out.append(f"    {mark} {t['commit']['short']}  {value:>9}  "
                   f"{t['commit']['subject'][:50]}")
    for f in d["findings"]:
        out.append(f"  {f['metric']} {mb(f['delta'], True)} vs parent in {f['unit']}")
        out += _verdict_lines(f["verdict"], "    ")
    return "\n".join(out + _warnings(d))
