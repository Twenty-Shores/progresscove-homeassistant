/** The icon card's tile resolution: which entities become tiles, and in what shape.
 *
 *  Every card bug so far was found by looking at a dashboard rather than by a test, among them a
 *  duplicate tile and an editor that froze its form. These drive the real class, not a
 *  re-implementation of it.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadCard, fakeHass } from "./harness.mjs";

const { defined } = await loadCard("progresscove-icon-card.js");
const Card = defined.get("progresscove-icon-card");
const Editor = defined.get("progresscove-icon-card-editor");

const LIST = "todo.home_repeating";
const PATH = "Home › Repeating home tasks";

function entity(id, path, nodeId, extra = {}) {
  return { entity_id: id, attributes: { path, node_id: nodeId, ...extra } };
}

function world() {
  return {
    // A list's friendly_name IS its full path; that is what the card matches on. Confirmed
    // against the running instance rather than assumed.
    [LIST]: { entity_id: LIST, attributes: { friendly_name: PATH } },
    "button.bins": entity("button.bins", `${PATH} › Bins`, "node-bins", { actionable: true }),
    "button.plants": entity("button.plants", `${PATH} › Plants`, "node-plants", {
      actionable: false,
    }),
    "switch.bins": entity("switch.bins", `${PATH} › Bins`, "node-bins", { actionable: true }),
    "button.elsewhere": entity("button.elsewhere", "Home › Shopping › Butter", "node-butter"),
  };
}

function card(config, states = world()) {
  const instance = new Card();
  instance.setConfig(config);
  instance._hass = fakeHass(states);
  return instance;
}

test("a followed project picks up every task under it", () => {
  const tiles = card({ groups: [LIST] })._tiles();
  assert.deepEqual(
    tiles.map(t => t.attributes.node_id).sort(),
    ["node-bins", "node-plants"]
  );
});

test("a task outside the followed project is not pulled in", () => {
  const tiles = card({ groups: [LIST] })._tiles();
  assert.ok(!tiles.some(t => t.attributes.node_id === "node-butter"));
});

test("one task added as both a button and a switch draws ONE tile", () => {
  // Accepting both domains once doubled the tile.
  const tiles = card({ groups: [LIST] })._tiles();
  const bins = tiles.filter(t => t.attributes.node_id === "node-bins");
  assert.equal(bins.length, 1);
});

test("a prefix match does not swallow a sibling with a longer name", () => {
  const states = world();
  states["todo.home_repeat2"] = {
    entity_id: "todo.home_repeat2",
    attributes: { friendly_name: `${PATH} 2` },
  };
  states["button.other"] = entity("button.other", `${PATH} 2 › Other`, "node-other");
  const tiles = card({ groups: [LIST] }, states)._tiles();
  assert.ok(!tiles.some(t => t.attributes.node_id === "node-other"));
});

test("nothing configured draws nothing rather than every button in the house", () => {
  assert.deepEqual(card({})._tiles(), []);
});

test("a pinned entity that does not exist yet is skipped, not rendered as a hole", () => {
  const tiles = card({ entities: ["button.bins", "button.not_yet"] })._tiles();
  assert.equal(tiles.length, 1);
});

test("switches are eligible for a tile, not just buttons", () => {
  const states = world();
  delete states["button.bins"];
  const tiles = card({ groups: [LIST] }, states)._tiles();
  assert.ok(tiles.some(t => t.attributes.node_id === "node-bins"));
});

test("pressing a button tile presses the button", async () => {
  const instance = card({ groups: [LIST] });
  await instance._press("button.plants");
  assert.deepEqual(instance._hass.calls, [
    { domain: "button", service: "press", data: { entity_id: "button.plants" }, target: undefined },
  ]);
});

test("pressing a switch tile turns it OFF, because on means open", async () => {
  const instance = card({ groups: [LIST] });
  await instance._press("switch.bins");
  assert.equal(instance._hass.calls[0].domain, "switch");
  assert.equal(instance._hass.calls[0].service, "turn_off");
});

test("the editor shows a saved card's own settings", () => {
  // The form was once built only on the first update, so a card saved with a followed project
  // opened with an empty picker and looked unconfigured.
  const editor = new Editor();
  editor.setConfig({ groups: [LIST], columns: 4 });
  editor.hass = fakeHass();
  const form = editor.children[0];
  assert.deepEqual(form.data.groups, [LIST]);
  assert.equal(form.data.columns, 4);
});

test("the editor keeps up when the config arrives after hass", () => {
  // The real order: setConfig fires first and returns early with no hass, then hass arrives.
  const editor = new Editor();
  editor.hass = fakeHass();
  editor.setConfig({ groups: [LIST] });
  assert.deepEqual(editor.children[0].data.groups, [LIST]);
});

test("the editor does not rebuild the form under the user's cursor", () => {
  const editor = new Editor();
  editor.setConfig({ groups: [] });
  editor.hass = fakeHass();
  const first = editor.children[0];
  editor.setConfig({ groups: [LIST] });
  assert.equal(editor.children.length, 1, "a second form was appended");
  assert.equal(editor.children[0], first, "the form was replaced rather than updated");
});
