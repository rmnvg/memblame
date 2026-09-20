"""Portable Markdown and self-contained HTML reports for CI artifacts."""

from __future__ import annotations

import html
import json
import secrets
from pathlib import Path

from .report import mb


def render(data: dict, kind: str) -> str:
    """Render a schema-1 result as Markdown or a standalone HTML document."""
    if kind in ("md", "markdown"):
        return markdown(data)
    if kind == "html":
        return html_report(data)
    raise ValueError(f"unknown report format {kind!r}")


def write(text: str, path: str | Path) -> Path:
    """Write a report, creating its parent directories, and return the resolved path."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")
    return target.resolve()


def _md(text: object) -> str:
    return (str(text).replace("\\", "\\\\").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;").replace("`", "\\`")
            .replace("|", "\\|"))


def _code(text: object) -> str:
    return f"`{_md(text)}`"


def _commit(c: dict | None) -> str:
    if not c:
        return "unknown commit"
    name = "WORKTREE" if c.get("sha") == "WORKTREE" else c.get("short", "")
    return f"{_code(name)} {_md(c.get('subject', ''))}".strip()


def _metadata_md(data: dict) -> list[str]:
    return [
        f"- **Workload:** {_code(data.get('workload', ''))}",
        f"- **Repository:** {_code(data.get('repo', ''))}",
        f"- **Python:** {_code(data.get('python', ''))}",
    ]


def _warnings_md(data: dict) -> list[str]:
    warnings = data.get("warnings", [])
    notes = data.get("notes", [])
    if not warnings and not notes:
        return []
    out = ["", "## Notes"]
    out += [f"- ⚠️ {_md(w)}" for w in warnings]
    out += [f"- ℹ️ {_md(n)}" for n in notes]
    return out


def _incomplete_text(data: dict) -> str | None:
    """One sentence for both formats; None when the measurement is complete."""
    status = data.get("measurement_status", "complete")
    if status == "complete":
        return None
    if data.get("kind") == "range" and data.get("findings"):
        n = data.get("incomplete_commits", 0)
        which = f"{n} commit(s)" if n else "some commits"
        return (f"Measurement incomplete: {which} could not be measured or did not pass. The "
                "findings below are between commits that were measured; more may hide in the "
                "gaps, so this is not an all-clear.")
    detail = ("Measurement unavailable" if status == "error"
              else "Measurement incomplete because one or more workloads did not pass")
    return f"{detail}; no memory-regression conclusion can be made."


def _incomplete_md(data: dict) -> list[str]:
    text = _incomplete_text(data)
    return [] if text is None else ["", f"**{text}**"]


def _verdict_md(verdict: dict) -> list[str]:
    kind = verdict.get("kind", "none")
    if kind == "none":
        return []
    if kind == "unattributed":
        out = [f"- **Attribution:** unavailable — {_md(verdict.get('reason', 'unknown'))}"]
    else:
        qualname = ("module level" if verdict.get("qualname") == "<module>"
                    else f"{verdict.get('qualname', 'unknown')}()")
        where = f"{verdict.get('file', '?')}:{verdict.get('line', '?')}"
        out = [
            f"- **Attribution:** {kind} in {_code(where)} {_md(qualname)}, "
            f"{mb(verdict.get('cum_delta', 0), True)} including callees",
        ]
        for hunk in verdict.get("hunks", [])[:5]:
            out.append(f"  - Changed hunk: {_code(hunk.get('file', ''))} "
                       f"{_code(hunk.get('header', ''))}")
        for line in verdict.get("hot_lines", [])[:5]:
            location = f"{line['file']}:{line['line']}"
            out.append(f"  - Hot allocation: {_code(location)} ({mb(line['bytes'])})")
        for line in verdict.get("allocated_at", [])[:5]:
            location = f"{line['file']}:{line['line']}"
            out.append(f"  - Retained memory allocated at {_code(location)} "
                       f"({mb(line['bytes'])})")
    if verdict.get("note"):
        out.append(f"  - {_md(verdict['note'])}")
    return out


def _finding_md(finding: dict, commit: dict | None = None) -> list[str]:
    direction = "Regression" if finding.get("delta", 0) > 0 else "Improvement"
    out = ["", f"### {direction}: {_md(finding.get('metric', 'memory'))} "
           f"{mb(finding.get('delta', 0), True)}"]
    if commit:
        out.append(f"- **Commit:** {_commit(commit)}")
    out += [
        f"- **Unit:** {_code(finding.get('unit', ''))}",
        f"- **Change:** {mb(finding.get('base', 0))} → {mb(finding.get('head', 0))} "
        f"(noise ±{mb(finding.get('band', 0))})",
        *_verdict_md(finding.get("verdict", {})),
    ]
    functions = finding.get("functions", [])[:8]
    if functions:
        out += ["", "| Function | Δ incl. callees | Δ own | Changed |",
                "|---|---:|---:|:---:|"]
        for fn in functions:
            out.append(f"| {_code(fn.get('id', ''))} | {mb(fn.get('cum_delta', 0), True)} | "
                       f"{mb(fn.get('self_delta', 0), True)} | "
                       f"{'yes' if fn.get('changed') else ''} |")
    return out


def markdown(data: dict) -> str:
    """A GitHub-flavoured Markdown summary suitable for GITHUB_STEP_SUMMARY."""
    kind = data.get("kind")
    title = {
        "run": "Memory measurement",
        "diff": "Memory comparison",
        "range": "Memory history",
        "bisect": "Memory regression bisect",
    }.get(kind, "MemBlame report")
    out = [f"# MemBlame: {title}", "", *_metadata_md(data)]
    if kind == "run":
        out += _run_md(data)
    elif kind == "diff":
        out += _diff_md(data)
    elif kind == "range":
        out += _range_md(data)
    elif kind == "bisect":
        out += _bisect_md(data)
    else:
        out += ["", "```json", json.dumps(data, indent=2), "```"]
    out += _warnings_md(data)
    out += ["", "---", "Generated by [MemBlame](https://github.com/rmnvg/memblame)."]
    return "\n".join(out).rstrip() + "\n"


def _run_md(data: dict) -> list[str]:
    result = data.get("result", {})
    out = ["", f"## {_commit(data.get('commit'))}", *_incomplete_md(data)]
    if not result.get("valid", False):
        return out + ["", "**Measurement unavailable.**"]
    out += ["", "| Unit | Peak | Spread | Retained | Outcome |",
            "|---|---:|---:|---:|---|"]
    for name, unit in result.get("units", {}).items():
        spread = unit["peak"]["max"] - unit["peak"]["min"]
        out.append(f"| {_code(name)} | {mb(unit['peak']['median'])} | {mb(spread)} | "
                   f"{mb(unit['end']['median'])} | {_md(unit['outcome'])} |")
    return out


def _diff_md(data: dict) -> list[str]:
    out = ["", f"## {_commit(data.get('base'))} → {_commit(data.get('head'))}"]
    findings = data.get("findings", [])
    if data.get("measurement_status", "complete") != "complete":
        return out + _incomplete_md(data)
    if data.get("valid") is False:
        return out + ["", "**Comparison unavailable.**"]
    if any(f.get("delta", 0) > 0 for f in findings):
        out += ["", "**Result: memory regression detected.**"]
    elif findings:
        out += ["", "**Result: memory improvement detected.**"]
    else:
        out += ["", "**Result: no significant memory change.**"]
    out += ["", "| Unit | Metric | Before | After | Δ | Noise | Result |",
            "|---|---|---:|---:|---:|---:|---|"]
    for unit in data.get("units", []):
        if unit.get("status") != "compared":
            out.append(f"| {_code(unit.get('name', ''))} | — | — | — | — | — | "
                       f"{_md(unit.get('status', 'not compared'))} |")
            continue
        for metric in unit.get("metrics", []):
            result = ("regression" if metric.get("delta", 0) > 0 else "improvement") \
                if metric.get("significant") else "within noise"
            out.append(f"| {_code(unit.get('name', ''))} | {_md(metric.get('metric', ''))} | "
                       f"{mb(metric.get('base', 0))} | {mb(metric.get('head', 0))} | "
                       f"{mb(metric.get('delta', 0), True)} | "
                       f"±{mb(metric.get('band', 0))} | {result} |")
    for finding in findings:
        out += _finding_md(finding)
    return out


def _range_md(data: dict) -> list[str]:
    points = data.get("points", [])
    findings = data.get("findings", [])
    commits = {p.get("commit", {}).get("sha"): p.get("commit") for p in points}
    out = ["", f"**Measured {data.get('measured', len(points))} of {len(points)} commits "
           f"in {data.get('mode', 'unknown')} mode.**", *_incomplete_md(data)]
    if data.get("measurement_status", "complete") != "complete" and not findings:
        out += ["", "## Findings", "", "No conclusion because the measurement is incomplete."]
    elif findings:
        out += ["", f"## Findings ({len(findings)})"]
        for finding in findings:
            out += _finding_md(finding, commits.get(finding.get("commit")))
    else:
        out += ["", "## Findings", "", "No significant memory changes."]
    out += ["", "## Timeline", "", "| Commit | Unit | Peak | Retained | Outcome |",
            "|---|---|---:|---:|---|"]
    for point in points:
        commit = point.get("commit", {})
        if not point.get("measured", True):
            out.append(f"| {_commit(commit)} | — | — | — | not measured |")
            continue
        if not point.get("units"):
            out.append(f"| {_commit(commit)} | — | — | — | unavailable |")
        for name, unit in point.get("units", {}).items():
            out.append(f"| {_commit(commit)} | {_code(name)} | {mb(unit['peak']['median'])} | "
                       f"{mb(unit['end']['median'])} | {_md(unit['outcome'])} |")
    return out


def _bisect_md(data: dict) -> list[str]:
    if data.get("status") != "found":
        return ["", f"**{_md(data.get('message', 'Bisect did not find a regression.'))}**"]
    out = [
        "",
        "## " + ("First verified crossing" if data.get("verified") else
                  "Threshold crossing (monotonicity assumed)"),
        "",
        f"**{_commit(data.get('culprit'))}**",
        "",
        f"{_code(data.get('unit', ''))} {data.get('metric', '')} crossed "
        f"{mb(data.get('threshold', 0))} after {data.get('steps', 0)} bisect steps.",
        *_incomplete_md(data),
    ]
    for finding in data.get("findings", []):
        out += _finding_md(finding, data.get("culprit"))
    out += ["", "## Measurements", "", "| State | Commit | Value |", "|---|---|---:|"]
    for item in data.get("measurements", []):
        state = "skipped" if item.get("skipped") else "bad" if item.get("bad") else "good"
        value = "—" if item.get("value") is None else mb(item["value"])
        out.append(f"| {state} | {_commit(item.get('commit'))} | {value} |")
    return out


def _h(text: object) -> str:
    return html.escape(str(text), quote=True)


def _status_html(data: dict) -> str:
    text = _incomplete_text(data)
    return "" if text is None else f'<p class="status bad">{_h(text)}</p>'


def _commit_html(c: dict | None) -> str:
    if not c:
        return '<span class="muted">unknown commit</span>'
    name = "WORKTREE" if c.get("sha") == "WORKTREE" else c.get("short", "")
    return (f'<span class="sha">{_h(name)}</span> {_h(c.get("subject", ""))} '
            f'<span class="muted">{_h(c.get("author", ""))}</span>')


def _verdict_html(verdict: dict) -> str:
    kind = verdict.get("kind", "none")
    if kind == "none":
        return ""
    if kind == "unattributed":
        body = f'<p class="muted">Not attributed: {_h(verdict.get("reason", "unknown"))}</p>'
    else:
        qualname = ("module level" if verdict.get("qualname") == "<module>"
                    else f'{verdict.get("qualname", "unknown")}()')
        where = f'{verdict.get("file", "?")}:{verdict.get("line", "?")}'
        body = (f'<p><span class="pill {kind}">{_h(kind)}</span> '
                f'<code>{_h(where)}</code> {_h(qualname)} '
                f'<strong>{_h(mb(verdict.get("cum_delta", 0), True))}</strong></p>')
        hunks = verdict.get("hunks", [])[:5]
        hot = verdict.get("hot_lines", [])[:5]
        allocated = verdict.get("allocated_at", [])[:5]
        if hunks or hot or allocated:
            changed = "".join(
                f'<li><code>{_h(h.get("file", ""))}</code> '
                f'<code>{_h(h.get("header", ""))}</code></li>' for h in hunks
            ) or '<li class="muted">No matching changed hunk.</li>'
            evidence = "".join(
                f'<li>Hot: <code>{_h(line["file"])}:{line["line"]}</code> '
                f'{_h(mb(line["bytes"]))}</li>' for line in hot
            ) + "".join(
                f'<li>Allocated at: <code>{_h(line["file"])}:{line["line"]}</code> '
                f'{_h(mb(line["bytes"]))}</li>' for line in allocated
            )
            body += (f'<div class="evidence"><div><h4>Changed code</h4><ul>{changed}</ul></div>'
                     f'<div><h4>Allocation evidence</h4><ul>{evidence}</ul></div></div>')
    if verdict.get("note"):
        body += f'<p class="muted">{_h(verdict["note"])}</p>'
    return body


def _finding_html(finding: dict, commit: dict | None = None) -> str:
    up = finding.get("delta", 0) > 0
    direction = "Regression" if up else "Improvement"
    functions = finding.get("functions", [])[:8]
    rows = "".join(
        f'<tr><td><code>{_h(fn.get("id", ""))}</code></td>'
        f'<td class="num">{_h(mb(fn.get("cum_delta", 0), True))}</td>'
        f'<td class="num">{_h(mb(fn.get("self_delta", 0), True))}</td>'
        f'<td>{"changed" if fn.get("changed") else ""}</td></tr>' for fn in functions
    )
    table = (f'<table><thead><tr><th>Function</th><th>Δ incl. callees</th>'
             f'<th>Δ own</th><th></th></tr></thead><tbody>{rows}</tbody></table>') if rows else ""
    commit_line = f'<p class="commit">{_commit_html(commit)}</p>' if commit else ""
    return (
        f'<article class="card {"up" if up else "down"}">{commit_line}'
        f'<h3><span class="pill {"regression" if up else "improvement"}">{direction}</span> '
        f'{_h(finding.get("metric", "memory"))} '
        f'{_h(mb(finding.get("delta", 0), True))}</h3>'
        f'<p><code>{_h(finding.get("unit", ""))}</code> · '
        f'{_h(mb(finding.get("base", 0)))} → {_h(mb(finding.get("head", 0)))} · '
        f'noise ±{_h(mb(finding.get("band", 0)))}</p>'
        f'{_verdict_html(finding.get("verdict", {}))}{table}</article>'
    )


def _table(headers: list[str], rows: list[list[str]], classes: list[str] | None = None) -> str:
    classes = classes or [""] * len(headers)
    head = "".join(f'<th class="{_h(classes[i])}">{_h(value)}</th>'
                   for i, value in enumerate(headers))
    body = "".join("<tr>" + "".join(
        f'<td class="{_h(classes[i])}">{value}</td>' for i, value in enumerate(row)
    ) + "</tr>" for row in rows)
    return (f"<div class=scroll><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>")


def _units(points: list[dict]) -> list[str]:
    names: list[str] = []
    for point in points:
        for name in point.get("units", {}):
            if name not in names:
                names.append(name)
    return names


def _range_chart(points: list[dict], unit: str, findings: list[dict]) -> str:
    width, height, left, right, top, bottom = 900, 280, 68, 24, 20, 42
    peak = [p.get("units", {}).get(unit, {}).get("peak", {}).get("median") for p in points]
    retained = [p.get("units", {}).get(unit, {}).get("end", {}).get("median") for p in points]
    values = [value for value in peak + retained if value is not None]
    # y() divides by this, and an all-zero series is still a series (max() of it is 0).
    maximum = max(max(values, default=0), 1) * 1.08
    span_x, span_y = width - left - right, height - top - bottom

    def x(index: int) -> float:
        return left + (span_x / 2 if len(points) < 2 else index * span_x / (len(points) - 1))

    def y(value: int | float) -> float:
        return top + span_y * (1 - value / maximum)

    def path(values_: list[int | None]) -> str:
        started = False
        commands = []
        for index, value in enumerate(values_):
            if value is None:
                continue
            commands.append(f'{"L" if started else "M"}{x(index):.1f},{y(value):.1f}')
            started = True
        return " ".join(commands)

    flagged_peak = {f.get("commit") for f in findings
                    if f.get("unit") == unit and f.get("metric") == "peak"}
    flagged_retained = {f.get("commit") for f in findings
                        if f.get("unit") == unit and f.get("metric") == "retained"}
    grid = []
    for tick in range(5):
        value = maximum * tick / 4
        yy = y(value)
        grid.append(f'<line class="grid" x1="{left}" x2="{width-right}" y1="{yy:.1f}" '
                    f'y2="{yy:.1f}"/><text class="axis" x="{left-8}" y="{yy+4:.1f}" '
                    f'text-anchor="end">{_h(mb(value))}</text>')
    every = max(1, (len(points) + 9) // 10)
    marks = []
    for index, point in enumerate(points):
        commit = point.get("commit", {})
        title = (f'{commit.get("short", "")} {commit.get("subject", "")} · peak '
                 f'{"—" if peak[index] is None else mb(peak[index])} · retained '
                 f'{"—" if retained[index] is None else mb(retained[index])}')
        for values_, name, flagged in ((peak, "peak", flagged_peak),
                                       (retained, "retained", flagged_retained)):
            value = values_[index]
            if value is not None:
                cls = f"dot {name}" + (" flagged" if commit.get("sha") in flagged else "")
                marks.append(f'<circle class="{cls}" cx="{x(index):.1f}" cy="{y(value):.1f}" '
                             f'r="{6 if commit.get("sha") in flagged else 3.5}"><title>'
                             f'{_h(title)}</title></circle>')
        if index % every == 0 or index == len(points) - 1:
            marks.append(f'<text class="axis" x="{x(index):.1f}" y="{height-12}" '
                         f'text-anchor="middle">{_h(commit.get("short", ""))}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Memory timeline for {_h(unit)}">{"".join(grid)}'
            f'<path class="line peak" d="{path(peak)}"/>'
            f'<path class="line retained" d="{path(retained)}"/>{"".join(marks)}</svg>'
            f'<p class="legend"><span class="key peak"></span> Peak '
            f'<span class="key retained"></span> Retained '
            f'<span class="key flagged"></span> Significant change</p>')


def _html_body(data: dict) -> str:
    kind = data.get("kind")
    if kind == "run":
        return _run_html(data)
    if kind == "diff":
        return _diff_html(data)
    if kind == "range":
        return _range_html(data)
    if kind == "bisect":
        return _bisect_html(data)
    return f'<pre>{_h(json.dumps(data, indent=2))}</pre>'


def _run_html(data: dict) -> str:
    result = data.get("result", {})
    if not result.get("valid", False):
        return f'<h1>Memory measurement</h1><p>{_commit_html(data.get("commit"))}</p>' \
               '<p class="status bad">Measurement unavailable.</p>'
    rows = []
    for name, unit in result.get("units", {}).items():
        spread = unit["peak"]["max"] - unit["peak"]["min"]
        rows.append([f'<code>{_h(name)}</code>', _h(mb(unit["peak"]["median"])),
                     _h(mb(spread)), _h(mb(unit["end"]["median"])),
                     _h(unit["outcome"])])
    status = _status_html(data)
    return (f'<h1>Memory measurement</h1><p>{_commit_html(data.get("commit"))}</p>{status}'
            + _table(["Unit", "Peak", "Spread", "Retained", "Outcome"], rows,
                     ["", "num", "num", "num", ""]))


def _diff_html(data: dict) -> str:
    findings = data.get("findings", [])
    if data.get("measurement_status", "complete") != "complete":
        status = _status_html(data)
        findings = []
    elif data.get("valid") is False:
        status = '<p class="status bad">Comparison unavailable.</p>'
    elif any(f.get("delta", 0) > 0 for f in findings):
        status = '<p class="status bad">Memory regression detected.</p>'
    elif findings:
        status = '<p class="status good">Memory improvement detected.</p>'
    else:
        status = '<p class="status">No significant memory change.</p>'
    rows = []
    for unit in data.get("units", []):
        if unit.get("status") != "compared":
            rows.append([f'<code>{_h(unit.get("name", ""))}</code>', "—", "—", "—", "—",
                         _h(unit.get("status", "not compared"))])
            continue
        for metric in unit.get("metrics", []):
            result = ("regression" if metric.get("delta", 0) > 0 else "improvement") \
                if metric.get("significant") else "within noise"
            rows.append([f'<code>{_h(unit.get("name", ""))}</code>',
                         _h(metric.get("metric", "")), _h(mb(metric.get("base", 0))),
                         _h(mb(metric.get("head", 0))),
                         _h(mb(metric.get("delta", 0), True)), result])
    cards = "".join(_finding_html(finding) for finding in findings)
    return (f'<h1>Memory comparison</h1><p>{_commit_html(data.get("base"))} → '
            f'{_commit_html(data.get("head"))}</p>{status}{cards}<h2>Measurements</h2>'
            + _table(["Unit", "Metric", "Before", "After", "Δ", "Result"], rows,
                     ["", "", "num", "num", "num", ""]))


def _range_html(data: dict) -> str:
    points = data.get("points", [])
    findings = data.get("findings", [])
    commits = {p.get("commit", {}).get("sha"): p.get("commit") for p in points}
    units = _units(points)
    select = ""
    if len(units) > 1:
        options = "".join(f'<option value="{_h(unit)}">{_h(unit)}</option>' for unit in units)
        select = f'<label>Unit <select id="unit-select">{options}</select></label>'
    charts = "".join(
        f'<section class="unit-chart" data-unit="{_h(unit)}" '
        f'{"" if index == 0 else "hidden"}>{_range_chart(points, unit, findings)}</section>'
        for index, unit in enumerate(units)
    )
    cards = "".join(_finding_html(f, commits.get(f.get("commit"))) for f in findings)
    if data.get("measurement_status", "complete") != "complete":
        cards = _status_html(data) + cards
    elif not cards:
        cards = '<p class="status good">No significant memory changes.</p>'
    rows = []
    for point in points:
        commit = point.get("commit", {})
        if not point.get("measured", True):
            rows.append([_commit_html(commit), "—", "—", "—", "not measured"])
            continue
        if not point.get("units"):
            rows.append([_commit_html(commit), "—", "—", "—", "unavailable"])
        for name, unit in point.get("units", {}).items():
            rows.append([_commit_html(commit), f'<code>{_h(name)}</code>',
                         _h(mb(unit["peak"]["median"])), _h(mb(unit["end"]["median"])),
                         _h(unit["outcome"])])
    return (f'<h1>Memory history</h1><p class="lede">Measured '
            f'{data.get("measured", len(points))} of {len(points)} commits in '
            f'{_h(data.get("mode", "unknown"))} mode.</p>{select}{charts}'
            f'<h2>Findings</h2>{cards}<h2>Timeline</h2>'
            + _table(["Commit", "Unit", "Peak", "Retained", "Outcome"], rows,
                     ["", "", "num", "num", ""]))


def _bisect_html(data: dict) -> str:
    if data.get("status") != "found":
        return (f'<h1>Memory regression bisect</h1><p class="status">'
                f'{_h(data.get("message", "No regression found."))}</p>')
    cards = "".join(_finding_html(f, data.get("culprit"))
                    for f in data.get("findings", []))
    rows = []
    for item in data.get("measurements", []):
        state = "skipped" if item.get("skipped") else "bad" if item.get("bad") else "good"
        value = "—" if item.get("value") is None else _h(mb(item["value"]))
        rows.append([state, _commit_html(item.get("commit")), value])
    title = "First verified crossing" if data.get("verified") else "Threshold crossing"
    return (f'<h1>{title}</h1>{_status_html(data)}<p class="culprit">'
            f'{_commit_html(data.get("culprit"))}</p><p class="lede">'
            f'<code>{_h(data.get("unit", ""))}</code> {_h(data.get("metric", ""))} crossed '
            f'{_h(mb(data.get("threshold", 0)))} after {data.get("steps", 0)} steps.</p>'
            f'{cards}<h2>Measurements</h2>'
            + _table(["State", "Commit", "Value"], rows, ["", "", "num"]))


_STYLE = """
:root { color-scheme: light dark; --bg:#0b1020; --panel:#141b2d; --text:#e8edf7;
  --muted:#9aa7bd; --line:#2b3650; --blue:#62a8ff; --purple:#c58bff;
  --red:#ff6b7a; --green:#4fd69c; --orange:#ffb454; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,
  BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:auto; padding:34px 24px 70px; }
