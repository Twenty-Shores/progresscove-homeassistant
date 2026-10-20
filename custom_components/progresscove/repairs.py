"""The one-step fix for entities whose task the server confirmed is gone."""

from __future__ import annotations

from typing import cast

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
import voluptuous as vol

from .coordinator import ProgressCoveConfigEntry
from .prune import (
    MAX_CHECKS_PER_REFRESH,
    Verdict,
    async_confirm_gone,
    clear_deleted_issue,
    missing_subentries,
    remove_subentries,
    subentry_names,
)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Only the deleted-tasks issue is fixable, and it always carries its entry."""
    assert data is not None
    return DeletedTasksRepairFlow(cast(str, data["entry_id"]))


class DeletedTasksRepairFlow(RepairsFlow):
    """Name the entities, then remove them once the person confirms.

    The server is asked at both steps rather than trusting what the poll found, so a task restored
    in the app since then keeps its entity. Removal waits until every check has been answered:
    nothing is removed on a partial answer.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._gone: list[ConfigSubentry] = []

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        entry = self._loaded_entry()
        if entry is None:
            return self.async_abort(reason="not_loaded")
        coordinator = entry.runtime_data
        # The ones the repair names come first, so a backlog bigger than one check is removed in
        # the order the person was told about it.
        candidates = sorted(
            missing_subentries(entry, coordinator.data),
            key=lambda subentry: (
                coordinator.prune_verdicts.get(subentry.subentry_id) is not Verdict.GONE
            ),
        )
        gone, unconfirmed = await async_confirm_gone(
            coordinator.client, candidates[:MAX_CHECKS_PER_REFRESH]
        )
        if unconfirmed:
            return self.async_abort(reason="cannot_connect")
        if not gone:
            clear_deleted_issue(self.hass, entry)
            return self.async_abort(reason="nothing_to_remove")
        self._gone = gone
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._loaded_entry()
            if entry is None:
                return self.async_abort(reason="not_loaded")
            # Asked again: a task restored in the app while this form was open keeps its entity.
            # One already removed since, by the prune or by another person, is left out.
            still_there = [
                subentry for subentry in self._gone if subentry.subentry_id in entry.subentries
            ]
            gone, unconfirmed = await async_confirm_gone(entry.runtime_data.client, still_there)
            if not unconfirmed:
                remove_subentries(self.hass, entry, gone)
                clear_deleted_issue(self.hass, entry)
                return self.async_create_entry(data={})
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"names": subentry_names(self._gone)},
            errors=errors,
        )

    def _loaded_entry(self) -> ProgressCoveConfigEntry | None:
        """The entry, while it is set up. Its tree and client are what the checks need, and an
        entry that is not loaded has neither."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return None
        return cast(ProgressCoveConfigEntry, entry)
