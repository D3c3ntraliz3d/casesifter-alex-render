/**
 * Client-side app routes without react-router.
 * http(s): / /matters/:id /matters/:id/docs/:docId /account /admin
 * file:// (desktop packaged): ?view= &matter= &doc=
 */

const PRESERVE_QUERY = new Set([
  "checkout",
  "cs_code",
  "reset_token",
  "seat_invite",
  "next",
]);

function useQueryRoutes() {
  if (typeof window === "undefined") return false;
  return window.location.protocol === "file:";
}

function cleanPathname(pathname) {
  const raw = String(pathname || "/");
  if (raw.endsWith(".html")) {
    // Packaged desktop: .../index.html
    return "/";
  }
  const trimmed = raw.replace(/\/+$/, "");
  return trimmed || "/";
}

/** @returns {{ view: string, matterId: string|null, docId: string|null, next: string|null }} */
export function parseAppRoute(loc = typeof window !== "undefined" ? window.location : null) {
  if (!loc) {
    return { view: "cases", matterId: null, docId: null, next: null };
  }
  let params;
  try {
    params = new URLSearchParams(loc.search || "");
  } catch {
    params = new URLSearchParams();
  }
  const next = (params.get("next") || "").trim() || null;
  const path = cleanPathname(loc.pathname);

  const qView = (params.get("view") || "").trim();
  const qMatter = (params.get("matter") || "").trim();
  const qDoc = (params.get("doc") || "").trim();

  if (path === "/account" || qView === "account") {
    return { view: "account", matterId: null, docId: null, next };
  }
  if (path === "/admin" || qView === "admin") {
    return { view: "admin", matterId: null, docId: null, next };
  }

  const matterPath = path.match(/^\/matters\/([^/]+)(?:\/docs\/([^/]+))?$/i);
  if (matterPath) {
    let matterId = matterPath[1];
    let docId = matterPath[2] || null;
    try {
      matterId = decodeURIComponent(matterId);
      if (docId) docId = decodeURIComponent(docId);
    } catch {
      /* keep raw */
    }
    return {
      view: docId ? "report" : "case",
      matterId,
      docId,
      next,
    };
  }

  if (qMatter) {
    return {
      view: qDoc ? "report" : "case",
      matterId: qMatter,
      docId: qDoc || null,
      next,
    };
  }

  if (path === "/" || path === "" || qView === "cases" || qView === "") {
    return { view: "cases", matterId: null, docId: null, next };
  }

  // Unknown path → cases (hosted SPA may still serve index.html)
  return { view: "cases", matterId: null, docId: null, next };
}

function preservedSearch(extra = {}) {
  let params;
  try {
    params = new URLSearchParams(typeof window !== "undefined" ? window.location.search || "" : "");
  } catch {
    params = new URLSearchParams();
  }
  const out = new URLSearchParams();
  for (const key of PRESERVE_QUERY) {
    const v = params.get(key);
    if (v) out.set(key, v);
  }
  for (const [k, v] of Object.entries(extra)) {
    if (v == null || v === "") out.delete(k);
    else out.set(k, String(v));
  }
  // Drop routing keys we manage explicitly
  out.delete("view");
  out.delete("matter");
  out.delete("doc");
  const qs = out.toString();
  return qs ? `?${qs}` : "";
}

/**
 * @param {{ view: string, matterId?: string|null, docId?: string|null, next?: string|null }} route
 * @returns {string} path + optional query (relative to origin)
 */
export function buildAppHref(route) {
  const view = route?.view || "cases";
  const matterId = route?.matterId || null;
  const docId = route?.docId || null;
  // Clear next= except when gating to Account with a return target.
  const nextExtra = {
    next: view === "account" && route?.next ? route.next : null,
  };

  if (useQueryRoutes()) {
    // Keep index.html path on file://
    const filePath =
      typeof window !== "undefined" && String(window.location.pathname || "").endsWith(".html")
        ? window.location.pathname
        : (typeof window !== "undefined" ? window.location.pathname : "/") || "/";

    let params;
    try {
      params = new URLSearchParams(typeof window !== "undefined" ? window.location.search || "" : "");
    } catch {
      params = new URLSearchParams();
    }
    for (const key of [...params.keys()]) {
      if (!PRESERVE_QUERY.has(key)) params.delete(key);
    }
    params.delete("view");
    params.delete("matter");
    params.delete("doc");
    if (nextExtra.next) params.set("next", nextExtra.next);
    else params.delete("next");
    if (view === "account") params.set("view", "account");
    else if (view === "admin") params.set("view", "admin");
    else if ((view === "case" || view === "report") && matterId) {
      params.set("matter", matterId);
      if (view === "report" && docId) params.set("doc", docId);
    }
    const qs = params.toString();
    return `${filePath}${qs ? `?${qs}` : ""}`;
  }

  let path = "/";
  if (view === "account") path = "/account";
  else if (view === "admin") path = "/admin";
  else if ((view === "case" || view === "report") && matterId) {
    path = `/matters/${encodeURIComponent(matterId)}`;
    if (view === "report" && docId) {
      path += `/docs/${encodeURIComponent(docId)}`;
    }
  }

  return `${path}${preservedSearch(nextExtra)}`;
}

export function routesEqual(a, b) {
  return (
    (a?.view || "cases") === (b?.view || "cases")
    && (a?.matterId || null) === (b?.matterId || null)
    && (a?.docId || null) === (b?.docId || null)
  );
}

/** Write the browser URL to match the app route. */
export function writeAppHref(route, { replace = false } = {}) {
  if (typeof window === "undefined") return;
  const href = buildAppHref(route);
  const current = `${window.location.pathname}${window.location.search || ""}`;
  if (href === current) return;
  const state = { appRoute: true };
  if (replace) window.history.replaceState(state, "", href);
  else window.history.pushState(state, "", href);
}

/** Parse a stored next= path back into a route (path or query form). */
export function parseNextTarget(next) {
  if (!next || typeof next !== "string") return null;
  try {
    if (next.startsWith("?")) {
      return parseAppRoute({ pathname: "/", search: next });
    }
    if (next.startsWith("/")) {
      const u = new URL(next, "https://app.local");
      return parseAppRoute({ pathname: u.pathname, search: u.search });
    }
  } catch {
    return null;
  }
  return null;
}
