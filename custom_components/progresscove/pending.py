"""A grace period between "I tapped complete" and the server hearing about it.

A completion is held for the undo window and fires only when it closes. Undo inside the window
cancels the timer, so nothing was sent and there is nothing to reverse. An entry that unloads, or
a Home Assistant that stops, inside the window sends what it holds at once rather than dropping it.
"""

# NOT A SERVER UNDO, and it cannot become one: completing a repeat advances its due date
# server-side, and uncompleting does not restore it. The only safe undo is one where the server was
# never told.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import CONF_COMPLETE_EARLY, DEFAULT_COMPLETE_EARLY, DOMAIN, STATUS_COMPLETED
from .helpers import (
    can_complete,
    due_date,
    refuse_if_read_only,
    surfaced,
    translated_error,
)
from .names import display_name

if TYPE_CHECKING:
    # The coordinator owns a PendingCompletions, so the type is only named here.
    from .coordinator import ProgressCoveCoordinator

_LOGGER = logging.getLogger(__name__)

# Long enough to reach for undo, short enough that nobody wonders whether it worked.
UNDO_WINDOW_SECONDS = 10

# Home Assistant gives its shutdown jobs 20 seconds before it moves on and cancels every background
# task, a held completion included. Ending ours first is what lets it say which ones were cut off.
SHUTDOWN_FLUSH_SECONDS = 15


def needs_window(node: dict[str, Any]) -> bool:
    """Whether completing this node has to be held before it is sent.

    Only a repeat, whose completion is irreversible the moment it lands. A plain task reopens
    afterwards, so holding it only made a checkbox sit unchanged for ten seconds.
    """
    return bool(node.get("recurrence_rule"))


def refuse_if_too_early(
    coordinator: ProgressCoveCoordinator, node: dict[str, Any], name: str
) -> None:
    """Raise if this task is not due yet, unless the user opted into completing early.

    Completing an occurrence that has not arrived rolls a repeat past the real one, so the task
    the user was waiting for never appears. Refused by default on every surface; a switch can be
    turned off by an automation or a voice assistant without anyone deciding to, which is exactly
    when a silent roll would go unnoticed.
    """
    if coordinator.config_entry.options.get(CONF_COMPLETE_EARLY, DEFAULT_COMPLETE_EARLY):
        return
    if can_complete(node):
        return
    due = due_date(node, dt_util.get_default_time_zone())
    if due is None:
        raise translated_error("not_due_yet_undated", ServiceValidationError, name=name)
    raise translated_error(
        "not_due_yet", ServiceValidationError, name=name, due=due.isoformat()
    )


def complete(
    coordinator: ProgressCoveCoordinator, node_id: str, repaint: Callable[[], None]
) -> None:
    """Complete a task through the undo window, whichever surface asked.

    Home Assistant makes us publish a button and a switch for what is one action, and services and
    the My Day list ask for the same thing again. They differ only in how the surface repaints, so
    the completion itself lives here: an entity that skipped the window would send instantly while
    its neighbour held, and the same task would behave differently depending on which tile the user
    happened to press.

    Completing an already-completed task is a no-op rather than a second call.
    """
    refuse_if_read_only(coordinator)
    node = coordinator.data.by_id.get(node_id, {})
    if node.get("status") == STATUS_COMPLETED:
        return

    async def send() -> None:
        await send_completion(coordinator, node_id)

    coordinator.pending.schedule(node_id, send, repaint, hold=needs_window(node))


async def send_completion(coordinator: ProgressCoveCoordinator, node_id: str) -> None:
    """Send a completion and refresh; on failure tell the user, then re-raise for the log.

    A held completion is sent seconds after the tap with nobody waiting on the result, so a
    failure would otherwise be a button that quietly undid itself. Any failure, classified or not,
    is told: the person believes the task is done, and only this notification says otherwise.
    """
    try:
        with surfaced("complete_failed"):
            await coordinator.client.async_complete(node_id)
    except Exception:
        await _async_notify_not_sent(coordinator, node_id)
        raise
    if coordinator.pending.closing:
        # The next setup refreshes anyway, and a refresh now could prune subentries of an entry
        # that is being torn down.
        return
    # Not async_request_refresh: in its cooldown a second write waits 10 s to show.
    await coordinator.async_refresh()


async def _async_notify_not_sent(coordinator: ProgressCoveCoordinator, node_id: str) -> None:
    """Say, in the house's language, that a held completion never reached the server.

    One notification per task, so a task failing repeatedly replaces its message instead of
    stacking.
    """
    hass = coordinator.hass
    catalogue = await async_get_translations(hass, hass.config.language, "exceptions", {DOMAIN})
    template = catalogue[f"component.{DOMAIN}.exceptions.complete_not_sent.message"]
    name = display_name(coordinator.data.by_id.get(node_id, {}).get("name")) or node_id
    persistent_notification.async_create(
        hass,
        template.format(name=name),
        title="ProgressCove",
        notification_id=f"{DOMAIN}_complete_failed_{node_id}",
    )


