import { useEffect, useRef, useState } from "react";
import { FolderOpen, ShieldAlert, Upload } from "lucide-react";
import {
  createCase,
  exportUrl,
  getCase,
  getEntitlement,
  getJob,
  listCases,
  loginCloud,
  logoutCloud,
  patchDocument,
  saveLicense,
  scanDocument,
  sendFindingFeedback,
  setShareOutcomes,
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
};

const RISK = {
  green: "Low risk",
  yellow: "Moderate risk",
  red: "High risk",
};

const ACCEPTED = new Set([".pdf", ".docx", ".xlsx", ".txt", ".jpg", ".jpeg", ".png", ".gif"]);

function extensionOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
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
            setJob(progress);
          }
        }
      } catch {
        /* keep polling */
      }
    }, 1000);
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
    const progress = await getJob(doc.scan_job_id);
    setJob(progress);
    setView("report");
  }

  return (
    <div className="app">
      <header className="masthead">
        <div className="brand">
          <div className="seal">EX</div>
          <div>
            <h1>Court Exhibit Scanner</h1>
            <p>Case-local evidence · hybrid subscription control</p>
          </div>
        </div>
        <div className="actions">
          <button className="btn secondary" type="button" onClick={() => setView("cases")}>
            Cases
          </button>
          <button className="btn secondary" type="button" onClick={() => { refreshEntitlement(); setView("account"); }}>
            Account
          </button>
          <div className="badge-soft">
            {entitlement?.active ? (entitlement.plan || "Active") : "Inactive"}
          </div>
        </div>
      </header>

      {error && <div className="card error">{error}</div>}

      {view === "cases" && (
        <CasesPage
          cases={cases}
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
        />
      )}

      {view === "report" && (
        <>
          <div className="card">
            <button className="btn secondary" type="button" onClick={() => setView("case")}>
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

function CasesPage({ cases, busy, onCreate, onOpen }) {
  const [caption, setCaption] = useState("");
  const [court, setCourt] = useState("");
  const [indexNumber, setIndexNumber] = useState("");

  return (
    <>
      <div className="card">
        <p className="kicker">New matter</p>
        <h2 className="serif" style={{ marginTop: 4 }}>Open a case</h2>
        <p className="footnote">
          Documents stay on this machine under the case folder. The cloud only handles
          subscription status and optional anonymized finding stats.
        </p>
        <form
          className="case-form"
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
          }}
        >
          <label>
            Case caption
            <input value={caption} onChange={(e) => setCaption(e.target.value)} placeholder="Bank v. Defendant" required />
          </label>
          <label>
            Court
            <input value={court} onChange={(e) => setCourt(e.target.value)} placeholder="Supreme Court, Kings County" />
          </label>
          <label>
            Index number
            <input value={indexNumber} onChange={(e) => setIndexNumber(e.target.value)} placeholder="11467/2007" />
          </label>
          <button className="btn" type="submit" disabled={busy || !caption.trim()}>
            Create case
          </button>
        </form>
      </div>

      <div className="card">
        <p className="kicker">Your cases</p>
        {cases.length === 0 && <p>No cases yet. Create one to start filing exhibits locally.</p>}
        <ul className="batch-list">
          {cases.map((item) => (
            <li key={item.id}>
              <button type="button" className="batch-item" onClick={() => onOpen(item.id)}>
                <span className="batch-index"><FolderOpen size={16} /></span>
                <span className="batch-meta">
                  <strong>{item.caption}</strong>
                  <span className="footnote">
                    {[item.index_number, item.court].filter(Boolean).join(" · ") || "No index yet"}
                    {" · "}
                    {item.document_count || 0} document{(item.document_count || 0) === 1 ? "" : "s"}
                  </span>
                </span>
                <span className="batch-pill">open</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}

function CaseWorkspace({ caseData, busy, onBack, onRefresh, onError, onOpenReport, setBusy }) {
  const [drag, setDrag] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState("");
  const inputRef = useRef(null);
  const docs = caseData.documents || [];

  async function onUpload(fileList) {
    const files = Array.from(fileList || []);
    const valid = files.filter((f) => ACCEPTED.has(extensionOf(f.name)));
    const rejected = files.filter((f) => !ACCEPTED.has(extensionOf(f.name))).map((f) => f.name);
    if (rejected.length) onError(`Skipped unsupported file(s): ${rejected.join(", ")}`);
    if (!valid.length) return;
    setBusy(true);
    onError("");
    try {
      for (const file of valid) {
        await uploadCaseDocument(caseData.id, file, { autoScan: true });
      }
      await onRefresh();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
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

  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <button className="btn secondary" type="button" onClick={onBack} style={{ marginBottom: 10 }}>
              ← All cases
            </button>
            <p className="kicker">Case workspace</p>
            <h2 className="serif" style={{ margin: "4px 0" }}>{caseData.caption}</h2>
            <p className="footnote">
              {[caseData.index_number, caseData.court].filter(Boolean).join(" · ") || "Index / court not set"}
            </p>
          </div>
        </div>
        <div
          className={`dropzone ${drag ? "active" : ""}`}
          style={{ marginTop: 16, padding: "28px 16px" }}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            onUpload(e.dataTransfer.files);
          }}
          onClick={() => inputRef.current?.click()}
        >
          <Upload size={24} />
          <h2>Add filings to this case</h2>
          <p>Files are stored locally and scanned against prior documents already in the case.</p>
          <div className="formats">PDF · TXT · DOCX · XLSX · JPG · PNG · GIF</div>
          <input
            ref={inputRef}
            type="file"
            hidden
            multiple
            accept=".pdf,.txt,.docx,.xlsx,.jpg,.jpeg,.png,.gif"
            onChange={(e) => onUpload(e.target.files)}
          />
        </div>
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
          <div>
            <p className="kicker">Documents</p>
            <h2 className="serif" style={{ margin: "4px 0 0" }}>
              {docs.length} file{docs.length === 1 ? "" : "s"}
            </h2>
          </div>
          <button className="btn secondary" type="button" disabled={busy} onClick={() => inputRef.current?.click()}>
            Upload more
          </button>
        </div>
        {docs.length === 0 && <p>Drop complaints, motions, and exhibits here as the matter progresses.</p>}
        <table className="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Risk</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {docs.map((doc) => (
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
                <td>{doc.doc_kind || "—"}</td>
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
      </div>
    </>
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

function ReportViewer({ job, report, findings, tab, setTab, docKind }) {
  const material = findings.filter((f) => f.severity !== "info");
  const caseAlerts = findings.filter((f) => f.category === "case_consistency");
  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <p className="kicker">Authenticity report</p>
            <h2 className="serif" style={{ margin: "4px 0 8px" }}>{report.audit.filename}</h2>
            <span className={`risk ${report.risk_level}`}>
              <ShieldAlert size={16} />
              {RISK[report.risk_level]} · {report.risk_score}/100
            </span>
            {caseAlerts.length > 0 && (
              <p className="footnote" style={{ marginTop: 8 }}>
                {caseAlerts.length} case-consistency alert{caseAlerts.length === 1 ? "" : "s"} vs prior filings.
              </p>
            )}
          </div>
          <div className="actions">
            <a className="btn gold" href={exportUrl(job.job_id, "html")}>Export HTML</a>
            <a className="btn" href={exportUrl(job.job_id, "pdf")}>Export PDF</a>
            <a className="btn secondary" href={exportUrl(job.job_id, "json")}>Export JSON</a>
          </div>
        </div>
        <p style={{ marginTop: 16 }}>{report.executive_summary}</p>
      </div>

      <div className="card">
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
              {(report.top_findings || []).length === 0 && <p>No material findings.</p>}
              {(report.top_findings || []).map((item) => (
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
            <p className="footnote">{material.length} material finding(s), {findings.length} total including notes.</p>
            {findings.map((item) => (
              <FindingCard key={item.id} item={item} docKind={docKind} />
            ))}
            {findings.length === 0 && <p>The automated modules did not flag this file.</p>}
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
            <h3 className="serif">Page dimensions</h3>
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
    </>
  );
}

function FindingCard({ item, docKind }) {
  const heat = item.evidence?.heatmap;
  const aiLead = Boolean(item.evidence?.ai_generated);
  const caseHit = Boolean(item.evidence?.case_consistency);
  const [feedback, setFeedback] = useState(null);

  return (
    <article className="finding">
      <header>
        <h3>{item.title}</h3>
        <span className={`sev ${item.severity}`}>{item.severity} · {Math.round(item.confidence)}%</span>
      </header>
      <p className="footnote">{CATEGORY[item.category] || item.category}{item.page ? ` · page ${item.page}` : ""}</p>
      {caseHit && item.evidence?.prior_display_name && (
        <p className="footnote">Prior file: {item.evidence.prior_display_name}</p>
      )}
      <p>{item.explanation}</p>
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
