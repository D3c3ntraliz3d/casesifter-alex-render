import { useEffect, useMemo, useRef, useState } from "react";
import { ShieldAlert, Upload } from "lucide-react";
import { exportUrl, getJob, uploadExhibit } from "./api";

const STAGES = [
  ["queued", "Queued"],
  ["ingesting", "File intake"],
  ["metadata", "Metadata"],
  ["pdf_integrity", "PDF integrity"],
  ["structural", "Page structure"],
  ["image_forensics", "Image forensics"],
  ["ocr_text", "Text / OCR"],
  ["contextual", "Contextual logic"],
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
};

const RISK = {
  green: "Low risk",
  yellow: "Moderate risk",
  red: "High risk",
};

export default function App() {
  const [file, setFile] = useState(null);
  const [drag, setDrag] = useState(false);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("summary");
  const inputRef = useRef(null);

  useEffect(() => {
    if (!job?.job_id || job.status === "complete" || job.status === "failed") return;
    const timer = setInterval(async () => {
      try {
        const next = await getJob(job.job_id);
        setJob(next);
      } catch (err) {
        setError(err.message);
      }
    }, 900);
    return () => clearInterval(timer);
  }, [job?.job_id, job?.status]);

  const report = job?.report;
  const findings = report?.findings || [];

  async function onUpload(selected) {
    if (!selected) return;
    setError("");
    setTab("summary");
    setFile(selected);
    try {
      const created = await uploadExhibit(selected);
      setJob({ ...created, percent: 2, stage: "queued", message: "Upload received." });
    } catch (err) {
      setError(err.message);
    }
  }

  function reset() {
    setFile(null);
    setJob(null);
    setError("");
    setTab("summary");
  }

  return (
    <div className="app">
      <header className="masthead">
        <div className="brand">
          <div className="seal">EX</div>
          <div>
            <h1>Court Exhibit Scanner</h1>
            <p>Fraud indicators & authenticity verification · Phase 1</p>
          </div>
        </div>
        <div className="badge-soft">Decision support — not a legal finding</div>
      </header>

      {!job && (
        <div className="card">
          <p className="kicker">Submit an exhibit</p>
          <div
            className={`dropzone ${drag ? "active" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDrag(true);
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDrag(false);
              onUpload(e.dataTransfer.files?.[0]);
            }}
            onClick={() => inputRef.current?.click()}
          >
            <Upload size={28} />
            <h2>Drop a PDF, image, Word, or Excel file</h2>
            <p>Single file, up to 100 MB. Analysis starts immediately after upload.</p>
            <div className="formats">PDF · DOCX · XLSX · JPG · PNG · GIF</div>
            <input
              ref={inputRef}
              type="file"
              hidden
              accept=".pdf,.docx,.xlsx,.jpg,.jpeg,.png,.gif"
              onChange={(e) => onUpload(e.target.files?.[0])}
            />
          </div>
        </div>
      )}

      {error && <div className="card error">{error}</div>}

      {job && job.status !== "complete" && job.status !== "failed" && (
        <ProgressPanel job={job} filename={file?.name || job.filename} />
      )}

      {job?.status === "failed" && (
        <div className="card">
          <h2 className="serif">Analysis could not be completed</h2>
          <p>{job.error || job.message}</p>
          <button className="btn" onClick={reset}>Scan another exhibit</button>
        </div>
      )}

      {report && job.status === "complete" && (
        <ReportViewer
          job={job}
          report={report}
          findings={findings}
          tab={tab}
          setTab={setTab}
          onReset={reset}
        />
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

function ReportViewer({ job, report, findings, tab, setTab, onReset }) {
  const material = findings.filter((f) => f.severity !== "info");
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
          </div>
          <div className="actions">
            <a className="btn gold" href={exportUrl(job.job_id, "html")}>Export HTML</a>
            <a className="btn" href={exportUrl(job.job_id, "pdf")}>Export PDF</a>
            <a className="btn secondary" href={exportUrl(job.job_id, "json")}>Export JSON</a>
            <button className="btn secondary" onClick={onReset}>New scan</button>
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
                <FindingCard key={item.id} item={item} />
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
              <FindingCard key={item.id} item={item} />
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

function FindingCard({ item }) {
  const heat = item.evidence?.heatmap;
  const aiLead = Boolean(item.evidence?.ai_generated);
  return (
    <article className="finding">
      <header>
        <h3>{item.title}</h3>
        <span className={`sev ${item.severity}`}>{item.severity} · {Math.round(item.confidence)}%</span>
      </header>
      <p className="footnote">{CATEGORY[item.category] || item.category}{item.page ? ` · page ${item.page}` : ""}</p>
      <p>{item.explanation}</p>
      <p className="why">{item.why_suspicious}</p>
      {aiLead && (
        <p className="ai-lead">
          <strong>AI-Assisted Research Lead:</strong> This finding is based on an automated
          model review, not definitive evidence. Verify independently before relying on it.
        </p>
      )}
      {item.recommended_action && <p><strong>Suggested next step:</strong> {item.recommended_action}</p>}
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
