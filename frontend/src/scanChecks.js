/**
 * Per-check lights for a document row.
 *
 * Green: that check finished (a finding is a result, not a failure).
 * Yellow: that check is still running.
 * Amber (warn): soft-fail — finished with a non-blocking module error (Meta).
 * Red: that check failed or never returned (blocks Complete clearance).
 *
 * Dots come only from module rows and review flags already stored on the
 * document or its report. A required visual review with no response is red.
 * Skipped modules stay omitted.
 *
 * Meta packaging/timestamp crashes are warn-only: they must not drive
 * hasFailedCheck / Incomplete when the rest of the scan finished.
 *
 * A rescan clears the previous colors: every included dot is yellow until
 * that check finishes, then green, warn, or red. The previous ok/failed
 * colors are not reused while the new scan is in progress.
 */

const MODULE_LABELS = {
  metadata: "Meta",
  pdf_integrity: "Structure",
  structural: "Pages",
  image_forensics: "Image",
  ocr_text: "Text",
  contextual_logic: "Context",
};

const STAGE_CHECK = {
  metadata: "metadata",
  pdf_integrity: "pdf_integrity",
  structural: "structural",
  image_forensics: "image_forensics",
  ocr_text: "ocr_text",
  ocr: "ocr_text",
  contextual: "contextual_logic",
  contextual_logic: "contextual_logic",
  gemini: "visual",
  document_analysis: "visual",
};

const CHECK_ORDER = [
  "metadata",
  "pdf_integrity",
  "structural",
  "image_forensics",
  "ocr_text",
  "statement_dates",
  "contextual_logic",
  "visual",
];

// Index of the check that is running. Earlier checks have finished.
// Dates run in the local stage (before contextual / Gemini).
const STAGE_CURSOR = {
  queued: -1,
  ingesting: -1,
  metadata: 0,
  pdf_integrity: 1,
  structural: 2,
  image_forensics: 3,
  ocr_text: 4,
  ocr: 4,
  local_done: 6,
  contextual: 6,
  contextual_logic: 6,
  gemini: 7,
  document_analysis: 7,
  case_consistency: 8,
  generating_report: 8,
};

const SCANNING = new Set(["queued", "scanning", "running", "local_done"]);

function light(id, label, state, title) {
  return { id, label, state, title };
}

function shortName(name) {
  const text = String(name || "").replace(/_/g, " ").trim();
  if (!text) return "Check";
  return text.length > 14 ? `${text.slice(0, 13)}…` : text;
}

function moduleList(doc) {
  if (Array.isArray(doc.scan_modules)) return doc.scan_modules;
  if (Array.isArray(doc.modules)) return doc.modules;
  if (Array.isArray(doc.report?.modules)) return doc.report.modules;
  return [];
}

