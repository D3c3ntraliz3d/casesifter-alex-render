/**
 * Write public/build-info.json from Render's RENDER_GIT_COMMIT (or VITE_GIT_COMMIT).
 * Served at /build-info.json so ops can confirm which SHA the static app was built from.
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "public", "build-info.json");
const full = String(process.env.RENDER_GIT_COMMIT || process.env.VITE_GIT_COMMIT || "").trim();

const payload = {
  status: "ok",
  service: "casesifter-app",
  git_commit_source: full ? (process.env.RENDER_GIT_COMMIT ? "RENDER_GIT_COMMIT" : "VITE_GIT_COMMIT") : null,
  git_commit: full || null,
  git_commit_short: full ? full.slice(0, 12) : null,
  built_at: new Date().toISOString(),
};

mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
console.log(`wrote ${out} git_commit=${payload.git_commit_short || "(none)"}`);
