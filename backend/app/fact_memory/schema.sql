-- Fact memory schema (Postgres). SQLite uses the same tables via SQLAlchemy models.
-- Sensitive columns (*_enc) hold Fernet ciphertext; never store PDF bytes.

CREATE TABLE IF NOT EXISTS firms (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retention_policy TEXT NOT NULL DEFAULT 'until_deleted',
  persist_facts BOOLEAN NOT NULL DEFAULT TRUE,
  wrapped_data_key TEXT NOT NULL,
  stripe_customer_id TEXT,
  store_snippets BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'owner',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS matters (
  id TEXT PRIMARY KEY,
  firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  index_number TEXT,
  court TEXT,
  county TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_matters_firm ON matters(firm_id);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
  firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL,
  original_filename_enc TEXT,
  display_name_enc TEXT,
  doc_type TEXT,
  filed_date DATE,
  page_count INTEGER,
  scanned_at TIMESTAMPTZ,
  extractor_version TEXT,
  snapshot_enc TEXT,
  report_enc TEXT,
  last_job_id TEXT,
  UNIQUE (matter_id, sha256)
);
CREATE INDEX IF NOT EXISTS ix_documents_firm ON documents(firm_id);

CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
  firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  canonical_name_enc TEXT,
  aliases_enc TEXT
);
CREATE INDEX IF NOT EXISTS ix_entities_matter ON entities(matter_id);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
  firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
  attribute TEXT NOT NULL,
  value_enc TEXT,
  value_normalized_enc TEXT,
  page INTEGER,
  snippet_enc TEXT,
  confidence DOUBLE PRECISION,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  fact_blob_enc TEXT
);
CREATE INDEX IF NOT EXISTS ix_facts_matter ON facts(matter_id);
CREATE INDEX IF NOT EXISTS ix_facts_document ON facts(document_id);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
  firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  job_id TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running',
  document_ids_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_matter ON runs(matter_id);

CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY,
  matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
  firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  finding_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  confidence DOUBLE PRECISION,
  description_enc TEXT,
  title_enc TEXT,
  fact_ids_json TEXT,
  evidence_enc TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_findings_matter ON findings(matter_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  firm_id TEXT,
  user_id TEXT,
  action TEXT NOT NULL,
  target TEXT,
  request_id TEXT,
  at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_audit_firm ON audit_log(firm_id);
