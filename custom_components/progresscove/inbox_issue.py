"""The Repairs issue shown while the inbox, and with it the capture list, could not be loaded."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def _issue_id(entry: ConfigEntry) -> str:
    return f"inbox_unavailable_{entry.entry_id}"


def raise_issue(hass: HomeAssistant, entry: ConfigEntry, reason: str) -> None:
    """Tell the person the capture list is missing and what the server said.

    Not fixable from the repair: the inbox is resolved when the entry sets up, so the remedy is
    reloading the integration once the cause is gone, which the description says.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="inbox_unavailable",
        translation_placeholders={"reason": reason},
    )


def clear_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
