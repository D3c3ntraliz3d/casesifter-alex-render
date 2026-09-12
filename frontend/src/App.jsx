import { useEffect, useMemo, useRef, useState } from "react";
import { Archive, FolderOpen, MessageSquare, RotateCcw, Search, ShieldAlert, Trash2, Upload, X } from "lucide-react";
import {
  archiveCase,
  askCaseChat,
  createCase,
  deleteCase,
  documentFileUrl,
  exportUrl,
  getCase,
  getEntitlement,
  getJob,
  jobSourceUrl,
  listCaseChat,
  listCaseGotchas,
  listCases,
  loginCloud,
  logoutCloud,
  patchDocument,
  saveLicense,
  scanDocument,
  sendFindingFeedback,
  setShareOutcomes,
  unarchiveCase,
  uploadCaseDocument,
} from "./api";

const STAGES = [
  ["queued", "Queued"],
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
  if (!kind) return "needs review — kind not set (not a risk score)";
  return COURT_FILING_KINDS.has((kind || "").toLowerCase()) ? "court filing" : "exhibit forensic";
}

const ACCEPTED = new Set([".pdf", ".docx", ".xlsx", ".txt", ".jpg", ".jpeg", ".png", ".gif"]);

function extensionOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
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
  const [entitlement, setEntitlement] = useState(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");

  async function refreshCases() {
    const rows = await listCases();
    setCases(rows);
  }

  async function refreshCase(id = caseId) {
    if (!id) return null;
    const data = await getCase(id);
    setCaseData(data);
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
    refreshCases().catch((err) => setError(err.message));
    refreshEntitlement();
  }, []);

  useEffect(() => {
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

  return (
    <div className="app">
      <header className="masthead">
        <button type="button" className="brand" onClick={() => { setBusy(false); setView("cases"); setQuery(""); }}>
          <div className="seal">LS</div>
          <div>
            <h1>LawSifter</h1>
            <p>Sift the record. Flag what matters.</p>
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
          <button type="button" className={view === "account" ? "on" : ""} onClick={() => { refreshEntitlement(); setView("account"); }}>
            Account
          </button>
          <span className={`status-dot ${entitlement?.active ? "live" : ""}`}>
            {entitlement?.active ? (entitlement.plan || "Active") : "Offline"}
          </span>
        </nav>
      </header>

      {error && <div className="banner error">{error}</div>}

      {view === "cases" && (
        <CasesPage
          cases={filteredActive}
          archived={filteredArchived}
          totalCount={activeCases.length}
          archivedCount={archivedCases.length}
          query={query}
          busy={busy}
          onCreate={async (payload) => {
            setBusy(true);
            setError("");
            try {
              const created = await createCase(payload);
              await refreshCases();
              await openCase(created.id);
            } catch (err) {
              setError(err.message);
            } finally {
              setBusy(false);
            }
          }}
          onOpen={openCase}
          onArchive={handleArchive}
          onUnarchive={handleUnarchive}
          onDelete={handleDelete}
        />
      )}

      {view === "case" && caseData && (
        <CaseWorkspace
          caseData={caseData}
          busy={busy}
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

function CasesPage({ cases, archived, totalCount, archivedCount, query, busy, onCreate, onOpen, onArchive, onUnarchive, onDelete }) {
  const [caption, setCaption] = useState("");
  const [court, setCourt] = useState("");
  const [indexNumber, setIndexNumber] = useState("");
  const [showNew, setShowNew] = useState(false);

  return (
    <>
      <div className="page-head">
        <div>
          <p className="kicker">Matters</p>
          <h2 className="serif">Cases</h2>
          <p className="footnote">
            {query.trim()
              ? `${cases.length} match${cases.length === 1 ? "" : "es"} of ${totalCount} active`
              : `${totalCount} active matter${totalCount === 1 ? "" : "s"}`}
            {archivedCount ? ` · ${archivedCount} archived` : ""}
          </p>
        </div>
        <button className="btn" type="button" onClick={() => setShowNew((v) => !v)}>
          {showNew ? "Cancel" : "New case"}
        </button>
      </div>

      {showNew && (
        <div className="card quiet">
          <p className="kicker">New matter</p>
          <h3 className="serif" style={{ margin: "4px 0 8px" }}>Open a case</h3>
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
              <input value={caption} onChange={(e) => setCaption(e.target.value)} placeholder="Bank v. Defendant" required />
            </label>
            <label>
              Court
              <input value={court} onChange={(e) => setCourt(e.target.value)} placeholder="Supreme Court, Kings County" />
            </label>
            <label>
              Index
              <input value={indexNumber} onChange={(e) => setIndexNumber(e.target.value)} placeholder="11467/2007" />
            </label>
            <button className="btn gold" type="submit" disabled={busy || !caption.trim()}>
              Create
            </button>
          </form>
        </div>
      )}

      <div className="card flush">
        {cases.length === 0 ? (
          <div className="empty">
            <FolderOpen size={22} />
            <p>
              {totalCount === 0
                ? "No cases yet. Open a matter to keep filings together."
                : "No cases match that search."}
            </p>
          </div>
        ) : (
          <table className="data cases">
            <thead>
              <tr>
                <th>Caption</th>
                <th>Parties</th>
                <th>Index</th>
                <th>Court</th>
                <th>Files</th>
                <th className="actions-col"> </th>
              </tr>
            </thead>
            <tbody>
              {cases.map((item) => (
                <CaseRow key={item.id} item={item} busy={busy} onOpen={onOpen} onArchive={onArchive} onDelete={onDelete} />
              ))}
            </tbody>
          </table>
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
            <table className="data cases">
              <thead>
                <tr>
                  <th>Caption</th>
                  <th>Index</th>
                  <th>Files</th>
                  <th className="actions-col"> </th>
                </tr>
              </thead>
              <tbody>
                {archived.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <button type="button" className="linkish" onClick={() => onOpen(item.id)}>
                        {item.caption}
                      </button>
                    </td>
                    <td className="mono">{item.index_number || "—"}</td>
                    <td className="num">{item.document_count || 0}</td>
                    <td className="row-actions" onClick={(e) => e.stopPropagation()}>
                      <button type="button" className="icon-btn" disabled={busy} title="Restore" onClick={() => onUnarchive(item)}>
                        <RotateCcw size={15} />
                        <span>Restore</span>
                      </button>
                      <button type="button" className="icon-btn danger" disabled={busy} title="Delete permanently" onClick={() => onDelete(item)}>
                        <Trash2 size={15} />
                        <span>Delete</span>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </details>
      )}
    </>
  );
}

function CaseRow({ item, busy, onOpen, onArchive, onDelete }) {
  const parties = partiesFromCaption(item.caption);
  return (
    <tr className="click-row" onClick={() => onOpen(item.id)}>
      <td>
        <strong className="matter-name">{item.caption}</strong>
      </td>
      <td className="muted">
        {parties.length > 1 ? parties.join(" · ") : parties[0] || "—"}
      </td>
      <td className="mono">{item.index_number || "—"}</td>
      <td className="muted">{item.court || "—"}</td>
      <td className="num">{item.document_count || 0}</td>
      <td className="row-actions" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="icon-btn" disabled={busy} title="Archive" onClick={() => onArchive(item)}>
          <Archive size={15} />
        </button>
        <button type="button" className="icon-btn danger" disabled={busy} title="Delete permanently" onClick={() => onDelete(item)}>
          <Trash2 size={15} />
        </button>
      </td>
    </tr>
  );
}

function CaseWorkspace({ caseData, busy, onBack, onRefresh, onError, onOpenReport, setBusy, onArchive, onUnarchive, onDelete }) {
  const [drag, setDrag] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState("");
  const [docQuery, setDocQuery] = useState("");
  const [uploadNote, setUploadNote] = useState("");
  const inputRef = useRef(null);
  const docs = caseData.documents || [];
  const visibleDocs = docs.filter((doc) =>
    matchesQuery(
      [doc.display_name, doc.original_filename, doc.doc_kind, doc.status, doc.risk_level].filter(Boolean).join(" "),
      docQuery
    )
  );

  async function onUpload(fileList) {
    const files = Array.from(fileList || []);
    const valid = files.filter((f) => ACCEPTED.has(extensionOf(f.name)));
    const rejected = files.filter((f) => !ACCEPTED.has(extensionOf(f.name))).map((f) => f.name);
    if (rejected.length) onError(`Skipped unsupported file(s): ${rejected.join(", ")}`);
    if (!valid.length) return;
    onError("");
    try {
      for (let i = 0; i < valid.length; i += 1) {
        setUploadNote(`Adding ${i + 1} of ${valid.length}: ${valid[i].name}`);
        await uploadCaseDocument(caseData.id, valid[i], { autoScan: true });
        await onRefresh();
      }
      setUploadNote(`Added ${valid.length} file${valid.length === 1 ? "" : "s"}. Scans will finish in the background.`);
    } catch (err) {
      onError(err.message);
      setUploadNote("");
    } finally {
      if (inputRef.current) inputRef.current.value = "";
    }
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
    try {
      await scanDocument(caseData.id, doc.id);
      await onRefresh();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const scanned = docs.filter((d) => d.status === "complete" && d.risk_level);
  const byRisk = {
    red: scanned.filter((d) => d.risk_level === "red"),
    yellow: scanned.filter((d) => d.risk_level === "yellow"),
    green: scanned.filter((d) => d.risk_level === "green"),
  };

  return (
    <>
      <div className="card case-hero">
        <div className="toolbar tight">
          <button className="text-link" type="button" onClick={() => { setBusy(false); onBack(); }}>← All cases</button>
          <div className="row-actions">
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
        <p className="kicker">{caseData.archived_at ? "Archived case" : "Case"}</p>
        <h2 className="serif">{caseData.caption}</h2>
        <p className="meta-line">
          {partiesFromCaption(caseData.caption).join(" · ") || "Parties from caption"}
          <span className="dot" />
          {caseData.index_number || "No index"}
          <span className="dot" />
          {caseData.court || "Court not set"}
        </p>
        {uploadNote && <p className="footnote">{uploadNote}</p>}
        {caseData.archived_at ? (
          <p className="footnote">This matter is archived. Restore it to add filings.</p>
        ) : (
        <div
          className={`dropzone compact ${drag ? "active" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            onUpload(e.dataTransfer.files);
          }}
          onClick={() => inputRef.current?.click()}
        >
          <Upload size={18} />
          <div>
            <strong>Add filings</strong>
            <p>Drop files here or click to upload. Pleadings get content review; exhibits get forensic review.</p>
          </div>
          <input
            ref={inputRef}
            type="file"
            hidden
            multiple
            accept=".pdf,.txt,.docx,.xlsx,.jpg,.jpeg,.png,.gif"
            onChange={(e) => onUpload(e.target.files)}
          />
        </div>
        )}
      </div>

      <CaseRiskSummary
        byRisk={byRisk}
        pendingCount={docs.filter((d) => d.status !== "complete").length}
        onOpenReport={onOpenReport}
      />

      <CaseGotchaBoard caseId={caseData.id} documents={docs} onOpenReport={onOpenReport} />

      <CaseChat
        caseId={caseData.id}
        documents={docs}
        onOpenReport={onOpenReport}
        onError={onError}
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
          <button className="btn secondary" type="button" disabled={busy} onClick={() => inputRef.current?.click()}>
            Upload
          </button>
          )}
        </div>
        {docs.length === 0 && <p className="empty-copy">Drop complaints, motions, and exhibits as the matter progresses.</p>}
        {docs.length > 0 && visibleDocs.length === 0 && (
          <p className="empty-copy">No documents match that filter.</p>
        )}
        {visibleDocs.length > 0 && (
        <table className="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind / scan</th>
              <th>Status</th>
              <th>Risk</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {visibleDocs.map((doc) => (
              <tr key={doc.id}>
                <td>
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
                      className="linkish"
                      onClick={() => {
                        setEditingId(doc.id);
                        setEditName(doc.display_name || doc.original_filename);
                      }}
                    >
                      {doc.display_name || doc.original_filename}
                    </button>
                  )}
                  <div className="footnote">{doc.original_filename}</div>
                </td>
                <td>
                  <select
                    value={doc.doc_kind || ""}
                    disabled={busy}
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
                <td>{doc.status}</td>
                <td>{doc.risk_level ? `${RISK[doc.risk_level] || doc.risk_level}` : "—"}</td>
                <td>
                  <div className="actions">
                    {doc.status === "complete" && (
                      <button className="btn gold" type="button" onClick={() => onOpenReport(doc)}>
                        Report
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
            ))}
          </tbody>
        </table>
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

function CaseGotchaBoard({ caseId, documents, onOpenReport }) {
  const [gotchas, setGotchas] = useState([]);
  const [factConflicts, setFactConflicts] = useState([]);

  useEffect(() => {
    let cancelled = false;
    listCaseGotchas(caseId)
      .then((data) => {
        if (cancelled) return;
        setGotchas(data.gotchas || []);
        setFactConflicts(data.factConflicts || []);
      })
      .catch(() => {
        if (cancelled) return;
        setGotchas([]);
        setFactConflicts([]);
      });
    return () => {
      cancelled = true;
    };
  }, [caseId, documents]);

  const total = gotchas.length + factConflicts.length;
  if (total === 0) return null;

  return (
    <div className="card gotcha-board">
      <p className="kicker">Gotcha finder</p>
      <h2 className="serif" style={{ margin: "4px 0 8px" }}>
        {total} apparent contradiction{total === 1 ? "" : "s"} worth verifying
      </h2>
      <p className="footnote" style={{ marginBottom: 8 }}>
        Each conflict is listed once, with every file that states it. Candidates for review,
        not findings of fact. Confirm the quoted language before relying on any of them.
      </p>
      {factConflicts.map((item) => (
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
      {gotchas.map((item) => {
        const sources = relatedDocs(item, documents);
        return (
          <div key={`${item.id || item.title}-${item.document_id}`} className="gotcha-item">
            <h3>{item.title}</h3>
            {(item.contradiction_type || item.gotcha_level) && (
              <p className="footnote">{item.gotcha_level || item.contradiction_type}</p>
            )}
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
            {item.tainted_support ? (
              <p className="footnote">
                They produced this record in support of the claim, and the record itself appears
                fraudulent or false.
              </p>
            ) : item.self_undercut ? (
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
  "What was the Wells Fargo statement used for?",
  "Which motion cites this exhibit?",
  "Where do the amounts conflict across filings?",
];

const REPORT_CHAT_HINTS = [
  "What was this document used for?",
  "Which motions cite this exhibit?",
  "Do the amounts here match other filings?",
];

function CaseChat({ caseId, documents, onOpenReport, onError, focusDocument }) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [asking, setAsking] = useState(false);
  const threadRef = useRef(null);

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

  return (
    <div className={`card case-chat${focusDocument ? " on-report" : ""}`}>
      <p className="kicker">Case clerk</p>
      <h2 className="serif" style={{ margin: "4px 0 8px" }}>
        {focusDocument ? "Ask about this document" : "Ask about these documents"}
      </h2>
      <p className="footnote" style={{ marginBottom: 14 }}>
        {focusDocument
          ? `Questions treat “this” as ${focusDocument.display_name || focusDocument.original_filename}, then search the rest of the case for citations and conflicts.`
          : "Answers come from the filings and exhibits in this case — for example, where a motion cites a Wells Fargo statement. Click a source to open its report."}
      </p>
      <div className="chat-thread" ref={threadRef}>
        {messages.length === 0 && !asking && (
          <div className="chat-empty">
            <MessageSquare size={18} />
            <p>
              {focusDocument
                ? "Ask what this filing was used for, or whether another document in the case contradicts it."
                : "Ask what an exhibit was used for, or where a party or amount appears in the motions."}
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
                {cites.map((cite, idx) => (
                  <button
                    key={`${msg.id}-${idx}`}
                    type="button"
                    className="chat-cite"
                    onClick={() => openCite(cite)}
                    title={cite.note || "Open this document’s report"}
                  >
                    Open {cite.display_name}
                  </button>
                ))}
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
              : "e.g. What was the Wells Fargo mortgage statement used for?"
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

function AccountPanel({ entitlement, onRefresh, onError }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [license, setLicense] = useState("");

  if (!entitlement) {
    return <div className="card"><p>Loading account status…</p></div>;
  }

  return (
    <div className="card">
      <p className="kicker">Hybrid control plane</p>
      <h2 className="serif" style={{ marginTop: 4 }}>Subscription & data sharing</h2>
      <p>
        Mode: <strong>{entitlement.mode}</strong>
        {" · "}
        {entitlement.active ? "Scans unlocked" : "Scans locked"}
      </p>
      <p className="footnote">{entitlement.message}</p>

      <label className="share-row">
        <input
          type="checkbox"
          checked={Boolean(entitlement.share_anonymized_outcomes)}
          onChange={async (e) => {
            try {
              await setShareOutcomes(e.target.checked);
              await onRefresh();
            } catch (err) {
              onError(err.message);
            }
          }}
        />
        Share anonymized finding outcomes (types, severity, confidence — never the PDF)
      </label>

      {entitlement.mode === "license" && (
        <form
          className="case-form"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await saveLicense(license);
              await onRefresh();
            } catch (err) {
              onError(err.message);
            }
          }}
        >
          <label>
            License key
            <input value={license} onChange={(e) => setLicense(e.target.value)} placeholder="XXXX-XXXX-XXXX" />
          </label>
          <button className="btn" type="submit">Save license</button>
        </form>
      )}

      {entitlement.mode === "cloud" && (
        <form
          className="case-form"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await loginCloud(email, password);
              await onRefresh();
            } catch (err) {
              onError(err.message);
            }
          }}
        >
          <label>
            Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </label>
          <label>
            Password
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          <div className="actions">
            <button className="btn" type="submit">Sign in</button>
            <button
              className="btn secondary"
              type="button"
              onClick={async () => {
                await logoutCloud();
                await onRefresh();
              }}
            >
              Sign out
            </button>
          </div>
        </form>
      )}

      {entitlement.mode === "open" && (
        <p className="footnote" style={{ marginTop: 12 }}>
          Running in open/local-dev mode. Set <code>ENTITLEMENT_MODE=license</code> or
          <code> cloud</code> in <code>.env</code> when you are ready to gate monthly access.
        </p>
      )}
    </div>
  );
}

function ProgressPanel({ job, filename }) {
  const percent = job.percent || 0;
  const current = job.stage;
  return (
    <div className="card">
      <p className="kicker">Analyzing</p>
      <h2 className="serif" style={{ marginTop: 4 }}>{filename}</h2>
      <p>{job.message}</p>
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
    && item.evidence.field !== "missing_exhibit"
  );
  const visibleFindings = findings.filter((f) => !isCrossDocFact(f));
  const material = visibleFindings.filter((f) => f.severity !== "info");
  const caseAlerts = visibleFindings.filter((f) => f.category === "case_consistency");
  const hiddenCrossDoc = findings.length - visibleFindings.length;
  const [showDoc, setShowDoc] = useState(true);
  const fileUrl = caseId && documentId
    ? documentFileUrl(caseId, documentId)
    : jobSourceUrl(job.job_id);
  const lowerName = (documentName || job.filename || "").toLowerCase();
  const isPdf = lowerName.endsWith(".pdf");
  const isImage = /\.(png|jpe?g|gif)$/i.test(lowerName);
  const isText = lowerName.endsWith(".txt");

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
          </div>
          <div className="actions">
            <button
              className={`btn ${showDoc ? "gold" : "secondary"}`}
              type="button"
              onClick={() => setShowDoc((v) => !v)}
            >
              {showDoc ? "Hide document" : "View document"}
            </button>
            <a className="btn secondary" href={fileUrl} target="_blank" rel="noreferrer">
              Open in new tab
            </a>
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
        />
      )}

      <div className={`report-layout ${showDoc ? "with-doc" : ""}`}>
        {showDoc && (
          <div className="card doc-pane">
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
              <p className="kicker" style={{ margin: 0 }}>Source document</p>
              <a className="footnote" href={fileUrl} target="_blank" rel="noreferrer">Fullscreen</a>
            </div>
            <DocumentPreview url={fileUrl} isPdf={isPdf} isImage={isImage} isText={isText} name={lowerName} />
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
                {(report.top_findings || []).filter((item) => !isCrossDocFact(item)).length === 0 && <p>No material findings.</p>}
                {(report.top_findings || []).filter((item) => !isCrossDocFact(item)).map((item) => (
                  <FindingCard key={item.id} item={item} docKind={docKind} />
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
                <FindingCard key={item.id} item={item} docKind={docKind} />
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

function FindingCard({ item, docKind }) {
  const heat = item.evidence?.heatmap;
  const aiLead = Boolean(item.evidence?.ai_generated);
  const caseHit = Boolean(item.evidence?.case_consistency);
  const gotcha = Boolean(item.evidence?.gotcha) || item.category === "gotcha";
  const [feedback, setFeedback] = useState(null);

  return (
    <article className={`finding${gotcha ? " gotcha-card" : ""}`}>
      <header>
        <h3>{item.title}</h3>
        <span className={`sev ${item.severity}`}>
          {gotcha && item.evidence?.gotcha_level ? `${item.evidence.gotcha_level} · ` : ""}
          {item.severity} · {Math.round(item.confidence)}%
        </span>
      </header>
      <p className="footnote">{CATEGORY[item.category] || item.category}{item.page ? ` · page ${item.page}` : ""}</p>
      {caseHit && item.evidence?.prior_display_name && (
        <p className="footnote">Prior file: {item.evidence.prior_display_name}</p>
      )}
      {gotcha && item.evidence?.claim_quote ? (
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
      <p className="why">{item.why_suspicious}</p>
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
