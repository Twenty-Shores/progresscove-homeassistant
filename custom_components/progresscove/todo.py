"""One to-do list per node the user picked.

The list's direct children are its items. Items are served through `todo.get_items`; the entity's
attributes carry counts only.
"""

# A to-do item has no children and the frontend cannot nest them, but flattening a task in beside
# its own parent would be a lie about the data. They are served by `progresscove.get_nested_items`.

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CLOSED_STATUSES,
    ATTR_ITEMS_DONE,
    ATTR_NODE_ID,
    ATTR_ITEMS_COMPLETE,
    ATTR_ITEMS_PCT,
    ATTR_ITEMS_TOTAL,
    ATTR_NESTED_DONE,
    ATTR_NESTED_TOTAL,
    CONF_PROJECTS,
    STATUS_COMPLETED,
)
from .coordinator import ProgressCoveCoordinator
from .my_day import ProgressCoveMyDayEntity
from .pending import needs_window
from .names import display_name
from .helpers import _parse_due, surfaced

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One entity per node the user treats as a list, which is their call rather than a tier
    rule: the level people keep checkable things at differs per branch."""
    coordinator: ProgressCoveCoordinator = entry.runtime_data
    selected = entry.data.get(CONF_PROJECTS)
    candidates = coordinator.data.candidate_lists()
    if selected:
        # Honoured exactly, including a node with no children yet: that is a list about to be
        # filled, not a mistake to correct.
        chosen = {n["id"] for n in candidates} & set(selected)
    else:
        # A fresh entry starts with the nodes that already hold something to check off: taking
        # all of them would mint an entity per shopping item on first connect.
        chosen = {n["id"] for n in candidates if coordinator.data.children(n["id"])}
    entities: list[TodoListEntity] = [
        ProgressCoveTodoListEntity(coordinator, entry.entry_id, node)
        for node in candidates
        if node["id"] in chosen
    ]
    # Always present, and not a subentry: nobody picked it.
    entities.append(ProgressCoveMyDayEntity(coordinator, entry.entry_id))
    async_add_entities(entities)




class ProgressCoveTodoListEntity(CoordinatorEntity[ProgressCoveCoordinator], TodoListEntity):
    """A ProgressCove project, as a to-do list."""

    _attr_has_entity_name = True

    # MOVE_TODO_ITEM is deliberately absent: ordering is the app's own, so letting Home Assistant
    # reorder would advertise a capability we cannot honour.
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    )

    def __init__(
        self,
        coordinator: ProgressCoveCoordinator,
        entry_id: str,
        project: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._project_id: str = project["id"]
        self._attr_unique_id = f"{entry_id}-{self._project_id}"
        # The full path, so two lists of the same name are tellable apart.
        self._attr_name = coordinator.data.path_of(project["id"]) or project["name"]
        self._apply()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply()
        super()._handle_coordinator_update()

    def _apply(self) -> None:
        """Rebuild the item list and the extra attributes from the current tree."""
        tree = self.coordinator.data
        tasks = tree.children(self._project_id)

        items: list[TodoItem] = []
        subtask_map: dict[str, list[dict[str, Any]]] = {}
        done_count = 0

        for task in tasks:
            subs = tree.children(task["id"])
            # A pending completion reads as done, or the box springs back and the user taps
            # again, which is what rolled a repeat twice.
            complete = (
                self.coordinator.pending.is_pending(task["id"])
                or task.get("status") == STATUS_COMPLETED
            )
            done_count += complete

            items.append(
                TodoItem(
                    uid=task["id"],
                    summary=display_name(task["name"]),
                    status=TodoItemStatus.COMPLETED if complete else TodoItemStatus.NEEDS_ACTION,
                    due=_parse_due(task.get("due_at")),
                )
            )
            if subs:
                subtask_map[task["id"]] = [
                    {
                        "uid": s["id"],
                        "summary": display_name(s["name"]),
                        # Pending counts as done here for the same reason it does on the task
                        # above: the row is what the user just clicked, and waiting for the server
                        # to confirm leaves it visibly unchanged for a round trip.
                        "done": (
                            self.coordinator.pending.is_pending(s["id"])
                            or s.get("status") in CLOSED_STATUSES
                        ),
                    }
                    for s in subs
                ]

        self._attr_todo_items = items
        self._subtasks = subtask_map
        self._done = done_count
        self._total = len(tasks)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Counts only, for automations and for the card's header."""
        return {
            ATTR_NODE_ID: self._project_id,
            ATTR_ITEMS_DONE: self._done,
            ATTR_ITEMS_TOTAL: self._total,
            # An empty project is not a finished one.
            ATTR_ITEMS_COMPLETE: self._total > 0 and self._done == self._total,
            ATTR_ITEMS_PCT: round(self._done / self._total * 100) if self._total else 0,
            # The items themselves are NOT here: an attribute payload is rewritten in full on
            # every change and truncated past 16 KB, which broke a ~200-task list outright.
            ATTR_NESTED_TOTAL: sum(len(v) for v in self._subtasks.values()),
            ATTR_NESTED_DONE: sum(
                1 for v in self._subtasks.values() for s in v if s["done"]
            ),
        }

    async def async_create_todo_item(self, item: TodoItem) -> None:
        due = item.due.isoformat() if item.due else None
        # One tier below whatever this list is, since a list can sit at any tier.
        parent = self.coordinator.data.by_id.get(self._project_id, {})
        depth = int(parent.get("depth", 2)) + 1
        try:
            with surfaced("add that task"):
                await self.coordinator.client.async_create_task(
                    self._project_id, item.summary or "", due_at=due, depth=depth
                )
        finally:
            await self.coordinator.async_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        uid = item.uid
        if not uid:
            return
        current = self.coordinator.data.by_id.get(uid, {})
        # A repeat is never left COMPLETED: completing one rolls it to its next occurrence, so
        # `status == COMPLETED` is permanently False and every tick read as a fresh completion,
        # pushing the date out one interval per tap. A pending completion is what "already done"
        # means here.
        was_done = (
            self.coordinator.pending.is_pending(uid)
            or current.get("status") == STATUS_COMPLETED
        )
        now_done = item.status == TodoItemStatus.COMPLETED

        new_name = _renamed_to(item.summary, current.get("name"))

        # DATA LOSS TRAP: Home Assistant's update service sends a FULL item, so a rename or a tick
        # still arrives carrying the existing `due`. The API reads `due_at: null` as CLEAR and an
        # absent key as leave alone, so sending it unconditionally wipes the date on any task that
        # has none. Only send it when it actually differs.
        was_due = _parse_due(current.get("due_at"))
        due_changed = item.due != was_due

        # One update can be two calls and the first may land before the second fails, so the
        # refresh in `finally` keeps the list honest about what actually persisted.
        try:
            # Completion is its own verb: the server runs recurrence and the progress rollup off
            # it, which a status PATCH would skip.
            if now_done != was_done:
                if now_done:
                    # Held for the undo window, as on the switch and My Day.
                    async def send() -> None:
                        with surfaced("complete that task"):
                            await self.coordinator.client.async_complete(uid)
                        await self.coordinator.async_refresh()

                    # _apply() first, or the OLD unticked list is pushed to the frontend.
                    def rerender() -> None:
                        self._apply()
                        self.async_write_ha_state()

                    self.coordinator.pending.schedule(
                        uid, send, rerender, hold=needs_window(current),
                    )
                elif not self.coordinator.pending.undo(uid):
                    with surfaced("update that task"):
                        await self.coordinator.client.async_uncomplete(uid)
                else:
                    self._apply()

            if new_name is not None or due_changed:
                # due_at only when it was part of the edit: leaving it out keeps the existing
                # date, passing None clears it.
                changes: dict[str, Any] = {}
                if new_name is not None:
                    changes["name"] = new_name
                if due_changed:
                    changes["due_at"] = item.due.isoformat() if item.due else None
                with surfaced("update that task"):
                    await self.coordinator.client.async_update_task(uid, **changes)
        finally:
            await self.coordinator.async_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        # A later delete can fail after earlier ones are gone, so the refresh in `finally` shows
        # exactly what survived.
        try:
            with surfaced("delete that task"):
                for uid in uids:
                    await self.coordinator.client.async_delete(uid)
        finally:
            await self.coordinator.async_refresh()




def _renamed_to(summary: str | None, stored_name: str | None) -> str | None:
    """The new name, or None when nothing was renamed.

    HA's update service echoes back the summary it was given even when the caller only meant to
    change the status, so an unconditional rename would be a write on every checkbox tick.
    """
    if summary is None or summary == stored_name:
        return None
    return summary
