/** The add card: what it sends, what it says afterwards, and what it refuses before sending.
 *
 *  The failure that matters is a task the person believes was added and was not, so every path
 *  that does not land has to leave the text in the field and say why.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadCard, fakeHass, underDevice } from "./harness.mjs";

const { defined } = await loadCard("progresscove-add-card.js");
const Card = defined.get("progresscove-add-card");
const Editor = defined.get("progresscove-add-card-editor");

const INBOX = "todo.progresscove_inbox";
const GROCERIES = "todo.home_groceries";

function world({ readOnly = false } = {}) {
  return {
    [INBOX]: {
      entity_id: INBOX,
      attributes: { friendly_name: "Inbox", node_id: "inbox-1", read_only: readOnly },
    },
    [GROCERIES]: {
      entity_id: GROCERIES,
      attributes: { friendly_name: "Home › Groceries", node_id: "node-groceries", read_only: readOnly },
    },
  };
}

function card(config = {}, { states = world(), responses = {} } = {}) {
  const instance = new Card();
  instance.setConfig(config);
  instance.hass = fakeHass(states, responses);
  return instance;
}

test("typed text goes to the inbox through todo.add_item by default", async () => {
  const instance = card();
  instance._input.value = "water plants every monday";
  await instance._submit();
  assert.deepEqual(instance._hass.calls, [{
    domain: "todo",
    service: "add_item",
    data: { item: "water plants every monday" },
    target: { entity_id: INBOX },
  }]);
});

test("the chosen list is the one added to", async () => {
  const instance = card({ entity: GROCERIES });
  instance._input.value = "oat milk";
  await instance._submit();
  assert.deepEqual(instance._hass.calls[0].target, { entity_id: GROCERIES });
});

test("a successful add clears the field and says where it went", async () => {
  const instance = card({ entity: GROCERIES });
  instance._input.value = "oat milk";
  await instance._submit();
  assert.equal(instance._input.value, "");
  assert.equal(instance._status.textContent, "Added to Groceries.");
  assert.equal(instance._status.attributes.role, "status");
});

test("surrounding whitespace is trimmed and blank text sends nothing", async () => {
  const instance = card();
  instance._input.value = "   ";
  await instance._submit();
  assert.equal(instance._hass.calls.length, 0);
  instance._input.value = "  bins  ";
  await instance._submit();
  assert.equal(instance._hass.calls[0].data.item, "bins");
});

test("a refused add keeps the text and shows the integration's own reason", async () => {
  const refusal = Object.assign(new Error("ProgressCove could not add that task."), { code: "home_assistant_error" });
  const instance = card({}, { responses: { "todo.add_item": refusal } });
  instance._input.value = "call the plumber";
  await instance._submit();
  assert.equal(instance._input.value, "call the plumber", "the typed text was thrown away");
  assert.equal(instance._status.textContent, "Not added: ProgressCove could not add that task.");
  assert.equal(instance._status.attributes.role, "alert");
});

test("a failure with no message still says it did not save", async () => {
  const instance = card({}, { responses: { "todo.add_item": () => { throw {}; } } });
  instance._input.value = "call the plumber";
  await instance._submit();
  assert.equal(instance._status.textContent, "Not added. ProgressCove could not save that task.");
});

test("the controls are back after a failure, so the add can be retried", async () => {
  const instance = card({}, { responses: { "todo.add_item": new Error("offline") } });
  instance._input.value = "call the plumber";
  await instance._submit();
  assert.equal(instance._input.disabled, false);
  assert.equal(instance._button.disabled, false);
});

test("a read-only list says adding is not allowed and sends nothing", async () => {
  const instance = card({}, { states: world({ readOnly: true }) });
  assert.equal(instance._input.disabled, true);
  assert.equal(instance._button.disabled, true);
  assert.equal(instance._status.textContent, "Adding is off while ProgressCove is read-only.");
  instance._input.value = "anything";
  await instance._submit();
  assert.equal(instance._hass.calls.length, 0);
});

test("a list that does not exist is named rather than accepting text into nowhere", async () => {
  const instance = card({ entity: "todo.gone" });
  assert.equal(instance._input.disabled, true);
  assert.equal(instance._status.textContent, "todo.gone is not available.");
  instance._input.value = "anything";
  await instance._submit();
  assert.equal(instance._hass.calls.length, 0);
});

test("a second submit while one is in flight sends nothing", async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const instance = card({}, { responses: { "todo.add_item": () => pending } });
  instance._input.value = "bins";
  const first = instance._submit();
  await instance._submit();
  release();
  await first;
  assert.equal(instance._hass.calls.length, 1);
});

test("a state update does not rebuild the field being typed in", () => {
  const instance = card();
  const field = instance._input;
  field.value = "half typ";
  instance.hass = fakeHass(world());
  assert.equal(instance._input, field);
  assert.equal(instance._input.value, "half typ");
});

test("the field and button are labelled with the list they add to", () => {
  const instance = card({ entity: GROCERIES });
  assert.equal(instance._input.attributes["aria-label"], "New task for Groceries");
  assert.equal(instance._button.attributes["aria-label"], "Add task to Groceries");
});

test("a heading shows only when one is set", () => {
  assert.equal(card()._title.hidden, true);
  const titled = card({ title: "Quick add" });
  assert.equal(titled._title.hidden, false);
  assert.equal(titled._title.textContent, "Quick add");
});

test("the editor defaults to the inbox and offers only ProgressCove lists", () => {
  const editor = new Editor();
  editor.setConfig({});
  editor.hass = fakeHass(world());
  assert.equal(editor._form.data.entity, INBOX);
  const picker = editor._form.schema.find(field => field.name === "entity");
  assert.deepEqual(picker.selector.entity, { integration: "progresscove", domain: "todo" });
});

test("the list is named without the device name Home Assistant puts in front", () => {
  const { states, registry } = underDevice(world());
  for (const [entity, expected] of [[INBOX, "Inbox"], [GROCERIES, "Groceries"]]) {
    const instance = new Card();
    instance.setConfig({ entity });
    instance.hass = fakeHass(states, {}, registry);
    assert.equal(instance._input.placeholder, `Add a task to ${expected}`);
  }
});
