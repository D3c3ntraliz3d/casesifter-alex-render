import { useEffect, useMemo, useRef, useState } from "react";
import { Archive, FolderOpen, MessageSquare, RefreshCw, RotateCcw, Search, ShieldAlert, Trash2, Upload, X } from "lucide-react";
import {
  archiveCase,
  askCaseChat,
  ANALYSIS_OFFLINE_MESSAGE,
  checkAnalysisHealth,
  createAnalysisJob,
  createCase,
  deleteCase,
  deleteDocument,
  exportUrl,
  getCase,
  getEntitlement,
  getJob,
  isAnalysisUnreachableError,
  isHostedAnalysis,
  isInterruptedOrExpiredJob,
  listCaseChat,
  listCaseGotchas,
  listCases,
  listRetryableJobs,
  absorbSessionHandoff,
  loginCloud,
  logoutCloud,
  openCheckoutUrl,
  reportProblem,
  signupCloud,
  startCheckout,
  openBillingPortal,
  patchDocument,
  pickDesktopDocuments,
  getDesktopReport,
  isDesktopApp,
  rescanCase,
  recomputeMatterFacts,
  retitleCaseDocuments,
  saveLicense,
  scanDocument,
  sendFindingFeedback,
  unarchiveCase,
  uploadCaseDocument,
} from "./api";
import { sha256OfFile } from "./localFiles.js";
import { localCacheAdapter } from "./storage/localCache.js";

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
};

const DOC_KIND_OPTIONS = [
  ["complaint", "Complaint"],
  ["answer", "Answer"],
  ["motion", "Motion"],
  ["order", "Order"],
  ["affidavit", "Affidavit"],
  ["affirmation", "Affirmation"],
  ["proof_of_service", "Affidavit of service"],
  ["summons", "Summons"],
  ["stipulation", "Stipulation"],
  ["transcript", "Transcript"],
  ["letter", "Letter"],
  ["notice", "Notice"],
  ["filing", "Administrative filing"],
  ["rji", "RJI"],
  ["exhibit", "Exhibit (forensic)"],
  ["bill", "Statement / bill (forensic)"],
  ["evidence", "Evidence (forensic)"],
];

const COURT_FILING_KINDS = new Set([
  "complaint", "answer", "motion", "order", "affidavit", "affirmation",
  "letter", "stipulation", "transcript", "summons", "notice", "brief",
  "memorandum", "reply", "opposition", "filing", "proof_of_service",
  "rji", "notice_of_pendency", "calendar",
]);

