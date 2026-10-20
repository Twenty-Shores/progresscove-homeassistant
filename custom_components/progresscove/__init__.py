"""Set up and tear down a ProgressCove config entry."""

from __future__ import annotations

from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import ProgressCoveClient
from .const import CONF_DEVICE_ID, CONF_NODE_ID, DOMAIN
from .coordinator import ProgressCoveConfigEntry, ProgressCoveCoordinator
from .frontend import async_register_cards, async_register_static_path, async_remove_cards
from .inbox_issue import clear_issue as clear_inbox_issue
from .names import display_name
from .prune import clear_stale_issues
from .read_only import clear_issue as clear_read_only_issue
from .reload import SetupInputs, async_schedule_reload_if_changed, async_update_listener
from .services import async_register as async_register_services
from .subscription_issue import clear_issue as clear_subscription_issue

PLATFORMS = [Platform.TODO, Platform.BUTTON, Platform.SWITCH, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    await async_register_static_path(hass)
    await async_register_cards(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ProgressCoveConfigEntry) -> bool:
    client = ProgressCoveClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_TOKEN],
        entry.data.get(CONF_DEVICE_ID),
    )
    coordinator = ProgressCoveCoordinator(hass, entry, client)

    # Before any entity exists, so a dead token fails setup rather than filling the UI with
    # unavailable lists.
    await coordinator.async_config_entry_first_refresh()
    # After the first refresh, so a broken token has already failed with a message about the token
    # rather than about an inbox.
    await coordinator.async_resolve_inbox()

    entry.runtime_data = coordinator
    # A no-op unless the last account was removed earlier in this run and took the cards with it:
    # async_setup does not run again for the account added after it.
    await async_register_cards(hass)
    _retitle_subentries(hass, entry, coordinator)
    coordinator.setup_inputs = SetupInputs.of(entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    entry.async_on_unload(
        hass.async_add_shutdown_job(HassJob(coordinator.pending.async_flush_before_shutdown))
    )
    # A change made while the platforms were being set up reached no listener: the last setup's
    # was removed when it unloaded, and this one's did not exist yet.
    async_schedule_reload_if_changed(hass, entry)
    return True


def _retitle_subentries(
    hass: HomeAssistant, entry: ProgressCoveConfigEntry, coordinator: ProgressCoveCoordinator
) -> None:
    """Keep every subentry row titled with its node's current name.

    A title used to be a snapshot of the picker's label at the moment of adding, so rows added
    before and after a formatting change sat side by side in different formats, and a task renamed
    in the app kept its old name here forever. The tree is the source of truth for a name, so the
    row is re-derived from it on every setup rather than trusted from storage.
    """
    for subentry in list(entry.subentries.values()):
        node_id = subentry.data.get(CONF_NODE_ID)
        if not node_id:
            continue
        node = coordinator.data.by_id.get(node_id)
        if not node:
            # The node is gone from the account. Leave the title alone: it is the only clue left
            # about what this row was, and the entity already reports itself unavailable.
            continue
        # Cleaned like the title it was created with, or every row would be retitled once.
        name = display_name(node.get("name"))
        if name and name != subentry.title:
            hass.config_entries.async_update_subentry(entry, subentry, title=name)


async def async_unload_entry(hass: HomeAssistant, entry: ProgressCoveConfigEntry) -> bool:
    await entry.runtime_data.pending.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ProgressCoveConfigEntry) -> None:
    """Take the entry's repairs with it, and the cards with the last entry, or they would outlive
    the thing they serve.

    Home Assistant has already dropped this entry from its list by the time this runs, so an empty
    list means it was the last one.
    """
    clear_read_only_issue(hass, entry)
    clear_inbox_issue(hass, entry)
    clear_stale_issues(hass, entry)
    clear_subscription_issue(hass, entry)
    if not hass.config_entries.async_entries(DOMAIN):
        await async_remove_cards(hass)
