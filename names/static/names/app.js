const toast = (message) => zUIkit.notification(message, { pos: "bottom-center", status: "danger" });

function say(button) {
  const utterance = new SpeechSynthesisUtterance(button.dataset.say);
  utterance.lang = button.dataset.lang || "en-IN";
  speechSynthesis.cancel();
  speechSynthesis.speak(utterance);
}

async function playAudio(button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const response = await fetch(button.dataset.audio);
    if (!response.ok) throw new Error(await response.text());
    new Audio(URL.createObjectURL(await response.blob())).play();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function copy(button) {
  await navigator.clipboard.writeText(document.querySelector(button.dataset.copy).value);
  button.textContent = "Copied";
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-say], [data-audio], [data-copy]");
  if (!button) return;
  if (button.dataset.say) say(button);
  else if (button.dataset.audio) playAudio(button);
  else copy(button);
});

document.body.addEventListener("htmx:responseError", (event) => toast(event.detail.xhr.responseText));
document.body.addEventListener("htmx:sendError", () => toast("Couldn't reach the server. Check your connection."));
document.body.addEventListener("htmx:afterRequest", (event) => {
  if (event.detail.successful && event.target.matches("[data-reset-on-add]")) {
    event.target.querySelector("[name=text]").value = "";
  }
});