h1 { font-size:2rem; margin:.2rem 0 .35rem; letter-spacing:-.03em; }
h2 { margin-top:2rem; font-size:1.2rem; } h3 { margin:.1rem 0 .5rem; }
h4 { margin:.2rem 0; color:var(--muted); text-transform:uppercase; font-size:.72rem;
  letter-spacing:.08em; }
code,.sha { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
.sha { color:var(--blue); font-weight:700; } .muted,.lede { color:var(--muted); }
.meta { display:flex; gap:18px; flex-wrap:wrap; color:var(--muted); margin-bottom:22px; }
.meta code { color:var(--text); }
.status,.culprit { padding:12px 15px; border:1px solid var(--line); border-radius:9px;
  background:var(--panel); font-weight:650; }
.status.bad { border-color:var(--red); } .status.good { border-color:var(--green); }
.warn { padding:10px 14px; margin:8px 0; border-left:4px solid var(--orange);
  background:color-mix(in srgb,var(--orange) 10%,var(--panel)); border-radius:5px; }
.card { background:var(--panel); border:1px solid var(--line); border-left:5px solid var(--red);
  border-radius:10px; padding:17px 19px; margin:14px 0; box-shadow:0 10px 35px #0002; }
.card.down { border-left-color:var(--green); } .commit { margin-top:0; }
.pill { display:inline-block; border-radius:999px; padding:2px 8px; font-size:.72rem;
  font-weight:750; letter-spacing:.04em; text-transform:uppercase; background:var(--line); }
.pill.regression { background:var(--red); color:#210208; }
.pill.improvement { background:var(--green); color:#02170e; }
.pill.direct { background:var(--blue); color:#05101f; }
.pill.indirect { background:var(--orange); color:#1d1000; }
.evidence { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.evidence>div { background:#0002; border-radius:7px; padding:9px 12px; }
.evidence ul { margin:.3rem 0; padding-left:20px; }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:9px; }
table { width:100%; border-collapse:collapse; background:var(--panel); }
th,td { text-align:left; padding:9px 11px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; }
tr:last-child td { border-bottom:0; } .num { text-align:right; white-space:nowrap;
  font-variant-numeric:tabular-nums; }
label { display:inline-flex; gap:8px; align-items:center; color:var(--muted); }
select { color:var(--text); background:var(--panel); border:1px solid var(--line);
  border-radius:6px; padding:5px 9px; }
svg { width:100%; min-width:600px; height:auto; background:var(--panel);
  border:1px solid var(--line);
  border-radius:10px; margin-top:12px; }
.grid { stroke:var(--line); } .axis { fill:var(--muted); font:11px ui-monospace,monospace; }
.line { fill:none; stroke-width:2.3; } .line.peak { stroke:var(--blue); }
.line.retained { stroke:var(--purple); stroke-dasharray:6 4; }
.dot.peak { fill:var(--blue); } .dot.retained { fill:var(--purple); }
.dot.flagged { fill:var(--red); stroke:var(--bg); stroke-width:2; }
.legend { color:var(--muted); display:flex; gap:11px; align-items:center; }
.key { width:16px; height:3px; display:inline-block; } .key.peak { background:var(--blue); }
.key.retained { background:var(--purple); } .key.flagged { width:10px; height:10px;
  border-radius:50%; background:var(--red); }
details { margin-top:30px; color:var(--muted); } pre { overflow:auto; font-size:12px; }
footer { color:var(--muted); margin-top:34px; font-size:.85rem; }
@media (prefers-color-scheme:light) { :root { --bg:#f5f7fb; --panel:#fff; --text:#172033;
  --muted:#66728a; --line:#d9dfeb; --blue:#1267c4; --purple:#7c3cb5; --red:#c72d44;
  --green:#087b50; --orange:#a75a00; } }
@media (max-width:700px) { .evidence { grid-template-columns:1fr; } main { padding:22px 14px; } }
"""

_SCRIPT = """
const select = document.getElementById('unit-select');
if (select) select.addEventListener('change', () => {
  document.querySelectorAll('.unit-chart').forEach((chart) => {
    chart.hidden = chart.dataset.unit !== select.value;
  });
});
"""


def html_report(data: dict) -> str:
    """A dependency-free HTML report with interactive range charts."""
    nonce = secrets.token_urlsafe(18)
    warnings = "".join(f'<div class="warn">⚠ {_h(w)}</div>' for w in data.get("warnings", []))
    notes = "".join(f'<div class="warn">ℹ {_h(n)}</div>' for n in data.get("notes", []))
    raw = _h(json.dumps(data, indent=2, ensure_ascii=False))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        f'style-src \'unsafe-inline\'; script-src \'nonce-{nonce}\'; base-uri \'none\'">'
        f'<title>MemBlame · {_h(data.get("kind", "report"))}</title><style>{_STYLE}</style>'
        f'</head><body><main>{warnings}{notes}{_html_body(data)}'
        f'<div class="meta"><span>Workload <code>{_h(data.get("workload", ""))}</code></span>'
        f'<span>Python <code>{_h(data.get("python", ""))}</code></span></div>'
        f'<details><summary>Machine-readable result</summary><pre>{raw}</pre></details>'
        '<footer>Generated by MemBlame · schema 1 · self-contained report</footer>'
        f'</main><script nonce="{nonce}">{_SCRIPT}</script></body></html>'
    )
