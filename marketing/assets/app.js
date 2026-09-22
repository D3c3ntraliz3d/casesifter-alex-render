document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", () => links.classList.toggle("open"));
  }

  const form = document.getElementById("contact-form");
  const status = document.getElementById("form-status");
  if (form && status) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      status.textContent = "Thanks — we'll be in touch within one business day.";
      status.classList.add("show");
      form.reset();
    });
  }

  wireDownloads();
});

async function wireDownloads() {
  const anchors = document.querySelectorAll("[data-download]");
  if (!anchors.length) return;

  let catalog = null;
  try {
    const res = await fetch("assets/downloads.json", { cache: "no-store" });
    if (res.ok) catalog = await res.json();
  } catch {
    /* keep placeholders */
  }

  anchors.forEach((el) => {
    const key = el.getAttribute("data-download");
    const entry = catalog?.[key];
    if (!entry?.url) return;
    el.href = entry.url;
    el.setAttribute("download", entry.filename || "");
    if (entry.label && el.id === "download-windows") {
      el.textContent = entry.label;
    }
  });

  const meta = document.getElementById("download-meta");
  if (meta && catalog?.windows?.version) {
    meta.textContent = `Windows 10/11 · NSIS installer · v${catalog.windows.version}`;
  }
  const notes = document.getElementById("download-notes");
  if (notes && catalog?.windows?.notes) {
    notes.textContent = catalog.windows.notes;
  }
}
