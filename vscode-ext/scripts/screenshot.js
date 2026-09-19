// Render report JSON with VS Code "Dark Modern" theme variables and screenshot it (for docs).
//   node scripts/screenshot.js <report.json> <out.png> [width] [height] [clickPoint]
const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");
const { renderHtml } = require("../out/src/render");

const [input, output, width = "900", height = "900", point] = process.argv.slice(2);
const theme = {
  "font-family": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif", "font-size": "13px",
  foreground: "#cccccc", "editor-background": "#1f1f1f", descriptionForeground: "#9d9d9d",
  "textLink-foreground": "#4daafc", "panel-border": "#2b2b2b", "editorWidget-background": "#252526",
  "badge-background": "#616161", "badge-foreground": "#f8f8f8", "charts-red": "#f14c4c",
  "charts-green": "#89d185", "charts-blue": "#3794ff", "charts-purple": "#b180d7",
  "charts-orange": "#d18616", "editor-font-family": "Menlo, Monaco, monospace",
  "list-hoverBackground": "#2a2d2e", "inputValidation-warningBorder": "#b89500",
  "inputValidation-warningBackground": "#352a05", "dropdown-background": "#313131",
  "dropdown-foreground": "#cccccc", "dropdown-border": "#3c3c3c",
};
const vars = Object.entries(theme).map(([k, v]) => `--vscode-${k}: ${v};`).join(" ");
let html = renderHtml(JSON.parse(fs.readFileSync(input, "utf8")), "doc", "'self'")
  .replace(/<meta http-equiv="Content-Security-Policy"[^>]*>/, "")
  .replace("<style>", `<style>:root { ${vars} }`)
  .replace("const vscode = acquireVsCodeApi();", "const vscode = { postMessage() {} };");
if (point) {
  html = html.replace("</body>", `<script>document.querySelector('.hit[data-point="${point}"]').dispatchEvent(new MouseEvent('click', {bubbles: true}));</script></body>`);
}
const tmp = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "mb-shot-")), "report.html");
fs.writeFileSync(tmp, html);
const chrome = process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
cp.execFileSync(chrome, ["--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
  `--window-size=${width},${height}`, `--screenshot=${path.resolve(output)}`, `file://${tmp}`], { stdio: "ignore" });
console.log(`wrote ${output}`);
