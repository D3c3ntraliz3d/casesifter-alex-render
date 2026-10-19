import { rememberScanFile, sha256OfFile } from "./localFiles.js";
import { isAllowedStripeUrl } from "./stripeUrl.js";

const jsonHeaders = { Accept: "application/json" };

/** Soft-launch hosted URLs (Vite build-time). Fallbacks keep local + marketing signup working. */
const CONTROL_PLANE_URL = String(
  import.meta.env.VITE_CONTROL_PLANE_URL || "https://casesifter-control.onrender.com"
).replace(/\/$/, "");
const ANALYSIS_API_URL = String(import.meta.env.VITE_ANALYSIS_API_URL || "").replace(/\/$/, "");
const ANALYSIS_API_KEY = String(import.meta.env.VITE_ANALYSIS_API_KEY || "").trim();

const SESSION_TOKEN_KEY = "casesifter_session_token";
const SESSION_EMAIL_KEY = "casesifter_email";
const SESSION_FLAG = "casesifter_signed_in";

function desktop() {
  return typeof window !== "undefined" && window.casesifter?.isDesktop ? window.casesifter : null;
}

function getSessionToken() {
  // Browser sessions are HttpOnly cookies. Never read a bearer out of storage.
  return "";
}

function rememberEmail(email) {
  try {
    if (email) sessionStorage.setItem(SESSION_EMAIL_KEY, email);
  } catch {
    /* private mode */
  }
}

function storedEmail() {
  try {
    return sessionStorage.getItem(SESSION_EMAIL_KEY) || "";
  } catch {
    return "";
  }
}

function markSignedIn(email) {
  try {
    sessionStorage.setItem(SESSION_FLAG, "1");
    localStorage.removeItem(SESSION_TOKEN_KEY);
    localStorage.removeItem(SESSION_EMAIL_KEY);
  } catch {
    /* private mode */
  }
  rememberEmail(email);
}

function hasSessionHint() {
  try {
    return sessionStorage.getItem(SESSION_FLAG) === "1";
  } catch {
    return false;
  }
}

function clearSession() {
  try {
    sessionStorage.removeItem(SESSION_FLAG);
    sessionStorage.removeItem(SESSION_EMAIL_KEY);
    localStorage.removeItem(SESSION_TOKEN_KEY);
    localStorage.removeItem(SESSION_EMAIL_KEY);
  } catch {
    /* private mode */
  }
}

async function acceptAnalysisSession(code) {
  if (!code || !ANALYSIS_API_URL) return;
  await fetch(`${ANALYSIS_API_URL}/api/v1/session/accept`, {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ code }),
  });
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
 * Absorb marketing→app session handoff via one-time ?cs_code= (60s exchange).
 * Legacy ?cs_token= is ignored (tokens must not land in history/logs).
 */
