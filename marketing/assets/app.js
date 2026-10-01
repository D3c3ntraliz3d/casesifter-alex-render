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

async function wireSignup() {
  const form = document.getElementById("signup-form");
  if (!form) return;

  const status = document.getElementById("form-status");
  const submit = document.getElementById("signup-submit");
  const success = document.getElementById("signup-success");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!status || !submit) return;

    status.classList.remove("show", "error");
    status.textContent = "";
    submit.disabled = true;

    const body = {
      name: (form.name?.value || "").trim() || null,
      email: (form.email?.value || "").trim(),
      password: form.password?.value || "",
      accepted_terms: Boolean(form.accepted_terms?.checked),
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
      if (success) success.hidden = false;
    } catch {
      status.textContent = "Could not reach the signup service. Check your connection and try again.";
      status.classList.add("show", "error");
      submit.disabled = false;
    }
  });
}
