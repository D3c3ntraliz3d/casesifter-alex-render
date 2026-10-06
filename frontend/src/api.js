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
  return apiUrl(`/api/cases/${caseId}/documents/${documentId}/file`);
}

export function jobSourceUrl(jobId) {
  return apiUrl(`/api/jobs/${jobId}/source`);
}

function vaultUnavailableError() {
  return new Error(
    "Case memory is not on the hosted analysis API yet (Phase 2). Account, trial, and Stripe work now; durable case folders ship next."
  );
}

/**
 * Soft-launch browser build points VITE_ANALYSIS_API_URL at APP_MODE=analysis,
 * which mounts /api/health + /api/v1 only — not durable /api/cases. Hitting
 * /api/cases against a suspended Render service returns HTML 503 without CORS,
 * which the browser surfaces as opaque "Failed to fetch" on Account.
 */
function isHostedAnalysis() {
  return Boolean(ANALYSIS_API_URL);
}

function analysisUnreachableError(cause) {
  const raw = String(cause?.message || cause || "");
  if (/failed to fetch|networkerror|load failed|503|service suspended/i.test(raw)) {
    return new Error(
      "Analysis service unreachable (suspended or offline). Account and billing still work; resume casesifter-analysis on Render to scan."
    );
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
  // Hosted analysis has no case vault — skip the fetch so Account stays clean.
  if (isHostedAnalysis()) return [];
  const res = await apiFetch("/api/cases");
  if (res.status === 404) return [];
  const data = await readJson(res);
  return data.cases || [];
}

export async function createCase(payload) {
  const cs = desktop();
  if (cs) return cs.createCase(payload);
  if (isHostedAnalysis()) throw vaultUnavailableError();
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
  if (isHostedAnalysis()) throw vaultUnavailableError();
  const res = await apiFetch(`/api/cases/${caseId}`);
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function archiveCase(caseId) {
  const cs = desktop();
  if (cs) return cs.archiveCase(caseId);
  if (isHostedAnalysis()) throw vaultUnavailableError();
  const res = await apiFetch(`/api/cases/${caseId}/archive`, { method: "POST" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function unarchiveCase(caseId) {
  const cs = desktop();
  if (cs) return cs.unarchiveCase(caseId);
  if (isHostedAnalysis()) throw vaultUnavailableError();
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
  if (isHostedAnalysis()) throw vaultUnavailableError();
  const res = await apiFetch(`/api/cases/${caseId}`, { method: "DELETE" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function deleteDocument(caseId, documentId) {
  const cs = desktop();
  if (cs) return cs.deleteDocument(caseId, documentId);
  if (isHostedAnalysis()) throw vaultUnavailableError();
  const res = await apiFetch(`/api/cases/${caseId}/documents/${documentId}`, {
    method: "DELETE",
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function updateCase(caseId, payload) {
  if (isHostedAnalysis()) throw vaultUnavailableError();
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

export async function uploadCaseDocument(caseId, file, { displayName, autoScan = true } = {}) {
  const cs = desktop();
  if (cs) {
    // Desktop uses native file picker; browser File upload writes via temp is not used.
    // Callers in desktop should use pickAndAddDocuments — keep a clear error if File is passed.
    throw new Error("In the desktop app, use Add files to pick documents from your PC.");
  }
  if (isHostedAnalysis()) throw vaultUnavailableError();
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
  if (isHostedAnalysis()) throw vaultUnavailableError();
  const res = await apiFetch(`/api/cases/${caseId}/documents/${documentId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

export async function retitleCaseDocuments(caseId) {
  if (isHostedAnalysis()) throw vaultUnavailableError();
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
    return { gotchas: [], factConflicts: [], risksElevated: 0, caseImpact: null };
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
  if (isHostedAnalysis()) throw vaultUnavailableError();
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
  if (isHostedAnalysis()) throw vaultUnavailableError();
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
  if (isHostedAnalysis()) throw vaultUnavailableError();
  const res = await apiFetch(`/api/cases/${caseId}/rescan`, { method: "POST" });
  if (res.status === 404) throw vaultUnavailableError();
  return readJson(res);
}

/**
 * Create an ephemeral analysis job on the hosted analysis API (soft-launch path).
 * Requires an active control-plane session (Bearer) or VITE_ANALYSIS_API_KEY.
 */
export async function createAnalysisJob(file, { displayName, docKind, submittedBy = "web-app" } = {}) {
  const body = new FormData();
  body.append("file", file);
  body.append("submitted_by", submittedBy);
  if (displayName) body.append("display_name", displayName);
  if (docKind) body.append("doc_kind", docKind);
  const res = await analysisFetch("/api/v1/jobs", { method: "POST", body });
  return readJson(res);
}

export async function getEntitlement() {
  const cs = desktop();
  if (cs) return callDesktop(cs.entitlement());

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
    };
  }
  try {
    const res = await fetch(controlUrl("/v1/entitlement"), { headers: controlHeaders() });
    const data = await readJson(res);
    return {
      ...data,
      mode: "cloud",
      hasSession: true,
      email: data.email || (() => {
        try {
          return localStorage.getItem(SESSION_EMAIL_KEY) || undefined;
        } catch {
          return undefined;
        }
      })(),
    };
  } catch {
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
      hasSession: true,
      email,
      message: "Control plane unreachable. Sign in when online.",
      cached: true,
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

export async function startCheckout(trial = false, quantity = 1) {
  const seats = Math.min(50, Math.max(1, Number(quantity) || 1));
  const cs = desktop();
  if (cs) return callDesktop(cs.checkout(trial, seats));
  const token = getSessionToken();
  if (!token) throw new Error("Sign in first.");
  let res;
  try {
    res = await fetch(controlUrl("/v1/checkout"), {
      method: "POST",
      headers: { ...controlHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ trial: Boolean(trial), quantity: seats }),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  return readJson(res);
}

export async function openBillingPortal() {
  const token = getSessionToken();
  if (!token) throw new Error("Sign in first.");
  let res;
  try {
    res = await fetch(controlUrl("/v1/billing-portal"), {
      method: "POST",
      headers: { ...controlHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  } catch (err) {
    throw controlUnreachableError(err);
  }
  return readJson(res);
}

export async function saveLicense(licenseKey) {
  const res = await apiFetch("/api/entitlement/license", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ license_key: licenseKey }),
  });
  return readJson(res);
}

export async function setShareOutcomes(enabled) {
  if (desktop()) return getEntitlement();
  // Hosted soft-launch: share preference is local-only until vault settings exist.
  try {
    const res = await apiFetch("/api/settings/share-outcomes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (res.status === 404) return getEntitlement();
    return readJson(res);
  } catch {
    return getEntitlement();
  }
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
