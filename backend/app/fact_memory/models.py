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
    # Full report JSON for reopen without job TTL (encrypted). No PDF bytes.
    report_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    matter: Mapped["Matter"] = relationship(back_populates="documents")
    facts: Mapped[list["Fact"]] = relationship(back_populates="document", cascade="all, delete-orphan")


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
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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


def reset_engine() -> None:
    """Test helper — drop cached engine so a new URL can be used."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
