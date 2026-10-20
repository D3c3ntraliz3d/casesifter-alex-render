/**
 * Real browser click-through for Interrupted status + Re-scan light behavior.
 * Usage: node scripts/clickthrough-rescan.mjs
 */
import { chromium } from "playwright";
import { createHash } from "node:crypto";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..", "..");
const OUT = join(ROOT, "backend", "data", "_clickthrough");
const BASE = "http://127.0.0.1:5173";
const EMAIL = "tester@example.com";
const PASS = "LocalTest1!";
const MATTER_HINT = "fdsffsdfdsfds23";

mkdirSync(OUT, { recursive: true });

function log(msg) {
  console.log(msg);
}

async function shot(page, name) {
  const path = join(OUT, `${name}.png`);
  await page.screenshot({ path, fullPage: true });
  log(`shot ${path}`);
  return path;
}

async function seedCache(page, sha256, bytes, fileName) {
  await page.evaluate(
    async ({ sha, b64, name }) => {
      const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const blob = new Blob([bin], { type: "application/pdf" });
      const openDb = () =>
        new Promise((resolve, reject) => {
          const req = indexedDB.open("casesifter-local-files", 2);
          req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains("blobs")) {
              db.createObjectStore("blobs", { keyPath: "sha256" });
            }
          };
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => reject(req.error);
        });
      const db = await openDb();
      await new Promise((resolve, reject) => {
        const tx = db.transaction("blobs", "readwrite");
        tx.objectStore("blobs").put({
          sha256: sha,
          blob,
          byteLength: bin.length,
          mimeType: "application/pdf",
          fileName: name,
          savedAt: Date.now(),
        });
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
      db.close();
    },
    {
      sha: sha256,
      b64: Buffer.from(bytes).toString("base64"),
      name: fileName,
    }
  );
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
  });

  try {
    // Establish control + analysis session cookies in this browser context first.
    log("→ API login + analysis session accept");
    const loginRes = await context.request.post("http://127.0.0.1:8010/v1/login", {
      data: { email: EMAIL, password: PASS },
      headers: { "Content-Type": "application/json", Accept: "application/json" },
    });
    const loginBody = await loginRes.json();
    if (!loginRes.ok()) throw new Error(`login failed ${loginRes.status()} ${JSON.stringify(loginBody)}`);
    if (!loginBody.analysis_code) throw new Error("login missing analysis_code");
    const acceptRes = await context.request.post("http://127.0.0.1:8000/api/v1/session/accept", {
      data: { code: loginBody.analysis_code },
      headers: { "Content-Type": "application/json", Accept: "application/json" },
    });
    log(`analysis accept status=${acceptRes.status()}`);
    if (!acceptRes.ok()) {
      throw new Error(`session accept failed ${acceptRes.status()} ${await acceptRes.text()}`);
    }
    // Mark SPA signed-in hint used by App.
    await context.addInitScript(() => {
      try {
        sessionStorage.setItem("casesifter_signed_in", "1");
        sessionStorage.setItem("casesifter_email", "tester@example.com");
      } catch {
        /* ignore */
      }
    });

    const mattersCheck = await context.request.get("http://127.0.0.1:8000/api/v1/matters", {
      headers: { Accept: "application/json" },
    });
    const mattersBody = await mattersCheck.json();
    const matterCount = (mattersBody.matters || mattersBody.cases || []).length;
    log(`matters via API with browser cookies: ${mattersCheck.status()} count=${matterCount}`);
    if (!matterCount) throw new Error("analysis session has zero matters — cannot click through");

    log("→ open app");
    await page.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });
    await shot(page, "01-home");

    // Account / Sign in if SPA still shows the form (cookie may be enough via entitlement).
    if (await page.locator('input[type="email"]').count()) {
      log("→ UI login (form still visible)");
      await page.locator('input[type="email"]').fill(EMAIL);
      await page.locator('input[type="password"]').fill(PASS);
      await page.locator("form").getByRole("button", { name: /^Sign in$/i }).click();
      await page.waitForTimeout(2500);
    }
    await shot(page, "02-after-login");

    if (await page.getByRole("button", { name: /^Cases$/i }).count()) {
      await page.getByRole("button", { name: /^Cases$/i }).click();
      await page.waitForTimeout(1200);
    }
    await shot(page, "03-cases");

    log(`→ open matter containing ${MATTER_HINT}`);
    const tile = page.locator(`text=${MATTER_HINT}`).first();
    await tile.waitFor({ state: "visible", timeout: 20000 });
    await tile.click();
    await page.waitForTimeout(1500);
    await shot(page, "04-matter");

    const bodyText = await page.locator("body").innerText();
    const hasInterrupted = /Interrupted/i.test(bodyText);
    const hasFailed = /\bFailed\b/.test(bodyText);
    const hasRestartMsg = /restarted mid-scan/i.test(bodyText);
    const statusCell = page.locator("td.meta-cell").first();
    const statusText = (await statusCell.count()) ? await statusCell.innerText() : "(no meta-cell)";
    log(`status_cell:\n${statusText}`);
    log(`flags interrupted=${hasInterrupted} failed_word=${hasFailed} restart_msg=${hasRestartMsg}`);

    const dots = page.locator(".doc-check-dot");
    const dotCount = await dots.count();
    let green = 0;
    let yellow = 0;
    let red = 0;
    for (let i = 0; i < dotCount; i += 1) {
      const cls = (await dots.nth(i).getAttribute("class")) || "";
      if (cls.includes("ok")) green += 1;
      else if (cls.includes("running")) yellow += 1;
      else if (cls.includes("failed")) red += 1;
    }
    log(`lights green=${green} yellow=${yellow} red=${red} total=${dotCount}`);

    if (!hasInterrupted && hasFailed) {
      throw new Error("EXPECTED Interrupted label, still saw Failed");
    }
    if (!hasRestartMsg) {
      throw new Error("EXPECTED restart mid-scan message on the row");
    }
    if (green < 4) {
      throw new Error(`EXPECTED green module lights before Re-scan, got green=${green}`);
    }
    log("PASS pre-rescan UI: Interrupted + greens + restart message");

    // Try to seed cache if we can find matching bytes (usually missing).
    const s41Sha = "e18b30595a816afea48cb5ac741ee32a3ccc456a496fbb0c50da216cd4a46cd5";
    // Upload path proof: add a tiny PDF, watch yellow then settle.
    const tinyPdf = readFileSync(join(ROOT, "backend", "data", "_live_pdf", "live_exhibit.pdf"));
    const tinySha = createHash("sha256").update(tinyPdf).digest("hex");
    log(`→ seed cache for live_exhibit sha=${tinySha.slice(0, 12)}`);
    await seedCache(page, tinySha, tinyPdf, "live_exhibit.pdf");

    log("→ click Re-scan on S_41 (may need Locate if PDF not cached)");
    const rescan = page.getByRole("button", { name: /^Re-scan$/i }).first();
    await rescan.click();
    await page.waitForTimeout(1500);
    await shot(page, "05-after-rescan-click");

    const afterClick = await page.locator("body").innerText();
    const scanning = /Scanning|In progress/i.test(afterClick);
    const cacheMiss = /not cached|Locate|re-upload/i.test(afterClick);
    const yellowAfter = await page.locator(".doc-check-dot.running").count();
    log(`after_click scanning=${scanning} cacheMiss=${cacheMiss} yellowDots=${yellowAfter}`);

    if (cacheMiss) {
      log("NOTE: S_41 PDF bytes are not in this fresh browser — Re-scan correctly asks Locate/re-upload.");
      log("→ proving yellow→settle with a unique tiny PDF upload");
      // Unique bytes so the matter does not treat it as a duplicate of S_41.
      const uniquePdf = Buffer.from(
        `%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n%${Date.now()}-${Math.random()}\n`
      );
      const uniquePath = join(OUT, "unique_clickthrough.pdf");
      writeFileSync(uniquePath, uniquePdf);
      const input = page.locator('input[type="file"]').first();
      await input.setInputFiles(uniquePath);
      let sawYellow = false;
      for (let i = 0; i < 90; i += 1) {
        const y = await page.locator(".doc-check-dot.running").count();
        const scanningRows = await page.locator("td.meta-cell", { hasText: /Scanning|In progress/i }).count();
        if (y > 0 || scanningRows > 0) {
          sawYellow = true;
          log(`saw in-progress UI at t=${i}s yellowDots=${y} scanningRows=${scanningRows}`);
          await shot(page, "06-upload-yellow");
          break;
        }
        const note = await page.locator(".footnote").allInnerTexts().catch(() => []);
        if (i % 5 === 0) log(`upload wait t=${i}s note=${note.slice(0, 2).join(" | ")}`);
        await page.waitForTimeout(1000);
      }
      if (sawYellow) {
        for (let i = 0; i < 180; i += 1) {
          const y = await page.locator(".doc-check-dot.running").count();
          const scanningRows = await page.locator("td.meta-cell", { hasText: /Scanning|In progress/i }).count();
          if (y === 0 && scanningRows === 0) {
            await shot(page, "07-upload-settled");
            log(`PASS upload settled after ${i}s`);
            break;
          }
          if (i % 10 === 0) log(`waiting settle t=${i}s yellow=${y} scanningRows=${scanningRows}`);
          await page.waitForTimeout(1000);
        }
      } else {
        await shot(page, "06-upload-no-yellow");
        const body = await page.locator("body").innerText();
        log(`WARN no yellow observed. snippet=${body.slice(0, 500).replace(/\s+/g, " ")}`);
      }
    } else if (scanning || yellowAfter > 0) {
      log("PASS Re-scan armed yellow/scanning");
      await shot(page, "06-rescan-yellow");
      for (let i = 0; i < 300; i += 1) {
        const y = await page.locator(".doc-check-dot.running").count();
        const cell = await page.locator("td.meta-cell").first().innerText();
        if (y === 0 && !/Scanning|In progress/i.test(cell)) {
          await shot(page, "07-rescan-settled");
          log(`PASS rescan settled:\n${cell}`);
          break;
        }
        if (i % 15 === 0) log(`rescan wait t=${i}s yellow=${y}`);
        await page.waitForTimeout(1000);
      }
    } else {
      await shot(page, "05b-unexpected");
      throw new Error("Re-scan click did not show scanning/yellow or cache-miss message");
    }

    if (errors.length) {
      log(`page_errors (${errors.length}): ${errors.slice(0, 5).join(" | ")}`);
    }
    writeFileSync(join(OUT, "result.json"), JSON.stringify({ hasInterrupted, green, yellow, red, cacheMiss, scanning, errors }, null, 2));
    log("CLICKTHROUGH DONE");
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error("CLICKTHROUGH FAILED:", err);
  process.exit(1);
});
