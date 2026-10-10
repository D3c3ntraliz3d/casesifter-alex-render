"""Firm-scoped data-access layer for persistent fact memory.

Every read/write takes firm_id. Snapshots mirror vault cases_db shapes so
compare_to_case stays the cross-doc engine.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fact_memory import encryption as enc
from app.fact_memory.models import (
    AnalysisJob,
    AuditLog,
    Document,
    Entity,
    Fact,
    FindingRow,
    Firm,
    Matter,
    Run,
    User,
    get_session_factory,
    init_db,
    reset_engine,
)

logger = logging.getLogger(__name__)

_store_lock = threading.Lock()
_default_store: "FactMemoryStore | None" = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class FactMemoryStore:
    def __init__(self, session_factory=None, *, database_url: str | None = None):
        if database_url:
            reset_engine()
            init_db(database_url)
            self._sf = get_session_factory(database_url)
        else:
            init_db()
            self._sf = session_factory or get_session_factory()
        self.flag_missing_sources()

    def session(self) -> Session:
        return self._sf()

    # ── Firms / users ────────────────────────────────────────────────────

    def ensure_firm_for_email(self, email: str, *, name: str | None = None) -> dict[str, Any]:
        """1:1 firm per account email (soft-launch). Returns firm + user ids."""
        email_n = (email or "").strip().lower()
        if not email_n:
            raise ValueError("email required")
        with self.session() as s:
            user = s.execute(select(User).where(User.email == email_n)).scalar_one_or_none()
            if user:
                firm = s.get(Firm, user.firm_id)
                assert firm is not None
                return {
                    "firm_id": firm.id,
                    "user_id": user.id,
                    "email": user.email,
                    "persist_facts": bool(firm.persist_facts),
                    "store_snippets": bool(firm.store_snippets),
                    "name": firm.name,
                }
            firm_key = enc.generate_firm_key()
            firm = Firm(
                id=_new_id(),
                name=name or f"Firm ({email_n})",
                wrapped_data_key=enc.wrap_firm_key(firm_key),
                persist_facts=True,
                store_snippets=True,
            )
            user = User(id=_new_id(), firm_id=firm.id, email=email_n, role="owner")
            s.add(firm)
            s.add(user)
            s.commit()
            self._audit(s, firm.id, user.id, "firm.create", firm.id)
            s.commit()
            return {
                "firm_id": firm.id,
                "user_id": user.id,
                "email": user.email,
                "persist_facts": True,
                "store_snippets": True,
                "name": firm.name,
            }

    def get_firm(self, firm_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            firm = s.get(Firm, firm_id)
            if not firm:
                return None
            return {
                "id": firm.id,
                "name": firm.name,
                "persist_facts": bool(firm.persist_facts),
                "store_snippets": bool(firm.store_snippets),
                "retention_policy": firm.retention_policy,
                "created_at": firm.created_at.isoformat() if firm.created_at else None,
            }

    def set_persist_facts(self, firm_id: str, enabled: bool) -> None:
        with self.session() as s:
            firm = s.get(Firm, firm_id)
            if not firm:
                raise KeyError("firm not found")
            firm.persist_facts = bool(enabled)
            s.commit()

    def _fernet(self, s: Session, firm_id: str) -> Fernet:
        firm = s.get(Firm, firm_id)
        if not firm:
            raise KeyError("firm not found")
        return enc.firm_fernet(firm.wrapped_data_key)

    def _audit(
        self,
        s: Session,
        firm_id: str | None,
        user_id: str | None,
        action: str,
        target: str | None,
    ) -> None:
        s.add(
            AuditLog(
                firm_id=firm_id,
                user_id=user_id,
                action=action,
                target=target,
                at=_utcnow(),
            )
        )

    def audit(
        self,
        firm_id: str | None,
        user_id: str | None,
        action: str,
        target: str | None = None,
    ) -> None:
        with self.session() as s:
            self._audit(s, firm_id, user_id, action, target)
            s.commit()

    # ── Matters ──────────────────────────────────────────────────────────

    def create_matter(
        self,
        firm_id: str,
        *,
        name: str,
        index_number: str | None = None,
        court: str | None = None,
        county: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        with self.session() as s:
            if not s.get(Firm, firm_id):
                raise KeyError("firm not found")
            matter = Matter(
                id=_new_id(),
                firm_id=firm_id,
                name=(name or "Untitled matter").strip() or "Untitled matter",
                index_number=index_number,
                court=court,
                county=county,
                created_at=_utcnow(),
                last_activity_at=_utcnow(),
            )
            s.add(matter)
            self._audit(s, firm_id, user_id, "matter.create", matter.id)
            s.commit()
            return self._matter_dict(matter)

    def list_matters(self, firm_id: str) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(
                select(Matter).where(Matter.firm_id == firm_id).order_by(Matter.last_activity_at.desc())
            ).scalars().all()
            return [self._matter_dict(m) for m in rows]

    def get_matter(self, firm_id: str, matter_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            matter = s.execute(
                select(Matter).where(Matter.id == matter_id, Matter.firm_id == firm_id)
            ).scalar_one_or_none()
            return self._matter_dict(matter) if matter else None

    def delete_matter(self, firm_id: str, matter_id: str, *, user_id: str | None = None) -> bool:
        with self.session() as s:
            matter = s.execute(
                select(Matter).where(Matter.id == matter_id, Matter.firm_id == firm_id)
            ).scalar_one_or_none()
            if not matter:
                return False
            s.delete(matter)
            self._audit(s, firm_id, user_id, "matter.delete", matter_id)
            s.commit()
            return True

    @staticmethod
    def _matter_dict(m: Matter) -> dict[str, Any]:
        return {
            "id": m.id,
            "firm_id": m.firm_id,
            "name": m.name,
            "caption": m.name,  # frontend cases UI alias
            "index_number": m.index_number,
            "court": m.court,
            "county": m.county,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "last_activity_at": m.last_activity_at.isoformat() if m.last_activity_at else None,
            "updated_at": m.last_activity_at.isoformat() if m.last_activity_at else None,
        }

    # ── Documents / snapshots ────────────────────────────────────────────

    def get_document_by_sha(self, firm_id: str, matter_id: str, sha256: str) -> dict[str, Any] | None:
        with self.session() as s:
            doc = s.execute(
                select(Document).where(
                    Document.firm_id == firm_id,
                    Document.matter_id == matter_id,
                    Document.sha256 == sha256,
                )
            ).scalar_one_or_none()
            if not doc:
                return None
            fernet = self._fernet(s, firm_id)
            return self._document_dict(doc, fernet)

    def list_documents(self, firm_id: str, matter_id: str) -> list[dict[str, Any]]:
        with self.session() as s:
            if not s.execute(
                select(Matter).where(Matter.id == matter_id, Matter.firm_id == firm_id)
            ).scalar_one_or_none():
                return []
            fernet = self._fernet(s, firm_id)
            rows = s.execute(
                select(Document).where(Document.firm_id == firm_id, Document.matter_id == matter_id)
            ).scalars().all()
            return [self._document_dict(d, fernet) for d in rows]

    def update_document_filed_date(
        self,
        firm_id: str,
        document_id: str,
        filed_date: date | str | None,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.session() as s:
            doc = s.execute(
                select(Document).where(Document.id == document_id, Document.firm_id == firm_id)
            ).scalar_one_or_none()
            if not doc:
                return None
            if isinstance(filed_date, str):
                filed_date = date.fromisoformat(filed_date) if filed_date else None
            doc.filed_date = filed_date
            self._audit(s, firm_id, user_id, "document.update_filed_date", document_id)
            s.commit()
            fernet = self._fernet(s, firm_id)
            return self._document_dict(doc, fernet)

    def update_document_labels(
        self,
        firm_id: str,
        document_id: str,
        *,
        display_name: str | None = None,
        doc_type: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.session() as s:
            doc = s.execute(
                select(Document).where(Document.id == document_id, Document.firm_id == firm_id)
            ).scalar_one_or_none()
            if not doc:
                return None
            fernet = self._fernet(s, firm_id)
            if display_name is not None:
                doc.display_name_enc = enc.encrypt_text(fernet, display_name)
            if doc_type is not None:
                doc.doc_type = doc_type or None
            self._audit(s, firm_id, user_id, "document.retitle", document_id)
            s.commit()
            return self._document_dict(doc, fernet)

    def delete_document(self, firm_id: str, document_id: str, *, user_id: str | None = None) -> bool:
        """Hard delete document + cascade facts; mark findings that referenced it stale."""
        with self.session() as s:
            doc = s.execute(
                select(Document).where(Document.id == document_id, Document.firm_id == firm_id)
            ).scalar_one_or_none()
            if not doc:
                return False
            matter_id = doc.matter_id
            # Stale findings that cite this document's sha/id in evidence.
            findings = s.execute(
                select(FindingRow).where(
                    FindingRow.firm_id == firm_id,
                    FindingRow.matter_id == matter_id,
                    FindingRow.status == "open",
                )
            ).scalars().all()
            fernet = self._fernet(s, firm_id)
            for finding in findings:
                evidence = enc.decrypt_json(fernet, finding.evidence_enc) or {}
                refs = evidence.get("refs") or []
                if any(
                    (r.get("document_id") == document_id) or (r.get("sha256") == doc.sha256)
                    for r in refs
                    if isinstance(r, dict)
                ):
                    finding.status = "stale"
            s.delete(doc)
            self._audit(s, firm_id, user_id, "document.delete", document_id)
            s.commit()
            return True

    def _document_dict(self, doc: Document, fernet: Fernet) -> dict[str, Any]:
        risk_level = None
        risk_score = None
        file_size_bytes = None
        if doc.report_enc:
            try:
                report = enc.decrypt_json(fernet, doc.report_enc) or {}
                if isinstance(report, dict):
                    risk_level = report.get("risk_level")
                    risk_score = report.get("risk_score")
                    audit = report.get("audit") if isinstance(report.get("audit"), dict) else {}
                    file_size_bytes = audit.get("file_size_bytes")
            except Exception:
                logger.debug("Could not decrypt report risk for doc %s", doc.id, exc_info=True)
        return {
            "id": doc.id,
            "matter_id": doc.matter_id,
            "firm_id": doc.firm_id,
            "sha256": doc.sha256,
            "original_filename": enc.decrypt_text(fernet, doc.original_filename_enc),
            "display_name": enc.decrypt_text(fernet, doc.display_name_enc),
            "doc_type": doc.doc_type,
            "doc_kind": doc.doc_type,
            "filed_date": doc.filed_date.isoformat() if doc.filed_date else None,
            "page_count": doc.page_count,
            "scanned_at": doc.scanned_at.isoformat() if doc.scanned_at else None,
            "extractor_version": doc.extractor_version,
            "status": "complete",
            "scan_job_id": doc.last_job_id,
            "has_report": bool(doc.report_enc),
            "has_source": bool(doc.source_enc) and not doc.source_missing,
            "source_missing": bool(doc.source_missing) or not doc.source_enc,
            "mime_type": doc.mime_type,
            "source_size": doc.source_size,
            "risk_level": risk_level,
            "risk_score": risk_score,
            "file_size_bytes": file_size_bytes,
        }

    def list_prior_snapshots(
        self, firm_id: str, matter_id: str, *, exclude_document_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Vault-compatible prior snapshots for compare_to_case."""
        with self.session() as s:
            if not s.execute(
                select(Matter).where(Matter.id == matter_id, Matter.firm_id == firm_id)
            ).scalar_one_or_none():
                return []
            fernet = self._fernet(s, firm_id)
            q = select(Document).where(Document.firm_id == firm_id, Document.matter_id == matter_id)
            if exclude_document_id:
                q = q.where(Document.id != exclude_document_id)
            rows = s.execute(q).scalars().all()
            out: list[dict[str, Any]] = []
            for doc in rows:
                snap = enc.decrypt_json(fernet, doc.snapshot_enc) or {}
                if not snap:
                    # Rebuild minimal snapshot from facts table.
                    facts = self._facts_for_document(s, fernet, firm_id, doc.id)
                    snap = {
                        "document_id": doc.id,
                        "display_name": enc.decrypt_text(fernet, doc.display_name_enc),
                        "original_filename": enc.decrypt_text(fernet, doc.original_filename_enc),
                        "doc_kind": doc.doc_type,
                        "sha256": doc.sha256,
                        "facts": facts,
                        "entities": {},
                        "text_excerpt": "",
                        "text_fingerprint": None,
                    }
                else:
                    snap.setdefault("document_id", doc.id)
                    snap.setdefault("sha256", doc.sha256)
                    snap.setdefault("doc_kind", doc.doc_type)
                snap["filed_date"] = doc.filed_date.isoformat() if doc.filed_date else None
                out.append(snap)
            return out

    def get_document_report(
        self, firm_id: str, matter_id: str, document_id: str
    ) -> dict[str, Any] | None:
        with self.session() as s:
            doc = s.execute(
                select(Document).where(
                    Document.id == document_id,
                    Document.firm_id == firm_id,
                    Document.matter_id == matter_id,
                )
            ).scalar_one_or_none()
            if not doc or not doc.report_enc:
                return None
            fernet = self._fernet(s, firm_id)
            return enc.decrypt_json(fernet, doc.report_enc)

    def get_document(
        self, firm_id: str, document_id: str
    ) -> dict[str, Any] | None:
        with self.session() as s:
            doc = s.execute(
                select(Document).where(Document.id == document_id, Document.firm_id == firm_id)
            ).scalar_one_or_none()
            if not doc:
                return None
            fernet = self._fernet(s, firm_id)
            return self._document_dict(doc, fernet)

    def _facts_for_document(
        self, s: Session, fernet: Fernet, firm_id: str, document_id: str
    ) -> list[dict[str, Any]]:
        rows = s.execute(
            select(Fact).where(Fact.firm_id == firm_id, Fact.document_id == document_id)
        ).scalars().all()
        facts = []
        for row in rows:
            blob = enc.decrypt_json(fernet, row.fact_blob_enc)
            if isinstance(blob, dict):
                facts.append(blob)
            else:
                facts.append(
                    {
                        "key": row.attribute,
                        "value": enc.decrypt_text(fernet, row.value_normalized_enc)
                        or enc.decrypt_text(fernet, row.value_enc),
                        "display": enc.decrypt_text(fernet, row.value_enc),
                        "page": row.page,
                        "quote": enc.decrypt_text(fernet, row.snippet_enc),
                    }
                )
        return facts

    def recompute_facts_from_excerpts(
        self,
        firm_id: str,
        matter_id: str,
        *,
        user_id: str | None = None,
        progress: Callable[..., None] | None = None,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        """Re-extract facts from stored text_excerpt (no PDFs) and refresh snapshots.

        Processes documents in small committed batches so RSS stays bounded on
        512 MB instances. Intended to run in a child Process (not the web worker).
        """
        import gc

        from app.case_consistency import aggregate_fact_conflicts
        from app.config import settings
        from app.fact_extract import extract_facts
        from app.resource import rss_mb

        batch = max(1, int(batch_size or getattr(settings, "recompute_batch_size", 4) or 4))
        mem_guard = float(
            max(200, int(getattr(settings, "recompute_memory_guard_mb", 350) or 350))
        )

        before_snaps = self.list_prior_snapshots(firm_id, matter_id)
        before = [
            {
                "field": c.get("field"),
                "title": c.get("title") or c.get("label"),
                "severity": c.get("severity"),
                "discrepancy": c.get("discrepancy"),
            }
            for c in aggregate_fact_conflicts(before_snaps)
            if str(c.get("severity") or "") in {"high", "medium", "critical"}
        ]
        # Drop large snap list before the per-doc loop.
        before_snaps = []
        gc.collect()

        with self.session() as s:
            if not s.execute(
                select(Matter).where(Matter.id == matter_id, Matter.firm_id == firm_id)
            ).scalar_one_or_none():
                raise KeyError("matter not found")
            firm = s.get(Firm, firm_id)
            store_snippets = bool(firm.store_snippets) if firm is not None else True
            doc_ids = list(
                s.execute(
                    select(Document.id).where(
                        Document.firm_id == firm_id, Document.matter_id == matter_id
                    )
                )
                .scalars()
                .all()
            )

        total = len(doc_ids)
        updated = 0
        skipped = 0
        if progress is not None:
            progress(done=0, total=total, updated=0, skipped=0, phase="docs")

        for batch_start in range(0, total, batch):
            chunk_ids = doc_ids[batch_start : batch_start + batch]
            with self.session() as s:
                fernet = self._fernet(s, firm_id)
                docs = (
                    s.execute(select(Document).where(Document.id.in_(chunk_ids)))
                    .scalars()
                    .all()
                )
                by_id = {d.id: d for d in docs}
                for offset, doc_id in enumerate(chunk_ids):
                    idx = batch_start + offset + 1
                    doc = by_id.get(doc_id)
                    if doc is None:
                        skipped += 1
                        if progress is not None:
                            progress(
                                done=idx,
                                total=total,
                                updated=updated,
                                skipped=skipped,
                                phase="docs",
                            )
                        continue
                    snap = enc.decrypt_json(fernet, doc.snapshot_enc) or {}
                    text = str(snap.get("text_excerpt") or "").strip()
                    if not text:
                        skipped += 1
                        if progress is not None:
                            progress(
                                done=idx,
                                total=total,
                                updated=updated,
                                skipped=skipped,
                                phase="docs",
                            )
                        continue
                    facts = extract_facts(text)
                    for fact in facts:
                        if isinstance(fact, dict) and not fact.get("document_id"):
                            fact["document_id"] = doc.id
                    snap["facts"] = facts
                    snap.setdefault("document_id", doc.id)
                    doc.snapshot_enc = enc.encrypt_json(fernet, snap)
                    for old in list(doc.facts):
                        s.delete(old)
                    s.flush()
                    for fact in facts:
                        if not isinstance(fact, dict):
                            continue
                        attr = str(fact.get("key") or fact.get("attribute") or "unknown")
                        value = fact.get("display") or fact.get("value")
                        value_norm = fact.get("value")
                        snippet = None
                        if store_snippets:
                            raw_snip = fact.get("quote") or fact.get("snippet") or ""
                            snippet = str(raw_snip)[:300] if raw_snip else None
                        s.add(
                            Fact(
                                id=_new_id(),
                                matter_id=matter_id,
                                firm_id=firm_id,
                                document_id=doc.id,
                                entity_id=None,
                                attribute=attr,
                                value_enc=enc.encrypt_text(
                                    fernet, None if value is None else str(value)
                                ),
                                value_normalized_enc=enc.encrypt_text(
                                    fernet,
                                    None if value_norm is None else str(value_norm),
                                ),
                                page=fact.get("page"),
                                snippet_enc=enc.encrypt_text(fernet, snippet),
                                confidence=float(fact.get("confidence") or 0) or None,
                                created_at=_utcnow(),
                                fact_blob_enc=enc.encrypt_json(fernet, fact),
                            )
                        )
                    updated += 1
                    if progress is not None:
                        progress(
                            done=idx,
                            total=total,
                            updated=updated,
                            skipped=skipped,
                            phase="docs",
                        )
                s.commit()

            gc.collect()
            rss = rss_mb()
            if rss is not None and rss >= mem_guard:
                logger.warning(
                    "recompute memory pause matter=%s rss_mb=%s guard=%s — gc wait",
                    matter_id[:8],
                    rss,
                    mem_guard,
                )
                time.sleep(0.5)
                gc.collect()
                rss2 = rss_mb()
                if rss2 is not None and rss2 >= mem_guard:
                    # Soft wait — do not fail the job; let RSS settle.
                    time.sleep(2.0)
                    gc.collect()

        if progress is not None:
            progress(
                done=total,
                total=total,
                updated=updated,
                skipped=skipped,
                phase="aggregate",
            )
        with self.session() as s:
            findings = s.execute(
                select(FindingRow).where(
                    FindingRow.firm_id == firm_id,
                    FindingRow.matter_id == matter_id,
                    FindingRow.status == "open",
                )
            ).scalars().all()
            for finding in findings:
                if str(finding.finding_type or "") == "fact_conflict":
                    finding.status = "stale"
            self._audit(s, firm_id, user_id, "matter.recompute_facts", matter_id)
            s.commit()

        after_snaps = self.list_prior_snapshots(firm_id, matter_id)
        after = [
            {
                "field": c.get("field"),
                "title": c.get("title") or c.get("label"),
                "severity": c.get("severity"),
                "discrepancy": c.get("discrepancy"),
            }
            for c in aggregate_fact_conflicts(after_snaps)
            if str(c.get("severity") or "") in {"high", "medium", "critical"}
        ]
        return {
            "updated": updated,
            "skipped_no_excerpt": skipped,
            "before": before,
            "after": after,
        }

    def persist_scan(
        self,
        *,
        firm_id: str,
        matter_id: str,
        job_id: str,
        sha256: str,
        original_filename: str,
        display_name: str | None,
        doc_type: str | None,
        filed_date: date | str | None,
        page_count: int | None,
        entities: dict[str, Any],
        facts: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        text_fingerprint: str | None,
        text_excerpt: str | None,
        page_offsets: list[dict[str, Any]] | None,
        amounts: list[dict[str, Any]] | None = None,
        extractor_version: str | None = None,
        user_id: str | None = None,
        force: bool = False,
        report: dict[str, Any] | None = None,
        source_bytes: bytes | None = None,
        mime_type: str | None = None,
        source_size: int | None = None,
    ) -> dict[str, Any]:
        """Persist facts, findings, and exhibit metadata. Exhibit bytes are not stored."""
        byte_length = None
        if source_size is not None:
            try:
                byte_length = int(source_size)
            except (TypeError, ValueError):
                byte_length = None
            if byte_length is not None and byte_length < 0:
                byte_length = None
        with self.session() as s:
            firm = s.get(Firm, firm_id)
            if not firm:
                raise KeyError("firm not found")
            if not firm.persist_facts:
                return {"persisted": False, "reason": "persist_facts=false"}
            matter = s.execute(
                select(Matter).where(Matter.id == matter_id, Matter.firm_id == firm_id)
            ).scalar_one_or_none()
            if not matter:
                raise KeyError("matter not found")
            fernet = self._fernet(s, firm_id)
            store_snippets = bool(firm.store_snippets)

            existing = s.execute(
                select(Document).where(
                    Document.firm_id == firm_id,
                    Document.matter_id == matter_id,
                    Document.sha256 == sha256,
                )
            ).scalar_one_or_none()
            if existing and not force:
                # Still record a run pointing at existing doc; refresh report if provided.
                if report is not None:
                    existing.report_enc = enc.encrypt_json(fernet, report)
                    existing.last_job_id = job_id
                if byte_length is not None:
                    existing.source_size = byte_length
                run = Run(
                    id=_new_id(),
                    matter_id=matter_id,
                    firm_id=firm_id,
                    job_id=job_id,
                    started_at=_utcnow(),
                    finished_at=_utcnow(),
                    status="complete",
                    document_ids_json=json.dumps([existing.id]),
                )
                s.add(run)
                matter.last_activity_at = _utcnow()
                s.commit()
                return {
                    "persisted": True,
                    "deduped": True,
                    "document_id": existing.id,
                    "run_id": run.id,
                }

            if isinstance(filed_date, str):
                filed_date = date.fromisoformat(filed_date) if filed_date else None

            doc = existing or Document(
                id=_new_id(),
                matter_id=matter_id,
                firm_id=firm_id,
                sha256=sha256,
            )
            doc.original_filename_enc = enc.encrypt_text(fernet, original_filename)
            doc.display_name_enc = enc.encrypt_text(
                fernet, display_name or original_filename
            )
            doc.doc_type = doc_type
            doc.filed_date = filed_date
            doc.page_count = page_count
            doc.scanned_at = _utcnow()
            doc.extractor_version = extractor_version
            doc.last_job_id = job_id
            if byte_length is not None:
                doc.source_size = byte_length
            if report is not None:
                doc.report_enc = enc.encrypt_json(fernet, report)

            # Cap excerpt for vault-compat priors (encrypted). Prefer empty if firm opts out of snippets.
            excerpt = (text_excerpt or "")[:24000] if store_snippets else ""
            snapshot = {
                "document_id": doc.id,
                "display_name": display_name or original_filename,
                "original_filename": original_filename,
                "doc_kind": doc_type,
                "sha256": sha256,
                "entities": entities or {},
                "facts": facts or [],
                "text_excerpt": excerpt,
                "text_fingerprint": text_fingerprint,
                "page_offsets": page_offsets or [],
                "amounts": amounts or [],
                "findings_summary": [
                    {"id": f.get("id"), "severity": f.get("severity"), "title": f.get("title")}
                    for f in (findings or [])[:40]
                    if isinstance(f, dict)
                ],
            }
            doc.snapshot_enc = enc.encrypt_json(fernet, snapshot)
            if not existing:
                s.add(doc)
            else:
                # Replace facts for re-scan
                for old in list(doc.facts):
                    s.delete(old)
            s.flush()  # document id must exist for facts FK

            # Resolve / upsert entities from people/orgs lists
            entity_map = self._upsert_entities(s, fernet, firm_id, matter_id, entities or {})

            for fact in facts or []:
                if not isinstance(fact, dict):
                    continue
                attr = str(fact.get("key") or fact.get("attribute") or "unknown")
                value = fact.get("display") or fact.get("value")
                value_norm = fact.get("value")
                snippet = None
                if store_snippets:
                    raw_snip = fact.get("quote") or fact.get("snippet") or ""
                    snippet = str(raw_snip)[:300] if raw_snip else None
                entity_id = None
                ent_name = fact.get("entity") or fact.get("subject")
                if ent_name and str(ent_name).lower() in entity_map:
                    entity_id = entity_map[str(ent_name).lower()]
                row = Fact(
                    id=_new_id(),
                    matter_id=matter_id,
                    firm_id=firm_id,
                    document_id=doc.id,
                    entity_id=entity_id,
                    attribute=attr,
                    value_enc=enc.encrypt_text(fernet, None if value is None else str(value)),
                    value_normalized_enc=enc.encrypt_text(
                        fernet, None if value_norm is None else str(value_norm)
                    ),
                    page=fact.get("page"),
                    snippet_enc=enc.encrypt_text(fernet, snippet),
                    confidence=float(fact.get("confidence") or 0) or None,
                    created_at=_utcnow(),
                    fact_blob_enc=enc.encrypt_json(fernet, fact),
                )
                s.add(row)

            run = Run(
                id=_new_id(),
                matter_id=matter_id,
                firm_id=firm_id,
                job_id=job_id,
                started_at=_utcnow(),
                finished_at=_utcnow(),
                status="complete",
                document_ids_json=json.dumps([doc.id]),
            )
            s.add(run)
            s.flush()  # ensure run/doc rows exist before findings FK inserts

            # Suppress re-emitting the same material interrupt on later uploads.
            from app.case_consistency import finding_stable_key

            existing_keys: set[tuple[str, ...]] = set()
            prior_findings = s.execute(
                select(FindingRow).where(
                    FindingRow.firm_id == firm_id,
                    FindingRow.matter_id == matter_id,
                    FindingRow.status == "open",
                )
            ).scalars().all()
            for prior in prior_findings:
                prior_ev = enc.decrypt_json(fernet, prior.evidence_enc) or {}
                prior_item = {
                    "id": prior.id,
                    "finding_type": prior.finding_type,
                    "title": enc.decrypt_text(fernet, prior.title_enc),
                    "explanation": enc.decrypt_text(fernet, prior.description_enc),
                    "evidence": prior_ev,
                    "field": prior_ev.get("field"),
                    "fact_named": prior_ev.get("fact_named"),
                    "claim_quote": prior_ev.get("claim_quote"),
                    "evidence_quote": prior_ev.get("evidence_quote"),
                }
                existing_keys.add(finding_stable_key(prior_item))

            stored_findings = 0
            for finding in findings or []:
                if not isinstance(finding, dict):
                    continue
                ftype = (
                    (finding.get("evidence") or {}).get("analysis_type")
                    or finding.get("category")
                    or "finding"
                )
                key = finding_stable_key(
                    {
                        **finding,
                        "finding_type": ftype,
                        "field": (finding.get("evidence") or {}).get("field"),
                        "fact_named": (finding.get("evidence") or {}).get("fact_named"),
                        "claim_quote": (finding.get("evidence") or {}).get("claim_quote"),
                        "evidence_quote": (finding.get("evidence") or {}).get("evidence_quote"),
                    }
                )
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                # Always mint our own finding PK — pipeline ids may exceed varchar(36)
                # or collide across runs.
                row = FindingRow(
                    id=_new_id(),
                    matter_id=matter_id,
                    firm_id=firm_id,
                    run_id=run.id,
                    finding_type=str(ftype)[:128],
                    severity=str(finding.get("severity") or "info")[:32],
                    confidence=float(finding.get("confidence") or 0) or None,
                    description_enc=enc.encrypt_text(fernet, finding.get("explanation")),
                    title_enc=enc.encrypt_text(fernet, finding.get("title")),
                    fact_ids_json=json.dumps(finding.get("fact_ids") or []),
                    evidence_enc=enc.encrypt_json(fernet, finding.get("evidence") or {}),
                    status="open",
                    created_at=_utcnow(),
                )
                s.add(row)
                stored_findings += 1

            matter.last_activity_at = _utcnow()
            self._audit(s, firm_id, user_id, "document.scan", doc.id)
            s.commit()
            return {
                "persisted": True,
                "deduped": False,
                "document_id": doc.id,
                "run_id": run.id,
                "fact_count": len(facts or []),
                "finding_count": stored_findings,
            }

    def _upsert_entities(
        self,
        s: Session,
        fernet: Fernet,
        firm_id: str,
        matter_id: str,
        entities: dict[str, Any],
    ) -> dict[str, str]:
        """Return lower(name) → entity_id map."""
        existing = s.execute(
            select(Entity).where(Entity.firm_id == firm_id, Entity.matter_id == matter_id)
        ).scalars().all()
        by_name: dict[str, str] = {}
        for ent in existing:
            name = enc.decrypt_text(fernet, ent.canonical_name_enc) or ""
            if name:
                by_name[name.lower()] = ent.id
            aliases = enc.decrypt_json(fernet, ent.aliases_enc) or []
            for a in aliases:
                if a:
                    by_name[str(a).lower()] = ent.id

        def _add(etype: str, names: list):
            for name in names or []:
                if not name or not str(name).strip():
                    continue
                key = str(name).strip().lower()
                if key in by_name:
                    continue
                ent = Entity(
                    id=_new_id(),
                    matter_id=matter_id,
                    firm_id=firm_id,
                    entity_type=etype,
                    canonical_name_enc=enc.encrypt_text(fernet, str(name).strip()),
                    aliases_enc=enc.encrypt_json(fernet, [str(name).strip()]),
                )
                s.add(ent)
                by_name[key] = ent.id

        _add("person", entities.get("people") or [])
        _add("org", entities.get("organizations") or [])
        _add("property", entities.get("locations") or [])
        return by_name

    def list_findings(
        self, firm_id: str, matter_id: str, *, include_stale: bool = False
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            if not s.execute(
                select(Matter).where(Matter.id == matter_id, Matter.firm_id == firm_id)
            ).scalar_one_or_none():
                return []
            fernet = self._fernet(s, firm_id)
            q = select(FindingRow).where(
                FindingRow.firm_id == firm_id, FindingRow.matter_id == matter_id
            )
            if not include_stale:
                q = q.where(FindingRow.status != "stale")
            rows = s.execute(q.order_by(FindingRow.created_at.desc())).scalars().all()
            out = []
            for row in rows:
                out.append(
                    {
                        "id": row.id,
                        "matter_id": matter_id,
                        "run_id": row.run_id,
                        "finding_type": row.finding_type,
                        "severity": row.severity,
                        "confidence": row.confidence,
                        "title": enc.decrypt_text(fernet, row.title_enc),
                        "explanation": enc.decrypt_text(fernet, row.description_enc),
                        "evidence": enc.decrypt_json(fernet, row.evidence_enc) or {},
                        "status": row.status,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                )
            return out

    def export_matter(self, firm_id: str, matter_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        matter = self.get_matter(firm_id, matter_id)
        if not matter:
            raise KeyError("matter not found")
        docs = self.list_documents(firm_id, matter_id)
        snaps = self.list_prior_snapshots(firm_id, matter_id)
        findings = self.list_findings(firm_id, matter_id, include_stale=True)
        self.audit(firm_id, user_id, "matter.export", matter_id)
        return {
            "matter": matter,
            "documents": docs,
            "snapshots": [
                {
                    "document_id": s.get("document_id"),
                    "sha256": s.get("sha256"),
                    "display_name": s.get("display_name"),
                    "doc_kind": s.get("doc_kind"),
                    "filed_date": s.get("filed_date"),
                    "facts": s.get("facts") or [],
                    "entities": s.get("entities") or {},
                    # Omit long text_excerpt from export by default? Keep facts only for confidentiality.
                }
                for s in snaps
            ],
            "findings": findings,
            "exported_at": _utcnow().isoformat(),
        }

    def load_document_source(
        self, firm_id: str, matter_id: str, document_id: str
    ) -> dict[str, Any] | None:
        """Exhibit bytes are not stored. Callers get nothing to decrypt."""
        return None

    def save_document_source(
        self,
        firm_id: str,
        matter_id: str,
        document_id: str,
        source_bytes: bytes,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Exhibit bytes are not stored."""
        return None

    def flag_missing_sources(self) -> int:
        """Mark reports whose original bytes were never stored."""
        with self.session() as s:
            rows = s.execute(select(Document).where(Document.source_enc.is_(None))).scalars().all()
            changed = 0
            for doc in rows:
                if not doc.source_missing:
                    doc.source_missing = True
                    changed += 1
            if changed:
                s.commit()
            return changed

    def source_status_for_job(self, job_id: str) -> dict[str, Any]:
        with self.session() as s:
            doc = s.execute(
                select(Document).where(Document.last_job_id == job_id)
            ).scalar_one_or_none()
            if doc is None:
                return {"job_id": job_id, "status": "unknown"}
            if doc.source_enc and not doc.source_missing:
                return {
                    "job_id": job_id,
                    "document_id": doc.id,
                    "status": "present",
                    "sha256": doc.sha256,
                }
            doc.source_missing = True
            s.commit()
            return {
                "job_id": job_id,
                "document_id": doc.id,
                "status": "source_missing",
                "sha256": doc.sha256,
            }

    def purge_firm(self, firm_id: str, *, user_id: str | None = None) -> bool:
        with self.session() as s:
            firm = s.get(Firm, firm_id)
            if not firm:
                return False
            self._audit(s, firm_id, user_id, "firm.purge", firm_id)
            s.delete(firm)
            s.commit()
            return True

    # ── Durable analysis jobs (survive ephemeral disk wipe) ───────────────

    def upsert_analysis_job(
        self,
        *,
        job_id: str,
        firm_id: str,
        matter_id: str | None = None,
        document_id: str | None = None,
        sha256: str = "",
        status: str = "queued",
        message: str | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        now = _utcnow()
        with self.session() as s:
            row = s.get(AnalysisJob, job_id)
            if row is None:
                row = AnalysisJob(
                    job_id=job_id,
                    firm_id=firm_id,
                    matter_id=matter_id or None,
                    document_id=document_id or None,
                    sha256=(sha256 or "").lower(),
                    status=status or "queued",
                    attempts=int(attempts) if attempts is not None else 1,
                    message=message,
                    error=error,
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
            else:
                if matter_id is not None:
                    row.matter_id = matter_id or None
                if document_id is not None:
                    row.document_id = document_id or None
                if sha256:
                    row.sha256 = sha256.lower()
                prev_status = row.status
                prev_error = str(row.error or "")
                if attempts is not None:
                    row.attempts = int(attempts)
                elif status == "queued" and prev_status in {"interrupted", "failed"}:
                    # Instance restarts must not burn an attempt when the job resumes.
                    if prev_error not in {
                        "interrupted_by_restart",
                        "local_done_missing_artifacts",
                    }:
                        row.attempts = int(row.attempts or 0) + 1
                if message is not None:
                    row.message = message
                if error is not None:
                    row.error = error
                elif status == "queued":
                    row.error = None
                row.status = status or row.status
                row.updated_at = now
            s.commit()
            return self._analysis_job_dict(row)

    def update_analysis_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        document_id: str | None = None,
        message: str | None = None,
        error: str | None = None,
        sha256: str | None = None,
        attempts: int | None = None,
    ) -> dict[str, Any] | None:
        terminal = frozenset({"complete", "failed", "interrupted"})
        with self.session() as s:
            row = s.get(AnalysisJob, job_id)
            if row is None:
                return None
            # Allow recovery from restart interrupt (resume / mark complete).
            restart_resume = (
                row.status == "interrupted"
                and str(row.error or "")
                in {"interrupted_by_restart", "local_done_missing_artifacts"}
                and status
                in {"queued", "running", "local_done", "complete"}
            )
            if (
                status is not None
                and row.status in terminal
                and status != row.status
                and not restart_resume
            ):
                # Sticky terminal — refuse overwrite (A3).
                return self._analysis_job_dict(row)
            if status is not None:
                row.status = status
            if document_id is not None:
                row.document_id = document_id or None
            if message is not None:
                row.message = message
            if error is not None:
                row.error = error
            if sha256:
                row.sha256 = sha256.lower()
            if attempts is not None:
                row.attempts = int(attempts)
            row.updated_at = _utcnow()
            s.commit()
            return self._analysis_job_dict(row)

    def find_active_analysis_job(
        self,
        *,
        firm_id: str,
        matter_id: str,
        sha256: str,
    ) -> dict[str, Any] | None:
        """Return queued/running/local_done job for matter+sha if any (server dedupe)."""
        digest = (sha256 or "").lower().strip()
        if not digest or not matter_id:
            return None
        active = ("queued", "running", "local_done")
        with self.session() as s:
            row = (
                s.execute(
                    select(AnalysisJob)
                    .where(AnalysisJob.firm_id == firm_id)
                    .where(AnalysisJob.matter_id == matter_id)
                    .where(AnalysisJob.sha256 == digest)
                    .where(AnalysisJob.status.in_(active))
                    .order_by(AnalysisJob.created_at.desc())
                )
                .scalars()
                .first()
            )
            return self._analysis_job_dict(row) if row else None

    def max_attempts_for_sha(
        self,
        *,
        firm_id: str,
        matter_id: str,
        sha256: str,
    ) -> int:
        """Highest attempts count across all durable jobs for matter+sha256."""
        digest = (sha256 or "").lower().strip()
        if not digest or not matter_id:
            return 0
        with self.session() as s:
            rows = (
                s.execute(
                    select(AnalysisJob)
                    .where(AnalysisJob.firm_id == firm_id)
                    .where(AnalysisJob.matter_id == matter_id)
                    .where(AnalysisJob.sha256 == digest)
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0
            return max(int(r.attempts or 0) for r in rows)

    def latest_job_for_sha(
        self,
        *,
        firm_id: str,
        matter_id: str,
        sha256: str,
    ) -> dict[str, Any] | None:
        digest = (sha256 or "").lower().strip()
        if not digest or not matter_id:
            return None
        with self.session() as s:
            row = (
                s.execute(
                    select(AnalysisJob)
                    .where(AnalysisJob.firm_id == firm_id)
                    .where(AnalysisJob.matter_id == matter_id)
                    .where(AnalysisJob.sha256 == digest)
                    .order_by(AnalysisJob.updated_at.desc())
                )
                .scalars()
                .first()
            )
            return self._analysis_job_dict(row) if row else None

    def list_exhausted_jobs(
        self,
        *,
        firm_id: str,
        matter_id: str,
        max_attempts: int = 3,
    ) -> list[dict[str, Any]]:
        """Failed/interrupted jobs that have used up auto-retry attempts."""
        with self.session() as s:
            rows = (
                s.execute(
                    select(AnalysisJob)
                    .where(AnalysisJob.firm_id == firm_id)
                    .where(AnalysisJob.matter_id == matter_id)
                    .where(AnalysisJob.status.in_(("failed", "interrupted")))
                    .order_by(AnalysisJob.updated_at.desc())
                )
                .scalars()
                .all()
            )
            out: list[dict[str, Any]] = []
            seen_sha: set[str] = set()
            for row in rows:
                sha = (row.sha256 or "").lower()
                if not sha or sha in seen_sha:
                    continue
                quarantined = (
                    str(row.error or "") == "restart_quarantine"
                    or int(getattr(row, "restart_interruptions", 0) or 0) >= 2
                )
                if int(row.attempts or 0) < max_attempts and not quarantined:
                    continue
                seen_sha.add(sha)
                out.append(self._analysis_job_dict(row))
            return out

    def list_retryable_failed_jobs(
        self,
        *,
        firm_id: str,
        matter_id: str,
        max_attempts: int = 3,
    ) -> list[dict[str, Any]]:
        """Failed/interrupted jobs eligible for auto-retry (stall/timeout/interrupted).

        Attempts are enforced per matter+sha256 on the server. Backpressure waits
        never fail a job, so they are not retry-eligible.
        """
        active_statuses = ("queued", "running", "local_done")
        with self.session() as s:
            all_rows = (
                s.execute(
                    select(AnalysisJob)
                    .where(AnalysisJob.firm_id == firm_id)
                    .where(AnalysisJob.matter_id == matter_id)
                    .order_by(AnalysisJob.updated_at.desc())
                )
                .scalars()
                .all()
            )
            max_by_sha: dict[str, int] = {}
            max_restarts_by_sha: dict[str, int] = {}
            active_shas: set[str] = set()
            for row in all_rows:
                sha = (row.sha256 or "").lower()
                if not sha:
                    continue
                max_by_sha[sha] = max(max_by_sha.get(sha, 0), int(row.attempts or 0))
                max_restarts_by_sha[sha] = max(
                    max_restarts_by_sha.get(sha, 0),
                    int(getattr(row, "restart_interruptions", 0) or 0),
                )
                if row.status in active_statuses:
                    active_shas.add(sha)
            out: list[dict[str, Any]] = []
            seen_sha: set[str] = set()
            for row in all_rows:
                if row.status not in ("failed", "interrupted"):
                    continue
                sha = (row.sha256 or "").lower()
                if not sha or sha in seen_sha or sha in active_shas:
                    continue
                attempts = max_by_sha.get(sha, int(row.attempts or 0))
                restart_count = max_restarts_by_sha.get(
                    sha, int(getattr(row, "restart_interruptions", 0) or 0)
                )
                # Crash-loop guard: same file running at 2+ restarts → no auto-retry.
                if (
                    restart_count >= 2
                    or str(row.error or "") == "restart_quarantine"
                ):
                    continue
                restart_err = str(row.error or "") in {
                    "interrupted_by_restart",
                    "local_done_missing_artifacts",
                }
                # Restart interruptions remain retryable even at the attempt cap —
                # resuming must not burn a slot (see create job next_attempts).
                if attempts >= max_attempts and not restart_err:
                    continue
                err = f"{row.error or ''} {row.message or ''}".lower()
                if "backpressure" in err:
                    continue
                eligible = (
                    restart_err
                    or row.status == "interrupted"
                    or "no progress for more than" in err
                    or "timed out" in err
                    or "stall" in err
                    or "timeout" in err
                    or "interrupted" in err
                    or "local_timeout" in err
                    or "gemini_timeout" in err
                )
                if not eligible:
                    continue
                seen_sha.add(sha)
                payload = self._analysis_job_dict(row)
                payload["attempts"] = attempts
                out.append(payload)
            return out

    def get_analysis_job(self, job_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(AnalysisJob, job_id)
            if row is None:
                return None
            return self._analysis_job_dict(row)

    def max_restart_interruptions_for_sha(
        self,
        *,
        firm_id: str,
        matter_id: str,
        sha256: str,
    ) -> int:
        digest = (sha256 or "").lower().strip()
        if not digest or not matter_id:
            return 0
        with self.session() as s:
            rows = (
                s.execute(
                    select(AnalysisJob)
                    .where(AnalysisJob.firm_id == firm_id)
                    .where(AnalysisJob.matter_id == matter_id)
                    .where(AnalysisJob.sha256 == digest)
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0
            return max(int(getattr(r, "restart_interruptions", 0) or 0) for r in rows)

    def mark_active_jobs_interrupted(
        self,
        *,
        message: str = "Instance restarted; job was interrupted. Re-submit to continue.",
    ) -> int:
        """Mark queued/running durable jobs interrupted after a process boot.

        Leaves local_done alone — boot recovery resumes or requeues those.
        Jobs that were *running* bump restart_interruptions per matter+sha; at 2
        they are quarantined (no auto-retry — likely crashing the instance).
        """
        now = _utcnow()
        with self.session() as s:
            rows = (
                s.execute(
                    select(AnalysisJob).where(AnalysisJob.status.in_(("queued", "running")))
                )
                .scalars()
                .all()
            )
            # Max restart count per matter+sha before this interrupt wave.
            max_by_key: dict[tuple[str, str, str], int] = {}
            for row in rows:
                sha = (row.sha256 or "").lower()
                key = (row.firm_id, row.matter_id or "", sha)
                cur = int(getattr(row, "restart_interruptions", 0) or 0)
                max_by_key[key] = max(max_by_key.get(key, 0), cur)
            # Also consider historical jobs for the same sha (not only active).
            for row in rows:
                sha = (row.sha256 or "").lower()
                if not sha or not row.matter_id:
                    continue
                hist = (
                    s.execute(
                        select(AnalysisJob)
                        .where(AnalysisJob.firm_id == row.firm_id)
                        .where(AnalysisJob.matter_id == row.matter_id)
                        .where(AnalysisJob.sha256 == sha)
                    )
                    .scalars()
                    .all()
                )
                key = (row.firm_id, row.matter_id, sha)
                for h in hist:
                    max_by_key[key] = max(
                        max_by_key.get(key, 0),
                        int(getattr(h, "restart_interruptions", 0) or 0),
                    )

            for row in rows:
                was_running = row.status == "running"
                sha = (row.sha256 or "").lower()
                key = (row.firm_id, row.matter_id or "", sha)
                if was_running:
                    logger.warning(
                        "restart while running job=%s matter=%s sha=%s attempts=%s "
                        "prior_restart_interruptions=%s",
                        (row.job_id or "")[:8],
                        (row.matter_id or "")[:8],
                        sha[:12],
                        row.attempts,
                        max_by_key.get(key, 0),
                    )
                    new_count = max_by_key.get(key, 0) + 1
                    max_by_key[key] = new_count
                    row.restart_interruptions = new_count
                    if new_count >= 2:
                        row.status = "failed"
                        row.error = "restart_quarantine"
                        row.message = (
                            "Needs review — may be crashing the server "
                            f"(running at {new_count} instance restarts)."
                        )
                        logger.error(
                            "quarantine sha=%s matter=%s after %s restart interruptions "
                            "job=%s",
                            sha[:12],
                            (row.matter_id or "")[:8],
                            new_count,
                            (row.job_id or "")[:8],
                        )
                    else:
                        row.status = "interrupted"
                        row.error = "interrupted_by_restart"
                        row.message = message
                else:
                    row.status = "interrupted"
                    row.error = "interrupted_by_restart"
                    row.message = message
                row.updated_at = now
            s.commit()
            return len(rows)

    def list_analysis_jobs_by_status(
        self, statuses: list[str], *, firm_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        with self.session() as s:
            q = (
                select(AnalysisJob)
                .where(AnalysisJob.status.in_(tuple(statuses)))
                .order_by(AnalysisJob.created_at.asc())
            )
            if firm_id:
                q = q.where(AnalysisJob.firm_id == firm_id)
            rows = s.execute(q).scalars().all()
            return [self._analysis_job_dict(row) for row in rows]

    def set_restart_interruptions(self, job_id: str, count: int) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(AnalysisJob, job_id)
            if row is None:
                return None
            row.restart_interruptions = max(0, int(count))
            row.updated_at = _utcnow()
            s.commit()
            return self._analysis_job_dict(row)

    def bump_analysis_job_attempts(self, job_id: str, *, status: str, message: str | None = None) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(AnalysisJob, job_id)
            if row is None:
                return None
            row.attempts = int(row.attempts or 0) + 1
            row.status = status
            if message is not None:
                row.message = message
            row.error = None
            row.updated_at = _utcnow()
            s.commit()
            return self._analysis_job_dict(row)

    def _analysis_job_dict(self, row: AnalysisJob) -> dict[str, Any]:
        return {
            "job_id": row.job_id,
            "firm_id": row.firm_id,
            "matter_id": row.matter_id,
            "document_id": row.document_id,
            "sha256": row.sha256 or "",
            "status": row.status,
            "attempts": int(row.attempts or 0),
            "restart_interruptions": int(getattr(row, "restart_interruptions", 0) or 0),
            "message": row.message,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


def _attach_source(
    doc: Document,
    fernet: Fernet,
    sha256: str,
    source_bytes: bytes | None,
    mime_type: str | None,
) -> None:
    """Exhibit bytes are not stored. Leave source_enc untouched."""
    if not doc.source_enc:
        doc.source_missing = True


def _mime_for_bytes(data: bytes, file_type: str) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    ft = (file_type or "").lower().lstrip(".")
    return {
        "pdf": "application/pdf",
        "txt": "text/plain; charset=utf-8",
        "text": "text/plain; charset=utf-8",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
    }.get(ft, "application/octet-stream")


def content_disposition_inline(filename: str, mime: str) -> str:
    """ASCII fallback plus RFC 5987 filename* so § # ( ) survive."""
    from urllib.parse import quote

    raw = (filename or "source").replace("\\", " ").replace("/", " ").replace('"', "").strip()
    if mime.split(";")[0].strip() == "application/pdf" and not raw.lower().endswith(".pdf"):
        raw = f"{raw}.pdf"
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in raw) or "source.pdf"
    encoded = quote(raw, safe="")
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def get_store() -> FactMemoryStore:
    global _default_store
    with _store_lock:
        if _default_store is None:
            _default_store = FactMemoryStore()
        return _default_store


def reset_store() -> None:
    global _default_store
    with _store_lock:
        _default_store = None
        reset_engine()
