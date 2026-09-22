const fs = require("fs");
const path = require("path");
const os = require("os");

const MAX_EVENTS = 40;

function telemetryPath(root) {
  return path.join(root, "product_events.json");
}

function loadEvents(root) {
  try {
    const raw = JSON.parse(fs.readFileSync(telemetryPath(root), "utf8"));
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function saveEvents(root, events) {
  fs.writeFileSync(telemetryPath(root), JSON.stringify(events.slice(-MAX_EVENTS), null, 2));
}

function recordEvent(root, type, payload = {}) {
  const events = loadEvents(root);
  events.push({
    id: `ev-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
    type,
    payload,
    created_at: new Date().toISOString(),
  });
  saveEvents(root, events);
  return events[events.length - 1];
}

function recentEvents(root, limit = 20) {
  return loadEvents(root).slice(-limit);
}

function fileMbBucket(bytes) {
  if (!bytes || bytes <= 0) return "unknown";
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return "lt_1mb";
  if (mb < 5) return "1_5mb";
  if (mb < 20) return "5_20mb";
  if (mb < 50) return "20_50mb";
  return "50mb_plus";
}

async function flushToControl(cfg, root, remoteJson) {
  if (!cfg.controlPlaneUrl) return { flushed: 0 };
  const events = loadEvents(root);
  const pending = events.filter((e) => !e.synced);
  if (!pending.length) return { flushed: 0 };
  const batch = pending.slice(0, 50);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    await remoteJson(`${cfg.controlPlaneUrl.replace(/\/$/, "")}/v1/telemetry`, {
      method: "POST",
      signal: controller.signal,
      headers: {
        ...(cfg.sessionToken ? { Authorization: `Bearer ${cfg.sessionToken}` } : {}),
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        support_code: cfg.supportCode || null,
        events: batch.map((e) => ({
          id: e.id,
          type: e.type,
          payload: e.payload,
          created_at: e.created_at,
        })),
      }),
    });
  } finally {
    clearTimeout(timer);
  }
  const syncedIds = new Set(batch.map((e) => e.id));
  const next = events.map((e) => (syncedIds.has(e.id) ? { ...e, synced: true } : e));
  saveEvents(root, next);
  return { flushed: batch.length };
}

function platformLabel() {
  return `${process.platform} ${os.release()}`;
}

module.exports = {
  recordEvent,
  recentEvents,
  fileMbBucket,
  flushToControl,
  platformLabel,
};
