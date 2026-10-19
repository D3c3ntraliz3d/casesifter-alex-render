import assert from "node:assert/strict";
import {
  checkLights,
  documentRowView,
  formatScanError,
  modulesFromReport,
  rowClearance,
} from "../src/scanChecks.js";

const finished = {
  id: "doc-1",
  status: "complete",
  risk_level: "red",
  scan_job_id: "job-old",
  filed_date: "2024-03-01",
  scan_profile: "forensic",
  scan_modules: [
    { name: "metadata", status: "ok" },
    { name: "pdf_integrity", status: "ok" },
    { name: "structural", status: "ok" },
    { name: "image_forensics", status: "error" },
    { name: "ocr_text", status: "ok" },
    { name: "contextual_logic", status: "ok" },
    { name: "logo_chrome", status: "skipped" },
  ],
};

const done = checkLights(finished);
assert.deepEqual(
  done.map((item) => [item.label, item.state]),
  [
    ["Meta", "ok"],
    ["Structure", "ok"],
    ["Pages", "ok"],
    ["Image", "failed"],
    ["Text", "ok"],
    ["Context", "ok"],
  ]
);
// Dates is not invented when the pipeline never stored a statement_dates module.
assert.equal(done.some((item) => item.label === "Dates"), false);
// Red Image is not a clean Complete clearance.
assert.equal(rowClearance(finished), "incomplete");
assert.match(
  done.find((item) => item.label === "Image").title,
  /module error|did not finish/i
);

const depFail = checkLights({
  ...finished,
  scan_modules: [
    { name: "image_forensics", status: "error", error: "No module named 'cv2'" },
  ],
});
assert.match(
  depFail.find((item) => item.label === "Image").title,
  /missing library|Re-scan/i
);

const armed = documentRowView(finished, {
  armed: true,
  previousJobId: "job-old",
  scan_modules: finished.scan_modules,
  scan_profile: "forensic",
});
assert.equal(armed.status, "scanning");
assert.equal(armed.risk_level, null);
assert.equal(armed.filed_date, "2024-03-01");
assert.equal(armed.scan_in_progress, true);
assert.equal(armed.rescan_armed, true);
assert.equal(rowClearance(armed), "scanning");
const started = checkLights(armed);
assert.ok(started.every((item) => item.state === "running"));
assert.equal(started.some((item) => item.label === "Image"), true);
assert.equal(started.some((item) => item.label === "logo_chrome"), false);
// All-ok stored modules + Complete list row must still stay yellow while armed
// (early local_done persist used to flash every check green immediately).
const armedComplete = documentRowView(
  { ...finished, status: "complete", scan_in_progress: false },
  { armed: true, previousJobId: "job-old", scan_modules: finished.scan_modules }
);
assert.equal(armedComplete.rescan_armed, true);
assert.ok(checkLights(armedComplete).every((item) => item.state === "running"));
assert.ok(armedComplete.scan_modules.every((m) => m.status === "skipped" || m.status === "running"));

// Mid-pipeline: stay yellow until THIS job publishes module outcomes.
// A jumped stage must not invent greens from the prior report.
const mid = checkLights({ ...armed, stage: "ocr_text" });
assert.ok(mid.every((item) => item.state === "running"));
assert.equal(mid.find((item) => item.label === "Image")?.state, "running");
assert.equal(mid.find((item) => item.label === "Text")?.state, "running");

const afterLocal = checkLights({
  ...armed,
  stage: "local_done",
  scan_modules: [
    ...finished.scan_modules.filter((m) => m.name !== "logo_chrome"),
    { name: "statement_dates", status: "ok" },
  ],
});
assert.ok(afterLocal.every((item) => item.state === "running"));

// Jumping straight to generating_report must NOT paint every prior check green.
const finalize = checkLights({ ...armed, stage: "generating_report" });
assert.ok(finalize.every((item) => item.state === "running"));
assert.match(finalize.find((item) => item.label === "Image").title, /finishing|waiting|running/i);

// Mid-scan with early local modules must STILL be all yellow — no green flash.
const fromJob = documentRowView(finished, {
  armed: true,
  previousJobId: "job-old",
  jobId: "job-new",
  stage: "local_done",
  modulesFromJob: true,
  scan_modules: [
    { name: "metadata", status: "ok" },
    { name: "pdf_integrity", status: "ok" },
    { name: "structural", status: "ok" },
    { name: "image_forensics", status: "ok" },
    { name: "ocr_text", status: "ok" },
    { name: "contextual_logic", status: "ok" },
  ],
});
assert.equal(fromJob.scan_in_progress, true);
assert.equal(fromJob.rescan_armed, true);
assert.ok(fromJob.scan_modules.every((m) => m.status === "running"));
const fromJobLights = checkLights(fromJob);
assert.ok(fromJobLights.every((item) => item.state === "running"));
assert.equal(fromJobLights.find((item) => item.label === "Image")?.state, "running");
assert.equal(fromJobLights.find((item) => item.label === "Meta")?.state, "running");
// Even if something stripped scan_in_progress, rescan_armed still blocks greens.
const stripped = { ...fromJob, scan_in_progress: false, status: "complete" };
assert.ok(checkLights(stripped).every((item) => item.state === "running"));

// After the armed overlay settles, finished lights use real module outcomes.
const settledLights = checkLights({
  ...finished,
  scan_job_id: "job-new",
  scan_modules: [
    { name: "metadata", status: "ok" },
    { name: "image_forensics", status: "ok" },
    { name: "ocr_text", status: "ok" },
    { name: "contextual_logic", status: "ok" },
  ],
});
assert.equal(settledLights.find((item) => item.label === "Image")?.state, "ok");

