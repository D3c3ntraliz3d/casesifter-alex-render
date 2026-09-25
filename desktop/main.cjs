const { app, BrowserWindow, dialog, shell, ipcMain, protocol } = require("electron");
const path = require("path");
const fs = require("fs");
const store = require("./local_store.cjs");
const telemetry = require("./product_telemetry.cjs");
const pkg = require("./package.json");

function initSentry(cfg) {
  const packaged = loadPackagedDefaults();
  const dsn =
    process.env.SENTRY_DSN ||
    process.env.CASESIFTER_SENTRY_DSN ||
    packaged.sentryDsn ||
    "";
  if (!dsn) return;
  try {
    const Sentry = require("@sentry/electron/main");
    Sentry.init({
      dsn,
      environment:
        process.env.CASESIFTER_SENTRY_ENV ||
        packaged.sentryEnvironment ||
        "production",
      release: `casesifter-desktop@${pkg.version}`,
      beforeSend(event) {
        const scrub = (v) => {
          if (typeof v !== "string") return v;
          return v
            .replace(/(?:[A-Za-z]:\\|\/)[^\s"']{3,}/g, "[path]")
            .slice(0, 500);
        };
        if (event.exception?.values) {
          for (const ex of event.exception.values) {
            if (ex.value) ex.value = scrub(ex.value);
          }
        }
        return event;
      },
    });
    Sentry.setTag("service", "casesifter-desktop");
    if (cfg.supportCode) Sentry.setUser({ id: cfg.supportCode });
  } catch {
    /* optional dependency */
  }
}

function persistEntitlement(cfg, ent) {
  cfg.lastEntitlementActive = Boolean(ent?.active);
  if (ent?.support_code) cfg.supportCode = ent.support_code;
  saveConfig(cfg);
}

let _flushTimer = null;
function track(cfg, type, payload) {
  if (!root) return;
  telemetry.recordEvent(root, type, payload);
  // Debounce — never flush on every scan tick (control plane timeouts made the app feel stuck).
  if (_flushTimer) clearTimeout(_flushTimer);
  _flushTimer = setTimeout(() => {
    telemetry.flushToControl(cfg, root, remoteJson).catch(() => {});
  }, 5000);
}

protocol.registerSchemesAsPrivileged([
  { scheme: "casesifter-local", privileges: { standard: true, supportFetchAPI: true, bypassCSP: true } },
]);

function loadPackagedDefaults() {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, "packaged-defaults.json"), "utf8"));
  } catch {
    return {};
  }
}

function buildDefaults() {
  const packaged = loadPackagedDefaults();
  const useProdApis = app.isPackaged;
  return {
    analysisApiUrl:
      process.env.CASESIFTER_ANALYSIS_URL ||
      (useProdApis ? packaged.analysisApiUrl : null) ||
      "http://127.0.0.1:8000",
    controlPlaneUrl:
      process.env.CASESIFTER_CONTROL_URL ||
      (useProdApis ? packaged.controlPlaneUrl : null) ||
      "http://127.0.0.1:8010",
    analysisApiKey: process.env.CASESIFTER_ANALYSIS_KEY || "",
    sessionToken: "",
    lastEntitlementActive: false,
    // Dev default is Vite; packaged builds ignore uiUrl and load embedded renderer.
    uiUrl: process.env.CASESIFTER_UI_URL || "http://127.0.0.1:5173",
  };
}

let DEFAULTS = null;
let root;
let configPath;

function loadConfig() {
  const base = DEFAULTS || buildDefaults();
  try {
    return { ...base, ...JSON.parse(fs.readFileSync(configPath, "utf8")) };
  } catch {
    return { ...base };
  }
}

function saveConfig(cfg) {
  fs.writeFileSync(configPath, JSON.stringify(cfg, null, 2));
}

function embeddedUiPath() {
  return path.join(__dirname, "renderer", "app", "index.html");
}

function authHeaders(cfg) {
  const headers = { Accept: "application/json" };
  if (cfg.analysisApiKey) headers["X-API-Key"] = cfg.analysisApiKey;
  if (cfg.sessionToken) headers.Authorization = `Bearer ${cfg.sessionToken}`;
  return headers;
}

