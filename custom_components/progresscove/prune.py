"""Remove lists, buttons, switches and sensors whose task no longer exists.

A subentry is removed only after the server confirms, with a 404, that its task is gone: on its
own when the person turned that on, otherwise from a repair they confirm. Anything the server could
not answer leaves the subentry in place and is reported as a repair of its own.
"""

# WHAT AUTHORISES A DELETION: a 404 for that specific node, and nothing else. Absence from the bulk
# tree is not evidence, since the response could have been truncated or malformed, and acting on it
# would delete configured entities over a bad poll. Every other outcome leaves the subentry alone.
#
# An expired token is a 401 and a lapsed subscription a 403, so neither reaches here as a 404. The
# one real ambiguity is a shared node whose access was revoked, where the server answers 404 to
# avoid disclosing that it exists; the end state is the same dead entity removed.

from __future__ import annotations

from enum import StrEnum
import logging

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import ProgressCoveClient, ProgressCoveError, ProgressCoveNotFound
from .const import (
    CONF_AUTO_PRUNE,
    CONF_NODE_ID,
    DEFAULT_AUTO_PRUNE,
    DOMAIN,
    SUBENTRY_BUTTON,
    SUBENTRY_LIST,
    SUBENTRY_SENSOR,
    SUBENTRY_SWITCH,
)
from .tree import Tree

_LOGGER = logging.getLogger(__name__)

_NODE_SUBENTRY_TYPES = (SUBENTRY_LIST, SUBENTRY_BUTTON, SUBENTRY_SWITCH, SUBENTRY_SENSOR)

# Deleting a project in the app can leave dozens missing at once, and one request each inside a
# single poll is a burst the rate limit would rightly punish. The rest wait for later refreshes.
MAX_CHECKS_PER_REFRESH = 5


class Verdict(StrEnum):
    """What the server last said about the node of a subentry missing from the tree."""

    GONE = "gone"
    PRESENT = "present"
    UNKNOWN = "unknown"


# The last verdict per missing subentry, by subentry id, least recently asked first. A pass that may
# only ask a few starts at the front, so a backlog bigger than one pass is worked through in turn
# instead of the same few being asked on every poll while the rest wait for them to be fixed.
type Verdicts = dict[str, Verdict]

# The server confirmed these are gone, and the person removes them from the repair.
ISSUE_DELETED = "stale_entities"
# The server could not say, so there is nothing to remove yet.
ISSUE_UNCONFIRMED = "unconfirmed_entities"


def missing_subentries(entry: ConfigEntry, tree: Tree) -> list[ConfigSubentry]:
    """The node subentries whose node is absent from the tree.

    Absence only raises the question; `async_confirm_gone` answers it.
    """
    return [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type in _NODE_SUBENTRY_TYPES
        and subentry.data.get(CONF_NODE_ID)
        and subentry.data[CONF_NODE_ID] not in tree.by_id
    ]


def _least_recently_asked(
    subentries: list[ConfigSubentry], verdicts: Verdicts
) -> list[ConfigSubentry]:
    """Those never asked about first, in the entry's order, then the rest, longest waiting first."""
    asked_at = {subentry_id: turn for turn, subentry_id in enumerate(verdicts)}
    return sorted(subentries, key=lambda subentry: asked_at.get(subentry.subentry_id, -1))


async def async_confirm_gone(
    client: ProgressCoveClient, subentries: list[ConfigSubentry]
) -> tuple[list[ConfigSubentry], list[ConfigSubentry]]:
    """Ask the server about each one, as (confirmed gone, could not tell).

    A subentry whose node the server still has is in neither: it is not stale, whatever the tree
    said.
    """
    gone: list[ConfigSubentry] = []
    unconfirmed: list[ConfigSubentry] = []
    for subentry in subentries:
        node_id = subentry.data[CONF_NODE_ID]
        try:
            await client.async_node_exists(node_id)
        except ProgressCoveNotFound:
            gone.append(subentry)
        except ProgressCoveError as err:
            _LOGGER.debug("Could not confirm task %s, leaving it alone: %s", node_id, err)
            unconfirmed.append(subentry)
    return gone, unconfirmed


def remove_subentries(
    hass: HomeAssistant, entry: ConfigEntry, subentries: list[ConfigSubentry]
) -> None:
    """Remove each one. Called only once every check has finished: the removals reload the entry,
    which must not start while a pass is still asking the server about the rest."""
    for subentry in subentries:
        _LOGGER.info(
            "Removing %s: the server confirmed task %s no longer exists",
            subentry.title, subentry.data[CONF_NODE_ID],
        )
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)


