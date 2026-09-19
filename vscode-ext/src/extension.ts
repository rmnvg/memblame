import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { buildArgs, runMemblame } from "./cli";
import { mb, renderHtml } from "./render";
import { findTests, isTestFile, suggestWorkloads } from "./workload";

let panel: vscode.WebviewPanel | undefined;
let lastResult: any;
let running: { cancel: () => void } | undefined;
const annotations = new Map<string, { line: number; title: string; hover: string }[]>(); // abs file -> lenses
const hotLines = new Map<string, { line: number; text: string }[]>();
const lensChanged = new vscode.EventEmitter<void>();
const hotDecoration = vscode.window.createTextEditorDecorationType({
  after: { margin: "0 0 0 2em", color: new vscode.ThemeColor("editorCodeLens.foreground"), fontStyle: "italic" },
  isWholeLine: true,
});

const canonCache = new Map<string, string>();
/** Resolve symlinks (e.g. macOS /var -> /private/var): git reports real paths, editors may not. */
function canon(p: string): string {
  let c = canonCache.get(p);
  if (c === undefined) {
    try {
      c = fs.realpathSync.native(p);
    } catch {
      c = p;
    }
    canonCache.set(p, c);
  }
  return c;
}

/** Returned from activate(); used by the integration tests. */
export interface MemBlameApi {
  lastResult(): any;
  reportHtml(): string | undefined;
}

export function activate(ctx: vscode.ExtensionContext): MemBlameApi {
  const bundled = path.join(ctx.extensionPath, "python");
  const cmd = (id: string, fn: (...a: any[]) => any) => ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));

  cmd("memblame.compareWorkingTree", (workload?: string) => runCommand(bundled, ["diff", "HEAD"], workload));
  cmd("memblame.compareCommits", async () => {
    const repo = await repoRoot();
    if (!repo) {
      return;
    }
    const base = await pickCommit(repo, "Base commit (before)");
    if (!base) {
      return;
    }
    const head = await pickCommit(repo, "Head commit (after)", true);
    if (!head) {
      return;
    }
    await runCommand(bundled, head === "WORKTREE" ? ["diff", base] : ["diff", base, head]);
  });
  cmd("memblame.analyzeRange", async (rangeArg?: string) => {
    const def = vscode.workspace.getConfiguration("memblame").get<string>("defaultRange") || "HEAD~20..HEAD";
    const range =
      rangeArg ??
      (await vscode.window.showInputBox({
        prompt: "Commit range (BASE..HEAD). Measures both ends and only subdivides where memory changed; results are cached.",
        value: def,
        validateInput: (v) => (v.includes("..") ? undefined : "use BASE..HEAD, e.g. main~20..main"),
      }));
    if (range) {
      await runCommand(bundled, ["range", range]);
    }
  });
  cmd("memblame.findRegression", async (goodArg?: string, thresholdArg?: string) => {
    const repo = await repoRoot();
    if (!repo) {
      return;
    }
    const good = goodArg ?? (await pickCommit(repo, "Last GOOD commit (memory was fine here)"));
    if (!good) {
      return;
    }
    const threshold =
      thresholdArg ??
      (await vscode.window.showInputBox({
        prompt: "Threshold (optional): 200MB, +20MB or +10%. Empty = any significant increase over the good commit",
      }));
    if (threshold === undefined) {
      return;
    }
    const args = ["bisect", "--good", good, "--bad", "HEAD"];
    if (threshold.trim()) {
      args.push("--threshold", threshold.trim());
    }
    await runCommand(bundled, args);
  });
  cmd("memblame.setWorkload", () => chooseWorkload(true));
  cmd("memblame.showLastReport", () => (lastResult ? showReport(ctx, lastResult) : vscode.window.showInformationMessage("MemBlame: no report yet.")));
  cmd("memblame.clearAnnotations", () => {
    annotations.clear();
    hotLines.clear();
    lensChanged.fire();
    refreshDecorations();
  });
  cmd("memblame.openLocation", (file: string, line: number) => openLocation(file, line));

  ctx.subscriptions.push(
    vscode.languages.registerCodeLensProvider({ language: "python" }, new LensProvider()),
    vscode.window.onDidChangeVisibleTextEditors(refreshDecorations),
    { dispose: () => running?.cancel() },
  );

  const api: MemBlameApi = { lastResult: () => lastResult, reportHtml: () => panel?.webview.html };

  async function runCommand(bundledPath: string, args: string[], workloadArg?: string) {
    if (running) {
      vscode.window.showWarningMessage("MemBlame is already running.");
      return;
    }
    const repo = await repoRoot();
    if (!repo) {
      return;
    }
    const workload = workloadArg ?? (await chooseWorkload(false));
    if (!workload) {
      return;
    }
    const cfg = vscode.workspace.getConfiguration("memblame");
    const python = await resolvePython(repo);
    const full = [
      ...args,
      ...buildArgs({
        workload,
        runs: cfg.get<number>("runs"),
        nframe: cfg.get<number>("nframe"),
        importPaths: cfg.get<string[]>("importPaths"),
        python,
        repo,
      }),
    ];
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "MemBlame", cancellable: true },
      async (progress, token) => {
        let last = 0;
        const job = runMemblame({
          python,
          repo,
          args: full,
          bundledPath,
          onProgress: (p) => {
            let increment: number | undefined;
            if (p.step) {
              const pct = ((p.step[0] - 1) / p.step[1]) * 100;
              increment = pct - last;
              last = pct;
            }
            progress.report({ message: p.message, increment });
          },
        });
        running = job;
        token.onCancellationRequested(job.cancel);
        try {
          const result = await job.result;
          lastResult = result;
          applyAnnotations(result);
          showReport(ctx, result);
          summarize(result);
        } catch (err: any) {
          if (err?.message !== "cancelled") {
            vscode.window.showErrorMessage(`MemBlame: ${err?.message ?? err}`);
          }
        } finally {
          running = undefined;
        }
      },
    );
  }

  return api;
}

