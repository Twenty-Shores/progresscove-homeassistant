"""My Day as a to-do list: today's work, tickable. Always present."""

# The server decides what is in it. Deriving today from the tree here would give this surface a
# different today than the app shows.

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STATUS_COMPLETED
from .coordinator import ProgressCoveCoordinator
from .pending import complete
from .names import display_name
from .helpers import _parse_due, surfaced, repeats

_LOGGER = logging.getLogger(__name__)

# In the order the server returns them. `TodoItem` has no section field, so the order is the only
# thing carrying it, and the full structure travels on the attributes for our card.
SECTION_DUE = "due"
SECTION_ONGOING = "ongoing"
SECTION_ALARM = "alarm"


class ProgressCoveMyDayEntity(CoordinatorEntity[ProgressCoveCoordinator], TodoListEntity):
    """Today, as the server computed it.

    Only UPDATE: "add to today" is a product decision the app has not made, and delete reads as
    "remove from today" while actually deleting the task.
    """

    _attr_has_entity_name = True
    _attr_name = "My Day"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, coordinator: ProgressCoveCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}-my-day"

    @property
    def todo_items(self) -> list[TodoItem]:
        items: list[TodoItem] = []
        for entry in self.coordinator.data.my_day:
            node_id = entry.get("id")
            if not node_id:
                continue
            items.append(
                TodoItem(
                    uid=node_id,
                    summary=display_name(entry.get("name")),
                    status=self._status_of(node_id),
                    due=_parse_due(entry.get("timeUtc")),
                )
            )
        return items

    def _status_of(self, node_id: str) -> TodoItemStatus:
        """Ticked while a completion waits out its undo window, so the box does not spring back
        for ten seconds."""
        if self.coordinator.pending.is_pending(node_id):
            return TodoItemStatus.COMPLETED
        node = self.coordinator.data.by_id.get(node_id, {})
        return (
            TodoItemStatus.COMPLETED
            if node.get("status") == STATUS_COMPLETED
            else TodoItemStatus.NEEDS_ACTION
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What `TodoItem` cannot carry: section, depth, and the project each item belongs to."""
        return {
            "items": [
                {
                    "id": entry.get("id"),
                    "name": display_name(entry.get("name")),
                    "section": entry.get("section"),
                    "time_utc": entry.get("timeUtc"),
                    "depth": entry.get("depth"),
                    "project_id": entry.get("containingT2Id"),
                    "emoji": self.coordinator.data.by_id.get(entry.get("id"), {}).get("icon"),
                    "path": self.coordinator.data.path_of(entry.get("id", "")),
                    "done": self._status_of(entry.get("id", "")) == TodoItemStatus.COMPLETED,
                }
                for entry in self.coordinator.data.my_day
                if entry.get("id")
            ],
            "sections": [SECTION_DUE, SECTION_ONGOING, SECTION_ALARM],
        }

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Tick an item off, or untick one that is not a repeat.

        A rename is ignored rather than refused: Home Assistant echoes the summary back on every
        tick, so treating it as an edit would rename the task each time.
        """
        uid = item.uid
        if not uid:
            return
        if item.status != TodoItemStatus.COMPLETED:
            if self._status_of(uid) != TodoItemStatus.COMPLETED:
                return
            if self.coordinator.pending.undo(uid):
                self.async_write_ha_state()
                return
            node = self.coordinator.data.by_id.get(uid, {})
            if repeats(node):
                raise HomeAssistantError(
                    f"{display_name(node.get('name')) or 'That task'} repeats, so completing it "
                    "moved it to its next occurrence. Reopening it here would leave it on the "
                    "wrong day."
                )
            with surfaced("reopen that task"):
                await self.coordinator.client.async_uncomplete(uid)
            await self.coordinator.async_refresh()
            return
        if self._status_of(uid) == TodoItemStatus.COMPLETED:
            return

        complete(self.coordinator, uid, self.async_write_ha_state)
