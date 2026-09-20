// Unit tests for the vscode-free modules. Run with: npm test
import * as assert from "node:assert/strict";
import * as cp from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { buildArgs, parseProgress, runMemblame, chooseInterpreters, selectedFromPythonApi } from "../src/cli";
import { MemblameResult, Point, parseEngineResponse } from "../src/contract";
import { chartSvg, esc, mb, niceStep, renderHtml } from "../src/render";
import { findTests, isTestFile, locateScope, moduleName, pytestWorkload, quoteWorkloadArg, suggestWorkloads, tomlDefinesKey, tomlDefinesWorkload } from "../src/workload";

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
    assert.equal(result.kind, "run");
    assert.equal(result.result?.units.workload.outcome, "passed");
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
  assert.deepEqual(buildArgs({ repo: "/r" }), ["-C", "/r", "--json"]);
});

test("engine result contract rejects unknown schemas and incomplete envelopes", () => {
  assert.throws(() => parseEngineResponse({ schema: 2, kind: "diff" }), /schema 2/);
  assert.throws(
    () => parseEngineResponse({ schema: 1, kind: "diff", repo: "/r" }),
    /field workload is missing/,
  );
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
  assert.match(bisect, /Threshold crossing/);
  assert.match(bisect, /include raw payload in rows/);
  assert.doesNotMatch(bisect, /undefined|NaN/);
});

test("nice axis steps", () => {
  assert.equal(niceStep(14_400_000), 20_000_000);
  assert.equal(niceStep(3_000), 5_000);
  assert.equal(niceStep(1_000_000), 1_000_000);
});

test("chart handles gaps and a single point", () => {
  const unit = (peak: number, end: number) => ({ outcome: "passed",
    peak: { median: peak, min: peak, max: peak }, end: { median: end, min: end, max: end } });
  const point = (short: string, units: Point["units"]): Point => ({
    commit: { sha: short, short, subject: "s", author: "x" }, measured: true, valid: true, units,
  });
  const pts: Point[] = [
    point("a", { u: unit(10, 1) }),
    point("b", {}),
    point("c", { u: unit(20, 2) }),
  ];
  const svg = chartSvg(pts, "u", new Set([2]));
  assert.doesNotMatch(svg, /NaN/);
  assert.doesNotMatch(chartSvg(pts.slice(0, 1), "u", new Set()), /NaN/);
});

test("runMemblame reports a missing interpreter clearly", async () => {
  const job = runMemblame({ python: "/definitely/not/python", repo: process.cwd(), args: ["--version"], bundledPath: "/x" });
  await assert.rejects(job.result, /could not start/);
});

