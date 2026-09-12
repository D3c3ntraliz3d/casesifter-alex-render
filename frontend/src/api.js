const jsonHeaders = { Accept: "application/json" };

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

export async function getJob(jobId) {
  const res = await fetch(`/api/jobs/${jobId}`, { headers: jsonHeaders });
  return readJson(res);
}

export function exportUrl(jobId, kind) {
  return `/api/jobs/${jobId}/export/${kind}`;
}

export function documentFileUrl(caseId, documentId) {
  return `/api/cases/${caseId}/documents/${documentId}/file`;
}

export function jobSourceUrl(jobId) {
  return `/api/jobs/${jobId}/source`;
}

export async function listCases() {
  const res = await fetch("/api/cases", { headers: jsonHeaders });
  const data = await readJson(res);
  return data.cases || [];
}

export async function createCase(payload) {
  const res = await fetch("/api/cases", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(res);
}

export async function getCase(caseId) {
  const res = await fetch(`/api/cases/${caseId}`, { headers: jsonHeaders });
  return readJson(res);
}

export async function archiveCase(caseId) {
  const res = await fetch(`/api/cases/${caseId}/archive`, { method: "POST", headers: jsonHeaders });
  return readJson(res);
}

export async function unarchiveCase(caseId) {
  const res = await fetch(`/api/cases/${caseId}/unarchive`, { method: "POST", headers: jsonHeaders });
  return readJson(res);
}

export async function deleteCase(caseId) {
  const res = await fetch(`/api/cases/${caseId}`, { method: "DELETE", headers: jsonHeaders });
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

export async function uploadCaseDocument(caseId, file, { displayName, autoScan = true } = {}) {
  const body = new FormData();
  body.append("file", file);
  if (displayName) body.append("display_name", displayName);
  body.append("auto_scan", autoScan ? "true" : "false");
  const res = await fetch(`/api/cases/${caseId}/documents`, { method: "POST", body });
  return readJson(res);
}

export async function patchDocument(caseId, documentId, payload) {
  const res = await fetch(`/api/cases/${caseId}/documents/${documentId}`, {
    method: "PATCH",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(res);
}

export async function scanDocument(caseId, documentId) {
  const res = await fetch(`/api/cases/${caseId}/documents/${documentId}/scan`, {
    method: "POST",
    headers: jsonHeaders,
  });
  return readJson(res);
}

export async function getEntitlement() {
  const res = await fetch("/api/entitlement", { headers: jsonHeaders });
  return readJson(res);
}

export async function loginCloud(email, password) {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  return readJson(res);
}

export async function logoutCloud() {
  const res = await fetch("/api/auth/logout", { method: "POST", headers: jsonHeaders });
  return readJson(res);
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
  const res = await fetch("/api/settings/share-outcomes", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return readJson(res);
}

export async function sendFindingFeedback(payload) {
  const res = await fetch("/api/telemetry/finding-feedback", {
    method: "POST",
    headers: { ...jsonHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(res);
}
