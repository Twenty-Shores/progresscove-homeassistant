"""Remove buttons, switches and sensors whose task no longer exists.

A subentry is removed only after the server confirms, with a 404, that its task is gone. Anything
else leaves it in place and raises a repair instead.
"""

# WHAT AUTHORISES A DELETION: a 404 for that specific node, and nothing else. Absence from the bulk
# tree is not evidence, since the response could have been truncated or malformed, and acting on it
# would delete configured entities over a bad poll. Every other outcome leaves the subentry alone.
#
# An expired token is a 401 and a lapsed subscription a 403, so neither reaches here as a 404. The
# one real ambiguity is a shared node whose access was revoked, where the server answers 404 to
# avoid disclosing that it exists; the end state is the same dead entity removed.

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import ProgressCoveError, ProgressCoveNotFound
from .const import CONF_AUTO_PRUNE, DEFAULT_AUTO_PRUNE, DOMAIN

_LOGGER = logging.getLogger(__name__)

# `list` is deliberately absent: its node id lives in entry.data, and todo.py already drops it by
# intersecting the selection with the live tree.
_NODE_SUBENTRY_TYPES = ("button", "switch", "sensor")

# Deleting a project in the app can leave dozens missing at once, and one request each inside a
# single poll is a burst the rate limit would rightly punish. The rest wait for later refreshes.
MAX_CHECKS_PER_REFRESH = 5


def _node_subentries(entry: ConfigEntry) -> list[tuple[str, Any]]:
    """Subentries that point at one node, as (node_id, subentry)."""
    return [
        (subentry.data["node_id"], subentry)
        for subentry in entry.subentries.values()
        if subentry.subentry_type in _NODE_SUBENTRY_TYPES and subentry.data.get("node_id")
    ]


async def async_prune_deleted(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Confirm-then-remove, for subentries whose node is missing from the tree.

    Call this only after a SUCCESSFUL refresh: the design rests on the tree being trustworthy
    enough to raise the question, and a 404 being the only thing that answers it.
    """
    if not coordinator.last_update_success:
        return
    # An empty tree is never read as "the account was emptied": an account with no nodes is
    # indistinguishable from a response that lost its payload, and one reading deletes everything.
    if not coordinator.data.by_id:
        return

    missing = [
        (node_id, subentry)
        for node_id, subentry in _node_subentries(entry)
        if node_id not in coordinator.data.by_id
    ]
    if not missing:
        return

    auto = entry.options.get(CONF_AUTO_PRUNE, DEFAULT_AUTO_PRUNE)
    unconfirmed: list[str] = []
    confirmed_gone: list[Any] = []

    for node_id, subentry in missing[:MAX_CHECKS_PER_REFRESH]:
        try:
            await coordinator.client.async_node_exists(node_id)
        except ProgressCoveNotFound:
            if not auto:
                unconfirmed.append(subentry.title)
                continue
            _LOGGER.info(
                "Removing %s: the server confirmed task %s no longer exists",
                subentry.title, node_id,
                extra={"event": "progresscove_pruned", "node_id": node_id},
            )
            confirmed_gone.append(subentry)
        except ProgressCoveError as err:
            _LOGGER.debug("Could not confirm task %s, leaving it alone: %s", node_id, err)
            unconfirmed.append(subentry.title)

    # After the loop, not inside it: each removal reloads the entry, and a reload cancels every
    # completion still inside its undo window.
    for subentry in confirmed_gone:
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)

    _sync_issue(hass, entry, unconfirmed)


def _sync_issue(hass: HomeAssistant, entry: ConfigEntry, stale: list[str]) -> None:
    """Raise or clear the repair for entities we would not delete ourselves."""
    issue_id = f"stale_entities_{entry.entry_id}"
    if not stale:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="stale_entities",
        translation_placeholders={"names": ", ".join(sorted(stale))},
    )
