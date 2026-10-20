/**
 * A text field that adds a task, for a wall panel where the list itself does not need to show.
 *
 * Adding goes through `todo.add_item` on one of the integration's list entities, so the text is
 * read the way the apps read a quick-add row: "water plants every monday" becomes a repeating task.
 *
 * Built once and updated in place, unlike the other cards, which redraw on every state change:
 * a redraw would wipe whatever is half typed and drop the keyboard.
 *
 * Design values are copied by hand from the app's tokens, as in the other cards.
 */

const RADIUS_CARD = 14;
const RADIUS_FIELD = 10;
const CARD_PADDING = 16;

// tokens.css: the app's accent
const ACCENT = "#009999";

const DEFAULT_ENTITY = "todo.progresscove_inbox";

const STYLE = `
  .pc-card{
    background:var(--ha-card-background,var(--card-background-color,#fff));
    border-radius:${RADIUS_CARD}px;
    box-shadow:var(--ha-card-box-shadow,0 2px 8px rgba(23,52,59,.10));
    padding:${CARD_PADDING}px;
    font-family:var(--primary-font-family,'DM Sans',system-ui,sans-serif);
    color:var(--primary-text-color,#17343b);
  }
  .pc-title{font-weight:600;font-size:1.05rem;letter-spacing:-.01em;margin-bottom:10px}
  .pc-form{display:flex;gap:8px;align-items:stretch}
  .pc-input{flex:1;min-width:0;font:inherit;font-size:.95rem;color:inherit;
            padding:9px 12px;border-radius:${RADIUS_FIELD}px;background:transparent;
            border:1.5px solid var(--divider-color,rgba(23,52,59,.28))}
  .pc-input::placeholder{color:var(--secondary-text-color,#4c666d)}
  .pc-input:focus-visible{outline:2px solid ${ACCENT};outline-offset:1px}
  .pc-input:disabled{opacity:.6}
  .pc-add{font:inherit;font-weight:600;font-size:.95rem;padding:0 16px;cursor:pointer;
          border:0;border-radius:${RADIUS_FIELD}px;background:${ACCENT};color:#fff}
  .pc-add:focus-visible{outline:2px solid ${ACCENT};outline-offset:2px}
  .pc-add:disabled{opacity:.45;cursor:default}
  .pc-status{font-size:.82rem;color:var(--secondary-text-color,#4c666d);margin-top:8px}
  .pc-status:empty{display:none}
  .pc-status.problem{color:var(--primary-text-color,#17343b)}
`;

/** Editor: the list to add to, and a heading. */
class ProgressCoveAddCardEditor extends HTMLElement {
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
      ({ title: "Heading (optional)", entity: "Add to list" }[field.name] ?? field.name);
    form.addEventListener("value-changed", (event) => {
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: { type: "custom:progresscove-add-card", ...event.detail.value } },
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
if (!customElements.get("progresscove-add-card-editor")) customElements.define("progresscove-add-card-editor", ProgressCoveAddCardEditor);

class ProgressCoveAddCard extends HTMLElement {
  static getStubConfig() {
    return { type: "custom:progresscove-add-card", entity: DEFAULT_ENTITY };
  }

  static getConfigElement() {
    return document.createElement("progresscove-add-card-editor");
  }

  setConfig(config) {
    this._config = { entity: DEFAULT_ENTITY, ...config };
    this._message = null;
    this._build();
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 2;
  }

  getGridOptions() {
    return { rows: "auto", min_columns: 3, min_rows: 1 };
  }

  _state() {
    return this._hass?.states?.[this._config.entity];
  }

  get _readOnly() {
    return this._state()?.attributes?.read_only === true;
  }

  /** The list's own name, not its path: "Groceries", where the entity is "Home › Groceries". */
  get _listName() {
    const name = ownName(this._hass, this._config.entity);
    return name.split(" › ").pop() || "the list";
  }

  _build() {
    if (this._input) return;
    this.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = STYLE;

    const card = document.createElement("div");
    card.className = "pc-card";

    this._title = document.createElement("div");
    this._title.className = "pc-title";

    // A real form, so Enter submits from the field and a screen reader announces it as one.
    const form = document.createElement("form");
    form.className = "pc-form";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      this._submit();
    });

    this._input = document.createElement("input");
    this._input.className = "pc-input";
    this._input.type = "text";
    this._input.enterKeyHint = "done";
    this._input.autocomplete = "off";
    this._input.addEventListener("input", () => {
      // The last outcome is about the last task; once a new one is being typed it no longer is.
      if (this._message && !this._busy) {
        this._message = null;
        this._update();
      }
    });

    this._button = document.createElement("button");
    this._button.className = "pc-add";
    this._button.type = "submit";
    this._button.textContent = "Add";

    this._status = document.createElement("div");
    this._status.className = "pc-status";

    form.appendChild(this._input);
    form.appendChild(this._button);
    card.appendChild(this._title);
    card.appendChild(form);
    card.appendChild(this._status);
    this.appendChild(style);
    this.appendChild(card);
  }