export function absorbSessionHandoff() {
  if (typeof window === "undefined") return false;
  try {
    const params = new URLSearchParams(window.location.search || "");
    let code = (params.get("cs_code") || "").trim();
    const hash = (window.location.hash || "").replace(/^#/, "");
    if (!code && hash) {
      const hp = new URLSearchParams(hash);
      code = (hp.get("cs_code") || "").trim();
    }
    // Strip any legacy token params from the URL without accepting them.
    params.delete("cs_token");
    params.delete("token");
    params.delete("cs_email");
    params.delete("email");
    params.delete("cs_code");
    const qs = params.toString();
    const next = `${window.location.pathname}${qs ? `?${qs}` : ""}`;
    if (code) {
      // Fire-and-forget exchange; caller may also await exchangeSessionCode.
      exchangeSessionCode(code).catch(() => {});
    }
    if (window.location.search || (hash && hash.includes("cs_"))) {
      window.history.replaceState({}, "", next || window.location.pathname);
    }
    return Boolean(code);
  } catch {
    return false;
  }
}

export async function exchangeSessionCode(code) {
  const res = await fetch(controlUrl("/v1/session/exchange"), {
    method: "POST",
    credentials: "include",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  const data = await readJson(res);
  if (data?.analysis_code) await acceptAnalysisSession(data.analysis_code);
  markSignedIn(data?.entitlement?.email);
  return data;
}

/** Open Stripe Checkout / Customer Portal URL in this tab (reliable after await). */
export function openCheckoutUrl(url) {
  if (!isAllowedStripeUrl(url)) return false;
  const target = new URL(url).href;
  try {
    window.location.assign(target);
    return true;
  } catch {
    try {
      window.open(target, "_blank", "noopener,noreferrer");
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
  // Never bake ops API keys into the hosted browser client — that bypasses
  // firm tenancy (shared ops firm / cross-job reads). Desktop/local vault only.
  if (ANALYSIS_API_KEY && !ANALYSIS_API_URL) {
    headers["X-API-Key"] = ANALYSIS_API_KEY;
  }
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
  return fetch(apiUrl(path), { ...options, headers, credentials: "include" });
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
    const detail = data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (typeof item === "string" ? item : item?.msg || JSON.stringify(item)))
        .filter(Boolean)
        .join("; ");
    }
    if (detail != null) return JSON.stringify(detail);
    return data?.message || JSON.stringify(data);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

/** Map login response entitlement into the shape AccountPanel expects. */
function entitlementFromLogin(data, fallbackEmail) {
  const raw = data?.entitlement;
  if (!raw || typeof raw !== "object") return null;
  const email = raw.email || fallbackEmail || undefined;
  return {
    ...raw,
    mode: "cloud",
    hasSession: true,
    email,
    share_anonymized_outcomes: true,
  };
}

async function sessionCookieWorks() {
  try {
    const res = await fetch(controlUrl("/v1/entitlement"), {
      headers: controlHeaders(),
      credentials: "include",
    });
    return res.ok;
  } catch {
    return false;
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

/** Vault-only direct link (local APP_MODE=vault). Prefer downloadExportReport. */
export function exportUrl(jobId, kind) {
  return apiUrl(`/api/jobs/${jobId}/export/${kind}`);
}

/**
 * Credentialed export download. Hosted analysis regenerates from durable report
 * via /api/v1/jobs/.../export; local vault still uses /api/jobs/.../export.
 */
export async function downloadExportReport(jobId, kind) {
  const k = String(kind || "").toLowerCase();
  if (!["html", "pdf", "json"].includes(k)) {
    throw new Error("Export kind must be html, pdf, or json.");
  }
  const path = ANALYSIS_API_URL
    ? `/api/v1/jobs/${jobId}/export/${k}`
    : `/api/jobs/${jobId}/export/${k}`;
  const res = ANALYSIS_API_URL
    ? await analysisFetch(path)
    : await apiFetch(path);
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `Export failed (${res.status}).`);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const match = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(cd);
  const filename = match
    ? decodeURIComponent(match[1].replace(/"/g, "").trim())
    : `exhibit-report-${jobId}.${k}`;
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
  return { filename, size: blob.size };
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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** True for transport blips / cold starts worth retrying with backoff. */
function isRetryableAnalysisFailure(err, status = 0) {
  if (status === 503 || status === 502 || status === 504) return true;
  const msg = String(err?.message || err || "");
  return /failed to fetch|networkerror|load failed|503|502|504|service suspended|analysis (service )?unreachable|analysis is temporarily unavailable/i.test(
    msg
  );
}

async function analysisFetch(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  // Only idempotent reads retry on transport/503. POSTs like recompute-facts
  // must surface the real error (and must not hammer a crashing instance).
  const retrySafe = method === "GET" || method === "HEAD";
  const maxAttempts = retrySafe ? 4 : 1;
  let lastErr = null;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const res = await apiFetch(path, options);
      if (res.status === 503 || res.status === 502 || res.status === 504) {
        lastErr = analysisUnreachableError(new Error(String(res.status)));
        if (retrySafe && attempt < maxAttempts - 1) {
          await sleep(400 * 2 ** attempt);
          continue;
        }
        throw lastErr;
      }
      return res;
    } catch (err) {
      lastErr = analysisUnreachableError(err);
      if (retrySafe && attempt < maxAttempts - 1 && isRetryableAnalysisFailure(err)) {
        await sleep(400 * 2 ** attempt);
        continue;
      }
      throw lastErr;
    }
  }
  throw lastErr || analysisUnreachableError(new Error("Analysis request failed."));
}

/** Job was wiped by a restart, or the durable ledger reports interrupted. */
export function isInterruptedOrExpiredJob(progressOrErr) {
  if (!progressOrErr) return false;
  if (typeof progressOrErr === "object" && !(progressOrErr instanceof Error)) {
    const status = String(progressOrErr.status || "").toLowerCase();
    if (status === "interrupted" || progressOrErr.interrupted) return true;
  }
  const msg = String(progressOrErr?.message || progressOrErr || "").toLowerCase();
  return (
    /unknown or expired job|interrupted_by_restart|job was interrupted|instance restarted/.test(
      msg
    ) || String(progressOrErr?.status || "").toLowerCase() === "interrupted"
  );
}

export async function listCases() {
  const cs = desktop();
  if (cs) return cs.listCases();
  if (isHostedAnalysis()) {
    // Fail closed: hosted matters require a firm session cookie.
    if (!hasSessionHint()) {
      throw new Error("Sign in to view your firm’s cases.");
    }
    const res = await analysisFetch("/api/v1/matters");
    if (res.status === 401 || res.status === 403) {
      clearSession();
      throw new Error("Sign in to view your firm’s cases.");
    }
    const data = await readJson(res);
    return data.matters || data.cases || [];
  }
  const res = await apiFetch("/api/cases");
  if (res.status === 404) return [];
  const data = await readJson(res);
  return data.cases || [];
}

export async function listAccessLog() {
  const signins = await controlAuthedJson("/v1/audit");
  let matterEvents = [];
  let matterAuditDenied = false;
  if (isHostedAnalysis()) {
    try {
      const res = await analysisFetch("/api/v1/matters/audit");
      if (res.status === 403) {
        matterAuditDenied = true;
      } else {
        const data = await readJson(res);
        matterEvents = data.events || [];
      }
    } catch (err) {
      // Firm matter log is owner/admin only — do not wipe personal sign-in rows.
      if (/403|forbidden|firm admin/i.test(String(err?.message || err || ""))) {
        matterAuditDenied = true;
      } else {
        throw err;
      }
    }
  }
  return {
    sign_in_events: signins.events || [],
    matter_events: matterEvents,
    matter_audit_denied: matterAuditDenied,
  };
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
    const body = {
      name: payload?.caption ?? payload?.name,
      caption: payload?.caption ?? payload?.name,
      index_number: payload?.index_number,
      court: payload?.court,
      county: payload?.county,
    };
    const res = await analysisFetch(`/api/v1/matters/${caseId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return readJson(res);
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
    onProgress?.("hashing");
    const sha = await sha256OfFile(file);
    onProgress?.("caching");
    await rememberScanFile(file, sha);
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
        job: {
          status: "queued",
          job_id: job.job_id,
          index_warning: job.index_warning || null,
          filename_index: job.filename_index || null,
          matter_index: job.matter_index || null,
        },
        sha256: sha,
        status: "queued",
        display_name: labels.displayName || file.name,
        original_filename: file.name,
        index_warning: job.index_warning || null,
        filename_index: job.filename_index || null,
        matter_index: job.matter_index || null,
      };
    }
    // Poll until complete so the matter document row exists.
    let progress = null;
    let jobId = job.job_id;
    let resubmitted = false;
    for (let i = 0; i < 120; i += 1) {
      try {
        progress = await getJob(jobId, { includeReport: true });
      } catch (err) {
        if (!resubmitted && isInterruptedOrExpiredJob(err)) {
          resubmitted = true;
          const again = await createAnalysisJob(file, {
            displayName: labels.displayName || file.name,
            docKind: labels.docKind || undefined,
            matterId: caseId,
            filedDate: filedDate || undefined,
            submittedBy: "web-app",
          });
          jobId = again.job_id;
          continue;
        }
        throw err;
      }
      if (progress.status === "interrupted" || progress.interrupted) {
        if (!resubmitted) {
          resubmitted = true;
          const again = await createAnalysisJob(file, {
            displayName: labels.displayName || file.name,
            docKind: labels.docKind || undefined,
            matterId: caseId,
            filedDate: filedDate || undefined,
            submittedBy: "web-app",
          });
          jobId = again.job_id;
          continue;
        }
        break;
      }
      if (progress.status === "complete" || progress.status === "failed") break;
      await sleep(1000);
    }
    const matter = await getCase(caseId);
    const docs = matter.documents || [];
    const match =
      docs.find((d) => d.sha256 && d.sha256.toLowerCase() === sha.toLowerCase()) ||
      docs[docs.length - 1] ||
      null;
    return {
      document: match,
      job_id: jobId,
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
  const cs = desktop();
  if (cs?.patchDocument) {
    return callDesktop(cs.patchDocument(caseId, documentId, payload));
  }
  if (isHostedAnalysis()) {
    const body = {};
    if (
      Object.prototype.hasOwnProperty.call(payload, "filed_date") ||
      Object.prototype.hasOwnProperty.call(payload, "filedDate")
    ) {
      body.filed_date = payload.filed_date ?? payload.filedDate ?? null;
    }
    if (Object.prototype.hasOwnProperty.call(payload, "display_name")) {
      body.display_name = payload.display_name;
    }
    if (Object.prototype.hasOwnProperty.call(payload, "doc_kind")) {
      body.doc_kind = payload.doc_kind;
    }
    const res = await analysisFetch(`/api/v1/matters/${caseId}/documents/${documentId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
    const res = await analysisFetch(`/api/v1/matters/${caseId}/retitle`, { method: "POST" });
    await readJson(res);
    return getCase(caseId);
  }
  const res = await apiFetch(`/api/cases/${caseId}/retitle`, { method: "POST" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function listRetryableJobs(matterId) {
  if (!isHostedAnalysis()) return { jobs: [] };
  const res = await analysisFetch(`/api/v1/matters/${matterId}/retryable-jobs`);
  if (!res.ok) return { jobs: [] };
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
          const ev = f.evidence || {};
          gotchas.push({
            ...f,
            document_id: doc.id,
            title: f.title,
            // Flatten nested evidence so Case Impact expand can render either shape.
            explanation: f.explanation,
            why_suspicious: f.why_suspicious || ev.why_suspicious,
            why_it_may_matter: f.why_it_may_matter || f.explanation,
            attorney_why: f.attorney_why || ev.attorney_why,
            claim_quote: f.claim_quote || ev.claim_quote,
            claim_source: f.claim_source || ev.claim_source,
            evidence_quote: f.evidence_quote || ev.evidence_quote || ev.quoted_evidence,
            evidence_source: f.evidence_source || ev.evidence_source,
            evidence_label: f.evidence_label || ev.evidence_label,
            materiality_reason: f.materiality_reason || ev.materiality_reason,
            fact_named: f.fact_named || ev.fact_named,
            case_impact: f.case_impact || ev.case_impact,
            case_impact_bucket: f.case_impact_bucket || ev.case_impact_bucket,
            tainted_support: Boolean(f.tainted_support || ev.tainted_support),
            self_undercut: Boolean(f.self_undercut || ev.self_undercut),
            page: f.page ?? ev.page ?? ev.evidence_page,
          });
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

/** Hosted re-scan: submit cached bytes to /v1/jobs (PDFs are not on the server). */
async function hostedRescanDocument(caseId, doc) {
  const sha = String(doc?.sha256 || "").toLowerCase();
  if (!sha) {
    throw new Error("This document has no hash. Use Locate or re-upload the file.");
  }
  const { localCacheAdapter } = await import("./storage/localCache.js");
  const blob = await localCacheAdapter.getFile(doc.id, sha);
  if (!blob) {
    throw new Error(
      `“${doc.display_name || doc.original_filename || "File"}” is not cached on this computer. Use Locate or re-upload to re-scan.`
    );
  }
  const name =
    doc.original_filename ||
    doc.display_name ||
    `rescan-${sha.slice(0, 12)}.pdf`;
  const file = new File([blob], name, { type: blob.type || "application/pdf" });
  const job = await createAnalysisJob(file, {
    matterId: caseId,
    displayName: doc.display_name,
    docKind: doc.doc_kind || doc.doc_type,
    filedDate: doc.filed_date || doc.filedDate || undefined,
    submittedBy: "web-app-rescan",
  });
  return { job_id: job.job_id, status: job.status || "queued", queued: true };
}

export async function scanDocument(caseId, documentId, docHint = null) {
  const cs = desktop();
  if (cs) return cs.analyzeDocument(caseId, documentId);
  if (isHostedAnalysis()) {
    let doc = docHint;
    if (!doc || !doc.sha256) {
      const matter = await getCase(caseId);
      doc = (matter.documents || []).find((d) => d.id === documentId);
    }
    if (!doc) {
      throw new Error("Document not found on this matter.");
    }
    return hostedRescanDocument(caseId, doc);
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
    const started = [];
    const concurrency = 2;

    async function worker() {
      while (next < docs.length) {
        const i = next;
        next += 1;
        const doc = docs[i];
        try {
          await cs.analyzeDocument(caseId, doc.id);
          queued += 1;
          started.push({ document_id: doc.id });
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
      started,
      error: errors[0] || null,
    };
  }
  if (isHostedAnalysis()) {
    const matter = await getCase(caseId);
    let docs = matter.documents || [];
    if (waitingOnly) {
      docs = docs.filter((d) => d.status === "uploaded" || d.status === "failed");
    }
    if (!docs.length) {
      return { status: "idle", queued: 0, skipped: 0, missing: [] };
    }
    let queued = 0;
    const missing = [];
    const errors = [];
    const started = [];
    for (const doc of docs) {
      try {
        const job = await hostedRescanDocument(caseId, doc);
        queued += 1;
        started.push({ document_id: doc.id, job_id: job?.job_id || null });
      } catch (err) {
        const msg = String(err?.message || err || "");
        if (/not cached|Locate or re-upload/i.test(msg)) {
          missing.push(doc.display_name || doc.original_filename || doc.id);
        } else {
          errors.push(`${doc.display_name || doc.id}: ${msg}`);
        }
      }
    }
    return {
      status: queued ? "started" : "idle",
      queued,
      skipped: missing.length + errors.length,
      missing,
      started,
      error: errors[0] || (missing.length ? `${missing.length} file(s) need Locate / re-upload.` : null),
    };
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

/** Start async recompute; polls until complete. onProgress({status,percent,message,...}). */
export async function recomputeMatterFacts(matterId, { onProgress } = {}) {
  const res = await analysisFetch(`/api/v1/matters/${matterId}/recompute-facts`, {
    method: "POST",
  });
  const started = await readJson(res);
  if (res.status !== 202 || !started?.job_id) {
    return started;
  }
  const jobId = started.job_id;
  if (typeof onProgress === "function") onProgress(started);
  for (let i = 0; i < 900; i += 1) {
    await sleep(1000);
    const poll = await analysisFetch(
      `/api/v1/matters/${matterId}/recompute-facts/${jobId}`
    );
    const row = await readJson(poll);
    if (typeof onProgress === "function") onProgress(row);
    if (row.status === "complete" || row.status === "failed") return row;
  }
  throw new Error("Recompute timed out waiting for server job.");
}

export async function exportMatter(matterId) {
  const res = await analysisFetch(`/api/v1/matters/${matterId}/export`);
  return readJson(res);
}

/** Credentialed matter review pack download (JSON). Hosted path only. */
export async function downloadMatterExport(matterId, { filename } = {}) {
  const payload = await exportMatter(matterId);
  const caption =
    payload?.matter?.caption ||
    payload?.matter?.name ||
    String(matterId || "matter").slice(0, 12);
  const safe = String(caption)
    .replace(/[^\w\-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 48) || "matter";
  const name = filename || `casesifter-${safe}-export.json`;
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
  return payload;
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

  try {
    const res = await fetch(controlUrl("/v1/entitlement"), {
      headers: controlHeaders(),
      credentials: "include",
    });
    if (!res.ok) {
      const detail = await safeDetail(res);
      if (isSessionAuthFailure(res, detail)) {
        const expired = hasSessionHint();
        clearSession();
        return {
          active: false,
          mode: "cloud",
          plan: null,
          message: expired ? "Session expired. Sign in again." : "Sign in to activate your subscription.",
          hasSession: false,
          billing_state: "none",
          trial_days_left: null,
          share_anonymized_outcomes: true,
        };
      }
      throw new Error(detail || "Request failed.");
    }
    const data = await res.json();
    const email = data.email || storedEmail() || undefined;
    if (email) markSignedIn(email);
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
    const email = storedEmail() || undefined;
    return {
      active: false,
      mode: "cloud",
      plan: "cloud-cached",
      hasSession: hasSessionHint(),
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

/** Request a password-reset email (always returns ok; does not reveal if account exists). */
export async function requestPasswordReset(email) {
  const addr = String(email || "").trim().toLowerCase();
  if (!addr.includes("@")) throw new Error("Enter a valid work email.");
  let res;
  try {
    res = await fetch(controlUrl("/v1/password-reset/request"), {
      method: "POST",
      credentials: "include",
      headers: { ...jsonHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ email: addr }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  const data = await readJson(res);
  return data;
}

/** Confirm reset with token from email link (marketing reset.html or app ?reset_token=). */
export async function confirmPasswordReset(token, password) {
  if (!token || String(token).length < 8) throw new Error("Invalid or missing reset token.");
  if (!password || password.length < 8) throw new Error("Password must be at least 8 characters.");
  let res;
  try {
    res = await fetch(controlUrl("/v1/password-reset/confirm"), {
      method: "POST",
      credentials: "include",
      headers: { ...jsonHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ token, password }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  const data = await readJson(res);
  if (data?.mfa_required && data?.mfa_token) {
    return {
      mfa_required: true,
      mfa_token: data.mfa_token,
      email: data.email,
      message: data.message || "Password updated. Enter your authenticator code.",
    };
  }
  if (data?.analysis_code) await acceptAnalysisSession(data.analysis_code);
  if (data?.email || data?.entitlement?.email) {
    markSignedIn(data.email || data.entitlement.email);
  }
  return getEntitlement();
}

async function finishBrowserLogin(data, email) {
  if (data?.analysis_code) {
    try {
      await acceptAnalysisSession(data.analysis_code);
    } catch {
      /* analysis cookie is best-effort; control session is the source of truth */
    }
  }
  const addr = data?.entitlement?.email || email;
  markSignedIn(addr);

  const fromLogin = entitlementFromLogin(data, addr);
  const cookieOk = await sessionCookieWorks();
  if (cookieOk) {
    try {
      return await getEntitlement();
    } catch {
      if (fromLogin) return fromLogin;
      throw new Error("Signed in, but entitlement refresh failed. Reload the page.");
    }
  }

  // Login POST succeeded but Set-Cookie did not stick (third-party / partitioned
  // cookie block in embedded browsers). Prefer a same-window handoff code when
  // control minted one; otherwise surface a clear cookies-blocked error.
  const handoff = String(data?.handoff_code || "").trim();
  if (handoff && typeof window !== "undefined") {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("cs_code", handoff);
      window.history.replaceState({}, "", `${url.pathname}${url.search}`);
      await exchangeSessionCode(handoff);
      if (await sessionCookieWorks()) return getEntitlement();
    } catch {
      /* fall through to cookies-blocked message */
    }
  }

  throw new Error(
    "Sign-in succeeded, but this browser blocked the cross-site session cookie. " +
      "Open this app in a normal browser tab (not an embedded/automated browser) and sign in again."
  );
}

export async function loginCloud(email, password) {
  const cs = desktop();
  if (cs) {
    await callDesktop(cs.login(email, password));
    return callDesktop(cs.entitlement());
  }
  const addr = String(email || "").trim().toLowerCase();
  let res;
  try {
    res = await fetch(controlUrl("/v1/login"), {
      method: "POST",
      credentials: "include",
      headers: { ...jsonHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ email: addr, password }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  const data = await readJson(res);
  if (data?.mfa_required && data?.mfa_token) {
    return {
      mfa_required: true,
      mfa_token: data.mfa_token,
      email: data.email || addr,
      message: data.message || "Enter your authenticator code.",
    };
  }
  return finishBrowserLogin(data, addr);
}

export async function loginMfaCloud(mfaToken, code) {
  let res;
  try {
    res = await fetch(controlUrl("/v1/login/mfa"), {
      method: "POST",
      credentials: "include",
      headers: { ...jsonHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ mfa_token: mfaToken, code }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  const data = await readJson(res);
  return finishBrowserLogin(data, data?.entitlement?.email);
}

export function ssoStartUrl() {
  return controlUrl("/v1/sso/oidc/start");
}

export async function fetchAuthStatus() {
  try {
    const res = await fetch(controlUrl("/v1/auth/status"), {
      credentials: "include",
      headers: { ...jsonHeaders },
    });
    if (!res.ok) return { sso_available: false, totp_available: false };
    return readJson(res);
  } catch {
    return { sso_available: false, totp_available: false };
  }
}

export async function startTotpEnrollment() {
  return controlAuthedJson("/v1/2fa/totp/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

export async function confirmTotpEnrollment(code) {
  return controlAuthedJson("/v1/2fa/totp/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
}

export async function disableTotp(password, code) {
  return controlAuthedJson("/v1/2fa/totp/disable", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password, code }),
  });
}

export async function fetchAdminOverview() {
  return controlAuthedJson("/v1/admin/overview");
}

export async function fetchAdminAudit() {
  return controlAuthedJson("/v1/admin/audit");
}

export async function revokeAdminSession(sessionId) {
  return controlAuthedJson(`/v1/admin/sessions/${encodeURIComponent(sessionId)}/revoke`, {
    method: "POST",
    body: "{}",
  });
}

export async function revokeOtherSessions() {
  return controlAuthedJson("/v1/admin/sessions/revoke-others", {
    method: "POST",
    body: "{}",
  });
}

export async function patchAdminSettings(body) {
  return controlAuthedJson("/v1/admin/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

export async function createSeatInvite(email, role = "member") {
  return controlAuthedJson("/v1/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role }),
  });
}

export async function revokeSeatInvite(inviteId) {
  return controlAuthedJson(`/v1/admin/invites/${encodeURIComponent(inviteId)}`, {
    method: "DELETE",
  });
}

export async function setMemberRole(userId, role) {
  const out = await controlAuthedJson(`/v1/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (isHostedAnalysis()) {
    try {
      const res = await analysisFetch(`/api/v1/matters/admin/members/${encodeURIComponent(userId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role }),
      });
      await readJson(res);
    } catch {
      /* control-plane role is source of truth; analysis syncs on next request */
    }
  }
  return out;
}

export async function removeOrgMember(userId) {
  const out = await controlAuthedJson(`/v1/admin/users/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
  if (isHostedAnalysis()) {
    try {
      const res = await analysisFetch(`/api/v1/matters/admin/members/${encodeURIComponent(userId)}`, {
        method: "DELETE",
      });
      await readJson(res);
    } catch {
      /* CP detach already revoked sessions; analysis detach best-effort */
    }
  }
  return out;
}

export async function revokeMemberSessions(userId) {
  return controlAuthedJson(
    `/v1/admin/users/${encodeURIComponent(userId)}/sessions/revoke`,
    { method: "POST", body: "{}" }
  );
}

export async function revokeFirmSessions() {
  return controlAuthedJson("/v1/admin/sessions/revoke-firm", {
    method: "POST",
    body: "{}",
  });
}

/** Read ?seat_invite= from URL once (Admin invite link → Account signup). */
export function absorbSeatInviteToken() {
  if (typeof window === "undefined") return "";
  try {
    const params = new URLSearchParams(window.location.search || "");
    const token = (params.get("seat_invite") || "").trim();
    if (!token) return "";
    params.delete("seat_invite");
    const qs = params.toString();
    window.history.replaceState(
      {},
      "",
      `${window.location.pathname}${qs ? `?${qs}` : ""}`
    );
    try {
      sessionStorage.setItem("cs_seat_invite", token);
    } catch {
      /* ignore */
    }
    return token;
  } catch {
    return "";
  }
}

export function peekSeatInviteToken() {
  try {
    return (
      (typeof sessionStorage !== "undefined" && sessionStorage.getItem("cs_seat_invite")) ||
      ""
    ).trim();
  } catch {
    return "";
  }
}

export function clearSeatInviteToken() {
  try {
    sessionStorage.removeItem("cs_seat_invite");
  } catch {
    /* ignore */
  }
}

export async function fetchFirmAdminOverview() {
  if (!isHostedAnalysis()) {
    return null;
  }
  const res = await analysisFetch("/api/v1/matters/admin");
  return readJson(res);
}

export async function patchFirmAdminSettings(body) {
  if (!isHostedAnalysis()) {
    throw new Error("Firm settings require hosted analysis.");
  }
  const res = await analysisFetch("/api/v1/matters/admin/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return readJson(res);
}

export async function signupCloud(email, password, name, acceptedTerms, inviteCode, seatInviteToken) {
  const cs = desktop();
  if (cs) {
    await callDesktop(cs.signup(email, password, name, acceptedTerms, inviteCode));
    return callDesktop(cs.entitlement());
  }
  if (!acceptedTerms) {
    throw new Error("Agree to the Terms of Service and Privacy Policy to continue.");
  }
  if (!password || password.length < 8) {
    throw new Error("Password must be at least 8 characters.");
  }
  let res;
  try {
    res = await fetch(controlUrl("/v1/signup"), {
      method: "POST",
      credentials: "include",
      headers: { ...jsonHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        name: name || null,
        accepted_terms: Boolean(acceptedTerms),
        invite_code: inviteCode || null,
        seat_invite_token: seatInviteToken || peekSeatInviteToken() || null,
      }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  const data = await readJson(res);
  if (res.ok) clearSeatInviteToken();
  // Session is issued only after email verification.
  return {
    active: false,
    mode: "cloud",
    plan: null,
    hasSession: false,
    email,
    email_verified: false,
    message: data?.message || "Check your email to verify your account before signing in.",
    dev_verify_token: data?.dev_verify_token,
    billing_state: "none",
    trial_days_left: null,
    share_anonymized_outcomes: true,
  };
}

export async function verifyEmailCloud(token) {
  const res = await fetch(controlUrl("/v1/verify-email"), {
    method: "POST",
    credentials: "include",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  const data = await readJson(res);
  if (data?.analysis_code) await acceptAnalysisSession(data.analysis_code);
  markSignedIn(data?.entitlement?.email);
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
  try {
    await fetch(controlUrl("/v1/logout"), {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json" },
    });
  } catch {
    /* still clear local session */
  }
  if (ANALYSIS_API_URL) {
    try {
      await fetch(`${ANALYSIS_API_URL}/api/v1/session/logout`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
    } catch {
      /* analysis cookie clear is best-effort */
    }
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
  let res;
  try {
    res = await fetch(controlUrl(path), {
      ...options,
      credentials: "include",
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
