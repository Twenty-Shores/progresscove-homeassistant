"""One switch per task the user wants to act on from Home Assistant.

A switch is on while the task is open and off once it is done. Turning it off completes the task;
turning it on reopens a plain task and is refused on a repeating one.
"""

# Only repeats are one-way: completing one advances its due date server-side and uncompleting
# restores neither the date nor the schedule. A plain task reopens freely, as it does in the app.

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STATUS_COMPLETED
from .coordinator import ProgressCoveCoordinator
from .pending import needs_window
from .names import display_name
from .helpers import _due_date, _surfaced, _zone, is_due, repeats

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One per subentry. Never automatic: a switch per node would be hundreds of entities."""
    coordinator: ProgressCoveCoordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != "switch":
            continue
        node_id = subentry.data.get("node_id")
        if node_id not in coordinator.data.by_id:
            continue
        async_add_entities(
            [ProgressCoveNodeSwitch(coordinator, entry.entry_id, node_id)],
            config_subentry_id=subentry_id,
        )


class ProgressCoveNodeSwitch(CoordinatorEntity[ProgressCoveCoordinator], SwitchEntity):
    """On = open, off = done. Turning it off completes the task after an undo window."""

    _attr_has_entity_name = True
    # Draws two buttons rather than a toggle, which would promise a reversibility repeats lack.
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: ProgressCoveCoordinator,
        entry_id: str,
        node_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self._attr_unique_id = f"{entry_id}-switch-{node_id}"
        self._attr_name = display_name(
            coordinator.data.by_id.get(node_id, {}).get("name")
        ) or "Task"

    @property
    def _node(self) -> dict[str, Any]:
        return self.coordinator.data.by_id.get(self._node_id, {})

    @property
    def available(self) -> bool:
        """Only false when the node is gone from the account, deleted in the app."""
        return bool(self._node)

    @property
    def is_on(self) -> bool:
        """On means there is still something to do.

        A pending completion reads as off for the whole window: the user said done, so the surface
        shows that rather than a countdown.
        """
        if self.coordinator.pending.is_pending(self._node_id):
            return False
        return self._node.get("status") != STATUS_COMPLETED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = _zone(self.hass)
        due = _due_date(self._node, zone)
        today = datetime.now(zone).date()
        return {
            "node_id": self._node_id,
            "emoji": self._node.get("icon"),
            "path": self.coordinator.data.path_of(self._node_id),
            # The same four the button publishes, so a card tile dims on one rule either way.
            "due_date": due.isoformat() if due else None,
            "due_today": bool(due and due == today),
            "actionable": is_due(self._node, self.hass),
            "days_until": (due - today).days if due else None,
            # Redundant with the state, but an automation author searching for "completed" finds
            # nothing and concludes the entity cannot answer.
            "completed": not self.is_on,
            # Tells "just tapped, still undoable" from "settled".
            "undo_pending": self.coordinator.pending.is_pending(self._node_id),
        }

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Mark done now; tell the server when the undo window closes."""
        if self._node.get("status") == STATUS_COMPLETED:
            return

        async def send() -> None:
            with _surfaced("complete that task"):
                await self.coordinator.client.async_complete(self._node_id)
            await self.coordinator.async_refresh()

        self.coordinator.pending.schedule(
            self._node_id, send, self.async_write_ha_state,
            hold=needs_window(self._node),
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Undo a pending completion, or reopen a task that is genuinely closed."""
        if self.coordinator.pending.undo(self._node_id):
            self.async_write_ha_state()
            return
        node = self._node
        if node.get("status") != STATUS_COMPLETED:
            # A rolled repeat lands here rather than on the guard below: completing one returns it
            # to open on its next occurrence, so there is nothing to reopen.
            return
        if repeats(node):
            raise HomeAssistantError(
                f"{self._attr_name} repeats, so completing it moved it to its next occurrence. "
                "Reopening it here would leave it on the wrong day. Change it in the "
                "ProgressCove app."
            )
        with _surfaced("reopen that task"):
            await self.coordinator.client.async_uncomplete(self._node_id)
        await self.coordinator.async_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        node = self._node
        if node:
            self._attr_name = display_name(node.get("name")) or self._attr_name
        super()._handle_coordinator_update()
