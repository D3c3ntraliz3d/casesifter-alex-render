/**
 * Windows NSIS build with optional Authenticode signing.
 *
 * Signing modes (first match wins):
 *   1. Azure Trusted Signing — AZURE_TRUSTED_SIGNING_ENDPOINT + account/profile
 *      + AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET
 *   2. PFX via electron-builder — CSC_LINK + CSC_KEY_PASSWORD
 *   3. Unsigned (dev / until a cert is purchased) — prints a loud warning
 *
 * Customer-facing downloads must use (1) or (2). Unsigned installers trip
 * SmartScreen / Safe Browsing and are not sellable.
 */
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const pkgPath = path.join(root, "package.json");
const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));

const azureEndpoint = (process.env.AZURE_TRUSTED_SIGNING_ENDPOINT || "").trim();
const azureAccount = (process.env.AZURE_TRUSTED_SIGNING_ACCOUNT || "").trim();
const azureProfile = (process.env.AZURE_TRUSTED_SIGNING_PROFILE || "").trim();
const hasAzure =
  Boolean(azureEndpoint && azureAccount && azureProfile) &&
  Boolean(process.env.AZURE_TENANT_ID && process.env.AZURE_CLIENT_ID && process.env.AZURE_CLIENT_SECRET);
const hasPfx = Boolean((process.env.CSC_LINK || "").trim() && (process.env.CSC_KEY_PASSWORD || "").trim());

const build = { ...pkg.build, win: { ...(pkg.build.win || {}) } };

if (hasAzure) {
  console.log("Signing mode: Azure Trusted Signing");
  build.win.azureSignOptions = {
    endpoint: azureEndpoint,
    codeSigningAccountName: azureAccount,
    certificateProfileName: azureProfile,
  };
} else if (hasPfx) {
  console.log("Signing mode: Authenticode PFX (CSC_LINK)");
  delete build.win.azureSignOptions;
} else {
  console.warn("");
  console.warn("╔══════════════════════════════════════════════════════════════════╗");
  console.warn("║  UNSIGNED Windows build — not for customer download pages.      ║");
  console.warn("║  Buy OV code signing or Azure Trusted Signing, then set secrets ║");
  console.warn("║  (see desktop/README.md → Windows SmartScreen).                 ║");
  console.warn("╚══════════════════════════════════════════════════════════════════╝");
  console.warn("");
  delete build.win.azureSignOptions;
}

const cfgPath = path.join(root, ".electron-builder.runtime.json");
fs.writeFileSync(cfgPath, JSON.stringify(build, null, 2));

const prep = spawnSync("npm", ["run", "prepare-ui"], { cwd: root, stdio: "inherit", shell: true });
if (prep.status !== 0) process.exit(prep.status || 1);

const dist = spawnSync(
  "npx",
  ["electron-builder", "--win", "nsis", "--config", cfgPath],
  { cwd: root, stdio: "inherit", shell: true, env: process.env },
);
try {
  fs.unlinkSync(cfgPath);
} catch {
  /* ignore */
}
process.exit(dist.status || 0);
