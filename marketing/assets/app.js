const year = document.getElementById("year");
if (year) year.textContent = String(new Date().getFullYear());

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", () => links.classList.toggle("open"));
  }

  const form = document.getElementById("contact-form");
  const status = document.getElementById("form-status");
  if (form && status && form.id === "contact-form") {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      status.textContent = "Thanks — we'll be in touch within one business day.";
      status.classList.remove("error");
      status.classList.add("show");
      form.reset();
    });
  }

  wireSignup();
  wireVerify();
  wireReset();
});

const CONTROL_PLANE_URL = "https://casesifter-control.onrender.com";
const ANALYSIS_API_URL = "https://casesifter-analysis.onrender.com";
/** Default Open web app target (works today). Prefer custom domain once DNS resolves. */
const APP_URL_DEFAULT = "https://casesifter-app.onrender.com";
const APP_URL_PREFERRED = "https://app.casesifter.com";

function showFormStatus(el, message, { error = false } = {}) {
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("error", Boolean(error && message));
  el.classList.toggle("show", Boolean(message));
  if (message && error && typeof el.scrollIntoView === "function") {
    try {
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch {
      /* older browsers */
    }
  }
}

async function preferAppDomainWhenLive(openApp, { checkoutTrial = false, code = "" } = {}) {
  if (!openApp) return;
  const params = new URLSearchParams();
  if (checkoutTrial) params.set("checkout", "trial");
  // One-time 60s exchange code — never put session tokens in the URL.
  if (code) params.set("cs_code", code);
  const qs = params.toString();
  const suffix = qs ? `/?${qs}` : "";
  openApp.href = `${APP_URL_DEFAULT}${suffix}`;
  try {
    await fetch(APP_URL_PREFERRED, { method: "HEAD", mode: "no-cors" });
    openApp.href = `${APP_URL_PREFERRED}${suffix}`;
  } catch {
    openApp.href = `${APP_URL_DEFAULT}${suffix}`;
  }
}

async function wireSignup() {
  const form = document.getElementById("signup-form");
  if (!form) return;

  const status = document.getElementById("form-status");
  const submit = document.getElementById("signup-submit");
  const success = document.getElementById("signup-success");
  const openApp = document.getElementById("open-app");
  if (openApp) {
    openApp.href = `${APP_URL_DEFAULT}/?checkout=trial`;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!status || !submit) return;

    showFormStatus(status, "");

    const email = (form.email?.value || "").trim();
    const password = form.password?.value || "";
    const invite = (form.invite_code?.value || "").trim();
    const accepted = Boolean(form.accepted_terms?.checked);

    // Surface native HTML5 constraints (required email/password/terms, minlength).
    if (typeof form.reportValidity === "function" && !form.reportValidity()) {
      showFormStatus(
        status,
        "Fix the highlighted fields — password (8+ chars) and terms are required.",
        { error: true },
      );
      return;
    }

    if (!email || !email.includes("@")) {
      showFormStatus(status, "Enter a valid work email.", { error: true });
      form.email?.focus();
      return;
    }
    if (password.length < 8) {
      showFormStatus(status, "Password must be at least 8 characters.", { error: true });
      form.password?.focus();
      return;
    }
    if (!accepted) {
      showFormStatus(status, "Agree to the Terms of Service and Privacy Policy to continue.", { error: true });
      form.accepted_terms?.focus();
      return;
    }

    submit.disabled = true;
    showFormStatus(status, "Creating your account…");

    const body = {
      name: (form.name?.value || "").trim() || null,
      email,
      password,
      accepted_terms: accepted,
      invite_code: invite,
    };

    try {
      const res = await fetch(`${CONTROL_PLANE_URL}/v1/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
      });
      let detail = "";
      try {
        const data = await res.json();
        detail = typeof data?.detail === "string"
          ? data.detail
          : Array.isArray(data?.detail)
            ? data.detail.map((d) => d.msg || JSON.stringify(d)).join(" ")
            : "";
        if (res.ok && data?.token) {
          // Rare: immediate session (legacy). Mint a short-lived handoff code.
          try {
            const handoff = await fetch(`${CONTROL_PLANE_URL}/v1/session/handoff-code`, {
              method: "POST",
              headers: {
                Authorization: `Bearer ${data.token}`,
                Accept: "application/json",
              },
            });
            const hb = await handoff.json();
            await preferAppDomainWhenLive(openApp, {
              checkoutTrial: true,
              code: hb?.code || "",
            });
          } catch {
            await preferAppDomainWhenLive(openApp, { checkoutTrial: true });
          }
        } else if (res.ok) {
          // Verify-first: no session until the email link is used.
          showFormStatus(
            status,
            data?.message || "Check your email to verify your account, then open the app to sign in.",
          );
        }
      } catch {
        /* non-JSON error body */
      }

      if (!res.ok) {
        const fallback =
          res.status === 403
            ? "That invite code is not valid. Leave it blank or use a code from your firm."
            : `Signup failed (${res.status}). Try again or contact us.`;
        showFormStatus(status, detail || fallback, { error: true });
        submit.disabled = false;
        return;
      }

      form.hidden = true;
      if (success) success.hidden = false;
      if (!openApp?.href?.includes("cs_code=")) {
        await preferAppDomainWhenLive(openApp, { checkoutTrial: true });
      }
    } catch {
      showFormStatus(
        status,
        "Could not reach the signup service. Check your connection and try again.",
        { error: true },
      );
      submit.disabled = false;
    }
  });
}

async function claimAnalysisCookie(code) {
  if (!code) return;
  await fetch(`${ANALYSIS_API_URL}/api/v1/session/accept`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ code }),
  });
}

function wireVerify() {
  const status = document.getElementById("verify-status");
  if (!status) return;
  const params = new URLSearchParams(window.location.search || "");
  const token = (params.get("verify_token") || "").trim();
  window.history.replaceState({}, "", window.location.pathname);
  if (!token) {
    showFormStatus(status, "This verification link is missing a token.", { error: true });
    return;
  }
  (async () => {
    try {
      const res = await fetch(`${CONTROL_PLANE_URL}/v1/verify-email`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showFormStatus(status, data.detail || "This verification link is invalid or expired.", { error: true });
        return;
      }
      await claimAnalysisCookie(data.analysis_code);
      showFormStatus(status, "Email verified. Taking you to the app…");
      window.location.assign(APP_URL_DEFAULT);
    } catch {
      showFormStatus(status, "Could not reach CaseSifter to verify this link.", { error: true });
    }
  })();
}

function wireReset() {
  const form = document.getElementById("reset-form");
  const status = document.getElementById("reset-status");
  if (!form || !status) return;
  const params = new URLSearchParams(window.location.search || "");
  const token = (params.get("reset_token") || "").trim();
  window.history.replaceState({}, "", window.location.pathname);
  if (!token) {
    showFormStatus(status, "This reset link is missing a token.", { error: true });
    form.hidden = true;
    return;
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = form.password?.value || "";
    if (password.length < 8) {
      showFormStatus(status, "Password must be at least 8 characters.", { error: true });
      return;
    }
    try {
      const res = await fetch(`${CONTROL_PLANE_URL}/v1/password-reset/confirm`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ token, password }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showFormStatus(status, data.detail || "This reset link is invalid or expired.", { error: true });
        return;
      }
      await claimAnalysisCookie(data.analysis_code);
      showFormStatus(status, "Password updated. Taking you to the app…");
      window.location.assign(APP_URL_DEFAULT);
    } catch {
      showFormStatus(status, "Could not reach CaseSifter to reset the password.", { error: true });
    }
  });
}
