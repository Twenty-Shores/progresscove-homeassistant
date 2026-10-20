"""My Day as a to-do list: today's work, tickable. Always present."""

# The server decides what is in it. Deriving today from the tree here would give this surface a
# different today than the app shows.

from __future__ import annotations

from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import ServiceCall, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_READ_ONLY, STATUS_COMPLETED
from .coordinator import ProgressCoveCoordinator
from .device import account_device
from .helpers import parse_due, repeats, surfaced, translated_error
from .names import display_name
from .pending import complete


class ProgressCoveMyDayEntity(CoordinatorEntity[ProgressCoveCoordinator], TodoListEntity):
    """Today, as the server computed it.

    Only UPDATE: "add to today" is a product decision the app has not made, and delete reads as
    "remove from today" while actually deleting the task.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "my_day"

    def __init__(self, coordinator: ProgressCoveCoordinator, account_id: str) -> None:
        super().__init__(coordinator)
        self._attr_supported_features = (
            TodoListEntityFeature(0)
            if coordinator.read_only
            else TodoListEntityFeature.UPDATE_TODO_ITEM
        )
        self._attr_unique_id = f"{account_id}-my-day"
        self._attr_device_info = account_device(account_id)

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
                    due=parse_due(entry.get("timeUtc"), dt_util.get_default_time_zone()),
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
        """Whether the card may draw tap targets. The items are not here: see `details`."""
        return {ATTR_READ_ONLY: self.coordinator.read_only}

    def details(self) -> ServiceResponse:
        """What a `TodoItem` has no field for, per item, in the server's order.

        The section an item sits in, the path to it, its emoji and the time it is for. Served on
        request rather than as attributes: an attribute is written into the state machine on every
        change and sent to every browser connected to Home Assistant, whether it shows My Day or
        not, and the recorder stores none of an entity's attributes once they pass 16 KB.
        """
        tree = self.coordinator.data
        return {
            "items": [
                {
                    "uid": entry["id"],
                    "section": entry.get("section"),
                    "time_utc": entry.get("timeUtc"),
                    "path": tree.path_of(entry["id"]),
                    "emoji": tree.by_id.get(entry["id"], {}).get("icon"),
                }
                for entry in tree.my_day
                if entry.get("id")
            ]
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
                raise translated_error(
                    "repeat_cannot_reopen",
                    ServiceValidationError,
                    name=display_name(node.get("name")) or uid,
                )
            with surfaced("reopen_failed"):
                await self.coordinator.client.async_uncomplete(uid)
            # Not async_request_refresh: in its cooldown a second write waits 10 s to show.
            await self.coordinator.async_refresh()
            return
        if self._status_of(uid) == TodoItemStatus.COMPLETED:
            return

        complete(self.coordinator, uid, self.async_write_ha_state)


def my_day_details(entity: Entity, call: ServiceCall) -> ServiceResponse:
    """The `get_my_day_details` action, for whichever to-do entities it was aimed at.

    Registered for every to-do entity of the integration, since Home Assistant offers no narrower
    target, so a list that is not My Day is refused by name.
    """
    if not isinstance(entity, ProgressCoveMyDayEntity):
        raise translated_error("not_my_day", ServiceValidationError, entity_id=entity.entity_id)
    return entity.details()
