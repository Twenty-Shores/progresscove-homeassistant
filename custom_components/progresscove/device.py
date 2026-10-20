"""The service device that every entity of one account belongs to."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN


def account_device(account_id: str) -> DeviceInfo:
    """One device per account, keyed by the account rather than the config entry, so removing
    the integration and adding it again finds the same device and the same entities."""
    return DeviceInfo(
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, account_id)},
        name="ProgressCove",
        manufacturer="Twenty Shores",
    )
