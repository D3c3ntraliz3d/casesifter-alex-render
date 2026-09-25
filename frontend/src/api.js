const jsonHeaders = { Accept: "application/json" };

function desktop() {
  return typeof window !== "undefined" && window.casesifter?.isDesktop ? window.casesifter : null;
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
  const res = await fetch("/api/upload", { method: "POST", body });
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
  const res = await fetch(`/api/jobs/${jobId}${qs}`, { headers: jsonHeaders });
  return readJson(res);
}

export function exportUrl(jobId, kind) {
  return `/api/jobs/${jobId}/export/${kind}`;
}

export function documentFileUrl(caseId, documentId) {
  const cs = desktop();
  if (cs) return `casesifter-local://${caseId}/${documentId}`;
  return `/api/cases/${caseId}/documents/${documentId}/file`;
}

export function jobSourceUrl(jobId) {
  return `/api/jobs/${jobId}/source`;
}

export async function listCases() {
  const cs = desktop();
  if (cs) return cs.listCases();
  const res = await fetch("/api/cases", { headers: jsonHeaders });
  const data = await readJson(res);
  return data.cases || [];
}

export async function createCase(payload) {
  const cs = desktop();
  if (cs) return cs.createCase(payload);
  const res = await fetch("/api/cases", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(res);
}

export async function getCase(caseId) {
  const cs = desktop();
  if (cs) return cs.getCase(caseId);
  const res = await fetch(`/api/cases/${caseId}`, { headers: jsonHeaders });
  return readJson(res);
}

export async function archiveCase(caseId) {
  const cs = desktop();
  if (cs) return cs.archiveCase(caseId);
  const res = await fetch(`/api/cases/${caseId}/archive`, { method: "POST", headers: jsonHeaders });
  return readJson(res);
}

export async function unarchiveCase(caseId) {
  const cs = desktop();
  if (cs) return cs.unarchiveCase(caseId);
  const res = await fetch(`/api/cases/${caseId}/unarchive`, { method: "POST", headers: jsonHeaders });
  return readJson(res);
}

export async function deleteCase(caseId) {
  const cs = desktop();
  if (cs) {
    await cs.deleteCase(caseId);
    return { deleted: caseId };
  }
  const res = await fetch(`/api/cases/${caseId}`, { method: "DELETE", headers: jsonHeaders });
  return readJson(res);
}

export async function deleteDocument(caseId, documentId) {
  const cs = desktop();
  if (cs) return cs.deleteDocument(caseId, documentId);
  const res = await fetch(`/api/cases/${caseId}/documents/${documentId}`, {
    method: "DELETE",
    headers: jsonHeaders,
  });
  return readJson(res);
}

export async function updateCase(caseId, payload) {
  const res = await fetch(`/api/cases/${caseId}`, {
    method: "PATCH",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
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
  let lastError = new Error("Upload failed.");
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const body = new FormData();
    body.append("file", file);
    if (displayName) body.append("display_name", displayName);
    body.append("auto_scan", autoScan ? "true" : "false");
    try {
      const res = await fetch(`/api/cases/${caseId}/documents`, { method: "POST", body });
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
  const res = await fetch(`/api/cases/${caseId}/documents/${documentId}`, {
    method: "PATCH",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
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
  const res = await fetch(`/api/cases/${caseId}/gotchas`, { headers: jsonHeaders });
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
  const res = await fetch(`/api/cases/${caseId}/chat`, { headers: jsonHeaders });
  const data = await readJson(res);
  return data.messages || [];
}

export async function askCaseChat(caseId, question, { focusDocumentId, documents } = {}) {
  const cs = desktop();
  if (cs) {
    return callDesktop(cs.askCaseChat(caseId, question, focusDocumentId));
  }
  const res = await fetch(`/api/cases/${caseId}/chat`, {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      focus_document_id: focusDocumentId || undefined,
    }),
  });
  return readJson(res);
}

export async function scanDocument(caseId, documentId) {
  const cs = desktop();
  if (cs) return cs.analyzeDocument(caseId, documentId);
  const res = await fetch(`/api/cases/${caseId}/documents/${documentId}/scan`, {
    method: "POST",
    headers: jsonHeaders,
  });
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
  const res = await fetch(`/api/cases/${caseId}/rescan`, {
    method: "POST",
    headers: jsonHeaders,
  });
  return readJson(res);
}

export async function getEntitlement() {
  const cs = desktop();
  if (cs) return callDesktop(cs.entitlement());
  const res = await fetch("/api/entitlement", { headers: jsonHeaders });
  return readJson(res);
}

export async function loginCloud(email, password) {
  const cs = desktop();
  if (cs) {
    await callDesktop(cs.login(email, password));
    return callDesktop(cs.entitlement());
  }
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  return readJson(res);
}

export async function signupCloud(email, password, name, acceptedTerms, inviteCode) {
  const cs = desktop();
  if (cs) {
    await callDesktop(cs.signup(email, password, name, acceptedTerms, inviteCode));
    return callDesktop(cs.entitlement());
  }
  throw new Error("Signup is available in the desktop app (control plane).");
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
  const res = await fetch("/api/auth/logout", { method: "POST", headers: jsonHeaders });
  return readJson(res);
}

export async function startCheckout(trial = false) {
  const cs = desktop();
  if (!cs) throw new Error("Checkout runs from the desktop app.");
  return callDesktop(cs.checkout(trial));
}

export async function saveLicense(licenseKey) {
  const res = await fetch("/api/entitlement/license", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ license_key: licenseKey }),
  });
  return readJson(res);
}

export async function setShareOutcomes(enabled) {
  if (desktop()) return getEntitlement();
  const res = await fetch("/api/settings/share-outcomes", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return readJson(res);
}

export async function sendFindingFeedback(payload) {
  const cs = desktop();
  if (cs) {
    return cs.findingFeedback(payload);
  }
  const res = await fetch("/api/telemetry/finding-feedback", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(res);
}

export function isDesktopApp() {
  return Boolean(desktop());
}
