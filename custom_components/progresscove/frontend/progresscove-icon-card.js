/**
 * A wall of tappable task icons, each tile one task drawn as its own emoji.
 *
 * A tile that can be pressed is one that is due; every other tile is dimmed and refuses. Dimmed is
 * the resting state, not a broken one, and the entity decides it: this card only draws what
 * `actionable` already says.
 *
 * Design values are copied by hand from the app's tokens, as in the other two cards.
 */

const RADIUS_TILE = 16;
// The narrowest a tile can be and still read: emoji, a name on two lines, and a date.
const MIN_TILE_PX = 96;
// A row needs room for the emoji plus two lines of text beside it, so it cannot go as narrow.
const MIN_ROW_PX = 200;
const CARD_PADDING = 16;

// tokens.css: the app's accent
const ACCENT = "#009999";

/** Every task entity this card can draw a tile from.
 *
 *  Buttons and switches both: they carry the same attributes, so a tile looks the same either way
 *  and adding a task as a switch does not mean adding it twice.
 */
function taskButtons(hass) {
  return Object.keys(hass?.states ?? {})
    .filter(id => (id.startsWith("button.") || id.startsWith("switch.")) &&
                  hass.states[id].attributes?.node_id !== undefined)
    .sort();
}

/** The path a followed list entity stands for, e.g. `Home › Repeating home tasks`. */
function listPath(hass, entityId) {
  const name = hass?.states?.[entityId]?.attributes?.friendly_name ?? "";
  // My Day is a todo entity with no branch beneath it, and the selector cannot exclude it, so
  // following it is ignored rather than silently matching nothing.
  return name.includes(" › ") ? name : "";
}

/** The card's own editor, so configuring it is a picker rather than hand-written YAML. */
class ProgressCoveIconCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  /** The values the form shows: our defaults, overwritten by whatever the card was saved with. */
  get _data() {
    return { columns: 3, layout: "grid", show_name: true, show_when: true, ...this._config };
  }

  _render() {
    if (!this._hass) return;

    // Built once, then kept in sync. Rebuilding would tear down the field being typed in, and
    // never updating leaves a saved card's pickers empty, since setConfig fires before hass.
    if (this._form) {
      this._form.hass = this._hass;
      this._form.data = this._data;
      return;
    }

    this.innerHTML = "";

    const form = document.createElement("ha-form");
    this._form = form;
    form.hass = this._hass;
    form.data = this._data;
    form.schema = [
      { name: "title", selector: { text: {} } },
      {
        name: "groups_help",
        // A disabled field is the only way to get explanatory text onto an ha-form.
        selector: { constant: { label: "Both lists add up. Followed projects also pick up tasks added later; listed tasks are exactly those." } },
      },
      {
        name: "groups",
        selector: {
          entity: { multiple: true, integration: "progresscove", domain: "todo" },
        },
      },
      {
        name: "entities",
        // Both domains, or the picker hides half the tasks someone has already added.
        selector: {
          entity: { multiple: true, integration: "progresscove", domain: ["button", "switch"] },
        },
      },
      {
        name: "layout",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "grid", label: "Grid, icon above the name" },
              { value: "horizontal", label: "Rows, icon beside the name" },
            ],
          },
        },
      },
      { name: "columns", selector: { number: { min: 1, max: 6, mode: "slider" } } },
      { name: "show_name", selector: { boolean: {} } },
      { name: "show_when", selector: { boolean: {} } },
    ];
    form.computeLabel = (field) => ({
      title: "Heading (optional)",
      groups: "Follow projects",
      entities: "Also show these tasks",
      layout: "Layout",
      columns: "Max columns",
      show_name: "Show the task name",
      show_when: "Show when it is next due",
      groups_help: "",
    }[field.name] ?? field.name);

    form.addEventListener("value-changed", (event) => {
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: { type: "custom:progresscove-icon-card", ...event.detail.value } },
          bubbles: true,
          composed: true,
        })
      );
    });
    this.appendChild(form);
  }
}

// Home Assistant can evaluate a card module more than once in a page's lifetime: it re-registers
// its frontend resources without tearing the document down. A second define() throws
// NotSupportedError, which aborts the rest of the module and leaves the card unrenderable until a
// full reload, so registering is skipped when the name is already taken.
if (!customElements.get("progresscove-icon-card-editor")) customElements.define("progresscove-icon-card-editor", ProgressCoveIconCardEditor);

class ProgressCoveIconCard extends HTMLElement {
  /** A new card starts EMPTY: filling it with every button would make the common case, one
   *  project on a wall, a deletion exercise. */
  static getStubConfig() {
    return { type: "custom:progresscove-icon-card" };
  }

