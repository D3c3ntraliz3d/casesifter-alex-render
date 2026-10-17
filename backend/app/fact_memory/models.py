"""SQLAlchemy models for firm-scoped fact memory (Postgres or SQLite)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Firm(Base):
    __tablename__ = "firms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    retention_policy: Mapped[str] = mapped_column(String(64), default="until_deleted")
    persist_facts: Mapped[bool] = mapped_column(Boolean, default=True)
    # Wrapped Fernet key (master-encrypted).
    wrapped_data_key: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    store_snippets: Mapped[bool] = mapped_column(Boolean, default=True)

    users: Mapped[list["User"]] = relationship(back_populates="firm", cascade="all, delete-orphan")
    matters: Mapped[list["Matter"]] = relationship(back_populates="firm", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    firm_id: Mapped[str] = mapped_column(String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(64), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    firm: Mapped["Firm"] = relationship(back_populates="users")


class Matter(Base):
    __tablename__ = "matters"
    __table_args__ = (Index("ix_matters_firm", "firm_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    firm_id: Mapped[str] = mapped_column(String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    index_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    court: Mapped[str | None] = mapped_column(String(255), nullable=True)
    county: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    firm: Mapped["Firm"] = relationship(back_populates="matters")
    documents: Mapped[list["Document"]] = relationship(back_populates="matter", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("matter_id", "sha256", name="uq_documents_matter_sha"),
        Index("ix_documents_firm", "firm_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matter_id: Mapped[str] = mapped_column(String(36), ForeignKey("matters.id", ondelete="CASCADE"), nullable=False)
    firm_id: Mapped[str] = mapped_column(String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Encrypted filename
    original_filename_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extractor_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Vault-compatible snapshot JSON (entities/facts/fingerprint) — encrypted blob.
    snapshot_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full report JSON for reopen without job TTL (encrypted).
    report_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Unused. Left in place so existing databases do not need a column drop.
    # Exhibit bytes are not stored. A one-shot migration nulls any leftover value.
    source_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_missing: Mapped[bool] = mapped_column(Boolean, default=True)
    last_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    matter: Mapped["Matter"] = relationship(back_populates="documents")
    facts: Mapped[list["Fact"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class SchemaMigration(Base):
    """One row per applied data migration. Presence means do not run it again."""

    __tablename__ = "schema_migrations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(40), nullable=False)


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (Index("ix_entities_matter", "matter_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matter_id: Mapped[str] = mapped_column(String(36), ForeignKey("matters.id", ondelete="CASCADE"), nullable=False)
    firm_id: Mapped[str] = mapped_column(String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_name_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    aliases_enc: Mapped[str | None] = mapped_column(Text, nullable=True)


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (Index("ix_facts_matter", "matter_id"), Index("ix_facts_document", "document_id"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matter_id: Mapped[str] = mapped_column(String(36), ForeignKey("matters.id", ondelete="CASCADE"), nullable=False)
    firm_id: Mapped[str] = mapped_column(String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True)
    attribute: Mapped[str] = mapped_column(String(128), nullable=False)
    value_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_normalized_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snippet_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Full vault fact dict (encrypted) so compare_to_case gets identical shapes.
    fact_blob_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="facts")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_matter", "matter_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matter_id: Mapped[str] = mapped_column(String(36), ForeignKey("matters.id", ondelete="CASCADE"), nullable=False)
    firm_id: Mapped[str] = mapped_column(String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    document_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class FindingRow(Base):
    __tablename__ = "findings"
    __table_args__ = (Index("ix_findings_matter", "matter_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    matter_id: Mapped[str] = mapped_column(String(36), ForeignKey("matters.id", ondelete="CASCADE"), nullable=False)
    firm_id: Mapped[str] = mapped_column(String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    finding_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    description_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open, dismissed, confirmed, stale
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_firm", "firm_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firm_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AnalysisJob(Base):
    """Durable ephemeral-job ledger — survives Render disk wipes / restarts."""

    __tablename__ = "analysis_jobs"
    __table_args__ = (
        Index("ix_analysis_jobs_firm", "firm_id"),
        Index("ix_analysis_jobs_matter", "matter_id"),
        Index("ix_analysis_jobs_status", "status"),
        Index("ix_analysis_jobs_sha", "sha256"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    firm_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False
    )
    # Submitting user — used by worker result persist (never trusted from worker body).
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    matter_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("matters.id", ondelete="SET NULL"), nullable=True
    )
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Times this matter+sha was status=running when the instance restarted (OOM/health).
    restart_interruptions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Labels for provisional Documents rows before fact persist creates a Document.
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_review: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_review_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


_engine = None
_SessionLocal = None


def database_url_from_env() -> str:
    import os
    from pathlib import Path

    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        # Render sometimes gives postgres:// — SQLAlchemy wants postgresql+psycopg://
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://") and "+psycopg" not in url:
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
        return url
    # SQLite fallback for local/CI — same schema via SQLAlchemy.
    data = Path(os.environ.get("FACT_MEMORY_SQLITE_PATH") or "")
    if not data.parts:
        from app.config import settings

        data = settings.data_dir / "fact_memory.sqlite3"
    data.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data}"


def get_engine(url: str | None = None):
    global _engine, _SessionLocal
    if _engine is not None and url is None:
        return _engine
    db_url = url or database_url_from_env()
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(db_url, future=True, connect_args=connect_args)
    if db_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_fk(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    if url is None:
        _engine = engine
        _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_session_factory(url: str | None = None):
    global _SessionLocal
    if _SessionLocal is not None and url is None:
        return _SessionLocal
    engine = get_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    if url is None:
        _SessionLocal = factory
    return factory


def init_db(url: str | None = None) -> None:
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    ensure_source_columns(engine)
    ensure_analysis_job_columns(engine)
    ensure_audit_columns(engine)
    clear_source_enc_once(engine)


def clear_source_enc_once(engine) -> int | None:
    """Null leftover exhibit bytes once. Does not VACUUM FULL (that locks the table).

    Returns the number of rows cleared, or None when the marker already exists.
    """
    import logging

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    log = logging.getLogger(__name__)
    marker = "clear_source_enc"
    try:
        with engine.begin() as conn:
            found = conn.execute(
                text("SELECT id FROM schema_migrations WHERE id = :id"),
                {"id": marker},
            ).first()
            if found:
                return None
            result = conn.execute(
                text(
                    "UPDATE documents SET source_enc = NULL, source_missing = :missing "
                    "WHERE source_enc IS NOT NULL"
                ),
                {"missing": True},
            )
            count = int(result.rowcount or 0)
            conn.execute(
                text("INSERT INTO schema_migrations (id, applied_at) VALUES (:id, :ts)"),
                {"id": marker, "ts": _utcnow().isoformat()},
            )
    except IntegrityError:
        log.info("clear_source_enc already applied")
        return None
    log.info("clear_source_enc cleared %s document rows", count)
    return count


def ensure_source_columns(engine) -> None:
    """create_all does not add columns to an existing documents table."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "documents" not in insp.get_table_names():
        return
    have = {col["name"] for col in insp.get_columns("documents")}
    default_missing = "TRUE" if engine.dialect.name == "postgresql" else "1"
    stmts: list[str] = []
    if "source_enc" not in have:
        stmts.append("ALTER TABLE documents ADD COLUMN source_enc TEXT")
    if "mime_type" not in have:
        stmts.append("ALTER TABLE documents ADD COLUMN mime_type VARCHAR(128)")
    if "source_size" not in have:
        stmts.append("ALTER TABLE documents ADD COLUMN source_size INTEGER")
    if "source_missing" not in have:
        stmts.append(
            f"ALTER TABLE documents ADD COLUMN source_missing BOOLEAN DEFAULT {default_missing}"
        )
    if not stmts:
        return
    with engine.begin() as conn:
        for sql in stmts:
            conn.execute(text(sql))


