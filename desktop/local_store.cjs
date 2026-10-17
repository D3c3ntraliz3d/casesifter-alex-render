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
  const rank = { red: 0, yellow: 1, incomplete: 2, green: 3 };
  return readIndex(root)
    .cases.slice()
    .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""))
    .map((row) => {
      const docsPath = path.join(root, "cases", row.id, "documents.json");
      if (!fs.existsSync(docsPath)) return row;
      try {
        const docs = JSON.parse(fs.readFileSync(docsPath, "utf8"));
        let highest = null;
        let best = 99;
        for (const doc of docs || []) {
          const level = String(doc.risk_level || "").toLowerCase();
          const r = rank[level];
          if (r == null) continue;
          if (r < best) {
            best = r;
            highest = level;
          }
        }
        const next = { ...row, document_count: Array.isArray(docs) ? docs.length : row.document_count || 0 };
        if (highest) next.highest_risk = highest;
        return next;
      } catch {
        return row;
      }
    });
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

function callsOf(value) {
  return typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : null;
}

const ANALYSIS_TAG_LABELS = {
  date_inconsistency: "Statement dates",
  historical_impossibility: "Issuer era",
  math_error: "Statement math",
  line_self_contradiction: "Line contradiction",
  template_fabrication: "Template chrome",
  logo_chrome_mismatch: "Logo chrome",
  text_overlay: "Text overlay",
  graphic_manipulation: "Graphic edit",
};
const MODULE_TAG_LABELS = {
  metadata: "Meta",
  image_forensics: "Image",
  ocr_text: "OCR",
  contextual_logic: "Logic",
};
const ANALYSIS_PRIORITY = [
  "date_inconsistency",
  "historical_impossibility",
  "math_error",
  "line_self_contradiction",
  "template_fabrication",
  "logo_chrome_mismatch",
  "text_overlay",
  "graphic_manipulation",
];

