// Launch a real VS Code with the extension against a generated fixture repo.
//   npm run test:integration   (set VSCODE_EXECUTABLE to use an installed VS Code)
import * as cp from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

async function main() {
  // When launched from a terminal inside VS Code this is set and would start VS Code as plain Node.
  delete process.env.ELECTRON_RUN_AS_NODE;
  const extRoot = path.resolve(__dirname, "..", "..", "..");
  const projectRoot = path.resolve(extRoot, "..");
  const python = process.env.MEMBLAME_TEST_PYTHON ?? path.join(projectRoot, ".venv", "bin", "python");
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "mb-it-"));
  const repo = path.join(work, "repo");
  cp.execFileSync(python, [path.join(projectRoot, "tests", "fixture_repo.py"), repo, "planted"], { stdio: "inherit" });
  fs.mkdirSync(path.join(repo, ".vscode"));
  fs.writeFileSync(
    path.join(repo, ".vscode", "settings.json"),
    JSON.stringify({ "memblame.pythonPath": python, "memblame.workload": "call:shop.app:run", "memblame.runs": 2 }),
  );
  // Make an uncommitted change that grows memory, for the "working tree vs HEAD" command.
  const parse = path.join(repo, "shop", "parse.py");
  fs.writeFileSync(parse, fs.readFileSync(parse, "utf8").replace("bytes(100))", "bytes(100), [0] * 30)"));
  await runTests({
    vscodeExecutablePath: process.env.VSCODE_EXECUTABLE,
    extensionDevelopmentPath: extRoot,
    extensionTestsPath: path.join(__dirname, "suite"),
    launchArgs: [repo, "--disable-extensions", "--skip-welcome", "--skip-release-notes", `--user-data-dir=${path.join(work, "ud")}`],
    extensionTestsEnv: { MEMBLAME_TEST_REPO: repo },
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
