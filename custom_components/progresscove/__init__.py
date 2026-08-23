"""Set up and tear down a ProgressCove config entry."""

from __future__ import annotations


from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ProgressCoveClient
from .const import CONF_BASE_URL, CONF_DEVICE_ID, CONF_TOKEN, MIN_HA_VERSION
from .coordinator import ProgressCoveCoordinator
from .frontend import async_register as async_register_frontend
from .services import async_register as async_register_services
from .helpers import scan_minutes

PLATFORMS = [Platform.TODO, Platform.BUTTON, Platform.SWITCH, Platform.SENSOR]


def _too_old() -> str | None:
    """The running version if it is below MIN_HA_VERSION, else None."""
    if (MAJOR_VERSION, MINOR_VERSION) >= MIN_HA_VERSION:
        return None
    return f"{MAJOR_VERSION}.{MINOR_VERSION}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if (running := _too_old()) is not None:
        raise ConfigEntryError(
            f"ProgressCove needs Home Assistant {MIN_HA_VERSION[0]}.{MIN_HA_VERSION[1]} or newer. "
            f"You are on {running}."
        )

    client = ProgressCoveClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_TOKEN],
        entry.data.get(CONF_DEVICE_ID),
    )
    coordinator = ProgressCoveCoordinator(
        hass,
        entry,
        client,
        scan_minutes(entry.options),
    )

    # Before any entity exists, so a dead token fails setup rather than filling the UI with
    # unavailable lists.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    async_register_services(hass)
    # Version-stamped: the frontend caches cards hard, so an upgrade that changed one would
    # otherwise stay invisible until the browser cache cleared.
    await async_register_frontend(hass)
    # Before the update listener is added: retitling notifies listeners, and a listener here would
    # reload the entry, which retitles again.
    _retitle_subentries(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


def _retitle_subentries(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Keep every subentry row titled with its node's current name.

    A title used to be a snapshot of the picker's label at the moment of adding, so rows added
    before and after a formatting change sat side by side in different formats, and a task renamed
    in the app kept its old name here forever. The tree is the source of truth for a name, so the
    row is re-derived from it on every setup rather than trusted from storage.
    """
    for subentry in list(entry.subentries.values()):
        node_id = subentry.data.get("node_id")
        if not node_id:
            continue
        node = coordinator.data.by_id.get(node_id)
        if not node:
            # The node is gone from the account. Leave the title alone: it is the only clue left
            # about what this row was, and the entity already reports itself unavailable.
            continue
        name = node.get("name")
        if name and name != subentry.title:
            hass.config_entries.async_update_subentry(entry, subentry, title=name)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry.runtime_data.pending.cancel_all()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed. Reload to take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