export function deactivate() {
  running?.cancel();
}

// ------------------------------------------------------------------ helpers

async function repoRoot(): Promise<string | undefined> {
  const doc = vscode.window.activeTextEditor?.document;
  const folder = (doc && vscode.workspace.getWorkspaceFolder(doc.uri)) ?? vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    vscode.window.showErrorMessage("MemBlame: open a folder inside a git repository first.");
    return undefined;
  }
  const { execFile } = await import("child_process");
  return new Promise((resolve) =>
    execFile("git", ["rev-parse", "--show-toplevel"], { cwd: folder.uri.fsPath }, (err, stdout) => {
      if (err) {
        vscode.window.showErrorMessage("MemBlame: this folder is not a git repository.");
        resolve(undefined);
      } else {
        resolve(stdout.trim());
      }
    }),
  );
}

async function pickCommit(repo: string, title: string, allowWorktree = false): Promise<string | undefined> {
  const { execFile } = await import("child_process");
  const log = await new Promise<string>((resolve) =>
    execFile("git", ["log", "--first-parent", "-50", "--format=%h%x00%s%x00%an%x00%ar"], { cwd: repo }, (_e, out) => resolve(out ?? "")),
  );
  const items: (vscode.QuickPickItem & { rev: string })[] = [];
  if (allowWorktree) {
    items.push({ label: "$(edit) Working tree", description: "including uncommitted changes", rev: "WORKTREE" });
  }
  for (const line of log.split("\n").filter(Boolean)) {
    const [sha, subject, author, when] = line.split("\0");
    items.push({ label: `$(git-commit) ${sha}`, description: subject, detail: `${author}, ${when}`, rev: sha });
  }
  const pick = await vscode.window.showQuickPick(items, { title, placeHolder: "Pick a commit (or type to filter)", matchOnDescription: true });
  return pick?.rev;
}

async function resolvePython(repo: string): Promise<string> {
  const configured = vscode.workspace.getConfiguration("memblame").get<string>("pythonPath");
  if (configured) {
    return configured;
  }
  const ext = vscode.extensions.getExtension("ms-python.python");
  if (ext) {
    try {
      const api: any = ext.isActive ? ext.exports : await ext.activate();
      const envPath = api?.environments?.getActiveEnvironmentPath?.(vscode.Uri.file(repo));
      if (envPath) {
        const env = await api.environments.resolveEnvironment(envPath);
        const exe = env?.executable?.uri?.fsPath ?? envPath.path;
        if (exe) {
          return exe;
        }
      }
    } catch {
      // fall through to defaults
    }
  }
  return process.platform === "win32" ? "python" : "python3";
}

async function chooseWorkload(force: boolean): Promise<string | undefined> {
  const cfg = vscode.workspace.getConfiguration("memblame");
  const current = cfg.get<string>("workload");
  if (current && !force) {
    return current;
  }
  const ed = vscode.window.activeTextEditor;
  const repo = await repoRoot();
  const items: (vscode.QuickPickItem & { workload?: string })[] = [];
  if (ed && repo) {
    const rel = path.relative(repo, ed.document.uri.fsPath).split(path.sep).join("/");
    for (const s of suggestWorkloads(rel, ed.document.getText(), ed.selection.active.line)) {
      items.push({ label: s.label, description: s.workload, detail: s.detail, workload: s.workload });
    }
  }
  items.push({ label: "$(pencil) Enter a workload…", detail: "pytest:<node id>  ·  script:<path> [args]  ·  call:<module>:<function>" });
  const pick = await vscode.window.showQuickPick(items, {
    title: "What should MemBlame run at each commit?",
    placeHolder: current ? `current: ${current}` : "Choose a deterministic test, script or function",
  });
  if (!pick) {
    return undefined;
  }
  let workload = pick.workload;
  if (!workload) {
    workload = await vscode.window.showInputBox({ prompt: "Workload", value: current || "pytest:tests/", placeHolder: "pytest:tests/test_big.py::test_load" });
  }
  if (workload) {
    await cfg.update("workload", workload, vscode.ConfigurationTarget.Workspace);
  }
  return workload;
}

