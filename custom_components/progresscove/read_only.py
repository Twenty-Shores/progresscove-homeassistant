"""The Repairs issue shown while the token can only read."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def _issue_id(entry: ConfigEntry) -> str:
    return f"read_only_{entry.entry_id}"


def sync_issue(hass: HomeAssistant, entry: ConfigEntry, read_only: bool) -> None:
    """Raise the repair while the token is read-only, clear it the moment it is not.

    Not fixable from the repair itself: the remedy is a different token, pasted through the
    integration's Reconfigure, which the description walks through.
    """
    if not read_only:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="read_only",
    )


def clear_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the repair with the entry it belonged to."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
