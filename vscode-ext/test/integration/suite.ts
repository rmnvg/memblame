// Runs inside VS Code (extension host). Exported run() is called by @vscode/test-electron.
import * as assert from "node:assert/strict";
import * as path from "node:path";
import * as vscode from "vscode";

async function step(name: string, fn: () => Promise<void>) {
  const t = Date.now();
  try {
    await fn();
  } catch (err: any) {
    console.log(`  ✖ ${name}: ${err?.stack ?? err}`);
    throw err;
  }
  console.log(`  ✔ ${name} (${((Date.now() - t) / 1000).toFixed(1)}s)`);
}

export async function run(): Promise<void> {
  const repo = process.env.MEMBLAME_TEST_REPO!;
  const ext = vscode.extensions.getExtension("memblame.memblame")!;
  assert.ok(ext, "extension is installed");
  const api: any = await ext.activate();

  await step("commands are registered", async () => {
    const all = await vscode.commands.getCommands(true);
    for (const c of ["compareWorkingTree", "compareCommits", "analyzeRange", "findRegression", "setWorkload"]) {
      assert.ok(all.includes(`memblame.${c}`), c);
    }
  });

  await step("'Memory vs HEAD' lens appears on pytest tests", async () => {
    const uri = vscode.Uri.file(path.join(repo, "tests", "test_app.py"));
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));
    const lenses = (await vscode.commands.executeCommand<vscode.CodeLens[]>("vscode.executeCodeLensProvider", uri)) ?? [];
    const titles = lenses.map((l) => l.command?.title ?? "");
    assert.ok(titles.filter((t) => t.includes("Memory vs HEAD")).length >= 2, titles.join(" | "));
  });

  await step("clicking the test lens measures exactly that test", async () => {
    const uri = vscode.Uri.file(path.join(repo, "tests", "test_app.py"));
    const lenses = (await vscode.commands.executeCommand<vscode.CodeLens[]>("vscode.executeCodeLensProvider", uri)) ?? [];
    const lens = lenses.find((l) => l.command?.title.includes("Memory vs HEAD"))!;
    await vscode.commands.executeCommand(lens.command!.command, ...(lens.command!.arguments ?? []));
    const r = api.lastResult();
    assert.equal(r?.kind, "diff", JSON.stringify(r));
    assert.ok(r.valid !== false && r.units, `no measurements; warnings: ${JSON.stringify(r.warnings)}`);
    assert.equal(r.workload, "pytest:tests/test_app.py::test_pipeline");
    assert.deepEqual(r.units.map((u: any) => u.name), ["tests/test_app.py::test_pipeline"]);
    assert.equal(r.findings[0]?.verdict?.function, "shop/parse.py::load_rows");
  });

  await step("working tree vs HEAD blames the uncommitted change", async () => {
    await vscode.commands.executeCommand("memblame.compareWorkingTree");
    const r = api.lastResult();
    assert.equal(r?.kind, "diff");
    assert.equal(r.head.sha, "WORKTREE");
    const f = r.findings.find((x: any) => x.metric === "peak");
    assert.ok(f && f.delta > 0, JSON.stringify(r.findings));
    assert.equal(f.verdict.function, "shop/parse.py::load_rows");
    assert.match(api.reportHtml() ?? "", /Memory went up/);
  });

  await step("blamed function gets a CodeLens", async () => {
    const uri = vscode.Uri.file(path.join(repo, "shop", "parse.py"));
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));
    const lenses = (await vscode.commands.executeCommand<vscode.CodeLens[]>("vscode.executeCodeLensProvider", uri)) ?? [];
    const lens = lenses.find((l) => /peak memory \+/.test(l.command?.title ?? ""));
    assert.ok(lens, lenses.map((l) => l.command?.title).join(" | "));
    assert.equal(lens!.range.start.line, 3); // def load_rows is on line 4
  });

  await step("range over history finds both planted regressions", async () => {
    await vscode.commands.executeCommand("memblame.analyzeRange", "HEAD~9..HEAD");
    const r = api.lastResult();
    assert.equal(r?.kind, "range");
    const fns = r.findings.map((f: any) => `${f.metric}:${f.verdict.function}`).sort();
    assert.deepEqual(fns, ["peak:shop/parse.py::load_rows", "retained:shop/report.py::summarize"]);
    assert.match(api.reportHtml() ?? "", /<svg class="chart"/);
  });

  await step("bisect finds the first bad commit", async () => {
    await vscode.commands.executeCommand("memblame.findRegression", "HEAD~9", "+10MB");
    const r = api.lastResult();
    assert.equal(r?.status, "found", JSON.stringify(r));
    // Bisect tracks the metric that grew most (relatively): retained memory, 12 KB -> 58 MB.
    assert.equal(r.metric, "retained");
    assert.equal(r.culprit.subject, "cache summarize results");
    assert.equal(r.findings[0].verdict.function, "shop/report.py::summarize");
  });
}
