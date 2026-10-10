import * as Sentry from "@sentry/react";

const DSN = (import.meta.env.VITE_SENTRY_DSN || "").trim();

const HEX32 = /\b[a-f0-9]{32}\b/gi;
const UUID =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
const ID_SEGMENT =
  /(?<=\/)(?:[a-f0-9]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\/|$)/gi;

function routeTemplate(urlOrPath) {
  if (!urlOrPath) return "";
  let path = String(urlOrPath);
  try {
    const u = new URL(path, "https://local.invalid");
    path = u.pathname;
  } catch {
    path = path.split("?")[0].split("#")[0];
  }
  return path.replace(ID_SEGMENT, "{id}").replace(HEX32, "{id}");
}

function scrubString(value) {
  return String(value)
    .replace(/(?:[A-Za-z]:\\|\/)[^\s"'`]{3,}/g, "[path]")
    .replace(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g, "[email]")
    .replace(HEX32, "[id]")
    .replace(UUID, "[id]")
    .slice(0, 300);
}

function scrubException(exc) {
  if (!exc || typeof exc !== "object") return exc;
  const out = { type: String(exc.type || "Error").slice(0, 120) };
  const frames = exc.stacktrace?.frames;
  if (Array.isArray(frames)) {
    out.stacktrace = {
      frames: frames.map((f) => ({
        filename: scrubString(f.filename || ""),
        function: String(f.function || "").slice(0, 120),
        lineno: f.lineno,
        module: f.module ? String(f.module).slice(0, 200) : undefined,
      })),
    };
  }
  return out;
}

function scrubEvent(event) {
  if (!event || typeof event !== "object") return event;
  const out = {
    level: event.level,
    release: event.release ? scrubString(event.release) : undefined,
    environment: event.environment,
    transaction: event.transaction ? routeTemplate(event.transaction) : undefined,
    tags: event.tags,
    event_id: event.event_id,
    platform: event.platform,
  };
  if (event.exception?.values) {
    out.exception = { values: event.exception.values.map(scrubException) };
  }
  if (event.request) {
    out.request = {
      url: routeTemplate(event.request.url || ""),
      method: event.request.method,
      headers: event.request.headers?.["User-Agent"]
        ? { "User-Agent": String(event.request.headers["User-Agent"]).slice(0, 200) }
        : undefined,
    };
  }
  if (event.contexts) {
    out.contexts = {};
    for (const key of ["runtime", "os"]) {
      if (event.contexts[key]) out.contexts[key] = event.contexts[key];
    }
  }
  if (event.user?.id) {
    out.user = { id: scrubString(String(event.user.id)).slice(0, 64) };
  }
  // Drop extra, breadcrumbs handled in beforeBreadcrumb.
  return out;
}

function scrubBreadcrumb(crumb) {
  if (!crumb || typeof crumb !== "object") return null;
  if (crumb.category === "console" || crumb.category === "ui.click") return null;
  const out = {
    category: String(crumb.category || "").slice(0, 80),
    level: String(crumb.level || "info").slice(0, 20),
  };
  const url = crumb.data?.url || crumb.data?.to;
  if (url) out.data = { url: routeTemplate(url) };
  return out;
}

/**
 * Privacy-safe browser Sentry.
 * No-op unless VITE_SENTRY_DSN is set at build/dev time.
 */
export function initSentry() {
  if (!DSN) return;

  Sentry.init({
    dsn: DSN,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT || import.meta.env.MODE || "development",
    integrations: [Sentry.browserTracingIntegration()],
    tracesSampleRate: Number(import.meta.env.VITE_SENTRY_TRACES_SAMPLE_RATE ?? 0.2),
    tracePropagationTargets: [
      "localhost",
      "127.0.0.1",
      /^https:\/\/.*\.onrender\.com\/api/,
      /^https:\/\/.*casesifter\.com/,
    ],
    sendDefaultPii: false,
    beforeSend(event) {
      return scrubEvent(event);
    },
    beforeBreadcrumb(crumb) {
      return scrubBreadcrumb(crumb);
    },
  });
  Sentry.setTag("service", "casesifter-ui");
}

export { Sentry };
