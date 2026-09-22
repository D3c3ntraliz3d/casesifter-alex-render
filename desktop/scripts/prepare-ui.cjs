/**
 * Build the Vite frontend into desktop/renderer/app for packaged Electron.
 * Optional env overrides written into packaged-defaults.json before dist:
 *   CASESIFTER_ANALYSIS_URL, CASESIFTER_CONTROL_URL
 *   VITE_SENTRY_DSN / CASESIFTER_SENTRY_DSN (baked into UI + Electron main)
 *   VITE_SENTRY_ENVIRONMENT / CASESIFTER_SENTRY_ENV
 */
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const desktopRoot = path.join(__dirname, "..");
const repoRoot = path.join(desktopRoot, "..");
const frontendRoot = path.join(repoRoot, "frontend");
const outDir = path.join(desktopRoot, "renderer", "app");
const defaultsPath = path.join(desktopRoot, "packaged-defaults.json");

function run(cmd, args, cwd, env) {
  const r = spawnSync(cmd, args, { cwd, stdio: "inherit", shell: true, env });
  if (r.status !== 0) process.exit(r.status || 1);
}

if (!fs.existsSync(path.join(frontendRoot, "package.json"))) {
  console.error("frontend/package.json not found");
  process.exit(1);
}

const sentryDsn = (
  process.env.VITE_SENTRY_DSN ||
  process.env.CASESIFTER_SENTRY_DSN ||
  process.env.SENTRY_DSN ||
  ""
).trim();
const sentryEnv = (
  process.env.VITE_SENTRY_ENVIRONMENT ||
  process.env.CASESIFTER_SENTRY_ENV ||
  "production"
).trim();

const defaults = {
  analysisApiUrl:
    process.env.CASESIFTER_ANALYSIS_URL || "https://casesifter-analysis.onrender.com",
  controlPlaneUrl:
    process.env.CASESIFTER_CONTROL_URL || "https://casesifter-control.onrender.com",
};
if (sentryDsn) {
  defaults.sentryDsn = sentryDsn;
  defaults.sentryEnvironment = sentryEnv;
}
fs.writeFileSync(defaultsPath, JSON.stringify(defaults, null, 2) + "\n");
console.log(
  "Wrote",
  defaultsPath,
  {
    ...defaults,
    sentryDsn: defaults.sentryDsn ? "[set]" : undefined,
  }
);

const buildEnv = {
  ...process.env,
  VITE_SENTRY_DSN: sentryDsn,
  VITE_SENTRY_ENVIRONMENT: sentryEnv,
};

fs.mkdirSync(outDir, { recursive: true });
run(
  "npm",
  ["run", "build", "--", "--outDir", outDir, "--base", "./", "--emptyOutDir"],
  frontendRoot,
  buildEnv
);

if (!fs.existsSync(path.join(outDir, "index.html"))) {
  console.error("UI build missing index.html at", outDir);
  process.exit(1);
}
if (sentryDsn) {
  console.log("Packaged UI built with VITE_SENTRY_DSN (service=casesifter-ui)");
} else {
  console.log("Packaged UI built without Sentry DSN (set VITE_SENTRY_DSN at pack time to enable)");
}
console.log("UI ready at", outDir);
