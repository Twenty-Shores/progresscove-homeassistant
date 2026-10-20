/** Load a card file in Node.
 *
 *  The cards are plain ES modules apart from the `customElements.define` and `window.customCards`
 *  at the bottom, which is what this stands in for. Nothing here fakes card BEHAVIOUR: the tests
 *  drive the real classes, because a stub that answers on the class's behalf is how a test comes
 *  to report a pass it never observed.
 */
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

let loads = 0;

const CARDS = join(
  dirname(fileURLToPath(import.meta.url)),
  "..", "..", "custom_components", "progresscove", "frontend"
);

export async function loadCard(filename, registry) {
  // Passing a registry back in re-evaluates a module against names it already registered, which is
  // what Home Assistant does when it reloads its frontend resources.
  const defined = registry ?? new Map();
  // define() THROWS on a name already taken, as the real CustomElementRegistry does. A Map.set here
  // would overwrite silently, and a card that registers unconditionally would pass a test it fails
  // in a browser.
  globalThis.customElements = {
    define: (name, cls) => {
      if (defined.has(name)) {
        throw new DOMException(
          `Failed to execute 'define' on 'CustomElementRegistry': the name "${name}" has already been used`,
          "NotSupportedError",
        );
      }
      defined.set(name, cls);
    },
    get: (name) => defined.get(name),
  };
  globalThis.window = globalThis;
  globalThis.HTMLElement = class {
    constructor() {
      this.innerHTML = "";
      this.children = [];
    }
    appendChild(child) {
      this.children.push(child);
      return child;
    }
    dispatchEvent() {
      return true;
    }
    addEventListener() {}
    setAttribute(name, value) {
      this.attributes = { ...this.attributes, [name]: String(value) };
    }
    focus() {}
    querySelectorAll() {
      return [];
    }
  };
  globalThis.document = {
    createElement: () => new globalThis.HTMLElement(),
  };
  globalThis.CustomEvent = class {
    constructor(type, init) {
      this.type = type;
      Object.assign(this, init);
    }
  };

  const source = await readFile(join(CARDS, filename), "utf8");
  // A trailing comment makes each load a distinct specifier: Node caches modules by URL, so
  // re-importing the same source would return the cached namespace without re-running it, and a
  // test of what happens on a second evaluation would never evaluate anything twice.
  loads += 1;
  const unique = `${source}\n//${loads}`;
  // Evaluated as a module so top-level `class`/`function` stay scoped to it.
  const module = await import(
    `data:text/javascript;base64,${Buffer.from(unique).toString("base64")}`
  );
  return { defined, module };
}

/** A `hass` with just enough of the real shape: states keyed by entity id, and a callService that
 *  records what it was asked for and answers from a script. */
export function fakeHass(states = {}, responses = {}, { entities = {}, devices = {} } = {}) {
  const calls = [];
  return {
    states,
    entities,
    devices,
    calls,
    async callService(domain, service, data, target) {
      calls.push({ domain, service, data, target });
      const key = `${domain}.${service}`;
      const answer = responses[key];
      if (answer instanceof Error) throw answer;
      return typeof answer === "function" ? answer(data, target) : answer;
    },
  };
}

/** States as Home Assistant publishes them for entities under the integration's service device.
 *
 *  Every friendly_name carries the device's name in front, and the entity and device registries
 *  say which device that is. `nameByUser` stands for a device the person renamed, whose new name
 *  is then the one in front.
 */
export function underDevice(states, { name = "ProgressCove", nameByUser = null } = {}) {
  const shown = nameByUser || name;
  const prefixed = {};
  const entities = {};
  for (const [id, state] of Object.entries(states)) {
    const own = state.attributes?.friendly_name;
    prefixed[id] = own === undefined
      ? state
      : { ...state, attributes: { ...state.attributes, friendly_name: `${shown} ${own}` } };
    entities[id] = { entity_id: id, device_id: "device-1" };
  }
  const devices = { "device-1": { id: "device-1", name, name_by_user: nameByUser } };
  return { states: prefixed, registry: { entities, devices } };
}

/** Home Assistant's websocket connection, as far as `subscribeMessage` goes: it keeps each
 *  subscription so a test can push to it, and answers with the unsubscribe function the real one
 *  resolves to, or with `refusal` when the server would refuse the subscription. */
export function fakeConnection({ refusal = null } = {}) {
  const subscriptions = [];
  return {
    subscriptions,
    subscribeMessage(callback, message) {
      const subscription = { message, callback, active: true };
      subscriptions.push(subscription);
      if (refusal) return Promise.reject(refusal);
      return Promise.resolve(() => {
        subscription.active = false;
      });
    },
    /** What Home Assistant sends a live subscription: the entity's items, all of them. */
    async push(items) {
      const live = subscriptions.filter((subscription) => subscription.active);
      await Promise.all(live.map((subscription) => subscription.callback({ items })));
    },
    active() {
      return subscriptions.filter((subscription) => subscription.active);
    },
  };
}
