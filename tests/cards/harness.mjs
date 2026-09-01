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
