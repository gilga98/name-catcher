// Shows a relative the card the parents would get. Nothing reaches the parents until they send it from here.
const previewRoot = document.getElementById("family-preview");

function closePreview() {
  previewRoot.querySelector("dialog")?.close();
}

// The toolkit's error messages sit underneath an open dialog, so a failed send has to close it for them to be seen.
function afterSend(form, successful) {
  closePreview();
  if (!successful) return;
  const text = form.elements.text.value;
  const typed = document.getElementById("add-text");
  if (typed.value.trim() === text) typed.value = "";
  const idea = [...document.querySelectorAll("[data-idea]")].find((button) => button.value === text);
  if (idea) ideaSent(idea);
}

previewRoot.addEventListener("htmx:afterSwap", (event) => {
  if (event.target === previewRoot) previewRoot.querySelector("dialog")?.showModal();
});
// Removing it stops any card still checking on its details.
previewRoot.addEventListener("close", () => previewRoot.replaceChildren(), true);
previewRoot.addEventListener("click", (event) => {
  // A click landing on the dialog itself, rather than anything in it, is a click on the backdrop.
  if (event.target.closest("[data-close-preview]") || event.target.matches("dialog")) closePreview();
});
previewRoot.addEventListener("htmx:afterRequest", (event) => {
  if (event.target.matches("[data-send-name]")) afterSend(event.target, event.detail.successful);
});