  /** Bring the built elements in line with the config, the entity and the last outcome. */
  _update() {
    if (!this._input) return;
    const heading = this._config.title ?? "";
    this._title.textContent = heading;
    this._title.hidden = !heading;

    const state = this._state();
    const blocked = !state || this._readOnly;
    this._input.disabled = blocked || this._busy;
    this._button.disabled = blocked || this._busy;
    this._input.placeholder = `Add a task to ${this._listName}`;
    this._input.setAttribute("aria-label", `New task for ${this._listName}`);
    this._button.setAttribute("aria-label", `Add task to ${this._listName}`);

    const message = this._blockedMessage() ?? this._message;
    this._status.textContent = message?.text ?? "";
    this._status.className = `pc-status${message?.problem ? " problem" : ""}`;
    // An outcome is announced politely; a refusal interrupts, since what was typed did not land.
    this._status.setAttribute("role", message?.problem ? "alert" : "status");
  }

  /** Why nothing can be added right now, before anyone tries. */
  _blockedMessage() {
    if (!this._hass) return null;
    if (!this._state()) {
      return { text: `${this._config.entity} is not available.`, problem: true };
    }
    if (this._readOnly) {
      return { text: "Adding is off while ProgressCove is read-only.", problem: true };
    }
    return null;
  }

  async _submit() {
    const text = this._input.value.trim();
    if (!text || this._busy || this._blockedMessage()) return;
    this._busy = true;
    this._message = null;
    this._update();
    try {
      await this._hass.callService(
        "todo", "add_item", { item: text }, { entity_id: this._config.entity }
      );
      this._input.value = "";
      this._message = { text: `Added to ${this._listName}.`, problem: false };
    } catch (err) {
      // The text stays in the field: it did not land, and retyping it is the user's loss.
      this._message = { text: failureText(err), problem: true };
    } finally {
      this._busy = false;
      this._update();
      this._input.focus();
    }
  }
}

/** An entity's own name: its friendly_name without the device name Home Assistant puts in front.
 *
 *  Every entity of an account sits under one ProgressCove device, so a list's friendly_name reads
 *  "ProgressCove Home › Groceries" while the path is what this card shows and matches on. A name
 *  the person set on the entity itself carries no prefix and comes back as it is.
 */
function ownName(hass, entityId) {
  const friendly = hass?.states?.[entityId]?.attributes?.friendly_name ?? "";
  const device = hass?.devices?.[hass?.entities?.[entityId]?.device_id];
  const deviceName = device?.name_by_user || device?.name;
  return deviceName && friendly.startsWith(`${deviceName} `)
    ? friendly.slice(deviceName.length + 1)
    : friendly;
}

/** What to tell the person when the add was refused.
 *
 *  The integration words its own refusals, so those are shown as they come. Anything without a
 *  message still says it did not save rather than going quiet.
 */
function failureText(err) {
  const reason = typeof err?.message === "string" ? err.message.trim() : "";
  return reason ? `Not added: ${reason}` : "Not added. ProgressCove could not save that task.";
}

if (!customElements.get("progresscove-add-card")) customElements.define("progresscove-add-card", ProgressCoveAddCard);

window.customCards = window.customCards || [];
// Guarded for the same reason define() is: a second evaluation would list the card twice in the
// picker.
if (!window.customCards.some((c) => c.type === "progresscove-add-card"))
  window.customCards.push({
    type: "progresscove-add-card",
    name: "ProgressCove Add Task",
    description: "Type a task and add it to a list, the inbox by default.",
  });
