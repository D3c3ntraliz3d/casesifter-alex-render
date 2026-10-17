/**
 * Unit checks for document-list merge / dedupe helpers used by App.jsx.
 * Run: node frontend/scripts/test-doc-list-dedupe.mjs
 */

/** Prefer durable over provisional; never collapse distinct shas by label. */
function dedupeDocumentList(docs) {
  const byKey = new Map();
  const order = [];
  for (const doc of docs || []) {
    const sha = String(doc.sha256 || "").toLowerCase();
    const key = sha ? `sha:${sha}` : `id:${doc.id}`;
    const prev = byKey.get(key);
    if (!prev) {
      byKey.set(key, doc);
      order.push(key);
      continue;
    }
    const prevProvisional = Boolean(prev.pending_upload || prev.provisional);
    const nextProvisional = Boolean(doc.pending_upload || doc.provisional);
    if (prevProvisional && !nextProvisional) {
      byKey.set(key, doc);
    }
  }
  return order.map((key) => byKey.get(key));
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || "assertion failed");
}

// Same sha: durable wins over pending placeholder.
{
  const out = dedupeDocumentList([
    { id: "pending:j1", sha256: "AA", pending_upload: true, display_name: "A" },
    { id: "doc-1", sha256: "aa", display_name: "Durable A" },
  ]);
  assert(out.length === 1, "same sha collapses");
  assert(out[0].id === "doc-1", "durable preferred");
}

// Distinct shas with similar AOM labels must both stay.
{
  const out = dedupeDocumentList([
    { id: "1", sha256: "111", display_name: "Affidavit of Mailing 1" },
    { id: "2", sha256: "222", display_name: "Affidavit of Mailing 2" },
  ]);
  assert(out.length === 2, "distinct AOM filings must not collapse");
}

// Server provisional + client pending same sha → one row.
{
  const out = dedupeDocumentList([
    { id: "pending:job", sha256: "abc", provisional: true, pending_upload: true },
    { id: "pending:local", sha256: "abc", pending_upload: true },
  ]);
  assert(out.length === 1, "pending same sha collapses");
}

console.log("test-doc-list-dedupe: ok");