function numberOrNull(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

function geminiCalls(doc) {
  const comparison = doc.report?.comparison || doc.comparison || {};
  return (
    numberOrNull(doc.gemini_calls) ??
    numberOrNull(comparison.timing?.gemini_calls) ??
    numberOrNull(comparison.gemini_scan_summary?.calls) ??
    numberOrNull(comparison.gemini_usage?.calls)
  );
}

function reviewMode(doc) {
  if (typeof doc.review_mode === "string" && doc.review_mode) return doc.review_mode;
  const comparison = doc.report?.comparison || doc.comparison || {};
  return typeof comparison.review_mode === "string" ? comparison.review_mode : null;
}

function visualRequired(doc) {
  if (doc.visual_review_required === true || doc.visual_review_required === false) {
    return doc.visual_review_required;
  }
  const comparison = doc.report?.comparison || doc.comparison || {};
  if (comparison.visual_review_required === true || comparison.visual_review_required === false) {
    return comparison.visual_review_required;
  }
  return null;
}

function fileSent(doc) {
  if (doc.visual_file_sent === true || doc.visual_file_sent === false) return doc.visual_file_sent;
  return null;
}

function visualLight(doc) {
  const comparison = doc.report?.comparison || doc.comparison || {};
  const ai = doc.ai_review || doc.report?.ai_review || comparison.ai_review || null;
  const reason = doc.ai_review_reason || doc.report?.ai_review_reason || comparison.ai_review_reason || "";
  const mode = reviewMode(doc);
  const required = visualRequired(doc);
  const calls = geminiCalls(doc);
  const sent = fileSent(doc);
  const gotCalls = typeof calls === "number" && calls > 0;
  const gotFile = sent === true;
  const explicitMiss = sent === false && !gotCalls;

  if (ai === "incomplete") {
    return light(
      "visual",
      "Visual",
      "failed",
      reason
        ? `Visual model pass did not finish (${reason}) — not a fraud finding`
        : "Visual model pass did not finish — not a fraud finding"
    );
  }
  if (ai === "local_only" || mode === "local_only" || mode === "court_filing" || mode === "not_required") {
    return null;
  }
  if (required === false) return null;

  if (explicitMiss && (required === true || mode === "forensic")) {
    return light("visual", "Visual", "failed", "Visual review did not return");
  }
  if (gotFile || gotCalls) {
    return light("visual", "Visual", "ok", "Visual review finished");
  }
  if (required === true && ai === "complete" && !explicitMiss) {
    return light("visual", "Visual", "ok", "Visual review finished");
  }
  if (required === true) {
    return light("visual", "Visual", "failed", "Visual review did not return");
  }
  return null;
}

function runningLight(doc) {
  const stage = STAGE_CHECK[doc.stage] || STAGE_CHECK[doc.scan_stage] || null;
  if (!stage) return null;
  if (stage === "visual") {
    return light("visual", "Visual", "running", "Visual review is still running");
  }
  const label = MODULE_LABELS[stage] || shortName(stage);
  return light(stage, label, "running", `${label} is still running`);
}

function moduleFailTitle(label, name, error) {
  const detail = String(error || "").trim();
  if (name === "image_forensics") {
    if (/cv2|pypdfium|opencv/i.test(detail)) {
      return "Image check crashed (missing library). Re-scan after installing analysis deps — not a manipulation finding";
    }
    if (detail) {
      return `Image check did not finish (${detail.slice(0, 120)}) — not a manipulation finding`;
    }
    return "Image check did not finish (module error — not a manipulation finding)";
  }
  if (name === "metadata") {
    if (/can not convert date|convert date/i.test(detail)) {
      return "Meta warning: unreadable PDF timestamps (e-file date junk) — packaging only, not a fraud finding";
    }
    if (detail) {
      return `Meta warning (${detail.slice(0, 120)}) — packaging / timestamps only, not a fraud finding`;
    }
    return "Meta warning: check did not finish — packaging / timestamps only, not a fraud finding";
  }
  if (detail) return `${label} check did not finish (${detail.slice(0, 120)})`;
  return `${label} check did not finish`;
}

/** Meta errors are soft-fail: warn light + chip, never Incomplete clearance. */
function isSoftFailModule(name) {
  return name === "metadata";
}

function finishedLights(doc) {
  const modules = moduleList(doc);
  const lights = [];
  for (const mod of modules) {
    const name = String(mod?.name || "");
    const status = String(mod?.status || "");
    if (!name || status === "skipped") continue;
    // Only real module rows. Dates is not a stored module — do not invent a green light.
    if (name === "statement_dates") {
      const label = "Dates";
      if (status === "ok") lights.push(light(name, label, "ok", "Statement-date check finished"));
      else lights.push(light(name, label, "failed", moduleFailTitle(label, name, mod?.error)));
      continue;
    }
    const label = MODULE_LABELS[name] || shortName(name);
    if (status === "ok") lights.push(light(name, label, "ok", `${label} finished`));
    else if (isSoftFailModule(name)) {
      lights.push(light(name, label, "warn", moduleFailTitle(label, name, mod?.error)));
    } else {
      lights.push(light(name, label, "failed", moduleFailTitle(label, name, mod?.error)));
    }
  }
  const visual = visualLight(doc);
  if (visual) lights.push(visual);
  return lights;
}

/**
 * In-progress lights for the CURRENT job.
 *
 * Every included check stays yellow while the scan is open (red only if the
 * job failed). Prior ok/error and early local_done module rows must not paint
 * greens mid-rescan — that looked like every check finishing in one flash.
 */
function progressLights(doc, failed) {
  const included = finishedLights(doc);
  if (!included.length) {
    const active = runningLight(doc);
    if (!active) return [];
    return failed
      ? [light(active.id, active.label, "failed", `${active.label} did not return`)]
      : [active];
  }
  const stage = doc.stage || doc.scan_stage || "";
  const finalizing = stage === "generating_report" || stage === "case_consistency";
  return included.map((item) => {
    if (failed) {
      return light(item.id, item.label, "failed", `${item.label} did not return`);
    }
    if (finalizing) {
      return light(item.id, item.label, "running", `${item.label} is finishing`);
    }
    const active = STAGE_CHECK[stage] === item.id;
    if (active) {
      return light(item.id, item.label, "running", `${item.label} is still running`);
    }
    return light(item.id, item.label, "running", `${item.label} is waiting`);
  });
}

export function isScanInProgress(doc) {
  if (!doc || typeof doc !== "object") return false;
  // Local arm wins over a stale Complete list row (hosted matters hardcode
  // status "complete", and early local_done persist can look finished).
  if (doc.rescan_armed) return !doc.scan_failed_open;
  return Boolean(doc.scan_in_progress) || SCANNING.has(doc.status);
}

/** Compact module rows from a finished job report (for liveScan overlay). */
export function modulesFromReport(report) {
  if (!report || typeof report !== "object") return [];
  const out = [];
  for (const mod of report.modules || []) {
    if (!mod || typeof mod !== "object") continue;
    const name = mod.name;
    const status = mod.status;
    if (typeof name !== "string" || !name || typeof status !== "string" || !status) continue;
    const row = { name, status };
    if (status === "error") {
      const extras = mod.extras && typeof mod.extras === "object" ? mod.extras : {};
      const detail =
        (typeof extras.error === "string" && extras.error.trim()) ||
        (typeof mod.summary === "string" && mod.summary.trim()) ||
        "";
      if (detail) row.error = detail.slice(0, 240);
    }
    out.push(row);
  }
  return out;
}

function scanErrorRaw(docOrError) {
  if (typeof docOrError === "string") return docOrError.trim();
  return String(docOrError?.last_error || docOrError?.error || docOrError?.message || "").trim();
}

/** True when the job died on a server restart (not a per-check crash). */
export function isRestartInterrupt(doc) {
  if (!doc || typeof doc !== "object") return false;
  if (doc.status === "interrupted") return true;
  if (!doc.scan_failed_open && doc.status !== "failed") return false;
  const raw = scanErrorRaw(doc).toLowerCase();
  return (
    raw === "interrupted_by_restart" ||
    /instance restarted|job was interrupted|interrupted_by_restart/.test(raw)
  );
}

/**
 * Human text for scan_failed_open / last_error (raw codes are not useful in the row).
 */
export function formatScanError(docOrError) {
  const raw = scanErrorRaw(docOrError);
  if (!raw) return "";
  const key = raw.toLowerCase();
  if (key === "interrupted_by_restart" || /instance restarted|job was interrupted/i.test(raw)) {
    return "Analysis server restarted mid-scan. Re-scan to finish — checks that already ran are shown below.";
  }
  if (key === "local_done_missing_artifacts" || /local_done artifacts/i.test(raw)) {
    return "Scan artifacts were lost after a restart. Re-scan (re-upload if Locate is offered).";
  }
  if (/failed 3 times|needs re-upload/i.test(raw)) {
    return raw;
  }
  if (/^interrupted/i.test(raw)) {
    return "Scan was interrupted. Re-scan to finish.";
  }
  // Drop bare machine codes; keep readable sentences.
  if (/^[a-z0-9_]+$/i.test(raw) && raw.length < 48) {
    return `Scan did not finish (${raw.replace(/_/g, " ")}). Re-scan to retry.`;
  }
  return raw;
}

function modulesHaveOutcomes(doc) {
  return moduleList(doc).some((mod) => {
    const status = String(mod?.status || "");
    return status === "ok" || status === "error";
  });
}

export function checkLights(doc) {
  if (!doc || typeof doc !== "object") return [];
  // Armed rescan: never paint finished ok/failed greens until the overlay settles.
  if (doc.rescan_armed) {
    return progressLights(doc, Boolean(doc.scan_failed_open));
  }
  if (isScanInProgress(doc)) return progressLights(doc, false);
  if (doc.scan_failed_open || doc.status === "interrupted") {
    // Job-level interrupt/crash is not "every check failed". Prefer real module
    // rows when early persist already wrote them; only paint all-red when nothing finished.
    if (modulesHaveOutcomes(doc)) return finishedLights(doc);
    return progressLights(doc, true);
  }
  return finishedLights(doc);
}

/** True when a finished row has any red check (failed / never returned).
 * Soft-fail Meta (warn) does not count — packaging/timestamps only. */
export function hasFailedCheck(doc) {
  if (!doc || typeof doc !== "object") return false;
  if (isScanInProgress(doc)) return false;
  return checkLights(doc).some((item) => item.state === "failed");
}

/**
 * Row clearance for the status column.
 * A red Image (or any hard-failed required check) is not a clean Complete.
 * Meta soft-fail (warn) alone still clears as Complete.
 */
export function rowClearance(doc) {
  if (!doc || typeof doc !== "object") return "unknown";
  if (isScanInProgress(doc)) return "scanning";
  // Restart mid-scan: Interrupted (with or without module greens). Failed is
  // reserved for hard module/job crashes.
  if (isRestartInterrupt(doc) || doc.status === "interrupted") return "interrupted";
  if (doc.scan_failed_open || doc.status === "failed") return "failed";
  if (hasFailedCheck(doc) || doc.ai_review === "incomplete") return "incomplete";
  if (doc.status === "complete") return "complete";
  return doc.status || "unknown";
}

/**
 * While a rescan is armed locally, hide the previous Complete / risk result
 * until the NEW job we started has actually finished (or failed). Hosted
 * matter docs hardcode status "complete", so list refreshes must not restore
 * the prior report for a millisecond flash.
 *
 * A filed date on the document is left as-is.
 */
export function documentRowView(doc, live) {
  if (!doc || !live?.armed) return doc;
  const jobId = doc.scan_job_id || null;
  const previous = live.previousJobId || null;
  const replaced = Boolean(jobId && jobId !== previous);
  const matchesJob = Boolean(live.jobId && jobId === live.jobId);

  // Only accept the server Complete after the job we started has finished and
  // the list has attached that job (and is no longer in progress).
  const serverReady =
    Boolean(live.finished) &&
    (replaced || matchesJob) &&
    doc.status === "complete" &&
    !doc.scan_in_progress &&
    !doc.scan_failed_open;
  if (serverReady) return doc;

  const view = {
    ...doc,
    risk_level: null,
    risk_score: null,
    ai_review: null,
    ai_review_reason: null,
    // Hard gate for checkLights — finished ok rows must not paint while armed.
    rescan_armed: true,
  };
  // Keep prior module NAMES for which dots to show, but never carry ok/error
  // colors into the armed view. Early local_done persist writes all-ok rows
  // into the stored report; those must stay yellow until serverReady.
  const moduleSource =
    live.modulesFromJob && Array.isArray(live.scan_modules)
      ? live.scan_modules
      : Array.isArray(view.scan_modules) && view.scan_modules.length
        ? view.scan_modules
        : Array.isArray(live.scan_modules)
          ? live.scan_modules
          : null;
  if (moduleSource) {
    view.scan_modules = moduleSource.map((mod) => {
      const name = String(mod?.name || "");
      const status = String(mod?.status || "");
      if (!name || status === "skipped") return mod;
      return { name, status: "running" };
    });
  }
  if (view.scan_profile == null && live.scan_profile != null) view.scan_profile = live.scan_profile;
  // Visual flags stay cleared until serverReady — a prior gemini_calls>0 must
  // not paint a green Visual dot mid-rescan.

  const failedNow =
    Boolean(live.failed) ||
    (replaced && (doc.status === "failed" || doc.scan_failed_open)) ||
    (live.jobId && jobId === live.jobId && (doc.status === "failed" || doc.scan_failed_open));
  if (failedNow) {
    return {
      ...view,
      status: "failed",
      scan_failed_open: true,
      scan_in_progress: false,
      rescan_armed: true,
      stage: live.stage || doc.stage || null,
    };
  }
  // Hosted list rows stay status "complete". Never copy that through while armed.
  let status = "scanning";
  if (SCANNING.has(doc.status)) {
    status = doc.status === "running" ? "scanning" : doc.status;
  }
  return {
    ...view,
    status,
    stage: live.stage || doc.stage || "queued",
    scan_in_progress: true,
    scan_failed_open: false,
    rescan_armed: true,
  };
}
