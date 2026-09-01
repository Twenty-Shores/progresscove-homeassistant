"""Services taking a node id rather than an entity, so they reach any depth.

`todo.update_item` resolves its item against the entity's own items, which stops at a list's direct
children. `complete` and `reopen` go deeper; `get_nested_items` reads one level below a list.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, STATUS_COMPLETED
from .pending import complete
from .names import display_name
from .helpers import surfaced, repeats

_LOGGER = logging.getLogger(__name__)

SERVICE_COMPLETE = "complete"
SERVICE_REOPEN = "reopen"
SERVICE_GET_NESTED_ITEMS = "get_nested_items"
ATTR_NODE_ID = "node_id"

_SCHEMA = vol.Schema({vol.Required(ATTR_NODE_ID): cv.string})


def _coordinator_for(hass: HomeAssistant, node_id: str):
    """The entry that owns this node.

    Searched rather than passed: a node id is globally unique, so the caller does not have to know
    which entry it belongs to.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator and node_id in coordinator.data.by_id:
            return coordinator
    raise ServiceValidationError(
        f"No ProgressCove task with id {node_id}. Copy it from a card or the node_id attribute."
    )


async def _complete(call: ServiceCall) -> None:
    node_id = call.data[ATTR_NODE_ID]
    coordinator = _coordinator_for(call.hass, node_id)
    # async_update_listeners repaints every entity at once; without it the card waits for the
    # window to close before the tick appears.
    complete(coordinator, node_id, coordinator.async_update_listeners)


async def _reopen(call: ServiceCall) -> None:
    node_id = call.data[ATTR_NODE_ID]
    coordinator = _coordinator_for(call.hass, node_id)
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
        raise ServiceValidationError(
            f"{display_name(node.get('name')) or 'That task'} repeats, so completing it moved "
            "it to its next occurrence. Reopening it here would leave it on the wrong day."
        )
    with surfaced("reopen that task"):
        await coordinator.client.async_uncomplete(node_id)
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
    """Registered once for the integration: these take a node id rather than an entity, so a
    second account does not want a second copy."""
    if hass.services.has_service(DOMAIN, SERVICE_COMPLETE):
        return
    hass.services.async_register(DOMAIN, SERVICE_COMPLETE, _complete, schema=_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REOPEN, _reopen, schema=_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_GET_NESTED_ITEMS, _get_nested_items, schema=_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