function profileTags(report, scanModules) {
  const tags = [];
  const seen = new Set();
  const add = (label) => {
    const text = String(label || "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    tags.push(text);
  };
  const byType = {};
  const findings = [...(report.top_findings || []), ...(report.findings || [])];
  for (const item of findings) {
    if (!item || typeof item !== "object") continue;
    const ev = item.evidence && typeof item.evidence === "object" ? item.evidence : {};
    const analysis = String(item.analysis_type || ev.analysis_type || "").trim();
    if (!analysis) continue;
    const sev = String(item.severity || "").toLowerCase();
    if (sev === "info") continue;
    byType[analysis] = sev || "medium";
  }
  const ordered = [
    ...ANALYSIS_PRIORITY.filter((a) => a in byType),
    ...Object.keys(byType).filter((a) => !ANALYSIS_PRIORITY.includes(a)).sort(),
  ];
  for (const analysis of ordered) {
    add(ANALYSIS_TAG_LABELS[analysis] || analysis.replace(/_/g, " "));
    if (tags.length >= 5) break;
  }
  for (const mod of scanModules || []) {
    if (String(mod?.status || "") !== "error") continue;
    const name = String(mod?.name || "");
    add(`${MODULE_TAG_LABELS[name] || name || "Module"} error`);
  }
  if (String(report.ai_review || "").toLowerCase() === "incomplete") add("AI review incomplete");
  return tags.slice(0, 6);
}

function uniqStrings(values, limit = 6) {
  const out = [];
  const seen = new Set();
  for (const raw of values || []) {
    const text = String(raw || "").trim();
    if (!text) continue;
    const key = text.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(text);
    if (out.length >= limit) break;
  }
  return out;
}

function entityNames(entities, keys) {
  if (!entities || typeof entities !== "object") return [];
  const names = [];
  for (const key of keys) {
    const bucket = entities[key];
    if (Array.isArray(bucket)) {
      for (const item of bucket) {
        if (typeof item === "string") names.push(item);
        else if (item && typeof item === "object") names.push(item.name || item.display || item.value || "");
      }
    } else if (typeof bucket === "string") names.push(bucket);
  }
  return uniqStrings(names, 8);
}

function dataProfile(report, doc) {
  const comparison = report.comparison && typeof report.comparison === "object" ? report.comparison : {};
  const entities = comparison.entities && typeof comparison.entities === "object" ? comparison.entities : {};
  const findings = [...(report.top_findings || []), ...(report.findings || [])].filter(
    (f) => f && typeof f === "object"
  );
  const counts = {};
  for (const item of findings) {
    const sev = String(item.severity || "").toLowerCase();
    if (["critical", "high", "medium", "low"].includes(sev)) counts[sev] = (counts[sev] || 0) + 1;
  }
  const top = [];
  for (const item of findings) {
    if (String(item.severity || "").toLowerCase() === "info") continue;
    const title = String(item.title || "").trim();
    if (!title) continue;
    top.push({ title: title.slice(0, 160), severity: String(item.severity || "medium") });
    if (top.length >= 3) break;
  }
  const dims = report.page_dimensions || comparison.page_dimensions || [];
  const pages =
    (typeof doc?.page_count === "number" && doc.page_count > 0 && doc.page_count) ||
    (Array.isArray(dims) && dims.length ? dims.length : null);
  return {
    kind: doc?.doc_kind || comparison.doc_kind || null,
    pages,
    organizations: entityNames(entities, ["organizations", "orgs", "issuers", "companies"]),
    people: entityNames(entities, ["people", "persons", "parties"]),
    dates: [],
    amounts: [],
    addresses: [],
    finding_counts: counts,
    top_findings: top,
  };
}

function infoSummary(report, data, tags) {
  const exec = typeof report.executive_summary === "string" ? report.executive_summary.trim() : "";
  if (exec) {
    const cleaned = exec.replace(/\s+/g, " ");
    return cleaned.length > 260 ? `${cleaned.slice(0, 259).replace(/\s+\S*$/, "")}…` : cleaned;
  }
  const parts = [];
  if (data?.organizations?.length) parts.push(data.organizations.slice(0, 3).join(" · "));
  if (data?.top_findings?.length) {
    parts.push(`Flags: ${data.top_findings.map((t) => t.title).filter(Boolean).slice(0, 2).join("; ")}`);
  } else if (tags?.length) {
    parts.push(`Flags: ${tags.slice(0, 4).join(", ")}`);
  }
  return parts.join(". ").slice(0, 280);
}

/** Slim module/review fields already stored on a cached report. */
function scanSignals(report, doc) {
  const empty = {
    scan_modules: [],
    scan_profile: null,
    visual_review_required: null,
    review_mode: null,
    gemini_calls: null,
    document_analysis: null,
    visual_file_sent: null,
    profile_tags: [],
    data_profile: {},
    info_summary: "",
  };
  if (!report || typeof report !== "object") return empty;
  const modules = Array.isArray(report.modules) ? report.modules : [];
  const scanModules = [];
  let extras = {};
  for (const mod of modules) {
    if (!mod || typeof mod !== "object") continue;
    if (mod.name && mod.status) scanModules.push({ name: String(mod.name), status: String(mod.status) });
    if (mod.name === "contextual_logic" && mod.extras && typeof mod.extras === "object") extras = mod.extras;
  }
  const comparison = report.comparison && typeof report.comparison === "object" ? report.comparison : {};
  const timing = comparison.timing && typeof comparison.timing === "object" ? comparison.timing : {};
  const summary = comparison.gemini_scan_summary && typeof comparison.gemini_scan_summary === "object"
    ? comparison.gemini_scan_summary
    : {};
  const usage = comparison.gemini_usage && typeof comparison.gemini_usage === "object" ? comparison.gemini_usage : {};
  const ctxUsage = extras.gemini_usage && typeof extras.gemini_usage === "object" ? extras.gemini_usage : {};
  const calls = callsOf(timing.gemini_calls) ?? callsOf(summary.calls) ?? callsOf(ctxUsage.calls) ?? callsOf(usage.calls);
  let required = null;
  if (typeof comparison.visual_review_required === "boolean") required = comparison.visual_review_required;
  else if (typeof extras.visual_review_required === "boolean") required = extras.visual_review_required;
  const mode = typeof comparison.review_mode === "string" && comparison.review_mode
    ? comparison.review_mode
    : (typeof extras.review_mode === "string" && extras.review_mode ? extras.review_mode : null);
  let fileSent = null;
  if (Object.prototype.hasOwnProperty.call(extras, "document_analysis_result")) {
    const dar = extras.document_analysis_result;
    fileSent = Boolean(dar && typeof dar === "object" && dar.file_sent);
  }
  const tags = profileTags(report, scanModules);
  const data = dataProfile(report, doc);
  return {
    scan_modules: scanModules,
    scan_profile: typeof comparison.scan_profile === "string" ? comparison.scan_profile : null,
    visual_review_required: required,
    review_mode: mode,
    gemini_calls: calls,
    document_analysis: typeof extras.document_analysis === "string" ? extras.document_analysis : null,
    visual_file_sent: fileSent,
    ai_review: report.ai_review || comparison.ai_review || null,
    ai_review_reason: report.ai_review_reason || comparison.ai_review_reason || null,
    profile_tags: tags,
    data_profile: data,
    info_summary: infoSummary(report, data, tags),
  };
}

function getCase(root, caseId) {
  const caseDir = path.join(root, "cases", caseId);
  if (!fs.existsSync(caseDir)) return null;
  const meta = JSON.parse(fs.readFileSync(path.join(caseDir, "case.json"), "utf8"));
  const docsPath = path.join(caseDir, "documents.json");
  const documents = JSON.parse(fs.readFileSync(docsPath, "utf8"));
  // Reports are read once, then the slim signals stay on the document row.
  let dirty = false;
  for (const doc of documents) {
    if (doc.status !== "complete") continue;
    // Backfill when profile/summary fields are still missing on older rows.
    if (
      Array.isArray(doc.scan_modules) &&
      Array.isArray(doc.profile_tags) &&
      doc.data_profile &&
      typeof doc.info_summary === "string"
    ) {
      continue;
    }
    const report = loadReport(root, caseId, doc.id);
    if (!report) continue;
    Object.assign(doc, scanSignals(report, doc));
    dirty = true;
  }
  if (dirty) fs.writeFileSync(docsPath, JSON.stringify(documents, null, 2));
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
  const docsPath = path.join(root, "cases", caseId, "documents.json");
  const docs = fs.existsSync(docsPath) ? JSON.parse(fs.readFileSync(docsPath, "utf8")) : [];
  const existing = Array.isArray(docs) ? docs.find((d) => d.id === documentId) : null;
  const patch = scanSignals(report, existing);
  if (excerpt) patch.text_excerpt = String(excerpt).slice(0, 24000);
  if (patch.scan_modules || patch.info_summary || excerpt) {
    updateDocument(root, caseId, documentId, patch);
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
