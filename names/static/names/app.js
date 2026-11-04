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

function hasVoiceFor(lang) {
  const voices = speechSynthesis.getVoices();
  const language = lang.split("-")[0];
  return !voices.length || voices.some((voice) => voice.lang.replace("_", "-").startsWith(language));
}

const FALLBACK_VOICE = "en-IN";

// Asked early because the first answer can come back empty. A device with no voice for this language would stay silent.
window.speechSynthesis?.getVoices();

function say(button) {
  const inLanguage = hasVoiceFor(button.dataset.lang);
  const utterance = new SpeechSynthesisUtterance(inLanguage ? button.dataset.say : button.dataset.fallback);
  utterance.lang = inLanguage ? button.dataset.lang : FALLBACK_VOICE;
  speechSynthesis.cancel();
  speechSynthesis.speak(utterance);
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
  if (event.detail.successful && event.target.matches("[data-idea]")) ideaSent(event.target);
  if (event.detail.successful && event.target.matches("[data-reset-on-add]")) {
    event.target.querySelector("[name=text]").value = "";
  }
});
