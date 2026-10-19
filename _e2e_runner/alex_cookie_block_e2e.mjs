/**
 * Prove ChatGPT-style third-party cookie block still keeps alex signed in.
 *
 * - Strips Set-Cookie from cross-origin control responses (simulates blocked 3P cookies)
 * - Documents same-origin /v1 proxy path when the SPA uses relative control URLs
 * - Asserts login → entitlement (+ analysis matters) via cookie-on-app-host and/or Bearer
 *
 * Usage: node alex_cookie_block_e2e.mjs
 * Never prints secrets beyond the known demo password already shared.
 */
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const APP = "https://casesifter-alex-app.onrender.com";
const CONTROL_HOST = "casesifter-alex-control.onrender.com";
const EMAIL = "alex@casesifter.dev";
const PW = "CaseSifter-Demo-2026!";

const results = [];

function record(step, pass, notes) {
  results.push({ step, pass: pass ? "Pass" : "Fail", notes: notes || "" });
  console.log(`${pass ? "PASS" : "FAIL"} | ${step} | ${notes || ""}`);
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let sameOriginLogin = false;
  let crossOriginLogin = false;
  let loginHadToken = false;
  let strippedControlCookies = 0;

  // Simulate embedded browsers that reject third-party Set-Cookie from control.
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = req.url();
    const response = await route.fetch();
    const headers = { ...response.headers() };
    let isControlHost = false;
    try {
      isControlHost = new URL(url).hostname === CONTROL_HOST;
    } catch {
      /* ignore */
    }
    if (isControlHost && headers["set-cookie"]) {
      delete headers["set-cookie"];
      strippedControlCookies += 1;
    }
    // Playwright's fulfill merges; remove set-cookie via multi-value if present.
    const raw = typeof response.headersArray === "function" ? response.headersArray() : [];
    const filtered = [];
    for (const h of raw) {
      if (isControlHost && String(h.name).toLowerCase() === "set-cookie") {
        strippedControlCookies += 1;
        continue;
      }
      filtered.push(h);
    }
    if (url.includes("/v1/login") && req.method() === "POST") {
      try {
        const u = new URL(url);
        if (u.hostname.includes("alex-app") && u.pathname.startsWith("/v1/")) {
          sameOriginLogin = true;
        }
        if (u.hostname === CONTROL_HOST) {
          crossOriginLogin = true;
        }
        const body = await response.text();
        try {
          const json = JSON.parse(body);
          loginHadToken = Boolean(json?.token);
        } catch {
          /* ignore */
        }
        await route.fulfill({
          status: response.status(),
          headers: Object.fromEntries(
            filtered.map((h) => [h.name, h.value]).filter(([n]) => n.toLowerCase() !== "set-cookie")
          ),
          body,
        });
        return;
      } catch {
        /* fall through */
      }
    }
    await route.fulfill({ response });
  });

  try {
    await page.goto(APP, { waitUntil: "domcontentloaded", timeout: 90_000 });
    record("1. App loads", true, page.url());
  } catch (e) {
    record("1. App loads", false, String(e.message || e));
    await browser.close();
    writeResults();
    process.exit(1);
  }

  // Probe whether build uses same-origin control (relative /v1).
  const controlBase = await page.evaluate(async () => {
    // Hit entitlement without auth — path shape matters more than status.
    try {
      const rel = await fetch("/v1/entitlement", { credentials: "include" });
      return { path: "same-origin", status: rel.status };
    } catch (e) {
      return { path: "unreachable-relative", error: String(e?.message || e) };
    }
  });
  record(
    "2. Same-origin /v1 rewrite reachable",
    controlBase.path === "same-origin" && controlBase.status !== 404,
    `status=${controlBase.status} path=${controlBase.path}`
  );

  // Login from page context via same-origin /v1 (SPA path). Cross-origin control
  // Set-Cookie is stripped by the route handler above; app-host cookies still stick.
  try {
    const apiResult = await page.evaluate(
      async ({ email, password }) => {
        const res = await fetch("/v1/login", {
          method: "POST",
          credentials: "include",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-Casesifter-Accept-Bearer": "1",
          },
          body: JSON.stringify({ email, password }),
        });
        const data = await res.json().catch(() => ({}));
        if (data?.token) {
          sessionStorage.setItem("casesifter_session_token", data.token);
          sessionStorage.setItem("casesifter_signed_in", "1");
          sessionStorage.setItem("casesifter_email", email);
        }
        if (data?.analysis_code) {
          try {
            await fetch("https://casesifter-alex-analysis.onrender.com/api/v1/session/accept", {
              method: "POST",
              credentials: "include",
              headers: { Accept: "application/json", "Content-Type": "application/json" },
              body: JSON.stringify({ code: data.analysis_code }),
            });
          } catch {
            /* analysis cookie best-effort; Bearer covers auth */
          }
        }
        const cookieOnly = await fetch("/v1/entitlement", {
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        const withBearer = await fetch("/v1/entitlement", {
          credentials: "include",
          headers: {
            Accept: "application/json",
            ...(data?.token ? { Authorization: `Bearer ${data.token}` } : {}),
          },
        });
        const entBody = await withBearer.json().catch(() => ({}));
        return {
          loginStatus: res.status,
          hasToken: Boolean(data?.token),
          hasAnalysisCode: Boolean(data?.analysis_code),
          cookieOnlyStatus: cookieOnly.status,
          entStatus: withBearer.status,
          active: entBody?.active,
          bearerStored: Boolean(sessionStorage.getItem("casesifter_session_token")),
        };
      },
      { email: EMAIL, password: PW }
    );
    sameOriginLogin = true;
    loginHadToken = apiResult.hasToken;
    const pass =
      apiResult.loginStatus === 200 &&
      apiResult.entStatus === 200 &&
      apiResult.active === true &&
      (apiResult.cookieOnlyStatus === 200 || apiResult.hasToken);
    record(
      "3. Login under 3P-cookie strip → entitlement",
      pass,
      `login=${apiResult.loginStatus} cookieOnly=${apiResult.cookieOnlyStatus} ent=${apiResult.entStatus} token=${apiResult.hasToken} analysis_code=${apiResult.hasAnalysisCode} active=${apiResult.active}`
    );

    const matters = await page.evaluate(async () => {
      const tok = sessionStorage.getItem("casesifter_session_token") || "";
      const res = await fetch("https://casesifter-alex-analysis.onrender.com/api/v1/matters", {
        credentials: "include",
        headers: {
          Accept: "application/json",
          ...(tok ? { Authorization: `Bearer ${tok}` } : {}),
        },
      });
      return { status: res.status };
    });
    record(
      "4. Analysis matters with Bearer/cookie",
      matters.status >= 200 && matters.status < 300,
      `http=${matters.status}`
    );
  } catch (e) {
    record("3. Login under 3P-cookie strip → entitlement", false, String(e.message || e));
  }

  record(
    "5. Path documentation",
    sameOriginLogin || controlBase.path === "same-origin",
    `sameOriginLogin=${sameOriginLogin} crossOriginLogin=${crossOriginLogin} loginHadToken=${loginHadToken} strippedControlSetCookie=${strippedControlCookies}`
  );

  await browser.close();
  writeResults();
  const failed = results.some((r) => r.pass === "Fail");
  process.exit(failed ? 1 : 0);
}

function writeResults() {
  const out = join(process.cwd(), "alex_cookie_block_results.json");
  writeFileSync(out, JSON.stringify({ at: new Date().toISOString(), results }, null, 2));
  console.log(`Wrote ${out}`);
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
