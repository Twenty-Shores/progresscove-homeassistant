"""Services taking a node id rather than an entity, so they reach any depth.

`todo.update_item` resolves its item against the entity's own items, which stops at a list's direct
children. `complete` and `reopen` go deeper; `get_nested_items` reads one level below a list.
`get_my_day_details` is the exception, aimed at the My Day entity for what its items cannot carry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.todo import DOMAIN as TODO_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, service
import voluptuous as vol

from .const import ATTR_NODE_ID, DOMAIN, STATUS_COMPLETED
from .helpers import refuse_if_read_only, repeats, surfaced, translated_error
from .my_day import my_day_details
from .names import display_name
from .pending import complete

if TYPE_CHECKING:
    from .coordinator import ProgressCoveConfigEntry, ProgressCoveCoordinator

SERVICE_COMPLETE = "complete"
SERVICE_REOPEN = "reopen"
SERVICE_GET_NESTED_ITEMS = "get_nested_items"
SERVICE_GET_MY_DAY_DETAILS = "get_my_day_details"

_SCHEMA = vol.Schema({vol.Required(ATTR_NODE_ID): cv.string})


def _coordinator_for(hass: HomeAssistant, node_id: str) -> ProgressCoveCoordinator:
    """The loaded entry that owns this node.

    Searched rather than passed: a node id is globally unique, so the caller does not have to know
    which entry it belongs to. Only a loaded entry is asked, because one that failed setup or is
    unloading has no tree to answer from.
    """
    loaded: list[ProgressCoveConfigEntry] = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        raise translated_error("not_loaded", ServiceValidationError)
    for entry in loaded:
        if node_id in entry.runtime_data.data.by_id:
            return entry.runtime_data
    raise translated_error("unknown_node", ServiceValidationError, node_id=node_id)


async def _complete(call: ServiceCall) -> None:
    node_id = call.data[ATTR_NODE_ID]
    coordinator = _coordinator_for(call.hass, node_id)
    # async_update_listeners repaints every entity at once; without it the card waits for the
    # window to close before the tick appears.
    complete(coordinator, node_id, coordinator.async_update_listeners)


async def _reopen(call: ServiceCall) -> None:
    node_id = call.data[ATTR_NODE_ID]
    coordinator = _coordinator_for(call.hass, node_id)
    refuse_if_read_only(coordinator)
    if coordinator.pending.undo(node_id):
        # Cancelling changes nothing on the server, so nothing else would repaint the box.
        coordinator.async_update_listeners()
        return
    node = coordinator.data.by_id[node_id]
    if node.get("status") != STATUS_COMPLETED:
        # A rolled repeat lands here: it is already open on its next occurrence. The guard below
        # is for the rarer one left genuinely closed by an exhausted or malformed rule.
        return
    if repeats(node):
        raise translated_error(
            "repeat_cannot_reopen",
            ServiceValidationError,
            name=display_name(node.get("name")) or node_id,
        )
    with surfaced("reopen_failed"):
        await coordinator.client.async_uncomplete(node_id)
    # Not async_request_refresh: in its cooldown a second write waits 10 s to show.
    await coordinator.async_refresh()


async def _get_nested_items(call: ServiceCall) -> ServiceResponse:
    """The children of each item in a list, which `todo.get_items` cannot return.

    A TodoItem has no children, so `get_items` stops one level short and this answers the rest.
    Named by position rather than tier: one level below a list is a T3 under a domain and a T5
    under a section.
    """
    node_id = call.data[ATTR_NODE_ID]
    coordinator = _coordinator_for(call.hass, node_id)
    return {
        "nested_items": {
            child["id"]: [
                {
                    "uid": grandchild["id"],
                    "summary": display_name(grandchild.get("name")),
                    "done": grandchild.get("status") == STATUS_COMPLETED,
                }
                for grandchild in coordinator.data.children(child["id"])
            ]
            for child in coordinator.data.children(node_id)
        }
    }


def async_register(hass: HomeAssistant) -> None:
    """Registered once for the integration, at setup rather than per entry: these take a node id
    rather than an entity, so a second account does not want a second copy, and an action that
    exists only while an entry is loaded breaks automations that name it while it is not."""
    hass.services.async_register(DOMAIN, SERVICE_COMPLETE, _complete, schema=_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REOPEN, _reopen, schema=_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_GET_NESTED_ITEMS, _get_nested_items, schema=_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    service.async_register_platform_entity_service(
        hass, DOMAIN, SERVICE_GET_MY_DAY_DETAILS,
        entity_domain=TODO_DOMAIN, func=my_day_details, schema=None,
        supports_response=SupportsResponse.ONLY,
    )
