import * as Sentry from "@sentry/react";

const DSN = (import.meta.env.VITE_SENTRY_DSN || "").trim();

function scrubString(value) {
  return String(value)
    .replace(/(?:[A-Za-z]:\\|\/)[^\s"'`]{3,}/g, "[path]")
    .replace(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g, "[email]")
    .slice(0, 500);
}

function scrubValue(value, depth = 0) {
  if (depth > 6) return "[truncated]";
  if (typeof value === "string") return scrubString(value);
  if (Array.isArray(value)) return value.slice(0, 20).map((v) => scrubValue(v, depth + 1));
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      // Never ship case captions / document text style fields if they leak into extras.
      if (/caption|excerpt|ocr|pdf|document.?text|question/i.test(k)) {
        out[k] = "[redacted]";
        continue;
      }
      out[k] = scrubValue(v, depth + 1);
    }
    return out;
  }
  return value;
}

function scrubEvent(event) {
  if (!event || typeof event !== "object") return event;
  for (const key of ["breadcrumbs", "request", "extra", "contexts", "exception"]) {
    if (key in event) event[key] = scrubValue(event[key]);
  }
  return event;
}

/**
 * Privacy-safe browser/Electron renderer Sentry.
 * No-op unless VITE_SENTRY_DSN is set at build/dev time.
 */
export function initSentry() {
  if (!DSN) return;

  Sentry.init({
    dsn: DSN,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT || import.meta.env.MODE || "development",
    integrations: [Sentry.browserTracingIntegration()],
    // Pilot: capture most traces; dial down later if volume is high.
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
  });
  Sentry.setTag("service", "casesifter-ui");
}

export { Sentry };
