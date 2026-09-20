import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { buildArgs, chooseInterpreters, InterpreterChoice, runMemblame } from "./cli";
import { Finding, MemblameResult } from "./contract";
import { mb, renderHtml } from "./render";
import { findTests, isTestFile, locateScope, pytestWorkload, suggestWorkloads, tomlDefinesKey } from "./workload";

interface PythonExtensionApi {
  environments?: {
    getActiveEnvironmentPath?(resource: vscode.Uri): { path: string } | undefined;
    resolveEnvironment(path: { path: string }): Promise<{
      executable?: { uri?: vscode.Uri };
    } | undefined>;
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

let panel: vscode.WebviewPanel | undefined;
let state: vscode.ExtensionContext | undefined;
let lastResult: MemblameResult | undefined;
let running: { cancel: () => void } | undefined;
const annotations = new Map<string, { line: number; qualname: string; title: string; hover: string }[]>(); // abs file -> lenses
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
  lastResult(): MemblameResult | undefined;
  reportHtml(): string | undefined;
}

export function activate(ctx: vscode.ExtensionContext): MemBlameApi {
  state = ctx;
  const bundled = path.join(ctx.extensionPath, "python");
  const cmd = (id: string, fn: (...args: never[]) => unknown) =>
    ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));

  cmd("memblame.compareWorkingTree", async (arg?: string | { file: string; test: string }) => {
    let workload = typeof arg === "string" ? arg : undefined;
    if (arg && typeof arg === "object") {
      const repo = await repoRoot(vscode.Uri.file(arg.file));
      if (!repo) {
        return;
      }
      const rel = path.relative(canon(repo), canon(arg.file)).split(path.sep).join("/");
      workload = pytestWorkload(rel, arg.test);
    }
    if (!(await saveOrContinue())) {
      return;
    }
    await runCommand(bundled, ["diff", "HEAD"], workload);
  });
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
    if (head === "WORKTREE" && !(await saveOrContinue())) {
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
    let workload = workloadArg ?? explicitSetting<string>("workload");
    const fromRepoConfig = !workload && (await repositoryDefinesKey(repo, "workload"));
    if (!workload && !fromRepoConfig) {
      workload = state?.workspaceState.get<string>("workload") ?? await chooseWorkload(false);
    }
    if (!workload && !fromRepoConfig) {
      return;
    }
    const { launcher, project } = await resolvePython(repo);
    const full = [
      ...args,
      ...buildArgs({
        workload,
        runs: explicitSetting<number>("runs"),
        nframe: explicitSetting<number>("nframe"),
        importPaths: explicitSetting<string[]>("importPaths"),
        python: project,
        repo,
      }),
    ];
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "MemBlame", cancellable: true },
      async (progress, token) => {
        let last = 0;
        const job = runMemblame({
          python: launcher,
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
          await applyAnnotations(result);
          showReport(ctx, result);
          summarize(result);
        } catch (err: unknown) {
          if (errorMessage(err) !== "cancelled") {
            vscode.window.showErrorMessage(`MemBlame: ${errorMessage(err)}`);
          }
        } finally {
          running = undefined;
        }
      },
    );
  }

  return api;
}

async function repositoryDefinesKey(repo: string, key: string): Promise<boolean> {
  for (const [name, pyproject] of [["memblame.toml", false], ["pyproject.toml", true]] as const) {
    try {
      const contents = await fs.promises.readFile(path.join(repo, name), "utf8");
      if (tomlDefinesKey(contents, pyproject, key)) {
        return true;
      }
    } catch (err: unknown) {
      if (!(err instanceof Error && "code" in err && err.code === "ENOENT")) {
        throw err;
      }
    }
  }
  return false;
}

export function deactivate() {
  running?.cancel();
}

// ------------------------------------------------------------------ helpers

async function repoRoot(forFile?: vscode.Uri): Promise<string | undefined> {
  const doc = forFile ?? vscode.window.activeTextEditor?.document.uri;
  const folder = (doc && vscode.workspace.getWorkspaceFolder(doc)) ?? vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    vscode.window.showErrorMessage("MemBlame: open a folder inside a git repository first.");
    return undefined;
  }
  const cwd = forFile ? path.dirname(forFile.fsPath) : folder.uri.fsPath;
  const { execFile } = await import("child_process");
  return new Promise((resolve) =>
    execFile("git", ["rev-parse", "--show-toplevel"], { cwd }, (err, stdout) => {
      if (err) {
        vscode.window.showErrorMessage("MemBlame: this folder is not a git repository.");
        resolve(undefined);
      } else {
        resolve(stdout.trim());
      }
    }),
  );
}