assert.deepEqual(
  modulesFromReport({
    modules: [
      { name: "image_forensics", status: "error", extras: { error: "No module named 'cv2'" } },
      { name: "ocr_text", status: "ok" },
    ],
  }),
  [
    { name: "image_forensics", status: "error", error: "No module named 'cv2'" },
    { name: "ocr_text", status: "ok" },
  ]
);

const failed = documentRowView(finished, {
  armed: true,
  previousJobId: "job-old",
  failed: true,
  stage: "image_forensics",
  scan_modules: finished.scan_modules,
  scan_profile: "forensic",
});
assert.equal(failed.status, "failed");
assert.equal(failed.risk_level, null);
const failedLights = checkLights(failed);
const failedBy = Object.fromEntries(failedLights.map((item) => [item.label, item.state]));
// Armed fail with neutralized modules: included checks go red (did not return).
assert.equal(failedBy.Meta, "failed");
assert.equal(failedBy.Image, "failed");
assert.equal(failedBy.Text, "failed");

// Settled interrupt with early-persisted ok modules: keep real outcomes, do not
// paint every check red (job died; the checks themselves did not).
const interrupted = {
  ...finished,
  status: "interrupted",
  scan_failed_open: true,
  last_error: "interrupted_by_restart",
  scan_modules: [
    { name: "metadata", status: "ok" },
    { name: "image_forensics", status: "ok" },
    { name: "ocr_text", status: "ok" },
    { name: "contextual_logic", status: "ok" },
  ],
};
assert.equal(rowClearance(interrupted), "interrupted");
assert.match(formatScanError(interrupted), /restarted mid-scan/i);
const interruptedLights = checkLights(interrupted);
assert.equal(interruptedLights.find((item) => item.label === "Image")?.state, "ok");
assert.equal(interruptedLights.find((item) => item.label === "Meta")?.state, "ok");
// No module outcomes yet → empty or red; clearance still Interrupted.
const bareInterrupt = {
  status: "interrupted",
  scan_failed_open: true,
  last_error: "interrupted_by_restart",
  scan_modules: [],
};
assert.equal(rowClearance(bareInterrupt), "interrupted");
const bareFail = checkLights(bareInterrupt);
assert.ok(bareFail.length === 0 || bareFail.every((item) => item.state === "failed"));

// List still showing old Complete must NOT settle — stay Scanning until the
// new job we started actually finishes (live.finished).
const stillOld = documentRowView(
  { ...finished, status: "complete", risk_level: "red" },
  { armed: true, previousJobId: "job-old", jobId: "job-new" }
);
assert.equal(stillOld.status, "scanning");
assert.equal(stillOld.risk_level, null);
assert.equal(stillOld.scan_in_progress, true);

// New job id on the list with Complete, but poll has not marked finished yet.
const premature = documentRowView(
  { ...finished, scan_job_id: "job-new", risk_level: "yellow", status: "complete" },
  { armed: true, previousJobId: "job-old", jobId: "job-new" }
);
assert.equal(premature.status, "scanning");
assert.equal(premature.risk_level, null);

// Only after the new job finishes do we accept Complete / risk from the list.
const settled = documentRowView(
  {
    ...finished,
    scan_job_id: "job-new",
    risk_level: "yellow",
    status: "complete",
    scan_modules: [
      { name: "metadata", status: "ok" },
      { name: "image_forensics", status: "ok" },
      { name: "ocr_text", status: "ok" },
      { name: "contextual_logic", status: "ok" },
    ],
  },
  { armed: true, previousJobId: "job-old", jobId: "job-new", finished: true }
);
assert.equal(settled.status, "complete");
assert.equal(settled.risk_level, "yellow");
assert.equal(rowClearance(settled), "complete");

const clean = {
  ...finished,
  risk_level: "green",
  scan_modules: [
    { name: "metadata", status: "ok" },
    { name: "image_forensics", status: "ok" },
    { name: "ocr_text", status: "ok" },
    { name: "contextual_logic", status: "ok" },
  ],
};
assert.equal(rowClearance(clean), "complete");

// Real Dates module can fail.
const datesFailed = checkLights({
  ...clean,
  scan_modules: [...clean.scan_modules, { name: "statement_dates", status: "error" }],
});
assert.equal(datesFailed.find((item) => item.label === "Dates")?.state, "failed");

// Meta soft-fail: warn light + Complete clearance (not Incomplete).
const metaSoft = {
  ...clean,
  scan_modules: [
    {
      name: "metadata",
      status: "error",
      error: "Can not convert date: D:20260211214524Z'",
    },
    { name: "image_forensics", status: "ok" },
    { name: "ocr_text", status: "ok" },
    { name: "contextual_logic", status: "ok" },
  ],
};
const metaLights = checkLights(metaSoft);
assert.equal(metaLights.find((item) => item.label === "Meta")?.state, "warn");
assert.match(metaLights.find((item) => item.label === "Meta").title, /Meta warning|packaging/i);
assert.equal(rowClearance(metaSoft), "complete");

// Meta warn + hard Image fail still Incomplete.
const metaAndImage = {
  ...metaSoft,
  scan_modules: [
    { name: "metadata", status: "error", error: "Can not convert date" },
    { name: "image_forensics", status: "error", error: "No module named 'cv2'" },
    { name: "ocr_text", status: "ok" },
  ],
};
assert.equal(rowClearance(metaAndImage), "incomplete");
assert.equal(checkLights(metaAndImage).find((item) => item.label === "Meta")?.state, "warn");
assert.equal(checkLights(metaAndImage).find((item) => item.label === "Image")?.state, "failed");

console.log("scan check rescan lights ok");
