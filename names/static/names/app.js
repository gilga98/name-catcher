const SOMETHING_WENT_WRONG = "Something went wrong. Please try again.";

// The toolkit renders messages as HTML, so they're always escaped first.
function toast(message) {
  const safe = document.createElement("div");
  safe.textContent = message;
  zUIkit.notification(safe.innerHTML, { pos: "bottom-center", status: "danger" });
}

// Our errors are plain text. Anything else is an error page from somewhere along the way, not a message for people.
function errorMessage(contentType, body) {
  return (contentType || "").startsWith("text/plain") ? body : SOMETHING_WENT_WRONG;
}

const FALLBACK_VOICE = "en-IN";
const VOICE_START_MS = 3000;
const NO_SOUND = "This device couldn't read the name aloud. Check that its speech voices are installed.";

// Asked early because the first answer can come back empty.
window.speechSynthesis?.getVoices();

// The device's own voices come first, because online ones can stay silent without reporting an error.
function voiceFor(lang) {
  const language = lang.split("-")[0];
  const tag = (voice) => voice.lang.replace("_", "-");
  const matching = speechSynthesis.getVoices().filter((voice) => tag(voice).split("-")[0] === language);
  return matching.find((voice) => voice.localService && tag(voice) === lang)
    || matching.find((voice) => voice.localService)
    || matching[0];
}

let activeUtterance;
let voiceTimer;

// Some voices fail with an error and others never start, so both count as silence. A newer tap cancels this one, which isn't.
function speak(text, lang, voice, onSilent) {
  const utterance = new SpeechSynthesisUtterance(text);
  activeUtterance = utterance;
  utterance.lang = lang;
  if (voice) utterance.voice = voice;
  const cleanup = () => {
    clearTimeout(voiceTimer);
    if (activeUtterance === utterance) activeUtterance = null;
  };
  const silent = () => {
    cleanup();
    speechSynthesis.cancel();
    onSilent();
  };
  utterance.addEventListener("start", () => clearTimeout(voiceTimer));
  utterance.addEventListener("end", cleanup);
  utterance.addEventListener("error", (event) => {
    cleanup();
    if (event.error !== "canceled" && event.error !== "interrupted") silent();
  });
  voiceTimer = setTimeout(silent, VOICE_START_MS);
  speechSynthesis.speak(utterance);
}

function say(button) {
  if (!window.speechSynthesis) {
    toast(NO_SOUND);
    return;
  }
  clearTimeout(voiceTimer);
  if (speechSynthesis.speaking || speechSynthesis.pending) {
    speechSynthesis.cancel();
  }
  speechSynthesis.resume(); // Some browsers stay paused after the page has been in the background.
  const spelled = () => speak(button.dataset.fallback, FALLBACK_VOICE, voiceFor(FALLBACK_VOICE), () => toast(NO_SOUND));
  const voice = voiceFor(button.dataset.lang);
  if (voice) speak(button.dataset.say, button.dataset.lang, voice, spelled);
  else spelled();
}

const COPIED_MS = 2000;

async function copy(button) {
  await navigator.clipboard.writeText(document.querySelector(button.dataset.copy).value);
  const label = button.querySelector("[data-label]");
  const original = label.textContent;
  label.textContent = "Copied";
  setTimeout(() => { label.textContent = original; }, COPIED_MS);
}

function share(button) {
  // Dismissing the phone's share sheet rejects, and that isn't a failure.
  navigator.share({ url: document.querySelector(button.dataset.share).value }).catch(() => {});
}

// Only phones and some browsers can share, so the button appears only where it works.
if (navigator.share) {
  document.querySelectorAll("[data-share]").forEach((button) => { button.hidden = false; });
}