function describeDetail(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  // FastAPI validation errors come back as a list of {loc, msg, ...} objects.
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        const field = Array.isArray(item?.loc) ? item.loc.filter((p) => p !== "body").join(".") : "";
        const msg = item?.msg || JSON.stringify(item);
        return field ? `${field}: ${msg}` : msg;
      })
      .join("; ");
  }
  if (typeof detail === "object") return detail.msg || JSON.stringify(detail);
  return String(detail);
}

async function remoteJson(url, options = {}) {
  const res = await fetch(url, options);
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    throw new Error(describeDetail(data.detail) || data.message || res.statusText || "Request failed");
  }
  return data;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 960,
    minHeight: 640,
    title: "CaseSifter",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  const cfg = loadConfig();
  // Packaged builds load the embedded Vite UI unless CASESIFTER_UI_URL overrides.
  if (!process.env.CASESIFTER_UI_URL && app.isPackaged) {
    const indexHtml = embeddedUiPath();
    if (fs.existsSync(indexHtml)) {
      win.loadFile(indexHtml).catch(() => {
        win.loadFile(path.join(__dirname, "renderer", "fallback.html"));
      });
      return;
    }
  }
  const ui = process.env.CASESIFTER_UI_URL || cfg.uiUrl || "http://127.0.0.1:5173";
  win.loadURL(ui).catch(() => {
    win.loadFile(path.join(__dirname, "renderer", "fallback.html"));
  });
}

