"""Helpers shared by the entity modules, kept here so no entity imports a sibling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import ProgressCoveError, ProgressCoveRejected
from .const import DEFAULT_SCAN_MINUTES, MAX_SCAN_MINUTES, MIN_SCAN_MINUTES

_LOGGER = logging.getLogger(__name__)


def _parse_due(value: str | None) -> datetime | date | None:
    """The API sends tz-aware ISO-8601; HA wants a date or an aware datetime.

    Never returns a naive datetime: HA rejects those, and the project's timezone invariant forbids
    creating one anywhere.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


@contextmanager
def _surfaced(action: str) -> Iterator[None]:
    """Make every write failure reach the person who asked for it.

    A rejection the server explained is shown in its own words; anything unclassifiable still says
    something went wrong rather than dying in the log.
    """
    try:
        yield
    except ProgressCoveRejected as err:
        raise HomeAssistantError(str(err)) from err
    except ProgressCoveError as err:
        _LOGGER.error("Could not %s: %s", action, err)
        raise HomeAssistantError(f"ProgressCove could not {action}.") from err


def repeats(node: dict) -> bool:
    """Whether completing this node advances a schedule rather than finishing it.

    The line between reversible and not: completing a repeat moves its due date and uncompleting
    does not move it back, so reopening one leaves it on the wrong day.
    """
    return bool(node.get("recurrence_rule"))


def _zone(hass: HomeAssistant) -> ZoneInfo:
    """The house's timezone. "Due today" is a question about the wall calendar, not about UTC."""
    try:
        return ZoneInfo(hass.config.time_zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


def _due_date(node: dict[str, Any], zone: ZoneInfo) -> date | None:
    """The calendar day this task is due, as a person would name it.

    A date-only due is stored as UTC midnight, which is a DAY rather than an instant. Converting
    it to a behind-UTC zone lands on the previous day, so "due Friday" would light a button on
    Thursday afternoon in the Americas. Midnight-UTC is read in UTC; only a real time of day is
    converted into the house's zone.
    """
    raw = node.get("due_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    in_utc = parsed.astimezone(timezone.utc)
    if (in_utc.hour, in_utc.minute, in_utc.second) == (0, 0, 0):
        return in_utc.date()
    return parsed.astimezone(zone).date()


def is_due(node: dict, hass: HomeAssistant) -> bool:
    """Whether today is the day this task is for.

    False for an undated task, which has no day to be. `can_complete` is the one to ask before
    acting: an undated task is completable but never "due".

    `<=` rather than `==`, so an occurrence whose day has passed but which the server has not yet
    rolled still counts.
    """
    from .const import STATUS_COMPLETED

    if not node or node.get("status") == STATUS_COMPLETED:
        return False
    zone = _zone(hass)
    due = _due_date(node, zone)
    return due is not None and due <= datetime.now(zone).date()


def can_complete(node: dict, hass: HomeAssistant) -> bool:
    """Whether completing this task now is a sensible thing to do.

    Only a FUTURE due date says no, since completing an occurrence that has not arrived rolls a
    repeat past the real one. An undated task has no too-early to be.
    """
    from .const import STATUS_COMPLETED

    if not node or node.get("status") == STATUS_COMPLETED:
        return False
    zone = _zone(hass)
    due = _due_date(node, zone)
    return due is None or due <= datetime.now(zone).date()


def scan_minutes(options: dict) -> int:
    """The poll interval, clamped.

    The options flow bounds the FORM; this bounds what is read back from storage, where a
    hand-edited 0 would poll continuously and a string would take setup down inside timedelta.
    """
    raw = options.get("scan_minutes", DEFAULT_SCAN_MINUTES)
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Ignoring an unreadable scan_minutes (%r); using %s", raw, DEFAULT_SCAN_MINUTES
        )
        return DEFAULT_SCAN_MINUTES
    return max(MIN_SCAN_MINUTES, min(MAX_SCAN_MINUTES, minutes))
