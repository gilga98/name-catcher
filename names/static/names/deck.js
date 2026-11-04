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
  let drag = null;
  let lastUndoUrl = "";

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
    const name = card.querySelector(".name").firstChild.textContent.trim();
    status.textContent = button.dataset.taste === "liked" ? `Kept ${name}.` : `Passed on ${name}.`;
  });

  undo.addEventListener("click", () => {
    if (!lastUndoUrl) return;
    htmx.ajax("POST", lastUndoUrl, { target: "#review", swap: "afterbegin" });
    lastUndoUrl = "";
    undo.hidden = true;
  });

  const refresh = () => { empty.hidden = Boolean(topCard()); };
  document.body.addEventListener("htmx:afterSettle", refresh);
  // If the server refused, put the card back rather than losing it.
  document.body.addEventListener("htmx:responseError", () => {
    review.querySelectorAll(".fly-left, .fly-right").forEach((card) => {
      card.classList.remove("fly-left", "fly-right");
      place(card, 0);
    });
  });
  refresh();

  // Baby photo. It's only ever kept in this browser, so nothing about the child reaches our servers.
  const input = document.getElementById("baby-photo-input");
  const remove = document.getElementById("baby-photo-remove");
  const key = `baby-photo-${input.dataset.board}`;
  const avatar = document.querySelector("[data-baby-photo]");
  const original = avatar.dataset.original || avatar.getAttribute("src");

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
    avatar.src = photo;
    remove.hidden = false;
    try { localStorage.setItem(key, photo); } catch { /* Shows until the page closes if storage is blocked. */ }
    input.value = "";
  });

  remove.addEventListener("click", () => {
    try { localStorage.removeItem(key); } catch { /* Nothing was stored. */ }
    avatar.src = original;
    remove.hidden = true;
  });
})();
