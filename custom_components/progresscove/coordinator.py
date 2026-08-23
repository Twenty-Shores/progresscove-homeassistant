"""One poll, shared by every entity, so ten lists do not make ten requests a minute."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import ProgressCoveAuthError, ProgressCoveClient, ProgressCoveError
from .pending import PendingCompletions
from .names import PATH_SEPARATOR, display_name
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class Tree:
    """The tree, indexed once per poll so every entity lookup is a dict access."""

    children_of: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    # As the SERVER computed it. Re-deriving today here would give this surface a different one
    # than the app shows.
    my_day: list[dict[str, Any]] = field(default_factory=list)

    def children(self, node_id: str) -> list[dict[str, Any]]:
        return self.children_of.get(node_id, [])

    def path_of(self, node_id: str) -> str:
        """`Home › Shopping › Groceries`, so two lists of the same name are tellable apart.

        The seen-set is not padding: a parent chain that loops appends names until the process
        dies, and this runs on every poll.
        """
        names: list[str] = []
        seen: set[str] = set()
        current = self.by_id.get(node_id)
        while current is not None and current["id"] not in seen:
            seen.add(current["id"])
            names.append(display_name(current["name"]))
            parent = current.get("parent_id")
            current = self.by_id.get(parent) if parent else None
        return PATH_SEPARATOR.join(reversed(names))

    def candidate_lists(self) -> list[dict[str, Any]]:
        """Every node, whatever tier and whether or not it has children today.

        Deliberately unfiltered: a task is one subtask away from being a list, so hiding childless
        nodes would disqualify the ones most likely to become one.
        """
        return list(self.by_id.values())

    @classmethod
    def from_nodes(
        cls, nodes: list[dict[str, Any]], my_day: list[dict[str, Any]] | None = None
    ) -> Tree:
        tree = cls()
        tree.by_id = {n["id"]: n for n in nodes}
        for node in nodes:
            parent = node.get("parent_id")
            if parent:
                tree.children_of.setdefault(parent, []).append(node)

        # display_order is the user's own arrangement; the id only breaks ties, so a list does
        # not reshuffle between polls.
        def order(item: dict[str, Any]) -> tuple[float, str]:
            return (item.get("display_order") if item.get("display_order") is not None else 1e9,
                    item["id"])

        for items in tree.children_of.values():
            items.sort(key=order)
        tree.my_day = my_day or []
        return tree


class ProgressCoveCoordinator(DataUpdateCoordinator[Tree]):
    """Polls the tree and exposes it as an indexed structure."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ProgressCoveClient,
        scan_minutes: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_minutes),
            config_entry=entry,
        )
        self.client = client
        # One set for the whole entry: a completion tapped on a switch and undone from a card is
        # the same task.
        self.pending = PendingCompletions()

    async def _async_update_data(self) -> Tree:
        try:
            # Independent reads, so not two round trips in series.
            nodes, my_day = await asyncio.gather(
                self.client.async_get_tree(),
                self.client.async_get_my_day(),
            )
        except ProgressCoveAuthError as err:
            # Not UpdateFailed: this opens the reauth flow rather than showing "unavailable"
            # forever with no way to act on it.
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProgressCoveError as err:
            raise UpdateFailed(str(err)) from err
        return Tree.from_nodes(nodes, my_day)

    async def _async_refresh(self, *args: Any, **kwargs: Any) -> None:
        """Refresh, then reconcile subentries against what came back.

        The prune runs AFTER the base class, which is what settles `last_update_success` and
        `self.data`; it reads both to decide whether the tree can be acted on at all.
        """
        await super()._async_refresh(*args, **kwargs)
        if not self.last_update_success or self.config_entry is None:
            return
        from .prune import async_prune_deleted

        try:
            await async_prune_deleted(self.hass, self.config_entry, self)
        except Exception:  # noqa: BLE001 - housekeeping must never take the poll down with it
            _LOGGER.exception("Pruning deleted tasks failed", extra={"event": "prune_failed"})