function profileForKind(kind) {
  if (!kind) {
    return "Needs review — kind not set (not a risk score)";
  }
  return COURT_FILING_KINDS.has((kind || "").toLowerCase()) ? "Court filing" : "Exhibit forensic";
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

/** Collapse duplicate matter rows (same sha, or repeated AOM / RPAPL 1303 labels). */
function dedupeDocumentList(docs) {
  const seen = new Set();
  const out = [];
  for (const doc of docs || []) {
    const sha = String(doc.sha256 || "").toLowerCase();
    const label = `${doc.display_name || ""} ${doc.original_filename || ""}`
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
    const aom1303 =
      /\b(aom|affidavit of mailing|1303|rpapl\s*1303)\b/.test(label)
        ? `aom1303:${label.replace(/\s+/g, " ")}`
        : null;
    const key = sha ? `sha:${sha}` : aom1303 || `id:${doc.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(doc);
  }
  return out;
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
  const [view, setView] = useState("cases"); // cases | case | report | account
  const [cases, setCases] = useState([]);
  const [caseId, setCaseId] = useState(null);
  const [caseData, setCaseData] = useState(null);
  const [selectedDocId, setSelectedDocId] = useState(null);
  const [job, setJob] = useState(null);
  const [tab, setTab] = useState("summary");
  const [error, setError] = useState("");
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const [analysisOffline, setAnalysisOffline] = useState(false);
  const [entitlement, setEntitlement] = useState(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const checkoutTrialStarted = useRef(false);

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
      setEntitlement(await getEntitlement());
    } catch {
      setEntitlement(null);
    }
  }

  useEffect(() => {
    // Marketing signup stores the session on casesifter.com; absorb ?cs_token= handoff here.
    absorbSessionHandoff();
    // Hosted analysis: list/create matters hit casesifter-analysis. Surface a clear
    // ops banner + gate New case — do not leave an empty "No cases yet" state.
    refreshCases().catch((err) => {
      if (markAnalysisOffline(err)) return;
      setError(String(err?.message || "Could not load cases."));
    });
    refreshEntitlement();
  }, []);

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
        setView("account");
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

  async function openCase(id) {
    setError("");
    setCaseId(id);
    setSelectedDocId(null);
    setJob(null);
    setTab("summary");
    const data = await refreshCase(id);
    setCaseData(data);
    setView("case");
  }

  async function openDocumentReport(doc) {
    setSelectedDocId(doc.id);
    setTab("summary");
    if (isDesktopApp()) {
      const report = await getDesktopReport(caseId || caseData?.id, doc.id);
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
      return;
    }
    // Hosted matter memory: prefer persisted report, then live job.
    try {
      const { getMatterDocumentReport } = await import("./api");
      if (doc.has_report || !doc.scan_job_id) {
        try {
          const report = await getMatterDocumentReport(caseId || caseData?.id, doc.id);
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
  }

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
        setView("cases");
        setCaseId(null);
        setCaseData(null);
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
        setView("cases");
        setCaseId(null);
        setCaseData(null);
        setJob(null);
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
        if (caseId === id) {
          setView("cases");
          setCaseId(null);
          setCaseData(null);
        }
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
        if (caseId === id) {
          setView("cases");
          setCaseId(null);
          setCaseData(null);
          setJob(null);
        }
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
      setView("account");
      return false;
    }
    if (!canCreateCase) {
      setError("");
      setView("account");
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
            setView("account");
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
            setView("account");
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
          setView("account");
        }}
      >
        {billingState === "past_due" ? "Past due" : "No active plan"}
      </button>
    );
  }

  return (
    <div className="app">
      <header className="masthead">
        <button type="button" className="brand" onClick={() => { setBusy(false); setView("cases"); setQuery(""); }}>
          <div className="seal">CS</div>
          <div>
            <h1>CaseSifter</h1>
            <p>Find what matters in the record.</p>
          </div>
        </button>
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
        <nav className="mast-nav">
          <button type="button" className={view !== "account" ? "on" : ""} onClick={() => { setBusy(false); setView("cases"); }}>
            Cases
          </button>
          <button
            type="button"
            className={view === "account" ? "on" : ""}
            onClick={() => {
              // Account talks to the control plane; drop leftover case/analysis noise.
              setError("");
              refreshEntitlement();
              setView("account");
            }}
          >
            Account
          </button>
          {planBadge()}
        </nav>
      </header>

      {(() => {
        const bannerText =
          error ||
          (analysisOffline && view !== "account" && !bannerDismissed
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

      {view === "cases" && (
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

      {view === "case" && caseData && (
        <CaseWorkspace
          caseData={caseData}
          busy={busy}
          entitlement={entitlement}
          onBack={() => setView("cases")}
          onRefresh={() => refreshCase(caseData.id)}
          onError={setError}
          onOpenReport={openDocumentReport}
          setBusy={setBusy}
          onArchive={() => handleArchive(caseData)}
          onUnarchive={() => handleUnarchive(caseData)}
          onDelete={() => handleDelete(caseData)}
        />
      )}

      {view === "report" && (
        <>
          <div className="crumb-bar">
            <button className="text-link" type="button" onClick={() => setView("case")}>
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
          <h2 className="serif">Cases</h2>
          <p className="footnote">
            {analysisOffline
              ? "Cases are unavailable until analysis is back online."
              : query.trim()
                ? `${cases.length} match${cases.length === 1 ? "" : "es"}`
                : totalCount === 0
                  ? "Open a case, drop in files, see what stands out."
                  : `${totalCount} open · tap one to continue`}
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
  return (
    <div
      className={`case-tile${selected ? " selected" : ""}`}
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
        <span>{subtitle || "Tap to open"}</span>
      </div>
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

function CaseWorkspace({ caseData, busy, entitlement, onBack, onRefresh, onError, onOpenReport, setBusy, onArchive, onUnarchive, onDelete }) {
  const [drag, setDrag] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState("");
  const [docQuery, setDocQuery] = useState("");
  const [uploadNote, setUploadNote] = useState("");
  const [scanBacklogError, setScanBacklogError] = useState("");
  const [selectedDocs, setSelectedDocs] = useState(() => new Set());
  const [stampById, setStampById] = useState({});
  const [failedBatch, setFailedBatch] = useState([]);
  const [cacheCoverage, setCacheCoverage] = useState(null);
  const inputRef = useRef(null);
  const backfillKeyRef = useRef("");
  const scanningRef = useRef(false);
  const autoRetryKeyRef = useRef("");
  // New array every render would retrigger effects that list this as a dependency.
  const docs = useMemo(
    () => dedupeDocumentList(caseData.documents || []),
    [caseData.documents]
  );

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
  const waitingDocs = docs.filter((d) => d.status === "uploaded" || d.status === "failed");
  const visibleDocs = docs.filter((doc) =>
    matchesQuery(
      [doc.display_name, doc.original_filename, doc.doc_kind, doc.status, doc.risk_level].filter(Boolean).join(" "),
      docQuery
    )
  );

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
        try {
          await scanDocument(caseData.id, doc.id, doc);
          queued += 1;
        } catch (err) {
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

    // Hosted: preflight health, queue every file first, then poll — do not await
    // the full pipeline per file (one blip must not wipe the batch).
    if (isHostedAnalysis()) {
      setBusy(true);
      setFailedBatch([]);
      try {
        const health = await checkAnalysisHealth();
        if (!health.ok) {
          onError(ANALYSIS_OFFLINE_MESSAGE);
          setUploadNote("");
          return;
        }
        const pending = [];
        const queueFailed = [];
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
              continue;
            }
            if (sha) seenSha.add(sha);
            const result = await uploadCaseDocument(caseData.id, valid[i], {
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
            pending.push({
              file: valid[i],
              jobId: result.job_id,
              sha256: result.sha256 || sha,
              name,
            });
          } catch (err) {
            queueFailed.push({ file: valid[i], error: err.message || "upload failed" });
          }
        }
        try {
          await retitleCaseDocuments(caseData.id);
          await onRefresh();
        } catch {
          /* kinds still fixable via toolbar */
        }
        let done = 0;
        let ok = 0;
        const analysisFailed = [];
        const resubmittedSha = new Set();
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
                    matterId: caseData.id,
                    submittedBy: "web-app",
                  });
                  item.jobId = again.job_id;
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
                  matterId: caseData.id,
                  submittedBy: "web-app",
                });
                item.jobId = again.job_id;
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
              setUploadNote(
                `${ok} complete · ${done} done · ${pending.length - done} left · ` +
                  `${item.name}${queueLabel}${etaLabel} — ${stage}`
              );
              await new Promise((r) => setTimeout(r, 1000));
            }
            if (progress?.status === "complete") ok += 1;
            else {
              analysisFailed.push({
                file: item.file,
                error: progress?.error || progress?.message || "analysis failed",
              });
            }
          } catch (err) {
            analysisFailed.push({ file: item.file, error: err.message || "analysis failed" });
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
          ? ` Skipped ${skippedDupes} duplicate file${skippedDupes === 1 ? "" : "s"}.`
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
    setBusy(true);
    setScanBacklogError("");
    onError("");
    try {
      const result = await scanDocument(caseData.id, doc.id, doc);
      setUploadNote(
        result?.queued
          ? `Re-scan queued for ${doc.display_name || doc.original_filename || "file"}.`
          : `Re-scan started for ${doc.display_name || doc.original_filename || "file"}.`
      );
      await onRefresh();
    } catch (err) {
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

  async function rerunAi(doc) {
    // Full re-scan via IndexedDB cache (same path as Re-scan). Locate first if missing.
    setBusy(true);
    setScanBacklogError("");
    onError("");
    try {
      const sha = String(doc?.sha256 || "").toLowerCase();
      if (isHostedAnalysis() && sha) {
        const cached = await localCacheAdapter.hasFile(doc.id, sha).catch(() => false);
        if (!cached) {
          const { locateOneExhibit } = await import("./storage/index.js");
          await locateOneExhibit({
            exhibitId: doc.id,
            sha256: sha,
            byteLength: doc.source_size,
          });
        }
      }
      const result = await scanDocument(caseData.id, doc.id, doc);
      setUploadNote(
        result?.queued
          ? `AI review re-run queued for ${doc.display_name || doc.original_filename || "file"}.`
          : `AI review re-run started for ${doc.display_name || doc.original_filename || "file"}.`
      );
      await onRefresh();
    } catch (err) {
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
    setBusy(true);
    scanningRef.current = true;
    setScanBacklogError("");
    try {
      const result = await rescanCase(caseData.id, { waitingOnly: false });
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
      onError(err.message);
      setScanBacklogError(err.message);
    } finally {
      scanningRef.current = false;
      setBusy(false);
    }
  }

  const scanned = docs.filter((d) => d.status === "complete" && d.risk_level);
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
        <h2 className="serif">{caseData.caption}</h2>
        <p className="meta-line">
          {partiesFromCaption(caseData.caption).join(" · ") || "Parties from caption"}
          <span className="dot" />
          {caseData.index_number || "No index"}
          <span className="dot" />
          {caseData.court || "Court not set"}
        </p>
        {uploadNote && <p className="footnote">{uploadNote}</p>}
        {cacheCoverage && cacheCoverage.total > 0 && (
          <div className={`banner scan-backlog${cacheCoverage.missing.length ? " warn" : ""}`}>
            <div>
              <strong>
                Local cache: {cacheCoverage.cached} of {cacheCoverage.total} unique file
                {cacheCoverage.total === 1 ? "" : "s"}
              </strong>
              {cacheCoverage.missing.length ? (
                <p className="footnote" style={{ margin: "4px 0 0" }}>
                  {cacheCoverage.missing.length} of {cacheCoverage.total} not on this
                  computer (showing first {Math.min(12, cacheCoverage.missing.length)}):{" "}
                  {cacheCoverage.missing.slice(0, 12).join(", ")}
                  {cacheCoverage.missing.length > 12
                    ? ` +${cacheCoverage.missing.length - 12} more`
                    : ""}
                </p>
              ) : (
                <p className="footnote" style={{ margin: "4px 0 0" }}>
                  All matter PDFs are cached on this computer for re-scan.
                </p>
              )}
            </div>
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
        {caseData.archived_at ? (
          <p className="footnote">This matter is archived. Restore it to add filings.</p>
        ) : (
        <div
          className={`dropzone compact ${drag ? "active" : ""}`}
          onDragOver={(e) => { e.preventDefault(); if (!isDesktopApp()) setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            if (!isDesktopApp()) onUpload(e.dataTransfer.files);
          }}
          onClick={() => {
            if (isDesktopApp()) onUpload(null);
            else inputRef.current?.click();
          }}
        >
          <Upload size={18} />
          <div>
            <strong>Add filings</strong>
            <p>
              {isDesktopApp()
                ? "Files stay on this PC. Click to pick filings — analysis starts automatically."
                : "Drop files here or click to upload. Pleadings get content review; exhibits get forensic review."}
            </p>
          </div>
          {!isDesktopApp() && (
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
        )}
      </div>

      <CaseRiskSummary
        byRisk={byRisk}
        pendingCount={docs.filter((d) => d.status !== "complete").length}
        onOpenReport={onOpenReport}
      />

      <CaseGotchaBoard
        caseId={caseData.id}
        documents={docs}
        onOpenReport={onOpenReport}
        onRisksElevated={onRefresh}
      />

      <CaseChat
        caseId={caseData.id}
        documents={docs}
        onOpenReport={onOpenReport}
        onError={onError}
        onScanDocument={async (doc) => {
          await scanDocument(caseData.id, doc.id, doc);
        }}
        onRescanWaiting={async () => {
          await scanWaitingNow();
        }}
        onRefresh={onRefresh}
      />

      <div className="card">
        <div className="toolbar">
          <div>
            <p className="kicker">Documents</p>
            <h2 className="serif" style={{ margin: "4px 0 0" }}>
              {docs.length} file{docs.length === 1 ? "" : "s"}
            </h2>
          </div>
          <SearchField
            value={docQuery}
            onChange={setDocQuery}
            placeholder="Filter by name, kind, or risk…"
          />
          {!caseData.archived_at && (
          <>
          <button
            className="btn secondary"
            type="button"
            disabled={busy || docs.length === 0}
            title="Set Exhibit kind from EXHIBIT(S) / EXHIBIT_S filenames"
            onClick={async () => {
              setBusy(true);
              try {
                const result = await retitleCaseDocuments(caseData.id);
                setUploadNote(
                  result?.changed
                    ? `Updated ${result.changed} name/kind${result.changed === 1 ? "" : "s"} from filenames.`
                    : "No filename retitles needed."
                );
                await onRefresh();
              } catch (err) {
                onError(err.message || "Retitle failed.");
              } finally {
                setBusy(false);
              }
            }}
          >
            Fix kinds from names
          </button>
          <button className="btn secondary" type="button" disabled={busy} onClick={() => inputRef.current?.click()}>
            Upload
          </button>
          </>
          )}
        </div>
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
                checked={visibleDocs.length > 0 && visibleDocs.every((d) => selectedDocs.has(d.id))}
                disabled={busy}
                onChange={() => {
                  const all = visibleDocs.every((d) => selectedDocs.has(d.id));
                  if (all) {
                    setSelectedDocs((prev) => {
                      const next = new Set(prev);
                      visibleDocs.forEach((d) => next.delete(d.id));
                      return next;
                    });
                  } else {
                    setSelectedDocs((prev) => {
                      const next = new Set(prev);
                      visibleDocs.forEach((d) => next.add(d.id));
                      return next;
                    });
                  }
                }}
              />
              Select all
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
        <table className="data docs">
          <colgroup>
            <col className="col-check" />
            <col className="col-name" />
            <col className="col-kind" />
            <col className="col-meta" />
            <col className="col-actions" />
          </colgroup>
          <thead>
            <tr>
              <th className="check-col" aria-label="Select" />
              <th>Name</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleDocs.map((doc) => {
              const title = docListTitle(doc);
              const fullName = doc.original_filename || doc.display_name || "";
              const scannedAt = doc.last_scanned_at || stampById[doc.id] || null;
              return (
              <tr key={doc.id} className={selectedDocs.has(doc.id) ? "selected" : undefined}>
                <td className="check-col">
                  <input
                    type="checkbox"
                    checked={selectedDocs.has(doc.id)}
                    disabled={busy || Boolean(caseData.archived_at)}
                    onChange={() => toggleDocSelect(doc.id)}
                    aria-label={`Select ${title}`}
                  />
                </td>
                <td className="doc-name-cell">
                  {editingId === doc.id ? (
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
                  {scannedAt ? (
                    <div className="doc-scanned" title={new Date(scannedAt).toLocaleString()}>
                      Scanned {formatScanAt(scannedAt)}
                    </div>
                  ) : doc.status === "complete" ? (
                    <div className="doc-scanned">Scanned</div>
                  ) : null}
                </td>
                <td className="doc-kind-cell">
                  <select
                    value={doc.doc_kind || ""}
                    disabled={busy}
                    aria-label="Document kind"
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
                    <option value="">Unknown — needs review</option>
                    {DOC_KIND_OPTIONS.map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                  <div className="footnote">{profileForKind(doc.doc_kind)}</div>
                </td>
                <td className="meta-cell">
                  <div className="doc-status">{doc.status}</div>
                  <div className="doc-risk">
                    {doc.risk_level ? (RISK[doc.risk_level] || doc.risk_level) : "—"}
                  </div>
                  <label className="footnote" style={{ display: "block", marginTop: 4 }}>
                    Filed{" "}
                    <span className="filed-value">{doc.filed_date || "—"}</span>{" "}
                    <input
                      type="date"
                      value={doc.filed_date || ""}
                      disabled={busy || Boolean(caseData.archived_at)}
                      aria-label={`Filed date for ${title}`}
                      onChange={async (e) => {
                        try {
                          await patchDocument(caseData.id, doc.id, {
                            filed_date: e.target.value || null,
                          });
                          await onRefresh();
                        } catch (err) {
                          onError(err.message);
                        }
                      }}
                      style={{ maxWidth: "11rem" }}
                    />
                  </label>
                  {doc.last_error ? (
                    <div className="footnote" style={{ color: "var(--red)" }}>{doc.last_error}</div>
                  ) : null}
                  {doc.ai_review === "incomplete" ? (
                    <div className="ai-review-badge" title={doc.ai_review_reason || "AI review incomplete"}>
                      AI review incomplete — re-run
                    </div>
                  ) : null}
                </td>
                <td className="row-actions">
                  <div className="doc-actions">
                    {doc.status === "complete" && (
                      <button className="btn gold" type="button" onClick={() => onOpenReport(doc)}>
                        Report
                      </button>
                    )}
                    {doc.status === "complete" && doc.ai_review === "incomplete" && (
                      <button className="btn secondary" type="button" disabled={busy} onClick={() => rerunAi(doc)}>
                        {docNeedsLocate(doc) ? "Locate file to re-run" : "Re-run AI review"}
                      </button>
                    )}
                    {(doc.status === "failed" || doc.status === "uploaded" || doc.status === "complete") && (
                      <button className="btn secondary" type="button" disabled={busy} onClick={() => rescan(doc)}>
                        {doc.status === "complete" ? "Re-scan" : "Scan"}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
        </>
        )}
      </div>
    </>
  );
}

function relatedDocs(item, documents) {
  const fromApi = item.related_documents || [];
  if (fromApi.length) {
    return fromApi
      .map((ref) => documents.find((d) => d.id === ref.document_id) || ref)
      .filter((doc) => doc?.id || doc?.document_id);
  }
  const fallback = documents.find((d) => d.id === item.document_id);
  return fallback ? [fallback] : [];
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

function CaseGotchaBoard({ caseId, documents, onOpenReport, onRisksElevated }) {
  const [gotchas, setGotchas] = useState([]);
  const [factConflicts, setFactConflicts] = useState([]);
  const [caseImpact, setCaseImpact] = useState(null);
  const [showNoise, setShowNoise] = useState(false);
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
    listCaseGotchas(caseId)
      .then((data) => {
        if (cancelled) return;
        setGotchas(data.gotchas || []);
        setFactConflicts(data.factConflicts || []);
        setCaseImpact(data.caseImpact || null);
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
      });
    return () => {
      cancelled = true;
    };
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
  const total = uniqueGotchas.length + uniqueConflicts.length;
  const impact = caseImpact || {};
  // Count must match the filtered cards actually rendered (not a stale undeduped array).
  const materialCount = total;
  const noiseCount = impact.noise_collapsed ?? impact.noise_count ?? 0;
  const fraudCount = impact.fraud_count ?? 0;

  if (total === 0 && !materialCount && !noiseCount) return null;

  return (
    <div className="card gotcha-board">
      <p className="kicker">Case impact</p>
      <h2 className="serif" style={{ margin: "4px 0 8px" }}>
        {materialCount} material interrupt{materialCount === 1 ? "" : "s"}
        {fraudCount ? ` · ${fraudCount} fraud exhibit${fraudCount === 1 ? "" : "s"}` : ""}
      </h2>
      <div className="case-impact-strip">
        <span className="case-impact-pill material">{materialCount} material</span>
        {fraudCount > 0 && (
          <span className="case-impact-pill fraud">{fraudCount} altered exhibit</span>
        )}
        {noiseCount > 0 && (
          <button
            type="button"
            className="case-impact-pill noise"
            onClick={() => setShowNoise((v) => !v)}
          >
            {noiseCount} noise collapsed
          </button>
        )}
      </div>
      <p className="footnote" style={{ marginBottom: 8 }}>
        Fraud exhibits and verified AGAINST pairs first. Ordinary disputes and token
        collisions stay collapsed. Confirm quoted language before relying on any card.
      </p>
      {showNoise && noiseCount > 0 && (
        <p className="footnote case-impact-noise-note">
          {noiseCount} finding{noiseCount === 1 ? "" : "s"} demoted as noise (boilerplate
          overlap, he-said-she-said, or no case-impact bucket).
        </p>
      )}
      {uniqueConflicts.map((item) => (
        <div key={item.id || item.field} className="gotcha-item">
          <h3>{item.title}</h3>
          <p className="footnote">{item.gotcha_level || "Case fact conflict"}</p>
          {(item.variants || []).map((variant) => (
            <p key={variant.display} className="gotcha-quote">
              <strong>{variant.display}:</strong>
              {variant.quote ? ` “${variant.quote}”` : ""}
              {" — "}
              {(variant.documents || []).map((doc) => doc.display_name).join("; ")}
            </p>
          ))}
          {item.attorney_why && <p className="gotcha-why">{item.attorney_why}</p>}
          <div className="actions" style={{ marginTop: 8 }}>
            {relatedDocs(item, documents).map((doc) => {
              const id = doc.id || doc.document_id;
              const live = documents.find((d) => d.id === id);
              if (!live) return null;
              return (
                <button key={id} className="btn gold" type="button" onClick={() => onOpenReport(live)}>
                  Open {live.display_name || live.original_filename}
                </button>
              );
            })}
          </div>
        </div>
      ))}
      {uniqueGotchas.map((item) => {
        const sources = relatedDocs(item, documents);
        const tainted = item.tainted_support || item.evidence?.tainted_support;
        return (
          <div
            key={`${item.id || item.title}-${item.document_id}`}
            className={`gotcha-item${tainted ? " gotcha-fraud" : ""}`}
          >
            <h3>{item.title}</h3>
            {(item.case_impact_bucket || item.contradiction_type || item.gotcha_level) && (
              <p className="footnote">
                {item.case_impact_bucket
                  ? `Case impact · ${item.case_impact_bucket}`
                  : item.gotcha_level || item.contradiction_type}
              </p>
            )}
            {tainted ? (
              <>
                {item.claim_quote && (
                  <p className="gotcha-quote">
                    <strong>Claim:</strong> “{item.claim_quote}”
                    {item.claim_source ? ` — ${item.claim_source}` : ""}
                  </p>
                )}
                <p className="gotcha-quote">
                  {item.evidence_quote || "See the scan report for each flagged item."}
                </p>
                {(item.why_it_may_matter || item.attorney_why) && (
                  <p className="gotcha-why">{item.why_it_may_matter || item.attorney_why}</p>
                )}
              </>
            ) : (
              <>
                {item.claim_quote && (
                  <p className="gotcha-quote">
                    <strong>Claim:</strong> “{item.claim_quote}”
                    {item.claim_source ? ` — ${item.claim_source}` : ""}
                  </p>
                )}
                {item.evidence_quote && (
                  <p className="gotcha-quote">
                    <strong>{item.evidence_label || (item.evidence_tier === 2 ? "Opposing assertion" : "Evidence")}:</strong>{" "}
                    “{item.evidence_quote}”
                    {item.evidence_source ? ` — ${item.evidence_source}` : ""}
                  </p>
                )}
                {item.self_undercut ? (
                  <p className="footnote">This party’s own record appears to undercut their claim.</p>
                ) : null}
                {(item.attorney_why || item.materiality_reason || item.fact_named) && (
                  <p className="gotcha-why">
                    {item.attorney_why
                      || [item.fact_named && `Same fact: ${item.fact_named}.`, item.materiality_reason]
                          .filter(Boolean)
                          .join(" ")}
                  </p>
                )}
              </>
            )}
            <div className="actions" style={{ marginTop: 8 }}>
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
            </div>
          </div>
        );
      })}
    </div>
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
    <div className={`card case-chat${focusDocument ? " on-report" : ""}`}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <p className="kicker">Case clerk</p>
          <h2 className="serif" style={{ margin: "4px 0 8px" }}>
            {focusDocument ? "Ask about this document" : "Find things in this case"}
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

function CaseRiskSummary({ byRisk, pendingCount, onOpenReport }) {
  const groups = [
    { key: "red", label: "High risk", docs: byRisk.red },
    { key: "yellow", label: "Moderate risk", docs: byRisk.yellow },
    { key: "green", label: "Low risk", docs: byRisk.green },
  ];

  return (
    <div className="card">
      <p className="kicker">Triage</p>
      <h2 className="serif" style={{ margin: "4px 0 8px" }}>Reports by risk</h2>
      <p className="footnote" style={{ marginBottom: 14 }}>
        Open a group to jump straight to a document report.
        {pendingCount > 0 ? ` ${pendingCount} file(s) still scanning or not yet complete.` : ""}
      </p>
      <div className="risk-summary">
        {groups.map((group) => (
          <details key={group.key} className={`risk-drop risk-${group.key}`} open={group.key === "red" && group.docs.length > 0}>
            <summary>
              <span className={`risk-chip ${group.key}`}>{group.label}</span>
              <span className="risk-count">{group.docs.length}</span>
            </summary>
            {group.docs.length === 0 ? (
              <p className="footnote" style={{ margin: "8px 0 0" }}>No reports in this band yet.</p>
            ) : (
              <ul className="risk-links">
                {group.docs.map((doc) => (
                  <li key={doc.id}>
                    <button type="button" className="linkish" onClick={() => onOpenReport(doc)}>
                      {doc.display_name || doc.original_filename}
                    </button>
                    <span className="footnote">
                      {doc.doc_kind || "untyped"}
                      {doc.risk_score != null ? ` · score ${doc.risk_score}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </details>
        ))}
      </div>
    </div>
  );
}

