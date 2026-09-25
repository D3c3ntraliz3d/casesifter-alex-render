/**
 * Local-only case store for the thin desktop client.
 * Documents and reports stay under userData — never a cloud vault.
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function storeRoot(userData) {
  const root = path.join(userData, "CaseSifterData");
  ensureDir(root);
  ensureDir(path.join(root, "cases"));
  const indexPath = path.join(root, "index.json");
  if (!fs.existsSync(indexPath)) {
    fs.writeFileSync(indexPath, JSON.stringify({ cases: [] }, null, 2));
  }
  return root;
}

function readIndex(root) {
  return JSON.parse(fs.readFileSync(path.join(root, "index.json"), "utf8"));
}

function writeIndex(root, data) {
  fs.writeFileSync(path.join(root, "index.json"), JSON.stringify(data, null, 2));
}

function nowIso() {
  return new Date().toISOString();
}

function listCases(root) {
  return readIndex(root).cases.slice().sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
}

function createCase(root, payload) {
  const id = crypto.randomBytes(16).toString("hex");
  const caseDir = path.join(root, "cases", id);
  ensureDir(path.join(caseDir, "documents"));
  ensureDir(path.join(caseDir, "reports"));
  const row = {
    id,
    caption: payload.caption,
    court: payload.court || null,
    index_number: payload.index_number || null,
    notes: payload.notes || null,
    created_at: nowIso(),
    updated_at: nowIso(),
    archived_at: null,
    document_count: 0,
  };
  const idx = readIndex(root);
  idx.cases.push(row);
  writeIndex(root, idx);
  fs.writeFileSync(path.join(caseDir, "case.json"), JSON.stringify(row, null, 2));
  fs.writeFileSync(path.join(caseDir, "documents.json"), JSON.stringify([], null, 2));
  return row;
}

function getCase(root, caseId) {
  const caseDir = path.join(root, "cases", caseId);
  if (!fs.existsSync(caseDir)) return null;
  const meta = JSON.parse(fs.readFileSync(path.join(caseDir, "case.json"), "utf8"));
  const docsPath = path.join(caseDir, "documents.json");
  const documents = JSON.parse(fs.readFileSync(docsPath, "utf8"));
  // Do not load report files here — that made opening large cases / adding files crawl.
  return { ...meta, documents };
}

function addDocument(root, caseId, filePath, originalName) {
  const added = addDocuments(root, caseId, [{ filePath, originalName }]);
  return added[0] || null;
}

function addDocuments(root, caseId, items) {
  const caseDir = path.join(root, "cases", caseId);
  const docsPath = path.join(caseDir, "documents.json");
  const docs = JSON.parse(fs.readFileSync(docsPath, "utf8"));
  const created = [];
  for (const item of items) {
    const filePath = item.filePath || item;
    const originalName = item.originalName || path.basename(filePath);
    const id = crypto.randomBytes(16).toString("hex");
    const safe = path.basename(originalName || filePath);
    const destDir = path.join(caseDir, "documents", id);
    ensureDir(destDir);
    const dest = path.join(destDir, safe);
    fs.copyFileSync(filePath, dest);
    const doc = {
      id,
      case_id: caseId,
      display_name: safe,
      original_filename: safe,
      stored_path: dest,
      status: "uploaded",
      doc_kind: null,
      risk_level: null,
      scan_job_id: null,
      created_at: nowIso(),
    };
    docs.push(doc);
    created.push(doc);
  }
  fs.writeFileSync(docsPath, JSON.stringify(docs, null, 2));
  touchCase(root, caseId, docs.length);
  return created;
}

function touchCase(root, caseId, documentCount) {
  const idx = readIndex(root);
  const row = idx.cases.find((c) => c.id === caseId);
  if (!row) return;
  row.updated_at = nowIso();
  if (typeof documentCount === "number") {
    row.document_count = documentCount;
  } else {
    const docs = JSON.parse(fs.readFileSync(path.join(root, "cases", caseId, "documents.json"), "utf8"));
    row.document_count = docs.length;
  }
  writeIndex(root, idx);
  fs.writeFileSync(path.join(root, "cases", caseId, "case.json"), JSON.stringify({ ...row }, null, 2));
}

function updateDocument(root, caseId, documentId, patch) {
  const docsPath = path.join(root, "cases", caseId, "documents.json");
  const docs = JSON.parse(fs.readFileSync(docsPath, "utf8"));
  const doc = docs.find((d) => d.id === documentId);
  if (!doc) return null;
  Object.assign(doc, patch);
  fs.writeFileSync(docsPath, JSON.stringify(docs, null, 2));
  touchCase(root, caseId);
  return doc;
}

function saveReport(root, caseId, documentId, report) {
  const dest = path.join(root, "cases", caseId, "reports", `${documentId}.json`);
  fs.writeFileSync(dest, JSON.stringify(report, null, 2));
  const excerpt =
    (report && report.comparison && report.comparison.text_excerpt) ||
    (report && report.text_excerpt) ||
    "";
  if (excerpt) {
    updateDocument(root, caseId, documentId, {
      text_excerpt: String(excerpt).slice(0, 24000),
    });
  }
  return dest;
}

function listCaseMessages(root, caseId, limit = 40) {
  const dest = path.join(root, "cases", caseId, "messages.json");
  if (!fs.existsSync(dest)) return [];
  const rows = JSON.parse(fs.readFileSync(dest, "utf8"));
  return Array.isArray(rows) ? rows.slice(-limit) : [];
}

function addCaseMessage(root, caseId, role, content, citations) {
  const dest = path.join(root, "cases", caseId, "messages.json");
  const rows = fs.existsSync(dest) ? JSON.parse(fs.readFileSync(dest, "utf8")) : [];
  const msg = {
    id: crypto.randomBytes(8).toString("hex"),
    case_id: caseId,
    role,
    content,
    citations: citations || [],
    created_at: nowIso(),
  };
  rows.push(msg);
  fs.writeFileSync(dest, JSON.stringify(rows.slice(-80), null, 2));
  return msg;
}

function buildChatPacket(root, caseId) {
  const caseData = getCase(root, caseId);
  if (!caseData) return null;
  const docs = [];
  for (const doc of caseData.documents || []) {
    let excerpt = doc.text_excerpt || "";
    let findings = [];
    if (!excerpt && doc.status === "complete") {
      const report = loadReport(root, caseId, doc.id);
      excerpt =
        report?.comparison?.text_excerpt ||
        report?.text_excerpt ||
        "";
      if (excerpt && !doc.text_excerpt) {
        updateDocument(root, caseId, doc.id, { text_excerpt: String(excerpt).slice(0, 24000) });
      }
      findings = (report?.findings || []).slice(0, 8).map((f) => ({
        title: f.title,
        analysis_type: f.evidence?.analysis_type || f.category,
      }));
    }
    docs.push({
      document_id: doc.id,
      display_name: doc.display_name || doc.original_filename,
      original_filename: doc.original_filename,
      doc_kind: doc.doc_kind,
      text_excerpt: String(excerpt || "").slice(0, 24000),
      findings_summary: findings,
      status: doc.status,
    });
  }
  return { case: caseData, documents: docs };
}

/** Facts + excerpts from cached reports for case-level conflict aggregation. */
function buildFactConflictPacket(root, caseId) {
  const caseData = getCase(root, caseId);
  if (!caseData) return null;
  const docs = [];
  for (const doc of caseData.documents || []) {
    if (doc.status !== "complete") continue;
    const report = loadReport(root, caseId, doc.id);
    if (!report) continue;
    const comparison = report.comparison || {};
    const excerpt =
      comparison.text_excerpt ||
      report.text_excerpt ||
      doc.text_excerpt ||
      "";
    const facts = Array.isArray(comparison.facts) ? comparison.facts : [];
    if (!facts.length && !String(excerpt || "").trim()) continue;
    docs.push({
      document_id: doc.id,
      display_name: doc.display_name || doc.original_filename,
      original_filename: doc.original_filename,
      doc_kind: doc.doc_kind,
      text_excerpt: String(excerpt || "").slice(0, 24000),
      facts,
    });
  }
  return { case: caseData, documents: docs };
}

