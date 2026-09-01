"""One button per task the user wants on the wall. Press to complete the current occurrence."""

# Deliberately stays AVAILABLE on days the task is not due. `unavailable` is Home Assistant's word
# for an entity that is broken or gone: cards hide it, automations treat it as a fault, and the log
# counts it as a problem. A garbage-day button waiting for Friday is none of those things. Pressing
# early is refused by the press itself, which is where a "not yet" belongs, and unavailable is kept
# for the one case it really means: the task is gone from the account.

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STATUS_COMPLETED
from .coordinator import ProgressCoveCoordinator
from .pending import complete, refuse_if_too_early
from .names import display_name
from .helpers import due_date, timezone_of, is_due

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One button per subentry the user added. Never automatic: a button per task would be hundreds.

    Entities are attached to their SUBENTRY, not just the entry, so removing "Add button → Bulk
    pickup" in the UI removes exactly that entity and leaves the others alone.
    """
    coordinator: ProgressCoveCoordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != "button":
            continue
        node_id = subentry.data.get("node_id")
        if node_id not in coordinator.data.by_id:
            continue
        async_add_entities(
            [ProgressCoveTaskButton(coordinator, entry.entry_id, node_id)],
            config_subentry_id=subentry_id,
        )


class ProgressCoveTaskButton(CoordinatorEntity[ProgressCoveCoordinator], ButtonEntity):
    """Press to complete today's occurrence."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ProgressCoveCoordinator,
        entry_id: str,
        node_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self._attr_unique_id = f"{entry_id}-button-{node_id}"
        node = coordinator.data.by_id.get(node_id, {})
        self._attr_name = display_name(node.get("name")) or "Task"
        # The task's own emoji, so a row of these reads as icons rather than words. HA takes an mdi
        # name here, not an emoji, so the emoji travels as an attribute for the card to render.
        self._attr_icon = "mdi:checkbox-marked-circle-outline"

    @property
    def _node(self) -> dict[str, Any]:
        return self.coordinator.data.by_id.get(self._node_id, {})

    @property
    def available(self) -> bool:
        """Only false when the task itself is gone, deleted in the app, so nothing to press."""
        return bool(self._node)

    @property
    def _actionable(self) -> bool:
        """Whether pressing this would do anything, which is what a card tile lights up for.

        Mirrors the switch, so a tile drawn from either entity dims on the same rule. A pending
        completion is not actionable: the user already pressed it, and the server has not rolled
        the occurrence yet, so asking `is_due` alone would keep the tile lit and make the surface
        look frozen for the whole undo window.
        """
        if self.coordinator.pending.is_pending(self._node_id):
            return False
        return is_due(self._node, self.hass)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Enough for a card to say WHEN without pressing anything."""
        zone = timezone_of(self.hass)
        node = self._node
        due = due_date(node, zone)
        today = datetime.now(zone).date()
        return {
            "node_id": self._node_id,
            "emoji": node.get("icon"),
            "due_date": due.isoformat() if due else None,
            "due_today": bool(due and due == today),
            # What a card switches its icon on: lit when there is something to do, dim otherwise.
            "actionable": self._actionable,
            "days_until": (due - today).days if due else None,
            # A button press is an event and the entity never changes state, so to TRIGGER on a
            # completion use the task's switch instead.
            "completed": node.get("status") == STATUS_COMPLETED,
            "path": self.coordinator.data.path_of(self._node_id),
        }

    async def async_press(self) -> None:
        """Complete this occurrence. The server rolls the repeat to its next day.

        Refused only when the task is due LATER: completing an occurrence that has not arrived
        would move the schedule forward and quietly skip the real one. A task with no due date is
        pressed whenever the user likes. Said out loud rather than ignored, a press that appears to
        do nothing is the worse failure.

        A repeat goes through the undo window like everywhere else, so pressing the button and
        ticking the same task's switch behave the same.
        """
        refuse_if_too_early(self.coordinator, self._node, self._attr_name, self.hass)
        complete(self.coordinator, self._node_id, self.async_write_ha_state)

    @callback
    def _handle_coordinator_update(self) -> None:
        # Follow a rename made in the app: the name was read once at construction, so without this
        # the wall button kept whatever it was called on the day it was added.
        node = self._node
        if node:
            self._attr_name = display_name(node.get("name")) or self._attr_name
        super()._handle_coordinator_update()
