/** With a read-only token the integration says so on the entity, and the cards draw rows nobody can
 *  tap. A row that LOOKS tappable and does nothing is the failure here: it reads as a broken card.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadCard, fakeHass } from "./harness.mjs";

const projects = await loadCard("progresscove-card.js");
const myDay = await loadCard("progresscove-myday-card.js");
const icons = await loadCard("progresscove-icon-card.js");

const LIST = "todo.home_shopping";
const MY_DAY = "todo.my_day";

function listState(readOnly) {
  return {
    [LIST]: {
      entity_id: LIST,
      attributes: {
        friendly_name: "Home › Shopping",
        node_id: "node-shopping",
        items_done: 1,
        items_total: 2,
        read_only: readOnly,
      },
    },
  };
}

const TASKS = {
  "todo.get_items": {
    response: {
      [LIST]: { items: [{ uid: "milk", summary: "Milk", status: "needs_action" }] },
    },
  },
  "progresscove.get_nested_items": {
    response: { nested_items: { milk: [{ uid: "oat", summary: "Oat", done: false }] } },
  },
};

async function projectsHtml(readOnly) {
  const instance = new (projects.defined.get("progresscove-card"))();
  instance.setConfig({ entities: [LIST] });
  instance._hass = fakeHass(listState(readOnly), TASKS);
  await instance._refresh();
  return instance.innerHTML;
}

test("the projects card draws no buttons when read-only", async () => {
  const html = await projectsHtml(true);
  assert.ok(!html.includes("<button"), "a tap target was drawn");
  assert.ok(!html.includes("aria-pressed"), "a row still claims to be a toggle");
});

test("the projects card still shows the tasks and subtasks when read-only", async () => {
  const html = await projectsHtml(true);
  assert.ok(html.includes("Milk"));
  assert.ok(html.includes("Oat"));
});

test("the projects card draws buttons when it can write", async () => {
  const html = await projectsHtml(false);
  assert.ok(html.includes('<button class="pc-task"'));
  assert.ok(html.includes('<button class="pc-sub"'));
});

test("an entity that says nothing about it is treated as writable", async () => {
  const states = listState(undefined);
  delete states[LIST].attributes.read_only;
  const instance = new (projects.defined.get("progresscove-card"))();
  instance.setConfig({ entities: [LIST] });
  instance._hass = fakeHass(states, TASKS);
  await instance._refresh();
  assert.ok(instance.innerHTML.includes("<button"));
});

function myDayHtml(readOnly) {
  const instance = new (myDay.defined.get("progresscove-myday-card"))();
  instance.setConfig({});
  instance.hass = fakeHass({
    [MY_DAY]: {
      entity_id: MY_DAY,
      attributes: {
        read_only: readOnly,
        items: [{ id: "bins", name: "Bins", section: "due", done: false, path: "Home › Bins" }],
      },
    },
  });
  return instance.innerHTML;
}

test("the My Day card draws no buttons when read-only", () => {
  const html = myDayHtml(true);
  assert.ok(!html.includes("<button"), "a tap target was drawn");
  assert.ok(html.includes("Bins"));
});

test("the My Day card draws buttons when it can write", () => {
  assert.ok(myDayHtml(false).includes('<button class="pc-row"'));
});

function iconHtml(config, states) {
  const instance = new (icons.defined.get("progresscove-icon-card"))();
  instance.setConfig(config);
  instance.hass = fakeHass(states);
  return instance.innerHTML;
}

test("the icon card says why there are no tiles when the followed project is read-only", () => {
  const html = iconHtml({ groups: [LIST] }, listState(true));
  assert.ok(html.includes("read-only"));
  assert.ok(!html.includes("Add a task button"));
});

test("the icon card still asks for a button when the project can write", () => {
  const html = iconHtml({ groups: [LIST] }, listState(false));
  assert.ok(html.includes("Add a task button"));
});
