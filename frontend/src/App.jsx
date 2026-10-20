import { useEffect, useMemo, useRef, useState } from "react";
import { Archive, ChevronDown, Download, FolderOpen, MessageSquare, RefreshCw, RotateCcw, Search, ShieldAlert, Trash2, Upload, X } from "lucide-react";
import {
  archiveCase,
  askCaseChat,
  ANALYSIS_OFFLINE_MESSAGE,
  checkAnalysisHealth,
  createAnalysisJob,
  createCase,
  deleteCase,
  deleteDocument,
  downloadExportReport,
  downloadMatterExport,
  getCase,
  getEntitlement,
  getJob,
  isAnalysisUnreachableError,
  isHostedAnalysis,
  isInterruptedOrExpiredJob,
  listCaseChat,
  listCaseGotchas,
  listCases,
  listAccessLog,
  listRetryableJobs,
  absorbSessionHandoff,
  absorbSeatInviteToken,
  peekSeatInviteToken,
  createSeatInvite,
  revokeSeatInvite,
  setMemberRole,
  removeOrgMember,
  revokeMemberSessions,
  revokeFirmSessions,
  updateCase,
  loginCloud,
  loginMfaCloud,
  logoutCloud,
  requestPasswordReset,
  confirmPasswordReset,
  openCheckoutUrl,
  reportProblem,
  signupCloud,
  startCheckout,
  openBillingPortal,
  fetchAuthStatus,
  ssoStartUrl,
  startTotpEnrollment,
  confirmTotpEnrollment,
  disableTotp,
  fetchAdminOverview,
  revokeAdminSession,
  revokeOtherSessions,
  patchAdminSettings,
  fetchFirmAdminOverview,
  patchFirmAdminSettings,
  patchDocument,
  pickDesktopDocuments,
  getDesktopReport,
  isDesktopApp,
  applyLocalFindingFeedbackToList,
  isFindingIgnored,
  rescanCase,
  recomputeMatterFacts,
  retitleCaseDocuments,
  saveLicense,
  scanDocument,
  sendFindingFeedback,
  setMatterFindingFeedback,
  unarchiveCase,
  uploadCaseDocument,
} from "./api";
import { sha256OfFile } from "./localFiles.js";
import {
  checkLights,
  documentRowView,
  formatScanError,
  isScanInProgress,
  modulesFromReport,
  rowClearance,
} from "./scanChecks.js";
import { localCacheAdapter } from "./storage/localCache.js";
import {
  buildAppHref,
  parseAppRoute,
  parseNextTarget,
  writeAppHref,
} from "./appRoute.js";

const STAGES = [
  ["queued", "Queued"],
  ["local_done", "Local done"],
  ["ingesting", "File intake"],
  ["metadata", "Metadata"],
  ["pdf_integrity", "PDF integrity"],
  ["structural", "Page structure"],
  ["image_forensics", "Image forensics"],
  ["ocr_text", "Text / OCR"],
  ["contextual", "Contextual logic"],
  ["case_consistency", "Case consistency"],
  ["generating_report", "Report"],
  ["complete", "Complete"],
];

const TABS = [
  ["summary", "Summary"],
  ["findings", "Findings"],
  ["source", "Source"],
  ["timeline", "Timeline"],
  ["comparison", "Comparison"],
  ["audit", "Audit trail"],
];

const CATEGORY = {
  metadata: "Metadata",
  image: "Image",
  text: "Text / OCR",
  pdf_structure: "PDF structure",
  page_anomalies: "Page anomalies",
  contextual: "Contextual logic",
  case_consistency: "Case consistency",
  gotcha: "Apparent contradiction",
};

const RISK = {
  green: "Low risk",
  yellow: "Moderate risk",
  red: "High risk",
  incomplete: "Review incomplete",
};

function reviewDidNotComplete(report) {
  return report?.ai_review === "incomplete" || report?.comparison?.ai_review === "incomplete";
}

function unclearedReview(report) {
  if (!reviewDidNotComplete(report)) return false;
  const level = report?.risk_level;
  return !level || level === "green" || level === "incomplete";
}

function reportRiskLabel(report) {
  if (unclearedReview(report)) {
    return "Visual review did not complete — not cleared";
  }
  const drivers = report?.comparison?.score_drivers || [];
  const floor = drivers.find((row) => row && row.type === "critical_floor");
  if (floor && Number(floor.floor) >= 100) {
    return "Critical risk — strong indicators of alteration";
  }
  return RISK[report?.risk_level] || report?.risk_level || "";
}

/** Short fail-closed copy when incomplete review would otherwise look like a clean pass. */
function reportExecSummaryText(report, documentName) {
  if (
    unclearedReview(report)
    && /routine file|did not find strong signs of digital alteration|no high-confidence manipulation indicators were found/i.test(
      report.executive_summary || ""
    )
  ) {
    return (
      `Visual review of “${documentName || report.audit?.filename || "this file"}” did not complete, `
      + "so this file was not cleared. This is not a low-risk result. "
      + "No fraud finding is recorded from a review that did not finish."
    );
  }
  return report.executive_summary || "";
}

/** First 1–2 sentences for the hero; full text stays behind “Show full summary”. */
function shortExecLead(text, maxSentences = 2, maxChars = 280) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  const parts = raw.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [raw];
  let lead = parts.slice(0, maxSentences).join(" ").trim();
  if (lead.length > maxChars) {
    lead = `${lead.slice(0, maxChars - 1).trim()}…`;
  }
  return lead;
}

/** Grouped kind catalog — keep slugs stable; extend rather than rename. */
const DOC_KIND_GROUPS = [
  {
    label: "Pleadings",
    options: [
      ["complaint", "Complaint / Amended Complaint"],
      ["petition", "Petition / Amended Petition"],
      ["verified_pleading", "Verified Complaint / Verified Petition"],
      ["answer", "Answer / Amended Answer"],
      ["new_matter", "New Matter"],
      ["affirmative_defenses", "Affirmative Defenses"],
      ["counterclaim", "Counterclaim"],
      ["cross_claim", "Cross-claim"],
      ["third_party_complaint", "Third-Party Complaint"],
      ["reply", "Reply to Counterclaim / Reply to New Matter"],
    ],
  },
  {
    label: "Fact statements",
    options: [
      ["statement_of_facts", "Statement of Undisputed Material Facts"],
      ["counter_statement", "Counter-Statement / Response to Statement of Facts"],
      ["rule_19a_statement", "Rule 19-a Statement (NY) / Concise Statement of Facts"],
    ],
  },
  {
    label: "Testimony / discovery",
    options: [
      ["deposition_transcript", "Deposition transcripts"],
      ["hearing_transcript", "Hearing transcripts"],
      ["trial_transcript", "Trial transcripts"],
      ["transcript", "Transcript (other)"],
      ["interrogatory_answers", "Interrogatory answers"],
      ["rfa_responses", "Responses to Requests for Admission"],
      ["rfp_responses", "Responses to Document Requests"],
    ],
  },
  {
    label: "Arguments",
    options: [
      ["memorandum", "Memorandum of Law / Brief in Support"],
      ["brief", "Brief"],
      ["opposition", "Opposition / Response / Answer to Motion"],
      ["reply_brief", "Reply Brief / Reply Memorandum"],
      ["motion_summary_judgment", "Motion for Summary Judgment"],
      ["motion_dismiss", "Motion to Dismiss / Preliminary Objections"],
      ["motion_vacate", "Motion to Vacate / Set Aside"],
      ["motion_reargue", "Motion to Reargue / Reconsider"],
      ["order_to_show_cause", "Order to Show Cause (with supporting papers)"],
      ["pretrial_memorandum", "Pretrial Memorandum / Pretrial Statement"],
      ["proposed_findings", "Proposed Findings of Fact and Conclusions of Law"],
      ["appellate_brief", "Appellate Briefs (Appellant, Appellee, Reply)"],
      ["motion", "Motion (other)"],
    ],
  },
  {
    label: "Reference only",
    options: [
      ["order", "Orders"],
      ["decision", "Decisions / Opinions"],
      ["judgment", "Judgments"],
    ],
  },
  {
    label: "Other filings",
    options: [
      ["affidavit", "Affidavit"],
      ["affirmation", "Affirmation"],
      ["proof_of_service", "Affidavit of service"],
      ["summons", "Summons"],
      ["stipulation", "Stipulation"],
      ["letter", "Letter"],
      ["notice", "Notice"],
      ["filing", "Administrative filing"],
      ["rji", "RJI"],
      ["notice_of_pendency", "Notice of pendency"],
      ["calendar", "Calendar"],
    ],
  },
  {
    label: "Exhibits",
    options: [
      ["exhibit", "Exhibit"],
      ["bill", "Statement / bill"],
      ["evidence", "Evidence"],
    ],
  },
];

const COURT_FILING_KINDS = new Set([
  "complaint", "petition", "verified_pleading", "answer", "new_matter",
  "affirmative_defenses", "counterclaim", "cross_claim", "third_party_complaint",
  "reply", "statement_of_facts", "counter_statement", "rule_19a_statement",
  "deposition_transcript", "hearing_transcript", "trial_transcript", "transcript",
  "interrogatory_answers", "rfa_responses", "rfp_responses",
  "memorandum", "brief", "opposition", "reply_brief",
  "motion_summary_judgment", "motion_dismiss", "motion_vacate", "motion_reargue",
  "order_to_show_cause", "pretrial_memorandum", "proposed_findings",
  "appellate_brief", "motion",
  "order", "decision", "judgment",
  "affidavit", "affirmation", "letter", "stipulation", "summons", "notice",
  "filing", "proof_of_service", "rji", "notice_of_pendency", "calendar",
]);

const REFERENCE_KINDS = new Set(["order", "decision", "judgment"]);

function profileForKind(kind) {
  if (!kind) {
    return "Scan lane unset — defaults to forensic";
  }
  const code = (kind || "").toLowerCase();
  if (REFERENCE_KINDS.has(code)) {
    return "Reference only (orders / decisions / judgments — not lie-scan)";
  }
  return COURT_FILING_KINDS.has(code)
    ? "Court filing lane (content + case compare)"
    : "Exhibit lane (forensic)";
}

function kindLaneLabel(kind) {
  if (!kind) return "Untyped";
  const code = (kind || "").toLowerCase();
  if (REFERENCE_KINDS.has(code)) return "Reference";
  return COURT_FILING_KINDS.has(code) ? "Court" : "Forensic";
}

const STATEMENT_TAG_RE = /statement(?:\s|$)|issuer\s*era|statement\s*math|statement\s*dates/i;

function isStatementDoc(doc) {
  const kind = String(doc?.doc_kind || "").toLowerCase();
  if (kind === "bill") return true;
  const tags = Array.isArray(doc?.profile_tags) ? doc.profile_tags : [];
  return tags.some((tag) => STATEMENT_TAG_RE.test(String(tag || "")));
}

function kindContentHint(doc) {
  if (isStatementDoc(doc)) return "Statement";
  return kindLaneLabel(doc?.doc_kind);
}