/** Peer snapshots for ephemeral /v1/jobs so compare_to_case can score this file. */
function buildPeerSnapshots(root, caseId, excludeDocumentId) {
  const packet = buildFactConflictPacket(root, caseId);
  if (!packet) return [];
  return (packet.documents || [])
    .filter((d) => d.document_id && d.document_id !== excludeDocumentId)
    .slice(0, 80)
    .map((d) => ({
      document_id: d.document_id,
      display_name: d.display_name,
      original_filename: d.original_filename,
      doc_kind: d.doc_kind,
      text_excerpt: d.text_excerpt,
      facts: d.facts || [],
    }));
}

const RISK_RANK = { green: 0, yellow: 1, red: 2 };

/**
 * Elevate involved documents when case-level fact conflicts are high/medium.
 * Ephemeral single-file scores stay green until peers exist; this syncs the
 * board + "Reports by risk" after packet aggregate (and after each scan).
 */
function applyFactConflictRisks(root, caseId, conflicts) {
  let elevated = 0;
  for (const conflict of conflicts || []) {
    const sev = String(conflict.severity || "");
    if (!["medium", "high", "critical"].includes(sev)) continue;
    const target = sev === "medium" ? "yellow" : "red";
    const minScore = target === "red" ? 70 : 35;
    const ids = new Set();
    for (const variant of conflict.variants || []) {
      for (const doc of variant.documents || []) {
        if (doc?.document_id) ids.add(String(doc.document_id));
      }
    }
    for (const doc of conflict.related_documents || []) {
      const id = doc?.document_id || doc?.id;
      if (id) ids.add(String(id));
    }
    for (const documentId of ids) {
      const caseData = getCase(root, caseId);
      const live = (caseData?.documents || []).find((d) => d.id === documentId);
      if (!live) continue;
      const curRank = RISK_RANK[live.risk_level] ?? -1;
      if (curRank < RISK_RANK[target]) {
        updateDocument(root, caseId, documentId, { risk_level: target });
        elevated += 1;
      }
      const report = loadReport(root, caseId, documentId);
      if (!report) continue;
      const fid = `case-fact-${conflict.id || conflict.field || "x"}`;
      report.findings = Array.isArray(report.findings) ? report.findings : [];
      if (!report.findings.some((f) => f && f.id === fid)) {
        report.findings.unshift({
          id: fid,
          category: "case_consistency",
          severity: sev === "critical" ? "critical" : sev === "medium" ? "medium" : "high",
          title: conflict.title || "Case fact conflict",
          explanation: conflict.attorney_why || conflict.gotcha_level || "Cross-document fact conflict.",
          why_suspicious: conflict.attorney_why || "Same nameable fact with incompatible values across this case.",
          confidence: Number(conflict.confidence) || 76,
          evidence: {
            case_consistency: true,
            documentary: true,
            analysis_type: "fact_conflict",
            contradiction_type: "factual_contradiction",
            case_impact: true,
            case_impact_bucket: conflict.case_impact_bucket || "credibility",
            field: conflict.field,
            material: true,
            case_fact_conflict: true,
            variants: conflict.variants || [],
          },
          recommended_action: "Reconcile the conflicting exhibits before relying on either.",
        });
      }
      if ((RISK_RANK[report.risk_level] ?? -1) < RISK_RANK[target]) {
        report.risk_level = target;
        report.risk_score = Math.max(Number(report.risk_score) || 0, minScore);
      }
      saveReport(root, caseId, documentId, report);
    }
  }
  return elevated;
}

