const CONTROL_PLANE_URL = "https://casesifter-control.onrender.com";

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", () => links.classList.toggle("open"));
  }

  wireContactForm();
});

function showFormStatus(el, message, { error = false } = {}) {
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("error", Boolean(error && message));
  el.classList.toggle("show", Boolean(message));
}

function wireContactForm() {
  const form = document.getElementById("contact-form");
  const status = document.getElementById("form-status");
  if (!form || !status || form.id !== "contact-form") return;

  const submit = form.querySelector('button[type="submit"]');

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormStatus(status, "");

    if (typeof form.reportValidity === "function" && !form.reportValidity()) {
      showFormStatus(status, "Please fill in the required fields.", { error: true });
      return;
    }

    const body = {
      name: (form.name?.value || "").trim(),
      firm: (form.firm?.value || "").trim(),
      email: (form.email?.value || "").trim(),
      phone: (form.phone?.value || "").trim() || null,
      reason: (form.reason?.value || "other").trim(),
      message: (form.message?.value || "").trim() || null,
    };

    if (submit) submit.disabled = true;
    showFormStatus(status, "Sending…");

    try {
      const res = await fetch(`${CONTROL_PLANE_URL}/v1/contact`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
      });
      let detail = "";
      let okMessage = "";
      try {
        const data = await res.json();
        detail = typeof data?.detail === "string"
          ? data.detail
          : Array.isArray(data?.detail)
            ? data.detail.map((d) => d.msg || JSON.stringify(d)).join(" ")
            : "";
        okMessage = data?.message || "";
      } catch {
        /* non-JSON */
      }

      if (!res.ok) {
        showFormStatus(
          status,
          detail || "Could not send your message. Email sales@casesifter.com instead.",
          { error: true },
        );
        if (submit) submit.disabled = false;
        return;
      }

      showFormStatus(status, okMessage || "Thanks — we'll be in touch within one business day.");
      form.reset();
    } catch {
      showFormStatus(
        status,
        "Could not reach the contact service. Email sales@casesifter.com instead.",
        { error: true },
      );
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}