/** The engine measures files on disk: offer to save unsaved edits first. */
async function saveOrContinue(): Promise<boolean> {
  const dirty = vscode.workspace.textDocuments.filter((d) => d.isDirty && d.uri.scheme === "file");
  if (!dirty.length) {
    return true;
  }
  const choice = await vscode.window.showWarningMessage(
    `MemBlame measures the files on disk; ${dirty.length} file(s) have unsaved changes.`,
    "Save All and Continue",
    "Continue Without Saving",
  );
  if (choice === "Save All and Continue") {
    return vscode.workspace.saveAll(false);
  }
  return choice === "Continue Without Saving";
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

function explicitSetting<T>(key: string): T | undefined {
  const setting = vscode.workspace.getConfiguration("memblame").inspect<T>(key);
  return setting?.workspaceFolderValue ?? setting?.workspaceValue ?? setting?.globalValue;
}

async function resolvePython(repo: string): Promise<InterpreterChoice> {
  return chooseInterpreters({
    explicit: explicitSetting<string>("pythonPath"),
    repoDefinesPython: await repositoryDefinesKey(repo, "python"),
    selected: await selectedInterpreter(repo),
    fallback: process.platform === "win32" ? "python" : "python3",
  });
}

async function selectedInterpreter(repo: string): Promise<string | undefined> {
  const ext = vscode.extensions.getExtension("ms-python.python");
  if (!ext) {
    return undefined;
  }
  try {
    const api = (ext.isActive ? ext.exports : await ext.activate()) as PythonExtensionApi;
    const environments = api?.environments;
    const envPath = environments?.getActiveEnvironmentPath?.(vscode.Uri.file(repo));
    if (environments && envPath) {
      const env = await environments.resolveEnvironment(envPath);
      return env?.executable?.uri?.fsPath ?? envPath.path;
    }
  } catch {
    // no usable selection: fall through to the CLI's own discovery
  }
  return undefined;
}

async function chooseWorkload(force: boolean): Promise<string | undefined> {
  const configured = explicitSetting<string>("workload");
  const current = configured || state?.workspaceState.get<string>("workload");
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
  if (workload && configured && configured !== workload) {
    vscode.window.showInformationMessage("MemBlame: the memblame.workload setting takes precedence; update it to switch permanently.");
  }
  if (workload) {
    await state?.workspaceState.update("workload", workload);
  }
  return workload;
}

function showReport(ctx: vscode.ExtensionContext, result: MemblameResult) {
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

function summarize(result: MemblameResult) {
  const ups = (result.findings ?? []).filter((f) => f.delta > 0);
  if (result.measurement_status && result.measurement_status !== "complete") {
    const w = (result.warnings ?? []).find((x: string) => /INVALID|SKIPPED|failed/i.test(x)) ?? "see report";
    vscode.window.showErrorMessage(`MemBlame: measurement ${result.measurement_status}. ${w}`);
  } else if (result.kind === "bisect" && result.status === "found") {
    const label = result.verified ? "first verified crossing" : "threshold crossing";
    const culprit = result.culprit;
    vscode.window.showInformationMessage(
      `MemBlame: ${label} ${culprit?.short ?? "unknown"} "${culprit?.subject ?? ""}"`,
    );
  } else if (ups.length) {
    const f = ups[0];
    const where = f.verdict?.qualname ? ` in ${f.verdict.qualname}()` : "";
    vscode.window.showWarningMessage(`MemBlame: ${f.metric} memory ${mb(f.delta, true)}${where} (${f.unit}).`);
  } else if (result.valid === false || result.status === "error") {
    const w = (result.warnings ?? []).find((x: string) => /INVALID|SKIPPED/.test(x)) ?? result.message ?? "see report";
    vscode.window.showErrorMessage(`MemBlame: could not compare. ${w}`);
  } else {
    vscode.window.setStatusBarMessage("MemBlame: no significant memory increase", 8000);
  }
}

// ------------------------------------------------------------------ annotations

async function gitOut(cwd: string, args: string[]): Promise<string> {
  const { execFile } = await import("child_process");
  return new Promise((resolve) => execFile("git", args, { cwd }, (_e, out) => resolve((out ?? "").trim())));
}

async function isDirty(repo: string): Promise<boolean> {
  return (await gitOut(repo, ["status", "--porcelain", "--untracked-files=no"])) !== "";
}

async function applyAnnotations(result: MemblameResult) {
  annotations.clear();
  hotLines.clear();
  const repo: string = result.repo ?? "";
  const head = await gitOut(repo, ["rev-parse", "HEAD"]);
  const findings: Finding[] = result.findings ?? [];
  for (const f of findings) {
    const v = f.verdict;
    if (!v?.file) {
      continue;
    }
    const abs = canon(path.join(repo, v.file));
    const since = f.commit ? ` at ${String(f.commit).slice(0, 7)}` : result.kind === "diff" ? ` vs ${result.base?.short ?? "base"}` : "";
    const title = `$(${f.delta > 0 ? "arrow-up" : "arrow-down"}) ${f.metric} memory ${mb(f.delta, true)}${since} · ${v.kind} · ${f.unit}`;
    const list = annotations.get(abs) ?? [];
    list.push({ line: v.line ?? 1, qualname: v.qualname ?? "<module>", title,
      hover: `MemBlame: ${v.function ?? "unknown"}` });
    annotations.set(abs, list);
    // Line numbers are from the measured commit; only annotate lines if that is what's on disk.
    const lineCommit = f.commit ?? (result.kind === "diff" ? result.head?.sha : result.culprit?.sha);
    const current = lineCommit === "WORKTREE" || (lineCommit === head && !(await isDirty(repo)));
    const where = f.delta > 0 ? (v.hot_lines ?? []) : [];
    for (const hl of current ? [...where, ...(v.allocated_at ?? [])] : []) {
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
      const line = locateScope(doc.getText(), a.qualname, a.line);
      if (line !== undefined) {
        const range = doc.lineAt(line).range;
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
              arguments: [{ file: doc.uri.fsPath, test: t.name }],
            }),
          );
        }
      }
    }
    return lenses;
  }
}
