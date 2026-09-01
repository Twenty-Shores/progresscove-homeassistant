"""A grace period between "I tapped complete" and the server hearing about it.

A completion is held for the undo window and fires only when it closes. Undo inside the window
cancels the timer, so nothing was sent and there is nothing to reverse.
"""

# NOT A SERVER UNDO, and it cannot become one: completing a repeat advances its due date
# server-side, and uncompleting does not restore it. The only safe undo is one where the server was
# never told.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from homeassistant.exceptions import HomeAssistantError

from .const import CONF_COMPLETE_EARLY, DEFAULT_COMPLETE_EARLY, STATUS_COMPLETED
from .helpers import can_complete, due_date, surfaced, timezone_of

_LOGGER = logging.getLogger(__name__)

# Long enough to reach for undo, short enough that nobody wonders whether it worked.
UNDO_WINDOW_SECONDS = 10


def needs_window(node: dict) -> bool:
    """Whether completing this node has to be held before it is sent.

    Only a repeat, whose completion is irreversible the moment it lands. A plain task reopens
    afterwards, so holding it only made a checkbox sit unchanged for ten seconds.
    """
    return bool(node.get("recurrence_rule"))


def refuse_if_too_early(coordinator, node, name: str, hass) -> None:
    """Raise if this task is not due yet, unless the user opted into completing early.

    Completing an occurrence that has not arrived rolls a repeat past the real one, so the task
    the user was waiting for never appears. Refused by default on every surface; a switch can be
    turned off by an automation or a voice assistant without anyone deciding to, which is exactly
    when a silent roll would go unnoticed.
    """
    if coordinator.config_entry.options.get(CONF_COMPLETE_EARLY, DEFAULT_COMPLETE_EARLY):
        return
    if can_complete(node, hass):
        return
    due = due_date(node, timezone_of(hass))
    raise HomeAssistantError(
        f"{name} is not due yet, next on {due.isoformat() if due else 'a later date'}."
    )


def complete(coordinator, node_id: str, repaint) -> None:
    """Complete a task through the undo window, whichever surface asked.

    Home Assistant makes us publish a button and a switch for what is one action, and services and
    the My Day list ask for the same thing again. They differ only in how the surface repaints, so
    the completion itself lives here: an entity that skipped the window would send instantly while
    its neighbour held, and the same task would behave differently depending on which tile the user
    happened to press.

    Completing an already-completed task is a no-op rather than a second call.
    """
    node = coordinator.data.by_id.get(node_id, {})
    if node.get("status") == STATUS_COMPLETED:
        return

    async def send() -> None:
        with surfaced("complete that task"):
            await coordinator.client.async_complete(node_id)
        await coordinator.async_refresh()

    coordinator.pending.schedule(node_id, send, repaint, hold=needs_window(node))


class PendingCompletions:
    """Completions waiting out their undo window, keyed by node id.

    One instance on the coordinator, so tapping on a switch and undoing from a card works: they
    are the same task.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

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
        delay = UNDO_WINDOW_SECONDS if hold else 0
        self._tasks[node_id] = asyncio.create_task(self._run(node_id, send, on_change, delay))
        on_change()

    def undo(self, node_id: str) -> bool:
        """Cancel a pending completion. True if there was one to cancel."""
        task = self._tasks.pop(node_id, None)
        if task is None:
            return False
        task.cancel()
        return True

    def cancel_all(self) -> None:
        """Drop every pending completion without sending it, because the entry is unloading.

        Deliberately does NOT flush: a write at teardown has nothing left to report a failure to,
        and an un-sent completion is visible where a lost one is not.
        """
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _run(
        self,
        node_id: str,
        send: Callable[[], Awaitable[None]],
        on_change: Callable[[], None],
        delay: float,
    ) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            await send()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Nothing awaits this task, so an escaping exception would surface only as "Task
            # exception was never retrieved" and the entity would quietly revert.
            _LOGGER.error(
                "Could not complete task %s: it is still open in ProgressCove", node_id,
                exc_info=True,
            )
        finally:
            # Cleared before notifying, so a listener re-reading is_pending sees the truth.
            self._tasks.pop(node_id, None)
            on_change()
