// Unit tests for the vscode-free modules. Run with: npm test
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import { buildArgs, parseProgress, runMemblame } from "../src/cli";
import { chartSvg, esc, mb, renderHtml } from "../src/render";
import { findTests, isTestFile, moduleName, suggestWorkloads } from "../src/workload";

const fixture = (name: string) =>
  JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "test", "fixtures", `${name}.json`), "utf8"));

test("parseProgress", () => {
  assert.deepEqual(parseProgress("memblame: [3/10] measuring abc 'x'"), { message: "measuring abc 'x'", step: [3, 10] });
  assert.deepEqual(parseProgress("memblame: good abc cached"), { message: "good abc cached" });
  assert.equal(parseProgress("random pytest output"), undefined);
});

test("buildArgs", () => {
  const args = buildArgs({ workload: "pytest:t.py", runs: 2, importPaths: ["src"], python: "/py", repo: "/r" });
  assert.deepEqual(args, ["-C", "/r", "-w", "pytest:t.py", "--python", "/py", "--json", "--runs", "2", "--pythonpath", "src"]);
});

test("formatting and escaping", () => {
  assert.equal(mb(28_200_000, true), "+28.2 MB");
  assert.equal(mb(-512, true), "-0.5 KB");
  assert.equal(mb(0), "0.0 MB");
  assert.equal(esc(`<a href="x">'&`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;");
});

test("test discovery and workload suggestions", () => {
  const src = [
    "import pytest",
    "def helper(): pass",
    "def test_one():",
    "    pass",
    "class TestGroup:",
    "    def test_two(self):",
    "        pass",
    "    async def test_three(self):",
    "        pass",
    "class Other:",
    "    def test_not_collected(self): pass",
    "def test_four(): pass",
  ].join("\n");
  assert.deepEqual(
    findTests(src).map((t) => [t.name, t.line]),
    [["test_one", 2], ["TestGroup::test_two", 5], ["TestGroup::test_three", 7], ["test_four", 11]],
  );
  assert.ok(isTestFile("tests/test_x.py") && isTestFile("pkg/x_test.py") && !isTestFile("pkg/x.py"));
  const s = suggestWorkloads("tests/test_x.py", src, 6);
  assert.equal(s[0].workload, "pytest:tests/test_x.py::TestGroup::test_two");
  assert.equal(s[1].workload, "pytest:tests/test_x.py");
  assert.equal(moduleName("src/pkg/mod.py"), "pkg.mod");
  assert.equal(moduleName("pkg/__init__.py"), "pkg");
  const m = suggestWorkloads("src/pkg/cli.py", "def main():\n  pass\n", 0);
  assert.deepEqual(m.map((x) => x.workload), ["call:pkg.cli:main", "script:src/pkg/cli.py"]);
});

test("range report renders chart, flagged points and findings", () => {
  const d = fixture("range");
  const html = renderHtml(d, "NONCE", "vscode-resource:");
  assert.match(html, /script-src 'nonce-NONCE'/);
  assert.match(html, /<svg class="chart"/);
  assert.equal((html.match(/class="dot flagged peak"/g) ?? []).length, 1, "one peak regression marker");
  assert.equal((html.match(/class="dot flagged end"/g) ?? []).length, 1, "one retained regression marker");
  assert.match(html, /load_rows\(\)/);
  assert.match(html, /summarize\(\)/);
  assert.match(html, /Memory is allocated at/);
  assert.equal((html.match(/class="point"/g) ?? []).length, d.points.length);
  assert.doesNotMatch(html, /undefined|NaN/);
});

test("diff and bisect reports", () => {
  const diff = renderHtml(fixture("diff"), "N", "c");
  assert.match(diff, /Memory went up\./);
  assert.match(diff, /data-file="shop\/parse.py"/);
  assert.doesNotMatch(diff, /undefined|NaN/);
  const bisect = renderHtml(fixture("bisect"), "N", "c");
  assert.match(bisect, /First bad commit/);
  assert.match(bisect, /include raw payload in rows/);
  assert.doesNotMatch(bisect, /undefined|NaN/);
});

test("chart handles gaps and a single point", () => {
  const pts = [
    { commit: { short: "a", subject: "s" }, units: { u: { peak: { median: 10 }, end: { median: 1 } } } },
    { commit: { short: "b", subject: "s" }, units: {} },
    { commit: { short: "c", subject: "s" }, units: { u: { peak: { median: 20 }, end: { median: 2 } } } },
  ];
  const svg = chartSvg(pts, "u", new Set([2]));
  assert.doesNotMatch(svg, /NaN/);
  assert.doesNotMatch(chartSvg(pts.slice(0, 1), "u", new Set()), /NaN/);
});

test("runMemblame reports a missing interpreter clearly", async () => {
  const job = runMemblame({ python: "/definitely/not/python", repo: process.cwd(), args: ["--version"], bundledPath: "/x" });
  await assert.rejects(job.result, /could not start/);
});

test("runMemblame end-to-end with the bundled engine (skipped without python3)", async (t) => {
  const bundled = path.join(__dirname, "..", "..", "python");
  if (!fs.existsSync(path.join(bundled, "memblame", "cli.py"))) {
    t.skip("python bundle missing");
    return;
  }
  const job = runMemblame({ python: "python3", repo: process.cwd(), args: ["diff", "--json", "-w", "call:nope:nope", "-C", "/"], bundledPath: bundled });
  await assert.rejects(job.result, /not inside a git repository|exited with code/);
});