async def async_prune_deleted(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: ProgressCoveClient,
    tree: Tree,
    verdicts: Verdicts,
) -> bool:
    """Confirm-then-remove, for subentries whose node is missing from the tree.

    Call this only with a tree from a SUCCESSFUL fetch: the design rests on the tree being
    trustworthy enough to raise the question, and a 404 being the only thing that answers it.

    `verdicts` is what earlier passes learned, and is updated in place for the next one. The
    repairs name every subentry it holds a verdict for, not only the ones this pass asked about.

    Returns whether this pass settled every missing subentry. False means some were left unasked
    or unanswered, and a later pass owes them another look even if the tree has not changed.
    """
    # An empty tree is never read as "the account was emptied": an account with no nodes is
    # indistinguishable from a response that lost its payload, and one reading deletes everything.
    if not tree.by_id:
        return True

    missing = missing_subentries(entry, tree)
    _forget_all_but(verdicts, missing)
    batch = _least_recently_asked(missing, verdicts)[:MAX_CHECKS_PER_REFRESH]
    gone, unconfirmed = await async_confirm_gone(client, batch)
    _record(verdicts, batch, gone, unconfirmed)
    if entry.options.get(CONF_AUTO_PRUNE, DEFAULT_AUTO_PRUNE):
        remove_subentries(hass, entry, gone)
        missing = [subentry for subentry in missing if subentry not in gone]
        _forget_all_but(verdicts, missing)
    _sync_issue(
        hass, entry, ISSUE_DELETED, _with_verdict(missing, verdicts, Verdict.GONE), fixable=True
    )
    _sync_issue(
        hass, entry, ISSUE_UNCONFIRMED, _with_verdict(missing, verdicts, Verdict.UNKNOWN),
        fixable=False,
    )
    return len(verdicts) == len(missing) and Verdict.UNKNOWN not in verdicts.values()


def _forget_all_but(verdicts: Verdicts, subentries: list[ConfigSubentry]) -> None:
    """Drop the verdicts of subentries no longer in question: back in the tree, or removed."""
    keep = {subentry.subentry_id for subentry in subentries}
    for subentry_id in [subentry_id for subentry_id in verdicts if subentry_id not in keep]:
        del verdicts[subentry_id]


def _record(
    verdicts: Verdicts,
    asked: list[ConfigSubentry],
    gone: list[ConfigSubentry],
    unconfirmed: list[ConfigSubentry],
) -> None:
    """Store each answer, moving what was just asked to the back of the queue."""
    for subentry in asked:
        verdicts.pop(subentry.subentry_id, None)
        if subentry in gone:
            verdicts[subentry.subentry_id] = Verdict.GONE
        elif subentry in unconfirmed:
            verdicts[subentry.subentry_id] = Verdict.UNKNOWN
        else:
            verdicts[subentry.subentry_id] = Verdict.PRESENT


def _with_verdict(
    subentries: list[ConfigSubentry], verdicts: Verdicts, verdict: Verdict
) -> list[ConfigSubentry]:
    return [subentry for subentry in subentries if verdicts.get(subentry.subentry_id) is verdict]


def _issue_id(key: str, entry: ConfigEntry) -> str:
    return f"{key}_{entry.entry_id}"


def _sync_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    key: str,
    subentries: list[ConfigSubentry],
    *,
    fixable: bool,
) -> None:
    """Raise the repair naming these subentries, or clear it when there are none.

    The fixable one carries only the entry: its flow asks the server again before removing
    anything, rather than trusting a list that may have gone out of date while it waited.
    """
    if not subentries:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(key, entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(key, entry),
        is_fixable=fixable,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders={"names": subentry_names(subentries)},
        data={"entry_id": entry.entry_id} if fixable else None,
    )


def subentry_names(subentries: list[ConfigSubentry]) -> str:
    return ", ".join(sorted(subentry.title for subentry in subentries))


def clear_deleted_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(ISSUE_DELETED, entry))


def clear_stale_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop both repairs when their entry is removed, so they do not outlive what they name."""
    clear_deleted_issue(hass, entry)
    ir.async_delete_issue(hass, DOMAIN, _issue_id(ISSUE_UNCONFIRMED, entry))
