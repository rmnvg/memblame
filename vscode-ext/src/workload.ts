// Suggest memblame workloads from the file the user is looking at. No vscode imports.

export interface WorkloadSuggestion {
  label: string;
  workload: string;
  detail: string;
}

/** Whether a TOML file defines `key` in MemBlame's supported table. */
export function tomlDefinesKey(text: string, pyproject: boolean, key: string): boolean {
  let inTable = !pyproject;
  const assignment = new RegExp(`^\\s*(?:${key}|"${key}"|'${key}')\\s*=`);
  for (const line of text.split(/\r?\n/)) {
    const header = /^\s*\[\s*([^\]]+)\s*\]/.exec(line);
    if (header) {
      inTable = pyproject && header[1].trim() === "tool.memblame";
      continue;
    }
    if (inTable && assignment.test(line)) {
      return true;
    }
  }
  return false;
}

/** Whether a TOML file defines the repository workload in MemBlame's supported table. */
export function tomlDefinesWorkload(text: string, pyproject: boolean): boolean {
  return tomlDefinesKey(text, pyproject, "workload");
}

/** One argument for the engine's workload parser, including spaces and literal quotes. */
export function quoteWorkloadArg(value: string): string {
  if (/^[\w@%+=:,./-]+$/.test(value)) {
    return value;
  }
  return "'" + value.replace(/'/g, "'\"'\"'") + "'";
}

export function pytestWorkload(file: string, test?: string): string {
  return `pytest:${quoteWorkloadArg(test ? `${file}::${test}` : file)}`;
}

export function isTestFile(relPath: string): boolean {
  const base = relPath.split("/").pop() ?? "";
  return /^test_.*\.py$/.test(base) || /_test\.py$/.test(base);
}

/** Module name for a repo-relative path, e.g. src/pkg/mod.py -> pkg.mod */
export function moduleName(relPath: string): string {
  let p = relPath.replace(/\.py$/, "").replace(/\/__init__$/, "");
  p = p.replace(/^src\//, "");
  return p.split("/").join(".");
}

export interface TestFunction {
  name: string; // pytest node suffix, e.g. "TestX::test_y" or "test_y"
  line: number; // 0-based line of the def
}

/** All pytest-style test functions (including methods of Test* classes) in a file. */
export function findTests(text: string): TestFunction[] {
  const out: TestFunction[] = [];
  const lines = text.split(/\r?\n/);
  let cls: { name: string; indent: number } | null = null;
  lines.forEach((line, i) => {
    const m = /^(\s*)(?:async\s+)?(def|class)\s+(\w+)/.exec(line);
    if (!m) {
      return;
    }
    const indent = m[1].length;
    if (cls && indent <= cls.indent) {
      cls = null;
    }
    if (m[2] === "class") {
      cls = m[3].startsWith("Test") ? { name: m[3], indent } : null;
      return;
    }
    if (!m[3].startsWith("test")) {
      return;
    }
    if (indent === 0) {
      out.push({ name: m[3], line: i });
    } else if (cls && indent > cls.indent) {
      out.push({ name: `${cls.name}::${m[3]}`, line: i });
    }
  });
  return out;
}

export function suggestWorkloads(relPath: string, text: string, cursorLine: number): WorkloadSuggestion[] {
  const out: WorkloadSuggestion[] = [];
  if (!relPath.endsWith(".py")) {
    return out;
  }
  if (isTestFile(relPath)) {
    const tests = findTests(text).filter((t) => t.line <= cursorLine);
    const current = tests[tests.length - 1];
    if (current) {
      out.push({
        label: `$(beaker) ${current.name}`,
        workload: pytestWorkload(relPath, current.name),
        detail: "the test at the cursor",
      });
    }
    out.push({ label: `$(beaker) ${relPath}`, workload: pytestWorkload(relPath), detail: "every test in this file, measured separately" });
    return out;
  }
  if (/^\s*def main\s*\(/m.test(text)) {
    out.push({
      label: `$(symbol-function) ${moduleName(relPath)}:main`,
      workload: `call:${moduleName(relPath)}:main`,
      detail: "call main() from this module",
    });
  }
  out.push({ label: `$(file-code) ${relPath}`, workload: `script:${quoteWorkloadArg(relPath)}`, detail: "run this file as a script" });
  return out;
}

/**
 * 0-based line of `def`/`class` for a qualname like "Loader.load" or "Outer.<locals>.inner" in
 * the current text; `hint` (1-based line from the measured commit) breaks ties. "<module>"
 * maps to the first line.
 */
export function locateScope(text: string, qualname: string, hint: number): number | undefined {
  if (qualname === "<module>") {
    return 0;
  }
  const name = qualname.split(".").pop() ?? qualname;
  const re = new RegExp(`^\\s*(?:async\\s+)?(?:def|class)\\s+${name.replace(/[^\w]/g, "")}\\b`);
  const hits: number[] = [];
  text.split(/\r?\n/).forEach((line, i) => {
    if (re.test(line)) {
      hits.push(i);
    }
  });
  if (!hits.length) {
    return undefined;
  }
  return hits.reduce((best, i) => (Math.abs(i + 1 - hint) < Math.abs(best + 1 - hint) ? i : best));
}