function loadReport(root, caseId, documentId) {
  const dest = path.join(root, "cases", caseId, "reports", `${documentId}.json`);
  if (!fs.existsSync(dest)) return null;
  return JSON.parse(fs.readFileSync(dest, "utf8"));
}

function archiveCase(root, caseId, archived) {
  const idx = readIndex(root);
  const row = idx.cases.find((c) => c.id === caseId);
  if (!row) return null;
  row.archived_at = archived ? nowIso() : null;
  row.updated_at = nowIso();
  writeIndex(root, idx);
  fs.writeFileSync(path.join(root, "cases", caseId, "case.json"), JSON.stringify(row, null, 2));
  return row;
}

function deleteCase(root, caseId) {
  const idx = readIndex(root);
  idx.cases = idx.cases.filter((c) => c.id !== caseId);
  writeIndex(root, idx);
  const caseDir = path.join(root, "cases", caseId);
  fs.rmSync(caseDir, { recursive: true, force: true });
  return true;
}

function deleteDocument(root, caseId, documentId) {
  const caseDir = path.join(root, "cases", caseId);
  const docsPath = path.join(caseDir, "documents.json");
  if (!fs.existsSync(docsPath)) return false;
  const docs = JSON.parse(fs.readFileSync(docsPath, "utf8"));
  const doc = docs.find((d) => d.id === documentId);
  if (!doc) return false;
  const next = docs.filter((d) => d.id !== documentId);
  fs.writeFileSync(docsPath, JSON.stringify(next, null, 2));
  const reportPath = path.join(caseDir, "reports", `${documentId}.json`);
  try {
    if (fs.existsSync(reportPath)) fs.unlinkSync(reportPath);
  } catch {
    /* ignore */
  }
  const docFolder = path.join(caseDir, "documents", documentId);
  try {
    if (fs.existsSync(docFolder)) fs.rmSync(docFolder, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
  if (doc.stored_path && fs.existsSync(doc.stored_path)) {
    try {
      const st = fs.statSync(doc.stored_path);
      if (st.isFile()) fs.unlinkSync(doc.stored_path);
    } catch {
      /* ignore */
    }
  }
  touchCase(root, caseId);
  return true;
}

module.exports = {
  storeRoot,
  listCases,
  createCase,
  getCase,
  addDocument,
  addDocuments,
  updateDocument,
  deleteDocument,
  saveReport,
  loadReport,
  archiveCase,
  deleteCase,
  listCaseMessages,
  addCaseMessage,
  buildChatPacket,
  buildFactConflictPacket,
  buildPeerSnapshots,
  applyFactConflictRisks,
};
