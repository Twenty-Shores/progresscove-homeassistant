/** Home Assistant can evaluate a card module twice in one page lifetime.
 *
 *  It re-registers its frontend resources without tearing the document down, so a module that
 *  calls customElements.define() unconditionally throws NotSupportedError the second time. The
 *  throw aborts the rest of the module, the element never finishes registering, and the dashboard
 *  reports the card as an unknown type until a full reload. It presented as a configuration error
 *  on every other refresh.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadCard } from "./harness.mjs";

const CARDS = {
  "progresscove-card.js": "progresscove-card",
  "progresscove-icon-card.js": "progresscove-icon-card",
  "progresscove-myday-card.js": "progresscove-myday-card",
};

for (const [file, element] of Object.entries(CARDS)) {
  test(`${element} survives being loaded twice`, async () => {
    const first = await loadCard(file);
    assert.ok(first.defined.get(element), "did not register on the first load");

    // The same module evaluated again against a registry that already holds its name.
    const second = await loadCard(file, first.defined);
    assert.ok(second.defined.get(element), "lost its registration on the second load");
  });
}
