/** The projects card reads its tasks through services now, not off the entity's attributes.
 *
 *  It is the part with the most ways to go quietly wrong: two calls per project, either of which
 *  can fail, against counts that come from somewhere else entirely. The
 *  rule it has to keep is that a project with work left NEVER renders as an empty list, because an
 *  empty list on a card reads as "you finished everything".
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadCard, fakeHass } from "./harness.mjs";

const { defined } = await loadCard("progresscove-card.js");
const Card = defined.get("progresscove-card");

const LIST = "todo.home_shopping";
const NODE = "node-shopping";

const STATE = {
  [LIST]: {
    entity_id: LIST,
    attributes: {
      friendly_name: "Shopping",
      node_id: NODE,
      items_done: 1,
      items_total: 3,
    },
  },
};

const ITEMS = {
  response: {
    [LIST]: {
      items: [
        { uid: "groceries", summary: "Groceries", status: "needs_action" },
        { uid: "frozen", summary: "Frozen food", status: "completed" },
      ],
    },
  },
};

const NESTED = {
  response: {
    nested_items: {
      groceries: [
        { uid: "milk", summary: "Milk", done: true },
        { uid: "bread", summary: "Bread", done: false },
      ],
      frozen: [],
    },
  },
};

function card(responses) {
  const instance = new Card();
  instance.setConfig({ entities: [LIST] });
  instance._hass = fakeHass(STATE, responses);
  return instance;
}

const BOTH = {
  "todo.get_items": ITEMS,
  "progresscove.get_nested_items": NESTED,
};

test("items and their children arrive through the services", async () => {
  const instance = card(BOTH);
  await instance._refresh();
  const [project] = instance._items();
  assert.deepEqual(project.tasks.map(t => t.name), ["Groceries", "Frozen food"]);
  assert.deepEqual(project.tasks[0].subtasks.map(s => s.name), ["Milk", "Bread"]);
});

test("completion survives both hops", async () => {
  const instance = card(BOTH);
  await instance._refresh();
  const [project] = instance._items();
  assert.equal(project.tasks[0].done, false);
  assert.equal(project.tasks[1].done, true, "status: completed did not become done");
  assert.equal(project.tasks[0].subtasks[0].done, true);
});

test("the counts come from the attributes, not from what was fetched", async () => {
  // 3 total, but only 2 items came back. The header must still say 3: the counts are computed
  // over the whole tree server-side and are the honest number.
  const instance = card(BOTH);
  await instance._refresh();
  const [project] = instance._items();
  assert.equal(project.total, 3);
  assert.equal(project.done, 1);
});

test("counts render before any fetch has returned", () => {
  const instance = card(BOTH);
  const [project] = instance._items();
  assert.equal(project.total, 3, "the header waited for a service call");
  assert.deepEqual(project.tasks, []);
});

test("a failed items call leaves the counts standing rather than showing an empty project", async () => {
  const instance = card({
    "todo.get_items": new Error("unavailable"),
    "progresscove.get_nested_items": NESTED,
  });
  await instance._refresh();
  const [project] = instance._items();
  assert.equal(project.total, 3, "a failed fetch erased the count");
  assert.deepEqual(project.tasks, [], "a partial render would imply the rest are done");
});

test("a failed nested call still renders the items", async () => {
  const instance = card({
    "todo.get_items": ITEMS,
    "progresscove.get_nested_items": new Error("unavailable"),
  });
  await instance._refresh();
  const [project] = instance._items();
  assert.deepEqual(project.tasks, [], "one failed call must not half-render the project");
});

test("an unchanged state does not refetch", async () => {
  const instance = card(BOTH);
  await instance._refresh();
  const first = instance._hass.calls.length;
  await instance._refresh();
  assert.equal(instance._hass.calls.length, first, "refetched with nothing changed");
});

test("a changed count refetches", async () => {
  const instance = card(BOTH);
  await instance._refresh();
  const first = instance._hass.calls.length;
  instance._hass.states[LIST].attributes.items_done = 2;
  await instance._refresh();
  assert.ok(instance._hass.calls.length > first, "a completion did not refresh the card");
});

test("demo mode never calls a service", async () => {
  const instance = new Card();
  instance.setConfig({ demo: true });
  instance._hass = fakeHass(STATE, BOTH);
  await instance._refresh();
  assert.deepEqual(instance._hass.calls, []);
});

test("an unconfigured card asks for nothing", async () => {
  const instance = new Card();
  instance.setConfig({});
  instance._hass = fakeHass(STATE, BOTH);
  await instance._refresh();
  assert.deepEqual(instance._hass.calls, []);
});

test("get_items is asked for the ENTITY and get_nested_items for the NODE", async () => {
  const instance = card(BOTH);
  await instance._refresh();
  const items = instance._hass.calls.find(c => c.service === "get_items");
  const nested = instance._hass.calls.find(c => c.service === "get_nested_items");
  assert.deepEqual(items.target, { entity_id: LIST });
  assert.deepEqual(nested.data, { node_id: NODE });
});

test("a config naming a non-todo entity produces no service call", async () => {
  // The config is free text the user can edit. A todo service could never reach a lock anyway
  // (HA scopes entity services to their own platform), but asking would put a confusing failure
  // in the log for something the card can rule out itself.
  const instance = new Card();
  instance.setConfig({ entities: ["lock.front_door"] });
  instance._hass = fakeHass(
    { "lock.front_door": { entity_id: "lock.front_door", attributes: { node_id: "x" } } },
    BOTH
  );
  await instance._refresh();
  assert.deepEqual(instance._hass.calls, []);
});

test("an entity with no node_id is skipped rather than asked about with undefined", async () => {
  const instance = new Card();
  instance.setConfig({ entities: ["todo.someone_elses"] });
  instance._hass = fakeHass(
    { "todo.someone_elses": { entity_id: "todo.someone_elses", attributes: {} } },
    BOTH
  );
  await instance._refresh();
  assert.deepEqual(instance._hass.calls, []);
});

test("task names from the services are escaped before they reach innerHTML", () => {
  // Names are user data and the card builds its DOM with innerHTML. They arrive from a service
  // response now rather than an attribute, which is a new path for the same old hazard.
  //
  // Asserts on LITERAL markup, not on a substring: the escaped text `&lt;img ... onerror=alert(1)`
  // still contains "onerror=alert", so a naive `includes` check reports an XSS that is not there.
  const XSS = '<img src=x onerror=alert(1)>';
  const instance = new Card();
  instance.setConfig({ entities: [LIST] });
  instance._hass = fakeHass(
    {
      [LIST]: {
        entity_id: LIST,
        attributes: { friendly_name: XSS, node_id: NODE, items_done: 0, items_total: 1 },
      },
    },
    {
      "todo.get_items": {
        response: { [LIST]: { items: [{ uid: "a", summary: XSS, status: "needs_action" }] } },
      },
      "progresscove.get_nested_items": {
        response: { nested_items: { a: [{ uid: "b", summary: XSS, done: false }] } },
      },
    }
  );
  return instance._refresh().then(() => {
    instance._render();
    const html = instance.innerHTML;
    assert.ok(!/<img\b/i.test(html), "an unescaped tag reached the DOM");
    assert.ok(!/<[^>]+\bonerror=/i.test(html), "an event handler attribute reached the DOM");
    assert.equal((html.match(/&lt;img/g) || []).length, 3, "name, item and subtask all escaped");
  });
});

test("ticking a subtask re-fetches, though the project's own counts are unchanged", async () => {
  // The bug: the refresh stamp watched items_done/items_total only. Completing a SUBTASK leaves
  // both alone, so the card decided nothing had changed and kept drawing the old list until the
  // page was reloaded.
  const state = {
    [LIST]: {
      entity_id: LIST,
      attributes: {
        friendly_name: "Shopping",
        node_id: NODE,
        items_done: 1,
        items_total: 3,
        nested_items_done: 1,
        nested_items_total: 2,
      },
    },
  };
  const instance = new Card();
  instance.setConfig({ entities: [LIST] });
  instance._hass = fakeHass(state, BOTH);

  await instance._refresh();
  const first = instance._hass.calls.length;
  assert.ok(first > 0, "never fetched at all");

  // Nothing changed: the card must not fetch again.
  await instance._refresh();
  assert.equal(instance._hass.calls.length, first, "re-fetched when nothing had changed");

  // A subtask is ticked. Only the nested counts move.
  state[LIST].attributes.nested_items_done = 2;
  await instance._refresh();
  assert.ok(
    instance._hass.calls.length > first,
    "a ticked subtask left the card showing a stale list",
  );
});

test("a clicked row shows its new state before the server answers", async () => {
  // Three hops separate a click from a repaint: the service call, the entity update, and this
  // card's own re-fetch. A checkbox that sits unchanged for all of them reads as a click that
  // missed, which is what made ticking feel like it took a second.
  let release;
  const held = new Promise(r => { release = r; });
  const instance = card({
    ...BOTH,
    "progresscove.complete": () => held,
  });
  await instance._refresh();
  assert.equal(instance._items()[0].tasks[0].done, false, "fixture should start unticked");

  const inFlight = instance._toggle("groceries", false);
  // The service call has NOT returned yet.
  assert.equal(
    instance._items()[0].tasks[0].done, true,
    "the row waited for the round trip instead of showing the click",
  );

  release();
  await inFlight;
  // Still ticked: the service returning only means the command was accepted. Reverting here is
  // what made a row blink, since the card still held the pre-click data for the rest of the trip.
  assert.equal(
    instance._items()[0].tasks[0].done, true,
    "the row reverted when the service returned, before the server state arrived",
  );
});

test("a row that fails to write goes back to what the server holds", async () => {
  const instance = card({
    ...BOTH,
    "progresscove.complete": new Error("nope"),
  });
  await instance._refresh();
  await assert.rejects(() => instance._toggle("groceries", false));
  assert.equal(
    instance._items()[0].tasks[0].done, false,
    "a failed write left a tick that was never real",
  );
});

/** A click must survive the gap between the service returning and the entity updating.
 *
 *  callService resolves when the command is ACCEPTED. The entity, and so this card's data, catches
 *  up a moment later. Dropping the held click at the first of those made the row fall back to the
 *  pre-click data and blink.
 */