def ensure_analysis_job_columns(engine) -> None:
    """Add missing columns to existing analysis_jobs tables."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "analysis_jobs" not in insp.get_table_names():
        return
    have = {col["name"] for col in insp.get_columns("analysis_jobs")}
    alters: list[str] = []
    if "restart_interruptions" not in have:
        alters.append(
            "ALTER TABLE analysis_jobs ADD COLUMN restart_interruptions "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "ai_review" not in have:
        alters.append("ALTER TABLE analysis_jobs ADD COLUMN ai_review VARCHAR(32)")
    if "ai_review_reason" not in have:
        alters.append(
            "ALTER TABLE analysis_jobs ADD COLUMN ai_review_reason VARCHAR(32)"
        )
    if "user_id" not in have:
        alters.append("ALTER TABLE analysis_jobs ADD COLUMN user_id VARCHAR(36)")
    if "original_filename" not in have:
        alters.append("ALTER TABLE analysis_jobs ADD COLUMN original_filename TEXT")
    if "display_name" not in have:
        alters.append("ALTER TABLE analysis_jobs ADD COLUMN display_name TEXT")
    if "doc_kind" not in have:
        alters.append("ALTER TABLE analysis_jobs ADD COLUMN doc_kind VARCHAR(64)")
    if not alters:
        return
    with engine.begin() as conn:
        for sql in alters:
            conn.execute(text(sql))


def ensure_audit_columns(engine) -> None:
    """create_all does not add columns to an existing audit_log table."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "audit_log" not in insp.get_table_names():
        return
    have = {col["name"] for col in insp.get_columns("audit_log")}
    if "request_id" in have:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log ADD COLUMN request_id VARCHAR(64)"))


def reset_engine() -> None:
    """Test helper — drop cached engine so a new URL can be used."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
