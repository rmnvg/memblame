// Run the bundled memblame engine and parse its JSON. No vscode imports (unit-testable).
import * as cp from "child_process";
import * as path from "path";
import { MemblameResult, parseEngineResponse } from "./contract";

export interface Progress {
  message: string;
  step?: [number, number];
}

export interface RunRequest {
  python: string;
  repo: string;
  args: string[]; // e.g. ["diff", "HEAD"]
  bundledPath: string; // directory containing the memblame package
  onProgress?: (p: Progress) => void;
}

export interface Running {
  result: Promise<MemblameResult>;
  cancel: () => void;
}

/** "memblame: [3/10] measuring abc1234 'msg'" -> {message, step: [3, 10]} */
export function parseProgress(line: string): Progress | undefined {
  const text = line.replace(/^memblame:\s*/, "").trim();
  if (!text || !line.startsWith("memblame:")) {
    return undefined;
  }
  const m = /^\[(\d+)\/(\d+)\]\s*(.*)$/.exec(text);
  return m ? { message: m[3], step: [Number(m[1]), Number(m[2])] } : { message: text };
}

export interface InterpreterChoice {
  /** Runs the bundled memblame engine (needs Python 3.9+). */
  launcher: string;
  /** Passed as --python: the interpreter that runs *your* code. Undefined = let the CLI decide. */
  project?: string;
}

/**
 * Interpreter precedence, highest first:
 *   1. the explicit `memblame.pythonPath` setting
 *   2. `python` in the repository's memblame.toml / [tool.memblame]  (the CLI applies it)
 *   3. the interpreter selected in the Python extension
 *   4. the CLI's auto-discovery: active venv/conda env, then .venv or venv in the repository,
 *      then one beside the workload (backend/.venv for tests under backend/)
 *   5. python3 / python
 * The CLI's own --python flag beats repository config, so --python is only passed when
 * nothing in the repository should win (case 1 and 3).
 */
export function chooseInterpreters(input: {
  explicit?: string;
  repoDefinesPython: boolean;
  selected?: string;
  fallback: string;
}): InterpreterChoice {
  if (input.explicit) {
    return { launcher: input.explicit, project: input.explicit };
  }
  const launcher = input.selected ?? input.fallback;
  if (input.repoDefinesPython || !input.selected) {
    return { launcher };
  }
  return { launcher, project: input.selected };
}

/**
 * The `environments` half of the Python extension's API, structurally typed so this module
 * stays free of `vscode` imports and the glue below can be tested without an editor.
 */
export interface PythonEnvironmentsApi {
  getActiveEnvironmentPath?(resource: unknown): { path: string } | undefined;
  resolveEnvironment?(path: { path: string }): Promise<{ executable?: { uri?: { fsPath?: string } } } | undefined>;
}

/**
 * The interpreter the Python extension has selected, or undefined when there is none.
 *
 * Every field here is optional in practice: the API has changed shape before, an
 * environment may not be resolvable, and `resolveEnvironment` can reject. Anything
 * unusable means "no selection", which leaves the CLI's own discovery in charge rather
 * than failing the command.
 */
export async function selectedFromPythonApi(
  environments: PythonEnvironmentsApi | undefined,
  resource: unknown,
): Promise<string | undefined> {
  try {
    const envPath = environments?.getActiveEnvironmentPath?.(resource);
    if (!envPath) {
      return undefined;
    }
    const env = await environments?.resolveEnvironment?.(envPath);
    return env?.executable?.uri?.fsPath ?? envPath.path ?? undefined;
  } catch {
    return undefined;
  }
}

export function buildArgs(common: {
  workload?: string;
  runs?: number;
  nframe?: number;
  importPaths?: string[];
  python?: string;
  repo: string;
}): string[] {
  const args = ["-C", common.repo];
  if (common.workload) {
    args.push("-w", common.workload);
  }
  if (common.python) {
    args.push("--python", common.python);
  }
  args.push("--json");
  if (common.runs) {
    args.push("--runs", String(common.runs));
  }
  if (common.nframe) {
    args.push("--nframe", String(common.nframe));
  }
  for (const p of common.importPaths ?? []) {
    args.push("--pythonpath", p);
  }
  return args;
}

export function runMemblame(req: RunRequest): Running {
  const env = { ...process.env };
  env.PYTHONPATH = [req.bundledPath, env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  env.PYTHONIOENCODING = "utf-8";
  const child = cp.spawn(req.python, ["-m", "memblame", ...req.args], { cwd: req.repo, env });
  let stdout = "";
  let stderr = "";
  let pending = "";
  let cancelled = false;
  child.stdout.on("data", (d: Buffer) => (stdout += d.toString("utf8")));
  child.stderr.on("data", (d: Buffer) => {
    const text = d.toString("utf8");
    stderr += text;
    pending += text;
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() ?? "";
    for (const line of lines) {
      const p = parseProgress(line);
      if (p) {
        req.onProgress?.(p);
      }
    }
  });
  const result = new Promise<MemblameResult>((resolve, reject) => {
    child.on("error", (err) =>
      reject(new Error(`could not start ${req.python}: ${err.message}. Set memblame.pythonPath or select an interpreter.`)),
    );
    child.on("close", (code) => {
      if (cancelled) {
        reject(new Error("cancelled"));
        return;
      }
      let parsed;
      try {
        parsed = parseEngineResponse(JSON.parse(stdout) as unknown);
      } catch (err: unknown) {
        const tail = stderr.split(/\r?\n/).filter(Boolean).slice(-8).join("\n");
        const detail = err instanceof Error ? err.message : String(err);
        reject(new Error(`memblame exited with code ${code}: ${detail}\n${tail || stdout.slice(-2000)}`));
        return;
      }
      if (parsed.kind === "error") {
        reject(new Error(parsed.error));
      } else if (code !== 0 && code !== 3) {
        const details = [parsed.message, ...(parsed.warnings ?? [])].filter(Boolean).join("\n");
        reject(new Error(`memblame could not complete the measurement (exit ${code}):\n${details || stderr.slice(-2000)}`));
      } else {
        resolve(parsed);
      }
    });
  });
  return {
    result,
    cancel: () => {
      cancelled = true;
      if (process.platform === "win32" && child.pid) {
        cp.spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
      } else {
        child.kill("SIGTERM"); // the CLI cleans its runner process group and worktree
      }
    },
  };
}
