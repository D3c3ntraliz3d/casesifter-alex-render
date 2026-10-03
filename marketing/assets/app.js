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
/** Hosted web app (custom domain when DNS is live; Render URL always works). */
const APP_URL = "https://app.casesifter.com";
const APP_URL_FALLBACK = "https://casesifter-app.onrender.com";

async function wireSignup() {
  const form = document.getElementById("signup-form");
  if (!form) return;

  const status = document.getElementById("form-status");
  const submit = document.getElementById("signup-submit");
  const success = document.getElementById("signup-success");
  const openApp = document.getElementById("open-app");
  if (openApp) {
    openApp.href = APP_URL;
    openApp.addEventListener("click", (e) => {
      // If custom domain is not live yet, fall back to the Render app URL.
      if (openApp.dataset.fallback === "1") {
        e.preventDefault();
        window.location.href = APP_URL_FALLBACK;
      }
    });
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!status || !submit) return;

    status.classList.remove("show", "error");
    status.textContent = "";

    const email = (form.email?.value || "").trim();
    const password = form.password?.value || "";
    const accepted = Boolean(form.accepted_terms?.checked);

    // Surface native HTML5 constraints (required email/password/terms, minlength).
    if (typeof form.reportValidity === "function" && !form.reportValidity()) {
      status.textContent = "Fix the highlighted fields — password (6+ chars) and terms are required.";
      status.classList.add("show", "error");
      return;
    }

    if (!email || !email.includes("@")) {
      status.textContent = "Enter a valid work email.";
      status.classList.add("show", "error");
      form.email?.focus();
      return;
    }
    if (password.length < 6) {
      status.textContent = "Password must be at least 6 characters.";
      status.classList.add("show", "error");
      form.password?.focus();
      return;
    }
    if (!accepted) {
      status.textContent = "Agree to the Terms of Service and Privacy Policy to continue.";
      status.classList.add("show", "error");
      form.accepted_terms?.focus();
      return;
    }

    submit.disabled = true;
    status.textContent = "Creating your account…";
    status.classList.add("show");

    const body = {
      name: (form.name?.value || "").trim() || null,
      email,
      password,
      accepted_terms: accepted,
      invite_code: (form.invite_code?.value || "").trim() || null,
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
        status.textContent = detail || `Signup failed (${res.status}). Try again or contact us.`;
        status.classList.add("show", "error");
        submit.disabled = false;
        return;
      }

      form.hidden = true;
      status.classList.remove("show", "error");
      if (success) success.hidden = false;
      // Probe app custom domain; fall back to onrender if needed.
      try {
        const probe = await fetch(APP_URL, { method: "HEAD", mode: "no-cors" });
        void probe;
      } catch {
        if (openApp) openApp.dataset.fallback = "1";
      }
    } catch {
      status.textContent = "Could not reach the signup service. Check your connection and try again.";
      status.classList.add("show", "error");
      submit.disabled = false;
    }
  });
}
