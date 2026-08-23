/**
 * Projects with their tasks and progress, where the stock to-do card renders a flat checklist.
 *
 * Design values are copied by hand from the app's tokens: a Lovelace card is a standalone module
 * with no bundler, so there is nothing to import them from.
 */

const RADIUS_CARD = 14;
const RADIUS_PILL = 50;
const CARD_PADDING = 16;

// Shared design tokens: progressGradientFill, progressGradientDoneEnd
const PROGRESS_FROM = "#5e7ce2";
const PROGRESS_TO = "#92dce5";
const PROGRESS_DONE_END = "#F5C842";

// tokens.css: the app's accent
const ACCENT = "#009999";

const DEMO = [
  {
    name: "Home Improvement",
    done: 3,
    total: 5,
    tasks: [
      {
        name: "Aquarium build", due: "Aug 12", done: false,
        subtasks: [
          { name: "Order the tank", done: true },
          { name: "Cycle the filter", done: false },
        ],
      },
      { name: "Fix the porch light", done: false, subtasks: [] },
      { name: "Replace the mailbox", done: true, subtasks: [] },
    ],
  },
  {
    name: "Groceries",
    done: 1,
    total: 4,
    tasks: [
      {
        name: "Weekly shop", done: false,
        subtasks: [
          { name: "Oat milk", done: true },
          { name: "Coffee", done: false },
          { name: "Bread", done: false },
        ],
      },
    ],
  },
];

/** Editor: which project lists to draw. */
class ProgressCoveCardEditor extends HTMLElement {
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
    return { ...this._config };
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
        name: "entities",
        selector: { entity: { multiple: true, integration: "progresscove", domain: "todo" } },
      },
    ];
    form.computeLabel = (field) =>
      ({ title: "Heading (optional)", entities: "Projects" }[field.name] ?? field.name);
    form.addEventListener("value-changed", (event) => {
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: { type: "custom:progresscove-card", ...event.detail.value } },
          bubbles: true,
          composed: true,
        })
      );
    });
    this.appendChild(form);
  }
}

customElements.define("progresscove-card-editor", ProgressCoveCardEditor);

class ProgressCoveCard extends HTMLElement {
  static getStubConfig() {
    return { type: "custom:progresscove-card" };
  }

  static getConfigElement() {
    return document.createElement("progresscove-card-editor");
  }

  setConfig(config) {
    // No throw: an unconfigured card says what to do rather than showing HA's red error box.
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._refresh();
    this._render();
  }

  getCardSize() {
    return 6;
  }

  /** Pull each project's items and their children through the services that carry them.
   *
   *  Re-fetched when a list's state or counts change, not on every hass update: HA sets `hass` on
   *  every state change in the house, which would put two calls per project behind a light switch.
   */
  async _refresh() {
    const ids = this._config?.entities ?? [];
    if (!this._hass || this._config?.demo || !ids.length) return;

    const stamp = ids
      .map(id => {
        const st = this._hass.states[id];
        return st ? `${id}:${st.state}:${st.attributes?.items_done}/${st.attributes?.items_total}` : id;
      })
      .join("|");
    if (stamp === this._stamp) return;
    this._stamp = stamp;

    const fetched = await Promise.all(ids.map(id => this._fetchOne(id)));
    this._fetched = Object.fromEntries(fetched.filter(Boolean));
    this._render();
  }

  /** One project's items and subtask map, or null if either call fails.
   *
   *  Null rather than a partial: an empty task list for a project that HAS tasks reads as "you
   *  finished everything".
   */
  async _fetchOne(entityId) {
    // HA would drop a todo service aimed at another domain anyway, but asking puts a confusing
    // failure in the log for a config the user can edit freely.
    if (!entityId.startsWith("todo.")) return null;
    const nodeId = this._hass.states[entityId]?.attributes?.node_id;
    if (!nodeId) return null;
    try {
      const [items, nested] = await Promise.all([
        this._hass.callService("todo", "get_items", {}, { entity_id: entityId }, true, true),
        this._hass.callService(
          "progresscove", "get_nested_items", { node_id: nodeId }, undefined, true, true
        ),
      ]);
      return [entityId, {
        items: items?.response?.[entityId]?.items ?? [],
        subtasks: nested?.response?.nested_items ?? {},
      }];
    } catch (err) {
      // Never silently: showing nothing is indistinguishable from a finished project.
      console.error(`progresscove-card: could not read ${entityId}`, err);
      return null;
    }
  }

