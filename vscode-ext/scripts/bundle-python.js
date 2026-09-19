// Copy the stdlib-only Python engine into the extension so users need no pip install.
const fs = require("fs");
const path = require("path");
const src = path.resolve(__dirname, "..", "..", "src", "memblame");
const dest = path.resolve(__dirname, "..", "python", "memblame");
fs.rmSync(dest, { recursive: true, force: true });
fs.mkdirSync(dest, { recursive: true });
for (const f of fs.readdirSync(src)) {
  if (f.endsWith(".py")) fs.copyFileSync(path.join(src, f), path.join(dest, f));
}
console.log(`bundled ${fs.readdirSync(dest).length} files into ${dest}`);
