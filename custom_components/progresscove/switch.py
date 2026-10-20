"""One switch per task the user wants to act on from Home Assistant.

A switch is on while the task is open and off once it is done. Turning it off completes the task;
turning it on reopens a plain task and is refused on a repeating one.
"""

# Only repeats are one-way: completing one advances its due date server-side and uncompleting
# restores neither the date nor the schedule. A plain task reopens freely, as it does in the app.

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_NODE_ID, CONF_NODE_ID, STATUS_COMPLETED, SUBENTRY_SWITCH
from .coordinator import ProgressCoveConfigEntry, ProgressCoveCoordinator
from .device import account_device
from .helpers import due_date, is_due, repeats, surfaced, translated_error
from .names import display_name
from .pending import complete, refuse_if_too_early


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProgressCoveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One per subentry. Never automatic: a switch per node would be hundreds of entities."""
    coordinator = entry.runtime_data
    if coordinator.read_only:
        return
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_SWITCH:
            continue
        node_id = subentry.data.get(CONF_NODE_ID)
        if node_id not in coordinator.data.by_id:
            continue
        async_add_entities(
            [ProgressCoveNodeSwitch(coordinator, coordinator.account_id, node_id)],
            config_subentry_id=subentry_id,
        )


class ProgressCoveNodeSwitch(CoordinatorEntity[ProgressCoveCoordinator], SwitchEntity):
    """On = open, off = done. Turning it off completes the task after an undo window."""

    _attr_has_entity_name = True
    # The name when the task has none to show; a named task overrides it with its own.
    _attr_translation_key = "task"
    _unrecorded_attributes = frozenset({ATTR_NODE_ID, "emoji", "path"})

    def __init__(
        self,
        coordinator: ProgressCoveCoordinator,
        account_id: str,
        node_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self._attr_unique_id = f"{account_id}-switch-{node_id}"
        self._attr_device_info = account_device(account_id)
        if name := display_name(coordinator.data.by_id.get(node_id, {}).get("name")):
            self._attr_name = name

    @property
    def _node(self) -> dict[str, Any]:
        return self.coordinator.data.by_id.get(self._node_id, {})

    @property
    def available(self) -> bool:
        """False while the poll is failing, and when the node is gone from the account."""
        return super().available and bool(self._node)

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
    def _actionable(self) -> bool:
        """Whether pressing this would do anything, which is what a card tile lights up for.

        A pending completion is not actionable: the user already pressed it. Reading `is_due`
        alone left a tile lit for the whole undo window, because the server has not rolled the
        occurrence yet, so the tile looked unpressed and the surface looked frozen.
        """
        if self.coordinator.pending.is_pending(self._node_id):
            return False
        return is_due(self._node)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = dt_util.get_default_time_zone()
        due = due_date(self._node, zone)
        today = dt_util.now().date()
        return {
            ATTR_NODE_ID: self._node_id,
            "emoji": self._node.get("icon"),
            "path": self.coordinator.data.path_of(self._node_id),
            # The same four the button publishes, so a card tile dims on one rule either way.
            "due_date": due.isoformat() if due else None,
            "due_today": bool(due and due == today),
            "actionable": self._actionable,
            "days_until": (due - today).days if due else None,
            # Redundant with the state, but an automation author searching for "completed" finds
            # nothing and concludes the entity cannot answer.
            "completed": not self.is_on,
            # Tells "just tapped, still undoable" from "settled".
            "undo_pending": self.coordinator.pending.is_pending(self._node_id),
        }

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Mark done now; tell the server when the undo window closes."""
        refuse_if_too_early(self.coordinator, self._node, display_name(self._node.get("name")))
        complete(self.coordinator, self._node_id, self.async_write_ha_state)

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
            raise translated_error(
                "repeat_cannot_reopen", ServiceValidationError, name=display_name(node.get("name"))
            )
        with surfaced("reopen_failed"):
            await self.coordinator.client.async_uncomplete(self._node_id)
        # Not async_request_refresh: in its cooldown a second write waits 10 s to show.
        await self.coordinator.async_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        if name := display_name(self._node.get("name")):
            self._attr_name = name
        super()._handle_coordinator_update()
