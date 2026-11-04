import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

createRequire(import.meta.url)(fileURLToPath(new URL("../names/static/names/engine.js", import.meta.url)));
const { pick, similarity } = globalThis.NameEngine;

const entry = (id, k, r = "uncommon", g = []) => ({ id, t: k, k, r, g });
const state = (extra = {}) => ({ liked: [], passed: [], seen: [], avoid: [], themes: [], uniqueness: "any", startsWith: "", ...extra });

test("skips names the family has seen or wants to avoid", () => {
  const picks = pick([entry(1, "ahana"), entry(2, "tejas"), entry(3, "nila")], state({ seen: ["ahana"], avoid: ["nila"] }));
  assert.deepEqual(picks.map((e) => e.id), [2]);
});

test("honours starts-with as a hard limit", () => {
  const picks = pick([entry(1, "chetan"), entry(2, "tejas")], state({ startsWith: "che" }));
  assert.deepEqual(picks.map((e) => e.id), [1]);
});

test("prefers matching themes and the wanted rarity", () => {
  const entries = [entry(1, "abcd", "familiar"), entry(2, "wxyz", "rare", ["Light"])];
  assert.equal(pick(entries, state({ themes: ["Light"], uniqueness: "rare" }), { count: 1 })[0].id, 2);
});

test("learns from what was kept and passed", () => {
  const entries = [entry(1, "tejasvi"), entry(2, "mohana")];
  assert.equal(pick(entries, state({ liked: ["tejas"] }), { count: 1 })[0].id, 1);
  assert.equal(pick(entries, state({ passed: ["tejas"] }), { count: 1 })[0].id, 2);
});

test("favours the baby's own birth sound, then the star's other sounds", () => {
  const entries = [entry(1, "wxyz"), entry(2, "tejas"), entry(3, "mohana")];
  const birth = { birthSound: "ti", birthSounds: ["mo", "ta", "ti", "tu"] };
  assert.equal(pick(entries, state(birth), { count: 1 })[0].id, 3);
  assert.equal(pick([...entries, entry(4, "tilak")], state(birth), { count: 1 })[0].id, 4);
});

test("birth sounds are a preference, not a limit", () => {
  const picks = pick([entry(1, "wxyz")], state({ birthSound: "ti", birthSounds: ["ti"] }));
  assert.deepEqual(picks.map((e) => e.id), [1]);
});

test("varies the set instead of returning near-identical names", () => {
  const entries = [entry(1, "tejas"), entry(2, "tejasa"), entry(3, "mohan")];
  assert.deepEqual(pick(entries, state(), { count: 2 }).map((e) => e.id).sort(), [1, 3]);
});

test("more-like stays close to the anchor", () => {
  const picks = pick([entry(1, "tejasvi"), entry(2, "mohana")], state(), { anchor: "tejas" });
  assert.deepEqual(picks.map((e) => e.id), [1]);
});

test("gives the same answer for the same state", () => {
  const entries = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => entry(n, `name${"abcdefgh"[n - 1]}`));
  assert.deepEqual(pick(entries, state()), pick(entries, state()));
});

test("similarity is 1 for the same key and 0 for nothing shared", () => {
  assert.equal(similarity("abc", "abc"), 1);
  assert.equal(similarity("abc", "xyz"), 0);
});
