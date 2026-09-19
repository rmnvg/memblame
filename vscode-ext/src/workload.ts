// Suggest memblame workloads from the file the user is looking at. No vscode imports.

export interface WorkloadSuggestion {
  label: string;
  workload: string;
  detail: string;
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
        workload: `pytest:${relPath}::${current.name}`,
        detail: "the test at the cursor",
      });
    }
    out.push({ label: `$(beaker) ${relPath}`, workload: `pytest:${relPath}`, detail: "every test in this file, measured separately" });
    return out;
  }
  if (/^\s*def main\s*\(/m.test(text)) {
    out.push({
      label: `$(symbol-function) ${moduleName(relPath)}:main`,
      workload: `call:${moduleName(relPath)}:main`,
      detail: "call main() from this module",
    });
  }
  out.push({ label: `$(file-code) ${relPath}`, workload: `script:${relPath}`, detail: "run this file as a script" });
  return out;
}
