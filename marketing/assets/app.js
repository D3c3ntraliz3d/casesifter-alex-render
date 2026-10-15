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
});
