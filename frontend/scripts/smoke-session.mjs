/**
 * Smoke checks for session auth failure detection + handoff URL shape.
 * Run: node scripts/smoke-session.mjs
 */

function isSessionAuthFailure(res, detail = "") {
  if (res && (res.status === 401 || res.status === 403)) return true;
  const msg = String(detail || "").toLowerCase();
  return /invalid session|bearer token required|unknown user|session expired/.test(msg);
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

assert(isSessionAuthFailure({ status: 401 }, "Invalid session."), "401 Invalid session");
assert(isSessionAuthFailure({ status: 401 }, "Bearer token required."), "401 bearer");
assert(isSessionAuthFailure(null, "Invalid session."), "detail-only invalid session");
assert(!isSessionAuthFailure({ status: 502 }, "Stripe checkout failed"), "502 is not auth");
assert(!isSessionAuthFailure(null, "Control plane unreachable"), "network msg is not auth");

const params = new URLSearchParams({
  checkout: "trial",
  cs_token: "tok_test_abc",
  cs_email: "john@libertygroupfunding.com",
});
assert(params.get("cs_token") === "tok_test_abc", "handoff token");
assert(params.get("checkout") === "trial", "handoff checkout");

console.log("smoke-session: ok");
