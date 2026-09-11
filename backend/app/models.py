from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Category = Literal[
    "metadata", "image", "text", "pdf_structure", "page_anomalies", "contextual"
]
Severity = Literal["info", "low", "medium", "high", "critical"]
RiskLevel = Literal["green", "yellow", "red"]
JobStatus = Literal["queued", "running", "complete", "failed"]


class Finding(BaseModel):
    id: str
    category: Category
    severity: Severity
    title: str
    explanation: str
    why_suspicious: str
    confidence: float = Field(ge=0, le=100)
    page: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    recommended_action: str | None = None


class TimelineEvent(BaseModel):
    label: str
    timestamp: str | None = None
    source: str
    note: str | None = None
    anomaly: bool = False


class PageDimension(BaseModel):
    page: int
    width_pts: float | None = None
    height_pts: float | None = None
    width_px: int | None = None
    height_px: int | None = None
    dpi: float | None = None
    rotation: int | None = None
    orientation: str | None = None


class FontUsage(BaseModel):
    name: str
    pages: list[int] = Field(default_factory=list)
    sizes: list[float] = Field(default_factory=list)
    sample: str | None = None


class ModuleResult(BaseModel):
    name: str
    status: Literal["ok", "skipped", "error"]
    duration_ms: int = 0
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class AuditTrail(BaseModel):
    scanned_at: datetime
    analysis_version: str
    modules_run: list[str]
    submitted_by: str = "court-staff"
    filename: str
    file_size_bytes: int
    file_type: str
    sha256: str


class Report(BaseModel):
    job_id: str
    risk_level: RiskLevel
    risk_score: int = Field(ge=0, le=100)
    executive_summary: str
    top_findings: list[Finding] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    page_dimensions: list[PageDimension] = Field(default_factory=list)
    fonts: list[FontUsage] = Field(default_factory=list)
    comparison: dict[str, Any] = Field(default_factory=dict)
    modules: list[ModuleResult] = Field(default_factory=list)
    audit: AuditTrail
    artifact_urls: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "This report identifies potential indicators of alteration or inconsistency. "
        "It is a decision-support tool, not a legal determination of authenticity, "
        "fraud, or admissibility. Findings should be reviewed by qualified personnel."
    )


class JobProgress(BaseModel):
    job_id: str
    status: JobStatus
    stage: str
    percent: int = Field(ge=0, le=100)
    message: str = ""
    filename: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    report: Report | None = None


class UploadResponse(BaseModel):
    job_id: str
    filename: str
    status: JobStatus
