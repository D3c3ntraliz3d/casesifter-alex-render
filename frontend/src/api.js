const jsonHeaders = { Accept: "application/json" };

export async function uploadExhibit(file, submittedBy = "court-staff") {
  const body = new FormData();
  body.append("file", file);
  body.append("submitted_by", submittedBy);
  const res = await fetch("/api/upload", { method: "POST", body });
  if (!res.ok) {
    const detail = await safeDetail(res);
    throw new Error(detail || "Upload failed.");
  }
  return res.json();
}

export async function getJob(jobId) {
  const res = await fetch(`/api/jobs/${jobId}`, { headers: jsonHeaders });
  if (!res.ok) throw new Error("Could not load job status.");
  return res.json();
}

export function exportUrl(jobId, kind) {
  return `/api/jobs/${jobId}/export/${kind}`;
}

async function safeDetail(res) {
  try {
    const data = await res.json();
    return data.detail || JSON.stringify(data);
  } catch {
    return res.statusText;
  }
}
