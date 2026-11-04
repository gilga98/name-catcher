// The swipe deck. A swipe only ever presses the card's own Keep or Pass button, so touch, mouse,
// keyboard and screen readers all take one path through the server.
(function () {
  const review = document.getElementById("review");
  if (!review) return;

  const FLY_MS = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180;
  const undo = document.getElementById("undo-swipe");
  const status = document.getElementById("deck-status");
  const empty = document.getElementById("deck-empty");
  const topCard = () => review.querySelector(".name-card");
  const threshold = () => Math.min(120, review.clientWidth * 0.3);
  const hint = document.getElementById("deck-hint");
  const HINT_SEEN = "swipe-hint-seen";
  let drag = null;
  let lastUndoUrl = "";

  function learnedToSwipe() {
    hint.hidden = true;
    try { localStorage.setItem(HINT_SEEN, "1"); } catch { /* The hint just comes back next time. */ }
  }

  try { hint.hidden = localStorage.getItem(HINT_SEEN) === "1"; } catch { hint.hidden = false; }

  function place(card, dx) {
    card.style.transform = dx ? `translateX(${dx}px) rotate(${dx / 22}deg)` : "";
    card.style.setProperty("--keep", Math.max(0, Math.min(1, dx / threshold())));
    card.style.setProperty("--pass", Math.max(0, Math.min(1, -dx / threshold())));
  }

  function decide(card, liked) {
    const button = card.querySelector(liked ? "[data-taste=liked]" : "[data-taste=passed]");
    card.classList.remove("dragging");
    card.classList.add(liked ? "fly-right" : "fly-left");
    setTimeout(() => button.click(), FLY_MS);
  }

  review.addEventListener("pointerdown", (event) => {
    const card = event.target.closest(".name-card");
    if (!card || card !== topCard() || event.target.closest("button, a, input, summary")) return;
    drag = { card, startX: event.clientX, dx: 0 };
    card.setPointerCapture(event.pointerId);
    card.classList.add("dragging");
  });

  review.addEventListener("pointermove", (event) => {
    if (!drag) return;
    drag.dx = event.clientX - drag.startX;
    place(drag.card, drag.dx);
  });

  function release(accept) {
    if (!drag) return;
    const { card, dx } = drag;
    drag = null;
    if (accept && Math.abs(dx) >= threshold()) {
      decide(card, dx > 0);
    } else {
      card.classList.remove("dragging");
      place(card, 0);
    }
  }

  review.addEventListener("pointerup", () => release(true));
  review.addEventListener("pointercancel", () => release(false));

  document.addEventListener("keydown", (event) => {
    if (event.target.closest?.("input, textarea, select, summary") || event.altKey || event.metaKey || event.ctrlKey) return;
    if (!review.offsetParent) return; // Another tab is showing, so this person can't see what they'd be deciding on.
    const card = topCard();
    if (!card || (event.key !== "ArrowRight" && event.key !== "ArrowLeft")) return;
    event.preventDefault();
    decide(card, event.key === "ArrowRight");
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-taste]");
    if (!button) return;
    const card = button.closest(".name-card");
    lastUndoUrl = card.dataset.undoUrl;
    undo.hidden = false;
    learnedToSwipe();
    const name = card.querySelector(".name").firstChild.textContent.trim();
    status.textContent = button.dataset.taste === "liked" ? `Kept ${name}.` : `Passed on ${name}.`;
  });

  undo.addEventListener("click", () => {
    if (!lastUndoUrl) return;
    htmx.ajax("POST", lastUndoUrl, { target: "#review", swap: "afterbegin" });
    lastUndoUrl = "";
    undo.hidden = true;
  });

  const count = document.getElementById("deck-count");
  const shortlistCount = document.querySelector("[data-shortlist-count]");

  const refresh = () => {
    const left = review.querySelectorAll(".name-card").length;
    empty.hidden = left > 0;
    count.hidden = left === 0;
    count.lastElementChild.textContent = `${left} to go`;
    const kept = document.querySelectorAll("#shortlist .kept").length;
    shortlistCount.textContent = kept;
    shortlistCount.hidden = kept === 0;
  };
  document.body.addEventListener("htmx:afterSettle", refresh);
  // If the server refused, put the card back rather than losing it.
  document.body.addEventListener("htmx:responseError", () => {
    review.querySelectorAll(".fly-left, .fly-right").forEach((card) => {
      card.classList.remove("fly-left", "fly-right");
      place(card, 0);
    });
  });
  refresh();

  // Keeps two parents in step without a reload: their decisions, and names that reach the deck meanwhile.
  const SYNC_MS = 15000;
  let inFlight = 0;
  let syncing = false;
  let lastStamp = null;
  document.body.addEventListener("htmx:beforeRequest", () => { inFlight++; });
  document.body.addEventListener("htmx:afterRequest", () => { inFlight--; });

  function showNote(card, note) {
    const element = card.querySelector("[data-partner-note]");
    element.textContent = note.text;
    element.classList.toggle("did-keep", Boolean(note.text) && note.kept);
    element.classList.toggle("did-pass", Boolean(note.text) && !note.kept);
  }

  // Anything mid-flight could look like a new card, or be replaced under someone's finger.
  const busy = () => document.hidden || drag || inFlight || review.querySelector(".fly-left, .fly-right");

  async function sync() {
    // A phone waking up fires the timer and visibilitychange together, and two rounds would each fetch the new cards.
    if (syncing || busy()) return;
    syncing = true;
    let body;
    try {
      body = await (await fetch(review.dataset.decisionsUrl)).json();
    } catch {
      return; // Offline for a moment. The next round catches up.
    } finally {
      syncing = false;
    }
    if (busy()) return; // Something started during that fetch. The next round catches up.
    const fresh = [];
    Object.entries(body.notes).forEach(([id, note]) => {
      const card = review.querySelector(`[data-card-id="${id}"]`);
      if (card) showNote(card, note);
      else fresh.push(id);
    });
    if (fresh.length) {
      const query = fresh.map((id) => `id=${id}`).join("&");
      htmx.ajax("GET", `${review.dataset.cardsUrl}?${query}`, { target: "#review", swap: "beforeend" });
    }
    const pressing = document.querySelector("#shortlist:active");
    if (lastStamp !== null && body.stamp !== lastStamp && !pressing) {
      htmx.ajax("GET", review.dataset.shortlistUrl, { target: "#shortlist", swap: "outerHTML" });
    }
    if (!pressing) lastStamp = body.stamp;
  }

  setInterval(sync, SYNC_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });

  // Baby photo. It's only ever kept in this browser, so nothing about the child reaches our servers.
  const input = document.getElementById("baby-photo-input");
  const remove = document.getElementById("baby-photo-remove");
  const key = `baby-photo-${input.dataset.board}`;
  const avatars = document.querySelectorAll("[data-baby-photo]");
  const original = avatars[0].dataset.original || avatars[0].getAttribute("src");
  const showPicture = (source) => avatars.forEach((avatar) => { avatar.src = source; });

  function read() {
    try { return localStorage.getItem(key); } catch { return null; }
  }

  remove.hidden = !read();

  input.addEventListener("change", async () => {
    const [file] = input.files;
    if (!file) return;
    const bitmap = await createImageBitmap(file);
    const side = Math.min(bitmap.width, bitmap.height);
    const canvas = Object.assign(document.createElement("canvas"), { width: 160, height: 160 });
    // Redrawing also drops the file's metadata, such as GPS location.
    canvas.getContext("2d").drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, 160, 160);
    const photo = canvas.toDataURL("image/jpeg", 0.8);
    showPicture(photo);
    remove.hidden = false;
    try { localStorage.setItem(key, photo); } catch { /* Shows until the page closes if storage is blocked. */ }
    input.value = "";
  });

  remove.addEventListener("click", () => {
    try { localStorage.removeItem(key); } catch { /* Nothing was stored. */ }
    showPicture(original);
    remove.hidden = true;
  });
})();
