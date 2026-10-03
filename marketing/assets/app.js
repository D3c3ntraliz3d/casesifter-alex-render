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
});

const CONTROL_PLANE_URL = "https://casesifter-control.onrender.com";
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

async function preferAppDomainWhenLive(openApp) {
  if (!openApp) return;
  openApp.href = APP_URL_DEFAULT;
  try {
    await fetch(APP_URL_PREFERRED, { method: "HEAD", mode: "no-cors" });
    openApp.href = APP_URL_PREFERRED;
  } catch {
    openApp.href = APP_URL_DEFAULT;
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
    openApp.href = APP_URL_DEFAULT;
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
        "Fix the highlighted fields — password (6+ chars) and terms are required.",
        { error: true },
      );
      return;
    }

    if (!email || !email.includes("@")) {
      showFormStatus(status, "Enter a valid work email.", { error: true });
      form.email?.focus();
      return;
    }
    if (password.length < 6) {
      showFormStatus(status, "Password must be at least 6 characters.", { error: true });
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
          try {
            localStorage.setItem("casesifter_session_token", data.token);
            localStorage.setItem("casesifter_email", email);
          } catch {
            /* private mode */
          }
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
      showFormStatus(status, "");
      if (success) success.hidden = false;
      // Default CTA is onrender; upgrade to app.casesifter.com only when it resolves.
      await preferAppDomainWhenLive(openApp);
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
