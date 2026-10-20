/** The My Day card draws today in sections without the list ever being an attribute.
 *
 *  Rows come from Home Assistant's to-do item subscription and what a to-do item cannot carry
 *  (section, path, emoji, time) from `progresscove.get_my_day_details`. The card has to look the
 *  same as when both rode on the entity's attributes, and never pass an outage off as a quiet day.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadCard, fakeConnection, fakeHass } from "./harness.mjs";

const { defined } = await loadCard("progresscove-myday-card.js");
const MY_DAY = "todo.progresscove_my_day";

const ITEMS = [
  { uid: "bins", summary: "Bins out", status: "needs_action" },
  { uid: "call", summary: "Call mum", status: "completed" },
  { uid: "walk", summary: "Walk", status: "needs_action" },
];

const DETAILS = [
  { uid: "bins", section: "due", path: "Home › Chores › Bins out", emoji: "🗑️",
    time_utc: "2026-10-19T18:30:00+00:00" },
  { uid: "call", section: "ongoing", path: "Family › Call mum", emoji: null, time_utc: null },
  { uid: "walk", section: "alarm", path: "Walk", emoji: null, time_utc: null },
];

function myDayState(lastChanged = "t1", state = "2") {
  return { [MY_DAY]: { entity_id: MY_DAY, state, last_changed: lastChanged,
                       attributes: { read_only: false } } };
}

function details(items = DETAILS) {
  return { response: { [MY_DAY]: { items } } };
}

function setUp({ answers = { "progresscove.get_my_day_details": details() }, refusal } = {}) {
  const card = new (defined.get("progresscove-myday-card"))();
  card.isConnected = true;
  card.setConfig({});
  const connection = fakeConnection({ refusal });
  const hass = fakeHass(myDayState(), answers);
  hass.connection = connection;
  card.hass = hass;
  return { card, connection, hass };
}

function quietly(run) {
  const original = console.error;
  const logged = [];
  console.error = (...args) => logged.push(args);
  return Promise.resolve(run()).finally(() => {
    console.error = original;
  }).then(() => logged);
}

test("it subscribes to the entity's items the way the stock to-do card does", () => {
  const { connection } = setUp();
  assert.deepEqual(connection.subscriptions.map((s) => s.message),
                   [{ type: "todo/item/subscribe", entity_id: MY_DAY }]);
});

test("rows are drawn in their sections, with emoji, parent and time", async () => {
  const { card, connection, hass } = setUp();
  await connection.push(ITEMS);
  const html = card.innerHTML;
  assert.deepEqual(hass.calls.map((call) => [`${call.domain}.${call.service}`, call.target]),
                   [["progresscove.get_my_day_details", { entity_id: MY_DAY }]]);
  const order = ["Today", "Bins out", "Ongoing", "Call mum", "Reminders", "Walk"]
    .map((text) => html.indexOf(text));
  assert.ok(order.every((at) => at >= 0), "a section or row is missing");
  assert.deepEqual([...order].sort((a, b) => a - b), order, "sections out of order");
  assert.ok(html.includes("🗑️ Bins out"));
  assert.ok(html.includes('<div class="pc-path">Chores</div>'), "the parent is not shown");
  assert.ok(html.includes('<span class="pc-time">'), "the time is not shown");
  assert.ok(html.includes('pc-name done">Call mum'), "a completed row is not drawn done");
});

test("an empty day says so", async () => {
  const { card, connection } = setUp({
    answers: { "progresscove.get_my_day_details": details([]) },
  });
  await connection.push([]);
  assert.ok(card.innerHTML.includes("Nothing due today"));
});

test("nothing is claimed before the first rows arrive", () => {
  const { card } = setUp();
  assert.ok(!card.innerHTML.includes("Nothing due today"));
});

test("details that cannot be read say so instead of showing an empty day", async () => {
  const { card, connection } = setUp({
    answers: { "progresscove.get_my_day_details": new Error("not loaded") },
  });
  const logged = await quietly(() => connection.push(ITEMS));
  assert.ok(card.innerHTML.includes("My Day could not be loaded."));
  assert.ok(!card.innerHTML.includes("Nothing due today"));
  assert.equal(logged.length, 1);
});

test("a refused subscription says so, and is not retried on every state change", async () => {
  const { card, connection, hass } = setUp({ refusal: new Error("invalid_entity_id") });
  const logged = await quietly(() => new Promise((resolve) => setTimeout(resolve, 0)));
  assert.ok(card.innerHTML.includes("My Day could not be loaded."));
  assert.equal(logged.length, 1);
  card.hass = { ...hass, states: { ...myDayState(), "light.kitchen": { state: "on" } } };
  assert.equal(connection.subscriptions.length, 1);
});

test("an entity set up again is subscribed to afresh", async () => {
  const { card, connection, hass } = setUp();
  await connection.push(ITEMS);
  // A reload passes the state through unavailable, so it comes back with a new last_changed.
  card.hass = { ...hass, states: myDayState("t2") };
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(connection.subscriptions.length, 2);
  assert.equal(connection.active().length, 1, "the old subscription was left open");
});

test("an unavailable entity is said to be, and its subscription let go", async () => {
  const { card, connection, hass } = setUp();
  await connection.push(ITEMS);
  card.hass = { ...hass, states: myDayState("t2", "unavailable") };
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(card.innerHTML.includes("is not available"));
  assert.equal(connection.active().length, 0);
});

test("removing the card from the page ends the subscription", async () => {
  const { card, connection } = setUp();
  card.disconnectedCallback();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(connection.active().length, 0);
});

test("an older answer arriving after a newer one is not drawn", async () => {
  let release;
  const first = new Promise((resolve) => { release = resolve; });
  let calls = 0;
  const { card, connection } = setUp({
    answers: {
      "progresscove.get_my_day_details": () => (++calls === 1 ? first : details()),
    },
  });
  const stale = connection.push([{ uid: "old", summary: "Old", status: "needs_action" }]);
  await connection.push(ITEMS);
  release(details([{ uid: "old", section: "due", path: "Old" }]));
  await stale;
  assert.ok(card.innerHTML.includes("Bins out"));
  assert.ok(!card.innerHTML.includes(">Old<"));
});