// The board's tabs. The hash keeps a person's place through the full reloads some actions need.
function setupTabs(board) {
  const links = board.querySelectorAll("[data-tab]");
  const panels = board.querySelectorAll("[data-panel]");

  function show(tab) {
    board.dataset.active = tab;
    panels.forEach((panel) => panel.classList.toggle("is-active", panel.dataset.panel === tab));
    links.forEach((link) => {
      if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  links.forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    show(link.dataset.tab);
    history.replaceState(null, "", link.getAttribute("href"));
    window.scrollTo(0, 0);
  }));

  const start = location.hash.replace("#tab-", "");
  if ([...links].some((link) => link.dataset.tab === start)) show(start);
}

// One question at a time. Without scripts, every question simply shows at once.
function setupWizard(form) {
  const steps = [...form.querySelectorAll("[data-step]")];
  const back = form.querySelector("[data-wizard-back]");
  const next = form.querySelector("[data-wizard-next]");
  const progress = form.querySelector("[data-wizard-progress]");
  progress.replaceChildren(...steps.map(() => document.createElement("i")));
  let current = 0;

  function go(index, { focus = true } = {}) {
    current = index;
    steps.forEach((step, position) => step.classList.toggle("is-current", position === index));
    [...progress.children].forEach((bar, position) => bar.classList.toggle("is-done", position <= index));
    back.hidden = index === 0;
    form.classList.toggle("on-last", index === steps.length - 1);
    if (focus) steps[index].querySelector(".step-q").focus({ preventScroll: true });
  }

  next.addEventListener("click", () => go(current + 1));
  back.addEventListener("click", () => go(current - 1));
  // Enter means "next question" until the last one, where it submits.
  form.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.matches("input") || current === steps.length - 1) return;
    event.preventDefault();
    go(current + 1);
  });
  form.classList.add("is-wizard");
  go(0, { focus: false });
}

document.querySelectorAll("[data-board]").forEach(setupTabs);
document.querySelectorAll("[data-wizard]").forEach(setupWizard);

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
  const toggle = event.target.closest("[data-password-toggle]");
  if (toggle) {
    const wrap = toggle.closest(".password-wrap");
    const input = wrap?.querySelector("[data-password-input]");
    if (input) {
      const isPassword = input.type === "password";
      input.type = isPassword ? "text" : "password";
      toggle.setAttribute("aria-label", isPassword ? "Hide password" : "Show password");
      toggle.setAttribute("title", isPassword ? "Hide password" : "Show password");
    }
    return;
  }
  if (event.target.closest("[data-open-save-modal]")) {
    const modal = document.getElementById("save-board-modal");
    if (modal) (typeof modal.showModal === "function" ? modal.showModal() : (modal.open = true));
    return;
  }
  if (event.target.closest("[data-close-save-modal]")) {
    const modal = document.getElementById("save-board-modal");
    if (modal) (typeof modal.close === "function" ? modal.close() : (modal.open = false));
    return;
  }
  const sound = event.target.closest("[data-sound]");
  if (sound) document.querySelector("[data-starts-with]").value = sound.dataset.sound;
  const button = event.target.closest("[data-say], [data-copy], [data-share]");
  if (!button) return;
  if (button.dataset.say) say(button);
  else if (button.dataset.share) share(button);
  else copy(button);
});

const IDEA_SENT_MS = 1000;
const IDEA_LEAVE_MS = 250;

// The full message can be off screen, so the row itself says it worked before it goes.
function ideaSent(button) {
  const row = button.closest("li");
  button.disabled = true;
  row.classList.add("is-sent");
  setTimeout(() => row.classList.add("is-leaving"), IDEA_SENT_MS);
  setTimeout(() => row.remove(), IDEA_SENT_MS + IDEA_LEAVE_MS);
}

document.body.addEventListener("htmx:responseError", (event) => {
  const { xhr } = event.detail;
  toast(errorMessage(xhr.getResponseHeader("Content-Type"), xhr.responseText));
});
document.body.addEventListener("htmx:sendError", () => toast("Couldn't reach the server. Check your connection."));
document.body.addEventListener("htmx:afterRequest", (event) => {
  if (event.detail.successful && event.target.matches("[data-reset-on-add]")) {
    event.target.querySelector("[name=text]").value = "";
  }
});