app.whenReady().then(() => {
  DEFAULTS = buildDefaults();
  root = store.storeRoot(app.getPath("userData"));
  configPath = path.join(app.getPath("userData"), "casesifter-config.json");
  if (!fs.existsSync(configPath)) saveConfig(DEFAULTS);

  protocol.handle("casesifter-local", (request) => {
    try {
      const u = new URL(request.url);
      // casesifter-local://{caseId}/{documentId}
      const caseId = u.hostname;
      const documentId = decodeURIComponent((u.pathname || "/").replace(/^\//, ""));
      const caseData = store.getCase(root, caseId);
      const doc = (caseData?.documents || []).find((d) => d.id === documentId);
      if (!doc?.stored_path || !fs.existsSync(doc.stored_path)) {
        return new Response("Not found", { status: 404 });
      }
      const buf = fs.readFileSync(doc.stored_path);
      const ext = path.extname(doc.stored_path).toLowerCase();
      const type =
        ext === ".pdf"
          ? "application/pdf"
          : ext === ".png"
            ? "image/png"
            : ext === ".jpg" || ext === ".jpeg"
              ? "image/jpeg"
              : "application/octet-stream";
      return new Response(buf, { headers: { "Content-Type": type } });
    } catch {
      return new Response("Bad request", { status: 400 });
    }
  });

  const cfg = loadConfig();
  initSentry(cfg);
  track(cfg, "app_open", { app_version: pkg.version, platform: telemetry.platformLabel() });
  createWindow();
});

app.on("render-process-gone", (_event, _webContents, details) => {
  const cfg = loadConfig();
  track(cfg, "renderer_crash", { reason: details?.reason || "unknown" });
});

process.on("uncaughtException", (err) => {
  const cfg = loadConfig();
  track(cfg, "main_uncaught", { error_class: "other", message: String(err?.message || err).slice(0, 200) });
});

process.on("unhandledRejection", (reason) => {
  const cfg = loadConfig();
  track(cfg, "main_unhandled_rejection", {
    error_class: "other",
    message: String(reason || "rejection").slice(0, 200),
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

ipcMain.handle("cs:get-config", () => {
  const cfg = loadConfig();
  return {
    analysisApiUrl: cfg.analysisApiUrl,
    controlPlaneUrl: cfg.controlPlaneUrl,
    hasApiKey: Boolean(cfg.analysisApiKey),
    hasSession: Boolean(cfg.sessionToken),
    dataRoot: root,
  };
});

ipcMain.handle("cs:set-config", (_e, patch) => {
  const cfg = { ...loadConfig(), ...patch };
  saveConfig(cfg);
  return true;
});

ipcMain.handle("cs:list-cases", () => store.listCases(root));
ipcMain.handle("cs:create-case", (_e, payload) => store.createCase(root, payload));
ipcMain.handle("cs:get-case", (_e, id) => store.getCase(root, id));
ipcMain.handle("cs:archive-case", (_e, id) => store.archiveCase(root, id, true));
ipcMain.handle("cs:unarchive-case", (_e, id) => store.archiveCase(root, id, false));
ipcMain.handle("cs:delete-case", (_e, id) => store.deleteCase(root, id));
ipcMain.handle("cs:delete-doc", (_e, caseId, documentId) => {
  if (!store.deleteDocument(root, caseId, documentId)) {
    throw new Error("Unknown document.");
  }
  return { deleted: documentId, case_id: caseId };
});

ipcMain.handle("cs:pick-docs", async (_e, caseId) => {
  const result = await dialog.showOpenDialog({
    properties: ["openFile", "multiSelections"],
    filters: [
      { name: "Filings", extensions: ["pdf", "docx", "xlsx", "txt", "jpg", "jpeg", "png", "gif"] },
    ],
  });
  if (result.canceled) return [];
  return store.addDocuments(
    root,
    caseId,
    result.filePaths.map((fp) => ({ filePath: fp, originalName: path.basename(fp) }))
  );
});

function markAnalyzeFailed(caseId, documentId, message, extra = {}) {
  const text = String(message || "Analysis did not start.").slice(0, 500);
  store.updateDocument(root, caseId, documentId, {
    status: "failed",
    last_error: text,
    ...extra,
  });
  return text;
}

async function analysisApiEntitlement(cfg) {
  try {
    return await remoteJson(`${cfg.analysisApiUrl.replace(/\/$/, "")}/api/entitlement`, {
      headers: { Accept: "application/json" },
    });
  } catch {
    return null;
  }
}

async function ensureAnalyzeAllowed(cfg) {
  // Ops key bypasses product entitlement.
  if (cfg.analysisApiKey) return;

  // Local / open analysis API unlocks scans without a control-plane session.
  const localEnt = await analysisApiEntitlement(cfg);
  if (localEnt?.active && (localEnt.mode === "open" || localEnt.plan === "local-dev")) {
    cfg.lastEntitlementActive = true;
    saveConfig(cfg);
    return;
  }

  if (!cfg.sessionToken) {
    throw new Error("Sign in (or start a trial) before running analysis.");
  }
  try {
    const ent = await remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/entitlement`, {
      headers: { Authorization: `Bearer ${cfg.sessionToken}`, Accept: "application/json" },
    });
    cfg.lastEntitlementActive = Boolean(ent.active);
    saveConfig(cfg);
    if (!ent.active) {
      throw new Error(ent.message || "Subscription inactive. Subscribe or start a trial.");
    }
  } catch (err) {
    if (String(err.message || "").includes("Subscribe") || String(err.message || "").includes("Sign in")) {
      throw err;
    }
    if (!cfg.lastEntitlementActive) {
      throw new Error("Cannot verify subscription (control plane unreachable). Sign in when online.");
    }
  }
}

ipcMain.handle("cs:analyze", async (_e, caseId, documentId) => {
  const caseData = store.getCase(root, caseId);
  if (!caseData) throw new Error("Unknown case.");
  const doc = (caseData.documents || []).find((d) => d.id === documentId);
  if (!doc) throw new Error("Unknown document.");
  const cfg = loadConfig();

  try {
    await ensureAnalyzeAllowed(cfg);

    if (!fs.existsSync(doc.stored_path)) {
      throw new Error("Stored file is missing on this PC.");
    }

    const form = new FormData();
    const buf = fs.readFileSync(doc.stored_path);
    form.append("file", new Blob([buf]), doc.original_filename);
    form.append("submitted_by", "desktop-client");
    form.append("display_name", doc.display_name || doc.original_filename);
    if (doc.doc_kind) form.append("doc_kind", doc.doc_kind);
    const peers = store.buildPeerSnapshots(root, caseId, documentId);
    if (peers.length) {
      form.append("peer_snapshots", JSON.stringify(peers));
    }

    let sizeBytes = 0;
    try {
      sizeBytes = fs.statSync(doc.stored_path).size;
    } catch {
      /* ignore */
    }
    track(cfg, "scan_started", {
      doc_kind: doc.doc_kind || "unknown",
      file_mb_bucket: telemetry.fileMbBucket(sizeBytes),
      document_id: documentId.slice(0, 16),
    });
    store.updateDocument(root, caseId, documentId, { status: "queued", last_error: null });
    const headers = authHeaders(cfg);
    delete headers.Accept;
    let created;
    try {
      created = await remoteJson(`${cfg.analysisApiUrl.replace(/\/$/, "")}/api/v1/jobs`, {
        method: "POST",
        headers,
        body: form,
      });
    } catch (err) {
      throw new Error(
        err.message ||
          `Analysis API unreachable at ${cfg.analysisApiUrl}. Start the analysis service and try Scan again.`
      );
    }
    store.updateDocument(root, caseId, documentId, {
      status: "scanning",
      scan_job_id: created.job_id,
      last_error: null,
    });

    // Poll until complete, then cache report locally and ask server to purge.
    const jobId = created.job_id;
    let progress = null;
    // ~20 min @ 4s — above analysis scan_max_seconds (18 min) so server-side timeout wins first.
    for (let i = 0; i < 300; i++) {
      await new Promise((r) => setTimeout(r, 4000));
      progress = await remoteJson(
        `${cfg.analysisApiUrl.replace(/\/$/, "")}/api/v1/jobs/${jobId}`,
        { headers: authHeaders(cfg) }
      );
      if (progress.status === "complete") {
        const report = progress.report
          || (await remoteJson(
            `${cfg.analysisApiUrl.replace(/\/$/, "")}/api/v1/jobs/${jobId}/report`,
            { headers: authHeaders(cfg) }
          ));
        store.saveReport(root, caseId, documentId, report);
        store.updateDocument(root, caseId, documentId, {
          status: "complete",
          risk_level: report.risk_level || null,
          scan_job_id: jobId,
          last_error: null,
          last_scanned_at: (report.audit && report.audit.scanned_at) || new Date().toISOString(),
        });
        // Elevate peers + this file from case-level conflicts (covers scan order).
        try {
          const packet = store.buildFactConflictPacket(root, caseId);
          if ((packet?.documents || []).length >= 2) {
            const packed = await remoteJson(
              `${cfg.analysisApiUrl.replace(/\/$/, "")}/api/v1/fact-conflicts`,
              {
                method: "POST",
                headers: { ...authHeaders(cfg), "Content-Type": "application/json" },
                body: JSON.stringify({ documents: packet.documents }),
              }
            );
            store.applyFactConflictRisks(root, caseId, packed.fact_conflicts || []);
          }
        } catch {
          /* board refresh still available via listCaseFactConflicts */
        }
        track(cfg, "scan_completed", {
          job_id: jobId.slice(0, 32),
          risk_level: (store.getCase(root, caseId)?.documents || []).find((d) => d.id === documentId)
            ?.risk_level || report.risk_level || "unknown",
        });
        try {
          await remoteJson(`${cfg.analysisApiUrl.replace(/\/$/, "")}/api/v1/jobs/${jobId}`, {
            method: "DELETE",
            headers: authHeaders(cfg),
          });
        } catch {
          /* sweeper will remove */
        }
        return { status: "complete", report };
      }
      if (progress.status === "failed") {
        const msg = progress.error || progress.message || "Analysis failed.";
        const stage = progress.stage || "failed";
        cfg.lastScanFailure = { job_id: jobId, stage, error_class: "timeout", at: new Date().toISOString() };
        saveConfig(cfg);
        track(cfg, "scan_failed", { job_id: jobId.slice(0, 32), stage, error_class: "other" });
        markAnalyzeFailed(caseId, documentId, msg, { scan_job_id: jobId });
        throw new Error(msg);
      }
    }
    const timeoutMsg = "Analysis timed out.";
    cfg.lastScanFailure = {
      job_id: jobId,
      stage: progress?.stage || "unknown",
      error_class: "timeout",
      at: new Date().toISOString(),
    };
    saveConfig(cfg);
    track(cfg, "scan_failed", {
      job_id: String(jobId).slice(0, 32),
      stage: progress?.stage || "unknown",
      error_class: "timeout",
    });
    markAnalyzeFailed(caseId, documentId, timeoutMsg, { scan_job_id: jobId });
    throw new Error(timeoutMsg);
  } catch (err) {
    const latest = store.getCase(root, caseId);
    const row = (latest?.documents || []).find((d) => d.id === documentId);
    if (row && (row.status === "uploaded" || row.status === "queued" || row.status === "scanning")) {
      markAnalyzeFailed(caseId, documentId, err.message, {
        scan_job_id: row.scan_job_id || null,
      });
    }
    throw err;
  }
});

ipcMain.handle("cs:get-report", (_e, caseId, documentId) => store.loadReport(root, caseId, documentId));
ipcMain.handle("cs:list-chat", (_e, caseId) => store.listCaseMessages(root, caseId));
ipcMain.handle("cs:case-chat", async (_e, caseId, question, focusDocumentId) => {
  const cfg = loadConfig();
  await ensureAnalyzeAllowed(cfg);
  const packet = store.buildChatPacket(root, caseId);
  if (!packet) throw new Error("Unknown case.");
  const history = store.listCaseMessages(root, caseId, 8).map((m) => ({
    role: m.role,
    content: m.content,
  }));
  store.addCaseMessage(root, caseId, "user", String(question || "").trim(), []);
  const body = {
    question: String(question || "").trim(),
    focus_document_id: focusDocumentId || undefined,
    history,
    documents: packet.documents.filter((d) => (d.text_excerpt || "").trim().length > 40).slice(0, 60),
  };
  if (!body.documents.length) {
    const msg = store.addCaseMessage(
      root,
      caseId,
      "assistant",
      "No scanned text excerpts on this PC yet. Rescan waiting files, then ask again.",
      []
    );
    return { answer: msg.content, citations: [], message: msg, used_model: false };
  }
  let data;
  try {
    data = await remoteJson(`${cfg.analysisApiUrl.replace(/\/$/, "")}/api/v1/case-chat`, {
      method: "POST",
      headers: { ...authHeaders(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new Error(
      err.message ||
        `Case chat unreachable at ${cfg.analysisApiUrl}. Start the analysis service and try again.`
    );
  }
  const assistant = store.addCaseMessage(
    root,
    caseId,
    "assistant",
    data.answer || "No answer.",
    data.citations || []
  );
  track(cfg, "chat_asked", {
    used_model: Boolean(data.used_model),
    hit_count: (data.citations || []).length,
    question_len: String(question || "").length,
  });
  return {
    answer: assistant.content,
    citations: assistant.citations,
    message: assistant,
    used_model: Boolean(data.used_model),
  };
});

ipcMain.handle("cs:case-fact-conflicts", async (_e, caseId) => {
  const cfg = loadConfig();
  await ensureAnalyzeAllowed(cfg);
  const packet = store.buildFactConflictPacket(root, caseId);
  if (!packet) throw new Error("Unknown case.");
  const documents = (packet.documents || []).slice(0, 80);
  if (documents.length < 2) {
    return { fact_conflicts: [] };
  }
  try {
    const data = await remoteJson(`${cfg.analysisApiUrl.replace(/\/$/, "")}/api/v1/fact-conflicts`, {
      method: "POST",
      headers: { ...authHeaders(cfg), "Content-Type": "application/json" },
      body: JSON.stringify({ documents }),
    });
    const conflicts = data.fact_conflicts || [];
    const elevated = store.applyFactConflictRisks(root, caseId, conflicts);
    return { fact_conflicts: conflicts, elevated };
  } catch (err) {
    throw new Error(
      err.message ||
        `Fact-conflict check unreachable at ${cfg.analysisApiUrl}. Start the analysis service and reopen the case.`
    );
  }
});
ipcMain.handle("cs:open-doc", async (_e, caseId, documentId) => {
  const caseData = store.getCase(root, caseId);
  const doc = (caseData?.documents || []).find((d) => d.id === documentId);
  if (!doc?.stored_path) throw new Error("File missing.");
  await shell.openPath(doc.stored_path);
  return true;
});

ipcMain.handle("cs:login", async (_e, email, password) => {
  const cfg = loadConfig();
  const data = await remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ email, password }),
  });
  cfg.sessionToken = data.token;
  persistEntitlement(cfg, data.entitlement);
  return data.entitlement;
});

ipcMain.handle("cs:signup", async (_e, email, password, name, acceptedTerms, inviteCode) => {
  const cfg = loadConfig();
  const data = await remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      email,
      password,
      name,
      accepted_terms: Boolean(acceptedTerms),
      invite_code: inviteCode || undefined,
    }),
  });
  cfg.sessionToken = data.token;
  persistEntitlement(cfg, data.entitlement);
  return data.entitlement;
});

ipcMain.handle("cs:entitlement", async () => {
  const cfg = loadConfig();
  if (!cfg.sessionToken) {
    const localEnt = await analysisApiEntitlement(cfg);
    if (localEnt?.active && (localEnt.mode === "open" || localEnt.plan === "local-dev")) {
      cfg.lastEntitlementActive = true;
      saveConfig(cfg);
      return {
        ...localEnt,
        hasSession: false,
        message: localEnt.message || "Open mode — scans unlocked for local use.",
      };
    }
    return { active: false, mode: "cloud", message: "Sign in to activate your subscription.", plan: null, hasSession: false };
  }
  try {
    const data = await remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/entitlement`, {
      headers: { Authorization: `Bearer ${cfg.sessionToken}`, Accept: "application/json" },
    });
    persistEntitlement(cfg, data);
    return { ...data, mode: "cloud", hasSession: true, support_code: data.support_code || cfg.supportCode };
  } catch {
    return {
      active: Boolean(cfg.lastEntitlementActive),
      mode: "cloud",
      plan: "cloud-cached",
      hasSession: true,
      message: cfg.lastEntitlementActive
        ? "Using last known active entitlement; control plane unreachable."
        : "Control plane unreachable. Sign in when online.",
      cached: true,
      support_code: cfg.supportCode || null,
    };
  }
});

ipcMain.handle("cs:report-problem", async (_e, note) => {
  const cfg = loadConfig();
  if (!cfg.sessionToken) throw new Error("Sign in so we can attach your support code.");
  let ent = { plan: null };
  try {
    ent = await remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/entitlement`, {
      headers: { Authorization: `Bearer ${cfg.sessionToken}`, Accept: "application/json" },
    });
  } catch {
    ent = { plan: null, support_code: cfg.supportCode };
  }
  const body = {
    app_version: pkg.version,
    platform: telemetry.platformLabel(),
    entitlement_mode: "cloud",
    plan: ent?.plan || null,
    note: String(note || "").slice(0, 2000),
    recent_events: telemetry.recentEvents(root, 20),
    last_failure: cfg.lastScanFailure || null,
  };
  return remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/diagnostics`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${cfg.sessionToken}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(body),
  });
});

ipcMain.handle("cs:checkout", async (_e, trial) => {
  const cfg = loadConfig();
  if (!cfg.sessionToken) throw new Error("Sign in first.");
  return remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/checkout`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${cfg.sessionToken}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({ trial: Boolean(trial) }),
  });
});

ipcMain.handle("cs:finding-feedback", async (_e, payload) => {
  const cfg = loadConfig();
  const body = {
    finding_id: payload?.finding_id || "",
    useful: Boolean(payload?.useful),
    category: payload?.category || null,
    field: payload?.field || null,
    doc_kind: payload?.doc_kind || null,
  };
  // Prefer analysis API (local SQLite telemetry); fall back to control plane.
  try {
    return await remoteJson(`${cfg.analysisApiUrl.replace(/\/$/, "")}/api/telemetry/finding-feedback`, {
      method: "POST",
      headers: { ...authHeaders(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/telemetry`, {
      method: "POST",
      headers: {
        ...(cfg.sessionToken ? { Authorization: `Bearer ${cfg.sessionToken}` } : {}),
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        events: [
          {
            id: `desk-${Date.now()}`,
            type: "finding_feedback",
            payload: {
              finding_id_hash: String(body.finding_id).slice(0, 24),
              useful: body.useful,
              category: body.category,
              field: body.field,
              doc_kind: body.doc_kind,
            },
            created_at: new Date().toISOString(),
          },
        ],
      }),
    });
  }
});