function rig() {
  const state = { [LIST]: { entity_id: LIST, attributes: {
    friendly_name: "Shopping", node_id: "n",
    items_done: 0, items_total: 1, nested_items_done: 0, nested_items_total: 2 } } };
  let serverDone = false;
  const responses = {
    "todo.get_items": () => ({ response: { [LIST]: { items: [
      { uid: "groceries", summary: "Groceries", status: "needs_action" } ] } } }),
    "progresscove.get_nested_items": () => ({ response: { nested_items: {
      groceries: [{ uid: "milk", summary: "Milk", done: serverDone }] } } }),
    // The service returns immediately; the ENTITY updates later, as in HA.
    "progresscove.complete": () => { setTimeout(() => {
        serverDone = true;
        state[LIST].attributes.nested_items_done = 1;
      }, 20); },
    "progresscove.reopen": () => { setTimeout(() => {
        serverDone = false;
        state[LIST].attributes.nested_items_done = 0;
      }, 20); },
  };
  const c = new Card();
  c.setConfig({ entities: [LIST] });
  c._hass = fakeHass(state, responses);
  return { card: c, state };
}
const heldSub = (c) => c._items()[0].tasks[0].subtasks[0].done;

test("a ticked subtask stays ticked while the server catches up", async () => {
  const { card, state } = rig();
  await card._refresh();
  assert.equal(heldSub(card), false);

  await card._toggle("milk", false);
  // The service resolved but the entity has NOT updated yet. This is the blink window.
  assert.equal(heldSub(card), true, "row reverted before the server state arrived");

  await new Promise(r => setTimeout(r, 40));
  await card._refresh();
  assert.equal(heldSub(card), true, "row lost the tick after the refresh");
});

test("completing a parent ticks its open children too", async () => {
  // The server closes a completed task's still-open children as CANCELLED, so the whole subtree
  // follows the click. Painting only the clicked row left the children unchanged for the round
  // trip, which made a parent feel slower than a subtask, but only when it HAD open children.
  const instance = card(BOTH);
  await instance._refresh();
  const before = instance._items()[0].tasks[0];
  assert.equal(before.uid, "groceries");
  assert.ok(before.subtasks.some(s => !s.done), "fixture needs an open child");

  await instance._toggle("groceries", false);
  const after = instance._items()[0].tasks[0];
  assert.equal(after.done, true, "the clicked row did not tick");
  assert.deepEqual(
    after.subtasks.map(s => s.done), after.subtasks.map(() => true),
    "the children the server is about to close stayed visibly open",
  );
});
