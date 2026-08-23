"""One progress sensor per node the user wants to watch.

Reports how far through its children a node is, as a percentage. Added one at a time from the
integration page, never automatically.
"""

# For graphing, not for triggering on completion: `above` is strictly greater-than, so `above: 100`
# never fires. The to-do entity's `items_complete` boolean is the trigger.

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STATUS_COMPLETED
from .coordinator import ProgressCoveCoordinator
from .names import display_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: ProgressCoveCoordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != "sensor":
            continue
        node_id = subentry.data.get("node_id")
        if node_id not in coordinator.data.by_id:
            continue
        async_add_entities(
            [ProgressCoveProgressSensor(coordinator, entry.entry_id, node_id)],
            config_subentry_id=subentry_id,
        )


class ProgressCoveProgressSensor(
    CoordinatorEntity[ProgressCoveCoordinator], SensorEntity
):
    """How far through its children a node is, as a percentage."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:progress-check"

    def __init__(
        self,
        coordinator: ProgressCoveCoordinator,
        entry_id: str,
        node_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self._attr_unique_id = f"{entry_id}-progress-{node_id}"
        self._attr_name = display_name(
            coordinator.data.by_id.get(node_id, {}).get("name")
        ) or "Progress"

    @property
    def _children(self) -> list[dict[str, Any]]:
        return self.coordinator.data.children(self._node_id)

    @property
    def available(self) -> bool:
        """False only when the node is gone from the account, deleted in the app."""
        return bool(self.coordinator.data.by_id.get(self._node_id))

    @property
    def native_value(self) -> int | None:
        """Percent complete, or None for a node with nothing in it.

        None rather than 0, so a "below 50" trigger does not fire on a project nobody has started.
        """
        children = self._children
        if not children:
            return None
        done = sum(1 for c in children if c.get("status") == STATUS_COMPLETED)
        return round(done / len(children) * 100)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        children = self._children
        done = sum(1 for c in children if c.get("status") == STATUS_COMPLETED)
        return {
            "node_id": self._node_id,
            "path": self.coordinator.data.path_of(self._node_id),
            "done": done,
            "total": len(children),
            # Ready to drop into a notification body.
            "summary": f"{done} of {len(children)} done" if children else "nothing here yet",
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        node = self.coordinator.data.by_id.get(self._node_id)
        if node:
            self._attr_name = display_name(node.get("name")) or self._attr_name
        super()._handle_coordinator_update()
