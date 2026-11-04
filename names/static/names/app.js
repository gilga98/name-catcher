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

const VOICE_WAIT_MS = 1500;
const VOICE_ATTEMPTS = 20;

// The server records in the background and answers 202 until it's ready.
async function fetchVoice(url) {
  for (let attempt = 0; attempt < VOICE_ATTEMPTS; attempt++) {
    const response = await fetch(url);
    if (response.status === 200) return response.blob();
    if (response.status !== 202) throw new Error(await response.text());
    await new Promise((resolve) => setTimeout(resolve, VOICE_WAIT_MS));
  }
  throw new Error("The natural voice is taking too long. Try your device's voice.");
}

async function playAudio(button) {
  const label = button.textContent;
  if (button.dataset.ready) {
    new Audio(button.dataset.ready).play();
    button.textContent = label === "Tap to play" ? "Natural voice" : label;
    return;
  }
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const source = URL.createObjectURL(await fetchVoice(button.dataset.audio));
    button.dataset.ready = source;
    button.textContent = label;
    await new Audio(source).play();
  } catch (error) {
    // Browsers may block sound that starts long after the tap. The recording is ready for the next one.
    if (error.name === "NotAllowedError") button.textContent = "Tap to play";
    else {
      button.textContent = label;
      toast(error.message);
    }
  } finally {
    button.disabled = false;
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