  /** Projects to draw, from the list entities the integration publishes. */
  _items() {
    if (this._config?.demo) return DEMO;
    const ids = this._config?.entities ?? [];
    return ids
      .map(id => this._hass?.states?.[id])
      .filter(Boolean)
      .map(state => {
        const attrs = state.attributes ?? {};
        // Counts from attributes, tasks from the services, so the header is right on the first
        // paint and stays right if a fetch fails.
        const loaded = this._fetched?.[state.entity_id];
        const nested = loaded?.subtasks ?? {};
        return {
          name: attrs.friendly_name ?? "Project",
          done: attrs.items_done ?? 0,
          total: attrs.items_total ?? 0,
          tasks: (loaded?.items ?? []).map(item => ({
            name: item.summary ?? item.name,
            due: item.due ?? null,
            done: item.status === "completed" || item.done === true,
            uid: item.uid,
            subtasks: (nested[item.uid] ?? []).map(sub => ({
              uid: sub.uid,
              name: sub.summary ?? sub.name,
              done: sub.done === true || sub.status === "completed",
            })),
          })),
        };
      });
  }

  get _unconfigured() {
    return !(this._config?.demo || this._config?.entities?.length);
  }

  _render() {
    if (!this._config) return;
    const projects = this._items();

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
        .pc-project + .pc-project{margin-top:22px}
        .pc-head{display:flex;align-items:baseline;gap:10px}
        .pc-name{font-weight:600;font-size:1.05rem;letter-spacing:-.01em}
        /* The count leads with what is DONE, never what is left. */
        .pc-count{margin-left:auto;font-size:.82rem;color:var(--secondary-text-color,#4c666d);
                  font-variant-numeric:tabular-nums}
        /* The bar only mounts when there is progress to show: an empty track at 0 done is a
           reminder of nothing, and the count already carries that state. */
        .pc-bar{height:6px;border-radius:${RADIUS_PILL}px;background:rgba(23,52,59,.08);
                margin:8px 0 12px;overflow:hidden}
        .pc-fill{height:100%;border-radius:${RADIUS_PILL}px;
                 background:linear-gradient(90deg,${PROGRESS_FROM},${PROGRESS_TO})}
        .pc-fill.done{background:linear-gradient(90deg,${PROGRESS_TO},${PROGRESS_DONE_END})}
        .pc-task{display:flex;align-items:flex-start;gap:10px;padding:7px 0}
        .pc-box{width:17px;height:17px;border-radius:5px;flex:0 0 auto;margin-top:2px;
                border:1.5px solid rgba(23,52,59,.28);background:transparent}
        .pc-box.on{background:${ACCENT};border-color:${ACCENT};position:relative}
        .pc-box.on::after{content:"";position:absolute;left:5px;top:1.5px;width:4px;height:9px;
                          border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
        .pc-label{flex:1;min-width:0}
        .pc-title{font-size:.95rem;line-height:1.35}
        .pc-title.done{color:var(--secondary-text-color,#4c666d);text-decoration:line-through}
        .pc-meta{font-size:.78rem;color:var(--secondary-text-color,#4c666d);margin-top:2px}
        /* Subtasks: the thing the stock card cannot show, and the reason this card exists. The
           rail is what makes the nesting readable at a glance on a wall tablet. */
        .pc-subs{margin:2px 0 4px 27px;padding-left:12px;
                 border-left:2px solid rgba(0,153,153,.22)}
        .pc-task,.pc-sub{background:none;border:0;width:100%;text-align:left;font:inherit;
                         color:inherit;cursor:pointer}
        .pc-task:focus-visible,.pc-sub:focus-visible{outline:2px solid ${ACCENT};outline-offset:2px}
        .pc-sub{display:flex;align-items:center;gap:9px;padding:4px 0}
        .pc-sub .pc-box{width:14px;height:14px;border-radius:4px;margin-top:0}
        .pc-sub .pc-box.on::after{left:4px;top:1px;width:3px;height:7px;border-width:0 2px 2px 0}
        .pc-sub-title{font-size:.87rem;color:var(--secondary-text-color,#4c666d)}
        .pc-sub-title.done{text-decoration:line-through;opacity:.72}
        .pc-empty{color:var(--secondary-text-color,#4c666d);font-size:.9rem;padding:6px 0}
      </style>
      <div class="pc-card">
        ${projects.length ? projects.map(p => this._project(p)).join("") :
          `<div class="pc-empty">${this._unconfigured
            ? "Pick the projects you want to see."
            : "Nothing scheduled. Enjoy the quiet."}</div>`}
      </div>`;

    // innerHTML replaces the DOM on every render, so listeners cannot be bound once in the
    // constructor.
    for (const row of this.querySelectorAll(".pc-task, .pc-sub")) {
      row.addEventListener("click", () =>
        this._toggle(row.dataset.node, row.dataset.done === "true"));
    }
  }

  /** Complete or reopen ANY node, whatever its depth.
   *
   *  Not todo.update_item, which resolves against the entity's own items and cannot reach a
   *  nested one.
   */
  async _toggle(nodeId, done) {
    if (!nodeId) return;
    await this._hass.callService("progresscove", done ? "reopen" : "complete", {
      node_id: nodeId,
    });
  }

  _project(p) {
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    const complete = p.total > 0 && p.done === p.total;
    return `
      <div class="pc-project">
        <div class="pc-head">
          <span class="pc-name">${esc(p.name)}</span>
          <span class="pc-count">${p.done} of ${p.total} done</span>
        </div>
        ${pct > 0 ? `<div class="pc-bar"><div class="pc-fill ${complete ? "done" : ""}"
             style="width:${pct}%"></div></div>` : `<div style="height:12px"></div>`}
        ${p.tasks.map(t => this._task(t)).join("")}
      </div>`;
  }

  _task(t) {
    const subDone = t.subtasks.filter(s => s.done).length;
    const meta = [
      dueLabel(t.due),
      t.subtasks.length ? `${subDone} of ${t.subtasks.length}` : null,
    ].filter(Boolean).join(" · ");
    return `
      <button class="pc-task" data-node="${esc(t.uid ?? "")}" data-done="${t.done}"
              aria-pressed="${t.done}">
        <span class="pc-box ${t.done ? "on" : ""}"></span>
        <span class="pc-label">
          <div class="pc-title ${t.done ? "done" : ""}">${esc(t.name)}</div>
          ${meta ? `<div class="pc-meta">${esc(meta)}</div>` : ""}
        </span>
      </button>
      ${t.subtasks.length ? `<div class="pc-subs">${t.subtasks.map(s => `
        <button class="pc-sub" data-node="${esc(s.uid ?? "")}" data-done="${s.done}"
                aria-pressed="${s.done}">
          <span class="pc-box ${s.done ? "on" : ""}"></span>
          <span class="pc-sub-title ${s.done ? "done" : ""}">${esc(s.name)}</span>
        </button>`).join("")}</div>` : ""}`;
  }
}

/** Milliseconds sentinel marking a date-only due, meaning no time was set. A human never sets
 *  milliseconds, so .999 unambiguously means the time was not chosen. */
const NO_TIME_MS = 999;

/** A due date as a person would say it.
 *
 *  The raw value is a UTC instant and used to be printed verbatim, so a card showed
 *  `2026-09-04T06:59:59.999000+00:00`. Worse, that value is a date-only due stored at local
 *  23:59:59.999, so the "time" in it is the sentinel, and displaying it would tell the user their
 *  task is due at seven in the morning.
 */
function dueLabel(iso) {
  if (!iso) return null;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  const dateOnly = when.getMilliseconds() === NO_TIME_MS;
  const day = when.toLocaleDateString([], { month: "short", day: "numeric" });
  return dateOnly
    ? day
    : `${day}, ${when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

/** Task names are user data and land in innerHTML; escaping is not optional. */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

customElements.define("progresscove-card", ProgressCoveCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "progresscove-card",
  name: "ProgressCove",
  description: "Projects, tasks and subtasks with progress, the calm view.",
});
