"""Firm-scoped data-access layer for persistent fact memory.

Every read/write takes firm_id. Snapshots mirror vault cases_db shapes so
compare_to_case stays the cross-doc engine.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fact_memory import encryption as enc
from app.fact_memory.models import (
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
    ) -> dict[str, Any]:
        """Persist document/entities/facts/run/findings. No PDF bytes."""
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
                # Still record a run pointing at existing doc.
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

            for finding in findings or []:
                if not isinstance(finding, dict):
                    continue
                ftype = (
                    (finding.get("evidence") or {}).get("analysis_type")
                    or finding.get("category")
                    or "finding"
                )
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

            matter.last_activity_at = _utcnow()
            self._audit(s, firm_id, user_id, "document.scan", doc.id)
            s.commit()
            return {
                "persisted": True,
                "deduped": False,
                "document_id": doc.id,
                "run_id": run.id,
                "fact_count": len(facts or []),
                "finding_count": len(findings or []),
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

    def purge_firm(self, firm_id: str, *, user_id: str | None = None) -> bool:
        with self.session() as s:
            firm = s.get(Firm, firm_id)
            if not firm:
                return False
            self._audit(s, firm_id, user_id, "firm.purge", firm_id)
            s.delete(firm)
            s.commit()
            return True


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
