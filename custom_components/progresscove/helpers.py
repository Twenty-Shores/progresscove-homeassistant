"""Helpers shared by the entity modules, kept here so no entity imports a sibling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .api import (
    ProgressCoveError,
    ProgressCoveReadOnly,
    ProgressCoveRejected,
    ProgressCoveSubscriptionInactive,
)
from .const import DOMAIN, STATUS_COMPLETED

if TYPE_CHECKING:
    # The coordinator imports the modules that import this one, so the type is only named.
    from .coordinator import ProgressCoveCoordinator

# The apps store a due day with no time of day as 23:59:59.999 in the person's own timezone. The
# .999 is the marker: nobody picks milliseconds.
_NO_TIME_MICROSECOND = 999_000


def parse_due(value: str | None, zone: ZoneInfo) -> datetime | date | None:
    """The API sends tz-aware ISO-8601; HA wants a date or an aware datetime.

    A due day with no time comes back as that day in the house's timezone, so HA shows a date
    rather than 23:59. Never returns a naive datetime: HA rejects those, and the project's timezone
    invariant forbids creating one anywhere.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    if parsed.microsecond == _NO_TIME_MICROSECOND:
        return parsed.astimezone(zone).date()
    return parsed


def due_for_api(due: date | datetime | None, zone: ZoneInfo) -> str | None:
    """A due from HA as the API takes it: always an instant with its offset.

    A date becomes the end of that day in the house's timezone, marked as having no time, the way
    the apps store a due day. A datetime without a timezone is a wall-clock time in the house.
    """
    if due is None:
        return None
    if isinstance(due, datetime):
        return (due if due.tzinfo else due.replace(tzinfo=zone)).isoformat()
    return datetime(
        due.year, due.month, due.day, 23, 59, 59, _NO_TIME_MICROSECOND, tzinfo=zone
    ).isoformat()


def translated_error(
    key: str, error_type: type[HomeAssistantError] = HomeAssistantError, **placeholders: str
) -> HomeAssistantError:
    """An error whose message comes from the `exceptions` catalogue, in the person's language."""
    return error_type(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


@contextmanager
def surfaced(failure_key: str) -> Iterator[None]:
    """Make every write failure reach the person who asked for it.

    `failure_key` names the message for a failure nobody could classify. A rejection the server
    explained is shown in its own words; anything unclassifiable still says something went wrong
    rather than dying in the log. A refusal of what was asked is a ServiceValidationError, so
    Home Assistant shows it without logging a fault; a failure to reach or use the server is not.
    """
    try:
        yield
    except ProgressCoveReadOnly as err:
        raise translated_error("read_only", ServiceValidationError) from err
    except ProgressCoveRejected as err:
        raise translated_error("rejected", ServiceValidationError, reason=str(err)) from err
    except ProgressCoveSubscriptionInactive as err:
        raise translated_error("subscription_inactive") from err
    except ProgressCoveError as err:
        raise translated_error(failure_key) from err


def refuse_if_read_only(coordinator: ProgressCoveCoordinator) -> None:
    """Raise before a write the token would only have bounced.

    The server enforces this regardless. Asking first spares a round trip and puts the reason in
    front of the person instead of a generic failure.
    """
    if coordinator.read_only:
        raise translated_error("read_only", ServiceValidationError)


def repeats(node: dict[str, Any]) -> bool:
    """Whether completing this node advances a schedule rather than finishing it.

    The line between reversible and not: completing a repeat moves its due date and uncompleting
    does not move it back, so reopening one leaves it on the wrong day.
    """
    return bool(node.get("recurrence_rule"))


def due_date(node: dict[str, Any], zone: ZoneInfo) -> date | None:
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


def is_due(node: dict[str, Any]) -> bool:
    """Whether today is the day this task is for, in the house's timezone: "due today" is a
    question about the wall calendar, not about UTC.

    False for an undated task, which has no day to be. `can_complete` is the one to ask before
    acting: an undated task is completable but never "due".

    `<=` rather than `==`, so an occurrence whose day has passed but which the server has not yet
    rolled still counts.
    """
    if not node or node.get("status") == STATUS_COMPLETED:
        return False
    zone = dt_util.get_default_time_zone()
    due = due_date(node, zone)
    return due is not None and due <= dt_util.now().date()


def can_complete(node: dict[str, Any]) -> bool:
    """Whether completing this task now is a sensible thing to do.

    Only a FUTURE due date says no, since completing an occurrence that has not arrived rolls a
    repeat past the real one. An undated task has no too-early to be.
    """
    if not node or node.get("status") == STATUS_COMPLETED:
        return False
    zone = dt_util.get_default_time_zone()
    due = due_date(node, zone)
    return due is None or due <= dt_util.now().date()