const PLAN_SUMMARY = (
  <>
    <strong>$79/seat/month</strong>. Month-to-month, cancel anytime. A 14-day trial collects a card up front and converts to paid after 14 days unless you cancel.
  </>
);

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

  useEffect(() => {
    if (entitlement?.email && !email) setEmail(entitlement.email);
  }, [entitlement?.email]);

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

  async function startTrialSignedOut() {
    if (!email || !email.includes("@")) throw new Error("Enter a valid work email.");
    if (!password || password.length < 6) throw new Error("Password must be at least 6 characters.");
    if (!acceptedTerms) {
      throw new Error("Agree to the Terms of Service and Privacy Policy to continue.");
    }
    try {
      await signupCloud(email, password, name.trim() || null, true, inviteCode.trim() || null);
    } catch (err) {
      const msg = String(err?.message || "");
      if (/already exists/i.test(msg)) {
        try {
          await loginCloud(email, password);
        } catch {
          throw new Error(
            "That email already has an account, but the password did not match. Sign in with the correct password, then start your trial.",
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

      {entitlement.mode === "cloud" && !signedIn && (
        <form
          className="case-form"
          onSubmit={(e) => {
            e.preventDefault();
            guard("login", async () => {
              if (!email || !email.includes("@")) throw new Error("Enter a valid work email.");
              if (!password || password.length < 6) throw new Error("Password must be at least 6 characters.");
              await loginCloud(email, password);
              await onRefresh();
            });
          }}
        >
          <label>
            Name <span className="field-optional">(optional)</span>
            <input
              type="text"
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
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={6}
              autoComplete="current-password"
            />
          </label>
          <label>
            Invite code <span className="field-optional">(optional)</span>
            <input
              type="text"
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
              onClick={() => guard("trial", () => startTrialSignedOut())}
            >
              {working === "trial" ? "Opening checkout…" : "Start a 14-day free trial"}
            </button>
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
  const [showDoc, setShowDoc] = useState(true);
  const [localFileUrl, setLocalFileUrl] = useState(null);
  const [sourceKind, setSourceKind] = useState("");
  const [sourceMissing, setSourceMissing] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(true);
  const [locateNote, setLocateNote] = useState("");
  const [locating, setLocating] = useState(false);
  const blobUrlRef = useRef(null);
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
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <p className="kicker">Authenticity report</p>
            <h2 className="serif" style={{ margin: "4px 0 8px" }}>{documentName || report.audit.filename}</h2>
            <span className={`risk ${report.risk_level}`}>
              <ShieldAlert size={16} />
              {RISK[report.risk_level]} · {report.risk_score}/100
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
            {(report.ai_review === "incomplete" || report.comparison?.ai_review === "incomplete") && (
              <div className="ai-review-banner">
                <div>
                  <p>AI review incomplete — re-run</p>
                  <p className="footnote">
                    Gemini did not finish (
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
                    Re-uploads the file from this computer’s cache and runs the full scan again.
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
          <div className="actions">
            <button
              className={`btn ${showDoc ? "gold" : "secondary"}`}
              type="button"
              onClick={() => setShowDoc((v) => !v)}
            >
              {showDoc ? "Hide document" : "View document"}
            </button>
            {sourceLoading || sourceMissing ? (
              <button className="btn secondary" type="button" disabled>
                Open in new tab
              </button>
            ) : fileUrl ? (
              <a className="btn secondary" href={fileUrl} target="_blank" rel="noreferrer">
                Open in new tab
              </a>
            ) : (
              <button
                className="btn secondary"
                type="button"
                onClick={async () => {
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
              <button className="btn secondary" type="button" disabled>
                Download
              </button>
            ) : fileUrl ? (
              <a className="btn secondary" href={fileUrl} download={documentName || "source.pdf"}>
                Download
              </a>
            ) : null}
            <a className="btn gold" href={exportUrl(job.job_id, "html")}>Export HTML</a>
            <a className="btn" href={exportUrl(job.job_id, "pdf")}>Export PDF</a>
            <a className="btn secondary" href={exportUrl(job.job_id, "json")}>Export JSON</a>
          </div>
        </div>
        <p style={{ marginTop: 16 }}>{report.executive_summary}</p>
      </div>

      {caseId && onOpenReport && (
        <CaseChat
          caseId={caseId}
          documents={documents}
          focusDocument={documents.find((doc) => doc.id === documentId) || {
            id: documentId,
            display_name: documentName,
          }}
          onOpenReport={onOpenReport}
          onError={onError || (() => {})}
          onScanDocument={async (doc) => {
            await scanDocument(caseId, doc.id, doc);
          }}
          onRescanWaiting={async () => {
            await rescanCase(caseId, { waitingOnly: true });
          }}
        />
      )}

      <div className={`report-layout ${showDoc ? "with-doc" : ""}`}>
        {showDoc && (
          <div className="card doc-pane">
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
              <p className="kicker" style={{ margin: 0 }}>Source document</p>
              {sourceMissing ? (
                <span className="footnote">Source unavailable</span>
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
                <p>This exhibit is stored on the computer it was uploaded from.</p>
                <div className="row" style={{ gap: 8, marginTop: 10 }}>
                  <button className="btn gold" type="button" disabled={locating} onClick={locateFolder}>
                    Locate files
                  </button>
                  <button className="btn secondary" type="button" disabled={locating} onClick={locateThisExhibit}>
                    Locate this exhibit
                  </button>
                </div>
                {locateNote ? <p className="footnote" style={{ marginTop: 8 }}>{locateNote}</p> : null}
              </div>
            ) : fileUrl ? (
              <DocumentPreview url={fileUrl} isPdf={isPdf} isImage={isImage} isText={isText} name={documentName || ""} />
            ) : (
              <p className="empty-copy">
                This exhibit is stored on the computer it was uploaded from.
              </p>
            )}
          </div>
        )}

        <div className="card report-pane">
          <div className="tabs">
            {TABS.map(([id, label]) => (
              <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>
                {label}
              </button>
            ))}
          </div>

          {tab === "summary" && (
            <div className="grid-2">
              <div>
                <h3 className="serif">Top findings</h3>
                {(report.top_findings || []).filter((item) => !isCrossDocFact(item) && !isExhibitLabelNoise(item)).length === 0 && <p>No material findings.</p>}
                {(report.top_findings || []).filter((item) => !isCrossDocFact(item) && !isExhibitLabelNoise(item)).map((item) => (
                  <FindingCard key={item.id} item={item} docKind={docKind} siblings={visibleFindings} />
                ))}
              </div>
              <div>
                <h3 className="serif">Recommended actions</h3>
                <ol>
                  {(report.recommended_actions || []).map((action) => (
                    <li key={action}>{action}</li>
                  ))}
                </ol>
                <p className="disclaimer">{report.disclaimer}</p>
              </div>
            </div>
          )}

          {tab === "findings" && (
            <div>
              <p className="footnote">{material.length} material finding(s), {visibleFindings.length} total including notes.</p>
              {visibleFindings.map((item) => (
                <FindingCard key={item.id} item={item} docKind={docKind} siblings={visibleFindings} />
              ))}
              {visibleFindings.length === 0 && <p>The automated modules did not flag this file.</p>}
            </div>
          )}

          {tab === "timeline" && (
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

function FindingCard({ item, docKind, siblings = [] }) {
  const heat = item.evidence?.heatmap;
  const aiLead = Boolean(item.evidence?.ai_generated);
  const caseHit = Boolean(item.evidence?.case_consistency);
  const gotcha = Boolean(item.evidence?.gotcha) || item.category === "gotcha";
  const [feedback, setFeedback] = useState(null);
  const [linkNote, setLinkNote] = useState("");
  const tainted = gotcha && item.evidence?.tainted_support;
  const taintedCopy = tainted ? resolveTaintedCopy(item, siblings) : null;
  const refs = item.evidence?.refs || [];
  const pageNum = item.page != null ? Math.max(1, Number(item.page) || 1) : null;

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

  return (
    <article className={`finding${gotcha ? " gotcha-card" : ""}`}>
      <header>
        <h3>{item.title}</h3>
        <span className={`sev ${item.severity}`}>
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
        <p className="ai-lead">
          <strong>AI-Assisted Research Lead:</strong> This finding is based on an automated
          model review, not definitive evidence. Verify independently before relying on it.
        </p>
      )}
      {item.recommended_action && <p><strong>Suggested next step:</strong> {item.recommended_action}</p>}
      {caseHit && (
        <div className="actions" style={{ marginTop: 8 }}>
          <span className="footnote">Was this case alert useful?</span>
          <button
            className="btn secondary"
            type="button"
            disabled={feedback !== null}
            onClick={async () => {
              await sendFindingFeedback({
                finding_id: item.id,
                useful: true,
                category: item.category,
                field: item.evidence?.field,
                doc_kind: docKind,
              });
              setFeedback(true);
            }}
          >
            Yes
          </button>
          <button
            className="btn secondary"
            type="button"
            disabled={feedback !== null}
            onClick={async () => {
              await sendFindingFeedback({
                finding_id: item.id,
                useful: false,
                category: item.category,
                field: item.evidence?.field,
                doc_kind: docKind,
              });
              setFeedback(false);
            }}
          >
            No
          </button>
          {feedback !== null && <span className="footnote">Thanks — recorded.</span>}
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
