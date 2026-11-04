// Picks names in the browser, from a shard of the catalogue, so most suggestions cost nothing and arrive instantly.
// The server still checks every pick. This code decides what looks good, never what's allowed.
(function (root) {
  const WANTED = 6;
  const ENOUGH = 3;
  const RARITY = { familiar: 0, uncommon: 1, rare: 2 };

  function bigrams(text) {
    const pairs = new Set();
    for (let i = 0; i < text.length - 1; i++) pairs.add(text.slice(i, i + 2));
    return pairs;
  }

  function similarity(a, b) {
    if (a === b) return 1;
    const left = bigrams(a);
    const right = bigrams(b);
    if (!left.size || !right.size) return 0;
    let shared = 0;
    left.forEach((pair) => { if (right.has(pair)) shared++; });
    return (2 * shared) / (left.size + right.size);
  }

  const closestTo = (key, keys) => keys.reduce((best, other) => Math.max(best, similarity(key, other)), 0);

  // Stable, so the same state gives the same list, but it still breaks ties differently as a board grows.
  function jitter(id, seed) {
    return ((id * 2654435761 + seed * 40503) >>> 0) / 4294967296;
  }

  function rarityFit(entry, wanted) {
    if (wanted === "any") return 0.5 * RARITY[entry.r];
    return 2 - 2 * Math.abs(RARITY[entry.r] - RARITY[wanted]);
  }

  // The sound for the baby's own pada counts most, then the star's other sounds. It's a preference, not a limit:
  // parents who want it as a limit have "Starts with".
  function birthFit(entry, state) {
    if (state.birthSound && entry.k.startsWith(state.birthSound)) return 4;
    return (state.birthSounds || []).some((sound) => entry.k.startsWith(sound)) ? 2 : 0;
  }

  function score(entry, state, anchor) {
    const themes = entry.g.filter((genre) => state.themes.includes(genre)).length;
    return 3 * themes
      + birthFit(entry, state)
      + rarityFit(entry, state.uniqueness)
      + 2 * closestTo(entry.k, state.liked)
      - 1.5 * closestTo(entry.k, state.passed)
      + (anchor ? 6 * similarity(entry.k, anchor) : 0)
      + 0.25 * jitter(entry.id, state.seen.length);
  }

  function allowed(entry, state) {
    const blocked = [...state.seen, ...state.avoid];
    if (closestTo(entry.k, blocked) >= 0.9) return false;
    return !state.startsWith || entry.k.startsWith(state.startsWith);
  }

  // Take the best, then penalise whatever sounds like what's already chosen, so the set isn't six variations of one name.
  function pick(entries, state, { count = WANTED, anchor = "" } = {}) {
    const pool = entries.filter((entry) => allowed(entry, state) && (!anchor || similarity(entry.k, anchor) >= 0.25));
    const scored = pool.map((entry) => ({ entry, score: score(entry, state, anchor) }));
    const chosen = [];
    while (chosen.length < count && scored.length) {
      let bestIndex = 0;
      let bestScore = -Infinity;
      const taken = chosen.map((entry) => entry.k);
      scored.forEach((item, index) => {
        const adjusted = item.score - 3 * closestTo(item.entry.k, taken);
        if (adjusted > bestScore) { bestScore = adjusted; bestIndex = index; }
      });
      chosen.push(scored.splice(bestIndex, 1)[0].entry);
    }
    return chosen;
  }

  root.NameEngine = { pick, similarity, ENOUGH };
  if (typeof document === "undefined") return;

  const data = document.getElementById("engine-data");
  const actions = document.getElementById("suggest-actions");
  if (!data || !actions) return;

  const state = JSON.parse(data.textContent);
  let shard;
  const loadShard = () => {
    shard ??= fetch(actions.dataset.shardUrl).then((response) => response.json()).then((body) => body.names);
    return shard;
  };

  function addToReview(ids, swap) {
    return htmx.ajax("POST", actions.dataset.pickUrl, { target: "#review", swap, values: { id: ids } });
  }

  function offerAI() {
    document.getElementById("catalog-empty").hidden = false;
    const button = document.getElementById("suggest-ai");
    if (button) button.hidden = false;
    return button;
  }

  const deckSize = () => document.querySelectorAll("#review .name-card").length;
  let fetching = false;

  async function suggestLocally({ firstVisit = false, topUp = false } = {}) {
    if (fetching) return;
    fetching = true;
    const picks = pick(await loadShard(), state);
    fetching = false;
    picks.forEach((entry) => state.seen.push(entry.k));
    if (picks.length) addToReview(picks.map((entry) => entry.id), "beforeend");
    if (picks.length >= ENOUGH || (topUp && deckSize() > 0)) return;
    const button = offerAI();
    // A new family must see something. After that, spending a request is always their choice.
    if (firstVisit && !picks.length && button) button.click();
  }

  async function moreLike(button) {
    const picks = pick(await loadShard(), state, { count: 4, anchor: button.dataset.moreKey });
    if (picks.length >= 2) {
      picks.forEach((entry) => state.seen.push(entry.k));
      addToReview(picks.map((entry) => entry.id), "afterbegin");
      return;
    }
    if (state.aiAllowed) {
      htmx.ajax("POST", button.dataset.moreUrl, { target: "#ai-status", swap: "innerHTML" });
    } else {
      offerAI();
    }
  }

  document.addEventListener("click", (event) => {
    const taste = event.target.closest("[data-taste]");
    if (taste) state[taste.dataset.taste].push(taste.dataset.key);
    const more = event.target.closest("[data-more-key]");
    if (more) moreLike(more);
    if (event.target.closest("#suggest-local")) suggestLocally();
  });

  // Keep the deck stocked from the free catalogue, so parents never wait between cards.
  document.body.addEventListener("htmx:afterSettle", () => {
    if (deckSize() <= 2) suggestLocally({ topUp: true });
  });

  if (actions.dataset.isNew === "true") suggestLocally({ firstVisit: true });
  else if (deckSize() <= 2) suggestLocally({ topUp: true });
})(typeof window === "undefined" ? globalThis : window);
