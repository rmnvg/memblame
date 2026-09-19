// Run the bundled memblame engine and parse its JSON. No vscode imports (unit-testable).
import * as cp from "child_process";
import * as path from "path";

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
  result: Promise<any>;
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

export function buildArgs(common: {
  workload: string;
  runs?: number;
  nframe?: number;
  importPaths?: string[];
  python: string;
  repo: string;
}): string[] {
  const args = ["-C", common.repo, "-w", common.workload, "--python", common.python, "--json"];
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
  const result = new Promise<any>((resolve, reject) => {
    child.on("error", (err) =>
      reject(new Error(`could not start ${req.python}: ${err.message}. Set memblame.pythonPath or select an interpreter.`)),
    );
    child.on("close", (code) => {
      if (cancelled) {
        reject(new Error("cancelled"));
        return;
      }
      let parsed: any;
      try {
        parsed = JSON.parse(stdout);
      } catch {
        const tail = stderr.split(/\r?\n/).filter(Boolean).slice(-8).join("\n");
        reject(new Error(`memblame exited with code ${code}:\n${tail || stdout.slice(-2000)}`));
        return;
      }
      if (parsed.kind === "error") {
        reject(new Error(parsed.error));
      } else {
        resolve(parsed);
      }
    });
  });
  return {
    result,
    cancel: () => {
      cancelled = true;
      child.kill("SIGTERM"); // the CLI turns SIGTERM into a clean exit (worktree removal)
    },
  };
}
