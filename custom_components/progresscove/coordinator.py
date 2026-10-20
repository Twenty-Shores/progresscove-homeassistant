"""One poll, shared by every entity, so ten lists do not make ten requests a minute."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ProgressCoveAuthError,
    ProgressCoveClient,
    ProgressCoveError,
    ProgressCoveSubscriptionInactive,
)
from .const import (
    CONF_TOKEN_SCOPE,
    DOMAIN,
    SCOPE_READ,
    SCOPE_READ_WRITE,
    SUBSCRIPTION_RETRY,
    UPDATE_INTERVAL,
)
from .inbox_issue import clear_issue as clear_inbox_issue, raise_issue as raise_inbox_issue
from .pending import PendingCompletions
from .prune import Verdicts, async_prune_deleted
from .read_only import sync_issue
from .reload import SetupInputs
from .subscription_issue import (
    clear_issue as clear_subscription_issue,
    raise_issue as raise_subscription_issue,
)
from .tree import Tree

_LOGGER = logging.getLogger(__name__)

type ProgressCoveConfigEntry = ConfigEntry[ProgressCoveCoordinator]


class ProgressCoveCoordinator(DataUpdateCoordinator[Tree]):
    """Polls the tree and exposes it as an indexed structure."""

    config_entry: ProgressCoveConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ProgressCoveConfigEntry,
        client: ProgressCoveClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        # Every entry is created with the account's inbox id as its unique id; Home Assistant only
        # types it optional because other integrations' flows may leave it unset.
        self.account_id = cast(str, entry.unique_id)
        # One set for the whole entry: a completion tapped on a switch and undone from a card is
        # the same task.
        self.pending = PendingCompletions(hass, entry)
        # The same id the entry's unique id was set from, fetched again at setup rather than read
        # back from it: the fetch is what proves the inbox is reachable with the current token,
        # and a failure there is shown as a repair instead of a capture list that never appears.
        self.inbox_id: str | None = None
        # Seeded from what the entry last learned, then corrected by every refresh.
        self.read_only: bool = entry.data.get(CONF_TOKEN_SCOPE) == SCOPE_READ
        # What the current tree was built from. The client hands back these same objects for an
        # endpoint that answered 304, so identity is what says nothing changed.
        self._nodes: list[dict[str, Any]] | None = None
        self._my_day: list[dict[str, Any]] | None = None
        # Set while the last prune left subentries it could not settle, so an unchanged poll still
        # gets back to them.
        self._prune_owed = False
        # Kept across polls, so a backlog bigger than one prune pass is asked about in turn.
        self.prune_verdicts: Verdicts = {}
        # Set by setup once the entities are about to be built from the entry; see reload.py.
        self.setup_inputs: SetupInputs | None = None
        self.reload_scheduled = False

    async def async_resolve_inbox(self) -> None:
        """Find the account's inbox, so captures have somewhere to go.

        A missing inbox is not fatal: everything else in this integration works without one, and
        refusing to set up over it would trade every feature for the one that is unavailable. It is
        never quiet either. Every account has an inbox, so failing to get it means the server
        refused or broke, and the person is shown that rather than a list that never appears.
        """
        try:
            self.inbox_id = await self.client.async_get_inbox_id()
        except ProgressCoveError as err:
            _LOGGER.warning("Could not load the inbox, so there is no capture list: %s", err)
            self.inbox_id = None
            raise_inbox_issue(self.hass, self.config_entry, str(err))
            return
        clear_inbox_issue(self.hass, self.config_entry)

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
        except ProgressCoveSubscriptionInactive as err:
            # Not an auth failure: a new token would be refused the same way, so a reauth flow
            # could never finish. Failing the update keeps retrying, Home Assistant's own backoff
            # while setting up and SUBSCRIPTION_RETRY once running, and the repair says why.
            raise_subscription_issue(self.hass, self.config_entry)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="subscription_inactive",
                retry_after=SUBSCRIPTION_RETRY.total_seconds(),
            ) from err
        except ProgressCoveError as err:
            raise UpdateFailed(str(err)) from err
        clear_subscription_issue(self.hass, self.config_entry)
        self._apply_token_scope()
        tree_changed = nodes is not self._nodes
        # Rebuilt only from answers that came in full. Returning the same Tree still notifies the
        # entities, which is wanted: due-today and days-until follow the clock, not the data.
        if tree_changed or my_day is not self._my_day:
            tree = Tree.from_nodes(nodes, my_day)
        else:
            tree = self.data
        # Inside the update, so only a fetch that succeeded ever reaches it, whichever path asked
        # for the refresh (the scheduled poll, the first one at setup, the one after a write), and
        # the refresh does not finish until the prune has. The prune reads only the tree, so an
        # unchanged one has nothing new to tell it.
        if tree_changed or self._prune_owed:
            self._prune_owed = not await async_prune_deleted(
                self.hass, self.config_entry, self.client, tree, self.prune_verdicts
            )
        # Last, so a prune that raised is run again on the next poll rather than skipped as done.
        self._nodes, self._my_day = nodes, my_day
        return tree

    def _apply_token_scope(self) -> None:
        """Follow the scope the server reports, on every refresh.

        Entities fix their features when they are built, so a change is written to the entry, whose
        update listener reloads it and builds them again with the right ones.
        """
        scope = self.client.token_scope
        if scope is None:
            return
        self.read_only = scope != SCOPE_READ_WRITE
        sync_issue(self.hass, self.config_entry, self.read_only)
        if self.config_entry.data.get(CONF_TOKEN_SCOPE) != scope:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_TOKEN_SCOPE: scope}
            )