  static getConfigElement() {
    return document.createElement("progresscove-icon-card-editor");
  }

  setConfig(config) {
    // An empty `entities` means every task button, resolved at render time so one added later
    // appears without editing the card.
    this._config = { columns: 3, ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    const columns = this._config?.columns ?? 3;
    return Math.ceil(this._tiles().length / columns) + 1;
  }

  /** What the sections view may do to this card.
   *
   *  `rows: "auto"` is the important one: without it the view hands the card a fixed row count and
   *  crops whatever does not fit, which looks like the card misdrawing.
   */
  getGridOptions() {
    return { rows: "auto", min_columns: 1, min_rows: 1 };
  }

  /** Entities in the order the user listed them, skipping any that HA does not know about yet,    *  a card that renders a hole for a not-yet-loaded entity looks broken during startup. */
  /** What to draw, under whichever of the two rules this card was configured with.
   *
   *  `groups` FOLLOWS a project: whatever is under it now, including buttons added later.
   *  `entities` PINS an explicit set: exactly what was chosen, and nothing that appears afterwards.
   *  Both can be set, a project plus a couple of strays from elsewhere, and neither being set
   *  means nothing has been chosen yet, which the card says rather than guessing.
   */
  _tiles() {
    if (!this._hass) return [];
    const pinned = this._config.entities ?? [];
    const followed = this._config.groups ?? [];
    if (!pinned.length && !followed.length) return [];

    const followedPaths = followed
      .map(id => listPath(this._hass, id))
      .filter(Boolean);
    const fromGroups = followedPaths.length
      ? taskButtons(this._hass).filter(id => {
          const path = this._hass.states[id].attributes?.path ?? "";
          // Prefix match on the separator, so "Home › Shopping" never swallows "Home › Shopping 2".
          return followedPaths.some(g => path.startsWith(g + " › "));
        })
      : [];

    // One tile per TASK, not per entity. The same task can exist as both a button and a switch,     // adding a switch for an automation should not silently double its tile on the wall. A pinned
    // entity wins over one pulled in by a followed project, since it was chosen deliberately.
    const byNode = new Map();
    for (const id of [...pinned, ...fromGroups]) {
      const state = this._hass.states[id];
      if (!state) continue;
      const node = state.attributes?.node_id ?? id;
      if (!byNode.has(node)) byNode.set(node, state);
    }
    return [...byNode.values()];
  }

  /** The card's heading: the user's own, or the projects being followed so a wall of icons says
   *  where it came from. Explicit title always wins; "" turns it off. */
  _heading() {
    if (this._config.title !== undefined) return this._config.title;
    const followed = (this._config.groups ?? [])
      .map(id => listPath(this._hass, id))
      .filter(Boolean)
      .map(path => path.split(" › ").pop());
    return followed.join(", ");
  }

  /** Whether the card has been told what to show at all. */
  get _unconfigured() {
    return !(this._config.entities?.length || this._config.groups?.length);
  }

  async _press(entityId) {
    // Let HA surface the "not due yet" error the entity raises; swallowing it would turn a refused
    // press into a press that silently did nothing, which is the worse failure.
    const [domain] = entityId.split(".");
    if (domain === "switch") {
      // Completing is turn_OFF: on means open. The tile reads as "press to finish" either way.
      await this._hass.callService("switch", "turn_off", { entity_id: entityId });
      return;
    }
    await this._hass.callService("button", "press", { entity_id: entityId });
  }

  _render() {
    if (!this._config) return;
    const tiles = this._tiles();
    const columns = this._config.columns ?? 3;

    this.innerHTML = `
      <style>
        .pc-card{
          background:var(--ha-card-background,var(--card-background-color,#fff));
          border-radius:${RADIUS_TILE}px;
          box-shadow:var(--ha-card-box-shadow,0 2px 8px rgba(23,52,59,.10));
          padding:${CARD_PADDING}px;
          font-family:var(--primary-font-family,'DM Sans',system-ui,sans-serif);
          color:var(--primary-text-color,#17343b);
        }
        .pc-title{font-weight:600;font-size:1.05rem;letter-spacing:-.01em;margin-bottom:12px}
        /* auto-fit, not a fixed count: a hardcoded 3 columns squashes tiles into whatever width
           the card is given and truncates the names. The browser fits as many whole tiles as the
           space allows and drops to fewer: which is what "remove what will not fit" means for a
           grid. The columns setting caps it so a wide card does not become one long row. */
        .pc-grid{display:grid;gap:10px;
                 grid-template-columns:repeat(auto-fit,minmax(${MIN_TILE_PX}px,1fr));
                 ${columns ? `max-width:${columns * (MIN_TILE_PX + 10)}px;` : ""}}
        /* Horizontal: emoji left, text right, for a narrow card or a sidebar. A row is wider
           than a tile, so it gets its own minimum; a wide card fits several side by side rather
           than leaving the right half empty, still capped by max columns. */
        .pc-grid.row{grid-template-columns:repeat(auto-fit,minmax(${MIN_ROW_PX}px,1fr));
                     ${columns ? `max-width:${columns * (MIN_ROW_PX + 10)}px;` : ""}}
        .pc-grid.row .pc-tile{flex-direction:row;justify-content:flex-start;gap:12px;
                              text-align:left;padding:10px 12px}
        .pc-grid.row .pc-name{-webkit-line-clamp:1}
        .pc-grid.row .pc-label{flex:1;min-width:0;display:flex;flex-direction:column}
        .pc-tile{
          display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;
          padding:14px 8px;border-radius:${RADIUS_TILE}px;border:1.5px solid rgba(23,52,59,.12);
          background:transparent;font:inherit;color:inherit;text-align:center;
          cursor:pointer;transition:transform .12s ease,border-color .12s ease;
        }
        /* Due today: the only tile that invites a press. */
        .pc-tile.due{border-color:${ACCENT};background:rgba(0,153,153,.08)}
        .pc-tile.due:active{transform:scale(.96)}
        /* Waiting for its day. Dimmed, not disabled-looking-broken, and it still says WHEN. */
        .pc-tile.waiting{opacity:.45;cursor:default}
        .pc-tile:focus-visible{outline:2px solid ${ACCENT};outline-offset:2px}
        .pc-emoji{font-size:1.9rem;line-height:1}
        .pc-name{font-size:.82rem;line-height:1.25;overflow:hidden;display:-webkit-box;
                 -webkit-line-clamp:2;-webkit-box-orient:vertical}
        .pc-when{font-size:.72rem;color:var(--secondary-text-color,#4c666d);
                 font-variant-numeric:tabular-nums}
        .pc-empty{color:var(--secondary-text-color,#4c666d);font-size:.9rem;padding:6px 0}
        @media (prefers-reduced-motion:reduce){.pc-tile{transition:none}}
      </style>
      <div class="pc-card">
        ${this._heading() ? `<div class="pc-title">${esc(this._heading())}</div>` : ""}
        ${tiles.length
          ? `<div class="pc-grid ${this._config.layout === "horizontal" ? "row" : ""}">${
                tiles.map(s => this._tile(s)).join("")}</div>`
          : `<div class="pc-empty">${this._unconfigured
                ? "Pick a project to follow, or the task buttons you want."
                : "Nothing here yet. Add a task button in the integration."}</div>`}
      </div>`;

    for (const button of this.querySelectorAll(".pc-tile.due")) {
      button.addEventListener("click", () => this._press(button.dataset.entity));
    }
  }

  _tile(state) {
    const attrs = state.attributes || {};
    const due = attrs.actionable === true;
    // The emoji is the whole point; a task with none still needs a tile, so fall back to a mark
    // rather than rendering an empty square.
    const emoji = attrs.emoji || "•";
    const name = attrs.friendly_name || "Task";
    return `
      <button class="pc-tile ${due ? "due" : "waiting"}"
              data-entity="${esc(state.entity_id)}"
              ${due ? "" : "disabled"}
              aria-label="${esc(name)}${due ? ", due today" : ", " + esc(whenLabel(attrs))}">
        <span class="pc-emoji">${esc(emoji)}</span>
        <span class="pc-label">
          ${this._config.show_name === false ? "" : `<span class="pc-name">${esc(name)}</span>`}
          ${this._config.show_when === false ? "" : `<span class="pc-when">${esc(due ? "Today" : whenLabel(attrs))}</span>`}
        </span>
      </button>`;
  }
}

/** How far off the next occurrence is, in the words someone would actually use. */
function whenLabel(attrs) {
  const days = attrs.days_until;
  if (days === null || days === undefined) return "No date";
  if (days <= 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days < 7) return `In ${days} days`;
  return attrs.due_date || `In ${days} days`;
}

/** Task names and emoji are user data and land in innerHTML; escaping is not optional. */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

if (!customElements.get("progresscove-icon-card")) customElements.define("progresscove-icon-card", ProgressCoveIconCard);

window.customCards = window.customCards || [];
// Guarded for the same reason define() is: a second evaluation would list the card twice in the
// picker.
if (!window.customCards.some((c) => c.type === "progresscove-icon-card"))
  window.customCards.push({
    type: "progresscove-icon-card",
    name: "ProgressCove Icons",
    description: "Tappable task icons, lit when due, dimmed until their day.",
  });
