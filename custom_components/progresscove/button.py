"""One button per task the user wants on the wall. Press to complete the current occurrence."""

# Deliberately stays AVAILABLE on days the task is not due. `unavailable` is Home Assistant's word
# for an entity that is broken or gone: cards hide it, automations treat it as a fault, and the log
# counts it as a problem. A garbage-day button waiting for Friday is none of those things. Pressing
# early is refused by the press itself, which is where a "not yet" belongs, and unavailable is kept
# for what it really means: the task is gone from the account, or the server cannot be reached.

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_NODE_ID, CONF_NODE_ID, STATUS_COMPLETED, SUBENTRY_BUTTON
from .coordinator import ProgressCoveConfigEntry, ProgressCoveCoordinator
from .device import account_device
from .helpers import due_date, is_due
from .names import display_name
from .pending import complete, refuse_if_too_early


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProgressCoveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One button per subentry the user added. Never automatic: a button per task would be hundreds.

    Entities are attached to their SUBENTRY, not just the entry, so removing "Add button → Bulk
    pickup" in the UI removes exactly that entity and leaves the others alone.
    """
    coordinator = entry.runtime_data
    if coordinator.read_only:
        return
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_BUTTON:
            continue
        node_id = subentry.data.get(CONF_NODE_ID)
        if node_id not in coordinator.data.by_id:
            continue
        async_add_entities(
            [ProgressCoveTaskButton(coordinator, coordinator.account_id, node_id)],
            config_subentry_id=subentry_id,
        )


class ProgressCoveTaskButton(CoordinatorEntity[ProgressCoveCoordinator], ButtonEntity):
    """Press to complete today's occurrence."""

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
        self._attr_unique_id = f"{account_id}-button-{node_id}"
        self._attr_device_info = account_device(account_id)
        if name := display_name(coordinator.data.by_id.get(node_id, {}).get("name")):
            self._attr_name = name

    @property
    def _node(self) -> dict[str, Any]:
        return self.coordinator.data.by_id.get(self._node_id, {})

    @property
    def available(self) -> bool:
        """False while the poll is failing, and when the task is gone from the account.

        Never for a task that is merely not due: see the note at the top of this module.
        """
        return super().available and bool(self._node)

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
        return is_due(self._node)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Enough for a card to say WHEN without pressing anything."""
        zone = dt_util.get_default_time_zone()
        node = self._node
        due = due_date(node, zone)
        today = dt_util.now().date()
        return {
            ATTR_NODE_ID: self._node_id,
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
        refuse_if_too_early(self.coordinator, self._node, display_name(self._node.get("name")))
        complete(self.coordinator, self._node_id, self.async_write_ha_state)

    @callback
    def _handle_coordinator_update(self) -> None:
        # Follow a rename made in the app: the name was read once at construction, so without this
        # the wall button kept whatever it was called on the day it was added.
        if name := display_name(self._node.get("name")):
            self._attr_name = name
        super()._handle_coordinator_update()
