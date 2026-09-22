/**
 * Build the Vite frontend into desktop/renderer/app for packaged Electron.
 * Optional env overrides written into packaged-defaults.json before dist:
 *   CASESIFTER_ANALYSIS_URL, CASESIFTER_CONTROL_URL
 */
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const desktopRoot = path.join(__dirname, "..");
const repoRoot = path.join(desktopRoot, "..");
const frontendRoot = path.join(repoRoot, "frontend");
const outDir = path.join(desktopRoot, "renderer", "app");
const defaultsPath = path.join(desktopRoot, "packaged-defaults.json");

function run(cmd, args, cwd) {
  const r = spawnSync(cmd, args, { cwd, stdio: "inherit", shell: true });
  if (r.status !== 0) process.exit(r.status || 1);
}

if (!fs.existsSync(path.join(frontendRoot, "package.json"))) {
  console.error("frontend/package.json not found");
  process.exit(1);
}

const defaults = {
  analysisApiUrl:
    process.env.CASESIFTER_ANALYSIS_URL || "https://casesifter-analysis.onrender.com",
  controlPlaneUrl:
    process.env.CASESIFTER_CONTROL_URL || "https://casesifter-control.onrender.com",
};
fs.writeFileSync(defaultsPath, JSON.stringify(defaults, null, 2) + "\n");
console.log("Wrote", defaultsPath, defaults);

fs.mkdirSync(outDir, { recursive: true });
run("npm", ["run", "build", "--", "--outDir", outDir, "--base", "./", "--emptyOutDir"], frontendRoot);

if (!fs.existsSync(path.join(outDir, "index.html"))) {
  console.error("UI build missing index.html at", outDir);
  process.exit(1);
}
console.log("UI ready at", outDir);
