import { rememberScanFile, sha256OfFile } from "./localFiles.js";

const jsonHeaders = { Accept: "application/json" };

/** Soft-launch hosted URLs (Vite build-time). Fallbacks keep local + marketing signup working. */
const CONTROL_PLANE_URL = String(
  import.meta.env.VITE_CONTROL_PLANE_URL || "https://casesifter-control.onrender.com"
).replace(/\/$/, "");
const ANALYSIS_API_URL = String(import.meta.env.VITE_ANALYSIS_API_URL || "").replace(/\/$/, "");
const ANALYSIS_API_KEY = String(import.meta.env.VITE_ANALYSIS_API_KEY || "").trim();

const SESSION_TOKEN_KEY = "casesifter_session_token";
const SESSION_EMAIL_KEY = "casesifter_email";

function desktop() {
  return typeof window !== "undefined" && window.casesifter?.isDesktop ? window.casesifter : null;
}

function getSessionToken() {
  try {
    return localStorage.getItem(SESSION_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function setSession(token, email) {
  try {
    if (token) localStorage.setItem(SESSION_TOKEN_KEY, token);
    else localStorage.removeItem(SESSION_TOKEN_KEY);
    if (email) localStorage.setItem(SESSION_EMAIL_KEY, email);
  } catch {
    /* private mode */
  }
}

function clearSession() {
  try {
    localStorage.removeItem(SESSION_TOKEN_KEY);
    localStorage.removeItem(SESSION_EMAIL_KEY);
  } catch {
    /* private mode */
  }
}

/** True when a control-plane response means the Bearer token is gone/expired. */
function isSessionAuthFailure(res, detail = "") {
  if (res && (res.status === 401 || res.status === 403)) return true;
  const msg = String(detail || "").toLowerCase();
  return /invalid session|bearer token required|unknown user|session expired/.test(msg);
}

function sessionExpiredError() {
  return new Error("Session expired. Sign in again to start your trial.");
}

/**
 * Absorb marketing→app session handoff (?cs_token= / #cs_token=).
 * Signup stores the token on casesifter.com; the app is a different origin.
 */
export function absorbSessionHandoff() {
  if (typeof window === "undefined") return false;
  let token = "";
  let email = "";
  try {
    const params = new URLSearchParams(window.location.search || "");
    token = (params.get("cs_token") || params.get("token") || "").trim();
    email = (params.get("cs_email") || params.get("email") || "").trim();
    const hash = (window.location.hash || "").replace(/^#/, "");
    if (hash) {
      const hp = new URLSearchParams(hash);
      if (!token) token = (hp.get("cs_token") || hp.get("token") || "").trim();
      if (!email) email = (hp.get("cs_email") || hp.get("email") || "").trim();
    }
    if (!token) return false;
    setSession(token, email || undefined);
    params.delete("cs_token");
    params.delete("token");
    params.delete("cs_email");
    params.delete("email");
    const qs = params.toString();
    const next = `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash && !hash.includes("cs_token") && !hash.includes("token=") ? window.location.hash : ""}`;
    window.history.replaceState({}, "", next || window.location.pathname);
    return true;
  } catch {
    return false;
  }
}

/** Open Stripe Checkout / Customer Portal URL in this tab (reliable after await). */
export function openCheckoutUrl(url) {
  if (!url) return false;
  try {
    window.location.assign(url);
    return true;
  } catch {
    try {
      window.open(url, "_blank", "noopener,noreferrer");
      return true;
    } catch {
      return false;
    }
  }
}

function controlUrl(path) {
  return `${CONTROL_PLANE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

/** Analysis/API base: remote Render URL when set, else same-origin (Vite proxy / nginx). */
function apiUrl(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  if (ANALYSIS_API_URL) return `${ANALYSIS_API_URL}${p}`;
  return p;
}

function authHeaders(extra = {}) {
  const headers = { ...jsonHeaders, ...extra };
  const token = getSessionToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (ANALYSIS_API_KEY) headers["X-API-Key"] = ANALYSIS_API_KEY;
  return headers;
}

function controlHeaders(extra = {}) {
  const headers = { ...jsonHeaders, ...extra };
  const token = getSessionToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function apiFetch(path, options = {}) {
  const headers = { ...authHeaders(), ...(options.headers || {}) };
  // FormData must not force Content-Type (browser sets boundary).
  if (options.body instanceof FormData) {
    delete headers["Content-Type"];
  }
  return fetch(apiUrl(path), { ...options, headers });
}

// Electron prefixes every rejected ipcRenderer.invoke() with "Error invoking remote
// method 'x': ", which otherwise leaks straight into the UI as-is.
async function callDesktop(promise) {
  try {
    return await promise;
  } catch (err) {
    const raw = String(err?.message || err || "Something went wrong.");
    const cleaned = raw.replace(/^Error invoking remote method '[^']+':\s*(Error:\s*)?/, "");
    throw new Error(cleaned || "Something went wrong.");
  }
}

async function safeDetail(res) {
  try {
    const data = await res.json();
    return data.detail || JSON.stringify(data);
  } catch {
    return res.statusText;
  }
}

async function readJson(res) {
  if (!res.ok) {
    throw new Error((await safeDetail(res)) || "Request failed.");
  }
  return res.json();
}

export async function uploadExhibit(file, submittedBy = "court-staff") {
  const body = new FormData();
  body.append("file", file);
  body.append("submitted_by", submittedBy);
  const res = await apiFetch("/api/upload", { method: "POST", body });
  return readJson(res);
}

export async function getJob(jobId, { includeReport = false } = {}) {
  const cs = desktop();
  if (cs) {
    // Desktop jobs are ephemeral; status/report live in the local store after analyze.
    return {
      job_id: jobId,
      status: "unknown",
      stage: "unknown",
      percent: 0,
      message: "Open the document report from the case list (cached locally).",
      report: null,
      ephemeral: true,
    };
  }
  const qs = includeReport ? "?include_report=true" : "";
  // Prefer ephemeral v1 when talking to hosted analysis; fall back to vault job route.
  if (ANALYSIS_API_URL) {
    const res = await analysisFetch(`/api/v1/jobs/${jobId}${qs}`);
    return readJson(res);
  }
  const res = await apiFetch(`/api/jobs/${jobId}${qs}`);
  return readJson(res);
}

export function exportUrl(jobId, kind) {
  return apiUrl(`/api/jobs/${jobId}/export/${kind}`);
}

export function documentFileUrl(caseId, documentId) {
  const cs = desktop();
  if (cs) return `casesifter-local://${caseId}/${documentId}`;
  return null;
}

function vaultUnavailableError() {
  return new Error(
    "Case memory is unavailable. Sign in and ensure the analysis service is running."
  );
}

/**
 * Soft-launch browser build points VITE_ANALYSIS_API_URL at APP_MODE=analysis.
 * Matters / fact memory live on /api/v1/matters (not the local vault /api/cases).
 */
export function isHostedAnalysis() {
  return Boolean(ANALYSIS_API_URL);
}

/** User-facing copy when hosted analysis (matters + scans) is down. Ops steps stay in docs only. */
export const ANALYSIS_OFFLINE_MESSAGE =
  "Analysis is temporarily unavailable. You can still use Account and billing. Try again shortly, or contact support if this continues.";

/** Preflight for hosted bulk upload — GET analysis /api/health. */
export async function checkAnalysisHealth() {
  if (!isHostedAnalysis()) return { ok: true, status: "ok" };
  try {
    const res = await analysisFetch("/api/health");
    if (!res.ok) return { ok: false, status: res.status };
    const data = await readJson(res).catch(() => ({}));
    const status = String(data?.status || "").toLowerCase();
    return { ok: status === "ok" || status === "healthy" || !status, ...data };
  } catch (err) {
    return { ok: false, error: err };
  }
}

export function isAnalysisUnreachableError(err) {
  const msg = String(err?.message || err || "");
  return /analysis (service )?unreachable|analysis is temporarily unavailable|failed to fetch|networkerror|load failed|503|service suspended|phase 2/i.test(
    msg
  );
}

function analysisUnreachableError(cause) {
  const raw = String(cause?.message || cause || "");
  if (/failed to fetch|networkerror|load failed|503|service suspended/i.test(raw)) {
    return new Error(ANALYSIS_OFFLINE_MESSAGE);
  }
  return cause instanceof Error ? cause : new Error(raw || "Analysis request failed.");
}

async function analysisFetch(path, options = {}) {
  try {
    const res = await apiFetch(path, options);
    if (res.status === 503) throw analysisUnreachableError(new Error("503"));
    return res;
  } catch (err) {
    throw analysisUnreachableError(err);
  }
}

export async function listCases() {
  const cs = desktop();
  if (cs) return cs.listCases();
  if (isHostedAnalysis()) {
    const res = await analysisFetch("/api/v1/matters");
    const data = await readJson(res);
    return data.matters || data.cases || [];
  }
  const res = await apiFetch("/api/cases");
  if (res.status === 404) return [];
  const data = await readJson(res);
  return data.cases || [];
}

export async function createCase(payload) {
  const cs = desktop();
  if (cs) return cs.createCase(payload);
  if (isHostedAnalysis()) {
    const body = {
      name: payload.caption || payload.name || "Untitled matter",
      caption: payload.caption || payload.name,
      index_number: payload.index_number || null,
      court: payload.court || null,
      county: payload.county || null,
      notes: payload.notes || null,
    };
    const res = await analysisFetch("/api/v1/matters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return readJson(res);
  }
  const res = await apiFetch("/api/cases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function getCase(caseId) {
  const cs = desktop();
  if (cs) return cs.getCase(caseId);
  if (isHostedAnalysis()) {
    const res = await analysisFetch(`/api/v1/matters/${caseId}`);
    return readJson(res);
  }
  const res = await apiFetch(`/api/cases/${caseId}`);
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function archiveCase(caseId) {
  const cs = desktop();
  if (cs) return cs.archiveCase(caseId);
  if (isHostedAnalysis()) {
    // Hosted matters have no archive yet — treat as no-op soft hide client-side.
    return { id: caseId, archived_at: new Date().toISOString() };
  }
  const res = await apiFetch(`/api/cases/${caseId}/archive`, { method: "POST" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function unarchiveCase(caseId) {
  const cs = desktop();
  if (cs) return cs.unarchiveCase(caseId);
  if (isHostedAnalysis()) {
    return { id: caseId, archived_at: null };
  }
  const res = await apiFetch(`/api/cases/${caseId}/unarchive`, { method: "POST" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function deleteCase(caseId) {
  const cs = desktop();
  if (cs) {
    await cs.deleteCase(caseId);
    return { deleted: caseId };
  }
  if (isHostedAnalysis()) {
    const res = await analysisFetch(`/api/v1/matters/${caseId}`, { method: "DELETE" });
    return readJson(res);
  }
  const res = await apiFetch(`/api/cases/${caseId}`, { method: "DELETE" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function deleteDocument(caseId, documentId) {
  const cs = desktop();
  if (cs) return cs.deleteDocument(caseId, documentId);
  if (isHostedAnalysis()) {
    const res = await analysisFetch(`/api/v1/matters/${caseId}/documents/${documentId}`, {
      method: "DELETE",
    });
    return readJson(res);
  }
  const res = await apiFetch(`/api/cases/${caseId}/documents/${documentId}`, {
    method: "DELETE",
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function updateCase(caseId, payload) {
  if (isHostedAnalysis()) {
    // Name/index edits not yet exposed; return current matter.
    return getCase(caseId);
  }
  const res = await apiFetch(`/api/cases/${caseId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

function isTransientUploadError(err) {
  const text = String(err?.message || err || "");
  return /failed to fetch|networkerror|econnreset|econnrefused|502|503|504|database is locked/i.test(text);
}


/** NYSCEF EXHIBIT(S) / EXHIBIT_S — keep in sync with backend auto_title heuristics. */
function looksLikeExhibitFilename(name) {
  const text = String(name || "");
  if (!text) return false;
  return (
    /(?:^|[\s_\-\u2010-\u2015.\/(])exhibit(?:\s*\(\s*s\s*\))?(?=$|[\s_\-\u2010-\u2015.\/(])/i.test(
      text
    ) || /\bexhibit\b/i.test(text)
  );
}

function proposeExhibitDisplayName(filename) {
  const stem = String(filename || "")
    .replace(/\.[A-Za-z0-9]+$/, "")
    .replace(/\s*\(\d+\)\s*$/, "")
    .trim();
  // "111 - EXHIBIT(S) - Settlement Aff.pdf" → Exhibit 111 — Settlement Aff
  const leading = stem.match(/^(\d{1,4})(?:\s*[\-\u2010-\u2015]\s*|\s+)(?=exhibit)/i);
  if (leading && /exhibit\s*\(\s*s\s*\)/i.test(stem)) {
    const rest = stem
      .replace(/^.*?exhibit\s*\(\s*s\s*\)/i, "")
      .replace(/^[\s_\-\u2010-\u2015]+/, "")
      .trim();
    return rest ? `Exhibit ${leading[1]} — ${rest}` : `Exhibit ${leading[1]}`;
  }
  const underscored = stem.match(/^exhibit[_]+([A-Za-z0-9]{1,4})[_]+(\d{1,4})$/i);
  if (underscored) {
    return `Exhibit ${underscored[1].toUpperCase()}_${underscored[2]}`;
  }
  return null;
}

function resolveHostedUploadLabels(file, { displayName, docKind } = {}) {
  const fname = file?.name || "";
  let resolvedKind = docKind || undefined;
  let resolvedName = displayName || undefined;
  if (!resolvedKind && looksLikeExhibitFilename(fname)) {
    resolvedKind = "exhibit";
  }
  if ((!resolvedName || resolvedName === fname) && looksLikeExhibitFilename(fname)) {
    resolvedName = proposeExhibitDisplayName(fname) || resolvedName || fname;
  }
  return { displayName: resolvedName, docKind: resolvedKind };
}

export async function uploadCaseDocument(
  caseId,
  file,
  { displayName, autoScan = true, docKind, filedDate, waitForComplete = true, onProgress } = {}
) {
  const cs = desktop();
  if (cs) {
    // Desktop uses native file picker; browser File upload writes via temp is not used.
    // Callers in desktop should use pickAndAddDocuments — keep a clear error if File is passed.
    throw new Error("In the desktop app, use Add files to pick documents from your PC.");
  }
  if (isHostedAnalysis()) {
    // Hosted path: scan via /v1/jobs with matter_id; facts persist, PDF deleted server-side.
    // Bulk callers pass waitForComplete:false so one slow/failed job cannot wipe the batch.
    // #region agent log
    const _dbg = (message, data, hypothesisId) => {
      const payload = {
        sessionId: "de3dd9",
        runId: "post-fix",
        hypothesisId,
        location: "api.js:uploadCaseDocument",
        message,
        data,
        timestamp: Date.now(),
      };
      // Local Cursor ingest (works on http://localhost only; mixed-content blocked on HTTPS).
      fetch("http://127.0.0.1:7576/ingest/14fa87bc-56ce-47bf-bc32-89e0afe8ad7c", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "de3dd9" },
        body: JSON.stringify(payload),
      }).catch(() => {});
      // HTTPS-safe sink on analysis (readable via GET /api/agent-debug).
      if (ANALYSIS_API_URL) {
        fetch(`${ANALYSIS_API_URL}/api/agent-debug`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(payload),
        }).catch(() => {});
      }
    };
    _dbg(
      "hosted upload enter (static localFiles import)",
      {
        fileName: file?.name || "",
        hosted: true,
        hasRemember: typeof rememberScanFile === "function",
        hasSha: typeof sha256OfFile === "function",
      },
      "D"
    );
    // #endregion
    onProgress?.("hashing");
    const sha = await sha256OfFile(file);
    onProgress?.("caching");
    await rememberScanFile(file, sha);
    // #region agent log
    _dbg("sha ready, calling createAnalysisJob", { shaPrefix: String(sha || "").slice(0, 12) }, "D");
    // #endregion
    onProgress?.("uploading");
    const labels = resolveHostedUploadLabels(file, {
      displayName: displayName || file.name,
      docKind,
    });
    const job = await createAnalysisJob(file, {
      displayName: labels.displayName || file.name,
      docKind: labels.docKind || undefined,
      matterId: caseId,
      filedDate: filedDate || undefined,
      submittedBy: "web-app",
    });
    if (!waitForComplete) {
      return {
        document: null,
        job_id: job.job_id,
        job: { status: "queued", job_id: job.job_id },
        sha256: sha,
        status: "queued",
        display_name: labels.displayName || file.name,
        original_filename: file.name,
      };
    }
    // Poll until complete so the matter document row exists.
    let progress = null;
    for (let i = 0; i < 120; i += 1) {
      progress = await getJob(job.job_id, { includeReport: true });
      if (progress.status === "complete" || progress.status === "failed") break;
      await new Promise((r) => setTimeout(r, 1000));
    }
    const matter = await getCase(caseId);
    const docs = matter.documents || [];
    const match =
      docs.find((d) => d.sha256 && d.sha256.toLowerCase() === sha.toLowerCase()) ||
      docs[docs.length - 1] ||
      null;
    return {
      document: match,
      job_id: job.job_id,
      job: progress,
      matter,
      sha256: sha,
      status: progress?.status || "queued",
    };
  }
  let lastError = new Error("Upload failed.");
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const body = new FormData();
    body.append("file", file);
    if (displayName) body.append("display_name", displayName);
    body.append("auto_scan", autoScan ? "true" : "false");
    try {
      const res = await apiFetch(`/api/cases/${caseId}/documents`, { method: "POST", body });
      if (res.status === 404) throw vaultUnavailableError();
      return await readJson(res);
    } catch (err) {
      lastError = err;
      if (!isTransientUploadError(err) || attempt === 2) break;
      await new Promise((resolve) => setTimeout(resolve, 600 * (attempt + 1)));
    }
  }
  throw lastError;
}

export async function pickDesktopDocuments(caseId) {
  const cs = desktop();
  if (!cs) throw new Error("Desktop only.");
  return cs.pickAndAddDocuments(caseId);
}

export async function analyzeDesktopDocument(caseId, documentId) {
  const cs = desktop();
  if (!cs) throw new Error("Desktop only.");
  return cs.analyzeDocument(caseId, documentId);
}

export async function getDesktopReport(caseId, documentId) {
  const cs = desktop();
  if (!cs) return null;
  return cs.getLocalReport(caseId, documentId);
}

export async function patchDocument(caseId, documentId, payload) {
  if (isHostedAnalysis()) {
    const res = await analysisFetch(`/api/v1/matters/${caseId}/documents/${documentId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filed_date: payload.filed_date ?? payload.filedDate ?? null,
        display_name: payload.display_name,
        doc_kind: payload.doc_kind,
      }),
    });
    return readJson(res);
  }
  const res = await apiFetch(`/api/cases/${caseId}/documents/${documentId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function retitleCaseDocuments(caseId) {
  if (isHostedAnalysis()) {
    return getCase(caseId);
  }
  const res = await apiFetch(`/api/cases/${caseId}/retitle`, { method: "POST" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function listCaseGotchas(caseId) {
  const cs = desktop();
  if (cs) {
    // Gotchas live inside cached reports; case-level fact conflicts need a packet
    // aggregate because ephemeral /v1/jobs scans never receive case_id.
    const data = await cs.getCase(caseId);
    const gotchas = [];
    const allFindings = [];
    for (const doc of data?.documents || []) {
      const report = await cs.getLocalReport(caseId, doc.id);
      for (const f of report?.findings || []) {
        allFindings.push({ ...f, document_id: doc.id });
        if (f.evidence?.gotcha || f.category === "gotcha") {
          if (f.evidence?.case_impact === "noise" || f.severity === "info") continue;
          gotchas.push({ ...f, document_id: doc.id, title: f.title });
        }
      }
    }
    gotchas.sort((a, b) => {
      const ta = a.evidence?.tainted_support || a.tainted_support ? 0 : 1;
      const tb = b.evidence?.tainted_support || b.tainted_support ? 0 : 1;
      return ta - tb;
    });
    let factConflicts = [];
    let risksElevated = 0;
    try {
      if (typeof cs.listCaseFactConflicts === "function") {
        const packed = await callDesktop(cs.listCaseFactConflicts(caseId));
        factConflicts = packed?.fact_conflicts || packed?.factConflicts || [];
        risksElevated = Number(packed?.elevated) || 0;
      }
    } catch {
      factConflicts = [];
    }
    return {
      gotchas,
      factConflicts,
      risksElevated,
      caseImpact: summarizeCaseImpact(allFindings),
    };
  }
  if (isHostedAnalysis()) {
    const res = await analysisFetch(`/api/v1/matters/${caseId}/findings`);
    const data = await readJson(res);
    const findings = data.findings || [];
    const gotchas = data.gotchas || [];
    const factConflicts = data.fact_conflicts || [];
    // Case impact count must match the filtered board cards (gotchas + conflicts).
    const boardMaterial = [
      ...factConflicts.filter((f) => f.severity !== "info"),
      ...gotchas.filter((f) => f.case_impact !== "noise" && f.severity !== "info"),
    ];
    return {
      gotchas,
      factConflicts,
      risksElevated: factConflicts.filter((f) => f.severity === "high").length,
      caseImpact: {
        ...summarizeCaseImpact(findings),
        material_count: boardMaterial.length,
        material: boardMaterial.slice(0, 12).map((f) => ({
          id: f.id,
          title: f.title,
          severity: f.severity,
          impact: f.case_impact || "material_review",
          bucket: f.case_impact_bucket,
          tainted_support: Boolean(f.tainted_support || f.evidence?.tainted_support),
          analysis_type: f.analysis_type || f.finding_type || f.evidence?.analysis_type,
        })),
      },
    };
  }
  const res = await apiFetch(`/api/cases/${caseId}/gotchas`);
  if (res.status === 404) {
    return { gotchas: [], factConflicts: [], risksElevated: 0, caseImpact: null };
  }
  const data = await readJson(res);
  return {
    gotchas: data.gotchas || [],
    factConflicts: data.fact_conflicts || [],
    risksElevated: Number(data.risks_elevated) || 0,
    caseImpact: data.case_impact || null,
  };
}

function summarizeCaseImpact(findings) {
  const fraudTypes = new Set([
    "math_error",
    "historical_impossibility",
    "template_fabrication",
    "logo_chrome_mismatch",
    "date_inconsistency",
  ]);
  const material = [];
  let noise = 0;
  let fraud = 0;
  for (const f of findings || []) {
    const ev = f.evidence || f;
    const impact = ev.case_impact;
    const sev = f.severity;
    const isNoise = impact === "noise" || sev === "info";
    const isMaterial =
      !isNoise &&
      ["medium", "high", "critical"].includes(sev) &&
      (f.category === "gotcha" ||
        ev.gotcha ||
        fraudTypes.has(ev.analysis_type) ||
        ev.analysis_type === "fact_conflict" ||
        (f.category === "case_consistency" && ev.documentary && ev.case_impact) ||
        ev.tainted_support);
    if (isMaterial) {
      const row = {
        id: f.id,
        title: f.title,
        severity: sev,
        impact: impact || "material_review",
        bucket: ev.case_impact_bucket,
        tainted_support: Boolean(ev.tainted_support),
        analysis_type: ev.analysis_type,
      };
      material.push(row);
      if (ev.tainted_support || fraudTypes.has(ev.analysis_type)) fraud += 1;
    } else if (isNoise || sev === "info") {
      noise += 1;
    }
  }
  material.sort((a, b) => {
    const fa = a.tainted_support || fraudTypes.has(a.analysis_type) ? 0 : 1;
    const fb = b.tainted_support || fraudTypes.has(b.analysis_type) ? 0 : 1;
    return fa - fb;
  });
  return {
    material_count: material.length,
    noise_count: noise,
    fraud_count: fraud,
    material: material.slice(0, 12),
    noise_collapsed: noise,
  };
}

export async function listCaseChat(caseId) {
  const cs = desktop();
  if (cs) {
    return callDesktop(cs.listCaseChat(caseId));
  }
  if (isHostedAnalysis()) return [];
  const res = await apiFetch(`/api/cases/${caseId}/chat`);
  if (res.status === 404) return [];
  const data = await readJson(res);
  return data.messages || [];
}

export async function askCaseChat(caseId, question, { focusDocumentId, documents } = {}) {
  const cs = desktop();
  if (cs) {
    return callDesktop(cs.askCaseChat(caseId, question, focusDocumentId));
  }
  if (isHostedAnalysis()) {
    // Packet chat from client-held excerpts only (no server PDF vault).
    const res = await analysisFetch("/api/v1/case-chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        focus_document_id: focusDocumentId || undefined,
        documents: (documents || []).map((d) => ({
          document_id: d.id || d.document_id,
          display_name: d.display_name,
          original_filename: d.original_filename,
          doc_kind: d.doc_kind || d.doc_type,
          text_excerpt: d.text_excerpt || "",
        })),
      }),
    });
    return readJson(res);
  }
  const res = await apiFetch(`/api/cases/${caseId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      focus_document_id: focusDocumentId || undefined,
    }),
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function scanDocument(caseId, documentId) {
  const cs = desktop();
  if (cs) return cs.analyzeDocument(caseId, documentId);
  if (isHostedAnalysis()) {
    // Hosted scans happen at upload time; reopen stored report.
    const res = await analysisFetch(`/api/v1/matters/${caseId}/documents/${documentId}/report`);
    if (res.status === 404) throw new Error("No stored report for this document. Re-upload to scan.");
    const report = await readJson(res);
    return { job_id: documentId, status: "complete", report };
  }
  const res = await apiFetch(`/api/cases/${caseId}/documents/${documentId}/scan`, {
    method: "POST",
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function rescanCase(caseId, { waitingOnly = false } = {}) {
  const cs = desktop();
  if (cs) {
    const data = await cs.getCase(caseId);
    let docs = data?.documents || [];
    if (waitingOnly) {
      docs = docs.filter((d) => d.status === "uploaded" || d.status === "failed");
    } else {
      // Prefer waiting docs first so a stuck upload pile moves before full re-runs.
      docs = [
        ...docs.filter((d) => d.status === "uploaded" || d.status === "failed"),
        ...docs.filter((d) => d.status !== "uploaded" && d.status !== "failed"),
      ];
    }
    if (!docs.length) {
      return { status: "idle", queued: 0, skipped: 0 };
    }
    let queued = 0;
    let skipped = 0;
    let next = 0;
    const errors = [];
    const concurrency = 2;

    async function worker() {
      while (next < docs.length) {
        const i = next;
        next += 1;
        const doc = docs[i];
        try {
          await cs.analyzeDocument(caseId, doc.id);
          queued += 1;
        } catch (err) {
          skipped += 1;
          if (err?.message) errors.push(err.message);
        }
      }
    }

    await Promise.all(
      Array.from({ length: Math.min(concurrency, docs.length) }, () => worker())
    );
    return {
      status: queued ? "started" : "idle",
      queued,
      skipped,
      error: errors[0] || null,
    };
  }
  if (isHostedAnalysis()) {
    return { status: "idle", queued: 0, skipped: 0, message: "Re-upload a file to rescan on hosted matters." };
  }
  const res = await apiFetch(`/api/cases/${caseId}/rescan`, { method: "POST" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

/**
 * Create an analysis job on the hosted analysis API.
 * Pass matterId to persist facts and compare against prior filings in that matter.
 * Requires an active control-plane session (Bearer) or VITE_ANALYSIS_API_KEY.
 */
export async function createAnalysisJob(
  file,
  { displayName, docKind, submittedBy = "web-app", matterId, caseId, filedDate } = {}
) {
  const labels = resolveHostedUploadLabels(file, { displayName, docKind });
  const body = new FormData();
  body.append("file", file);
  body.append("submitted_by", submittedBy);
  if (labels.displayName) body.append("display_name", labels.displayName);
  if (labels.docKind) body.append("doc_kind", labels.docKind);
  const mid = matterId || caseId;
  if (mid) body.append("matter_id", mid);
  if (filedDate) body.append("filed_date", filedDate);
  const res = await analysisFetch("/api/v1/jobs", { method: "POST", body });
  return readJson(res);
}

export async function getMatterDocumentReport(matterId, documentId) {
  const res = await analysisFetch(`/api/v1/matters/${matterId}/documents/${documentId}/report`);
  return readJson(res);
}

export async function exportMatter(matterId) {
  const res = await analysisFetch(`/api/v1/matters/${matterId}/export`);
  return readJson(res);
}

export async function getEntitlement() {
  const cs = desktop();
  if (cs) return callDesktop(cs.entitlement());

  absorbSessionHandoff();

  // Local vault / Vite proxy (no remote analysis URL): keep open/license mode for John.
  if (!ANALYSIS_API_URL) {
    try {
      const res = await apiFetch("/api/entitlement");
      if (res.ok) {
        const data = await readJson(res);
        if (data.mode === "open" || data.mode === "license") return data;
      }
    } catch {
      /* fall through to control-plane cloud path */
    }
  }

  const token = getSessionToken();
  if (!token) {
    return {
      active: false,
      mode: "cloud",
      plan: null,
      message: "Sign in to activate your subscription.",
      hasSession: false,
      billing_state: "none",
      trial_days_left: null,
      share_anonymized_outcomes: true,
    };
  }
  try {
    const res = await fetch(controlUrl("/v1/entitlement"), { headers: controlHeaders() });
    if (!res.ok) {
      const detail = await safeDetail(res);
      if (isSessionAuthFailure(res, detail)) {
        clearSession();
        return {
          active: false,
          mode: "cloud",
          plan: null,
          message: "Session expired. Sign in again.",
          hasSession: false,
          billing_state: "none",
          trial_days_left: null,
          share_anonymized_outcomes: true,
        };
      }
      throw new Error(detail || "Request failed.");
    }
    const data = await res.json();
    let email = data.email;
    if (!email) {
      try {
        email = localStorage.getItem(SESSION_EMAIL_KEY) || undefined;
      } catch {
        email = undefined;
      }
    }
    if (email) setSession(token, email);
    return {
      ...data,
      mode: "cloud",
      hasSession: true,
      email,
      // Soft-launch: anonymized finding outcomes are always on for cloud users.
      share_anonymized_outcomes: true,
    };
  } catch (err) {
    const raw = String(err?.message || err || "");
    if (isSessionAuthFailure(null, raw)) {
      clearSession();
      return {
        active: false,
        mode: "cloud",
        plan: null,
        message: "Session expired. Sign in again.",
        hasSession: false,
        billing_state: "none",
        trial_days_left: null,
        share_anonymized_outcomes: true,
      };
    }
    // True network / CORS failure only (not 401). Keep token + email; Checkout will
    // surface a network error — never mis-label auth failure as "unreachable".
    let email;
    try {
      email = localStorage.getItem(SESSION_EMAIL_KEY) || undefined;
    } catch {
      email = undefined;
    }
    return {
      active: false,
      mode: "cloud",
      plan: "cloud-cached",
      hasSession: Boolean(getSessionToken()),
      email,
      message: "Control plane unreachable. Sign in when online.",
      cached: true,
      share_anonymized_outcomes: true,
    };
  }
}

function controlUnreachableError(cause) {
  const raw = String(cause?.message || cause || "");
  if (/failed to fetch|networkerror|load failed/i.test(raw)) {
    return new Error(
      `Control plane unreachable (${CONTROL_PLANE_URL}). Confirm VITE_CONTROL_PLANE_URL at build time and that casesifter-control is live.`
    );
  }
  return cause instanceof Error ? cause : new Error(raw || "Control plane request failed.");
}

export async function loginCloud(email, password) {
  const cs = desktop();
  if (cs) {
    await callDesktop(cs.login(email, password));
    return callDesktop(cs.entitlement());
  }
  let res;
  try {
    res = await fetch(controlUrl("/v1/login"), {
      method: "POST",
      headers: { ...jsonHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  const data = await readJson(res);
  if (!data?.token) throw new Error("Login returned no session token.");
  setSession(data.token, email);
  return getEntitlement();
}

export async function signupCloud(email, password, name, acceptedTerms, inviteCode) {
  const cs = desktop();
  if (cs) {
    await callDesktop(cs.signup(email, password, name, acceptedTerms, inviteCode));
    return callDesktop(cs.entitlement());
  }
  if (!acceptedTerms) {
    throw new Error("Agree to the Terms of Service and Privacy Policy to continue.");
  }
  if (!password || password.length < 6) {
    throw new Error("Password must be at least 6 characters.");
  }
  let res;
  try {
    res = await fetch(controlUrl("/v1/signup"), {
      method: "POST",
      headers: { ...jsonHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        name: name || null,
        accepted_terms: Boolean(acceptedTerms),
        invite_code: inviteCode || null,
      }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  const data = await readJson(res);
  if (!data?.token) throw new Error("Signup returned no session token.");
  setSession(data.token, email);
  return getEntitlement();
}

export async function reportProblem(note) {
  const cs = desktop();
  if (!cs?.reportProblem) {
    throw new Error("Report a problem is available in the desktop app.");
  }
  return callDesktop(cs.reportProblem(note));
}

export async function logoutCloud() {
  const cs = desktop();
  if (cs) {
    await callDesktop(cs.setConfig({ sessionToken: "" }));
    return { active: false, mode: "cloud", message: "Signed out." };
  }
  clearSession();
  return {
    active: false,
    mode: "cloud",
    plan: null,
    hasSession: false,
    message: "Signed out.",
    billing_state: "none",
    trial_days_left: null,
  };
}

async function controlAuthedJson(path, options = {}) {
  const token = getSessionToken();
  if (!token) throw new Error("Sign in first.");
  let res;
  try {
    res = await fetch(controlUrl(path), {
      ...options,
      headers: { ...controlHeaders(), ...(options.headers || {}) },
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  if (!res.ok) {
    const detail = await safeDetail(res);
    if (isSessionAuthFailure(res, detail)) {
      clearSession();
      throw sessionExpiredError();
    }
    throw new Error(detail || "Request failed.");
  }
  return res.json();
}

export async function startCheckout(trial = false, quantity = 1) {
  const seats = Math.min(50, Math.max(1, Number(quantity) || 1));
  const cs = desktop();
  if (cs) return callDesktop(cs.checkout(trial, seats));
  return controlAuthedJson("/v1/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trial: Boolean(trial), quantity: seats }),
  });
}

export async function openBillingPortal() {
  const cs = desktop();
  if (cs?.billingPortal) return callDesktop(cs.billingPortal());
  return controlAuthedJson("/v1/billing-portal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
}

export async function saveLicense(licenseKey) {
  const res = await apiFetch("/api/entitlement/license", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ license_key: licenseKey }),
  });
  return readJson(res);
}

/** Soft-launch: anonymized outcomes are always on; UI toggle removed. Desktop API kept as no-op. */
export async function setShareOutcomes(_enabled) {
  if (desktop()) return getEntitlement();
  const ent = await getEntitlement();
  return { ...ent, share_anonymized_outcomes: true };
}

export async function sendFindingFeedback(payload) {
  const cs = desktop();
  if (cs) {
    return cs.findingFeedback(payload);
  }
  const res = await apiFetch("/api/telemetry/finding-feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 404) return { recorded: false };
  return readJson(res);
}

export function isDesktopApp() {
  return Boolean(desktop());
}

export function controlPlaneUrl() {
  return CONTROL_PLANE_URL;
}

export function analysisApiUrl() {
  return ANALYSIS_API_URL;
}
