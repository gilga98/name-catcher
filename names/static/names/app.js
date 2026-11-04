const toast = (message) => zUIkit.notification(message, { pos: "bottom-center", status: "danger" });

function hasVoiceFor(lang) {
  const voices = speechSynthesis.getVoices();
  const language = lang.split("-")[0];
  return !voices.length || voices.some((voice) => voice.lang.replace("_", "-").startsWith(language));
}

// The device's voice is free and instant, so only fall back to the paid one when it can't say the name.
function say(button) {
  const lang = button.dataset.lang || "en-IN";
  const paid = button.parentElement.querySelector("[data-audio]");
  if (paid && !hasVoiceFor(lang)) {
    playAudio(paid);
    return;
  }
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

function dismissCookieNotice() {
  document.getElementById("cookie-notice").hidden = true;
  try {
    localStorage.setItem("cookie-notice-seen", "1");
  } catch {
    // Private browsing can block storage. The notice just comes back next time.
  }
}

function showCookieNotice() {
  let seen = false;
  try {
    seen = localStorage.getItem("cookie-notice-seen") === "1";
  } catch {
    seen = false;
  }
  document.getElementById("cookie-notice").hidden = seen;
}

showCookieNotice();

// A parent's own photo lives in this browser only, so swap it in wherever this baby's picture appears.
document.querySelectorAll("[data-baby-photo]").forEach((image) => {
  image.dataset.original = image.getAttribute("src");
  try {
    const photo = localStorage.getItem(`baby-photo-${image.dataset.babyPhoto}`);
    if (photo) image.src = photo;
  } catch {
    // Storage is blocked, so the illustrated picture stays.
  }
});

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-dismiss-cookies]")) dismissCookieNotice();
  const button = event.target.closest("[data-say], [data-audio], [data-copy]");
  if (!button) return;
  if (button.dataset.say) say(button);
  else if (button.dataset.audio) playAudio(button);
  else copy(button);
});

document.body.addEventListener("htmx:responseError", (event) => toast(event.detail.xhr.responseText));
document.body.addEventListener("htmx:sendError", () => toast("Couldn't reach the server. Check your connection."));
document.body.addEventListener("htmx:afterRequest", (event) => {
  if (event.detail.successful && event.target.matches("[data-idea]")) event.target.closest("li").remove();
  if (event.detail.successful && event.target.matches("[data-reset-on-add]")) {
    event.target.querySelector("[name=text]").value = "";
  }
});
