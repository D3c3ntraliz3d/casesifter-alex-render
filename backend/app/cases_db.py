"""Local SQLite store for cases, documents, and scan snapshots.

Evidence files live on disk under data/cases/{case_id}/documents/; this DB is the
index and memory the consistency engine reads when a new file arrives.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

_lock = threading.Lock()
_INITIALIZED = False


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    return settings.data_dir / "cases.db"


def cases_root() -> Path:
    path = settings.data_dir / "cases"
    path.mkdir(parents=True, exist_ok=True)
    return path


def case_dir(case_id: str) -> Path:
    path = cases_root() / case_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "documents").mkdir(exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_schema()
    conn = sqlite3.connect(str(db_path()), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    global _INITIALIZED
    with _lock:
        if _INITIALIZED and db_path().exists():
            return
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path()), timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    caption TEXT NOT NULL,
                    court TEXT,
                    index_number TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    doc_kind TEXT,
                    sha256 TEXT,
                    stored_path TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    scan_job_id TEXT,
                    status TEXT NOT NULL DEFAULT 'uploaded',
                    risk_level TEXT,
                    risk_score INTEGER,
                    FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS scan_snapshots (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL UNIQUE,
                    case_id TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    findings_summary_json TEXT NOT NULL,
                    text_fingerprint TEXT,
                    excerpt_hashes_json TEXT,
                    amounts_json TEXT,
                    text_excerpt TEXT,
                    facts_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS case_messages (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    citations_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS telemetry_events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            _migrate_schema(conn)
            conn.commit()
        finally:
            conn.close()
        _INITIALIZED = True


def _migrate_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
    if "archived_at" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN archived_at TEXT")
    snap_cols = {row[1] for row in conn.execute("PRAGMA table_info(scan_snapshots)")}
    if "text_excerpt" not in snap_cols:
        conn.execute("ALTER TABLE scan_snapshots ADD COLUMN text_excerpt TEXT")
    if "facts_json" not in snap_cols:
        conn.execute("ALTER TABLE scan_snapshots ADD COLUMN facts_json TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_messages (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            citations_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_case_messages_case ON case_messages(case_id, created_at)"
    )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def create_case(
    *,
    caption: str,
    court: str | None = None,
    index_number: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    case_id = uuid.uuid4().hex
    now = utcnow_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO cases (id, caption, court, index_number, notes, created_at, updated_at, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (case_id, caption.strip(), court or None, index_number or None, notes or None, now, now),
        )
    case_dir(case_id)
    return get_case(case_id)  # type: ignore[return-value]


def list_cases() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM documents d WHERE d.case_id = c.id) AS document_count
            FROM cases c
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_case(case_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM documents d WHERE d.case_id = c.id) AS document_count
            FROM cases c WHERE c.id = ?
            """,
            (case_id,),
        ).fetchone()
    return _row_to_dict(row)


def update_case(case_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"caption", "court", "index_number", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_case(case_id)
    updates["updated_at"] = utcnow_iso()
    cols = ", ".join(f"{k} = ?" for k in updates)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE cases SET {cols} WHERE id = ?",
            (*updates.values(), case_id),
        )
        if cur.rowcount == 0:
            return None
    return get_case(case_id)


def set_archived(case_id: str, archived: bool) -> dict[str, Any] | None:
    if get_case(case_id) is None:
        return None
    now = utcnow_iso()
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET archived_at = ?, updated_at = ? WHERE id = ?",
            (now if archived else None, now, case_id),
        )
    return get_case(case_id)