function showReport(ctx: vscode.ExtensionContext, result: any) {
  if (!panel) {
    panel = vscode.window.createWebviewPanel("memblame.report", "MemBlame", vscode.ViewColumn.Beside, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [],
    });
    panel.onDidDispose(() => (panel = undefined), null, ctx.subscriptions);
    panel.webview.onDidReceiveMessage((m) => {
      if (m?.type === "open") {
        openLocation(path.join(lastResult?.repo ?? "", m.file), m.line);
      }
    });
  }
  const nonce = crypto.randomBytes(16).toString("base64");
  panel.title = `MemBlame: ${result.kind}`;
  panel.webview.html = renderHtml(result, nonce, panel.webview.cspSource);
  panel.reveal(vscode.ViewColumn.Beside, true);
}

async function openLocation(file: string, line: number) {
  try {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    const pos = new vscode.Position(Math.max(0, (line || 1) - 1), 0);
    await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.One, selection: new vscode.Range(pos, pos) });
  } catch {
    vscode.window.showWarningMessage(`MemBlame: cannot open ${file} (it may not exist in the working tree).`);
  }
}

function summarize(result: any) {
  const ups = (result.findings ?? []).filter((f: any) => f.delta > 0);
  if (result.kind === "bisect" && result.status === "found") {
    vscode.window.showInformationMessage(`MemBlame: first bad commit ${result.culprit.short} "${result.culprit.subject}"`);
  } else if (ups.length) {
    const f = ups[0];
    const where = f.verdict?.qualname ? ` in ${f.verdict.qualname}()` : "";
    vscode.window.showWarningMessage(`MemBlame: ${f.metric} memory ${mb(f.delta, true)}${where} (${f.unit}).`);
  } else if (result.valid !== false) {
    vscode.window.setStatusBarMessage("MemBlame: no significant memory increase", 8000);
  }
}

// ------------------------------------------------------------------ annotations

function applyAnnotations(result: any) {
  annotations.clear();
  hotLines.clear();
  const repo: string = result.repo ?? "";
  const findings: any[] = result.findings ?? [];
  for (const f of findings) {
    const v = f.verdict;
    if (!v?.file) {
      continue;
    }
    const abs = canon(path.join(repo, v.file));
    const since = f.commit ? ` at ${String(f.commit).slice(0, 7)}` : result.kind === "diff" ? ` vs ${result.base?.short ?? "base"}` : "";
    const title = `$(${f.delta > 0 ? "arrow-up" : "arrow-down"}) ${f.metric} memory ${mb(f.delta, true)}${since} · ${v.kind} · ${f.unit}`;
    const list = annotations.get(abs) ?? [];
    list.push({ line: v.line, title, hover: `MemBlame: ${v.function}` });
    annotations.set(abs, list);
    for (const hl of [...(v.hot_lines ?? []), ...(v.allocated_at ?? [])]) {
      const habs = canon(path.join(repo, hl.file));
      const hs = hotLines.get(habs) ?? [];
      if (!hs.some((h) => h.line === hl.line)) {
        hs.push({ line: hl.line, text: `◀ ${mb(hl.bytes)} live at ${f.metric} (memblame)` });
      }
      hotLines.set(habs, hs);
    }
  }
  lensChanged.fire();
  refreshDecorations();
}

function refreshDecorations() {
  for (const ed of vscode.window.visibleTextEditors) {
    const hs = hotLines.get(canon(ed.document.uri.fsPath)) ?? [];
    ed.setDecorations(
      hotDecoration,
      hs
        .filter((h) => h.line - 1 < ed.document.lineCount)
        .map((h) => ({ range: ed.document.lineAt(h.line - 1).range, renderOptions: { after: { contentText: h.text } } })),
    );
  }
}

class LensProvider implements vscode.CodeLensProvider {
  onDidChangeCodeLenses = lensChanged.event;

  provideCodeLenses(doc: vscode.TextDocument): vscode.CodeLens[] {
    const lenses: vscode.CodeLens[] = [];
    for (const a of annotations.get(canon(doc.uri.fsPath)) ?? []) {
      if (a.line - 1 < doc.lineCount) {
        const range = doc.lineAt(Math.max(0, a.line - 1)).range;
        lenses.push(new vscode.CodeLens(range, { title: a.title, tooltip: a.hover, command: "memblame.showLastReport" }));
      }
    }
    const folder = vscode.workspace.getWorkspaceFolder(doc.uri);
    if (folder && vscode.workspace.getConfiguration("memblame").get<boolean>("testCodeLens")) {
      const rel = path.relative(folder.uri.fsPath, doc.uri.fsPath).split(path.sep).join("/");
      if (isTestFile(rel)) {
        for (const t of findTests(doc.getText())) {
          lenses.push(
            new vscode.CodeLens(doc.lineAt(t.line).range, {
              title: "$(pulse) Memory vs HEAD",
              tooltip: "MemBlame: compare this test's memory with and without your uncommitted changes",
              command: "memblame.compareWorkingTree",
              arguments: [`pytest:${rel}::${t.name}`],
            }),
          );
        }
      }
    }
    return lenses;
  }
}