test("runMemblame end-to-end with the bundled engine (skipped without a bundle)", async (t) => {
  const bundled = path.join(__dirname, "..", "..", "python");
  if (!fs.existsSync(path.join(bundled, "memblame", "cli.py"))) {
    t.skip("python bundle missing");
    return;
  }
  const job = runMemblame({ python, repo: process.cwd(), args: ["diff", "--json", "-w", "call:nope:nope", "-C", "/"], bundledPath: bundled });
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

test("a run result renders as a measurement table, not a raw JSON dump", () => {
  const run: MemblameResult = {
    schema: 1, kind: "run", repo: "/r", workload: "w", python: "p",
    settings: { runs: 2, nframe: 16, timeout: 10, pythonpath: null },
    measurement_status: "complete", warnings: [],
    commit: { sha: "abc1234", short: "abc1234", subject: "a commit", author: "x" },
    result: {
      valid: true, runs: 2,
      units: {
        "tests/test_a.py::test_big": {
          outcome: "passed",
          peak: { median: 58_000_000, min: 57_900_000, max: 58_100_000 },
          end: { median: 1_200_000, min: 1_200_000, max: 1_200_000 },
          top: [{ id: "shop/parse.py::load_rows", self: 57_000_000, cumulative: 58_000_000 }],
        },
      },
    },
  };
  const html = renderHtml(run, "N", "c");
  assert.match(html, /Memory measurement/);
  assert.match(html, /tests\/test_a\.py::test_big/);
  assert.match(html, /58\.0 MB/);           // peak
  assert.match(html, /shop\/parse\.py::load_rows/);  // the biggest holder
  assert.match(html, /passed/);
  assert.doesNotMatch(html, /<pre>/);        // used to fall through to a JSON dump
  assert.doesNotMatch(html, /undefined|NaN/);

  const broken: MemblameResult = { ...run, result: { valid: false, runs: 0, units: {} } };
  assert.match(renderHtml(broken, "N", "c"), /Measurement unavailable/);
});

test("reports explain skipped commits and units that were not compared", () => {
  const diff: MemblameResult = {
    schema: 1, kind: "diff", repo: "/r", workload: "w", python: "p",
    settings: { runs: 1, nframe: 16, timeout: 10, pythonpath: null },
    measurement_status: "complete", warnings: [], valid: true, findings: [],
    notes: ["working tree is clean"],
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

test("interpreter precedence: explicit setting > repo config > Python extension > CLI discovery", () => {
  const base = { repoDefinesPython: false, fallback: "python3" };
  const pick = (input: Parameters<typeof chooseInterpreters>[0]) => {
    const { launcher, project } = chooseInterpreters(input);
    return [launcher, project];
  };
  // 1. an explicit memblame.pythonPath wins over everything, and runs the project too
  assert.deepEqual(
    pick({ ...base, explicit: "/x/py", repoDefinesPython: true, selected: "/sel/py" }),
    ["/x/py", "/x/py"],
  );
  // 2. repository config beats the editor's selection: no --python, so the CLI applies it
  assert.deepEqual(pick({ ...base, repoDefinesPython: true, selected: "/sel/py" }), ["/sel/py", undefined]);
  // 3. the interpreter selected in the Python extension beats auto-discovery
  assert.deepEqual(pick({ ...base, selected: "/sel/py" }), ["/sel/py", "/sel/py"]);
  // 4./5. nothing selected: the CLI discovers .venv itself; the engine is launched with python3
  assert.deepEqual(pick(base), ["python3", undefined]);
  assert.deepEqual(pick({ ...base, repoDefinesPython: true }), ["python3", undefined]);
});

test("repository config detection works for any key, only in MemBlame's table", () => {
  assert.equal(tomlDefinesKey('python = ".venv/bin/python"\n', false, "python"), true);
  assert.equal(tomlDefinesKey('[tool.memblame]\npython = "py"\n', true, "python"), true);
  assert.equal(tomlDefinesKey('[tool.memblame]\nruns = 2\n', true, "python"), false);
  assert.equal(tomlDefinesKey('[tool.black]\npython = "x"\n', true, "python"), false);
  assert.equal(tomlDefinesKey('[project]\nname = "python"\n', true, "python"), false);
  assert.equal(tomlDefinesKey('pythonpath = ["src"]\n', false, "python"), false, "prefix of another key");
});

test("an incomplete range still shows its findings, under a banner, never as an all-clear", () => {
  const range = fixture("range") as MemblameResult;
  range.measurement_status = "incomplete";
  range.incomplete_commits = 1;
  const html = renderHtml(range, "N", "c");
  assert.match(html, /Incomplete: 1 commit\(s\) could not be measured or did not pass/);
  assert.match(html, /not an all-clear/);
  assert.match(html, /load_rows\(\)/, "the regression between measured commits is still listed");
  assert.doesNotMatch(html, /No significant memory changes/);
  range.findings = [];
  const none = renderHtml(range, "N", "c");
  assert.match(none, /Measurement incomplete; no memory-regression conclusion/);
});

test("cancelling a run rejects and does not leave a worktree behind", async (t) => {
  if (!fs.existsSync(path.join(bundled, "memblame", "cli.py"))) {
    t.skip("python bundle missing");
    return;
  }
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "mb-cancel-"));
  const git = (...a: string[]) => cp.execFileSync("git", a, { cwd: repo, stdio: "ignore" });
  try {
    git("init", "-q", "-b", "main");
    git("config", "user.email", "t@example.com");
    git("config", "user.name", "t");
    const slow = "import time\ndef run():\n    time.sleep(120)\n";
    fs.writeFileSync(path.join(repo, "bench.py"), slow);
    git("add", "-A");
    git("commit", "-qm", "base");
    fs.writeFileSync(path.join(repo, "bench.py"), slow + "    # and again\n");
    git("add", "-A");
    git("commit", "-qm", "head");

    // Cancel only once the engine has really started measuring, so a worktree exists.
    let started: () => void;
    let sawMeasuring = false;
    const measuring = new Promise<void>((r) => (started = r));
    const job = runMemblame({
      python,
      repo,
      bundledPath: bundled,
      args: ["diff", "HEAD~1", "HEAD", "--json", "-w", "call:bench:run",
             "--python", python, "--runs", "1", "--no-cache", "-C", repo],
      onProgress: (p) => { if (/measuring/.test(p.message)) { sawMeasuring = true; started(); } },
    });
    await Promise.race([measuring, new Promise((r) => setTimeout(r, 30_000))]);
    // Otherwise the cancel below would prove nothing: the engine never got far enough to
    // create a worktree, and any exit would look like a successful cancellation.
    assert.ok(sawMeasuring, "engine never reached the measuring phase; cancel proves nothing");
    job.cancel();
    await assert.rejects(job.result, /cancelled/);

    if (process.platform !== "win32") {
      // SIGTERM reaches the CLI, which removes its worktree on the way out. Windows cancels
      // with taskkill /F, so nothing runs there; the next run reclaims it instead (covered
      // by test_stale_worktree_from_killed_run_is_removed on the Python side).
      const listed = cp.execFileSync("git", ["worktree", "list"], { cwd: repo, encoding: "utf8" });
      assert.equal(listed.trim().split("\n").length, 1, listed);
    }
  } finally {
    cp.execFileSync("git", ["worktree", "prune"], { cwd: repo, stdio: "ignore" });
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("the Python extension's selected interpreter, through every shape it comes in", async () => {
  const uri = (fsPath: string) => ({ fsPath });
  const api = (over: Record<string, unknown> = {}) => ({
    getActiveEnvironmentPath: () => ({ path: "/envs/x" }),
    resolveEnvironment: async () => ({ executable: { uri: uri("/envs/x/bin/python") } }),
    ...over,
  });

  // The normal case: the resolved executable wins over the environment path.
  assert.equal(await selectedFromPythonApi(api(), undefined), "/envs/x/bin/python");

  // Degraded shapes must all mean "no selection", never a thrown command.
  const degraded: Array<[string, unknown]> = [
    ["no api at all", undefined],
    ["empty api", {}],
    ["no environment selected", api({ getActiveEnvironmentPath: () => undefined })],
    ["resolveEnvironment missing", { getActiveEnvironmentPath: () => ({ path: "/envs/x" }) }],
    ["getActiveEnvironmentPath throws", api({ getActiveEnvironmentPath: () => { throw new Error("boom"); } })],
    ["resolveEnvironment rejects", api({ resolveEnvironment: async () => { throw new Error("boom"); } })],
  ];
  for (const [name, environments] of degraded) {
    const got = await selectedFromPythonApi(environments as never, undefined);
    assert.equal(got, name === "resolveEnvironment missing" ? "/envs/x" : undefined, name);
  }

  // Resolvable but with no executable: fall back to the environment path itself.
  for (const partial of [{}, { executable: {} }, { executable: { uri: {} } }]) {
    assert.equal(
      await selectedFromPythonApi(api({ resolveEnvironment: async () => partial }) as never, undefined),
      "/envs/x",
      JSON.stringify(partial),
    );
  }

  // And it feeds chooseInterpreters, which decides whether --python is passed at all.
  const selected = await selectedFromPythonApi(api(), undefined);
  assert.deepEqual(chooseInterpreters({ repoDefinesPython: false, selected, fallback: "python3" }),
    { launcher: "/envs/x/bin/python", project: "/envs/x/bin/python" });
  assert.deepEqual(chooseInterpreters({ repoDefinesPython: true, selected, fallback: "python3" }),
    { launcher: "/envs/x/bin/python" });
});
