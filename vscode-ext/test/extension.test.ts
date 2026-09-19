// Unit tests for the vscode-free modules. Run with: npm test
import * as assert from "node:assert/strict";
import * as cp from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { buildArgs, parseProgress, runMemblame } from "../src/cli";
import { chartSvg, esc, mb, niceStep, renderHtml } from "../src/render";
import { findTests, isTestFile, locateScope, moduleName, pytestWorkload, quoteWorkloadArg, suggestWorkloads, tomlDefinesWorkload } from "../src/workload";

const fixture = (name: string) =>
  JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "test", "fixtures", `${name}.json`), "utf8"));

const bundled = path.join(__dirname, "..", "..", "python");
const python = process.env.MEMBLAME_TEST_PYTHON ?? (process.platform === "win32" ? "python" : "python3");

test("generated script and pytest workloads round-trip through the Python parser", () => {
  const file = "bench scripts/it's a benchmark.py";
  const testFile = "test folder/test_example.py";
  const nodeId = `test_example[a 'single' and "double" quote]`;
  const workloads = [
    suggestWorkloads(file, "x = 1", 0)[0].workload,
    ...suggestWorkloads(testFile, "def test_example(): pass", 0).map((s) => s.workload),
    pytestWorkload(testFile, nodeId),
    `script:${quoteWorkloadArg("C:\\my dir\\run.py")}`,
  ];
  const expected = [[file], [`${testFile}::test_example`], [testFile], [`${testFile}::${nodeId}`], ["C:\\my dir\\run.py"]];
  const code = [
    "import json, sys",
    "from memblame import measure",
    "workloads = json.loads(sys.argv[1])",
    "for platform in ('posix', 'nt'):",
    "    measure.os.name = platform",
    "    print(json.dumps([measure.split_args(w.partition(':')[2]) for w in workloads]))",
  ].join("\n");
  const output = cp.execFileSync(python, ["-c", code, JSON.stringify(workloads)], {
    env: { ...process.env, PYTHONPATH: bundled }, encoding: "utf8",
  });
  for (const line of output.trim().split(/\r?\n/)) {
    assert.deepEqual(JSON.parse(line), expected);
  }
});

test("generated script workload runs with spaces; a crashed workload is rejected", async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "mb-path-test-"));
  try {
    cp.execFileSync("git", ["init", "-q", repo]);
    cp.execFileSync("git", ["-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
      "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "initial"]);
    const rel = "bench scripts/it's a benchmark.py";
    fs.mkdirSync(path.join(repo, "bench scripts"));
    fs.writeFileSync(path.join(repo, rel), "x = bytearray(100000)\n");
    const workload = suggestWorkloads(rel, "x = 1", 0)[0].workload;
    const request = { python, repo, bundledPath: bundled,
      args: ["run", "--no-cache", "--runs", "1", ...buildArgs({ workload, python, repo })] };
    const result = await runMemblame(request).result;
    assert.equal(result.result.units.workload.outcome, "passed");
    fs.writeFileSync(path.join(repo, rel), "import os\nos._exit(7)\n");
    await assert.rejects(runMemblame(request).result, /runner crashed \(exit 7\)/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("parseProgress", () => {
  assert.deepEqual(parseProgress("memblame: [3/10] measuring abc 'x'"), { message: "measuring abc 'x'", step: [3, 10] });
  assert.deepEqual(parseProgress("memblame: good abc cached"), { message: "good abc cached" });
  assert.equal(parseProgress("random pytest output"), undefined);
});

test("buildArgs", () => {
  const args = buildArgs({ workload: "pytest:t.py", runs: 2, importPaths: ["src"], python: "/py", repo: "/r" });
  assert.deepEqual(args, ["-C", "/r", "-w", "pytest:t.py", "--python", "/py", "--json", "--runs", "2", "--pythonpath", "src"]);
  assert.deepEqual(buildArgs({ python: "/py", repo: "/r" }), ["-C", "/r", "--python", "/py", "--json"]);
});

test("repository workload configuration detection", () => {
  assert.equal(tomlDefinesWorkload('workload = "call:pkg:run"\n', false), true);
  assert.equal(tomlDefinesWorkload('[tool.memblame]\nruns = 2\nworkload = "pytest:tests"\n', true), true);
  assert.equal(tomlDefinesWorkload('[project]\nworkload = "not memblame"\n', true), false);
  assert.equal(tomlDefinesWorkload('[tool.memblame.other]\nworkload = "not direct"\n', true), false);
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

test("nice axis steps", () => {
  assert.equal(niceStep(14_400_000), 20_000_000);
  assert.equal(niceStep(3_000), 5_000);
  assert.equal(niceStep(1_000_000), 1_000_000);
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

test("locateScope finds the function in the current text, nearest to the old line", () => {
  const text = ["import x", "", "class Loader:", "    def load(self):", "        pass", "", "def load():", "    pass"].join("\n");
  assert.equal(locateScope(text, "Loader.load", 4), 3);
  assert.equal(locateScope(text, "load", 20), 6);
  assert.equal(locateScope(text, "Loader", 1), 2);
  assert.equal(locateScope(text, "<module>", 9), 0);
  assert.equal(locateScope(text, "gone", 3), undefined);
});

test("reports explain skipped commits and units that were not compared", () => {
  const diff = {
    kind: "diff", workload: "w", python: "p", valid: true, findings: [], notes: ["working tree is clean"],
    base: { sha: "a", short: "a", subject: "s", author: "x" }, head: { sha: "WORKTREE", short: "working", subject: "", author: "" },
    units: [
      { name: "t::a", status: "outcome_changed", outcome: { base: "passed", head: "error" } },
      { name: "t::b", status: "new" },
    ],
  };
  const html = renderHtml(diff, "N", "c");
  assert.match(html, /outcome changed passed → error/);
  assert.match(html, /only exists after the change/);
  assert.match(html, /working tree is clean/);
  const range = fixture("range");
  range.points[3].valid = false;
  range.points[3].units = {};
  const r = renderHtml(range, "N", "c");
  assert.match(r, /Skipped: this commit could not be measured/);
  assert.doesNotMatch(r, /undefined|NaN/);
});