def delete_case(case_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
        deleted = cur.rowcount > 0
    folder = cases_root() / case_id
    if folder.exists():
        import shutil

        shutil.rmtree(folder, ignore_errors=True)
    return deleted


def add_document(
    case_id: str,
    *,
    original_filename: str,
    display_name: str,
    doc_kind: str | None,
    stored_path: str,
    sha256: str | None = None,
) -> dict[str, Any]:
    from app.auto_title import uniquify_display_name

    taken = [item.get("display_name") or "" for item in list_documents(case_id)]
    display_name = uniquify_display_name(display_name, taken, original_filename)
    doc_id = uuid.uuid4().hex
    now = utcnow_iso()
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with connect() as conn:
                exists = conn.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone()
                if not exists:
                    raise KeyError(case_id)
                conn.execute(
                    """
                    INSERT INTO documents (
                        id, case_id, original_filename, display_name, doc_kind, sha256,
                        stored_path, uploaded_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'uploaded')
                    """,
                    (
                        doc_id,
                        case_id,
                        original_filename,
                        display_name,
                        doc_kind,
                        sha256,
                        stored_path,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE cases SET updated_at = ? WHERE id = ?",
                    (now, case_id),
                )
            return get_document(doc_id)  # type: ignore[return-value]
        except sqlite3.OperationalError as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise last_error or sqlite3.OperationalError("database is locked")


def list_documents(case_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM documents
            WHERE case_id = ?
            ORDER BY uploaded_at ASC
            """,
            (case_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_document(document_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return _row_to_dict(row)


def update_document(document_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "display_name",
        "doc_kind",
        "sha256",
        "scan_job_id",
        "status",
        "risk_level",
        "risk_score",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    current = get_document(document_id)
    if current is None:
        return None
    if "display_name" in updates:
        from app.auto_title import uniquify_display_name

        siblings = [
            item.get("display_name") or ""
            for item in list_documents(current["case_id"])
            if item.get("id") != document_id
        ]
        updates["display_name"] = uniquify_display_name(
            str(updates["display_name"] or ""),
            siblings,
            current.get("original_filename") or "",
        )
    if not updates:
        return current
    cols = ", ".join(f"{k} = ?" for k in updates)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE documents SET {cols} WHERE id = ?",
            (*updates.values(), document_id),
        )
        if cur.rowcount == 0:
            return None
        doc = conn.execute(
            "SELECT case_id FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if doc:
            conn.execute(
                "UPDATE cases SET updated_at = ? WHERE id = ?",
                (utcnow_iso(), doc["case_id"]),
            )
    return get_document(document_id)


def save_scan_snapshot(
    *,
    document_id: str,
    case_id: str,
    entities: dict[str, Any],
    findings_summary: list[dict[str, Any]],
    text_fingerprint: str | None,
    excerpt_hashes: list[str],
    amounts: list[dict[str, Any]],
    text_excerpt: str | None = None,
    facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snap_id = uuid.uuid4().hex
    now = utcnow_iso()
    with connect() as conn:
        conn.execute("DELETE FROM scan_snapshots WHERE document_id = ?", (document_id,))
        conn.execute(
            """
            INSERT INTO scan_snapshots (
                id, document_id, case_id, entities_json, findings_summary_json,
                text_fingerprint, excerpt_hashes_json, amounts_json, text_excerpt,
                facts_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap_id,
                document_id,
                case_id,
                json.dumps(entities),
                json.dumps(findings_summary),
                text_fingerprint,
                json.dumps(excerpt_hashes),
                json.dumps(amounts),
                (text_excerpt or "")[:24000] or None,
                json.dumps(facts or []),
                now,
            ),
        )
    return get_scan_snapshot(document_id)  # type: ignore[return-value]


def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["entities"] = json.loads(data.pop("entities_json") or "{}")
    data["findings_summary"] = json.loads(data.pop("findings_summary_json") or "[]")
    data["excerpt_hashes"] = json.loads(data.pop("excerpt_hashes_json") or "[]")
    data["amounts"] = json.loads(data.pop("amounts_json") or "[]")
    data["facts"] = json.loads(data.pop("facts_json") or "[]")
    data["text_excerpt"] = data.get("text_excerpt") or ""
    return data


def get_scan_snapshot(document_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM scan_snapshots WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    if row is None:
        return None
    return _decode_snapshot(row)


def list_prior_snapshots(case_id: str, exclude_document_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, d.display_name, d.doc_kind, d.original_filename
            FROM scan_snapshots s
            JOIN documents d ON d.id = s.document_id
            WHERE s.case_id = ? AND s.document_id != ?
            ORDER BY s.created_at ASC
            """,
            (case_id, exclude_document_id),
        ).fetchall()
    return [_decode_snapshot(row) for row in rows]


def list_case_gotchas(case_id: str) -> list[dict[str, Any]]:
    from app.case_consistency import dedupe_gotchas
    from app.evidence_tier import evidence_is_probative

    docs = list_documents(case_id)
    out: list[dict[str, Any]] = []
    for snap in list_case_snapshots(case_id):
        for item in snap.get("findings_summary") or []:
            if not item.get("gotcha") and item.get("analysis_type") != "gotcha":
                continue
            if not item.get("verified"):
                continue
            record = {
                **item,
                "document_id": item.get("document_id") or snap.get("document_id"),
                "display_name": snap.get("display_name") or snap.get("original_filename"),
                "scan_job_id": None,
            }
            if not evidence_is_probative(record, docs):
                continue
            out.append(record)
    out = dedupe_gotchas(out)
    out.sort(key=lambda item: (0 if item.get("tainted_support") else 1))
    return out


def list_case_fact_conflicts(case_id: str) -> list[dict[str, Any]]:
    from app.case_consistency import aggregate_fact_conflicts

    return aggregate_fact_conflicts(list_case_snapshots(case_id))


def list_case_snapshots(case_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, d.display_name, d.doc_kind, d.original_filename, d.stored_path
            FROM scan_snapshots s
            JOIN documents d ON d.id = s.document_id
            WHERE s.case_id = ?
            ORDER BY s.created_at ASC
            """,
            (case_id,),
        ).fetchall()
    return [_decode_snapshot(row) for row in rows]


def save_text_excerpt(document_id: str, case_id: str, excerpt: str) -> None:
    text = (excerpt or "")[:24000]
    if not text:
        return
    from app.fact_extract import extract_facts

    facts_json = json.dumps(extract_facts(text))
    now = utcnow_iso()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM scan_snapshots WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE scan_snapshots SET text_excerpt = ?, facts_json = ? WHERE document_id = ?",
                (text, facts_json, document_id),
            )
            return
        conn.execute(
            """
            INSERT INTO scan_snapshots (
                id, document_id, case_id, entities_json, findings_summary_json,
                text_fingerprint, excerpt_hashes_json, amounts_json, text_excerpt,
                facts_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                document_id,
                case_id,
                "{}",
                "[]",
                None,
                "[]",
                "[]",
                text,
                facts_json,
                now,
            ),
        )


def add_case_message(
    case_id: str,
    role: str,
    content: str,
    citations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    msg_id = uuid.uuid4().hex
    now = utcnow_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO case_messages (id, case_id, role, content, citations_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                case_id,
                role,
                content,
                json.dumps(citations or []),
                now,
            ),
        )
    return {
        "id": msg_id,
        "case_id": case_id,
        "role": role,
        "content": content,
        "citations": citations or [],
        "created_at": now,
    }


def list_case_messages(case_id: str, limit: int = 40) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM case_messages
            WHERE case_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (case_id, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        item = dict(row)
        item["citations"] = json.loads(item.pop("citations_json") or "[]")
        out.append(item)
    return out


def record_telemetry(event_type: str, payload: dict[str, Any]) -> str:
    event_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO telemetry_events (id, event_type, payload_json, created_at, synced)
            VALUES (?, ?, ?, ?, 0)
            """,
            (event_id, event_type, json.dumps(payload), utcnow_iso()),
        )
    return event_id


def list_unsynced_telemetry(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM telemetry_events
            WHERE synced = 0
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        out.append(item)
    return out


def mark_telemetry_synced(event_ids: list[str]) -> None:
    if not event_ids:
        return
    with connect() as conn:
        conn.executemany(
            "UPDATE telemetry_events SET synced = 1 WHERE id = ?",
            [(eid,) for eid in event_ids],
        )


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return default
    return str(row["value"])


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
