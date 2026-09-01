/**
 * Today in its three sections, which the stock to-do card flattens into one checklist.
 *
 * Sections, depth, project and emoji come off the entity's attributes, since a `TodoItem` has no
 * room for them. Ticking calls `todo.update_item`, the same service the stock card calls.
 *
 * Design values are copied by hand from the app's tokens, as in the other two cards.
 */

const RADIUS_CARD = 14;
const CARD_PADDING = 16;

// tokens.css: the app's accent
const ACCENT = "#009999";

const DEFAULT_ENTITY = "todo.my_day";

// The server's own order. Names are ours: "due" is a date, but a person reads it as today's work.
const SECTIONS = [
  { key: "due", label: "Today" },
  { key: "ongoing", label: "Ongoing" },
  { key: "alarm", label: "Reminders" },
];

/** Editor: one entity to pick, and a heading. */
class ProgressCoveMyDayCardEditor extends HTMLElement {
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
    return { entity: DEFAULT_ENTITY, ...this._config };
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
      { name: "entity", selector: { entity: { integration: "progresscove", domain: "todo" } } },
    ];
    form.computeLabel = (field) =>
      ({ title: "Heading (optional)", entity: "My Day list" }[field.name] ?? field.name);
    form.addEventListener("value-changed", (event) => {
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: { type: "custom:progresscove-myday-card", ...event.detail.value } },
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
if (!customElements.get("progresscove-myday-card-editor")) customElements.define("progresscove-myday-card-editor", ProgressCoveMyDayCardEditor);

class ProgressCoveMyDayCard extends HTMLElement {
  static getStubConfig() {
    return { type: "custom:progresscove-myday-card", entity: DEFAULT_ENTITY };
  }

  static getConfigElement() {
    return document.createElement("progresscove-myday-card-editor");
  }

  setConfig(config) {
    this._config = { entity: DEFAULT_ENTITY, ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 5;
  }

  _state() {
    return this._hass?.states?.[this._config.entity];
  }

  async _toggle(uid, done) {
    // The integration holds a completion for ten seconds, so tapping again undoes it and no
    // separate affordance is needed.
    await this._hass.callService("todo", "update_item", {
      entity_id: this._config.entity,
      item: uid,
      status: done ? "needs_action" : "completed",
    });
  }

  _render() {
    if (!this._config) return;
    const state = this._state();
    const items = state?.attributes?.items ?? [];

    this.innerHTML = `
      <style>
        .pc-card{
          background:var(--ha-card-background,var(--card-background-color,#fff));
          border-radius:${RADIUS_CARD}px;
          box-shadow:var(--ha-card-box-shadow,0 2px 8px rgba(23,52,59,.10));
          padding:${CARD_PADDING}px;
          font-family:var(--primary-font-family,'DM Sans',system-ui,sans-serif);
          color:var(--primary-text-color,#17343b);
        }
        .pc-title{font-weight:600;font-size:1.05rem;letter-spacing:-.01em;margin-bottom:4px}
        .pc-section{margin-top:14px}
        .pc-section:first-of-type{margin-top:8px}
        .pc-section-label{font-size:.74rem;text-transform:uppercase;letter-spacing:.06em;
                          color:var(--secondary-text-color,#4c666d);margin-bottom:6px}
        .pc-row{display:flex;align-items:flex-start;gap:10px;padding:7px 0;
                background:none;border:0;width:100%;text-align:left;font:inherit;color:inherit;
                cursor:pointer}
        .pc-box{width:17px;height:17px;border-radius:5px;flex:0 0 auto;margin-top:2px;
                border:1.5px solid rgba(23,52,59,.28);background:transparent}
        .pc-box.on{background:${ACCENT};border-color:${ACCENT};position:relative}
        .pc-box.on::after{content:"";position:absolute;left:5px;top:1.5px;width:4px;height:9px;
                          border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
        .pc-label{flex:1;min-width:0}
        .pc-name{font-size:.95rem;line-height:1.35}
        .pc-name.done{color:var(--secondary-text-color,#4c666d);text-decoration:line-through}
        /* Where it lives, so a bare task name is not stranded without context. */
        .pc-path{font-size:.76rem;color:var(--secondary-text-color,#4c666d);margin-top:2px;
                 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .pc-time{font-size:.78rem;color:var(--secondary-text-color,#4c666d);
                 font-variant-numeric:tabular-nums;flex:0 0 auto;margin-top:2px}
        .pc-empty{color:var(--secondary-text-color,#4c666d);font-size:.9rem;padding:8px 0}
        .pc-row:focus-visible{outline:2px solid ${ACCENT};outline-offset:2px;border-radius:6px}
      </style>
      <div class="pc-card">
        <div class="pc-title">${esc(this._config.title ?? "My Day")}</div>
        ${!state
          ? `<div class="pc-empty">${esc(this._config.entity)} is not available.</div>`
          : items.length
            ? SECTIONS.map(s => this._section(s, items)).join("")
            : `<div class="pc-empty">Nothing due today. Enjoy the quiet.</div>`}
      </div>`;

    for (const row of this.querySelectorAll(".pc-row")) {
      row.addEventListener("click", () =>
        this._toggle(row.dataset.uid, row.dataset.done === "true"));
    }
  }

  _section({ key, label }, items) {
    const mine = items.filter(i => i.section === key);
    if (!mine.length) return "";
    return `
      <div class="pc-section">
        <div class="pc-section-label">${esc(label)}</div>
        ${mine.map(i => this._row(i)).join("")}
      </div>`;
  }

  _row(item) {
    const done = item.done === true;
    // A nested task reads oddly alone, so the parent gives it context. Only the parent: a full
    // breadcrumb would wrap on a tablet.
    const parent = (item.path ?? "").split(" › ").slice(-2, -1)[0] ?? "";
    return `
      <button class="pc-row" data-uid="${esc(item.id)}" data-done="${done}"
              aria-pressed="${done}" aria-label="${esc(item.name)}">
        <span class="pc-box ${done ? "on" : ""}"></span>
        <span class="pc-label">
          <div class="pc-name ${done ? "done" : ""}">${esc(item.emoji ? item.emoji + " " : "")}${esc(item.name)}</div>
          ${parent ? `<div class="pc-path">${esc(parent)}</div>` : ""}
        </span>
        ${item.time_utc ? `<span class="pc-time">${esc(timeLabel(item.time_utc))}</span>` : ""}
      </button>`;
  }
}

/** A wall clock time in the viewer's own locale. The card is read across a room, not parsed. */
function timeLabel(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Task names and paths are user data and land in innerHTML; escaping is not optional. */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

if (!customElements.get("progresscove-myday-card")) customElements.define("progresscove-myday-card", ProgressCoveMyDayCard);

window.customCards = window.customCards || [];
// Guarded for the same reason define() is: a second evaluation would list the card twice in the
// picker.
if (!window.customCards.some((c) => c.type === "progresscove-myday-card"))
  window.customCards.push({
    type: "progresscove-myday-card",
    name: "ProgressCove My Day",
    description: "Today's work in sections: today, ongoing, reminders.",
  });
