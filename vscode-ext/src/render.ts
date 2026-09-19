// Render memblame JSON (schema 1) into webview HTML. No vscode imports (unit-testable).

export function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function mb(n: number, signed = false): string {
  const sign = signed && n > 0 ? "+" : "";
  if (n !== 0 && Math.abs(n) < 100_000) {
    return `${sign}${(n / 1e3).toFixed(1)} KB`;
  }
  return `${sign}${(n / 1e6).toFixed(1)} MB`;
}

function link(file: string, line: number, text: string): string {
  return `<a href="#" class="src" data-file="${esc(file)}" data-line="${Number(line) || 1}">${esc(text)}</a>`;
}

function commitLabel(c: any): string {
  if (!c) {
    return "";
  }
  if (c.sha === "WORKTREE") {
    return `<span class="sha">working tree</span>`;
  }
  return `<span class="sha">${esc(c.short)}</span> ${esc(c.subject)} <span class="muted">${esc(c.author)}</span>`;
}

// ------------------------------------------------------------------ findings

export function verdictHtml(v: any): string {
  if (!v || v.kind === "none") {
    return "";
  }
  if (v.kind === "unattributed") {
    return `<p class="verdict muted">Not attributed: ${esc(v.reason ?? "no attribution data")}</p>`;
  }
  const where = link(v.file, v.line, `${v.qualname}()  ${v.file}:${v.line}`);
  const parts = [
    `<p class="verdict"><span class="tag ${v.kind}">${v.kind}</span> ${where} <b>${mb(v.cum_delta, true)}</b></p>`,
  ];
  if (v.kind === "direct") {
    const hunks = (v.hunks ?? []).map((h: any) => link(h.file, h.new_start, h.header)).join(", ");
    parts.push(`<p class="sub">This function changed in ${hunks || "the diff"}.</p>`);
  } else {
    parts.push(
      `<p class="sub">This function did not change; its memory grew because of a change elsewhere (a caller, data or config). See changed functions below.</p>`,
    );
  }
  const hot = (v.hot_lines ?? []).map((l: any) => `<li>${link(l.file, l.line, `${l.file}:${l.line}`)} ${mb(l.bytes)}</li>`);
  if (hot.length) {
    parts.push(`<p class="sub">Hot lines</p><ul class="lines">${hot.join("")}</ul>`);
  }
  const alloc = (v.allocated_at ?? []).map((l: any) => `<li>${link(l.file, l.line, `${l.file}:${l.line}`)} ${mb(l.bytes)}</li>`);
  if (alloc.length) {
    parts.push(`<p class="sub">Memory is allocated at</p><ul class="lines">${alloc.join("")}</ul>`);
  }
  return parts.join("");
}

export function functionsTable(fns: any[]): string {
  if (!fns?.length) {
    return "";
  }
  const rows = fns
    .slice(0, 8)
    .map(
      (f) =>
        `<tr><td class="num">${mb(f.cum_delta, true)}</td><td class="num">${mb(f.self_delta, true)}</td>` +
        `<td>${link(f.file, f.line, f.id)}${f.changed ? ' <span class="tag direct">changed</span>' : ""}</td></tr>`,
    )
    .join("");
  return `<table class="fns"><thead><tr><th class="num">Δ incl. callees</th><th class="num">Δ own</th><th>function</th></tr></thead><tbody>${rows}</tbody></table>`;
}

export function findingCard(f: any, commit?: any): string {
  const up = f.delta > 0;
  const head = commit ? `<div class="commit">${commitLabel(commit)}</div>` : "";
  return (
    `<div class="card ${up ? "up" : "down"}">${head}` +
    `<div class="metric"><span class="tag ${up ? "regression" : "improvement"}">${up ? "regression" : "improvement"}</span> ` +
    `<b>${esc(f.metric)}</b> ${mb(f.base)} → ${mb(f.head)} <b>${mb(f.delta, true)}</b> ` +
    `<span class="muted">(noise ±${mb(f.band)}) · ${esc(f.unit)}</span></div>` +
    verdictHtml(f.verdict) +
    functionsTable(f.functions) +
    `</div>`
  );
}

// ------------------------------------------------------------------ chart

interface Series {
  peak: (number | null)[];
  end: (number | null)[];
}

