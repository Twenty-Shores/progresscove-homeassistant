"""The Repairs issue shown while the account's subscription is not active."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def _issue_id(entry: ConfigEntry) -> str:
    return f"subscription_inactive_{entry.entry_id}"


def raise_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Tell the person why nothing loads, and that renewing is the whole fix.

    Not fixable from the repair: the remedy is in the ProgressCove app, and the entry keeps
    retrying on its own, so it reconnects without anything being done here.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="subscription_inactive",
    )


def clear_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