function DocDataProfile({ profile }) {
  if (!profile || typeof profile !== "object") return null;
  const rows = [];
  if (typeof profile.pages === "number" && profile.pages > 0) {
    rows.push(["Pages", String(profile.pages)]);
  }
  if (Array.isArray(profile.organizations) && profile.organizations.length) {
    rows.push(["Orgs", profile.organizations.slice(0, 3).join(" · ")]);
  }
  if (Array.isArray(profile.people) && profile.people.length) {
    rows.push(["People", profile.people.slice(0, 3).join(" · ")]);
  }
  if (Array.isArray(profile.dates) && profile.dates.length) {
    rows.push(["Dates", profile.dates.slice(0, 3).join(" · ")]);
  }
  if (Array.isArray(profile.amounts) && profile.amounts.length) {
    rows.push(["Amounts", profile.amounts.slice(0, 3).join(" · ")]);
  }
  if (Array.isArray(profile.addresses) && profile.addresses.length) {
    rows.push(["Address", profile.addresses.slice(0, 2).join(" · ")]);
  }
  const counts = profile.finding_counts && typeof profile.finding_counts === "object"
    ? profile.finding_counts
    : null;
  if (counts) {
    const bits = ["critical", "high", "medium", "low"]
      .filter((k) => counts[k])
      .map((k) => `${counts[k]} ${k}`);
    if (bits.length) rows.push(["Findings", bits.join(", ")]);
  }
  if (!rows.length) return null;
  return (
    <dl className="doc-data-profile" aria-label="Data profile">
      {rows.map(([label, value]) => (
        <div key={label} className="doc-data-profile-row">
          <dt>{label}</dt>
          <dd title={value}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

const ACCEPTED = new Set([".pdf", ".docx", ".xlsx", ".txt", ".jpg", ".jpeg", ".png", ".gif"]);

function extensionOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

/** Readable label for long NYSCEF-style export names. */
function shortDocLabel(name) {
  const raw = String(name || "").trim();
  if (!raw) return "Untitled";
  const base = raw.replace(/\.[A-Za-z0-9]+$/, "");

  // Prefer the filing-type segment after parties (…_v_Party_TYPE_…_N).
  const typeTail = base.match(
    /_(SUMMONS(?:_WITH_NOTICE)?|STIPULATION(?:_{1,3}[A-Z]+)?|AFFIDAVIT(?:_OR_AFFIRM)?|AFFIRMATION(?:_AFFIDAV)?|COMPLAINT|ANSWER|NOTICE_OF_[A-Z0-9_]+|NOTICE_TO_[A-Z0-9_]+|ORDER(?:_{1,3}[A-Z0-9_]+)?|MOTION|LETTER(?:_{1,3}[A-Z0-9_]+)?|EXHIBIT(?:_[A-Z0-9_]+)?|RJI(?:_{1,3}[A-Z0-9_]+)?|CERTIFICATE(?:_OF_[A-Z0-9_]+)?|DECISION(?:_{1,3}[A-Z0-9_]+)?|STATEMENT_OF_[A-Z0-9_]+|NOTIFICATION(?:_FROM_[A-Z0-9_]+)?|ADDENDUM(?:_{1,3}[A-Z0-9_]+)?|REQUEST(?:_[A-Z0-9_]+)?|PROOF_OF_SERVICE)_(\d{1,4})$/i
  );
  if (typeTail) {
    const type = typeTail[1]
      .replace(/_+/g, " ")
      .toLowerCase()
      .replace(/\b\w/g, (c) => c.toUpperCase())
      .replace(/\bAdjou\b/i, "Adjournment")
      .replace(/\bAffidav\b/i, "Affidavit")
      .replace(/\bCorrespond\b/i, "Correspondence")
      .replace(/\bForeclos\b/i, "Foreclosure")
      .replace(/\bAuthor(?:ity)?\b/i, "Authority")
      .replace(/\bAppearanc\b/i, "Appearance")
      .replace(/\bResidentia\b/i, "Residential")
      .replace(/\bPendency\b/i, "Pendency")
      .replace(/\bRe\b/, "—");
    return `${type} · #${typeTail[2]}`;
  }

  if (raw.length <= 48) return raw;
  return `${raw.slice(0, 28)}…${raw.slice(-16)}`;
}

function docListTitle(doc) {
  const display = (doc.display_name || "").trim();
  const original = (doc.original_filename || "").trim();
  // Prefer a human rename; otherwise shorten the export name.
  // Keep exhibit descriptions ("Exhibit 111 — Settlement Aff"), not only "Exhibit N".
  if (display && display !== original) return display;
  if (/^exhibit\s+\S+/i.test(display) && display.includes("—")) return display;
  return shortDocLabel(display || original);
}

/** Collapse duplicate matter rows by sha (or id when sha is missing).

Prefer durable server rows over provisional/pending placeholders. Do not
collapse distinct filings that merely share an AOM / RPAPL 1303 label —
that silently hid accepted uploads from the Documents table.
*/
function dedupeDocumentList(docs) {
  const byKey = new Map();
  const order = [];
  for (const doc of docs || []) {
    const sha = String(doc.sha256 || "").toLowerCase();
    const key = sha ? `sha:${sha}` : `id:${doc.id}`;
    const prev = byKey.get(key);
    if (!prev) {
      byKey.set(key, doc);
      order.push(key);
      continue;
    }
    const prevProvisional = Boolean(prev.pending_upload || prev.provisional);
    const nextProvisional = Boolean(doc.pending_upload || doc.provisional);
    // Durable row wins; otherwise keep the first seen.
    if (prevProvisional && !nextProvisional) {
      byKey.set(key, doc);
    }
  }
  return order.map((key) => byKey.get(key));
}

const PENDING_DOCS_KEY = (matterId) => `cs_pending_docs_${matterId || ""}`;

function loadPendingDocs(matterId) {
  if (!matterId) return [];
  try {
    const raw = sessionStorage.getItem(PENDING_DOCS_KEY(matterId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function savePendingDocs(matterId, rows) {
  if (!matterId) return;
  try {
    const key = PENDING_DOCS_KEY(matterId);
    if (!rows?.length) {
      sessionStorage.removeItem(key);
      return;
    }
    // Strip File blobs — only serializable placeholder fields.
    const slim = rows.map((p) => ({
      id: p.id,
      sha256: p.sha256,
      original_filename: p.original_filename,
      display_name: p.display_name,
      doc_kind: p.doc_kind,
      status: p.status,
      scan_job_id: p.scan_job_id,
      scan_in_progress: p.scan_in_progress,
      has_report: false,
      pending_upload: true,
      provisional: true,
      last_error: p.last_error || undefined,
      stage: p.stage || undefined,
    }));
    sessionStorage.setItem(key, JSON.stringify(slim));
  } catch {
    /* private mode / quota */
  }
}

function partiesFromCaption(caption = "") {
  const raw = String(caption).replace(/\s+/g, " ").trim();
  if (!raw) return [];
  return raw
    .split(/\s+v\.?\s+|\s+vs\.?\s+|,\s*plaintiffs?,?\s*[-–—]\s*against\s+|[-–—]\s*against\s+/i)
    .map((part) => part.replace(/,?\s*(plaintiffs?|defendants?)\s*$/i, "").trim())
    .filter(Boolean);
}

function caseSearchText(item) {
  return [
    item.caption,
    item.court,
    item.index_number,
    item.notes,
    ...partiesFromCaption(item.caption),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function formatUpdatedAt(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Compact last-scan stamp for the document list. */
function formatScanAt(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const sameYear = date.getFullYear() === now.getFullYear();
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
  });
}

function matchesQuery(text, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const hay = (text || "").toLowerCase();
  return q.split(/\s+/).every((token) => hay.includes(token));
}

function SearchField({ value, onChange, placeholder, autoFocus, onKeyDown }) {
  return (
    <label className="search">
      <Search size={16} strokeWidth={2} />
      <input
        type="search"
        value={value}
        autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
      />
      {value ? (
        <button type="button" className="search-clear" onClick={() => onChange("")} aria-label="Clear search">
          <X size={14} />
        </button>
      ) : null}
    </label>
  );
}

export default function App() {
  // Restore view/matter from the URL so refresh and deep links survive entitlement boot.
  const bootRoute = useMemo(() => parseAppRoute(), []);
  const [view, setView] = useState(() => {
    if (bootRoute.view === "case" || bootRoute.view === "report") return bootRoute.view;
    if (bootRoute.view === "account" || bootRoute.view === "admin") return bootRoute.view;
    // Hosted bare `/`: wait for entitlement (avoid case-list flash for signed-out users).
    return isHostedAnalysis() ? "boot" : "cases";
  }); // boot | cases | case | report | account | admin
  const [cases, setCases] = useState([]);
  const [caseId, setCaseId] = useState(() => bootRoute.matterId || null);
  const [caseData, setCaseData] = useState(null);
  const [selectedDocId, setSelectedDocId] = useState(() => bootRoute.docId || null);
  const [job, setJob] = useState(null);
  const [tab, setTab] = useState("summary");
  const [error, setError] = useState("");
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const [analysisOffline, setAnalysisOffline] = useState(false);
  const [entitlement, setEntitlement] = useState(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const checkoutTrialStarted = useRef(false);
  const routeBootstrapped = useRef(false);
  const applyingPopState = useRef(false);
  const openCaseRef = useRef(null);
  const openDocumentReportRef = useRef(null);
  /** Last known signed-in entitlement — used so a control-plane blip does not wipe the vault. */
  const lastSignedInRef = useRef(null);

  function navigateApp(route, { replace = false, history = true } = {}) {
    const next = {
      view: route.view,
      matterId: "matterId" in route ? (route.matterId ?? null) : caseId,
      docId: "docId" in route ? (route.docId ?? null) : selectedDocId,
      next: route.next ?? null,
    };
    if (route.view) setView(route.view);
    if ("matterId" in route) {
      setCaseId(next.matterId);
      if (!next.matterId) {
        setCaseData(null);
        setSelectedDocId(null);
        setJob(null);
      }
    }
    if ("docId" in route) {
      setSelectedDocId(next.docId);
      if (!next.docId && route.view !== "report") setJob(null);
    }
    if (history && !applyingPopState.current) {
      writeAppHref(
        {
          view: route.view || view,
          matterId: next.matterId,
          docId: route.view === "report" ? next.docId : null,
          next: next.next,
        },
        { replace }
      );
    }
  }

  function goCases({ replace = false } = {}) {
    setBusy(false);
    setQuery("");
    setSelectedDocId(null);
    setJob(null);
    navigateApp({ view: "cases", matterId: null, docId: null }, { replace });
  }

  function markAnalysisOffline(err) {
    if (!isHostedAnalysis() || !isAnalysisUnreachableError(err)) return false;
    setAnalysisOffline(true);
    setBannerDismissed(false);
    setError(ANALYSIS_OFFLINE_MESSAGE);
    return true;
  }

  function dismissBanner() {
    setBannerDismissed(true);
    setError("");
  }

  // New errors re-open the single dismissible banner.
  useEffect(() => {
    if (error) setBannerDismissed(false);
  }, [error]);

  async function refreshCases() {
    const rows = await listCases();
    setCases(rows);
    if (isHostedAnalysis()) {
      setAnalysisOffline(false);
      setError((prev) => (prev === ANALYSIS_OFFLINE_MESSAGE ? "" : prev));
    }
  }

  async function refreshCase(id = caseId) {
    if (!id) return null;
    const data = await getCase(id);
    // Never silently accept a truncated matter document list.
    if (
      data &&
      data.documents_complete === false &&
      Array.isArray(data.documents)
    ) {
      const total = Number(data.documents_total);
      const got = data.documents.length;
      throw new Error(
        `Document list incomplete (${got} of ${Number.isFinite(total) ? total : "?"}). Refresh and try again — files were not dropped.`
      );
    }
    setCaseData(data);
    // A successful matter read means analysis is reachable. Drop a stale outage
    // banner left behind by an earlier failed fetch.
    if (isHostedAnalysis()) {
      setAnalysisOffline(false);
      setError((prev) => (prev === ANALYSIS_OFFLINE_MESSAGE ? "" : prev));
    }
    return data;
  }

  async function refreshEntitlement() {
    try {
      const next = await getEntitlement();
      setEntitlement(next);
      if (next?.hasSession) lastSignedInRef.current = next;
    } catch {
      // Never null out a good session on a transient failure — that hides Cases.
    }
  }

  useEffect(() => {
    // Marketing signup stores the session on casesifter.com; absorb ?cs_code= handoff here.
    absorbSessionHandoff();
    absorbSeatInviteToken();
    // Hosted multi-tenant: never list matters until we know there is a firm session.
    // Signed-out users see Account only — not another firm's cases or an empty vault tease.
    (async () => {
      await refreshEntitlement();
    })();
  }, []);

  // Browser back/forward: restore view from the URL without clobbering history.
  useEffect(() => {
    function onPopState() {
      const route = parseAppRoute();
      applyingPopState.current = true;
      setView(route.view);
      setCaseId(route.matterId);
      setSelectedDocId(route.docId);
      if (!route.matterId) {
        setCaseData(null);
        setJob(null);
      } else if (entitlement?.hasSession || !isHostedAnalysis()) {
        const open = openCaseRef.current;
        if (open) {
          open(route.matterId, { history: false }).then(() => {
            if (route.docId && openDocumentReportRef.current) {
              const doc = { id: route.docId };
              openDocumentReportRef.current(doc, { history: false });
            }
          }).catch(() => {});
        }
      }
      applyingPopState.current = false;
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [entitlement?.hasSession]);

  // After entitlement resolves: load cases when allowed; restore matter from URL once.
  useEffect(() => {
    if (!entitlement) return;
    const hosted = isHostedAnalysis();
    const signedIn = Boolean(entitlement.hasSession);
    // Control-plane unreachable with a prior/hint session: do not wipe the vault or bounce routes.
    if (hosted && !signedIn && entitlement.cached) {
      return;
    }
    if (hosted && !signedIn) {
      // Truly signed out (auth failure cleared the session). Keep return path for post-login.
      const intended = parseAppRoute();
      const returnHref = intended.matterId
        ? buildAppHref({
            view: intended.docId ? "report" : "case",
            matterId: intended.matterId,
            docId: intended.docId,
          })
        : intended.view === "admin" || intended.view === "cases"
          ? buildAppHref({ view: intended.view })
          : null;
      // Only clear the list when we know there is no session — never on flicker.
      setCases([]);
      setCaseData(null);
      setCaseId(null);
      setSelectedDocId(null);
      setJob(null);
      lastSignedInRef.current = null;
      routeBootstrapped.current = false;
      navigateApp(
        {
          view: "account",
          matterId: null,
          docId: null,
          next:
            returnHref && returnHref !== "/account" && returnHref !== "/"
              ? returnHref
              : intended.next,
        },
        { replace: true }
      );
      return;
    }

    lastSignedInRef.current = entitlement;

    refreshCases().catch((err) => {
      if (markAnalysisOffline(err)) return;
      const msg = String(err?.message || "Could not load cases.");
      // Auth errors on list: soft-gate to Account without pretending Admin.
      if (/sign in/i.test(msg)) {
        navigateApp({ view: "account", matterId: null, docId: null }, { replace: true });
        return;
      }
      setError(msg);
    });

    // One-shot deep-link restore after session is confirmed (refresh / login).
    if (routeBootstrapped.current) return;
    routeBootstrapped.current = true;

    const route = parseAppRoute();
    const fromNext = route.view === "account" ? parseNextTarget(route.next) : null;
    const target = fromNext || route;

    if (target.view === "admin") {
      if (entitlement.is_account_admin === true) {
        navigateApp({ view: "admin", matterId: null, docId: null }, { replace: true });
      } else {
        goCases({ replace: true });
      }
      return;
    }
    if (target.view === "account" && !fromNext) {
      navigateApp({ view: "account", matterId: null, docId: null }, { replace: true });
      return;
    }
    if (target.matterId) {
      const open = openCaseRef.current;
      if (open) {
        open(target.matterId, { history: "replace" }).then((data) => {
          if (!data) return;
          if (target.docId && openDocumentReportRef.current) {
            openDocumentReportRef.current({ id: target.docId }, { history: "replace" });
          }
        });
      }
      return;
    }
    // Signed-in default for bare `/`: Cases. Never auto-open Admin.
    if (view === "boot") {
      goCases({ replace: true });
    } else if (view === "cases") {
      writeAppHref({ view: "cases" }, { replace: true });
    }
  }, [entitlement?.hasSession, entitlement?.mode, entitlement?.cached]);

  // Post-signup: ?checkout=trial → start Stripe Checkout once when billing_state is none.
  useEffect(() => {
    if (!entitlement) return;
    if (checkoutTrialStarted.current) return;
    const signedIn = Boolean(entitlement.hasSession);
    if (!signedIn) return;
    if ((entitlement.billing_state || "none") !== "none") return;
    let params;
    try {
      params = new URLSearchParams(window.location.search || "");
    } catch {
      return;
    }
    if (params.get("checkout") !== "trial") return;
    checkoutTrialStarted.current = true;
    params.delete("checkout");
    const next = `${window.location.pathname}${params.toString() ? `?${params}` : ""}${window.location.hash || ""}`;
    window.history.replaceState({}, "", next);
    (async () => {
      setError("");
      try {
        const session = await startCheckout(true, 1);
        if (session?.url) {
          openCheckoutUrl(session.url);
          return;
        }
        if (session?.mode === "dev") {
          setError(session.message || "Trial updated (Stripe not configured).");
        }
        await refreshEntitlement();
      } catch (err) {
        setError(err.message || "Could not start checkout.");
        navigateApp({ view: "account", matterId: null, docId: null }, { replace: true });
        await refreshEntitlement();
      }
    })();
  }, [entitlement]);

  // Desktop: refresh while any doc is queued/scanning so status flips show up.
  useEffect(() => {
    if (!isDesktopApp()) return;
    if (!caseData?.id) return;
    const active = (caseData.documents || []).some(
      (d) => d.status === "queued" || d.status === "scanning"
    );
    if (!active) return;
    const timer = setInterval(() => {
      refreshCase(caseData.id).catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
    // Intentionally depend on a cheap signature, not the documents array identity,
    // so each refresh does not tear down and recreate the interval.
  }, [
    caseData?.id,
    (caseData?.documents || [])
      .filter((d) => d.status === "queued" || d.status === "scanning")
      .map((d) => d.id)
      .join(","),
  ]);

  useEffect(() => {
    if (isDesktopApp()) return;
    if (!caseData?.documents?.length) return;
    const pending = caseData.documents.filter(
      (d) => d.scan_job_id && d.status !== "complete" && d.status !== "failed" && d.status !== "uploaded"
    );
    if (!pending.length) return;
    const timer = setInterval(async () => {
      try {
        const next = await refreshCase(caseData.id);
        if (selectedDocId) {
          const doc = next?.documents?.find((d) => d.id === selectedDocId);
          if (doc?.scan_job_id) {
            const progress = await getJob(doc.scan_job_id);
            if (progress.status === "complete") {
              setJob(await getJob(doc.scan_job_id, { includeReport: true }));
            } else {
              setJob(progress);
            }
          }
        }
      } catch {
        /* keep polling */
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [caseData?.id, caseData?.documents, selectedDocId]);

  async function openCase(id, opts = {}) {
    if (!id) {
      setError("Could not open that case (missing id).");
      return null;
    }
    const historyMode = opts.history === false ? "none" : opts.history === "replace" ? "replace" : "push";
    setError("");
    setCaseId(id);
    setSelectedDocId(null);
    setJob(null);
    setTab("summary");
    try {
      const data = await refreshCase(id);
      setCaseData(data);
      setView("case");
      if (data?.storage_key_recovered && data?.message) {
        setError(String(data.message));
      }
      if (historyMode !== "none" && !applyingPopState.current) {
        writeAppHref({ view: "case", matterId: id, docId: null }, { replace: historyMode === "replace" });
      }
      return data;
    } catch (err) {
      if (!markAnalysisOffline(err)) {
        setError(err?.message || "Could not open that case.");
      }
      return null;
    }
  }
  openCaseRef.current = openCase;

  async function openDocumentReport(doc, opts = {}) {
    if (!doc?.id) return;
    const historyMode = opts.history === false ? "none" : opts.history === "replace" ? "replace" : "push";
    const matter = caseId || caseData?.id;
    setSelectedDocId(doc.id);
    setTab("summary");
    if (isDesktopApp()) {
      const report = await getDesktopReport(matter, doc.id);
      if (!report) {
        setJob(null);
        setError("This document has not been scanned yet. Use Scan on the filing list.");
        return;
      }
      setJob({
        job_id: doc.scan_job_id || doc.id,
        status: "complete",
        stage: "complete",
        percent: 100,
        message: "Report cached on this PC.",
        filename: doc.display_name || doc.original_filename,
        report,
        ephemeral: true,
      });
      setView("report");
      if (historyMode !== "none" && !applyingPopState.current && matter) {
        writeAppHref(
          { view: "report", matterId: matter, docId: doc.id },
          { replace: historyMode === "replace" }
        );
      }
      return;
    }
    const syncReportUrl = () => {
      if (historyMode !== "none" && !applyingPopState.current && matter) {
        writeAppHref(
          { view: "report", matterId: matter, docId: doc.id },
          { replace: historyMode === "replace" }
        );
      }
    };
    // Hosted matter memory: prefer persisted report, then live job.
    try {
      const { getMatterDocumentReport } = await import("./api");
      if (doc.has_report || !doc.scan_job_id) {
        try {
          const report = await getMatterDocumentReport(matter, doc.id);
          setJob({
            job_id: doc.scan_job_id || doc.id,
            status: "complete",
            stage: "complete",
            percent: 100,
            message: "Report from matter fact memory.",
            filename: doc.display_name || doc.original_filename,
            report,
            ephemeral: false,
            persist_facts: true,
          });
          setView("report");
          syncReportUrl();
          return;
        } catch {
          /* fall through to job */
        }
      }
    } catch {
      /* ignore */
    }
    if (!doc.scan_job_id) {
      setJob(null);
      setError("This document has not been scanned yet.");
      return;
    }
    const progress = await getJob(doc.scan_job_id, { includeReport: true });
    setJob(progress);
    setView("report");
    syncReportUrl();
  }
  openDocumentReportRef.current = openDocumentReport;

  const activeCases = useMemo(() => cases.filter((item) => !item.archived_at), [cases]);
  const archivedCases = useMemo(() => cases.filter((item) => item.archived_at), [cases]);
  const filteredActive = useMemo(
    () => activeCases.filter((item) => matchesQuery(caseSearchText(item), query)),
    [activeCases, query]
  );
  const filteredArchived = useMemo(
    () => archivedCases.filter((item) => matchesQuery(caseSearchText(item), query)),
    [archivedCases, query]
  );
  const searchHits = query.trim() ? [...filteredActive, ...filteredArchived] : filteredActive;

  async function handleArchive(item) {
    if (!window.confirm(`Archive “${item.caption}”? It will leave the active list and can be restored later.`)) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      await archiveCase(item.id);
      if (caseId === item.id) {
        goCases({ replace: true });
      }
      await refreshCases();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleUnarchive(item) {
    setBusy(true);
    setError("");
    try {
      await unarchiveCase(item.id);
      await refreshCases();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(item) {
    const count = item.document_count || 0;
    if (!window.confirm(`Permanently delete “${item.caption}” and ${count} local file${count === 1 ? "" : "s"}? This cannot be undone.`)) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      await deleteCase(item.id);
      if (caseId === item.id) {
        goCases({ replace: true });
      }
      await refreshCases();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleBulkArchive(ids) {
    if (!ids.length) return;
    if (!window.confirm(`Archive ${ids.length} case${ids.length === 1 ? "" : "s"}? They can be restored later.`)) return;
    setBusy(true);
    setError("");
    try {
      for (const id of ids) {
        await archiveCase(id);
        if (caseId === id) goCases({ replace: true });
      }
      await refreshCases();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleBulkUnarchive(ids) {
    if (!ids.length) return;
    setBusy(true);
    setError("");
    try {
      for (const id of ids) await unarchiveCase(id);
      await refreshCases();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleBulkDeleteCases(ids) {
    if (!ids.length) return;
    const n = ids.length;
    if (!window.confirm(`Permanently delete ${n} case${n === 1 ? "" : "s"}? This cannot be undone.`)) return;
    setBusy(true);
    setError("");
    try {
      for (const id of ids) {
        await deleteCase(id);
        if (caseId === id) goCases({ replace: true });
      }
      await refreshCases();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const billingState = entitlement?.billing_state || (entitlement?.active ? "active" : "none");
  const signedIn = Boolean(entitlement?.hasSession);
  const canCreateCase = billingState === "active" || billingState === "trialing";

  async function handleNewCaseIntent() {
    if (!signedIn) {
      setError("");
      navigateApp({ view: "account", matterId: null, docId: null });
      return false;
    }
    if (!canCreateCase) {
      setError("");
      navigateApp({ view: "account", matterId: null, docId: null });
      if (billingState === "none") {
        try {
          const session = await startCheckout(true, 1);
          if (session?.url) {
            openCheckoutUrl(session.url);
          } else if (session?.mode === "dev") {
            setError(session.message || "Trial updated (Stripe not configured).");
            await refreshEntitlement();
          }
        } catch (err) {
          setError(err.message || "Could not start checkout.");
          await refreshEntitlement();
        }
      }
      return false;
    }
    if (analysisOffline) {
      setError(ANALYSIS_OFFLINE_MESSAGE);
      return false;
    }
    return true;
  }

  function planBadge() {
    if (!entitlement) {
      return <span className="status-dot">…</span>;
    }
    if (billingState === "trialing" && entitlement.trial_days_left != null) {
      const n = entitlement.trial_days_left;
      return (
        <span className="status-dot live">
          Trial · {n} day{n === 1 ? "" : "s"} left
        </span>
      );
    }
    if (signedIn && billingState === "none") {
      return (
        <button
          type="button"
          className="status-dot clickable"
          onClick={() => {
            setError("");
            refreshEntitlement();
            navigateApp({ view: "account", matterId: null, docId: null });
          }}
        >
          Start 14-day trial
        </button>
      );
    }
    if (entitlement.active) {
      return (
        <span className={`status-dot live`}>
          {entitlement.plan || "Active"}
        </span>
      );
    }
    if (!signedIn) {
      return (
        <button
          type="button"
          className="status-dot clickable"
          onClick={() => {
            setError("");
            navigateApp({ view: "account", matterId: null, docId: null });
          }}
        >
          Sign in
        </button>
      );
    }
    return (
      <button
        type="button"
        className="status-dot clickable"
        onClick={() => {
          setError("");
          refreshEntitlement();
          navigateApp({ view: "account", matterId: null, docId: null });
        }}
      >
        {billingState === "past_due" ? "Past due" : "No active plan"}
      </button>
    );
  }

  const sessionOk = Boolean(entitlement?.hasSession) || !isHostedAnalysis();
  const showBoot =
    view === "boot"
    || (!entitlement && isHostedAnalysis() && view !== "account" && view !== "admin");
  const showMatterLoading =
    sessionOk
    && (view === "case" || view === "report")
    && caseId
    && !caseData;

  return (
    <div className="app">
      <header className="masthead">
        <button
          type="button"
          className="brand"
          onClick={() => {
            if (isHostedAnalysis() && entitlement && !entitlement.hasSession && !entitlement.cached) {
              navigateApp({ view: "account", matterId: null, docId: null });
              return;
            }
            goCases();
          }}
        >
          <div className="seal">CS</div>
          <div>
            <h1>CaseSifter</h1>
            <p>Find what matters in the record.</p>
          </div>
        </button>
        {sessionOk ? (
        <div className="mast-search">
          <SearchField
            value={query}
            onChange={setQuery}
            placeholder="Search cases, parties, index, or court…"
            onKeyDown={(e) => {
              if (e.key === "Enter" && searchHits.length === 1) {
                setQuery("");
                openCase(searchHits[0].id);
              }
            }}
          />
          {query.trim() && view !== "cases" && searchHits.length > 0 && (
            <div className="search-results">
              {searchHits.slice(0, 8).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    setQuery("");
                    openCase(item.id);
                  }}
                >
                  <strong>{item.caption}</strong>
                  <span>
                    {[item.archived_at ? "Archived" : null, item.index_number, item.court].filter(Boolean).join(" · ") || "No index"}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        ) : (
          <div className="mast-search mast-search-locked">
            <p className="footnote" style={{ margin: 0 }}>Sign in to open your firm&apos;s matters.</p>
          </div>
        )}
        <nav className="mast-nav">
          {sessionOk ? (
            <button
              type="button"
              className={view !== "account" && view !== "admin" && view !== "boot" ? "on" : ""}
              onClick={() => goCases()}
            >
              Cases
            </button>
          ) : null}
          <button
            type="button"
            className={view === "account" || (isHostedAnalysis() && entitlement && !entitlement.hasSession && !entitlement.cached) ? "on" : ""}
            onClick={() => {
              // Account talks to the control plane; drop leftover case/analysis noise.
              setError("");
              refreshEntitlement();
              navigateApp({ view: "account", matterId: null, docId: null });
            }}
          >
            Account
          </button>
          {entitlement?.hasSession && entitlement?.is_account_admin === true ? (
            <button
              type="button"
              className={view === "admin" ? "on" : ""}
              onClick={() => {
                setError("");
                navigateApp({ view: "admin", matterId: null, docId: null });
              }}
            >
              Admin
            </button>
          ) : null}
          {planBadge()}
        </nav>
      </header>

      {(() => {
        const bannerText =
          error ||
          (analysisOffline && view !== "account" && view !== "admin" && !bannerDismissed
            ? ANALYSIS_OFFLINE_MESSAGE
            : "");
        if (!bannerText || bannerDismissed) return null;
        return (
          <div className="banner error dismissible" role="alert">
            <span className="banner-text">{bannerText}</span>
            <button
              type="button"
              className="banner-dismiss"
              aria-label="Dismiss"
              onClick={dismissBanner}
            >
              <X size={16} />
            </button>
          </div>
        );
      })()}

      {showBoot && (
        <div className="card">
          <p>Loading your firm session…</p>
        </div>
      )}

      {view === "cases" && sessionOk && (
        <CasesPage
          cases={filteredActive}
          archived={filteredArchived}
          totalCount={activeCases.length}
          archivedCount={archivedCases.length}
          query={query}
          busy={busy}
          analysisOffline={analysisOffline}
          onRequestNewCase={handleNewCaseIntent}
          onCreate={async (payload) => {
            setBusy(true);
            setError("");
            try {
              const created = await createCase(payload);
              await refreshCases();
              await openCase(created.id);
            } catch (err) {
              if (!markAnalysisOffline(err)) setError(err.message);
            } finally {
              setBusy(false);
            }
          }}
          onOpen={openCase}
          onArchive={handleArchive}
          onUnarchive={handleUnarchive}
          onDelete={handleDelete}
          onBulkArchive={handleBulkArchive}
          onBulkUnarchive={handleBulkUnarchive}
          onBulkDelete={handleBulkDeleteCases}
        />
      )}

      {showMatterLoading && (
        <div className="card">
          <p>Opening matter…</p>
        </div>
      )}

      {view === "case" && caseData && sessionOk && (
        <CaseWorkspace
          caseData={caseData}
          busy={busy}
          entitlement={entitlement}
          onBack={() => goCases()}
          onRefresh={() => refreshCase(caseData.id)}
          onError={setError}
          onOpenReport={openDocumentReport}
          setBusy={setBusy}
          onArchive={() => handleArchive(caseData)}
          onUnarchive={() => handleUnarchive(caseData)}
          onDelete={() => handleDelete(caseData)}
        />
      )}

      {view === "report" && sessionOk && (
        <>
          <div className="crumb-bar">
            <button
              className="text-link"
              type="button"
              onClick={() => {
                const id = caseId || caseData?.id;
                if (id) {
                  navigateApp({ view: "case", matterId: id, docId: null });
                } else {
                  goCases();
                }
              }}
            >
              ← Back to case
            </button>
          </div>
          {job && job.status !== "complete" && job.status !== "failed" && (
            <ProgressPanel job={job} filename={job.filename} />
          )}
          {job?.status === "failed" && (
            <div className="card">
              <h2 className="serif">Analysis could not be completed</h2>
              <p>{job.error || job.message}</p>
            </div>
          )}
          {job?.status === "complete" && job.report && (
            <ReportViewer
              job={job}
              report={job.report}
              findings={job.report.findings || []}
              tab={tab}
              setTab={setTab}
              docKind={caseData?.documents?.find((d) => d.id === selectedDocId)?.doc_kind}
              caseId={caseId}
              documentId={selectedDocId}
              documentName={
                caseData?.documents?.find((d) => d.id === selectedDocId)?.display_name
                || job.filename
              }
              documents={caseData?.documents || []}
              onOpenReport={openDocumentReport}
              onError={setError}
            />
          )}
        </>
      )}

      {view === "account" && (
        <AccountPanel
          entitlement={entitlement}
          onRefresh={refreshEntitlement}
          onError={setError}
        />
      )}

      {view === "admin" && (
        <AdminPanel
          entitlement={entitlement}
          onError={setError}
          onRefresh={refreshEntitlement}
          onOpenAccount={() => {
            setError("");
            navigateApp({ view: "account", matterId: null, docId: null });
          }}
        />
      )}
    </div>
  );
}

function CasesPage({
  cases,
  archived,
  totalCount,
  archivedCount,
  query,
  busy,
  analysisOffline = false,
  onRequestNewCase,
  onCreate,
  onOpen,
  onArchive,
  onUnarchive,
  onDelete,
  onBulkArchive,
  onBulkUnarchive,
  onBulkDelete,
}) {
  const [caption, setCaption] = useState("");
  const [court, setCourt] = useState("");
  const [indexNumber, setIndexNumber] = useState("");
  const [showNew, setShowNew] = useState(false);
  const [selectedOpen, setSelectedOpen] = useState(() => new Set());
  const [selectedArchived, setSelectedArchived] = useState(() => new Set());

  useEffect(() => {
    if (analysisOffline) setShowNew(false);
  }, [analysisOffline]);

  useEffect(() => {
    setSelectedOpen((prev) => {
      const ids = new Set(cases.map((c) => c.id));
      return new Set([...prev].filter((id) => ids.has(id)));
    });
  }, [cases]);

  useEffect(() => {
    setSelectedArchived((prev) => {
      const ids = new Set(archived.map((c) => c.id));
      return new Set([...prev].filter((id) => ids.has(id)));
    });
  }, [archived]);

  function toggleOpen(id) {
    setSelectedOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleArchived(id) {
    setSelectedArchived((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const allOpenSelected = cases.length > 0 && cases.every((c) => selectedOpen.has(c.id));
  const allArchivedSelected = archived.length > 0 && archived.every((c) => selectedArchived.has(c.id));

  async function openNewCaseForm() {
    if (onRequestNewCase) {
      const ok = await onRequestNewCase();
      if (!ok) return;
    }
    setShowNew(true);
  }

  return (
    <>
      <div className="page-head">
        <div>
          <p className="kicker">Your matters</p>
          <h2 className="serif cases-page-title">Cases</h2>
          <p className="footnote">
            {analysisOffline
              ? "Cases are unavailable until analysis is back online."
              : query.trim()
                ? `${cases.length} match${cases.length === 1 ? "" : "es"}`
                : totalCount === 0
                  ? "Open a case, drop in files, see what stands out."
                  : `${totalCount} open · open one to continue`}
            {!analysisOffline && archivedCount ? ` · ${archivedCount} archived` : ""}
          </p>
        </div>
        <button
          className="btn gold"
          type="button"
          disabled={busy || (analysisOffline && !showNew)}
          title={analysisOffline ? ANALYSIS_OFFLINE_MESSAGE : undefined}
          onClick={() => {
            if (showNew) {
              setShowNew(false);
              return;
            }
            openNewCaseForm();
          }}
        >
          {showNew ? "Cancel" : "New case"}
        </button>
      </div>

      {showNew && (
        <div className="card quiet">
          <p className="kicker">About a minute</p>
          <h3 className="serif" style={{ margin: "4px 0 8px" }}>Name the case</h3>
          <form
            className="case-form inline"
            onSubmit={(e) => {
              e.preventDefault();
              if (!caption.trim()) return;
              onCreate({
                caption: caption.trim(),
                court: court.trim() || null,
                index_number: indexNumber.trim() || null,
              });
              setCaption("");
              setCourt("");
              setIndexNumber("");
              setShowNew(false);
            }}
          >
            <label>
              Caption
              <input value={caption} onChange={(e) => setCaption(e.target.value)} placeholder="Bank v. Defendant" required autoFocus />
            </label>
            <label>
              Court
              <input value={court} onChange={(e) => setCourt(e.target.value)} placeholder="Optional" />
            </label>
            <label>
              Index
              <input value={indexNumber} onChange={(e) => setIndexNumber(e.target.value)} placeholder="Optional" />
            </label>
            <button className="btn gold" type="submit" disabled={busy || !caption.trim()}>
              Open
            </button>
          </form>
        </div>
      )}

      <div className="card flush">
        {cases.length === 0 ? (
          <div className="empty">
            <FolderOpen size={28} strokeWidth={1.5} />
            <p>
              {analysisOffline
                ? "Can't reach analysis"
                : totalCount === 0
                  ? "No cases yet"
                  : "Nothing matches that search"}
            </p>
            {analysisOffline ? (
              <p className="footnote">
                Account and billing still work. Try again shortly, or contact support if this continues.
              </p>
            ) : totalCount === 0 ? (
              <>
                <p className="footnote">Create a case, add filings, and CaseSifter flags what needs a look.</p>
                <button className="btn gold" type="button" onClick={() => openNewCaseForm()}>
                  New case
                </button>
              </>
            ) : null}
          </div>
        ) : (
          <>
            <div className="bulk-bar">
              <label className="bulk-select-all">
                <input
                  type="checkbox"
                  checked={allOpenSelected}
                  disabled={busy}
                  onChange={() => {
                    if (allOpenSelected) setSelectedOpen(new Set());
                    else setSelectedOpen(new Set(cases.map((c) => c.id)));
                  }}
                />
                Select all
              </label>
              {selectedOpen.size > 0 && (
                <div className="bulk-actions">
                  <span className="footnote">{selectedOpen.size} selected</span>
                  <button
                    className="btn secondary"
                    type="button"
                    disabled={busy}
                    onClick={async () => {
                      await onBulkArchive([...selectedOpen]);
                      setSelectedOpen(new Set());
                    }}
                  >
                    <Archive size={14} /> Archive
                  </button>
                  <button
                    className="btn secondary danger"
                    type="button"
                    disabled={busy}
                    onClick={async () => {
                      await onBulkDelete([...selectedOpen]);
                      setSelectedOpen(new Set());
                    }}
                  >
                    <Trash2 size={14} /> Delete
                  </button>
                </div>
              )}
            </div>
            <div className="case-list">
              {cases.map((item) => (
                <CaseTile
                  key={item.id}
                  item={item}
                  busy={busy}
                  selected={selectedOpen.has(item.id)}
                  onToggleSelect={() => toggleOpen(item.id)}
                  onOpen={onOpen}
                  onArchive={onArchive}
                  onDelete={onDelete}
                />
              ))}
            </div>
          </>
        )}
      </div>

      {(archived.length > 0 || (!query.trim() && archivedCount > 0)) && (
        <details className="archive-panel" open={Boolean(query.trim())}>
          <summary>
            Archived
            <span>{query.trim() ? `${archived.length} shown` : archivedCount}</span>
          </summary>
          {archived.length === 0 ? (
            <p className="empty-copy">No archived cases match that search.</p>
          ) : (
            <>
              <div className="bulk-bar" style={{ padding: "8px 12px 0" }}>
                <label className="bulk-select-all">
                  <input
                    type="checkbox"
                    checked={allArchivedSelected}
                    disabled={busy}
                    onChange={() => {
                      if (allArchivedSelected) setSelectedArchived(new Set());
                      else setSelectedArchived(new Set(archived.map((c) => c.id)));
                    }}
                  />
                  Select all
                </label>
                {selectedArchived.size > 0 && (
                  <div className="bulk-actions">
                    <span className="footnote">{selectedArchived.size} selected</span>
                    <button
                      className="btn secondary"
                      type="button"
                      disabled={busy}
                      onClick={async () => {
                        await onBulkUnarchive([...selectedArchived]);
                        setSelectedArchived(new Set());
                      }}
                    >
                      <RotateCcw size={14} /> Restore
                    </button>
                    <button
                      className="btn secondary danger"
                      type="button"
                      disabled={busy}
                      onClick={async () => {
                        await onBulkDelete([...selectedArchived]);
                        setSelectedArchived(new Set());
                      }}
                    >
                      <Trash2 size={14} /> Delete
                    </button>
                  </div>
                )}
              </div>
              <div className="case-list" style={{ padding: "8px 12px 12px" }}>
                {archived.map((item) => (
                  <CaseTile
                    key={item.id}
                    item={item}
                    busy={busy}
                    selected={selectedArchived.has(item.id)}
                    onToggleSelect={() => toggleArchived(item.id)}
                    onOpen={onOpen}
                    onArchive={onUnarchive}
                    onDelete={onDelete}
                    archiveMode
                  />
                ))}
              </div>
            </>
          )}
        </details>
      )}
    </>
  );
}

function CaseTile({ item, busy, selected, onToggleSelect, onOpen, onArchive, onDelete, archiveMode = false }) {
  const parties = partiesFromCaption(item.caption);
  const subtitle = archiveMode
    ? [item.index_number, formatUpdatedAt(item.updated_at)].filter(Boolean).join(" · ")
    : [
        item.index_number,
        item.court,
        parties.length > 1 ? parties.join(" · ") : null,
      ].filter(Boolean).join(" · ");
  const riskBand = ["red", "yellow", "incomplete"].includes(item.highest_risk)
    ? item.highest_risk
    : "";
  const riskLabel =
    riskBand === "red"
      ? "High"
      : riskBand === "yellow"
        ? "Moderate"
        : riskBand === "incomplete"
          ? "Incomplete"
          : null;
  return (
    <div
      className={`case-tile${selected ? " selected" : ""}${riskBand ? ` risk-tile risk-${riskBand}` : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(item.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(item.id);
        }
      }}
    >
      <label className="case-tile-check" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={Boolean(selected)}
          disabled={busy}
          onChange={onToggleSelect}
          aria-label={`Select ${item.caption}`}
        />
      </label>
      <div className="case-tile-main">
        <strong>{item.caption}</strong>
        <span>{subtitle || "Open matter"}</span>
      </div>
      {riskLabel ? (
        <span className={`case-tile-risk risk-${riskBand}`}>{riskLabel}</span>
      ) : (
        <span className="case-tile-risk">—</span>
      )}
      <div className="case-tile-meta">
        <span className="files" title="Files">{item.document_count || 0}</span>
        <div className="case-tile-actions" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            className="icon-btn"
            disabled={busy}
            title={archiveMode ? "Restore" : "Archive"}
            onClick={() => onArchive(item)}
          >
            {archiveMode ? <RotateCcw size={15} /> : <Archive size={15} />}
          </button>
          <button type="button" className="icon-btn danger" disabled={busy} title="Delete" onClick={() => onDelete(item)}>
            <Trash2 size={15} />
          </button>
        </div>
      </div>
    </div>
  );
}

const DOC_PAGE_SIZE = 20;
const ACCESS_LOG_PAGE_SIZE = 20;

function PaginationBar({ page, pageSize, total, onChange, label = "items" }) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(Math.max(0, page), pageCount - 1);
  const from = total === 0 ? 0 : safePage * pageSize + 1;
  const to = Math.min(total, (safePage + 1) * pageSize);
  if (total <= pageSize) {
    return total > 0 ? (
      <p className="pagination-bar footnote">
        {total} {label}
      </p>
    ) : null;
  }
  return (
    <div className="pagination-bar">
      <span className="footnote">
        {from}–{to} of {total} {label}
      </span>
      <div className="pagination-actions">
        <button
          type="button"
          className="btn secondary"
          disabled={safePage <= 0}
          onClick={() => onChange(safePage - 1)}
        >
          Previous
        </button>
        <span className="footnote">
          Page {safePage + 1} / {pageCount}
        </span>
        <button
          type="button"
          className="btn secondary"
          disabled={safePage >= pageCount - 1}
          onClick={() => onChange(safePage + 1)}
        >
          Next
        </button>
      </div>
    </div>
  );
}

function CaseWorkspace({ caseData, busy, entitlement, onBack, onRefresh, onError, onOpenReport, setBusy, onArchive, onUnarchive, onDelete }) {
  const [editingMatter, setEditingMatter] = useState(false);
  const [editCaption, setEditCaption] = useState("");
  const [editIndex, setEditIndex] = useState("");
  const [editCourt, setEditCourt] = useState("");
  const [savingMatter, setSavingMatter] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState("");
  const [docQuery, setDocQuery] = useState("");
  const [docPage, setDocPage] = useState(0);
  const [docFilters, setDocFilters] = useState({
    risk: "",
    clearance: "",
    kind: "",
    issue: "",
  });
  const [uploadNote, setUploadNote] = useState("");
  const [indexMismatchNote, setIndexMismatchNote] = useState("");
  const [scanBacklogError, setScanBacklogError] = useState("");
  const [selectedDocs, setSelectedDocs] = useState(() => new Set());
  const [stampById, setStampById] = useState({});
  const [liveScan, setLiveScan] = useState({});
  // Hosted uploads create a job before a matter document row exists. Keep
  // placeholders so Documents is not empty while the footnote tracks progress.
  const [pendingDocs, setPendingDocs] = useState(() => loadPendingDocs(caseData?.id));
  const [failedBatch, setFailedBatch] = useState([]);
  const [cacheCoverage, setCacheCoverage] = useState(null);
  const [cacheBannerDismissed, setCacheBannerDismissed] = useState(false);
  const [locatingFiles, setLocatingFiles] = useState(false);
  const [locateNote, setLocateNote] = useState("");
  const [exportingMatter, setExportingMatter] = useState(false);
  const [listIncompleteError, setListIncompleteError] = useState("");
  const inputRef = useRef(null);
  const backfillKeyRef = useRef("");
  const scanningRef = useRef(false);
  const autoRetryKeyRef = useRef("");
  // New array every render would retrigger effects that list this as a dependency.
  const docs = useMemo(() => {
    const server = caseData.documents || [];
    const serverShas = new Set(
      server.map((d) => String(d.sha256 || "").toLowerCase()).filter(Boolean)
    );
    // Prefer server rows (durable or provisional job stubs). Keep client
    // placeholders only for uploads the matter list has not confirmed yet.
    const extras = (pendingDocs || []).filter((p) => {
      const sha = String(p.sha256 || "").toLowerCase();
      if (sha && serverShas.has(sha)) return false;
      const jobId = p.scan_job_id;
      if (
        jobId &&
        server.some((d) => d.scan_job_id === jobId || d.id === `pending:${jobId}`)
      ) {
        return false;
      }
      return true;
    });
    return dedupeDocumentList([...server, ...extras]);
  }, [caseData.documents, pendingDocs]);
  const shownDocs = useMemo(
    () => docs.map((doc) => documentRowView(doc, liveScan[doc.id])),
    [docs, liveScan]
  );

  useEffect(() => {
    // Restore session placeholders when opening a matter (refresh / navigate back).
    setPendingDocs(loadPendingDocs(caseData.id));
    setListIncompleteError("");
  }, [caseData.id]);

  useEffect(() => {
    savePendingDocs(caseData.id, pendingDocs);
  }, [caseData.id, pendingDocs]);

  useEffect(() => {
    if (
      caseData &&
      caseData.documents_complete === false &&
      Array.isArray(caseData.documents)
    ) {
      const total = Number(caseData.documents_total);
      const got = caseData.documents.length;
      setListIncompleteError(
        `Document list incomplete (${got} of ${Number.isFinite(total) ? total : "?"}). Refresh — do not assume files were deleted.`
      );
    } else {
      setListIncompleteError("");
    }
  }, [caseData]);

  useEffect(() => {
    const server = caseData.documents || [];
    const serverShas = new Set(
      server.map((d) => String(d.sha256 || "").toLowerCase()).filter(Boolean)
    );
    const serverJobIds = new Set(
      server
        .map((d) => {
          if (d.scan_job_id) return d.scan_job_id;
          const id = String(d.id || "");
          return id.startsWith("pending:") ? id.slice(8) : null;
        })
        .filter(Boolean)
    );
    // Empty refresh must not wipe placeholders for in-flight uploads.
    if (!serverShas.size && !serverJobIds.size) return;
    setPendingDocs((prev) => {
      const next = prev.filter((p) => {
        const sha = String(p.sha256 || "").toLowerCase();
        if (sha && serverShas.has(sha)) return false;
        if (p.scan_job_id && serverJobIds.has(p.scan_job_id)) return false;
        return true;
      });
      return next.length === prev.length ? prev : next;
    });
  }, [caseData.documents]);

  function armRescan(doc) {
    if (!doc?.id) return;
    // Armed overlay forces yellow via rescan_armed. Do not flushSync here —
    // React warns when arming from a nested update path.
    setLiveScan((prev) => ({
      ...prev,
      [doc.id]: {
        armed: true,
        previousJobId: doc.scan_job_id || null,
        scan_modules: doc.scan_modules,
        scan_profile: doc.scan_profile,
      },
    }));
  }

  function disarmRescan(docId) {
    setLiveScan((prev) => {
      if (!prev[docId]) return prev;
      const next = { ...prev };
      delete next[docId];
      return next;
    });
  }

  function noteJob(docId, jobId, stage) {
    if (!docId || !jobId) return;
    setLiveScan((prev) => {
      const cur = prev[docId];
      if (!cur) return prev;
      if (cur.jobId === jobId && (stage == null || cur.stage === stage)) return prev;
      return { ...prev, [docId]: { ...cur, jobId, stage: stage || cur.stage } };
    });
  }

  useEffect(() => {
    setLiveScan((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const doc of docs) {
        const live = next[doc.id];
        if (!live?.armed) continue;
        const jobId = doc.scan_job_id || null;
        const replaced = Boolean(jobId && jobId !== (live.previousJobId || null));
        const matchesJob = Boolean(live.jobId && jobId === live.jobId);
        // Hosted matters hardcode status "complete". Only drop the armed
        // overlay after the job we started finished and the list attached it.
        const settled =
          Boolean(live.finished) &&
          (replaced || matchesJob) &&
          doc.status === "complete" &&
          !doc.scan_in_progress &&
          !doc.scan_failed_open;
        if (settled) {
          delete next[doc.id];
          changed = true;
          continue;
        }
        if (live.failed && (doc.status === "failed" || doc.scan_failed_open)) {
          delete next[doc.id];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [docs]);

  useEffect(() => {
    if (isDesktopApp()) return undefined;
    // Stay armed (and keep refreshing) until the list attaches the new job.
    const pending = Object.entries(liveScan).filter(
      ([, live]) => live?.armed && live.jobId && !live.failed
    );
    if (!pending.length) return undefined;
    let cancelled = false;
    const timer = setInterval(async () => {
      for (const [docId, live] of pending) {
        try {
          if (live.finished) {
            try {
              await onRefresh();
            } catch {
              /* retry next tick */
            }
            continue;
          }
          const progress = await getJob(live.jobId);
          if (cancelled) return;
          const status = String(progress?.status || "");
          if (!status || status === "unknown") continue;
          // Do not apply module greens until the job is finished. Early
          // local_done scan_modules were painting every check green at once.
          if (status === "complete") {
            let modules = Array.isArray(progress?.scan_modules) ? progress.scan_modules : null;
            let visual = {
              visual_review_required: progress?.visual_review_required,
              review_mode: progress?.review_mode,
              gemini_calls: progress?.gemini_calls,
              visual_file_sent: progress?.visual_file_sent,
              document_analysis: progress?.document_analysis,
              ai_review: progress?.ai_review,
              ai_review_reason: progress?.ai_review_reason,
            };
            if (!modules?.length) {
              try {
                const full = await getJob(live.jobId, { includeReport: true });
                modules = modulesFromReport(full?.report);
                if (full?.ai_review != null) visual.ai_review = full.ai_review;
                if (full?.ai_review_reason != null) visual.ai_review_reason = full.ai_review_reason;
                if (full?.report) {
                  const rep = full.report;
                  const comparison = rep.comparison || {};
                  if (visual.visual_review_required == null && comparison.visual_review_required != null) {
                    visual.visual_review_required = comparison.visual_review_required;
                  }
                  if (!visual.review_mode && comparison.review_mode) {
                    visual.review_mode = comparison.review_mode;
                  }
                }
              } catch {
                /* attach from the matter list on refresh */
              }
            }
            setLiveScan((prev) => {
              const cur = prev[docId];
              if (!cur) return prev;
              if (cur.finished && cur.modulesFromJob) return prev;
              return {
                ...prev,
                [docId]: {
                  ...cur,
                  stage: "generating_report",
                  finished: true,
                  ...(modules?.length
                    ? {
                        scan_modules: modules,
                        modulesFromJob: true,
                        visual_review_required: visual.visual_review_required,
                        review_mode: visual.review_mode,
                        gemini_calls: visual.gemini_calls,
                        visual_file_sent: visual.visual_file_sent,
                        document_analysis: visual.document_analysis,
                        ai_review: visual.ai_review,
                        ai_review_reason: visual.ai_review_reason,
                      }
                    : {}),
                },
              };
            });
            try {
              await onRefresh();
            } catch {
              /* the next poll retries */
            }
            continue;
          }
          if (status === "failed" || status === "interrupted") {
            setLiveScan((prev) => {
              const cur = prev[docId];
              if (!cur || cur.failed) return prev;
              return { ...prev, [docId]: { ...cur, failed: true, stage: progress.stage || cur.stage } };
            });
            try {
              await onRefresh();
            } catch {
              /* keep the local failure */
            }
            continue;
          }
          const stage = progress.stage || null;
          if (stage && stage !== live.stage) {
            setLiveScan((prev) => {
              const cur = prev[docId];
              if (!cur || cur.stage === stage) return prev;
              return { ...prev, [docId]: { ...cur, stage } };
            });
          }
        } catch {
          /* keep the yellow row until the next poll */
        }
      }
    }, 500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [liveScan, onRefresh]);

  useEffect(() => {
    setCacheBannerDismissed(false);
    setLocateNote("");
  }, [caseData.id]);

  useEffect(() => {
    if (!isHostedAnalysis() || !docs.length) {
      setCacheCoverage(null);
      return;
    }
    let cancelled = false;
    import("./storage/localCache.js")
      .then(({ cacheCoverageForDocs }) => cacheCoverageForDocs(docs))
      .then((cov) => {
        if (!cancelled) setCacheCoverage(cov);
      })
      .catch(() => {
        if (!cancelled) setCacheCoverage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [docs, caseData.id, uploadNote]);

  // Hosted: auto-resubmit stall/timeout/interrupted jobs from IndexedDB once per matter session.
  // Server enforces attempts per matter+sha; sessionStorage prevents reload loops.
  useEffect(() => {
    if (!isHostedAnalysis() || caseData.archived_at) return;
    const key = String(caseData.id || "");
    if (!key) return;
    const sessionKey = `cs_auto_retry_${key}`;
    try {
      if (sessionStorage.getItem(sessionKey) === "1") return;
    } catch {
      /* private mode */
    }
    if (autoRetryKeyRef.current === key) return;
    autoRetryKeyRef.current = key;
    let cancelled = false;
    (async () => {
      try {
        const { jobs, exhausted } = await listRetryableJobs(caseData.id);
        if (cancelled) return;
        try {
          sessionStorage.setItem(sessionKey, "1");
        } catch {
          /* private mode */
        }
        const misses = [];
        let created = 0;
        let skippedActive = 0;
        let skippedExhausted = 0;
        const exhaustedShas = new Set(
          (exhausted || []).map((r) => String(r.sha256 || "").toLowerCase()).filter(Boolean)
        );
        if (!(jobs || []).length) {
          if ((exhausted || []).length) {
            const quarantined = (exhausted || []).filter(
              (r) =>
                String(r.error || "") === "restart_quarantine" ||
                Number(r.restart_interruptions || 0) >= 2
            ).length;
            const other = exhausted.length - quarantined;
            const parts = [];
            if (quarantined) {
              parts.push(
                `${quarantined} need review (may be crashing the server)`
              );
            }
            if (other) {
              parts.push(
                `${other} need re-upload (failed 3 times)`
              );
            }
            setUploadNote(parts.join(" · ") + ".");
          }
          return;
        }
        const pendingJobIds = [];
        for (const row of jobs) {
          const sha = String(row.sha256 || "").toLowerCase();
          if (!sha) continue;
          if (exhaustedShas.has(sha) || Number(row.attempts || 0) >= 3) {
            skippedExhausted += 1;
            continue;
          }
          const blob = await localCacheAdapter.getFile(null, sha);
          if (!blob) {
            misses.push(sha.slice(0, 12));
            continue;
          }
          const name = `retry-${sha.slice(0, 12)}.pdf`;
          const file = new File([blob], name, { type: blob.type || "application/pdf" });
          try {
            const result = await createAnalysisJob(file, {
              matterId: caseData.id,
              submittedBy: "web-app-auto-retry",
            });
            if (result?.job_id) pendingJobIds.push(result.job_id);
            if (result?.skipped === "already_active") skippedActive += 1;
            else created += 1;
          } catch (err) {
            const msg = String(err?.message || err || "");
            if (/failed 3 times|Needs re-upload/i.test(msg)) skippedExhausted += 1;
            else if (/already queued|already running|already_active/i.test(msg)) skippedActive += 1;
          }
        }
        const parts = [];
        if (created) parts.push(`created ${created}`);
        if (skippedActive) parts.push(`skipped ${skippedActive} (already active)`);
        if (skippedExhausted || (exhausted || []).length) {
          parts.push(
            `skipped ${(skippedExhausted || 0) + (exhausted?.length || 0)} (exhausted / needs re-upload)`
          );
        }
        if (misses.length) parts.push(`${misses.length} not in cache`);
        if (parts.length) {
          setUploadNote(`Auto-retry: ${parts.join(" · ")}.`);
        }
        if (pendingJobIds.length) {
          try {
            await onRefresh();
          } catch {
            /* ignore */
          }
          let complete = 0;
          let failed = 0;
          const open = new Set(pendingJobIds);
          for (let tick = 0; tick < 900 && open.size && !cancelled; tick += 1) {
            let running = 0;
            let queued = 0;
            let bestEta = null;
            let bestPos = null;
            await Promise.all(
              [...open].map(async (jobId) => {
                try {
                  const progress = await getJob(jobId, { includeReport: false });
                  const st = String(progress?.status || "");
                  if (st === "complete") {
                    open.delete(jobId);
                    complete += 1;
                    return;
                  }
                  if (st === "failed" || st === "interrupted" || progress?.interrupted) {
                    open.delete(jobId);
                    failed += 1;
                    return;
                  }
                  if (st === "queued" || st === "pending") queued += 1;
                  else running += 1;
                  const eta = progress?.eta_seconds;
                  const pos = progress?.queue_position;
                  if (typeof eta === "number" && eta > 0) {
                    bestEta = bestEta == null ? eta : Math.max(bestEta, eta);
                  }
                  if (typeof pos === "number" && pos > 0) {
                    bestPos = bestPos == null ? pos : Math.max(bestPos, pos);
                  }
                } catch {
                  /* keep polling */
                }
              })
            );
            if (cancelled) break;
            const etaLabel =
              typeof bestEta === "number" && bestEta > 0
                ? bestEta >= 60
                  ? ` · ETA ~${Math.round(bestEta / 60)} min`
                  : ` · ETA ~${bestEta}s`
                : typeof bestPos === "number" && bestPos > 0
                  ? ` · queue #${bestPos}`
                  : "";
            setUploadNote(
              `Auto-retry: ${complete} complete · ${running} running · ${queued} queued` +
                (failed ? ` · ${failed} failed` : "") +
                etaLabel
            );
            if (!open.size) break;
            await new Promise((r) => setTimeout(r, 2000));
          }
          try {
            await onRefresh();
          } catch {
            /* ignore */
          }
        }
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [caseData.id, caseData.archived_at, onRefresh]);
  const docIdKey = docs.map((d) => d.id).join("\n");
  const waitingDocs = shownDocs.filter((d) => d.status === "uploaded" || d.status === "failed");
  const pendingOrScanning = shownDocs.filter(
    (d) =>
      Boolean(d.pending_upload) ||
      isScanInProgress(d) ||
      ["uploaded", "queued", "waiting", "scanning", "running", "local_done"].includes(d.status)
  );
  const docFiltersActive =
    Boolean(docFilters.risk) ||
    Boolean(docFilters.clearance) ||
    Boolean(docFilters.kind) ||
    Boolean(docFilters.issue) ||
    Boolean(docQuery.trim());
  const visibleDocs = shownDocs.filter((doc) => {
    const profile = doc.data_profile && typeof doc.data_profile === "object" ? doc.data_profile : {};
    const hay = [
      doc.display_name,
      doc.original_filename,
      doc.doc_kind,
      doc.status,
      doc.risk_level,
      doc.info_summary,
      ...(Array.isArray(doc.profile_tags) ? doc.profile_tags : []),
      ...(Array.isArray(profile.organizations) ? profile.organizations : []),
      ...(Array.isArray(profile.people) ? profile.people : []),
      ...(Array.isArray(profile.dates) ? profile.dates : []),
      ...(Array.isArray(profile.amounts) ? profile.amounts : []),
    ]
      .filter(Boolean)
      .join(" ");
    if (!matchesQuery(hay, docQuery)) return false;
    if (docFilters.risk && doc.risk_level !== docFilters.risk) return false;
    if (docFilters.clearance) {
      const clearance = rowClearance(doc);
      if (docFilters.clearance === "uploaded") {
        const st = String(doc.status || "");
        if (!(st === "uploaded" || st === "waiting" || Boolean(doc.pending_upload))) {
          return false;
        }
      } else if (docFilters.clearance === "scanning") {
        if (clearance !== "scanning" && !isScanInProgress(doc)) return false;
      } else if (clearance !== docFilters.clearance) {
        return false;
      }
    }
    if (docFilters.kind === "exhibit") {
      const kind = String(doc.doc_kind || "").toLowerCase();
      if (kind !== "exhibit" && kind !== "evidence" && !kind.startsWith("exhibit")) return false;
    } else if (docFilters.kind === "statement") {
      if (!isStatementDoc(doc)) return false;
    } else if (docFilters.kind === "filing") {
      if (!COURT_FILING_KINDS.has(String(doc.doc_kind || "").toLowerCase())) return false;
    } else if (docFilters.kind === "unknown") {
      if (doc.doc_kind) return false;
    }
    if (docFilters.issue === "aiIncomplete" && doc.ai_review !== "incomplete") return false;
    if (
      docFilters.issue === "moduleError" &&
      !(Array.isArray(doc.scan_modules) && doc.scan_modules.some((m) => m?.status === "error"))
    ) {
      return false;
    }
    return true;
  });
  const hiddenByFilters = Math.max(0, shownDocs.length - visibleDocs.length);
  const docPageCount = Math.max(1, Math.ceil(visibleDocs.length / DOC_PAGE_SIZE) || 1);
  const safeDocPage = Math.min(Math.max(0, docPage), docPageCount - 1);
  const pageDocs = visibleDocs.slice(
    safeDocPage * DOC_PAGE_SIZE,
    (safeDocPage + 1) * DOC_PAGE_SIZE
  );
  useEffect(() => {
    setDocPage(0);
  }, [caseData.id, docQuery, docFilters.risk, docFilters.clearance, docFilters.kind, docFilters.issue]);
  useEffect(() => {
    if (docPage !== safeDocPage) setDocPage(safeDocPage);
  }, [docPage, safeDocPage]);
  function setDocFilter(key, value) {
    setDocFilters((prev) => ({ ...prev, [key]: value || "" }));
    setDocPage(0);
  }
  function clearDocFilters() {
    setDocFilters({
      risk: "",
      clearance: "",
      kind: "",
      issue: "",
    });
    setDocQuery("");
    setDocPage(0);
  }

  // Desktop: older complete docs may lack last_scanned_at — pull from cached reports.
  useEffect(() => {
    if (!isDesktopApp()) return;
    const need = docs.filter(
      (d) => d.status === "complete" && !d.last_scanned_at && !stampById[d.id]
    );
    if (!need.length) return;
    let cancelled = false;
    (async () => {
      const found = {};
      await Promise.all(
        need.map(async (doc) => {
          try {
            const report = await getDesktopReport(caseData.id, doc.id);
            const at = report?.audit?.scanned_at;
            if (at) found[doc.id] = at;
          } catch {
            /* ignore */
          }
        })
      );
      if (!cancelled && Object.keys(found).length) {
        setStampById((prev) => ({ ...prev, ...found }));
      }
    })();
    return () => {
      cancelled = true;
    };
    // stampById omitted on purpose — only fill missing ids
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseData.id, docs]);

  useEffect(() => {
    const ids = new Set(docIdKey ? docIdKey.split("\n") : []);
    setSelectedDocs((prev) => {
      let changed = false;
      const next = new Set();
      for (const id of prev) {
        if (ids.has(id)) next.add(id);
        else changed = true;
      }
      // Same Set reference: otherwise every case view re-renders forever.
      if (!changed) return prev;
      return next;
    });
  }, [docIdKey]);

  function toggleDocSelect(id) {
    setSelectedDocs((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function deleteSelectedDocuments() {
    const ids = [...selectedDocs];
    if (!ids.length) return;
    if (!window.confirm(`Remove ${ids.length} filing${ids.length === 1 ? "" : "s"} from this case? This cannot be undone.`)) {
      return;
    }
    setBusy(true);
    onError("");
    try {
      for (const id of ids) {
        await deleteDocument(caseData.id, id);
      }
      setSelectedDocs(new Set());
      await onRefresh();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function paceScanDocs(targetDocs, { notePrefix = "Analyzing" } = {}) {
    if (!targetDocs.length) return { queued: 0, failed: [] };
    const scanFailed = [];
    let queued = 0;
    const concurrency = 2;
    let next = 0;

    async function worker() {
      while (next < targetDocs.length) {
        const i = next;
        next += 1;
        const doc = targetDocs[i];
        setUploadNote(
          `${notePrefix} ${Math.min(i + 1, targetDocs.length)}/${targetDocs.length}: ${doc.display_name || doc.original_filename}`
        );
        const source = docs.find((item) => item.id === doc.id) || doc;
        armRescan(source);
        try {
          const result = await scanDocument(caseData.id, source.id, source);
          if (result?.job_id) noteJob(source.id, result.job_id);
          queued += 1;
        } catch (err) {
          disarmRescan(source.id);
          scanFailed.push(`${doc.display_name || doc.original_filename}: ${err.message || "scan failed"}`);
        }
        try {
          await onRefresh();
        } catch {
          /* keep going */
        }
        // Yield so the UI can paint between heavy desktop analyzes.
        await new Promise((r) => setTimeout(r, 50));
      }
    }

    await Promise.all(
      Array.from({ length: Math.min(concurrency, targetDocs.length) }, () => worker())
    );
    return { queued, failed: scanFailed };
  }

  // Once per case visit: auto-queue uploaded/failed files.
  useEffect(() => {
    if (caseData.archived_at) return;
    const key = caseData.id;
    if (backfillKeyRef.current === key) return;
    if (!waitingDocs.length) {
      backfillKeyRef.current = key;
      return;
    }
    if (scanningRef.current) return;
    backfillKeyRef.current = key;
    scanningRef.current = true;
    setScanBacklogError("");
    setUploadNote(
      `Starting analysis for ${waitingDocs.length} waiting file${waitingDocs.length === 1 ? "" : "s"} (2 at a time)…`
    );
    const snapshot = [...waitingDocs];
    (async () => {
      try {
        const { queued, failed } = await paceScanDocs(snapshot);
        if (failed.length) {
          const msg = failed[0].includes(": ")
            ? failed[0].slice(failed[0].indexOf(": ") + 2)
            : failed[0];
          setScanBacklogError(msg);
          onError(
            queued
              ? `Analyzed ${queued} of ${snapshot.length}. Failed: ${failed.join(" · ")}`
              : `Analysis did not start. ${failed.join(" · ")}`
          );
          setUploadNote(
            queued
              ? `${queued} analyzed; ${failed.length} still need Scan.`
              : "Files are saved. Fix sign-in / analysis API, then use Scan now."
          );
        } else {
          setUploadNote(`Analyzed ${queued} waiting file${queued === 1 ? "" : "s"}.`);
        }
      } catch (err) {
        setScanBacklogError(err.message || "Scan backfill failed");
        onError(err.message);
      } finally {
        scanningRef.current = false;
        try {
          await onRefresh();
        } catch {
          /* ignore */
        }
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseData.id]);

  async function onUpload(fileList) {
    if (isDesktopApp()) {
      setBusy(true);
      try {
        onError("");
        setScanBacklogError("");
        const added = await pickDesktopDocuments(caseData.id);
        const picked = Array.isArray(added) ? added : [];
        if (!picked.length) {
          setUploadNote("");
          setBusy(false);
          return;
        }
        setUploadNote(
          `Saved ${picked.length} file${picked.length === 1 ? "" : "s"} on this PC. Analysis started (2 at a time)…`
        );
        await onRefresh();
        setBusy(false);

        // Do not await full analysis here — that made "add files" feel stuck for minutes.
        scanningRef.current = true;
        paceScanDocs(picked)
          .then(({ queued, failed: scanFailed }) => {
            scanningRef.current = false;
            if (scanFailed.length) {
              const msg = scanFailed[0].includes(": ")
                ? scanFailed[0].slice(scanFailed[0].indexOf(": ") + 2)
                : scanFailed[0];
              setScanBacklogError(msg);
              onError(
                queued
                  ? `Analyzed ${queued} of ${picked.length}. Failed: ${scanFailed.join(" · ")}`
                  : `Files saved but analysis did not start. ${scanFailed.join(" · ")}`
              );
              setUploadNote(
                queued
                  ? `Saved ${picked.length}. ${queued} analyzed; use Scan on the rest.`
                  : `Saved ${picked.length}. Sign in / Start 14-day trial if scans are locked, then use Scan.`
              );
            } else {
              setUploadNote(
                `Saved and analyzed ${queued} file${queued === 1 ? "" : "s"}. Reports are cached on this PC.`
              );
            }
          })
          .catch((err) => {
            scanningRef.current = false;
            onError(err.message);
          });
      } catch (err) {
        scanningRef.current = false;
        onError(err.message);
        setBusy(false);
      }
      return;
    }
    const files = Array.from(fileList || []);
    const valid = files.filter((f) => ACCEPTED.has(extensionOf(f.name)));
    const rejected = files.filter((f) => !ACCEPTED.has(extensionOf(f.name))).map((f) => f.name);
    if (rejected.length) onError(`Skipped unsupported file(s): ${rejected.join(", ")}`);
    if (!valid.length) return;
    onError("");
    setIndexMismatchNote("");

    // Hosted: preflight health, queue every file first, then poll — do not await
    // the full pipeline per file (one blip must not wipe the batch).
    if (isHostedAnalysis()) {
      setBusy(true);
      setFailedBatch([]);
      // Pin matter id for the whole batch — do not read caseData.id mid-poll
      // (refresh can replace the object and drop uploads onto a stale id).
      const matterId = caseData.id;
      if (!matterId) {
        onError("Open a matter before uploading files.");
        setBusy(false);
        return;
      }
      try {
        const health = await checkAnalysisHealth();
        if (!health.ok) {
          onError(ANALYSIS_OFFLINE_MESSAGE);
          setUploadNote("");
          return;
        }
        const pending = [];
        const queueFailed = [];
        const indexWarnings = [];
        const seenSha = new Set(
          (caseData.documents || [])
            .map((d) => String(d.sha256 || "").toLowerCase())
            .filter(Boolean)
        );
        let skippedDupes = 0;
        for (let i = 0; i < valid.length; i += 1) {
          const name = valid[i].name;
          setUploadNote(`Registering ${i + 1} of ${valid.length}: ${name}`);
          // Yield so the note paints before a long hash/upload.
          await new Promise((r) => setTimeout(r, 0));
          try {
            setUploadNote(`Hashing ${i + 1} of ${valid.length}: ${name}`);
            const sha = String(await sha256OfFile(valid[i])).toLowerCase();
            if (sha && seenSha.has(sha)) {
              skippedDupes += 1;
              setUploadNote(
                `Already on this matter: ${name}. Use Re-scan on that row — uploading the same file again is skipped.`
              );
              continue;
            }
            if (sha) seenSha.add(sha);
            const result = await uploadCaseDocument(matterId, valid[i], {
              autoScan: true,
              waitForComplete: false,
              onProgress: (phase) => {
                const label = phase === "hashing"
                  ? "Hashing"
                  : phase === "caching"
                    ? "Caching"
                    : "Uploading";
                setUploadNote(`${label} ${i + 1} of ${valid.length}: ${name}`);
              },
            });
            if (result?.index_warning) {
              indexWarnings.push(`${name}: ${result.index_warning}`);
            } else if (result?.job?.index_warning) {
              indexWarnings.push(`${name}: ${result.job.index_warning}`);
            }
            const jobId = result.job_id;
            const fileSha = result.sha256 || sha;
            pending.push({
              file: valid[i],
              jobId,
              sha256: fileSha,
              name,
            });
            setPendingDocs((prev) => {
              const shaKey = String(fileSha || "").toLowerCase();
              if (shaKey && prev.some((p) => String(p.sha256 || "").toLowerCase() === shaKey)) {
                return prev;
              }
              return [
                ...prev,
                {
                  id: `pending:${jobId || fileSha || name}`,
                  sha256: fileSha,
                  original_filename: name,
                  display_name: result.display_name || name,
                  doc_kind: /\bexhibit\b/i.test(name) ? "exhibit" : null,
                  status: "queued",
                  scan_job_id: jobId,
                  scan_in_progress: true,
                  has_report: false,
                  pending_upload: true,
                },
              ];
            });
          } catch (err) {
            queueFailed.push({ file: valid[i], error: err.message || "upload failed" });
          }
        }
        if (indexWarnings.length) {
          setIndexMismatchNote(indexWarnings.slice(0, 3).join(" · "));
        }
        try {
          await retitleCaseDocuments(matterId);
          await onRefresh();
        } catch {
          /* names/kinds still apply on scan */
        }
        // Release global busy after queue so Re-scan / Re-run AI stay clickable
        // while polling (source PDFs are already in the local cache).
        setBusy(false);
        let done = 0;
        let ok = 0;
        const analysisFailed = [];
        const resubmittedSha = new Set();
        const patchPending = (item, patch) => {
          const shaKey = String(item.sha256 || "").toLowerCase();
          setPendingDocs((prev) =>
            prev.map((p) => {
              const match =
                (item.jobId && p.scan_job_id === item.jobId) ||
                (shaKey && String(p.sha256 || "").toLowerCase() === shaKey);
              return match ? { ...p, ...patch } : p;
            })
          );
        };
        for (const item of pending) {
          setUploadNote(
            `${ok} complete · ${pending.length - done} remaining · Analyzing ${done + 1} of ${pending.length}: ${item.name}`
          );
          let progress = null;
          let blips = 0;
          try {
            for (let i = 0; i < 240; i += 1) {
              try {
                progress = await getJob(item.jobId, { includeReport: false });
                blips = 0;
              } catch (err) {
                if (isInterruptedOrExpiredJob(err)) {
                  const sha = String(item.sha256 || "").toLowerCase();
                  if (sha && resubmittedSha.has(sha)) {
                    throw err;
                  }
                  if (sha) resubmittedSha.add(sha);
                  const again = await createAnalysisJob(item.file, {
                    matterId,
                    submittedBy: "web-app",
                  });
                  item.jobId = again.job_id;
                  patchPending(item, {
                    scan_job_id: again.job_id,
                    status: "queued",
                    scan_in_progress: true,
                    id: `pending:${again.job_id}`,
                  });
                  blips = 0;
                  continue;
                }
                // A scan can stall the status call. One dropped poll is not an outage.
                if (!isAnalysisUnreachableError(err) || blips >= 5) throw err;
                blips += 1;
                await new Promise((r) => setTimeout(r, 2000));
                continue;
              }
              if (progress.status === "interrupted" || progress.interrupted) {
                const sha = String(item.sha256 || "").toLowerCase();
                if (sha && resubmittedSha.has(sha)) {
                  break;
                }
                if (sha) resubmittedSha.add(sha);
                const again = await createAnalysisJob(item.file, {
                  matterId,
                  submittedBy: "web-app",
                });
                item.jobId = again.job_id;
                patchPending(item, {
                  scan_job_id: again.job_id,
                  status: "queued",
                  scan_in_progress: true,
                  id: `pending:${again.job_id}`,
                });
                continue;
              }
              if (progress.status === "complete" || progress.status === "failed") break;
              const pos = progress.queue_position;
              const eta = progress.eta_seconds;
              const etaLabel =
                typeof eta === "number" && eta > 0
                  ? eta >= 60
                    ? ` · ~${Math.round(eta / 60)} min`
                    : ` · ~${eta}s`
                  : "";
              const queueLabel =
                typeof pos === "number" && pos > 0 ? ` · queue #${pos}` : "";
              const stage = /gemini busy/i.test(String(progress.message || ""))
                ? "Gemini busy, retrying"
                : progress.status === "local_done"
                  ? "waiting for Gemini"
                  : progress.message || progress.stage || "analyzing";
              patchPending(item, {
                status: progress.status === "queued" ? "queued" : "scanning",
                stage: progress.stage || stage,
                scan_in_progress: true,
                scan_job_id: item.jobId,
              });
              setUploadNote(
                `${ok} complete · ${done} done · ${pending.length - done} left · ` +
                  `${item.name}${queueLabel}${etaLabel} — ${stage}`
              );
              await new Promise((r) => setTimeout(r, 1000));
            }
            if (progress?.status === "complete") {
              ok += 1;
              patchPending(item, {
                status: "scanning",
                scan_in_progress: true,
                stage: "persisting",
              });
            } else {
              analysisFailed.push({
                file: item.file,
                error: progress?.error || progress?.message || "analysis failed",
              });
              patchPending(item, {
                status: progress?.status === "interrupted" ? "interrupted" : "failed",
                scan_in_progress: false,
                scan_failed_open: true,
                last_error: progress?.error || progress?.message || "analysis failed",
              });
            }
          } catch (err) {
            analysisFailed.push({ file: item.file, error: err.message || "analysis failed" });
            patchPending(item, {
              status: "failed",
              scan_in_progress: false,
              scan_failed_open: true,
              last_error: err.message || "analysis failed",
            });
          }
          done += 1;
        }
        const allFailed = [
          ...queueFailed.map((f) => ({ file: f.file, error: f.error })),
          ...analysisFailed,
        ];
        setFailedBatch(allFailed.map((f) => f.file));
        try {
          await onRefresh();
        } catch {
          /* ignore */
        }
        const dupeNote = skippedDupes
          ? ` Skipped ${skippedDupes} duplicate file${skippedDupes === 1 ? "" : "s"} (already on this matter — use Re-scan).`
          : "";
        if (allFailed.length) {
          onError(
            ok
              ? `Queued ${pending.length}; ${ok} finished. Failed: ${allFailed
                  .map((f) => `${f.file.name}: ${f.error}`)
                  .join(" · ")}`
              : `None of ${valid.length} files finished. ${allFailed
                  .map((f) => `${f.file.name}: ${f.error}`)
                  .join(" · ")}`
          );
          setUploadNote(
            `${ok} of ${valid.length} complete. Use Retry failed for the rest.${dupeNote}`
          );
        } else if (!pending.length && skippedDupes) {
          onError("");
          setUploadNote(
            `Nothing new to upload.${dupeNote} Open the existing row and click Re-scan.`
          );
        } else {
          onError("");
          setUploadNote(
            `Added and analyzed ${ok} file${ok === 1 ? "" : "s"}.${dupeNote}`
          );
        }
      } finally {
        setBusy(false);
        if (inputRef.current) inputRef.current.value = "";
      }
      return;
    }

    const failed = [];
    const scanIssues = [];
    let added = 0;
    let queued = 0;
    for (let i = 0; i < valid.length; i += 1) {
      setUploadNote(`Adding ${i + 1} of ${valid.length}: ${valid[i].name}`);
      try {
        const result = await uploadCaseDocument(caseData.id, valid[i], { autoScan: true });
        added += 1;
        if (result?.status === "queued" || result?.job_id) {
          queued += 1;
        } else if (result?.scan_error) {
          scanIssues.push(`${valid[i].name}: ${result.scan_error}`);
        } else if (result?.status === "uploaded") {
          scanIssues.push(`${valid[i].name}: Scan did not start — use Scan`);
        }
      } catch (err) {
        failed.push(`${valid[i].name}: ${err.message || "upload failed"}`);
      }
    }
    try {
      await onRefresh();
    } catch {
      // Keep going even if the case refresh drops.
    }
    const problems = [...failed, ...scanIssues];
    if (problems.length) {
      onError(
        added
          ? `Added ${added} of ${valid.length}. Issues: ${problems.join(" · ")}`
          : `None of ${valid.length} files were added. ${problems.join(" · ")}`
      );
    } else {
      onError("");
    }
    setUploadNote(
      added
        ? queued
          ? `Added ${added} file${added === 1 ? "" : "s"}. ${queued} scan${queued === 1 ? "" : "s"} running in the background.`
          : `Added ${added} file${added === 1 ? "" : "s"}. Use Scan if a file stayed on uploaded.`
        : ""
    );
    if (inputRef.current) inputRef.current.value = "";
  }

  async function retryFailedUploads() {
    if (!failedBatch.length) return;
    const again = failedBatch.slice();
    setFailedBatch([]);
    await onUpload(again);
  }

  async function saveRename(doc) {
    const name = editName.trim();
    if (!name || name === doc.display_name) {
      setEditingId(null);
      return;
    }
    try {
      await patchDocument(caseData.id, doc.id, { display_name: name });
      setEditingId(null);
      await onRefresh();
    } catch (err) {
      onError(err.message);
    }
  }

  async function rescan(doc) {
    const source = docs.find((item) => item.id === doc.id) || doc;
    armRescan(source);
    setBusy(true);
    setScanBacklogError("");
    onError("");
    try {
      const result = await scanDocument(caseData.id, source.id, source);
      if (result?.job_id) noteJob(source.id, result.job_id);
      setUploadNote(
        result?.queued
          ? `Re-scan queued for ${source.display_name || source.original_filename || "file"}.`
          : `Re-scan started for ${source.display_name || source.original_filename || "file"}.`
      );
      await onRefresh();
    } catch (err) {
      disarmRescan(source.id);
      const msg = err?.message || String(err) || "Re-scan failed.";
      onError(msg);
      setScanBacklogError(msg);
      setUploadNote("");
    } finally {
      setBusy(false);
    }
  }

  function docNeedsLocate(doc) {
    const sha = String(doc?.sha256 || "").toLowerCase();
    if (!sha || !isHostedAnalysis()) return false;
    return Boolean(cacheCoverage?.missingShas?.includes(sha));
  }

  const missingCacheCount = cacheCoverage?.missing?.length || 0;

  async function refreshCacheCoverage() {
    if (!isHostedAnalysis() || !docs.length) {
      setCacheCoverage(null);
      return;
    }
    try {
      const { cacheCoverageForDocs } = await import("./storage/localCache.js");
      setCacheCoverage(await cacheCoverageForDocs(docs));
    } catch {
      setCacheCoverage(null);
    }
  }

  async function locateMissingOnMatter() {
    if (!isHostedAnalysis() || locatingFiles) return;
    setLocatingFiles(true);
    setLocateNote("");
    onError("");
    try {
      const exhibits = docs
        .filter((d) => d.sha256 && !d.pending_upload)
        .map((doc) => ({
          exhibitId: doc.id,
          sha256: doc.sha256,
          byteLength: doc.source_size || doc.file_size_bytes,
          name: doc.display_name || doc.original_filename || "",
        }));
      const { locateCaseFiles } = await import("./storage/index.js");
      const result = await locateCaseFiles(exhibits, {
        onProgress: ({ scanned, found, total }) => {
          setLocateNote(`Checking files… found ${found} of ${total} (${scanned} scanned).`);
        },
      });
      if (result.cancelled) {
        setLocateNote("");
        return;
      }
      setLocateNote(result.summary || "");
      setCacheBannerDismissed(false);
      await refreshCacheCoverage();
    } catch (err) {
      onError(err?.message || "Could not locate files.");
    } finally {
      setLocatingFiles(false);
    }
  }

  async function locateOneDoc(doc) {
    const sha = String(doc?.sha256 || "").toLowerCase();
    if (!sha || locatingFiles) return;
    setLocatingFiles(true);
    onError("");
    try {
      const { locateOneExhibit } = await import("./storage/index.js");
      await locateOneExhibit({
        exhibitId: doc.id,
        sha256: sha,
        byteLength: doc.source_size || doc.file_size_bytes,
      });
      setLocateNote(`Located “${doc.display_name || doc.original_filename || "file"}”.`);
      setCacheBannerDismissed(false);
      await refreshCacheCoverage();
    } catch (err) {
      onError(err?.message || "That file does not match this exhibit.");
    } finally {
      setLocatingFiles(false);
    }
  }

  async function exportMatterPack() {
    if (!isHostedAnalysis() || exportingMatter) return;
    setExportingMatter(true);
    onError("");
    try {
      await downloadMatterExport(caseData.id);
      setUploadNote("Matter export downloaded.");
    } catch (err) {
      onError(err?.message || "Matter export failed.");
    } finally {
      setExportingMatter(false);
    }
  }

  async function rerunAi(doc) {
    // Full re-scan via IndexedDB cache (same path as Re-scan). Locate first if missing.
    const source = docs.find((item) => item.id === doc.id) || doc;
    armRescan(source);
    setBusy(true);
    setScanBacklogError("");
    onError("");
    try {
      const sha = String(source?.sha256 || "").toLowerCase();
      if (isHostedAnalysis() && sha) {
        const cached = await localCacheAdapter.hasFile(source.id, sha).catch(() => false);
        if (!cached) {
          const { locateOneExhibit } = await import("./storage/index.js");
          await locateOneExhibit({
            exhibitId: source.id,
            sha256: sha,
            byteLength: source.source_size,
          });
        }
      }
      const result = await scanDocument(caseData.id, source.id, source);
      if (result?.job_id) noteJob(source.id, result.job_id);
      setUploadNote(
        result?.queued
          ? `AI review re-run queued for ${doc.display_name || doc.original_filename || "file"}.`
          : `AI review re-run started for ${doc.display_name || doc.original_filename || "file"}.`
      );
      await onRefresh();
    } catch (err) {
      disarmRescan(source.id);
      const msg = err?.message || String(err) || "AI review re-run failed.";
      const locateHint = /not cached|Locate|re-upload/i.test(msg)
        ? msg
        : `${msg} Locate the file to re-run.`;
      onError(locateHint);
      setScanBacklogError(locateHint);
      setUploadNote("");
    } finally {
      setBusy(false);
    }
  }

  async function scanWaitingNow() {
    if (!waitingDocs.length || scanningRef.current) return;
    setBusy(true);
    scanningRef.current = true;
    setScanBacklogError("");
    onError("");
    try {
      setUploadNote(
        `Scanning ${waitingDocs.length} waiting file${waitingDocs.length === 1 ? "" : "s"} (2 at a time)…`
      );
      const snapshot = [...waitingDocs];
      const { queued, failed } = await paceScanDocs(snapshot, { notePrefix: "Scanning" });
      if (failed.length) {
        const msg = failed[0].includes(": ")
          ? failed[0].slice(failed[0].indexOf(": ") + 2)
          : failed[0];
        setScanBacklogError(msg);
        onError(
          queued
            ? `Scanned ${queued} of ${snapshot.length}. Failed: ${failed.join(" · ")}`
            : `Scan did not start. ${failed.join(" · ")}`
        );
      } else {
        setUploadNote(
          isDesktopApp()
            ? `Scanned ${queued} file${queued === 1 ? "" : "s"}.`
            : `Queued ${queued} file${queued === 1 ? "" : "s"} for scan.`
        );
      }
      await onRefresh();
    } catch (err) {
      onError(err.message);
      setScanBacklogError(err.message);
    } finally {
      scanningRef.current = false;
      setBusy(false);
    }
  }

  async function rescanAll() {
    if (!docs.length) return;
    const waitingOnly = waitingDocs.length > 0 && waitingDocs.length === docs.length;
    const label = waitingOnly
      ? `Scan all ${docs.length} waiting file${docs.length === 1 ? "" : "s"}? Scans run two at a time.`
      : waitingDocs.length
        ? `Rescan all ${docs.length} file${docs.length === 1 ? "" : "s"} (${waitingDocs.length} still waiting)? Scans run two at a time.`
        : `Rescan all ${docs.length} file${docs.length === 1 ? "" : "s"} in this case? Scans run two at a time and may take a while.`;
    const ok = window.confirm(label);
    if (!ok) return;
    const targets = docs.slice();
    for (const doc of targets) armRescan(doc);
    setBusy(true);
    scanningRef.current = true;
    setScanBacklogError("");
    try {
      const result = await rescanCase(caseData.id, { waitingOnly: false });
      const startedRows = Array.isArray(result.started) ? result.started : null;
      if (startedRows) {
        const ids = new Set(
          startedRows.map((row) => (typeof row === "string" ? row : row.document_id || row.id))
        );
        for (const doc of targets) {
          if (!ids.has(doc.id)) {
            disarmRescan(doc.id);
            continue;
          }
          const row = startedRows.find(
            (item) => typeof item === "object" && (item.document_id || item.id) === doc.id
          );
          if (row?.job_id) noteJob(doc.id, row.job_id);
        }
      } else if (!result.queued) {
        for (const doc of targets) disarmRescan(doc.id);
      }
      const missing = result.missing || [];
      setUploadNote(
        result.status === "idle" && !missing.length
          ? "Nothing to rescan right now."
          : `Started rescan of ${result.queued || 0} file${(result.queued || 0) === 1 ? "" : "s"}` +
            (missing.length
              ? ` · ${missing.length} need Locate / re-upload: ${missing.slice(0, 5).join(", ")}${
                  missing.length > 5 ? ` +${missing.length - 5} more` : ""
                }`
              : result.skipped
                ? ` (${result.skipped} skipped).`
                : ".")
      );
      if (result.error || missing.length) {
        const msg =
          result.error ||
          `${missing.length} file(s) not in this computer’s cache. Use Locate or re-upload.`;
        setScanBacklogError(msg);
        onError(msg);
      }
      await onRefresh();
    } catch (err) {
      for (const doc of targets) disarmRescan(doc.id);
      onError(err.message);
      setScanBacklogError(err.message);
    } finally {
      scanningRef.current = false;
      setBusy(false);
    }
  }

  const scanned = shownDocs.filter((d) => d.status === "complete" && d.risk_level);
  const byRisk = {
    red: scanned.filter((d) => d.risk_level === "red"),
    yellow: scanned.filter((d) => d.risk_level === "yellow"),
    green: scanned.filter((d) => d.risk_level === "green"),
  };
  const entitlementLocked = entitlement && entitlement.active === false;
  const backlogHint =
    scanBacklogError ||
    (entitlementLocked
      ? entitlement.message || "Scans locked — sign in or start a trial."
      : "");

  return (
    <>
      <div className="card case-hero">
        <div className="toolbar tight">
          <button className="text-link" type="button" onClick={() => { setBusy(false); onBack(); }}>← All cases</button>
          <div className="row-actions">
            {!caseData.archived_at && docs.length > 0 && (
              <button className="btn secondary" type="button" disabled={busy} onClick={rescanAll}>
                <RefreshCw size={14} />{" "}
                {waitingDocs.length > 0 && waitingDocs.length === docs.length
                  ? "Scan all waiting"
                  : "Rescan all"}
              </button>
            )}
            {!caseData.archived_at &&
              isHostedAnalysis() &&
              missingCacheCount > 0 && (
              <button
                className="btn gold"
                type="button"
                disabled={busy || locatingFiles}
                title="Match missing PDFs from a folder on this computer"
                onClick={locateMissingOnMatter}
              >
                <FolderOpen size={14} />{" "}
                {locatingFiles ? "Locating…" : `Locate ${missingCacheCount} file${missingCacheCount === 1 ? "" : "s"}`}
              </button>
            )}
            {isHostedAnalysis() && docs.length > 0 && (
              <button
                className="btn secondary"
                type="button"
                disabled={busy || exportingMatter}
                title="Download Case Impact + document risk summary (JSON)"
                onClick={exportMatterPack}
              >
                <Download size={14} />{" "}
                {exportingMatter ? "Exporting…" : "Export matter"}
              </button>
            )}
            {!caseData.archived_at && isHostedAnalysis() && docs.length > 0 && (
              <button
                className="btn secondary"
                type="button"
                disabled={busy}
                title="Re-extract facts from stored text (no PDFs needed)"
                onClick={async () => {
                  setBusy(true);
                  setScanBacklogError("");
                  setUploadNote("Refreshing case facts…");
                  try {
                    const result = await recomputeMatterFacts(caseData.id, {
                      onProgress: (row) => {
                        if (!row) return;
                        if (row.status === "complete" || row.status === "failed") return;
                        const ahead = row.scans_ahead;
                        if (
                          row.status === "queued" &&
                          typeof ahead === "number"
                        ) {
                          setUploadNote(
                            ahead > 0
                              ? `Refresh facts queued behind ${ahead} scan${
                                  ahead === 1 ? "" : "s"
                                }`
                              : row.message || "Refresh facts queued behind scan slot"
                          );
                          return;
                        }
                        const pct =
                          typeof row.percent === "number" ? ` ${row.percent}%` : "";
                        setUploadNote(
                          `Refreshing case facts${pct}` +
                            (row.message ? ` — ${row.message}` : "…")
                        );
                      },
                    });
                    if (result?.status === "failed") {
                      throw new Error(result.message || result.error || "Recompute failed");
                    }
                    const dropped =
                      (result.before || []).length - (result.after || []).length;
                    setUploadNote(
                      `Case facts recomputed from ${result.updated || 0} excerpt${
                        (result.updated || 0) === 1 ? "" : "s"
                      }` +
                        (result.skipped_no_excerpt
                          ? ` (${result.skipped_no_excerpt} without text).`
                          : ".") +
                        (dropped > 0 ? ` ${dropped} case-impact card(s) cleared.` : "") +
                        ` Case-impact: ${(result.before || []).length} → ${(result.after || []).length}.`
                    );
                    await onRefresh();
                  } catch (err) {
                    const msg = err?.message || "Could not recompute case facts.";
                    onError(msg);
                    setScanBacklogError(msg);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Refresh facts
              </button>
            )}
            {caseData.archived_at ? (
              <button className="btn secondary" type="button" disabled={busy} onClick={onUnarchive}>
                <RotateCcw size={14} /> Restore
              </button>
            ) : (
              <button className="btn secondary" type="button" disabled={busy} onClick={onArchive}>
                <Archive size={14} /> Archive
              </button>
            )}
            <button className="btn secondary danger" type="button" disabled={busy} onClick={onDelete}>
              <Trash2 size={14} /> Delete
            </button>
          </div>
        </div>
        <p className="kicker">{caseData.archived_at ? "Archived" : "Case"}</p>
        {editingMatter ? (
          <form
            className="case-form"
            style={{ marginTop: 8, maxWidth: 480 }}
            onSubmit={async (e) => {
              e.preventDefault();
              if (!editCaption.trim()) return;
              setSavingMatter(true);
              try {
                await updateCase(caseData.id, {
                  caption: editCaption.trim(),
                  index_number: editIndex.trim() || null,
                  court: editCourt.trim() || null,
                });
                setEditingMatter(false);
                await onRefresh();
              } catch (err) {
                onError?.(err.message || "Could not update case.");
              } finally {
                setSavingMatter(false);
              }
            }}
          >
            <label>
              Caption
              <input value={editCaption} onChange={(e) => setEditCaption(e.target.value)} required />
            </label>
            <label>
              Index
              <input value={editIndex} onChange={(e) => setEditIndex(e.target.value)} />
            </label>
            <label>
              Court
              <input value={editCourt} onChange={(e) => setEditCourt(e.target.value)} />
            </label>
            <div className="actions">
              <button className="btn gold" type="submit" disabled={savingMatter || busy}>
                {savingMatter ? "Saving…" : "Save"}
              </button>
              <button className="btn secondary" type="button" disabled={savingMatter} onClick={() => setEditingMatter(false)}>
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <>
            <h2 className="serif">{caseData.caption}</h2>
            <p className="meta-line">
              {partiesFromCaption(caseData.caption).join(" · ") || "Parties from caption"}
              <span className="dot" />
              {caseData.index_number || "No index"}
              <span className="dot" />
              {caseData.court || "Court not set"}
              {!caseData.archived_at ? (
                <>
                  <span className="dot" />
                  <button
                    type="button"
                    className="linkish"
                    disabled={busy}
                    onClick={() => {
                      setEditCaption(caseData.caption || "");
                      setEditIndex(caseData.index_number || "");
                      setEditCourt(caseData.court || "");
                      setEditingMatter(true);
                    }}
                  >
                    Edit
                  </button>
                </>
              ) : null}
            </p>
          </>
        )}
        {uploadNote && <p className="footnote">{uploadNote}</p>}
        {locateNote && <p className="footnote">{locateNote}</p>}
        {indexMismatchNote && !caseData.archived_at && (
          <div className="banner scan-backlog warn" role="status">
            <div>
              <strong>Index mismatch</strong>
              <p className="footnote" style={{ margin: "4px 0 0" }}>{indexMismatchNote}</p>
            </div>
            <button
              className="btn secondary"
              type="button"
              onClick={() => setIndexMismatchNote("")}
            >
              Dismiss
            </button>
          </div>
        )}
        {failedBatch.length > 0 && !caseData.archived_at && (
          <div className="banner scan-backlog warn">
            <div>
              <strong>
                {failedBatch.length} file{failedBatch.length === 1 ? "" : "s"} failed in the last batch
              </strong>
              <p className="footnote" style={{ margin: "4px 0 0" }}>
                {failedBatch.map((f) => f.name).slice(0, 4).join(", ")}
                {failedBatch.length > 4 ? ` +${failedBatch.length - 4} more` : ""}
              </p>
            </div>
            <button className="btn gold" type="button" disabled={busy} onClick={retryFailedUploads}>
              Retry failed
            </button>
          </div>
        )}
        {!caseData.archived_at && waitingDocs.length > 0 && (
          <div className={`banner scan-backlog${backlogHint ? " warn" : ""}`}>
            <div>
              <strong>
                {waitingDocs.length} file{waitingDocs.length === 1 ? "" : "s"} saved but not analyzed
              </strong>
              {backlogHint ? <p className="footnote" style={{ margin: "4px 0 0" }}>{backlogHint}</p> : null}
            </div>
            <button className="btn gold" type="button" disabled={busy} onClick={scanWaitingNow}>
              Scan now
            </button>
          </div>
        )}
        {!caseData.archived_at &&
          waitingDocs.length === 0 &&
          pendingOrScanning.length > 0 && (
          <div className="banner scan-backlog">
            <div>
              <strong>
                {pendingOrScanning.length} file{pendingOrScanning.length === 1 ? "" : "s"} still scanning
              </strong>
              <p className="footnote" style={{ margin: "4px 0 0" }}>
                They stay in Documents with Status Scanning — use Status → Scanning / queued to filter.
              </p>
            </div>
            <button
              className="btn secondary"
              type="button"
              onClick={() => {
                setDocFilter("clearance", "scanning");
                document.getElementById("matter-documents")?.scrollIntoView({
                  behavior: "smooth",
                  block: "start",
                });
              }}
            >
              Show pending
            </button>
          </div>
        )}
        {caseData.archived_at ? (
          <p className="footnote">This matter is archived. Restore it to add filings.</p>
        ) : null}
        {!isDesktopApp() && !caseData.archived_at && (
          <input
            ref={inputRef}
            type="file"
            hidden
            multiple
            accept=".pdf,.txt,.docx,.xlsx,.jpg,.jpeg,.png,.gif"
            onChange={(e) => onUpload(e.target.files)}
          />
        )}
      </div>

      <CaseRiskSummary
        byRisk={byRisk}
        pendingCount={docs.filter((d) => d.status !== "complete").length}
        activeRisk={docFilters.risk}
        onFilterRisk={(band) => {
          setDocFilter("risk", docFilters.risk === band ? "" : band);
          const el = document.getElementById("matter-documents");
          if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
        }}
      />

      <CaseGotchaBoard
        caseId={caseData.id}
        documents={docs}
        onOpenReport={onOpenReport}
        onRisksElevated={onRefresh}
        onExportMatter={isHostedAnalysis() ? exportMatterPack : null}
        exportingMatter={exportingMatter}
      />

      <div className="card matter-docs" id="matter-documents">
        <div className="toolbar tight">
          <div>
            <p className="kicker">Documents</p>
            <h2 className="serif docs-heading">
              {docs.length} file{docs.length === 1 ? "" : "s"}
              {pendingOrScanning.length
                ? ` · ${pendingOrScanning.length} pending`
                : ""}
              {docFiltersActive ? ` · ${visibleDocs.length} shown` : ""}
            </h2>
          </div>
          <SearchField
            value={docQuery}
            onChange={(value) => {
              setDocQuery(value);
              setDocPage(0);
            }}
            placeholder="Filter by name, kind, tag, or risk…"
          />
          {!caseData.archived_at && (
            <button
              className="btn gold"
              type="button"
              disabled={busy}
              onClick={() => {
                if (isDesktopApp()) onUpload(null);
                else inputRef.current?.click();
              }}
            >
              <Upload size={14} /> Add filings
            </button>
          )}
        </div>
        {isHostedAnalysis() &&
          cacheCoverage &&
          cacheCoverage.total > 0 &&
          missingCacheCount > 0 &&
          !cacheBannerDismissed && (
          <div className="banner scan-backlog cache-banner warn" role="status">
            <div>
              <strong>
                {missingCacheCount} filing{missingCacheCount === 1 ? "" : "s"} not on this
                computer
              </strong>
              <p className="footnote" style={{ margin: "4px 0 0" }}>
                Findings stay on the server. Source PDFs stay on the machine that
                uploaded them — use Locate to match files here, or re-upload via Add
                filings, before re-scan.
              </p>
              {cacheCoverage.missing.length > 0 && (
                <details className="cache-missing-details">
                  <summary>
                    Show {Math.min(12, cacheCoverage.missing.length)} missing name
                    {cacheCoverage.missing.length === 1 ? "" : "s"}
                    {cacheCoverage.missing.length > 12
                      ? ` (+${cacheCoverage.missing.length - 12} more)`
                      : ""}
                  </summary>
                  <p className="footnote" style={{ margin: "6px 0 0" }}>
                    {cacheCoverage.missing.slice(0, 12).join(", ")}
                    {cacheCoverage.missing.length > 12
                      ? ` +${cacheCoverage.missing.length - 12} more`
                      : ""}
                  </p>
                </details>
              )}
            </div>
            <div className="cache-banner-actions">
              <button
                className="btn gold"
                type="button"
                disabled={busy || locatingFiles}
                onClick={locateMissingOnMatter}
              >
                <FolderOpen size={14} /> {locatingFiles ? "Locating…" : "Locate files"}
              </button>
              <button
                className="btn secondary"
                type="button"
                disabled={busy}
                onClick={() => {
                  if (isDesktopApp()) onUpload(null);
                  else inputRef.current?.click();
                }}
              >
                Re-upload
              </button>
              <button
                className="btn secondary"
                type="button"
                onClick={() => setCacheBannerDismissed(true)}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}
        {isHostedAnalysis() &&
          cacheCoverage &&
          cacheCoverage.total > 0 &&
          missingCacheCount === 0 && (
          <p className="footnote cache-ok-note">
            Files on this computer: {cacheCoverage.cached} of {cacheCoverage.total} ready
            for re-scan.
          </p>
        )}
        {listIncompleteError ? (
          <div className="banner scan-backlog warn" role="alert">
            <strong>{listIncompleteError}</strong>
          </div>
        ) : null}
        {hiddenByFilters > 0 && (
          <div className="banner scan-backlog filter-hidden-banner" role="status">
            <div>
              <strong>
                {hiddenByFilters} file{hiddenByFilters === 1 ? "" : "s"} hidden by filters
              </strong>
              <p className="footnote" style={{ margin: "4px 0 0" }}>
                {shownDocs.length} on this matter · {visibleDocs.length} shown
              </p>
            </div>
            <button type="button" className="btn secondary" onClick={clearDocFilters}>
              Clear
            </button>
          </div>
        )}
        {docs.length > 0 && (
          <div className="doc-filter-bar" aria-label="Document filters">
            <div className="doc-filter-groups">
              <label className="doc-filter-group">
                <span className="doc-filter-label">Risk</span>
                <select
                  className="doc-filter-select"
                  value={docFilters.risk}
                  onChange={(e) => setDocFilter("risk", e.target.value)}
                  aria-label="Filter by risk"
                >
                  <option value="">All</option>
                  <option value="red">High</option>
                  <option value="yellow">Moderate</option>
                  <option value="green">Low</option>
                </select>
              </label>
              <label className="doc-filter-group">
                <span className="doc-filter-label">Status</span>
                <select
                  className="doc-filter-select"
                  value={docFilters.clearance}
                  onChange={(e) => setDocFilter("clearance", e.target.value)}
                  aria-label="Filter by scan status"
                >
                  <option value="">All</option>
                  <option value="scanning">Scanning / queued</option>
                  <option value="uploaded">Waiting (not scanned)</option>
                  <option value="complete">Complete</option>
                  <option value="interrupted">Interrupted</option>
                  <option value="incomplete">Incomplete</option>
                  <option value="failed">Failed</option>
                </select>
              </label>
              <label className="doc-filter-group">
                <span className="doc-filter-label">Kind</span>
                <select
                  className="doc-filter-select"
                  value={docFilters.kind}
                  onChange={(e) => setDocFilter("kind", e.target.value)}
                  aria-label="Filter by kind"
                >
                  <option value="">All</option>
                  <option value="exhibit">Exhibits</option>
                  <option value="statement">Statements</option>
                  <option value="filing">Court filings</option>
                  <option value="unknown">Untyped</option>
                </select>
              </label>
              <label className="doc-filter-group">
                <span className="doc-filter-label">Issues</span>
                <select
                  className="doc-filter-select"
                  value={docFilters.issue}
                  onChange={(e) => setDocFilter("issue", e.target.value)}
                  aria-label="Filter by scan issues"
                >
                  <option value="">All</option>
                  <option value="aiIncomplete">AI incomplete</option>
                  <option value="moduleError">Module error</option>
                </select>
              </label>
              {docFiltersActive && (
                <button
                  type="button"
                  className="doc-filter-clear"
                  onClick={clearDocFilters}
                >
                  Clear
                </button>
              )}
            </div>
          </div>
        )}
        {docs.length === 0 && <p className="empty-copy">Drop complaints, motions, and exhibits as the matter progresses.</p>}
        {docs.length > 0 && visibleDocs.length === 0 && (
          <p className="empty-copy">No documents match that filter.</p>
        )}
        {visibleDocs.length > 0 && (
        <>
        {!caseData.archived_at && (
          <div className="bulk-bar" style={{ borderBottom: "1px solid var(--line)", marginBottom: 8 }}>
            <label className="bulk-select-all">
              <input
                type="checkbox"
                checked={pageDocs.length > 0 && pageDocs.every((d) => selectedDocs.has(d.id))}
                disabled={busy}
                onChange={() => {
                  const all = pageDocs.every((d) => selectedDocs.has(d.id));
                  if (all) {
                    setSelectedDocs((prev) => {
                      const next = new Set(prev);
                      pageDocs.forEach((d) => next.delete(d.id));
                      return next;
                    });
                  } else {
                    setSelectedDocs((prev) => {
                      const next = new Set(prev);
                      pageDocs.forEach((d) => next.add(d.id));
                      return next;
                    });
                  }
                }}
              />
              Select page
            </label>
            {selectedDocs.size > 0 && (
              <div className="bulk-actions">
                <span className="footnote">{selectedDocs.size} selected</span>
                <button className="btn secondary danger" type="button" disabled={busy} onClick={deleteSelectedDocuments}>
                  <Trash2 size={14} /> Delete
                </button>
              </div>
            )}
          </div>
        )}
        <PaginationBar
          page={safeDocPage}
          pageSize={DOC_PAGE_SIZE}
          total={visibleDocs.length}
          onChange={setDocPage}
          label="files"
        />
        <table className="data docs crm-docs">
          <colgroup>
            <col className="col-check" />
            <col className="col-name" />
            <col className="col-kind" />
            <col className="col-risk" />
            <col className="col-status" />
            <col className="col-signals" />
            <col className="col-actions" />
          </colgroup>
          <thead>
            <tr>
              <th className="check-col" aria-label="Select" />
              <th>Name</th>
              <th>Kind</th>
              <th>Risk</th>
              <th>Status</th>
              <th>Signals</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {pageDocs.map((doc) => {
              const title = docListTitle(doc);
              const fullName = doc.original_filename || doc.display_name || "";
              const pendingOnly = Boolean(doc.pending_upload);
              const inProgress = isScanInProgress(doc) || pendingOnly;
              const clearance = rowClearance(doc);
              const scannedAt = inProgress
                ? null
                : (doc.last_scanned_at || doc.scanned_at || stampById[doc.id] || null);
              const lights = checkLights(doc);
              const hasReport = Boolean(doc.has_report || doc.status === "complete");
              const statusLabel =
                clearance === "scanning"
                  ? "Scanning"
                  : clearance === "interrupted"
                    ? "Interrupted"
                    : clearance === "incomplete"
                      ? "Incomplete"
                      : clearance === "failed"
                        ? "Failed"
                        : clearance === "complete"
                          ? "Complete"
                          : doc.status;
              const riskLevel = !inProgress && !pendingOnly ? doc.risk_level : null;
              const riskBand =
                riskLevel && ["red", "yellow", "incomplete"].includes(riskLevel) ? riskLevel : "";
              const riskLabel = inProgress
                ? "—"
                : clearance === "interrupted" || clearance === "failed"
                  ? "—"
                  : riskLevel
                    ? RISK[riskLevel] || riskLevel
                    : "—";
              const tags = Array.isArray(doc.profile_tags) ? doc.profile_tags : [];
              const pages = doc.data_profile?.pages ?? doc.page_count;
              const profileDates = Array.isArray(doc.data_profile?.dates)
                ? doc.data_profile.dates.filter(Boolean).map(String)
                : [];
              const dateBit =
                doc.filed_date
                  ? `Filed ${String(doc.filed_date).slice(0, 10)}`
                  : profileDates[0]
                    ? profileDates.length > 1
                      ? `Dates ${profileDates.slice(0, 2).join(", ")}`
                      : `Date ${profileDates[0]}`
                    : null;
              const metaBits = [
                pendingOnly
                  ? "Uploading…"
                  : scannedAt
                    ? formatScanAt(scannedAt)
                    : clearance === "complete" || clearance === "incomplete"
                      ? "Scanned"
                      : null,
                typeof pages === "number" && pages > 0 ? `${pages}p` : null,
                dateBit,
              ].filter(Boolean);
              const needsLocate = docNeedsLocate(doc);
              const statusHint = pendingOnly
                ? "Provisional — waiting for server confirmation"
                : needsLocate
                  ? "Source not on this computer — Locate or re-upload to re-scan"
                  : clearance === "interrupted" || clearance === "failed"
                    ? formatScanError(doc) || "Scan did not finish"
                    : clearance === "incomplete"
                      ? doc.ai_review === "incomplete"
                        ? "Visual model pass did not finish — not a fraud finding. Re-run AI when file is cached."
                        : "Check failed — not a fraud finding. Re-scan when file is cached."
                      : clearance === "scanning"
                        ? "Scan in progress"
                        : null;
              const rowClass = [
                selectedDocs.has(doc.id) ? "selected" : "",
                riskBand ? `risk-row risk-${riskBand}` : "",
              ]
                .filter(Boolean)
                .join(" ");
              return (
              <tr key={doc.id} className={rowClass || undefined}>
                <td className="check-col">
                  <input
                    type="checkbox"
                    checked={selectedDocs.has(doc.id)}
                    disabled={busy || pendingOnly || Boolean(caseData.archived_at)}
                    onChange={() => toggleDocSelect(doc.id)}
                    aria-label={`Select ${title}`}
                  />
                </td>
                <td className="doc-name-cell">
                  {editingId === doc.id && !pendingOnly ? (
                    <input
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      onBlur={() => saveRename(doc)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") saveRename(doc);
                        if (e.key === "Escape") setEditingId(null);
                      }}
                      autoFocus
                      style={{ width: "100%" }}
                    />
                  ) : pendingOnly ? (
                    <span className="doc-title" title={fullName || title}>
                      {title}
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="linkish doc-title"
                      title={fullName || title}
                      onClick={() => {
                        setEditingId(doc.id);
                        setEditName(doc.display_name || doc.original_filename);
                      }}
                    >
                      {title}
                    </button>
                  )}
                  <div className="doc-name-meta">
                    {metaBits.length ? (
                      <span title={profileDates.length > 2 ? profileDates.join(", ") : undefined}>
                        {metaBits.join(" · ")}
                      </span>
                    ) : null}
                  </div>
                </td>
                <td className="doc-kind-cell">
                  <select
                    value={doc.doc_kind || ""}
                    disabled={busy || pendingOnly}
                    aria-label="Document kind"
                    title={profileForKind(doc.doc_kind)}
                    onChange={async (e) => {
                      const next = e.target.value || null;
                      try {
                        await patchDocument(caseData.id, doc.id, { doc_kind: next });
                        await onRefresh();
                      } catch (err) {
                        onError(err.message);
                      }
                    }}
                  >
                    <option value="">Untyped</option>
                    {DOC_KIND_GROUPS.map((group) => (
                      <optgroup key={group.label} label={group.label}>
                        {group.options.map(([value, label]) => (
                          <option key={value} value={value}>{label}</option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                  <span
                    className="doc-kind-lane"
                    title={
                      isStatementDoc(doc)
                        ? "Exhibit packet with statement/bill content (forensic lane)"
                        : profileForKind(doc.doc_kind)
                    }
                  >
                    {kindContentHint(doc)}
                  </span>
                </td>
                <td className="doc-risk-cell">
                  <span
                    className={`doc-risk-badge${riskBand ? ` risk-${riskBand}` : riskLevel === "green" ? " risk-green" : ""}`}
                    title={doc.info_summary || riskLabel}
                  >
                    {riskLabel}
                  </span>
                </td>
                <td className="doc-status-cell">
                  <div className="doc-status-inline">
                    <span
                      className={`doc-status-chip clearance-${clearance}${
                        clearance === "incomplete" ||
                        clearance === "failed" ||
                        clearance === "interrupted"
                          ? " doc-status-alert"
                          : ""
                      }`}
                    >
                      {statusLabel}
                    </span>
                    {lights.length > 0 && (
                      <ul className="doc-checks compact" aria-label="Scan checks">
                        {lights.map((item) => (
                          <li key={item.id} className="doc-check" title={`${item.label}: ${item.title}`}>
                            <span className={`doc-check-dot ${item.state}`} aria-hidden="true" />
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  {statusHint ? (
                    <div className="doc-status-hint" title={statusHint}>
                      {statusHint}
                    </div>
                  ) : null}
                </td>
                <td className="doc-signals-cell">
                  {tags.length > 0 ? (
                    <ul className="doc-profile-tags" aria-label="Signal tags" title={tags.join(", ")}>
                      {tags.slice(0, 3).map((tag) => (
                        <li key={tag}>{tag}</li>
                      ))}
                      {tags.length > 3 ? <li className="more">+{tags.length - 3}</li> : null}
                    </ul>
                  ) : (
                    <span className="footnote">—</span>
                  )}
                </td>
                <td className="row-actions">
                  <div className="doc-actions">
                    {hasReport && !inProgress && !pendingOnly && (
                      <button className="btn gold" type="button" onClick={() => onOpenReport(doc)}>
                        Report
                      </button>
                    )}
                    {docNeedsLocate(doc) && !inProgress && !pendingOnly ? (
                      <button
                        className="btn secondary"
                        type="button"
                        disabled={busy || locatingFiles}
                        title="Match this PDF on this computer for re-scan or preview"
                        onClick={() => locateOneDoc(doc)}
                      >
                        Locate
                      </button>
                    ) : null}
                    {hasReport &&
                      !inProgress &&
                      !pendingOnly &&
                      doc.ai_review === "incomplete" &&
                      !docNeedsLocate(doc) && (
                      <button
                        className="btn secondary"
                        type="button"
                        title="Visual model pass did not finish — re-run AI from the cached PDF (not a fraud finding)"
                        disabled={Boolean(busy) && Boolean(inProgress)}
                        onClick={() => rerunAi(doc)}
                      >
                        Re-run AI
                      </button>
                    )}
                    {!inProgress &&
                      !pendingOnly &&
                      !docNeedsLocate(doc) &&
                      (clearance === "failed" ||
                        clearance === "interrupted" ||
                        clearance === "complete" ||
                        clearance === "incomplete" ||
                        doc.status === "uploaded" ||
                        doc.status === "failed") && (
                      <button
                        className="btn secondary"
                        type="button"
                        title="Re-scan from the cached PDF on this computer"
                        disabled={Boolean(busy) && Boolean(inProgress)}
                        onClick={() => rescan(doc)}
                      >
                        {clearance === "complete" ||
                        clearance === "incomplete" ||
                        clearance === "interrupted" ||
                        hasReport
                          ? "Re-scan"
                          : "Scan"}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
        <PaginationBar
          page={safeDocPage}
          pageSize={DOC_PAGE_SIZE}
          total={visibleDocs.length}
          onChange={setDocPage}
          label="files"
        />
        </>
        )}
      </div>

      <details className="case-chat-panel">
        <summary>
          <span>Ask about this matter</span>
          <span>Case Clerk</span>
        </summary>
        <CaseChat
          embedded
          caseId={caseData.id}
          documents={docs}
          onOpenReport={onOpenReport}
          onError={onError}
          onScanDocument={async (doc) => {
            const source = docs.find((item) => item.id === doc.id) || doc;
            armRescan(source);
            try {
              const result = await scanDocument(caseData.id, source.id, source);
              if (result?.job_id) noteJob(source.id, result.job_id);
            } catch (err) {
              disarmRescan(source.id);
              throw err;
            }
          }}
          onRescanWaiting={async () => {
            await scanWaitingNow();
          }}
          onRefresh={onRefresh}
        />
      </details>
    </>
  );
}

function pickBoardField(item, ...keys) {
  const ev = item?.evidence && typeof item.evidence === "object" ? item.evidence : {};
  for (const key of keys) {
    const raw = item?.[key] ?? ev[key];
    if (raw == null) continue;
    if (typeof raw === "string") {
      const trimmed = raw.trim();
      if (trimmed) return trimmed;
      continue;
    }
    return raw;
  }
  return "";
}

function relatedDocs(item, documents) {
  const fromApi = item.related_documents || [];
  if (fromApi.length) {
    return fromApi
      .map((ref) => documents.find((d) => d.id === ref.document_id) || ref)
      .filter((doc) => doc?.id || doc?.document_id);
  }
  const ev = item?.evidence && typeof item.evidence === "object" ? item.evidence : {};
  const ids = [
    item?.document_id,
    ev.document_id,
    ...((Array.isArray(ev.refs) ? ev.refs : []).map((ref) => ref?.document_id)),
  ].filter(Boolean);
  const out = [];
  const seen = new Set();
  for (const id of ids) {
    const key = String(id);
    if (seen.has(key)) continue;
    seen.add(key);
    const live = documents.find((d) => d.id === key);
    if (live) out.push(live);
  }
  return out;
}

function boardItemKey(item) {
  const field = String(item.field || item.evidence?.field || item.fact_named || "").toLowerCase();
  const claim = String(item.claim_quote || item.evidence?.claim_quote || "").toLowerCase().slice(0, 120);
  const evidence = String(item.evidence_quote || item.evidence?.evidence_quote || "").toLowerCase().slice(0, 120);
  const variants = (item.variants || [])
    .map((v) => String(v.display || v.value || "").toLowerCase())
    .sort()
    .join("|");
  if (field || claim || evidence || variants) {
    return [field, claim, evidence, variants].join("::");
  }
  return String(item.id || item.title || Math.random());
}

function dedupeBoardItems(items) {
  const seen = new Set();
  const out = [];
  for (const item of items || []) {
    const key = boardItemKey(item);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

const CASE_IMPACT_PREVIEW = 6;

const CASE_IMPACT_EMPTY_COPY =
  "Case Impact lists material interrupts verified against this matter’s scanned filings — the same nameable fact, evidence that runs against a claim, and a hit on an element, procedure, credibility, or damages. Ordinary disputes, caption noise, and demoted findings stay off this board. Nothing material is flagged yet.";

function CaseGotchaBoard({
  caseId,
  documents,
  onOpenReport,
  onRisksElevated,
  onExportMatter = null,
  exportingMatter = false,
}) {
  const [gotchas, setGotchas] = useState([]);
  const [factConflicts, setFactConflicts] = useState([]);
  const [caseImpact, setCaseImpact] = useState(null);
  const [ignoredCount, setIgnoredCount] = useState(0);
  const [loadError, setLoadError] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [showNoise, setShowNoise] = useState(false);
  const [showIgnored, setShowIgnored] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [openKey, setOpenKey] = useState("");
  const [panelOpen, setPanelOpen] = useState(false);
  const [feedbackBusy, setFeedbackBusy] = useState("");
  const userToggledPanel = useRef(false);
  const refreshRef = useRef(onRisksElevated);
  refreshRef.current = onRisksElevated;
  const elevatedFor = useRef("");
  // Identity of `documents` changes on every parent render. Key the fetch on content.
  const docKey = (documents || [])
    .map((d) => `${d.id}:${d.status}:${d.risk_level || ""}`)
    .sort()
    .join("|");

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    setLoadError(false);
    listCaseGotchas(caseId)
      .then((data) => {
        if (cancelled) return;
        setGotchas(data.gotchas || []);
        setFactConflicts(data.factConflicts || []);
        setCaseImpact(data.caseImpact || null);
        setIgnoredCount(Number(data.ignoredCount) || 0);
        setLoaded(true);
        // One refresh per document snapshot. Calling this on every response
        // reloads the matter, which changes `documents`, which fetches again.
        if (data.risksElevated && elevatedFor.current !== docKey) {
          elevatedFor.current = docKey;
          refreshRef.current?.();
        }
      })
      .catch(() => {
        if (cancelled) return;
        setGotchas([]);
        setFactConflicts([]);
        setCaseImpact(null);
        setIgnoredCount(0);
        setLoadError(true);
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [caseId, docKey]);

  useEffect(() => {
    setShowAll(false);
    setShowIgnored(false);
    setOpenKey("");
    userToggledPanel.current = false;
  }, [caseId, docKey]);

  const materialGotchas = dedupeBoardItems(
    gotchas.filter((item) => item.case_impact !== "noise" && item.severity !== "info"),
  );
  // Prefer aggregate fact-conflict cards; drop gotcha rows that duplicate the same field.
  const conflictFields = new Set(
    factConflicts.map((item) => String(item.field || "").toLowerCase()).filter(Boolean),
  );
  const uniqueGotchas = materialGotchas.filter((item) => {
    const field = String(item.field || item.evidence?.field || "").toLowerCase();
    if (!field || !conflictFields.has(field)) return true;
    const analysis = item.analysis_type || item.finding_type || item.evidence?.analysis_type;
    return analysis !== "fact_conflict";
  });
  const uniqueConflicts = dedupeBoardItems(factConflicts);
  const allBoardItems = [...uniqueConflicts, ...uniqueGotchas];
  const ignoredBoardCount = allBoardItems.filter((item) => isFindingIgnored(item)).length
    || ignoredCount;
  const activeGotchas = uniqueGotchas.filter((item) => showIgnored || !isFindingIgnored(item));
  const activeConflicts = uniqueConflicts.filter((item) => showIgnored || !isFindingIgnored(item));
  const total = activeGotchas.length + activeConflicts.length;
  const impact = caseImpact || {};
  const materialCount = total;
  const noiseCount = impact.noise_collapsed ?? impact.noise_count ?? 0;
  const fraudCount = impact.fraud_count ?? 0;
  const hasInterrupts = materialCount > 0 || ignoredBoardCount > 0;

  useEffect(() => {
    if (!loaded || userToggledPanel.current) return;
    // Open by default when there are interrupts so partners see the product beat.
    setPanelOpen(hasInterrupts);
  }, [loaded, hasInterrupts, caseId, docKey]);

  const summaryLabel = !loaded
    ? "Loading…"
    : loadError
      ? "Could not load interrupts"
      : materialCount > 0
        ? [
            `${materialCount} material interrupt${materialCount === 1 ? "" : "s"}`,
            fraudCount ? `${fraudCount} fraud exhibit${fraudCount === 1 ? "" : "s"}` : null,
            ignoredBoardCount ? `${ignoredBoardCount} ignored` : null,
          ]
            .filter(Boolean)
            .join(" · ")
        : ignoredBoardCount > 0
          ? `${ignoredBoardCount} ignored interrupt${ignoredBoardCount === 1 ? "" : "s"}`
          : "No material interrupts";

  const rows = [
    ...activeConflicts.map((item) => ({
      key: `conflict-${item.id || item.field}`,
      kind: "conflict",
      item,
      title: item.title,
      meta: item.gotcha_level || "Case fact conflict",
      tainted: false,
      ignored: isFindingIgnored(item),
      confirmed: item.user_feedback?.status === "confirmed",
    })),
    ...activeGotchas.map((item) => {
      const tainted = Boolean(item.tainted_support || item.evidence?.tainted_support);
      return {
        key: `gotcha-${item.id || item.title}-${item.document_id}`,
        kind: "gotcha",
        item,
        title: item.title,
        meta: item.case_impact_bucket
          ? item.case_impact_bucket
          : item.gotcha_level || item.contradiction_type || "",
        tainted,
        ignored: isFindingIgnored(item),
        confirmed: item.user_feedback?.status === "confirmed",
      };
    }),
  ];
  const visibleRows = showAll ? rows : rows.slice(0, CASE_IMPACT_PREVIEW);
  const hiddenCount = Math.max(0, rows.length - CASE_IMPACT_PREVIEW);

  async function applyRowFeedback(row, status) {
    const item = row.item;
    const findingId = String(item.id || row.key || "").trim();
    if (!caseId || !findingId) return;
    const busyKey = `${row.key}:${status}`;
    setFeedbackBusy(busyKey);
    try {
      const result = await setMatterFindingFeedback(caseId, findingId, status, {
        item,
        fingerprint: item.feedback_fingerprint,
      });
      const nextFb = result?.user_feedback || null;
      const patch = (list) =>
        (list || []).map((f) => {
          if (f === item || f.id === item.id) {
            return {
              ...f,
              feedback_fingerprint: result?.fingerprint || f.feedback_fingerprint,
              user_feedback: nextFb,
            };
          }
          return f;
        });
      setGotchas((prev) => patch(prev));
      setFactConflicts((prev) => patch(prev));
      setIgnoredCount((prev) => {
        const wasIgnored = isFindingIgnored(item);
        const nowIgnored = nextFb?.status === "false_positive";
        if (!wasIgnored && nowIgnored) return prev + 1;
        if (wasIgnored && !nowIgnored) return Math.max(0, prev - 1);
        return prev;
      });
    } catch {
      /* keep prior state */
    } finally {
      setFeedbackBusy("");
    }
  }

  function renderFeedbackActions(row) {
    const item = row.item;
    const fbStatus = item.user_feedback?.status || null;
    const busy = Boolean(feedbackBusy) && feedbackBusy.startsWith(`${row.key}:`);
    return (
      <div className="gotcha-feedback-actions">
        {fbStatus === "false_positive" ? (
          <button
            type="button"
            className="btn secondary"
            disabled={busy}
            onClick={() => applyRowFeedback(row, "clear")}
          >
            Undo ignore
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn secondary"
              disabled={busy}
              title="Hide from Case Impact (false positive)"
              onClick={() => applyRowFeedback(row, "false_positive")}
            >
              Ignore (false positive)
            </button>
            {fbStatus === "confirmed" ? (
              <button
                type="button"
                className="btn secondary"
                disabled={busy}
                onClick={() => applyRowFeedback(row, "clear")}
              >
                Undo confirm
              </button>
            ) : (
              <button
                type="button"
                className="btn secondary"
                disabled={busy}
                title="Mark as looks right"
                onClick={() => applyRowFeedback(row, "confirmed")}
              >
                Looks right
              </button>
            )}
          </>
        )}
        {fbStatus === "false_positive" ? (
          <span className="footnote">Ignored for this firm</span>
        ) : fbStatus === "confirmed" ? (
          <span className="footnote">Confirmed</span>
        ) : null}
      </div>
    );
  }

  function renderRowDetail(row) {
    const { item, kind, tainted } = row;
    const sources = relatedDocs(item, documents);
    const claimQuote = pickBoardField(item, "claim_quote");
    const claimSource = pickBoardField(item, "claim_source");
    const evidenceQuote = pickBoardField(item, "evidence_quote", "quoted_evidence");
    const evidenceSource = pickBoardField(item, "evidence_source");
    const evidenceLabel = pickBoardField(item, "evidence_label")
      || (Number(item.evidence_tier ?? item.evidence?.evidence_tier) === 2
        ? "Opposing assertion"
        : "Evidence");
    const explanation = pickBoardField(item, "explanation");
    const whyItMayMatter = pickBoardField(item, "why_it_may_matter");
    const bodyFallback = explanation || whyItMayMatter;
    const attorneyWhy = pickBoardField(item, "attorney_why");
    const materiality = pickBoardField(item, "materiality_reason");
    const factNamed = pickBoardField(item, "fact_named");
    const whySuspicious = pickBoardField(item, "why_suspicious");
    const selfUndercut = Boolean(item.self_undercut || item.evidence?.self_undercut);
    const pageRaw = item.page ?? item.evidence?.page ?? item.evidence?.evidence_page;
    const pageNum = pageRaw != null && pageRaw !== ""
      ? Math.max(1, Number(pageRaw) || 0) || null
      : null;

    const whyLine = attorneyWhy
      || [factNamed && `Same fact: ${factNamed}.`, materiality].filter(Boolean).join(" ")
      || (whySuspicious && whySuspicious !== bodyFallback ? whySuspicious : "")
      || (whyItMayMatter && whyItMayMatter !== bodyFallback ? whyItMayMatter : "");

    if (kind === "conflict") {
      return (
        <>
          {(item.variants || []).map((variant) => (
            <p key={variant.display} className="gotcha-quote">
              <strong>{variant.display}:</strong>
              {variant.quote ? ` “${variant.quote}”` : ""}
              {" — "}
              {(variant.documents || []).map((doc) => doc.display_name).join("; ")}
            </p>
          ))}
          {(item.attorney_why || bodyFallback) && (
            <p className="gotcha-why">{item.attorney_why || bodyFallback}</p>
          )}
          {!item.variants?.length && !item.attorney_why && !bodyFallback && (
            <p className="footnote">Open the scan report for details on this interrupt.</p>
          )}
          <div className="gotcha-row-actions">
            {sources.map((doc) => {
              const id = doc.id || doc.document_id;
              const live = documents.find((d) => d.id === id);
              if (!live) return null;
              return (
                <button key={id} className="btn gold" type="button" onClick={() => onOpenReport(live)}>
                  Open {live.display_name || live.original_filename}
                </button>
              );
            })}
            {renderFeedbackActions(row)}
          </div>
        </>
      );
    }

    const hasQuotes = Boolean(claimQuote || evidenceQuote);
    const bodyText = tainted
      ? (evidenceQuote || bodyFallback || "See the scan report for each flagged item.")
      : (!hasQuotes ? bodyFallback : "");
    const showWhy = whyLine && whyLine !== bodyText;

    return (
      <>
        {claimQuote ? (
          <p className="gotcha-quote">
            <strong>Claim:</strong> “{claimQuote}”
            {claimSource ? ` — ${claimSource}` : ""}
          </p>
        ) : null}
        {tainted ? (
          <p className="gotcha-quote">{bodyText}</p>
        ) : evidenceQuote ? (
          <p className="gotcha-quote">
            <strong>{evidenceLabel}:</strong> “{evidenceQuote}”
            {evidenceSource ? ` — ${evidenceSource}` : ""}
          </p>
        ) : bodyText ? (
          <p className="gotcha-quote">{bodyText}</p>
        ) : null}
        {selfUndercut ? (
          <p className="footnote">This party’s own record appears to undercut their claim.</p>
        ) : null}
        {pageNum != null ? (
          <p className="footnote">Page {pageNum}</p>
        ) : null}
        {showWhy ? <p className="gotcha-why">{whyLine}</p> : null}
        {!claimQuote && !evidenceQuote && !bodyText && !showWhy ? (
          <p className="footnote">Open the scan report for details on this interrupt.</p>
        ) : null}
        <div className="gotcha-row-actions">
          {sources.map((doc) => {
            const id = doc.id || doc.document_id;
            const live = documents.find((d) => d.id === id);
            if (!live) return null;
            return (
              <button key={id} className="btn gold" type="button" onClick={() => onOpenReport(live)}>
                Open {live.display_name || live.original_filename}
              </button>
            );
          })}
          {renderFeedbackActions(row)}
        </div>
      </>
    );
  }

  const boardBody = hasInterrupts ? (
    <>
      <div className="case-impact-strip">
        <span className="case-impact-pill material">{materialCount} material</span>
        {fraudCount > 0 && (
          <span className="case-impact-pill fraud">{fraudCount} altered</span>
        )}
        {ignoredBoardCount > 0 && (
          <button
            type="button"
            className={`case-impact-pill noise${showIgnored ? " active" : ""}`}
            onClick={() => setShowIgnored((v) => !v)}
          >
            {showIgnored ? "Hide ignored" : `Show ignored (${ignoredBoardCount})`}
          </button>
        )}
        {noiseCount > 0 && (
          <button
            type="button"
            className="case-impact-pill noise"
            onClick={() => setShowNoise((v) => !v)}
          >
            {noiseCount} noise
          </button>
        )}
        {typeof onExportMatter === "function" ? (
          <button
            type="button"
            className="btn secondary case-impact-export"
            disabled={exportingMatter}
            onClick={onExportMatter}
          >
            <Download size={14} />{" "}
            {exportingMatter ? "Exporting…" : "Export matter"}
          </button>
        ) : null}
      </div>
      {showNoise && noiseCount > 0 && (
        <p className="footnote case-impact-noise-note">
          {noiseCount} finding{noiseCount === 1 ? "" : "s"} demoted as noise (boilerplate
          overlap, he-said-she-said, or no case-impact bucket).
        </p>
      )}
      {showIgnored && ignoredBoardCount > 0 && (
        <p className="footnote case-impact-noise-note">
          Showing firm-ignored findings. Evidence is kept; they stay out of material
          interrupts until you undo.
        </p>
      )}
      {rows.length === 0 ? (
        <p className="footnote case-impact-noise-note">
          All material interrupts are ignored. Use Show ignored to review or undo.
        </p>
      ) : null}
      <ul className="gotcha-list">
        {visibleRows.map((row) => {
          const open = openKey === row.key;
          return (
            <li
              key={row.key}
              className={`gotcha-row${row.tainted ? " gotcha-fraud" : ""}${row.ignored ? " gotcha-ignored" : ""}${open ? " open" : ""}`}
            >
              <button
                type="button"
                className="gotcha-row-toggle"
                aria-expanded={open}
                onClick={() => setOpenKey(open ? "" : row.key)}
              >
                <span className="gotcha-row-title">{row.title}</span>
                <span className="gotcha-row-meta">
                  {row.ignored ? "Ignored · " : ""}
                  {row.confirmed && !row.ignored ? "Confirmed · " : ""}
                  {row.tainted ? "Altered · " : ""}
                  {row.meta}
                </span>
                <span className="gotcha-row-chevron" aria-hidden="true">{open ? "▾" : "▸"}</span>
              </button>
              {open ? <div className="gotcha-row-detail">{renderRowDetail(row)}</div> : null}
            </li>
          );
        })}
      </ul>
      {hiddenCount > 0 && (
        <button
          type="button"
          className="gotcha-more"
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? "Show less" : `Show ${hiddenCount} more`}
        </button>
      )}
    </>
  ) : (
    <div className="case-impact-empty">
      <p className="case-impact-empty-copy">
        {loadError
          ? "Case Impact could not be loaded. Refresh the matter and try again."
          : CASE_IMPACT_EMPTY_COPY}
      </p>
      {typeof onExportMatter === "function" ? (
        <button
          type="button"
          className="btn secondary"
          disabled={exportingMatter}
          onClick={onExportMatter}
        >
          <Download size={14} />{" "}
          {exportingMatter ? "Exporting…" : "Export matter"}
        </button>
      ) : null}
    </div>
  );

  return (
    <details
      className="case-impact-panel"
      open={panelOpen}
      onToggle={(e) => {
        userToggledPanel.current = true;
        setPanelOpen(e.currentTarget.open);
      }}
    >
      <summary>
        <span>{summaryLabel}</span>
        <span>Case Impact</span>
      </summary>
      <div className="gotcha-board case-impact-body">{boardBody}</div>
    </details>
  );
}

const CHAT_HINTS = [
  "Which affidavit mentions a carnival?",
  "Where does defendant explain where he was at service?",
  "What was the Wells Fargo statement used for?",
];

const REPORT_CHAT_HINTS = [
  "What was this document used for?",
  "Which motions cite this exhibit?",
  "Does this filing mention where defendant was at service?",
];

function CaseChat({
  caseId,
  documents,
  onOpenReport,
  onError,
  focusDocument,
  onScanDocument,
  onRescanWaiting,
  onRefresh,
  embedded = false,
}) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [asking, setAsking] = useState(false);
  const [rescanning, setRescanning] = useState("");
  const threadRef = useRef(null);

  const waitingCount = (documents || []).filter((d) =>
    ["uploaded", "queued", "failed", "waiting"].includes(d.status)
  ).length;

  useEffect(() => {
    let cancelled = false;
    listCaseChat(caseId)
      .then((items) => {
        if (!cancelled) setMessages(items);
      })
      .catch(() => {
        if (!cancelled) setMessages([]);
      });
    return () => {
      cancelled = true;
    };
  }, [caseId]);

  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, asking]);

  function openCite(cite) {
    const doc = findCitedDocument(documents, cite);
    if (doc) onOpenReport(doc);
  }

  function needsRescan(doc) {
    if (!doc) return false;
    return ["uploaded", "queued", "failed", "waiting"].includes(doc.status) || !doc.last_scanned_at;
  }

  async function rescanCited(cite) {
    const doc = findCitedDocument(documents, cite);
    if (!doc || !onScanDocument) return;
    setRescanning(doc.id);
    onError("");
    try {
      await onScanDocument(doc);
      if (onRefresh) await onRefresh();
    } catch (err) {
      onError(err.message);
    } finally {
      setRescanning("");
    }
  }

  async function rescanWaiting() {
    if (!onRescanWaiting) return;
    setRescanning("waiting");
    onError("");
    try {
      await onRescanWaiting();
      if (onRefresh) await onRefresh();
    } catch (err) {
      onError(err.message);
    } finally {
      setRescanning("");
    }
  }

  async function send(question) {
    const text = (question || draft).trim();
    if (!text || asking) return;
    setDraft("");
    setAsking(true);
    onError("");
    const pending = { id: `local-${Date.now()}`, role: "user", content: text, citations: [] };
    setMessages((prev) => [...prev, pending]);
    try {
      const result = await askCaseChat(caseId, text, {
        focusDocumentId: focusDocument?.id,
        documents,
      });
      setMessages((prev) => [
        ...prev.filter((item) => item.id !== pending.id),
        { ...pending, id: `${pending.id}-kept` },
        result.message || {
          id: `${pending.id}-answer`,
          role: "assistant",
          content: result.answer,
          citations: result.citations || [],
        },
      ]);
    } catch (err) {
      setMessages((prev) => prev.filter((item) => item.id !== pending.id));
      onError(err.message);
    } finally {
      setAsking(false);
    }
  }

  const thinText =
    messages.some(
      (m) =>
        m.role === "assistant" &&
        /scan the filings|not scanned|no extracted text|rescann/i.test(m.content || "")
    );

  return (
    <div className={`${embedded ? "case-chat embedded" : "card case-chat"}${focusDocument ? " on-report" : ""}`}>
      {!embedded && (
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <p className="kicker">{focusDocument ? "Case clerk" : "Ask"}</p>
          <h2 className="serif" style={{ margin: "4px 0 8px" }}>
            {focusDocument ? "Ask about this document" : "Search filings in this matter"}
          </h2>
        </div>
        {onRescanWaiting && waitingCount > 0 && (
          <button
            className="btn secondary"
            type="button"
            disabled={Boolean(rescanning) || asking}
            onClick={rescanWaiting}
            title="Scan files that are still uploaded, failed, or waiting so chat can search their text"
          >
            <RefreshCw size={14} />
            {rescanning === "waiting" ? "Rescanning…" : `Rescan waiting (${waitingCount})`}
          </button>
        )}
      </div>
      )}
      {embedded && onRescanWaiting && waitingCount > 0 && (
        <div className="row" style={{ justifyContent: "flex-end", marginBottom: 10 }}>
          <button
            className="btn secondary"
            type="button"
            disabled={Boolean(rescanning) || asking}
            onClick={rescanWaiting}
            title="Scan files that are still uploaded, failed, or waiting so chat can search their text"
          >
            <RefreshCw size={14} />
            {rescanning === "waiting" ? "Rescanning…" : `Rescan waiting (${waitingCount})`}
          </button>
        </div>
      )}
      <p className="footnote" style={{ marginBottom: 14 }}>
        {focusDocument
          ? `Questions treat “this” as ${focusDocument.display_name || focusDocument.original_filename}, then search the rest of the case.`
          : "Ask which filing mentions a fact (e.g. a carnival, an address, a fee). Open the source, or rescan if text is missing."}
      </p>
      {thinText && onRescanWaiting && (
        <p className="footnote" style={{ marginTop: -6, marginBottom: 12 }}>
          If the clerk could not find it, try Rescan waiting so unscanned exhibits get text, then ask again.
        </p>
      )}
      <div className="chat-thread" ref={threadRef}>
        {messages.length === 0 && !asking && (
          <div className="chat-empty">
            <MessageSquare size={18} />
            <p>
              {focusDocument
                ? "Ask what this filing was used for, or whether another document in the case contradicts it."
                : "Ask which document mentions a person, place, or event — for example where the defendant said he was at service."}
            </p>
            <div className="chat-hints">
              {(focusDocument ? REPORT_CHAT_HINTS : CHAT_HINTS).map((hint) => (
                <button key={hint} type="button" className="chat-hint" onClick={() => send(hint)}>
                  {hint}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((msg) => {
          const cites = msg.role === "assistant"
            ? mergeCitations(msg.citations, msg.content, documents)
            : [];
          return (
          <div key={msg.id} className={`chat-bubble ${msg.role}`}>
            {msg.role === "assistant" ? (
              <ChatAnswer text={msg.content} citations={cites} onOpen={openCite} />
            ) : (
              <p>{msg.content}</p>
            )}
            {cites.length > 0 && (
              <div className="chat-cites">
                {cites.map((cite, idx) => {
                  const doc = findCitedDocument(documents, cite);
                  return (
                    <span key={`${msg.id}-${idx}`} className="chat-cite-group">
                      <button
                        type="button"
                        className="chat-cite"
                        onClick={() => openCite(cite)}
                        title={cite.note || "Open this document’s report"}
                      >
                        Open {cite.display_name}
                      </button>
                      {onScanDocument && doc && needsRescan(doc) && (
                        <button
                          type="button"
                          className="chat-cite chat-cite-rescan"
                          disabled={Boolean(rescanning)}
                          onClick={() => rescanCited(cite)}
                          title="Rescan this file to refresh searchable text"
                        >
                          {rescanning === doc.id ? "Rescanning…" : "Rescan this file"}
                        </button>
                      )}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
          );
        })}
        {asking && <p className="footnote">Searching the filings…</p>}
      </div>
      <form
        className="chat-compose"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <textarea
          value={draft}
          rows={2}
          placeholder={
            focusDocument
              ? "e.g. What was this exhibit used for?"
              : "e.g. Which affidavit mentions a carnival?"
          }
          disabled={asking}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button className="btn gold" type="submit" disabled={asking || !draft.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}

function findCitedDocument(documents, cite) {
  if (!cite) return null;
  const byId = documents.find((item) => item.id === cite.document_id);
  if (byId) return byId;
  const no = cite.nyscef_no || nyscefNumbers(`${cite.display_name || ""} ${cite.note || ""}`)[0];
  if (no) {
    const byNo = documents.find((item) => documentHasNyscef(item, no));
    if (byNo) return byNo;
  }
  const name = (cite.display_name || "").toLowerCase();
  if (name.length > 8) {
    return documents.find((item) => {
      const hay = `${item.display_name || ""} ${item.original_filename || ""}`.toLowerCase();
      return hay.includes(name) || name.includes(hay);
    }) || null;
  }
  return null;
}

function nyscefNumbers(text) {
  return [...(text || "").matchAll(/NYSCEF\s+Doc(?:ument)?\.?\s*No\.?\s*(\d+)/gi)].map((match) => match[1]);
}

function documentHasNyscef(doc, no) {
  const hay = `${doc.display_name || ""} ${doc.original_filename || ""}`;
  if (new RegExp(`NYSCEF\\s+Doc(?:ument)?\\.?\\s*No\\.?\\s*${no}\\b`, "i").test(hay)) return true;
  if (new RegExp(`(?:affirm|affidavit|motion|exhibit|order|complaint|doc)\\w*\\s+${no}\\b`, "i").test(hay)) return true;
  const stem = (doc.display_name || doc.original_filename || "").replace(/\.[^.]+$/, "").trim();
  return new RegExp(`(?:^|[\\s_\\-])${no}$`).test(stem);
}

function mergeCitations(citations, text, documents) {
  const out = [];
  const seen = new Set();
  function add(cite) {
    const doc = findCitedDocument(documents, cite);
    const key = doc?.id || cite.document_id || cite.display_name;
    if (!key || seen.has(key)) return;
    seen.add(key);
    const no = cite.nyscef_no || (doc && nyscefFromDoc(doc));
    let label = cite.display_name || doc?.display_name || "Document";
    if (no && !label.includes(no)) label = `${label} (NYSCEF ${no})`;
    out.push({
      ...cite,
      document_id: doc?.id || cite.document_id,
      display_name: label,
      nyscef_no: no,
    });
  }
  (citations || []).forEach(add);
  for (const match of text.matchAll(/["“]([^"”]{8,160})["”]\s*\((?:NYSCEF\s+)?Doc(?:ument)?\.?\s*No\.?\s*(\d+)\)/gi)) {
    add({ display_name: match[1], nyscef_no: match[2], note: "Named in the answer" });
  }
  for (const no of nyscefNumbers(text)) {
    add({ display_name: `NYSCEF Doc. No. ${no}`, nyscef_no: no, note: "Named in the answer" });
  }
  return out;
}

function nyscefFromDoc(doc) {
  const hay = `${doc.display_name || ""} ${doc.original_filename || ""}`;
  return nyscefNumbers(hay)[0] || (hay.match(/(?:affirm|affidavit|motion|exhibit|order|complaint)\w*\s+(\d{1,4})\b/i) || [])[1];
}

function ChatAnswer({ text, citations, onOpen }) {
  const ranges = [];
  for (const cite of citations) {
    if (cite.nyscef_no) {
      const re = new RegExp(`NYSCEF\\s+Doc(?:ument)?\\.?\\s*No\\.?\\s*${cite.nyscef_no}`, "gi");
      for (const match of text.matchAll(re)) {
        ranges.push({ start: match.index, end: match.index + match[0].length, cite, label: match[0] });
      }
    }
    const title = (cite.display_name || "").replace(/\s*\(NYSCEF \d+\)$/, "");
    if (title.length > 12) {
      const at = text.indexOf(title);
      if (at >= 0) ranges.push({ start: at, end: at + title.length, cite, label: title });
    }
  }
  ranges.sort((a, b) => a.start - b.start || b.end - a.end);
  const kept = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start < cursor) continue;
    kept.push(range);
    cursor = range.end;
  }
  if (kept.length === 0) return <p>{text}</p>;
  const nodes = [];
  let last = 0;
  kept.forEach((range, idx) => {
    if (range.start > last) nodes.push(text.slice(last, range.start));
    nodes.push(
      <button
        key={`link-${idx}`}
        type="button"
        className="chat-inline-link"
        onClick={() => onOpen(range.cite)}
      >
        {range.label}
      </button>
    );
    last = range.end;
  });
  if (last < text.length) nodes.push(text.slice(last));
  return <p>{nodes}</p>;
}

function CaseRiskSummary({ byRisk, pendingCount, activeRisk, onFilterRisk }) {
  const groups = [
    { key: "red", label: "High risk", docs: byRisk.red },
    { key: "yellow", label: "Moderate", docs: byRisk.yellow },
    { key: "green", label: "Low risk", docs: byRisk.green },
  ];

  return (
    <div className="card triage-card">
      <div className="triage-bar">
        <div className="triage-bar-label">
          <p className="kicker">Triage</p>
          {pendingCount > 0 ? (
            <span className="footnote">{pendingCount} scanning</span>
          ) : (
            <span className="footnote">Filter the table by risk</span>
          )}
        </div>
        <div className="triage-bands" role="group" aria-label="Risk filters">
          {groups.map((group) => (
            <button
              key={group.key}
              type="button"
              className={`triage-band risk-${group.key}${activeRisk === group.key ? " active" : ""}`}
              onClick={() => onFilterRisk?.(group.key)}
              title={`${group.label}: ${group.docs.length} — click to filter Documents`}
            >
              <span className="triage-band-label">{group.label}</span>
              <span className="triage-band-count">{group.docs.length}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

const PLAN_SUMMARY = (
  <>
    <strong>$79/seat/month</strong>. Month-to-month, cancel anytime. A 14-day trial collects a card up front and converts to paid after 14 days unless you cancel.
  </>
);

function accessActionLabel(action) {
  const labels = {
    "login.success": "Signed in",
    "login.failure": "Sign-in failed",
    "login.mfa_required": "Password accepted — MFA required",
    "login.mfa_failure": "MFA failed",
    "login.sso_success": "Signed in with SSO",
    "login.sso_failure": "SSO failed",
    "2fa.enroll_start": "Started 2FA setup",
    "2fa.enroll_confirm": "Enabled 2FA",
    "2fa.disable": "Disabled 2FA",
    "2fa.recovery_used": "Used a recovery code",
    "session.revoke": "Revoked a session",
    "session.revoke_all": "Revoked other sessions",
    "session.revoke_member": "Revoked a member's sessions",
    "session.revoke_firm": "Revoked firm-wide sessions",
    "admin.settings_view": "Opened admin overview",
    "admin.settings_update": "Updated admin settings",
    "admin.overview": "Opened firm admin",
    "admin.invite": "Invited a seat",
    "admin.invite_revoke": "Revoked a seat invite",
    "admin.role_change": "Changed a member role",
    "admin.member_remove": "Removed a member",
    "firm.member_remove": "Removed a firm member",
    "firm.member_join": "Member joined the firm",
    "firm.settings_update": "Updated firm settings",
    "matter.list": "Listed matters",
    "matter.view": "Opened a matter",
    "matter.create": "Created a matter",
    "matter.delete": "Deleted a matter",
    "matter.export": "Exported a matter",
    "scan.start": "Started a scan",
    "document.scan": "Scanned a document",
    "firm.create": "Created the firm",
  };
  return labels[action] || action || "Activity";
}

function SecurityPanel({ entitlement, onRefresh, onError, authStatus }) {
  const [working, setWorking] = useState("");
  const [enrollSecret, setEnrollSecret] = useState("");
  const [enrollUrl, setEnrollUrl] = useState("");
  const [confirmCode, setConfirmCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState(null);
  const [disablePassword, setDisablePassword] = useState("");
  const [disableCode, setDisableCode] = useState("");

  const totpOn = Boolean(entitlement?.totp_enabled);
  const totpOk = authStatus?.totp_available !== false;
  const accountRole = entitlement?.account_role || (entitlement?.is_account_admin ? "owner" : "member");

  async function guard(action, fn) {
    setWorking(action);
    onError("");
    try {
      await fn();
    } catch (err) {
      onError(err.message || "Security action failed.");
    } finally {
      setWorking("");
    }
  }

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <p className="kicker">Account security</p>
      <h3 className="serif" style={{ margin: "2px 0 6px" }}>Two-factor authentication</h3>
      <p className="footnote" style={{ marginTop: 0 }}>
        Turn on an authenticator app for password sign-in. After it is on, login asks for a 6-digit code
        (or a one-time recovery code). SSO users skip local TOTP when the identity provider already enforces MFA.
        {entitlement?.oidc_linked ? " This account is linked to SSO." : ""}
      </p>
      <p className="footnote">
        Role: <strong>{String(accountRole)}</strong>
        {" · "}
        Authenticator: <strong>{totpOn ? "On" : "Off"}</strong>
        {entitlement?.oidc_linked ? " · SSO linked" : ""}
      </p>

      {!totpOn && !totpOk ? (
        <p className="footnote">Authenticator setup is unavailable on this control plane (restart control with current deps).</p>
      ) : null}

      {!totpOn && totpOk && !enrollSecret ? (
        <>
          <ol className="footnote" style={{ paddingLeft: 18, margin: "8px 0 12px" }}>
            <li>Click <strong>Enable authenticator</strong>.</li>
            <li>Add the secret (or otpauth URL) in Google Authenticator, 1Password, Authy, etc.</li>
            <li>Enter the 6-digit code to confirm — save the recovery codes offline.</li>
          </ol>
          <button
            className="btn gold"
            type="button"
            disabled={Boolean(working)}
            onClick={() => guard("enroll", async () => {
              const data = await startTotpEnrollment();
              setEnrollSecret(data.secret || "");
              setEnrollUrl(data.otpauth_url || "");
            })}
          >
            {working === "enroll" ? "Starting…" : "Enable authenticator"}
          </button>
        </>
      ) : null}

      {enrollSecret ? (
        <div className="case-form" style={{ marginTop: 12 }}>
          <p className="footnote">
            Add this secret in your authenticator app
            {enrollUrl ? (
              <>
                {" "}or open <code style={{ wordBreak: "break-all" }}>{enrollUrl}</code>
              </>
            ) : null}
            .
          </p>
          <p className="footnote">Secret: <code>{enrollSecret}</code></p>
          <label>
            Confirm with a 6-digit code
            <input
              value={confirmCode}
              onChange={(e) => setConfirmCode(e.target.value)}
              autoComplete="one-time-code"
              placeholder="123456"
            />
          </label>
          <div className="actions">
            <button
              className="btn"
              type="button"
              disabled={Boolean(working)}
              onClick={() => guard("confirm", async () => {
                const data = await confirmTotpEnrollment(confirmCode.trim());
                setRecoveryCodes(data.recovery_codes || []);
                setEnrollSecret("");
                setEnrollUrl("");
                setConfirmCode("");
                await onRefresh();
              })}
            >
              {working === "confirm" ? "Confirming…" : "Confirm and enable"}
            </button>
            <button
              className="btn secondary"
              type="button"
              disabled={Boolean(working)}
              onClick={() => {
                setEnrollSecret("");
                setEnrollUrl("");
                setConfirmCode("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {recoveryCodes && recoveryCodes.length > 0 ? (
        <div style={{ marginTop: 12 }}>
          <p className="footnote" style={{ color: "var(--red)" }}>
            Save these recovery codes now. Each works once. They will not be shown again.
          </p>
          <ul className="footnote" style={{ paddingLeft: 18 }}>
            {recoveryCodes.map((code) => (
              <li key={code}><code>{code}</code></li>
            ))}
          </ul>
          <button className="btn secondary" type="button" onClick={() => setRecoveryCodes(null)}>
            Done
          </button>
        </div>
      ) : null}

      {totpOn ? (
        <form
          className="case-form"
          style={{ marginTop: 12 }}
          onSubmit={(e) => {
            e.preventDefault();
            guard("disable", async () => {
              await disableTotp(disablePassword, disableCode.trim());
              setDisablePassword("");
              setDisableCode("");
              await onRefresh();
            });
          }}
        >
          <p className="footnote">Disable 2FA requires your password and a current authenticator or recovery code. Admins cannot bypass this.</p>
          <label>
            Password
            <input
              type="password"
              value={disablePassword}
              onChange={(e) => setDisablePassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          <label>
            Authenticator or recovery code
            <input
              value={disableCode}
              onChange={(e) => setDisableCode(e.target.value)}
              autoComplete="one-time-code"
              required
            />
          </label>
          <button className="btn secondary danger" type="submit" disabled={Boolean(working)}>
            {working === "disable" ? "Disabling…" : "Disable 2FA"}
          </button>
        </form>
      ) : null}
    </div>
  );
}

function AdminPanel({ entitlement, onError, onRefresh, onOpenAccount }) {
  const [overview, setOverview] = useState(null);
  const [firm, setFirm] = useState(null);
  const [matterAudit, setMatterAudit] = useState([]);
  const [loadError, setLoadError] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [working, setWorking] = useState("");
  const [firmName, setFirmName] = useState("");
  const [persistFacts, setPersistFacts] = useState(true);
  const [storeSnippets, setStoreSnippets] = useState(true);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [inviteLink, setInviteLink] = useState("");

  const myRole = (
    entitlement?.account_role
    || overview?.account?.role
    || (entitlement?.is_account_admin ? "owner" : "member")
  ).toLowerCase();
  const isOwner = myRole === "owner";
  const isAdmin = myRole === "owner" || myRole === "admin";

  async function reloadAdmin() {
    const [cp, fm, access] = await Promise.all([
      fetchAdminOverview(),
      fetchFirmAdminOverview().catch(() => null),
      listAccessLog().catch(() => ({ matter_events: [] })),
    ]);
    setOverview(cp);
    setFirm(fm);
    setMatterAudit(access.matter_events || []);
    if (fm?.firm) {
      setFirmName(fm.firm.name || "");
      setPersistFacts(Boolean(fm.firm.persist_facts));
      setStoreSnippets(Boolean(fm.firm.store_snippets !== false));
    }
    return cp;
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (entitlement?.hasSession && entitlement?.is_account_admin !== true) {
        setForbidden(true);
        setLoadError("Firm admin access is required. Ask an owner or admin, or use Account for your personal security settings.");
        return;
      }
      try {
        await reloadAdmin();
      } catch (err) {
        if (cancelled) return;
        const msg = err.message || "Could not load admin overview.";
        const denied = /403|forbidden|admin access/i.test(msg);
        setForbidden(denied);
        setLoadError(msg);
        onError(msg);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function guard(action, fn) {
    setWorking(action);
    onError("");
    try {
      await fn();
    } catch (err) {
      onError(err.message || "Admin action failed.");
    } finally {
      setWorking("");
    }
  }

  if (forbidden || (loadError && !overview)) {
    return (
      <div className="card">
        <p className="kicker">Firm admin</p>
        <h2 className="serif" style={{ marginTop: 4 }}>Admin</h2>
        <p className="footnote">
          {forbidden
            ? "Permission denied. Only firm owners and admins can open this panel."
            : loadError}
        </p>
        {onOpenAccount ? (
          <button className="btn secondary" type="button" onClick={onOpenAccount}>
            Open Account
          </button>
        ) : null}
      </div>
    );
  }

  if (!overview) {
    return (
      <div className="card">
        <p className="footnote">Loading admin overview…</p>
      </div>
    );
  }

  const account = overview.account || {};
  const security = overview.security || {};
  const sessions = overview.sessions || [];
  const audit = overview.audit || [];
  const users = overview.users || [];
  const invites = overview.invites || [];
  const jobs = firm?.jobs || null;
  const ownerCount = users.filter((u) => (u.role || "").toLowerCase() === "owner").length;
  const myEmail = (account.email || entitlement?.email || "").toLowerCase();

  return (
    <div className="card">
      <p className="kicker">Firm admin</p>
      <h2 className="serif" style={{ marginTop: 4 }}>Admin</h2>
      <p className="footnote" style={{ marginTop: 0 }}>
        Access is limited to your account and firm. Document contents are never shown here.
      </p>
      <div className="role-legend">
        <p className="footnote" style={{ margin: 0 }}>
          <strong>Owner</strong> — seats, roles (including admin), firm-wide session revoke, billing seats.
          {" "}
          <strong>Admin</strong> — invite members, firm settings, matter access log, revoke member sessions.
          {" "}
          <strong>Member</strong> — cases and scans only; Account shows personal sign-in log and 2FA.
        </p>
      </div>

      <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Firm overview</h3>
      <p className="footnote" style={{ marginTop: 0 }}>
        {firm?.firm?.name || firmName || "Firm"}
        {" · "}
        {account.email || entitlement?.email}
        {account.role ? ` · ${account.role}` : ""}
        {account.plan ? ` · plan ${account.plan}` : ""}
        {account.billing_state ? ` · billing ${account.billing_state}` : ""}
        {account.seats != null
          ? ` · ${account.seats_used != null ? `${account.seats_used}/` : ""}${account.seats} seat${account.seats === 1 ? "" : "s"}`
          : " · seats from billing"}
        {account.support_code ? ` · ${account.support_code}` : ""}
      </p>
      <p className="footnote">{overview.message}</p>

      <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Members</h3>
      {users.length === 0 ? (
        <p className="footnote">No members listed yet.</p>
      ) : (
        <ul className="footnote" style={{ paddingLeft: 18 }}>
          {users.map((u) => {
            const role = (u.role || "member").toLowerCase();
            const isSelf = (u.email || "").toLowerCase() === myEmail;
            const canEditRole = Boolean(u.user_id) && (
              isOwner
              || (isAdmin && role === "member" && !isSelf)
            );
            const canRemove = Boolean(u.user_id) && !isSelf && (
              isOwner
              || (isAdmin && role === "member")
            ) && !(role === "owner" && ownerCount <= 1);
            return (
              <li key={u.user_id || u.email}>
                {u.email}
                {" · "}
                {canEditRole ? (
                  <select
                    aria-label={`Role for ${u.email}`}
                    value={role}
                    disabled={Boolean(working)}
                    onChange={(e) => guard(`role-${u.user_id}`, async () => {
                      await setMemberRole(u.user_id, e.target.value);
                      await reloadAdmin();
                      await onRefresh();
                    })}
                  >
                    {isOwner ? <option value="owner">owner</option> : null}
                    {isOwner || role === "admin" ? <option value="admin">admin</option> : null}
                    <option value="member">member</option>
                  </select>
                ) : (
                  <span>{role}{isSelf ? " (you)" : ""}</span>
                )}
                {u.totp_enabled ? " · 2FA on" : " · 2FA off"}
                {u.oidc_linked ? " · SSO" : ""}
                {u.user_id ? (
                  <>
                    {" · "}
                    <button
                      type="button"
                      className="linkish"
                      disabled={Boolean(working)}
                      onClick={() => guard(`sess-${u.user_id}`, async () => {
                        await revokeMemberSessions(u.user_id);
                        await reloadAdmin();
                        if (isSelf) await onRefresh();
                      })}
                    >
                      Revoke sessions
                    </button>
                  </>
                ) : null}
                {canRemove ? (
                  <>
                    {" · "}
                    <button
                      type="button"
                      className="linkish"
                      disabled={Boolean(working)}
                      onClick={() => guard(`rm-${u.user_id}`, async () => {
                        if (!window.confirm(`Remove ${u.email} from this firm?`)) return;
                        await removeOrgMember(u.user_id);
                        await reloadAdmin();
                      })}
                    >
                      Remove
                    </button>
                  </>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Pending invites</h3>
      {invites.length === 0 ? (
        <p className="footnote">No pending invites.</p>
      ) : (
        <ul className="footnote" style={{ paddingLeft: 18 }}>
          {invites.map((inv) => (
            <li key={inv.id}>
              {inv.email} · {inv.role}
              {inv.expires_at ? ` · expires ${inv.expires_at}` : ""}
              {" · "}
              <button
                type="button"
                className="linkish"
                disabled={Boolean(working)}
                onClick={() => guard(`rev-inv-${inv.id}`, async () => {
                  await revokeSeatInvite(inv.id);
                  await reloadAdmin();
                })}
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="case-form"
        style={{ marginTop: 12 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (!inviteEmail.trim()) return;
          guard("invite", async () => {
            const res = await createSeatInvite(inviteEmail.trim(), inviteRole);
            setInviteEmail("");
            setInviteLink(res.invite_url || "");
            await reloadAdmin();
          });
        }}
      >
        <label>
          Invite email
          <input
            type="email"
            value={inviteEmail}
            onChange={(e) => setInviteEmail(e.target.value)}
            placeholder="colleague@firm.com"
            required
          />
        </label>
        <label>
          Role
          <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
            <option value="member">member</option>
            {isOwner ? <option value="admin">admin</option> : null}
          </select>
        </label>
        <button className="btn gold" type="submit" disabled={Boolean(working) || !inviteEmail.trim()}>
          {working === "invite" ? "Inviting…" : "Invite seat"}
        </button>
      </form>
      {inviteLink ? (
        <p className="footnote" style={{ marginTop: 8 }}>
          Invite link (share if email was not sent): <code>{inviteLink}</code>
        </p>
      ) : null}

      <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Security posture</h3>
      <p className="footnote">
        Your 2FA: {security.totp_enabled ? "on" : "off"}
        {" · "}
        SSO available: {security.sso_available ? "yes" : "no"}
        {" · "}
        SSO linked: {security.oidc_linked ? "yes" : "no"}
        {" · "}
        IdP MFA trusted: {security.oidc_trusts_idp_mfa ? "yes" : "no"}
      </p>
      <p className="footnote">
        Manage authenticator enrollment on Account.
        {onOpenAccount ? (
          <>
            {" "}
            <button type="button" className="linkish" onClick={onOpenAccount}>
              Open Account security
            </button>
          </>
        ) : null}
      </p>
      <label className="footnote" style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input
          type="checkbox"
          checked={Boolean(security.sso_preferred)}
          disabled={Boolean(working) || !security.sso_available}
          onChange={(e) => guard("sso", async () => {
            await patchAdminSettings({ sso_preferred: e.target.checked });
            setOverview((prev) => ({
              ...prev,
              security: { ...prev.security, sso_preferred: e.target.checked },
            }));
          })}
        />
        Prefer SSO when signing in (password login remains available)
      </label>

      {firm?.firm ? (
        <>
          <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Firm settings</h3>
          <form
            className="case-form"
            onSubmit={(e) => {
              e.preventDefault();
              guard("firm", async () => {
                const data = await patchFirmAdminSettings({
                  name: firmName.trim() || undefined,
                  persist_facts: persistFacts,
                  store_snippets: storeSnippets,
                });
                setFirm((prev) => ({ ...prev, firm: data.firm || prev?.firm }));
              });
            }}
          >
            <label>
              Firm display name
              <input value={firmName} onChange={(e) => setFirmName(e.target.value)} />
            </label>
            <label className="footnote" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={persistFacts} onChange={(e) => setPersistFacts(e.target.checked)} />
              Persist encrypted fact memory
            </label>
            <label className="footnote" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={storeSnippets} onChange={(e) => setStoreSnippets(e.target.checked)} />
              Store short quotes used for comparisons
            </label>
            <button className="btn secondary" type="submit" disabled={Boolean(working)}>
              {working === "firm" ? "Saving…" : "Save firm settings"}
            </button>
          </form>
        </>
      ) : (
        <p className="footnote" style={{ marginTop: 16 }}>
          Hosted analysis firm settings appear here once the analysis service is reachable.
        </p>
      )}

      {jobs ? (
        <>
          <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Scan / job health</h3>
          <p className="footnote">
            Total jobs: {jobs.total ?? 0}
            {" · "}
            Active: {jobs.active ?? 0}
            {jobs.by_status
              ? ` · ${Object.entries(jobs.by_status).map(([k, v]) => `${k}: ${v}`).join(", ")}`
              : ""}
          </p>
          {(jobs.recent || []).length > 0 ? (
            <ul className="footnote" style={{ paddingLeft: 18 }}>
              {jobs.recent.slice(0, 12).map((j) => (
                <li key={j.job_id}>
                  {j.status}
                  {j.display_name ? ` · ${j.display_name}` : ""}
                  {j.updated_at ? ` · ${j.updated_at}` : ""}
                  {j.error ? ` · ${j.error}` : ""}
                </li>
              ))}
            </ul>
          ) : (
            <p className="footnote">No recent jobs.</p>
          )}
        </>
      ) : null}

      <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Sessions</h3>
      <p className="footnote" style={{ marginTop: 0 }}>
        Your sessions below. Use member “Revoke sessions” above for another seat, or firm-wide (owner).
      </p>
      <div className="actions" style={{ marginBottom: 8 }}>
        <button
          className="btn secondary"
          type="button"
          disabled={Boolean(working)}
          onClick={() => guard("revoke-others", async () => {
            await revokeOtherSessions();
            await reloadAdmin();
            await onRefresh();
          })}
        >
          {working === "revoke-others" ? "Revoking…" : "Revoke my other sessions"}
        </button>
        {isOwner ? (
          <button
            className="btn secondary"
            type="button"
            disabled={Boolean(working)}
            onClick={() => guard("revoke-firm", async () => {
              if (!window.confirm("Revoke all sessions for every firm member except this one?")) return;
              await revokeFirmSessions();
              await reloadAdmin();
              await onRefresh();
            })}
          >
            {working === "revoke-firm" ? "Revoking…" : "Revoke firm-wide sessions"}
          </button>
        ) : null}
      </div>
      {sessions.length === 0 ? <p className="footnote">No sessions listed.</p> : null}
      <ul className="footnote" style={{ paddingLeft: 18 }}>
        {sessions.map((s) => (
          <li key={s.id}>
            {s.active ? "Active" : s.revoked ? "Revoked" : "Expired"}
            {s.created_at ? ` · created ${s.created_at}` : ""}
            {s.active ? (
              <>
                {" · "}
                <button
                  type="button"
                  className="linkish"
                  disabled={Boolean(working)}
                  onClick={() => guard(`rev-${s.id}`, async () => {
                    await revokeAdminSession(s.id);
                    await reloadAdmin();
                  })}
                >
                  Revoke
                </button>
              </>
            ) : null}
          </li>
        ))}
      </ul>

      <h3 className="serif" style={{ marginBottom: 4, marginTop: 20 }}>Access / audit log</h3>
      <p className="footnote" style={{ marginTop: 0 }}>
        Auth and admin events for your account, plus firm matter activity (owner/admin).
      </p>
      {audit.length === 0 && matterAudit.length === 0 ? (
        <p className="footnote">No audit events yet.</p>
      ) : null}
      <ul className="footnote" style={{ paddingLeft: 18, marginBottom: 0 }}>
        {[
          ...audit.map((row) => ({ ...row, source: "account" })),
          ...matterAudit.map((row) => ({ ...row, source: "firm" })),
        ]
          .sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")))
          .slice(0, 50)
          .map((row, index) => (
            <li key={`${row.source}-${row.at}-${row.action}-${index}`}>
              {row.source === "firm" ? "Firm · " : "Account · "}
              {accessActionLabel(row.action)}
              {row.at ? ` · ${row.at}` : ""}
            </li>
          ))}
      </ul>
    </div>
  );
}

function AccessLog({ onError }) {
  const [rows, setRows] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [matterDenied, setMatterDenied] = useState(false);
  const [sourceFilter, setSourceFilter] = useState("all");
  const [logPage, setLogPage] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listAccessLog()
      .then((data) => {
        if (cancelled) return;
        setMatterDenied(Boolean(data.matter_audit_denied));
        const signIns = (data.sign_in_events || []).map((row) => ({
          source: "account",
          action: row.action,
          at: row.at,
          request_id: row.request_id,
          target: row.target,
        }));
        const matters = (data.matter_events || []).map((row) => ({
          source: "firm",
          action: row.action,
          at: row.at,
          request_id: row.request_id,
          target: row.target,
        }));
        const merged = [...signIns, ...matters].sort((a, b) =>
          String(b.at || "").localeCompare(String(a.at || ""))
        );
        setRows(merged.slice(0, 200));
        setLogPage(0);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err.message || "Could not load the access log.");
        onError(err.message || "Could not load the access log.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setLogPage(0);
  }, [sourceFilter]);

  const filtered = (rows || []).filter((row) => {
    if (sourceFilter === "all") return true;
    return row.source === sourceFilter;
  });
  const logPageCount = Math.max(1, Math.ceil(filtered.length / ACCESS_LOG_PAGE_SIZE) || 1);
  const safeLogPage = Math.min(Math.max(0, logPage), logPageCount - 1);
  const pageRows = filtered.slice(
    safeLogPage * ACCESS_LOG_PAGE_SIZE,
    (safeLogPage + 1) * ACCESS_LOG_PAGE_SIZE
  );

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <p className="kicker">Hybrid audit</p>
      <h3 className="serif" style={{ margin: "2px 0 6px" }}>Access log</h3>
      <p className="footnote" style={{ marginTop: 0 }}>
        Control-plane sign-ins and security events for <em>this account</em>, plus firm matter
        activity when you are a firm <strong>owner</strong> or <strong>admin</strong>. Document
        contents are never stored here.
      </p>
      {matterDenied ? (
        <p className="footnote">
          Firm matter activity is hidden for your role. Ask a firm owner/admin, or open the{" "}
          <strong>Admin</strong> tab if you have that access.
        </p>
      ) : null}
      <div className="doc-filter-groups" style={{ marginBottom: 8 }}>
        <label className="doc-filter-group">
          <span className="doc-filter-label">Source</span>
          <select
            className="doc-filter-select"
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            aria-label="Filter access log by source"
          >
            <option value="all">All</option>
            <option value="account">Account (control plane)</option>
            <option value="firm">Firm (matters)</option>
          </select>
        </label>
      </div>
      {loadError ? <p className="footnote">{loadError}</p> : null}
      {rows === null && !loadError ? <p className="footnote">Loading access log…</p> : null}
      {rows && filtered.length === 0 ? <p className="footnote">No access events yet.</p> : null}
      {rows && filtered.length > 0 ? (
        <>
          <PaginationBar
            page={safeLogPage}
            pageSize={ACCESS_LOG_PAGE_SIZE}
            total={filtered.length}
            onChange={setLogPage}
            label="events"
          />
          <table className="data access-log-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Source</th>
                <th>Event</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row, index) => (
                <tr key={`${row.at || "t"}-${row.action || "a"}-${index}`}>
                  <td className="access-log-when">{row.at || "—"}</td>
                  <td>{row.source === "firm" ? "Firm" : "Account"}</td>
                  <td>{accessActionLabel(row.action)}</td>
                  <td className="access-log-detail">
                    {[row.target, row.request_id ? `req ${String(row.request_id).slice(0, 10)}` : null]
                      .filter(Boolean)
                      .join(" · ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <PaginationBar
            page={safeLogPage}
            pageSize={ACCESS_LOG_PAGE_SIZE}
            total={filtered.length}
            onChange={setLogPage}
            label="events"
          />
        </>
      ) : null}
    </div>
  );
}

function AccountPanel({ entitlement, onRefresh, onError }) {
  const [email, setEmail] = useState(() => entitlement?.email || "");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [acceptedTerms, setAcceptedTerms] = useState(false);
  const [license, setLicense] = useState("");
  const [problemNote, setProblemNote] = useState("");
  const [showTerms, setShowTerms] = useState(false);
  const [working, setWorking] = useState("");
  const [cacheLabel, setCacheLabel] = useState("Cached exhibits on this computer: …");
  const [clearingCache, setClearingCache] = useState(false);
  const [mfaToken, setMfaToken] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [authStatus, setAuthStatus] = useState({ sso_available: false, totp_available: false });
  const [forgotMode, setForgotMode] = useState(false);
  const [forgotSent, setForgotSent] = useState(false);
  const [resetToken, setResetToken] = useState("");
  const [resetPassword, setResetPassword] = useState("");

  useEffect(() => {
    if (entitlement?.email && !email) setEmail(entitlement.email);
  }, [entitlement?.email]);

  useEffect(() => {
    // Deep-link from marketing reset email: /?reset_token=…
    if (typeof window === "undefined") return;
    try {
      const params = new URLSearchParams(window.location.search || "");
      const token = (params.get("reset_token") || "").trim();
      if (!token) return;
      setResetToken(token);
      setForgotMode(true);
      params.delete("reset_token");
      const next = `${window.location.pathname}${params.toString() ? `?${params}` : ""}${window.location.hash || ""}`;
      window.history.replaceState({}, "", next);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchAuthStatus().then((status) => {
      if (!cancelled) setAuthStatus(status || {});
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    import("./storage/index.js")
      .then(({ cacheUsageLabel }) => cacheUsageLabel())
      .then((label) => {
        if (!cancelled) setCacheLabel(label);
      })
      .catch(() => {
        if (!cancelled) setCacheLabel("Cached exhibits on this computer: unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!entitlement) {
    return <div className="card"><p>Loading account status…</p></div>;
  }

  const signedIn = Boolean(entitlement.hasSession);
  const busy = Boolean(working);
  const billingState = entitlement.billing_state
    || (entitlement.active ? (entitlement.plan === "trial" ? "trialing" : "active") : "none");

  async function guard(action, fn) {
    setWorking(action);
    onError("");
    try {
      await fn();
    } catch (err) {
      onError(err.message);
      if (/session expired|sign in again|invalid session/i.test(String(err.message || ""))) {
        await onRefresh();
      }
    } finally {
      setWorking("");
    }
  }

  async function openCheckoutTrial() {
    const session = await startCheckout(true, 1);
    if (session?.url) {
      openCheckoutUrl(session.url);
      return;
    }
    if (session?.mode === "dev") {
      onError(session.message || "Trial updated (Stripe not configured).");
    }
    await onRefresh();
  }

  /** Prefer DOM values so browser/ChatGPT autofill works with controlled inputs. */
  function credentialsFromForm(form) {
    const fd = form instanceof HTMLFormElement ? new FormData(form) : null;
    const domEmail = String(fd?.get("email") || "").trim();
    const domPassword = String(fd?.get("password") || "");
    const domName = String(fd?.get("name") || "").trim();
    const domInvite = String(fd?.get("invite_code") || "").trim();
    const nextEmail = (domEmail || email || "").trim();
    const nextPassword = domPassword || password || "";
    const nextName = domName || name.trim() || "";
    const nextInvite = domInvite || inviteCode.trim() || "";
    if (domEmail && domEmail !== email) setEmail(domEmail);
    if (domPassword && domPassword !== password) setPassword(domPassword);
    if (domName && domName !== name) setName(domName);
    if (domInvite && domInvite !== inviteCode) setInviteCode(domInvite);
    return { email: nextEmail, password: nextPassword, name: nextName, inviteCode: nextInvite };
  }

  async function startTrialSignedOut(form) {
    const creds = credentialsFromForm(form);
    if (!creds.email || !creds.email.includes("@")) throw new Error("Enter a valid work email.");
    if (!creds.password || creds.password.length < 8) throw new Error("Password must be at least 8 characters.");
    if (!acceptedTerms) {
      throw new Error("Agree to the Terms of Service and Privacy Policy to continue.");
    }
    try {
      await signupCloud(
        creds.email,
        creds.password,
        creds.name || null,
        true,
        creds.inviteCode || null,
        peekSeatInviteToken() || null
      );
    } catch (err) {
      const msg = String(err?.message || "");
      if (/already exists/i.test(msg)) {
        try {
          await loginCloud(creds.email, creds.password);
        } catch (loginErr) {
          const loginMsg = String(loginErr?.message || "");
          throw new Error(
            loginMsg && !/invalid email or password/i.test(loginMsg)
              ? loginMsg
              : "That email already has an account, but the password did not match. Sign in with the correct password, then start your trial.",
          );
        }
      } else {
        throw err;
      }
    }
    await openCheckoutTrial();
  }

  async function manageBilling() {
    if (!entitlement.has_billing_portal) {
      throw new Error("No Stripe customer yet — start a 14-day trial first.");
    }
    const session = await openBillingPortal();
    if (session?.url) {
      openCheckoutUrl(session.url);
      return;
    }
    throw new Error(session?.message || "Could not open billing portal.");
  }

  function statusLine() {
    if (billingState === "trialing" && entitlement.trial_days_left != null) {
      const n = entitlement.trial_days_left;
      return `Trial · ${n} day${n === 1 ? "" : "s"} left`;
    }
    if (billingState === "active") return "Subscription active";
    if (billingState === "past_due") return "Payment past due";
    if (billingState === "canceled") return "Subscription canceled";
    if (billingState === "trialing") return "Trial active";
    return entitlement.active ? "Scans unlocked" : "Scans locked";
  }

  return (
    <div className="card">
      <p className="kicker">Hybrid control plane</p>
      <h2 className="serif" style={{ marginTop: 4 }}>Subscription</h2>
      <p>
        Mode: <strong>{entitlement.mode}</strong>
        {" · "}
        {statusLine()}
        {signedIn && entitlement.email ? ` · ${entitlement.email}` : ""}
        {entitlement.seats != null ? ` · ${entitlement.seats} seat${entitlement.seats === 1 ? "" : "s"}` : ""}
      </p>
      <p className="footnote">{entitlement.message}</p>

      <div className="priority-support">
        <p className="priority-support-label">Priority support</p>
        {entitlement.support_code ? (
          <p className="footnote" style={{ margin: "4px 0 0" }}>
            Support code: <strong>{entitlement.support_code}</strong> — quote this in email so we can find your account without case files.
          </p>
        ) : null}
        <p className="footnote" style={{ margin: "4px 0 0" }}>
          Email:{" "}
          <a href="mailto:support@casesifter.com">support@casesifter.com</a>
        </p>
        <p className="footnote" style={{ margin: "4px 0 0" }}>
          For product help and billing. Do not paste document content or privileged filings into email.
        </p>
      </div>

      <div style={{ marginTop: 16 }}>
        <p className="footnote" style={{ margin: 0 }}>{cacheLabel}</p>
        <button
          className="btn secondary"
          type="button"
          style={{ marginTop: 8 }}
          disabled={clearingCache}
          onClick={async () => {
            if (!window.confirm("Clear cached exhibits on this computer? Server findings are kept.")) return;
            setClearingCache(true);
            try {
              const { clearCachedExhibits, cacheUsageLabel } = await import("./storage/index.js");
              await clearCachedExhibits();
              setCacheLabel(await cacheUsageLabel());
            } catch (err) {
              onError(err.message || "Could not clear cached exhibits.");
            } finally {
              setClearingCache(false);
            }
          }}
        >
          Clear cached exhibits
        </button>
      </div>

      {signedIn && (
        <SecurityPanel
          entitlement={entitlement}
          onRefresh={onRefresh}
          onError={onError}
          authStatus={authStatus}
        />
      )}

      {signedIn && <AccessLog onError={onError} />}

      {isDesktopApp() && signedIn && (
        <form
          className="case-form"
          style={{ marginTop: 12 }}
          onSubmit={(e) => {
            e.preventDefault();
            guard("report", async () => {
              await reportProblem(problemNote);
              setProblemNote("");
              onError("");
              alert("Diagnostics sent. We will follow up at your account email.");
            });
          }}
        >
          <label>
            Report a problem (optional note — no PDFs)
            <textarea
              rows={3}
              value={problemNote}
              onChange={(e) => setProblemNote(e.target.value)}
              placeholder="What happened? Include roughly when it occurred."
            />
          </label>
          <button className="btn secondary" type="submit" disabled={busy}>
            {working === "report" ? "Sending…" : "Send diagnostics"}
          </button>
        </form>
      )}

      {entitlement.mode === "license" && (
        <form
          className="case-form"
          onSubmit={(e) => {
            e.preventDefault();
            guard("license", async () => {
              await saveLicense(license);
              await onRefresh();
            });
          }}
        >
          <label>
            License key
            <input value={license} onChange={(e) => setLicense(e.target.value)} placeholder="XXXX-XXXX-XXXX" />
          </label>
          <button className="btn" type="submit" disabled={busy}>Save license</button>
        </form>
      )}

      {entitlement.mode === "cloud" && signedIn && (
        <div className="case-form">
          <p className="plan-summary">{PLAN_SUMMARY}</p>
          {billingState === "past_due" && (
            <p className="footnote" style={{ color: "var(--red)", marginTop: 8 }}>
              Your payment is past due. Update your card in Manage billing to restore scans.
            </p>
          )}
          <div className="actions">
            {billingState === "none" && (
              <button
                className="btn gold"
                type="button"
                disabled={busy}
                onClick={() => guard("trial", () => openCheckoutTrial())}
              >
                {working === "trial" ? "Opening checkout…" : "Start 14-day trial"}
              </button>
            )}
            {(billingState === "canceled") && (
              <button
                className="btn gold"
                type="button"
                disabled={busy}
                onClick={() => guard("trial", () => openCheckoutTrial())}
              >
                {working === "trial" ? "Opening checkout…" : "Start 14-day trial"}
              </button>
            )}
            {(billingState === "trialing" || billingState === "active" || billingState === "past_due") && (
              <button
                className="btn secondary"
                type="button"
                disabled={busy || !entitlement.has_billing_portal}
                onClick={() => guard("billing", () => manageBilling())}
              >
                {working === "billing" ? "Opening portal…" : "Manage billing"}
              </button>
            )}
            <button
              className="btn secondary"
              type="button"
              disabled={busy}
              onClick={() => guard("signout", async () => {
                await logoutCloud();
                await onRefresh();
                // Parent clears matter state when hasSession flips false.
              })}
            >
              Sign out
            </button>
          </div>
          <p className="footnote">
            {billingState === "none" || billingState === "canceled"
              ? "Start 14-day trial opens Stripe Checkout (card required). CaseSifter never sees your card number."
              : "Manage billing opens the Stripe Customer Portal. CaseSifter never sees or stores your card number."}
            {" "}
            {isDesktopApp()
              ? "Files stay on this PC; analysis runs remotely and source files are deleted from our servers immediately after each scan."
              : "Your PDFs are processed and deleted after analysis; we don't keep the files. We store an encrypted case index (key facts, short quotes and page references, and text excerpts used to compare documents) so your firm can find contradictions across filings. AI analysis is performed by Google Gemini under paid API terms (no training on your data; limited-period abuse logging). Large files are sent to Google’s Gemini file service for analysis and deleted immediately after; at most 48 hours if deletion fails. Short quotes from proposed findings are also sent to Anthropic’s Claude under its commercial API terms (no training on your data) to double-check findings before you see them. Only signed-in members of your firm can access your index."}
          </p>
        </div>
      )}

      {entitlement.mode === "cloud" && !signedIn && mfaToken && (
        <form
          className="case-form"
          onSubmit={(e) => {
            e.preventDefault();
            guard("mfa", async () => {
              if (!mfaCode || mfaCode.trim().length < 6) {
                throw new Error("Enter the 6-digit authenticator code or a recovery code.");
              }
              await loginMfaCloud(mfaToken, mfaCode.trim());
              setMfaToken("");
              setMfaCode("");
              setPassword("");
              await onRefresh();
            });
          }}
        >
          <h3 className="serif" style={{ marginBottom: 4 }}>Two-factor authentication</h3>
          <p className="footnote" style={{ marginTop: 0 }}>
            Enter the code from your authenticator app, or a one-time recovery code.
          </p>
          <label>
            Authenticator or recovery code
            <input
              type="text"
              value={mfaCode}
              onChange={(e) => setMfaCode(e.target.value)}
              autoComplete="one-time-code"
              inputMode="numeric"
              placeholder="123456"
              required
            />
          </label>
          <div className="actions">
            <button className="btn" type="submit" disabled={busy}>
              {working === "mfa" ? "Verifying…" : "Verify and sign in"}
            </button>
            <button
              className="btn secondary"
              type="button"
              disabled={busy}
              onClick={() => {
                setMfaToken("");
                setMfaCode("");
              }}
            >
              Back
            </button>
          </div>
        </form>
      )}

      {entitlement.mode === "cloud" && !signedIn && !mfaToken && forgotMode && resetToken && (
        <form
          className="case-form"
          onSubmit={(e) => {
            e.preventDefault();
            guard("reset-confirm", async () => {
              const result = await confirmPasswordReset(resetToken, resetPassword);
              if (result?.mfa_required && result?.mfa_token) {
                setMfaToken(result.mfa_token);
                setForgotMode(false);
                setResetToken("");
                setResetPassword("");
                return;
              }
              setForgotMode(false);
              setResetToken("");
              setResetPassword("");
              await onRefresh();
            });
          }}
        >
          <h3 className="serif" style={{ marginBottom: 4 }}>Choose a new password</h3>
          <p className="footnote" style={{ marginTop: 0 }}>
            This replaces your password and signs out other sessions on your account.
          </p>
          <label>
            New password
            <input
              type="password"
              value={resetPassword}
              onChange={(e) => setResetPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="new-password"
            />
          </label>
          <div className="actions">
            <button className="btn" type="submit" disabled={busy}>
              {working === "reset-confirm" ? "Updating…" : "Update password"}
            </button>
            <button
              className="btn secondary"
              type="button"
              disabled={busy}
              onClick={() => {
                setForgotMode(false);
                setResetToken("");
                setResetPassword("");
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {entitlement.mode === "cloud" && !signedIn && !mfaToken && forgotMode && !resetToken && (
        <form
          className="case-form"
          onSubmit={(e) => {
            e.preventDefault();
            guard("reset-request", async () => {
              await requestPasswordReset(email);
              setForgotSent(true);
            });
          }}
        >
          <h3 className="serif" style={{ marginBottom: 4 }}>Forgot password</h3>
          <p className="footnote" style={{ marginTop: 0 }}>
            We email a one-hour reset link if that address has an account. We never reveal whether the email is registered.
          </p>
          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
              placeholder="you@firm.com"
            />
          </label>
          {forgotSent ? (
            <p className="footnote" style={{ color: "var(--teal)" }}>
              If that account exists, a reset link was sent. Check your inbox (and spam).
            </p>
          ) : null}
          <div className="actions">
            <button className="btn" type="submit" disabled={busy}>
              {working === "reset-request" ? "Sending…" : "Send reset link"}
            </button>
            <button
              className="btn secondary"
              type="button"
              disabled={busy}
              onClick={() => {
                setForgotMode(false);
                setForgotSent(false);
              }}
            >
              Back to sign in
            </button>
          </div>
        </form>
      )}

      {entitlement.mode === "cloud" && !signedIn && !mfaToken && !forgotMode && (
        <form
          className="case-form"
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            guard("login", async () => {
              const creds = credentialsFromForm(form);
              if (!creds.email || !creds.email.includes("@")) throw new Error("Enter a valid work email.");
              if (!creds.password || creds.password.length < 8) {
                throw new Error("Password must be at least 8 characters.");
              }
              const result = await loginCloud(creds.email, creds.password);
              if (result?.mfa_required && result?.mfa_token) {
                setMfaToken(result.mfa_token);
                return;
              }
              await onRefresh();
            });
          }}
        >
          <label>
            Name <span className="field-optional">(optional)</span>
            <input
              type="text"
              name="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
              placeholder="Your name"
            />
          </label>
          <label>
            Email
            <input
              type="email"
              name="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
              placeholder={entitlement.email || "you@firm.com"}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              name="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="current-password"
            />
          </label>
          <p className="footnote" style={{ margin: "0 0 8px" }}>
            <button
              type="button"
              className="linkish"
              onClick={() => {
                setForgotMode(true);
                setForgotSent(false);
              }}
            >
              Forgot password?
            </button>
          </p>
          <label>
            {peekSeatInviteToken()
              ? "Firm seat invite (email must match the invite)"
              : <>Invite code <span className="field-optional">(optional)</span></>}
            <input
              type="text"
              name="invite_code"
              value={inviteCode}
              onChange={(e) => setInviteCode(e.target.value)}
              autoComplete="off"
              placeholder="Leave blank unless your firm sent one"
            />
          </label>

          <div className="terms-row">
            <input
              id="account-accepted-terms"
              type="checkbox"
              checked={acceptedTerms}
              onChange={(e) => setAcceptedTerms(e.target.checked)}
            />
            <label htmlFor="account-accepted-terms">
              I agree to the{" "}
              <button
                type="button"
                className="linkish"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setShowTerms(true);
                }}
              >
                Terms of Service and Privacy Policy
              </button>
              .
            </label>
          </div>

          <p className="plan-summary">
            {PLAN_SUMMARY} Sign in if you already have an account, or start a 14-day free trial (card required).
          </p>

          <div className="actions">
            <button className="btn" type="submit" disabled={busy}>
              {working === "login" ? "Signing in…" : "Sign in"}
            </button>
            <button
              className="btn gold"
              type="button"
              disabled={busy}
              onClick={(e) => {
                const form = e.currentTarget.closest("form");
                guard("trial", () => startTrialSignedOut(form));
              }}
            >
              {working === "trial" ? "Opening checkout…" : "Start a 14-day free trial"}
            </button>
            {(authStatus.sso_available || entitlement.sso_available) ? (
              <a className="btn secondary" href={ssoStartUrl()}>
                Sign in with SSO
              </a>
            ) : null}
          </div>
          <p className="footnote">
            {isDesktopApp()
              ? "Files stay on this PC. Analysis runs remotely; source files are deleted from our servers immediately after each scan. Start trial opens Stripe Checkout (card required)."
              : "Your PDFs are processed and deleted after analysis; we don't keep the files. We store an encrypted case index so your firm can find contradictions across filings. AI analysis uses Google Gemini and Anthropic Claude under paid API terms. Start trial opens Stripe Checkout (card required). CaseSifter never sees your card number."}
          </p>
        </form>
      )}

      {entitlement.mode === "open" && (
        <p className="footnote" style={{ marginTop: 12 }}>
          {isDesktopApp()
            ? "Desktop open mode: point CASESIFTER_CONTROL_URL at the control plane and sign in for monthly gating."
            : <>Running in open/local-dev mode. Set <code>ENTITLEMENT_MODE=license</code> or <code>cloud</code> in <code>.env</code> when you are ready to gate monthly access.</>}
        </p>
      )}

      {showTerms && <TermsModal onClose={() => setShowTerms(false)} />}
    </div>
  );
}

function TermsModal({ onClose }) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3 className="serif">Terms of Service & Privacy Policy</h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="modal-body">
          <p className="footnote" style={{ color: "var(--red)" }}>
            Draft for John&apos;s review — not approved legal text. Do not publish as final ToS/Privacy without his sign-off.
          </p>
          <h4>1. The service</h4>
          <p>
            CaseSifter is decision-support software for reviewing case filings and exhibits. It does not provide
            legal advice and does not determine authenticity, fraud, or admissibility. You remain responsible for
            independently verifying any finding before relying on it.
          </p>
          <h4>2. Files and data</h4>
          <p>
            Your PDFs are processed and deleted after analysis; we don&apos;t keep the files. We store an encrypted
            case index (key facts, short quotes and page references, and text excerpts used to compare documents)
            so your firm can find contradictions across filings. AI analysis is performed by Google Gemini under
            paid API terms (no training on your data; limited-period abuse logging). Large files are sent to
            Google&apos;s Gemini file service for analysis and deleted immediately after; at most 48 hours if
            deletion fails. Short quotes from proposed findings are also sent to Anthropic&apos;s Claude under its
            commercial API terms (no training on your data) to double-check findings before you see them. Only
            signed-in members of your firm can access your index. You can delete a document,
            a matter, or your entire account at any time.
            Database backups may retain encrypted copies for a limited window after deletion.
          </p>
          <h4>3. Billing and trials</h4>
          <p>
            Paid plans are <strong>$79 per seat per month</strong>, billed through Stripe; CaseSifter never
            receives or stores your card number. A free trial period requires a valid card on file — unless you
            cancel before the trial ends, it converts automatically into a paid subscription at the then-current
            per-seat price. Manage seats and payment methods in the Stripe Customer Portal from Account → Manage billing.
          </p>
          <h4>4. Cancellation</h4>
          <p>
            You may cancel at any time from Account → Manage billing (Customer Portal) or by emailing{" "}
            <a href="mailto:support@casesifter.com">support@casesifter.com</a>; cancellation takes effect at
            the end of the current billing period. Account closure can purge firm fact memory on request.
          </p>
          <h4>5. Acceptable use</h4>
          <p>
            You agree not to upload material you are not authorized to review, and not to use the service to
            circumvent a court’s rules or another party’s legal rights.
          </p>
          <h4>6. Privacy</h4>
          <p>
            Anonymized finding outcomes (types, severity, confidence) are shared with CaseSifter only if you opt in
            in the Account tab, and never include document content, fact values, or snippets.
          </p>
        </div>
      </div>
    </div>
  );
}

function ProgressPanel({ job, filename }) {
  const percent = job.percent || 0;
  const current = job.stage;
  const pos = job.queue_position;
  const eta = job.eta_seconds;
  const busyRetry = /gemini busy/i.test(String(job.message || ""));
  const etaLabel =
    typeof eta === "number" && eta > 0
      ? eta >= 60
        ? `~${Math.round(eta / 60)} min remaining`
        : `~${eta}s remaining`
      : null;
  return (
    <div className="card">
      <p className="kicker">{busyRetry ? "Gemini busy" : "Analyzing"}</p>
      <h2 className="serif" style={{ marginTop: 4 }}>{filename}</h2>
      <p>{busyRetry ? "Gemini busy, retrying…" : job.message}</p>
      {(typeof pos === "number" || etaLabel) && (
        <p className="footnote" style={{ marginTop: 4 }}>
          {typeof pos === "number" ? `Queue position ${pos}` : ""}
          {typeof pos === "number" && etaLabel ? " · " : ""}
          {etaLabel || ""}
        </p>
      )}
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span>{percent}%</span>
        <span className="footnote">{current}</span>
      </div>
      <ul className="stage-list">
        {STAGES.filter(([id]) => id !== "queued").map(([id, label]) => {
          const ids = STAGES.map((s) => s[0]);
          const here = ids.indexOf(current);
          const mine = ids.indexOf(id);
          const cls = mine < here ? "done" : mine === here ? "on" : "";
          return (
            <li key={id} className={cls}>
              <span>{label}</span>
              <span>{mine < here ? "Done" : mine === here ? "Running" : "Pending"}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

async function previewKind(blob, contentType) {
  let magic = "";
  try {
    const head = new Uint8Array(await blob.slice(0, 5).arrayBuffer());
    magic = String.fromCharCode(...head);
  } catch {
    magic = "";
  }
  if (magic.startsWith("%PDF-")) return "pdf";
  const hint = `${contentType || ""} ${blob?.type || ""}`.toLowerCase();
  if (hint.includes("pdf")) return "pdf";
  if (hint.includes("png") || hint.includes("jpeg") || hint.includes("gif") || hint.includes("image/")) return "image";
  if (hint.includes("text/plain") || hint === "text" || hint.includes(" text")) return "text";
  return "";
}

function ReportViewer({
  job,
  report,
  findings,
  tab,
  setTab,
  docKind,
  caseId,
  documentId,
  documentName,
  documents = [],
  onOpenReport,
  onError,
}) {
  const isCrossDocFact = (item) => Boolean(
    item.category === "case_consistency"
    && item.evidence?.prior_document_id
    && item.evidence?.field
  );
  const isExhibitLabelNoise = (item) => Boolean(
    item.category === "case_consistency"
    && item.evidence?.field === "missing_exhibit"
  );
  const visibleFindings = findings.filter((f) => !isCrossDocFact(f) && !isExhibitLabelNoise(f));
  const material = visibleFindings.filter((f) => f.severity !== "info");
  const caseAlerts = visibleFindings.filter((f) => f.category === "case_consistency");
  const hiddenCrossDoc = findings.length - visibleFindings.length;
  const [localFileUrl, setLocalFileUrl] = useState(null);
  const [sourceKind, setSourceKind] = useState("");
  const [sourceMissing, setSourceMissing] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(true);
  const [locateNote, setLocateNote] = useState("");
  const [locating, setLocating] = useState(false);
  const [exporting, setExporting] = useState("");
  const [showFullSummary, setShowFullSummary] = useState(false);
  const moreMenuRef = useRef(null);
  const blobUrlRef = useRef(null);
  const topScanFindings = (report.top_findings || []).filter(
    (item) => !isCrossDocFact(item) && !isExhibitLabelNoise(item)
  );
  const execFull = reportExecSummaryText(report, documentName);
  const execLead = shortExecLead(execFull);
  const showScanBullets = !unclearedReview(report) && topScanFindings.length > 0;
  const recommendedActions = (report.recommended_actions || []).filter((action) => (
    !reviewDidNotComplete(report)
    || !/no high-confidence manipulation indicators were found/i.test(action)
  ));
  const fileUrl = localFileUrl;
  const isPdf = sourceKind === "pdf";
  const isImage = sourceKind === "image";
  const isText = sourceKind === "text";
  const docSha =
    report?.audit?.sha256 ||
    documents.find((d) => d.id === documentId)?.sha256 ||
    null;

  function rememberBlobUrl(url) {
    if (blobUrlRef.current && blobUrlRef.current !== url) {
      URL.revokeObjectURL(blobUrlRef.current);
    }
    blobUrlRef.current = url;
    setLocalFileUrl(url);
  }

  useEffect(() => () => {
    if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
  }, []);

  useEffect(() => {
    setShowFullSummary(false);
  }, [documentId, job?.job_id]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!docSha) {
        setSourceMissing(true);
        rememberBlobUrl(null);
        return;
      }
      setSourceLoading(true);
      setSourceMissing(false);
      try {
        const { resolveExhibitFile } = await import("./storage/index.js");
        const blob = await resolveExhibitFile(documentId, docSha);
        if (cancelled) return;
        if (!blob) {
          setSourceMissing(true);
          rememberBlobUrl(null);
          return;
        }
        const kind = await previewKind(blob, blob.type);
        if (cancelled) return;
        rememberBlobUrl(URL.createObjectURL(blob));
        setSourceKind(kind);
        setSourceMissing(false);
      } catch {
        if (!cancelled) setSourceMissing(true);
      } finally {
        if (!cancelled) setSourceLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [documentId, docSha]);

  function caseExhibits() {
    const rows = (documents || [])
      .map((doc) => ({
        exhibitId: doc.id,
        sha256: doc.sha256,
        byteLength:
          doc.file_size_bytes
          || (doc.id === documentId ? report?.audit?.file_size_bytes : null),
        name: doc.display_name || doc.original_filename || "",
      }))
      .filter((row) => row.sha256);
    if (
      docSha
      && !rows.some((row) => String(row.sha256).toLowerCase() === String(docSha).toLowerCase())
    ) {
      rows.push({
        exhibitId: documentId,
        sha256: docSha,
        byteLength: report?.audit?.file_size_bytes,
        name: documentName || "",
      });
    }
    return rows;
  }

  async function showResolvedBlob(blob) {
    const kind = await previewKind(blob, blob.type);
    rememberBlobUrl(URL.createObjectURL(blob));
    setSourceKind(kind);
    setSourceMissing(false);
  }

  async function locateFolder() {
    setLocating(true);
    setLocateNote("");
    onError?.("");
    try {
      const { locateCaseFiles, resolveExhibitFile } = await import("./storage/index.js");
      const result = await locateCaseFiles(caseExhibits(), {
        onProgress: ({ scanned, found, total }) => {
          setLocateNote(`Checking files… found ${found} of ${total} (${scanned} scanned).`);
        },
      });
      if (result.cancelled) return;
      setLocateNote(result.summary);
      const blob = await resolveExhibitFile(documentId, docSha);
      if (blob) await showResolvedBlob(blob);
      else setSourceMissing(true);
    } catch (err) {
      onError?.(err.message || "Could not locate files.");
    } finally {
      setLocating(false);
    }
  }

  async function locateThisExhibit() {
    if (!docSha) return;
    setLocating(true);
    onError?.("");
    try {
      const { locateOneExhibit } = await import("./storage/index.js");
      const blob = await locateOneExhibit({
        exhibitId: documentId,
        sha256: docSha,
        byteLength: report?.audit?.file_size_bytes,
      });
      if (!blob) return;
      await showResolvedBlob(blob);
      setLocateNote("");
    } catch (err) {
      onError?.(err.message || "That file does not match this exhibit.");
    } finally {
      setLocating(false);
    }
  }

  return (
    <>
      <div className="card report-chrome">
        <div className="report-header">
          <div>
            <p className="kicker">Authenticity report</p>
            <h2 className="serif">{documentName || report.audit.filename}</h2>
            <span className={`risk ${unclearedReview(report) ? "incomplete" : report.risk_level}`}>
              <ShieldAlert size={14} />
              {reportRiskLabel(report)}
              {unclearedReview(report) ? "" : ` · ${report.risk_score}/100`}
            </span>
            {caseAlerts.length > 0 && (
              <p className="footnote" style={{ marginTop: 8 }}>
                {caseAlerts.length} case-consistency alert{caseAlerts.length === 1 ? "" : "s"} vs prior filings.
              </p>
            )}
            {((report.comparison?.priors_pending_extraction
              || report.comparison?.case_consistency?.priors_pending_extraction
              || 0) > 0) && (
              <div className="ai-review-banner" style={{ marginTop: 8 }}>
                <div>
                  <p>Cross-document comparison incomplete</p>
                  <p className="footnote">
                    {report.comparison?.priors_pending_message
                      || report.comparison?.case_consistency?.priors_pending_message
                      || `${report.comparison?.priors_pending_extraction
                        || report.comparison?.case_consistency?.priors_pending_extraction
                      } earlier documents not yet included in cross-document comparison`}
                    . Comparison will refresh automatically when extraction finishes.
                  </p>
                </div>
              </div>
            )}
            {hiddenCrossDoc > 0 && (
              <p className="footnote" style={{ marginTop: 4 }}>
                Labeled fact mismatches (rate, payment, dates) are listed once on the case page,
                with every file that states them.
              </p>
            )}
            {report.comparison?.scan_profile && (
              <p className="footnote" style={{ marginTop: 4 }}>
                Scan profile: {report.comparison.scan_profile === "court_filing"
                  ? "court filing (content + case compare)"
                  : "exhibit forensic"}
                {report.comparison.doc_kind ? ` · kind ${report.comparison.doc_kind}` : ""}
              </p>
            )}
            {(report.ai_review === "local_only" || report.comparison?.review_mode === "local_only") && (
              <p className="footnote" style={{ marginTop: 4 }}>
                Local-only review. The visual model pass was not run.
              </p>
            )}
            {reviewDidNotComplete(report) && (
              <div className="ai-review-banner">
                <div>
                  <p>Visual model pass did not finish — not a fraud finding</p>
                  <p className="footnote">
                    This file was not cleared as low risk. Gemini did not return a successful visual
                    review (
                    {report.ai_review_reason || report.comparison?.ai_review_reason || "error"}
                    ).
                    {(report.comparison?.ai_review_failed_page_ranges || []).length
                      ? ` Failed page ranges: ${(report.comparison.ai_review_failed_page_ranges || [])
                          .slice(0, 6)
                          .map((r) => `pp. ${r[0]}-${r[1]}`)
                          .join(", ")}${
                          (report.comparison.ai_review_failed_page_ranges || []).length > 6
                            ? "…"
                            : ""
                        }.`
                      : ""}{" "}
                    Re-run AI when the file is cached on this computer (full re-scan from the PDF).
                  </p>
                </div>
                <button
                  className="btn secondary"
                  type="button"
                  disabled={locating}
                  onClick={async () => {
                    if (!caseId || !documentId) {
                      onError?.("No document available for AI re-run.");
                      return;
                    }
                    const doc =
                      documents.find((d) => d.id === documentId) || {
                        id: documentId,
                        display_name: documentName,
                        sha256: docSha,
                        scan_job_id: job?.job_id || report.job_id,
                      };
                    try {
                      const sha = String(doc.sha256 || docSha || "").toLowerCase();
                      if (isHostedAnalysis() && sha) {
                        const cached = await localCacheAdapter.hasFile(doc.id, sha).catch(() => false);
                        if (!cached) {
                          setLocating(true);
                          const { locateOneExhibit } = await import("./storage/index.js");
                          await locateOneExhibit({
                            exhibitId: doc.id,
                            sha256: sha,
                            byteLength: report?.audit?.file_size_bytes,
                          });
                          setLocating(false);
                        }
                      }
                      const result = await scanDocument(caseId, documentId, doc);
                      const newJobId = result?.job_id;
                      if (newJobId) {
                        let progress = null;
                        for (let i = 0; i < 180; i += 1) {
                          progress = await getJob(newJobId, { includeReport: true });
                          if (progress.status === "complete" || progress.status === "failed") break;
                          await new Promise((r) => setTimeout(r, 1000));
                        }
                        if (progress?.status === "failed") {
                          onError?.(progress.error || progress.message || "AI review re-run failed.");
                          return;
                        }
                        if (onOpenReport) {
                          onOpenReport({ ...doc, scan_job_id: newJobId });
                        }
                      }
                    } catch (err) {
                      setLocating(false);
                      const msg = err?.message || "AI review re-run failed.";
                      onError?.(
                        /not cached|Locate|re-upload/i.test(msg)
                          ? msg
                          : `${msg} Locate the file to re-run.`
                      );
                    }
                  }}
                >
                  {sourceMissing ? "Locate file to re-run" : "Re-run AI review"}
                </button>
              </div>
            )}
          </div>
          <div className="report-actions">
            <button
              className="btn"
              type="button"
              disabled={!!exporting}
              onClick={async () => {
                setExporting("pdf");
                try {
                  await downloadExportReport(job.job_id, "pdf");
                } catch (err) {
                  onError?.(err.message || "Export PDF failed.");
                } finally {
                  setExporting("");
                }
              }}
            >
              {exporting === "pdf" ? "Exporting…" : "Export PDF"}
            </button>
            <details className="report-more" ref={moreMenuRef}>
              <summary className="btn secondary report-more-trigger">
                More
                <ChevronDown size={14} aria-hidden />
              </summary>
              <div className="report-more-menu" role="menu">
                <button
                  className="report-more-item"
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setTab("source");
                    moreMenuRef.current?.removeAttribute("open");
                  }}
                >
                  Source
                </button>
                {sourceLoading || sourceMissing ? (
                  <button className="report-more-item" type="button" role="menuitem" disabled>
                    Open in new tab
                  </button>
                ) : fileUrl ? (
                  <a
                    className="report-more-item"
                    href={fileUrl}
                    target="_blank"
                    rel="noreferrer"
                    role="menuitem"
                    onClick={() => moreMenuRef.current?.removeAttribute("open")}
                  >
                    Open in new tab
                  </a>
                ) : (
                  <button
                    className="report-more-item"
                    type="button"
                    role="menuitem"
                    onClick={async () => {
                      moreMenuRef.current?.removeAttribute("open");
                      if (!docSha) {
                        onError?.("No local file linked for this document.");
                        return;
                      }
                      try {
                        const { openLocalDocumentAtPage } = await import("./localFiles.js");
                        await openLocalDocumentAtPage({
                          sha256: docSha,
                          page: 1,
                          displayName: documentName,
                        });
                      } catch (err) {
                        onError?.(err.message || "Could not open local file.");
                      }
                    }}
                  >
                    Open local file
                  </button>
                )}
                {sourceLoading || sourceMissing ? (
                  <button className="report-more-item" type="button" role="menuitem" disabled>
                    Download source
                  </button>
                ) : fileUrl ? (
                  <a
                    className="report-more-item"
                    href={fileUrl}
                    download={documentName || "source.pdf"}
                    role="menuitem"
                    onClick={() => moreMenuRef.current?.removeAttribute("open")}
                  >
                    Download source
                  </a>
                ) : null}
                <button
                  className="report-more-item"
                  type="button"
                  role="menuitem"
                  disabled={!!exporting}
                  onClick={async () => {
                    moreMenuRef.current?.removeAttribute("open");
                    setExporting("html");
                    try {
                      await downloadExportReport(job.job_id, "html");
                    } catch (err) {
                      onError?.(err.message || "Export HTML failed.");
                    } finally {
                      setExporting("");
                    }
                  }}
                >
                  {exporting === "html" ? "Exporting…" : "Export HTML"}
                </button>
                <button
                  className="report-more-item"
                  type="button"
                  role="menuitem"
                  disabled={!!exporting}
                  onClick={async () => {
                    moreMenuRef.current?.removeAttribute("open");
                    setExporting("json");
                    try {
                      await downloadExportReport(job.job_id, "json");
                    } catch (err) {
                      onError?.(err.message || "Export JSON failed.");
                    } finally {
                      setExporting("");
                    }
                  }}
                >
                  {exporting === "json" ? "Exporting…" : "Export JSON"}
                </button>
              </div>
            </details>
          </div>
        </div>

        <div className="report-hero-scan">
          {showScanBullets ? (
            <>
              {showFullSummary ? (
                <p className="report-exec">{execFull}</p>
              ) : (
                <ol className="report-scan-list">
                  {topScanFindings.slice(0, 5).map((item) => {
                    const pageNum = item.page != null ? Math.max(1, Number(item.page) || 1) : null;
                    return (
                      <li key={item.id}>
                        <span className={`sev ${item.severity}`}>{item.severity}</span>
                        <span className="report-scan-title">{item.title}</span>
                        {pageNum != null ? (
                          <span className="footnote">p.{pageNum}</span>
                        ) : null}
                      </li>
                    );
                  })}
                  {topScanFindings.length > 5 ? (
                    <li className="report-scan-more footnote">
                      +{topScanFindings.length - 5} more on Summary / Findings
                    </li>
                  ) : null}
                </ol>
              )}
              {execFull ? (
                <button
                  className="report-summary-toggle"
                  type="button"
                  onClick={() => setShowFullSummary((v) => !v)}
                >
                  {showFullSummary ? "Show top interrupts" : "Show full summary"}
                </button>
              ) : null}
            </>
          ) : execFull.length > 320 ? (
            <>
              <p className="report-exec">
                {showFullSummary ? execFull : (execLead || execFull)}
              </p>
              {execFull !== execLead ? (
                <button
                  className="report-summary-toggle"
                  type="button"
                  onClick={() => setShowFullSummary((v) => !v)}
                >
                  {showFullSummary ? "Show less" : "Show full summary"}
                </button>
              ) : null}
            </>
          ) : (
            <p className="report-exec">{execFull}</p>
          )}
        </div>

        {!sourceLoading && sourceMissing ? (
          <div className="report-source-strip" role="status">
            <span className="footnote">
              Source PDF not on this computer — findings still apply.
            </span>
            <button
              className="btn secondary report-source-locate"
              type="button"
              disabled={locating}
              onClick={() => {
                setTab("source");
                locateThisExhibit();
              }}
            >
              {locating ? "Locating…" : "Locate"}
            </button>
          </div>
        ) : null}
      </div>

      <div className="report-layout">
        <div className="card report-pane">
          <div className="tabs">
            {TABS.map(([id, label]) => (
              <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>
                {label}
              </button>
            ))}
          </div>

          {tab === "source" && (
            <div className="report-source-tab">
              <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
                <p className="kicker" style={{ margin: 0 }}>Source document</p>
                {sourceMissing ? (
                  <span className="footnote">Not on this computer</span>
                ) : fileUrl ? (
                  <a className="footnote" href={fileUrl} target="_blank" rel="noreferrer">Fullscreen</a>
                ) : (
                  <span className="footnote">Link a local file to preview</span>
                )}
              </div>
              {sourceLoading ? (
                <p className="empty-copy">Loading source…</p>
              ) : sourceMissing ? (
                <div className="doc-fallback">
                  <p>
                    Findings for this exhibit are available. The source PDF is not on
                    this computer — CaseSifter does not keep exhibit bytes on the
                    server.
                  </p>
                  <p className="footnote" style={{ marginTop: 8 }}>
                    Locate this file (or a folder of filings) to preview pages and
                    re-run scans. Re-upload from the matter page if the original is
                    elsewhere.
                  </p>
                  <div className="row" style={{ gap: 8, marginTop: 10, justifyContent: "center" }}>
                    <button className="btn gold" type="button" disabled={locating} onClick={locateThisExhibit}>
                      {locating ? "Locating…" : "Locate this exhibit"}
                    </button>
                    <button className="btn secondary" type="button" disabled={locating} onClick={locateFolder}>
                      Locate files in folder
                    </button>
                  </div>
                  {locateNote ? <p className="footnote" style={{ marginTop: 8 }}>{locateNote}</p> : null}
                </div>
              ) : fileUrl ? (
                <DocumentPreview url={fileUrl} isPdf={isPdf} isImage={isImage} isText={isText} name={documentName || ""} />
              ) : (
                <p className="empty-copy">
                  Findings are available. Link a local file with Locate to preview
                  the source on this computer.
                </p>
              )}
            </div>
          )}

          {tab === "summary" && (
            <div className="report-summary">
              <div className="report-summary-findings">
                <h3 className="serif">Top findings</h3>
                {topScanFindings.length === 0 && (
                  reviewDidNotComplete(report)
                    ? <p>Visual review did not complete, so this is not a low-risk result. No fraud finding is recorded from a review that did not finish.</p>
                    : <p>No material findings.</p>
                )}
                {topScanFindings.map((item) => (
                  <FindingCard
                    key={item.id}
                    item={item}
                    docKind={docKind}
                    siblings={visibleFindings}
                    caseId={caseId}
                  />
                ))}
              </div>
              {recommendedActions.length > 0 ? (
                <details className="report-actions-panel">
                  <summary>
                    Recommended actions
                    <span className="footnote">({recommendedActions.length})</span>
                  </summary>
                  <ol>
                    {recommendedActions.map((action) => (
                      <li key={action}>{action}</li>
                    ))}
                  </ol>
                </details>
              ) : null}
              {report.disclaimer ? (
                <p className="disclaimer report-disclaimer">{report.disclaimer}</p>
              ) : null}
            </div>
          )}

          {tab === "findings" && (
            <div>
              <ReportFindingsList
                caseId={caseId}
                docKind={docKind}
                findings={visibleFindings}
                reviewIncomplete={reviewDidNotComplete(report)}
              />
            </div>
          )}

          {tab === "timeline" && (
            (report.timeline || []).length === 0 ? (
              <p className="empty-copy">No timeline events were extracted from this file.</p>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>Event</th>
                    <th>Timestamp</th>
                    <th>Source</th>
                    <th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {(report.timeline || []).map((event, i) => (
                    <tr key={`${event.label}-${i}`}>
                      <td>{event.label}{event.anomaly ? " ⚠" : ""}</td>
                      <td>{event.timestamp || "—"}</td>
                      <td>{event.source}</td>
                      <td>{event.note || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}

          {tab === "comparison" && (
            <>
              {((report.comparison?.priors_pending_extraction
                || report.comparison?.case_consistency?.priors_pending_extraction
                || 0) > 0) && (
                <p className="footnote" style={{ marginBottom: 12 }}>
                  {report.comparison?.priors_pending_message
                    || report.comparison?.case_consistency?.priors_pending_message
                    || "Earlier documents not yet included in cross-document comparison"}
                  .
                </p>
              )}
              <h3 className="serif">Facts in this document</h3>
              {(report.comparison?.facts || []).length === 0 ? (
                <p className="footnote">No labeled loan dates, rates, or amounts were extracted from this file.</p>
              ) : (
                <table className="data">
                  <thead>
                    <tr>
                      <th>Fact</th>
                      <th>This file</th>
                      <th>Quoted as</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.comparison.facts.map((fact, idx) => (
                      <tr key={`${fact.key}-${idx}`}>
                        <td>{fact.label}</td>
                        <td>{fact.display}</td>
                        <td className="footnote">{fact.quote}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {(report.comparison?.fact_matrix || []).length > 0 && (
                <>
                  <h3 className="serif" style={{ marginTop: 20 }}>Compared to other filings</h3>
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Fact</th>
                        <th>This file</th>
                        <th>Other document</th>
                        <th>Match?</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.comparison.fact_matrix.map((row, idx) => (
                        <tr key={`${row.key}-${row.prior_document_id || idx}`}>
                          <td>{row.label}</td>
                          <td>{row.this_display}</td>
                          <td>
                            {row.prior_display}
                            <div className="footnote">{row.prior_name}</div>
                          </td>
                          <td>
                            {row.flag === "red" ? "RED — material" : row.flag === "yellow" ? "YELLOW — verify" : "Match"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
              {(report.comparison?.score_drivers || []).length > 0 && (
                <>
                  <h3 className="serif" style={{ marginTop: 20 }}>Score drivers</h3>
                  <ul>
                    {report.comparison.score_drivers.map((driver, idx) => (
                      <li key={`${driver.type}-${idx}`}>
                        {driver.reason || driver.explanation}
                        {driver.floor != null ? ` (floor: ${driver.floor})` : ""}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              <h3 className="serif" style={{ marginTop: 20 }}>Page dimensions</h3>
              <table className="data">
                <thead>
                  <tr>
                    <th>Page</th><th>W (pt)</th><th>H (pt)</th><th>W (px)</th><th>H (px)</th><th>DPI</th><th>Orientation</th>
                  </tr>
                </thead>
                <tbody>
                  {(report.page_dimensions || []).map((dim) => (
                    <tr key={dim.page}>
                      <td>{dim.page}</td>
                      <td>{fmt(dim.width_pts)}</td>
                      <td>{fmt(dim.height_pts)}</td>
                      <td>{fmt(dim.width_px)}</td>
                      <td>{fmt(dim.height_px)}</td>
                      <td>{fmt(dim.dpi)}</td>
                      <td>{dim.orientation || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3 className="serif" style={{ marginTop: 20 }}>Fonts</h3>
              {(report.fonts || []).length === 0 && <p>No embedded font inventory for this file type.</p>}
              {(report.fonts || []).length > 0 && (
                <table className="data">
                  <thead>
                    <tr><th>Font</th><th>Pages</th><th>Sizes</th><th>Sample</th></tr>
                  </thead>
                  <tbody>
                    {report.fonts.map((font) => (
                      <tr key={font.name}>
                        <td>{font.name}</td>
                        <td>{(font.pages || []).join(", ")}</td>
                        <td>{(font.sizes || []).join(", ")}</td>
                        <td>{font.sample || ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}

          {tab === "audit" && (
            <table className="data">
              <tbody>
                <tr><th>Scanned at</th><td>{report.audit.scanned_at}</td></tr>
                <tr><th>Submitted by</th><td>{report.audit.submitted_by}</td></tr>
                <tr><th>Analysis version</th><td>{report.audit.analysis_version}</td></tr>
                <tr><th>Modules</th><td>{(report.audit.modules_run || []).join(", ")}</td></tr>
                <tr><th>File type</th><td>{report.audit.file_type}</td></tr>
                <tr><th>Size</th><td>{report.audit.file_size_bytes} bytes</td></tr>
                <tr><th>SHA-256</th><td style={{ wordBreak: "break-all" }}>{report.audit.sha256}</td></tr>
                <tr><th>Job ID</th><td>{job.job_id}</td></tr>
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}

function DocumentPreview({ url, isPdf, isImage, isText, name }) {
  if (isPdf) {
    return (
      <iframe
        className="doc-frame"
        title="Source document"
        src={`${url}#view=FitH`}
      />
    );
  }
  if (isImage) {
    return (
      <div className="doc-image-wrap">
        <img className="doc-image" src={url} alt="Source document" />
      </div>
    );
  }
  if (isText) {
    return (
      <iframe className="doc-frame" title="Source document" src={url} />
    );
  }
  return (
    <div className="doc-fallback">
      <p>Inline preview is not available for this file type{name ? ` (${name})` : ""}.</p>
      <a className="btn" href={url} target="_blank" rel="noreferrer">Download / open file</a>
    </div>
  );
}

function resolveTaintedCopy(item, siblings = []) {
  const genericRe = /appears fraudulently edited/i;
  const flaggedRe = /^Flagged on this exhibit:/i;
  const explanation = String(item.explanation || "").trim();
  const quote = String(item.evidence?.evidence_quote || "").trim();
  const needsEnrich = !explanation || genericRe.test(explanation) || flaggedRe.test(quote);
  const taintedTypes = new Set([
    "math_error",
    "historical_impossibility",
    "date_inconsistency",
    "template_fabrication",
    "text_overlay",
    "graphic_manipulation",
  ]);
  if (!needsEnrich) {
    return {
      detail: explanation,
      why: item.why_suspicious,
    };
  }
  const defects = siblings.filter((f) => (
    f.id !== item.id
    && taintedTypes.has(f.evidence?.analysis_type)
    && ["medium", "high", "critical"].includes(f.severity)
  ));
  if (defects.length) {
    const primary = defects[0];
    let detail = String(primary.explanation || primary.why_suspicious || primary.title || "")
      .replace(/^Automated document review reported:\s*/i, "")
      .trim();
    const quoted = String(primary.evidence?.quoted_evidence || "").trim();
    if (quoted && !detail.includes(quoted)) {
      detail = `${detail.replace(/\.$/, "")} Quoted on the page: “${quoted}”.`;
    }
    const extras = defects.slice(1, 3)
      .map((f) => f.title)
      .filter(Boolean);
    if (extras.length) {
      detail = `${detail.replace(/\.$/, "")}. Also flagged: ${extras.join("; ")}.`;
    }
    const why = primary.why_suspicious && primary.why_suspicious !== detail
      ? primary.why_suspicious
      : (item.why_suspicious && !genericRe.test(item.why_suspicious)
        ? item.why_suspicious
        : "A party's own exhibit that looks altered is not reliable proof until each flagged item is reviewed.");
    return { detail, why };
  }
  const detail = (quote && !flaggedRe.test(quote) ? quote : explanation)
    || "See the scan report for each flagged item.";
  return {
    detail,
    why: item.why_suspicious && !genericRe.test(item.why_suspicious)
      ? item.why_suspicious
      : "Open the scan report and review each flagged item before treating this exhibit as proof.",
  };
}

function ReportFindingsList({ caseId, docKind, findings, reviewIncomplete = false }) {
  const [showIgnored, setShowIgnored] = useState(false);
  const [rows, setRows] = useState(findings || []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (caseId) {
        const next = await applyLocalFindingFeedbackToList(caseId, findings || []);
        if (!cancelled) setRows(next);
      } else {
        setRows(findings || []);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [caseId, findings]);

  const ignored = rows.filter((f) => isFindingIgnored(f));
  const visible = rows.filter((f) => showIgnored || !isFindingIgnored(f));
  const materialVisible = visible.filter((f) => f.severity !== "info");

  return (
    <div>
      <p className="footnote">
        {materialVisible.length} material finding(s), {visible.length} total including notes
        {ignored.length > 0 ? (
          <>
            {" · "}
            <button
              type="button"
              className="linkish"
              onClick={() => setShowIgnored((v) => !v)}
            >
              {showIgnored ? "Hide ignored" : `Show ignored (${ignored.length})`}
            </button>
          </>
        ) : null}
      </p>
      {visible.map((item) => (
        <FindingCard
          key={item.id}
          item={item}
          docKind={docKind}
          siblings={rows}
          caseId={caseId}
          onFeedbackChange={(nextItem) => {
            setRows((prev) =>
              prev.map((f) => (f.id === nextItem.id ? { ...f, ...nextItem } : f)),
            );
          }}
        />
      ))}
      {visible.length === 0 && (
        ignored.length > 0 ? (
          <p>All findings are ignored. Use Show ignored to review or undo.</p>
        ) : reviewIncomplete ? (
          <p>
            Visual model pass did not finish — not a fraud finding. This file was not cleared.
            Re-run AI when the file is cached.
          </p>
        ) : (
          <p>The automated modules did not flag this file.</p>
        )
      )}
    </div>
  );
}

function FindingCard({ item, docKind, siblings = [], caseId = null, onFeedbackChange = null }) {
  // Hosted analysis deletes vault artifacts; only show inline/data heatmaps.
  const rawHeat = item.evidence?.heatmap;
  const heat = (() => {
    if (typeof rawHeat !== "string" || !rawHeat) return null;
    if (rawHeat.startsWith("data:")) return rawHeat;
    if (isHostedAnalysis() && /\/api\/jobs\//.test(rawHeat)) return null;
    return rawHeat;
  })();
  const aiLead = Boolean(item.evidence?.ai_generated);
  const caseHit = Boolean(item.evidence?.case_consistency);
  const gotcha = Boolean(item.evidence?.gotcha) || item.category === "gotcha";
  const [telemetryFb, setTelemetryFb] = useState(null);
  const [userFb, setUserFb] = useState(item.user_feedback || null);
  const [fbBusy, setFbBusy] = useState(false);
  const [linkNote, setLinkNote] = useState("");
  const tainted = gotcha && item.evidence?.tainted_support;
  const taintedCopy = tainted ? resolveTaintedCopy(item, siblings) : null;
  const refs = item.evidence?.refs || [];
  const pageNum = item.page != null ? Math.max(1, Number(item.page) || 1) : null;
  const ignored = userFb?.status === "false_positive";
  const confirmed = userFb?.status === "confirmed";
  const showJudgment = Boolean(caseId) && (gotcha || caseHit || item.severity !== "info");

  useEffect(() => {
    setUserFb(item.user_feedback || null);
  }, [item.id, item.user_feedback?.status, item.user_feedback?.fingerprint]);

  async function openRef(ref, pageOverride) {
    setLinkNote("");
    const page = pageOverride ?? ref.page ?? item.page ?? 1;
    try {
      const { openLocalDocumentAtPage } = await import("./localFiles.js");
      await openLocalDocumentAtPage({
        sha256: ref.sha256 || item.evidence?.sha256,
        page,
        displayName: ref.display_name || item.evidence?.display_name,
      });
    } catch (err) {
      setLinkNote(err.message || "Could not open local file.");
    }
  }

  async function openPageCite(e, page) {
    e.preventDefault();
    const ref = refs[0] || {
      sha256: item.evidence?.sha256,
      display_name: item.evidence?.display_name,
      page,
    };
    if (!ref.sha256 && !item.evidence?.sha256) {
      setLinkNote("No local file fingerprint for this page cite.");
      return;
    }
    await openRef({ ...ref, sha256: ref.sha256 || item.evidence?.sha256 }, page);
  }

  async function applyUserFeedback(status) {
    if (!caseId || !item.id) return;
    setFbBusy(true);
    try {
      const result = await setMatterFindingFeedback(caseId, item.id, status, {
        item,
        fingerprint: item.feedback_fingerprint,
      });
      const next = result?.user_feedback || null;
      setUserFb(next);
      onFeedbackChange?.({
        ...item,
        feedback_fingerprint: result?.fingerprint || item.feedback_fingerprint,
        user_feedback: next,
      });
      // Keep anonymized telemetry for case-consistency usefulness (separate from firm judgment).
      if (caseHit && status === "false_positive") {
        await sendFindingFeedback({
          finding_id: item.id,
          useful: false,
          category: item.category,
          field: item.evidence?.field,
          doc_kind: docKind,
        });
      } else if (caseHit && status === "confirmed") {
        await sendFindingFeedback({
          finding_id: item.id,
          useful: true,
          category: item.category,
          field: item.evidence?.field,
          doc_kind: docKind,
        });
      }
    } catch {
      /* keep prior */
    } finally {
      setFbBusy(false);
    }
  }

  return (
    <article className={`finding${gotcha ? " gotcha-card" : ""}${ignored ? " finding-ignored" : ""}`}>
      <header>
        <h3>{item.title}</h3>
        <span className={`sev ${item.severity}`}>
          {ignored ? "Ignored · " : confirmed ? "Confirmed · " : ""}
          {gotcha && item.evidence?.gotcha_level ? `${item.evidence.gotcha_level} · ` : ""}
          {item.severity} · {Math.round(item.confidence)}%
        </span>
      </header>
      <p className="footnote">
        {CATEGORY[item.category] || item.category}
        {pageNum != null ? (
          <>
            {" · "}
            <a
              className="linkish"
              href={`#page=${pageNum}`}
              onClick={(e) => openPageCite(e, pageNum)}
              title={`Open local file at page ${pageNum}`}
            >
              page {pageNum}
            </a>
          </>
        ) : null}
      </p>
      {caseHit && item.evidence?.prior_display_name && (
        <p className="footnote">Prior file: {item.evidence.prior_display_name}</p>
      )}
      {tainted ? (
        <p>{taintedCopy.detail}</p>
      ) : gotcha && item.evidence?.claim_quote ? (
        <>
          <p className="gotcha-quote">
            <strong>Claim:</strong> “{item.evidence.claim_quote}”
            {item.evidence.claim_source ? ` — ${item.evidence.claim_source}` : ""}
          </p>
          <p className="gotcha-quote">
            <strong>{item.evidence.evidence_label || "Evidence"}:</strong>{" "}
            “{item.evidence.evidence_quote}”
            {item.evidence.evidence_source ? ` — ${item.evidence.evidence_source}` : ""}
          </p>
          {item.evidence.self_undercut && (
            <p className="footnote">This party’s own record appears to undercut their claim.</p>
          )}
        </>
      ) : (
        <p>{item.explanation}</p>
      )}
      {refs.length > 0 && (
        <p className="footnote">
          Open local file:{" "}
          {refs.map((ref, i) => {
            const p = ref.page != null ? Math.max(1, Number(ref.page) || 1) : pageNum;
            return (
              <a
                key={`${ref.sha256 || ref.document_id}-${i}`}
                className="linkish"
                href={p != null ? `#page=${p}` : "#"}
                onClick={(e) => {
                  e.preventDefault();
                  openRef(ref, p);
                }}
                style={{ marginRight: 8 }}
                title={p != null ? `Open at page ${p}` : "Open local file"}
              >
                {(ref.display_name || "document").slice(0, 40)}
                {p != null ? ` p.${p}` : ""}
              </a>
            );
          })}
        </p>
      )}
      {linkNote && <p className="footnote" style={{ color: "var(--danger, #b33)" }}>{linkNote}</p>}
      <p className="why">{tainted ? taintedCopy.why : item.why_suspicious}</p>
      {aiLead && (
        <p className="ai-lead footnote">
          AI-assisted lead — verify independently before relying on it.
        </p>
      )}
      {item.recommended_action && <p><strong>Suggested next step:</strong> {item.recommended_action}</p>}
      {showJudgment && (
        <div className="actions finding-feedback-actions" style={{ marginTop: 8 }}>
          {ignored ? (
            <button
              className="btn secondary"
              type="button"
              disabled={fbBusy}
              onClick={() => applyUserFeedback("clear")}
            >
              Undo ignore
            </button>
          ) : (
            <>
              <button
                className="btn secondary"
                type="button"
                disabled={fbBusy}
                title="Hide as false positive for this firm"
                onClick={() => applyUserFeedback("false_positive")}
              >
                Ignore (false positive)
              </button>
              {confirmed ? (
                <button
                  className="btn secondary"
                  type="button"
                  disabled={fbBusy}
                  onClick={() => applyUserFeedback("clear")}
                >
                  Undo confirm
                </button>
              ) : (
                <button
                  className="btn secondary"
                  type="button"
                  disabled={fbBusy}
                  onClick={() => applyUserFeedback("confirmed")}
                >
                  Looks right
                </button>
              )}
            </>
          )}
          {ignored ? (
            <span className="footnote">Ignored for this firm — evidence kept</span>
          ) : confirmed ? (
            <span className="footnote">Confirmed</span>
          ) : null}
        </div>
      )}
      {caseHit && !showJudgment && (
        <div className="actions" style={{ marginTop: 8 }}>
          <span className="footnote">Was this case alert useful?</span>
          <button
            className="btn secondary"
            type="button"
            disabled={telemetryFb !== null}
            onClick={async () => {
              await sendFindingFeedback({
                finding_id: item.id,
                useful: true,
                category: item.category,
                field: item.evidence?.field,
                doc_kind: docKind,
              });
              setTelemetryFb(true);
            }}
          >
            Yes
          </button>
          <button
            className="btn secondary"
            type="button"
            disabled={telemetryFb !== null}
            onClick={async () => {
              await sendFindingFeedback({
                finding_id: item.id,
                useful: false,
                category: item.category,
                field: item.evidence?.field,
                doc_kind: docKind,
              });
              setTelemetryFb(false);
            }}
          >
            No
          </button>
          {telemetryFb !== null && <span className="footnote">Thanks — recorded.</span>}
        </div>
      )}
      <div className="conf"><span style={{ width: `${item.confidence}%` }} /></div>
      {heat && <img className="heatmap" src={heat} alt="Forensic overlay" />}
    </article>
  );
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return Number.isInteger(value) ? value : value.toFixed(1);
  return value;
}