export function chartSvg(points: any[], unit: string, flaggedPeak: Set<number>, flaggedEnd: Set<number> = new Set()): string {
  const W = 760, H = 240, L = 64, R = 16, T = 16, B = 36;
  const s: Series = {
    peak: points.map((p) => p.units?.[unit]?.peak?.median ?? null),
    end: points.map((p) => p.units?.[unit]?.end?.median ?? null),
  };
  const all = [...s.peak, ...s.end].filter((v): v is number => v !== null);
  const max = Math.max(1, ...all) * 1.1;
  const n = points.length;
  const x = (i: number) => L + (n <= 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (n - 1));
  const y = (v: number) => T + (H - T - B) * (1 - v / max);
  const grid: string[] = [];
  for (let k = 0; k <= 4; k++) {
    const v = (max / 4) * k;
    grid.push(
      `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>` +
        `<text class="axis" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${esc(mb(v))}</text>`,
    );
  }
  const path = (vals: (number | null)[]) => {
    let started = false;
    return vals
      .map((v, i) => {
        if (v === null) {
          return "";
        }
        const cmd = started ? "L" : "M";
        started = true;
        return `${cmd}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      })
      .join(" ");
  };
  const every = Math.max(1, Math.ceil(n / 12));
  const labels = points
    .map((p, i) =>
      i % every === 0 || i === n - 1
        ? `<text class="axis" x="${x(i)}" y="${H - B + 16}" text-anchor="middle">${esc(p.commit.short)}</text>`
        : "",
    )
    .join("");
  const dots = points
    .map((p, i) => {
      const pk = s.peak[i];
      const en = s.end[i];
      const tip = `${p.commit.short} ${p.commit.subject}\npeak ${pk === null ? "-" : mb(pk)}  retained ${en === null ? "-" : mb(en)}`;

      const col = `<rect class="hit" data-point="${i}" x="${x(i) - 8}" y="${T}" width="16" height="${H - T - B}"><title>${esc(tip)}</title></rect>`;
      const fp = flaggedPeak.has(i);
      const fe = flaggedEnd.has(i);
      const a = pk === null ? "" : `<circle class="dot${fp ? " flagged" : ""} peak" cx="${x(i)}" cy="${y(pk)}" r="${fp ? 6 : 3.5}"/>`;
      const b = en === null ? "" : `<circle class="dot${fe ? " flagged" : ""} end" cx="${x(i)}" cy="${y(en)}" r="${fe ? 6 : 3}"/>`;
      return a + b + col;
    })
    .join("");
  return (
    `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="memory by commit for ${esc(unit)}">` +
    grid.join("") +
    `<path class="line peak" d="${path(s.peak)}"/><path class="line end" d="${path(s.end)}"/>` +
    dots +
    labels +
    `</svg>` +
    `<div class="legend"><span class="key peak"></span>peak <span class="key end"></span>retained after run <span class="key flagged"></span>significant change</div>`
  );
}

// ------------------------------------------------------------------ views

function unitsOf(points: any[]): string[] {
  const names: string[] = [];
  for (const p of points) {
    for (const u of Object.keys(p.units ?? {})) {
      if (!names.includes(u)) {
        names.push(u);
      }
    }
  }
  return names;
}

function warnings(d: any): string {
  const ws: string[] = d.warnings ?? [];
  return ws.length ? `<div class="warn">${ws.map((w) => `<p>⚠ ${esc(w)}</p>`).join("")}</div>` : "";
}

function renderRange(d: any): string {
  const points: any[] = d.points ?? [];
  const units = unitsOf(points);
  const findings: any[] = d.findings ?? [];
  const bySha = new Map(points.map((p, i) => [p.commit.sha, i]));
  const ranked = [...units].sort(
    (a, b) =>
      Math.max(0, ...findings.filter((f) => f.unit === b).map((f) => Math.abs(f.delta))) -
      Math.max(0, ...findings.filter((f) => f.unit === a).map((f) => Math.abs(f.delta))),
  );
  const select =
    ranked.length > 1
      ? `<label class="muted">Unit <select id="unit">${ranked.map((u) => `<option value="${esc(u)}">${esc(u)}</option>`).join("")}</select></label>`
      : "";
  const charts = ranked
    .map((u, k) => {
      const at = (metric: string) =>
        new Set(findings.filter((f) => f.unit === u && f.metric === metric).map((f) => bySha.get(f.commit) ?? -1));
      return `<div class="unit-chart" data-unit="${esc(u)}" ${k ? "hidden" : ""}>${chartSvg(points, u, at("peak"), at("retained"))}</div>`;
    })
    .join("");
  const details = points
    .map((p, i) => {
      const fs = findings.filter((f) => f.commit === p.commit.sha);
      const rows = Object.entries(p.units ?? {})
        .map(
          ([name, u]: [string, any]) =>
            `<tr><td>${esc(name)}</td><td class="num">${mb(u.peak.median)}</td><td class="num">${mb(u.end.median)}</td><td>${esc(u.outcome)}</td></tr>`,
        )
        .join("");
      return (
        `<div class="point" data-point="${i}" hidden><h3>${commitLabel(p.commit)}</h3>` +
        (fs.length ? fs.map((f) => findingCard(f)).join("") : `<p class="muted">No significant change vs the previous commit.</p>`) +
        `<table class="fns"><thead><tr><th>unit</th><th class="num">peak</th><th class="num">retained</th><th>outcome</th></tr></thead><tbody>${rows}</tbody></table></div>`
      );
    })
    .join("");
  const summary = findings.length
    ? `<h2>Findings</h2>${findings.map((f) => findingCard(f, points[bySha.get(f.commit) ?? 0]?.commit)).join("")}`
    : `<h2>Findings</h2><p>No significant memory changes in this range.</p>`;
  return (
    `<h1>Memory over ${points.length} commits</h1>${select}${charts}` +
    `<p class="muted hint">Click a commit in the chart for details.</p>${details}${summary}`
  );
}

function renderDiff(d: any): string {
  const title = `${commitLabel(d.base)} → ${commitLabel(d.head)}`;
  if (d.valid === false) {
    return `<h1>Comparison</h1><p>${title}</p>`;
  }
  const findings: any[] = d.findings ?? [];
  const rows = (d.units ?? [])
    .filter((u: any) => u.status === "compared")
    .flatMap((u: any) =>
      u.metrics.map(
        (m: any) =>
          `<tr class="${m.significant ? (m.delta > 0 ? "bad" : "good") : ""}"><td>${esc(u.name)}</td><td>${esc(m.metric)}</td>` +
          `<td class="num">${mb(m.base)}</td><td class="num">${mb(m.head)}</td><td class="num">${mb(m.delta, true)}</td>` +
          `<td class="num muted">±${mb(m.band)}</td></tr>`,
      ),
    )
    .join("");
  const changed = (d.changed_functions ?? []).map((c: any) => link(c.file, c.line, c.id)).join(", ");
  const verdict = findings.length
    ? findings.some((f) => f.delta > 0)
      ? `<p class="big bad">Memory went up.</p>`
      : `<p class="big good">Memory went down.</p>`
    : `<p class="big">No significant memory change.</p>`;
  return (
    `<h1>Memory comparison</h1><p>${title}</p>${verdict}` +
    findings.map((f) => findingCard(f)).join("") +
    `<h2>All measurements</h2><table class="fns"><thead><tr><th>unit</th><th>metric</th><th class="num">before</th><th class="num">after</th><th class="num">Δ</th><th class="num">noise</th></tr></thead><tbody>${rows}</tbody></table>` +
    (changed ? `<p class="muted">Changed functions: ${changed}</p>` : "")
  );
}

function renderBisect(d: any): string {
  if (d.status !== "found") {
    return `<h1>Bisect</h1><p class="big">${esc(d.message)}</p>`;
  }
  const trail = (d.measurements ?? [])
    .map(
      (t: any) =>
        `<tr class="${t.bad ? "bad" : "good"}"><td>${t.bad ? "bad" : "good"}</td><td>${commitLabel(t.commit)}</td><td class="num">${mb(t.value)}</td></tr>`,
    )
    .join("");
  return (
    `<h1>First bad commit</h1><p class="big">${commitLabel(d.culprit)}</p>` +
    `<p class="muted">${esc(d.unit)} · ${esc(d.metric)} above ${mb(d.threshold)} · ${d.steps} bisect steps for ${d.candidates} candidate commits</p>` +
    (d.findings ?? []).map((f: any) => findingCard(f)).join("") +
    `<h2>Measurements</h2><table class="fns"><tbody>${trail}</tbody></table>`
  );
}

export function renderBody(d: any): string {
  const meta = `<p class="muted">workload <code>${esc(d.workload)}</code> · ${esc(d.python)}</p>`;
  let body: string;
  switch (d.kind) {
    case "range":
      body = renderRange(d);
      break;
    case "diff":
      body = renderDiff(d);
      break;
    case "bisect":
      body = renderBisect(d);
      break;
    default:
      body = `<pre>${esc(JSON.stringify(d, null, 1))}</pre>`;
  }
  return warnings(d) + body + meta;
}

const STYLE = `
:root { color-scheme: light dark; }
body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); background: var(--vscode-editor-background); padding: 0 20px 32px; max-width: 980px; }
h1 { font-size: 1.5em; font-weight: 600; margin: 18px 0 6px; }
h2 { font-size: 1.15em; font-weight: 600; margin: 24px 0 8px; }
h3 { font-size: 1em; font-weight: 600; }
a.src { color: var(--vscode-textLink-foreground); text-decoration: none; font-family: var(--vscode-editor-font-family); }
a.src:hover { text-decoration: underline; }
.muted { color: var(--vscode-descriptionForeground); }
.sha { font-family: var(--vscode-editor-font-family); font-weight: 600; }
.big { font-size: 1.2em; font-weight: 600; }
.big.bad { color: var(--vscode-charts-red); } .big.good { color: var(--vscode-charts-green); }
.card { border: 1px solid var(--vscode-panel-border); border-left: 4px solid var(--vscode-charts-red); border-radius: 4px; padding: 10px 14px; margin: 10px 0; background: var(--vscode-editorWidget-background); }
.card.down { border-left-color: var(--vscode-charts-green); }
.card .commit { margin-bottom: 6px; }
.tag { display: inline-block; font-size: 0.8em; padding: 0 6px; border-radius: 8px; text-transform: uppercase; letter-spacing: .03em; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
.tag.regression { background: var(--vscode-charts-red); color: #fff; } .tag.improvement { background: var(--vscode-charts-green); color: #fff; }
.tag.direct { background: var(--vscode-charts-blue); color: #fff; } .tag.indirect { background: var(--vscode-charts-orange); color: #fff; }
.verdict { margin: 8px 0 2px; } .sub { margin: 2px 0; color: var(--vscode-descriptionForeground); }
ul.lines { margin: 2px 0 6px; padding-left: 18px; }
table.fns { border-collapse: collapse; margin: 8px 0; width: 100%; }
table.fns th, table.fns td { text-align: left; padding: 3px 8px; border-bottom: 1px solid var(--vscode-panel-border); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.bad td { color: var(--vscode-charts-red); } tr.good td { color: var(--vscode-charts-green); }
.warn { border: 1px solid var(--vscode-inputValidation-warningBorder); background: var(--vscode-inputValidation-warningBackground); padding: 4px 12px; margin-top: 12px; border-radius: 4px; }
svg.chart { width: 100%; height: auto; margin-top: 8px; }
.grid { stroke: var(--vscode-panel-border); stroke-width: 1; }
.axis { fill: var(--vscode-descriptionForeground); font-size: 11px; font-family: var(--vscode-editor-font-family); }
.line { fill: none; stroke-width: 2; } .line.peak { stroke: var(--vscode-charts-blue); } .line.end { stroke: var(--vscode-charts-purple); stroke-dasharray: 5 4; }
.dot.peak { fill: var(--vscode-charts-blue); } .dot.end { fill: var(--vscode-charts-purple); }
.dot.flagged { fill: var(--vscode-charts-red); stroke: var(--vscode-editor-background); stroke-width: 2; }
.hit { fill: transparent; cursor: pointer; } .hit:hover, .hit.sel { fill: var(--vscode-list-hoverBackground); opacity: .5; }
.legend { font-size: .9em; color: var(--vscode-descriptionForeground); display: flex; gap: 14px; align-items: center; }
.key { display: inline-block; width: 14px; height: 3px; margin-right: 4px; vertical-align: middle; }
.key.peak { background: var(--vscode-charts-blue); } .key.end { background: var(--vscode-charts-purple); } .key.flagged { background: var(--vscode-charts-red); height: 10px; width: 10px; border-radius: 50%; }
select { background: var(--vscode-dropdown-background); color: var(--vscode-dropdown-foreground); border: 1px solid var(--vscode-dropdown-border); padding: 2px 4px; }
code { font-family: var(--vscode-editor-font-family); }
`;

const SCRIPT = `
const vscode = acquireVsCodeApi();
document.addEventListener('click', (e) => {
  const a = e.target.closest('a.src');
  if (a) { e.preventDefault(); vscode.postMessage({ type: 'open', file: a.dataset.file, line: Number(a.dataset.line) }); return; }
  const hit = e.target.closest('.hit');
  if (hit) {
    document.querySelectorAll('.hit.sel').forEach((h) => h.classList.remove('sel'));
    document.querySelectorAll('.hit[data-point="' + hit.dataset.point + '"]').forEach((h) => h.classList.add('sel'));
    document.querySelectorAll('.point').forEach((p) => { p.hidden = p.dataset.point !== hit.dataset.point; });
  }
});
const unit = document.getElementById('unit');
if (unit) unit.addEventListener('change', () => {
  document.querySelectorAll('.unit-chart').forEach((c) => { c.hidden = c.dataset.unit !== unit.value; });
});
`;

export function renderHtml(d: any, nonce: string, cspSource: string): string {
  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>MemBlame</title><style>${STYLE}</style></head>
<body>${renderBody(d)}<script nonce="${nonce}">${SCRIPT}</script></body></html>`;
}
