// Shows a relative the card the parents would get. Nothing reaches the parents until they send it from here.
const previewRoot = document.getElementById("family-preview");
const pathSegments = window.location.pathname.split("/").filter(Boolean);
const boardToken = pathSegments[1] || "guest";
const STORAGE_KEY = `sent_names_${boardToken}`;

function closePreview() {
  previewRoot.querySelector("dialog")?.close();
}

function renderSentPill(text, animate) {
  const shelf = document.getElementById("guest-sent-shelf");
  const pills = document.getElementById("guest-sent-pills");
  if (!shelf || !pills || !text) return;
  if ([...pills.children].some((pill) => pill.dataset.name.toLowerCase() === text.toLowerCase())) return;
  const pill = document.createElement("span");
  pill.className = "guest-sent-pill" + (animate ? " pill-pop" : "");
  pill.dataset.name = text;
  pill.innerHTML = `<strong>${escapeHtml(text)}</strong><span class="pill-check"><svg class="icon" aria-hidden="true" focusable="false"><use href="#i-check"></use></svg></span>`;
  pills.appendChild(pill);
  shelf.hidden = false;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function recordSentName(text) {
  if (!text) return;
  try {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]");
    if (!stored.some((item) => item.toLowerCase() === text.toLowerCase())) {
      stored.push(text);
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    }
  } catch {}
  renderSentPill(text, true);
}

function loadSentNames() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]");
    stored.forEach((item) => renderSentPill(item, false));
  } catch {}
}

// The toolkit's error messages sit underneath an open dialog, so a failed send has to close it for them to be seen.
function afterSend(form, successful) {
  closePreview();
  if (!successful) return;
  const text = form.elements.text.value;
  const typed = document.getElementById("add-text");
  if (typed && typed.value.trim().toLowerCase() === text.trim().toLowerCase()) typed.value = "";
  const idea = [...document.querySelectorAll("[data-idea]")].find(
    (button) => button.value.toLowerCase() === text.toLowerCase(),
  );
  if (idea && typeof ideaSent === "function") ideaSent(idea);
  recordSentName(text);
  const result = document.getElementById("add-result");
  if (result) {
    result.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

previewRoot.addEventListener("htmx:afterSwap", (event) => {
  if (event.target === previewRoot) {
    const dialog = previewRoot.querySelector("dialog");
    if (dialog && !dialog.open) {
      dialog.showModal();
    }
  }
});

// Removing it stops any card still checking on its details.
previewRoot.addEventListener("close", () => previewRoot.replaceChildren(), true);

previewRoot.addEventListener("click", (event) => {
  // A click landing on the dialog itself, rather than anything in it, is a click on the backdrop.
  if (event.target.closest("[data-close-preview]") || event.target.matches("dialog")) {
    closePreview();
  }
});

document.body.addEventListener("htmx:afterRequest", (event) => {
  if (event.target.matches("[data-send-name]")) {
    afterSend(event.target, event.detail.successful);
  }
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", loadSentNames);
} else {
  loadSentNames();
}
