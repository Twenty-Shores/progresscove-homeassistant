/** Load a card file in Node.
 *
 *  The cards are plain ES modules apart from the `customElements.define` and `window.customCards`
 *  at the bottom, which is what this stands in for. Nothing here fakes card BEHAVIOUR: the tests
 *  drive the real classes, because a stub that answers on the class's behalf is how a test comes
 *  to report a pass it never observed (the querySelectorAll incident, 2026-08-14).
 */
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CARDS = join(
  dirname(fileURLToPath(import.meta.url)),
  "..", "..", "custom_components", "progresscove", "frontend"
);

export async function loadCard(filename) {
  const defined = new Map();
  globalThis.customElements = { define: (name, cls) => defined.set(name, cls) };
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
  // Evaluated as a module so top-level `class`/`function` stay scoped to it.
  const module = await import(
    `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
  );
  return { defined, module };
}

/** A `hass` with just enough of the real shape: states keyed by entity id, and a callService that
 *  records what it was asked for and answers from a script. */
export function fakeHass(states = {}, responses = {}) {
  const calls = [];
  return {
    states,
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
