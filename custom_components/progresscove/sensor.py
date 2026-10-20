"""One progress sensor per node the user wants to watch.

Reports how far through its children a node is, as a percentage. Added one at a time from the
integration page, never automatically.
"""

# For graphing, not for triggering on completion: `above` is strictly greater-than, so `above: 100`
# never fires. The to-do entity's `items_complete` boolean is the trigger.

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_NODE_ID, CONF_NODE_ID, STATUS_COMPLETED, SUBENTRY_SENSOR
from .coordinator import ProgressCoveConfigEntry, ProgressCoveCoordinator
from .device import account_device
from .names import display_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProgressCoveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_SENSOR:
            continue
        node_id = subentry.data.get(CONF_NODE_ID)
        if node_id not in coordinator.data.by_id:
            continue
        async_add_entities(
            [ProgressCoveProgressSensor(coordinator, coordinator.account_id, node_id)],
            config_subentry_id=subentry_id,
        )


class ProgressCoveProgressSensor(
    CoordinatorEntity[ProgressCoveCoordinator], SensorEntity
):
    """How far through its children a node is, as a percentage."""

    _attr_has_entity_name = True
    # The name when the node has none to show; a named node overrides it with its own.
    _attr_translation_key = "progress"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({ATTR_NODE_ID, "path"})

    def __init__(
        self,
        coordinator: ProgressCoveCoordinator,
        account_id: str,
        node_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self._attr_unique_id = f"{account_id}-progress-{node_id}"
        self._attr_device_info = account_device(account_id)
        if name := display_name(coordinator.data.by_id.get(node_id, {}).get("name")):
            self._attr_name = name

    @property
    def _children(self) -> list[dict[str, Any]]:
        return self.coordinator.data.children(self._node_id)

    @property
    def available(self) -> bool:
        """False while the poll is failing, and when the node is gone from the account."""
        return super().available and bool(self.coordinator.data.by_id.get(self._node_id))

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
            ATTR_NODE_ID: self._node_id,
            "path": self.coordinator.data.path_of(self._node_id),
            "done": done,
            "total": len(children),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        node = self.coordinator.data.by_id.get(self._node_id, {})
        if name := display_name(node.get("name")):
            self._attr_name = name
        super()._handle_coordinator_update()
