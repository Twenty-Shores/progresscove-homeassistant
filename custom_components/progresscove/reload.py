"""Reload an entry once for any number of changes, and only when its setup would come out different.

Every change to an entry or its subentries calls the update listener, so "Manage lists" adding one
list and removing another, or a prune removing thirty deleted tasks, called it once per change. A
listener that reloads each time set the whole entry up again for every one of them, and each reload
flushes every completion still waiting out its undo window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

if TYPE_CHECKING:
    # The coordinator module imports the modules that import this one, so the type is only named.
    from .coordinator import ProgressCoveConfigEntry


@dataclass(frozen=True)
class SetupInputs:
    """What a running setup was built from.

    Subentry titles are left out: setup rewrites them from the tree, and no entity reads them.
    """

    data: dict[str, Any]
    options: dict[str, Any]
    subentries: dict[str, tuple[str, dict[str, Any]]]

    @classmethod
    def of(cls, entry: ConfigEntry) -> SetupInputs:
        return cls(
            data=dict(entry.data),
            options=dict(entry.options),
            subentries={
                subentry_id: (subentry.subentry_type, dict(subentry.data))
                for subentry_id, subentry in entry.subentries.items()
            },
        )


@callback
def async_schedule_reload_if_changed(
    hass: HomeAssistant, entry: ProgressCoveConfigEntry
) -> None:
    """Schedule one reload if the entry no longer matches what its setup was built from.

    The changes of one action all land before the reload runs, so the first one schedules it and
    the rest find it already scheduled. The flag lives on the running setup and is gone with it, so
    a change made after the reload has rebuilt the entry is compared afresh.
    """
    coordinator = entry.runtime_data
    if coordinator.reload_scheduled or SetupInputs.of(entry) == coordinator.setup_inputs:
        return
    coordinator.reload_scheduled = True
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_update_listener(hass: HomeAssistant, entry: ProgressCoveConfigEntry) -> None:
    async_schedule_reload_if_changed(hass, entry)