class PendingCompletions:
    """Completions waiting out their undo window, keyed by node id.

    One instance on the coordinator, so tapping on a switch and undoing from a card works: they
    are the same task.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Set to end a completion's undo window early.
        self._send_now: dict[str, asyncio.Event] = {}
        self.closing = False

    def is_pending(self, node_id: str) -> bool:
        return node_id in self._tasks

    @property
    def node_ids(self) -> set[str]:
        return set(self._tasks)

    def schedule(
        self,
        node_id: str,
        send: Callable[[], Awaitable[None]],
        on_change: Callable[[], None],
        *,
        hold: bool = True,
    ) -> None:
        """Run `send`, after the undo window when `hold`, immediately when not.

        A second tap on an already-pending task is a no-op rather than a second timer: restarting
        the countdown would make the undo window drift away from the user.
        """
        if node_id in self._tasks:
            return
        # A tap that lands while the entry closes has no window left to wait out.
        delay = UNDO_WINDOW_SECONDS if hold and not self.closing else 0
        send_now = self._send_now[node_id] = asyncio.Event()
        # Not eager: an eager start could finish a completion before it is recorded here, and the
        # `finally` in _run would then clear an entry that is only written afterwards, leaving the
        # task pending forever.
        self._tasks[node_id] = self._entry.async_create_background_task(
            self._hass,
            self._run(node_id, send, on_change, delay, send_now),
            name=f"{DOMAIN} completion {node_id}",
            eager_start=False,
        )
        on_change()

    def undo(self, node_id: str) -> bool:
        """Cancel a pending completion. True if there was one to cancel."""
        task = self._tasks.pop(node_id, None)
        if task is None:
            return False
        self._send_now.pop(node_id, None)
        task.cancel()
        return True

    async def async_flush(self) -> None:
        """Send every held completion now, because the entry is unloading or Home Assistant is
        stopping.

        The person saw the task ticked, so the completion is owed. Dropping it here would revert
        the tick after a reload with nothing said, and a reload is what every subentry change
        does. A failed send still reaches the person through the not-sent notification, which
        needs nothing the unload takes away, and is logged as an error, which is all that outlives
        a restart.

        Sent together, so a slow server costs one request timeout rather than one per task. Looped,
        because a tap that lands meanwhile is scheduled without a window and has to be waited for
        too.
        """
        self.closing = True
        while self._tasks:
            for send_now in self._send_now.values():
                send_now.set()
            sent, _ = await asyncio.wait(list(self._tasks.values()))
            # Nothing is left to log an unexpected failure later: Home Assistant may be exiting.
            for task in sent:
                if not task.cancelled() and (err := task.exception()) is not None:
                    _LOGGER.error("Could not complete a ProgressCove task", exc_info=err)

    async def async_flush_before_shutdown(self) -> None:
        """Flush as Home Assistant starts stopping, while the web session and the tasks still run.

        A shutdown job, not a stop-event listener: Home Assistant cancels every background task,
        the held completions among them, before it fires the stop event. Its notifications are
        not persisted, so a completion cut off here is logged, the one record a restart keeps.
        """
        try:
            async with asyncio.timeout(SHUTDOWN_FLUSH_SECONDS):
                await self.async_flush()
        except TimeoutError:
            _LOGGER.error(
                "Home Assistant stopped before ProgressCove confirmed these completions, so they "
                "may not have been saved: %s",
                ", ".join(sorted(self._tasks)),
            )

    async def _run(
        self,
        node_id: str,
        send: Callable[[], Awaitable[None]],
        on_change: Callable[[], None],
        delay: float,
        send_now: asyncio.Event,
    ) -> None:
        try:
            if delay:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(send_now.wait(), delay)
            await send()
        except HomeAssistantError as err:
            # Already told to the person by send_completion. Anything unexpected is left to
            # escape, so Home Assistant logs it with its traceback.
            _LOGGER.error(
                "Could not complete task %s: it is still open in ProgressCove: %s", node_id, err
            )
        finally:
            # Cleared before notifying, so a listener re-reading is_pending sees the truth. Only if
            # it is still this task's: an undo followed by a fresh tap has already replaced it.
            if self._tasks.get(node_id) is asyncio.current_task():
                del self._tasks[node_id]
                del self._send_now[node_id]
            on_change()
