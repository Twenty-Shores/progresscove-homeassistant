"""The ProgressCove server, stood in for, and the account it serves."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigSubentryData

from custom_components.progresscove.api import ProgressCoveError, ProgressCoveNotFound

ACCOUNT = "0b9d7a52-55a4-4c3e-9d0f-2f3c1c9e6a11"
SERVER = "https://api.progresscove.com"

TREE = [
    {"id": "root", "name": "Home", "depth": 1},
    {"id": ACCOUNT, "name": "Unsorted", "parent_id": "root", "depth": 2},
    {"id": "captured", "name": "Call the plumber", "parent_id": ACCOUNT, "depth": 3},
    {"id": "shopping", "name": "Shopping", "parent_id": "root", "depth": 2},
    {"id": "groceries", "name": "Groceries", "parent_id": "shopping", "depth": 3},
    {"id": "milk", "name": "Milk", "parent_id": "groceries", "depth": 4, "status": 0,
     "recurrence_rule": "RRULE:FREQ=WEEKLY"},
    {"id": "garden", "name": "Garden", "parent_id": "root", "depth": 2},
    {"id": "chores", "name": "Chores", "parent_id": "root", "depth": 2, "status": 0,
     "recurrence_rule": "RRULE:FREQ=DAILY", "icon": "🧹"},
]

MY_DAY = [
    {"id": "chores", "name": "Chores", "section": "due", "timeUtc": "2026-10-19T07:00:00+00:00"},
    {"id": "garden", "name": "Garden", "section": "ongoing"},
]


@dataclass
class Server:
    """What the stand-in server holds and has been asked."""

    nodes: list[dict[str, Any]] = field(default_factory=lambda: [dict(n) for n in TREE])
    my_day: list[dict[str, Any]] = field(default_factory=lambda: [dict(n) for n in MY_DAY])
    deleted: set[str] = field(default_factory=set)
    completed: list[str] = field(default_factory=list)
    refuse_completion: bool = False
    tokens_seen: list[str] = field(default_factory=list)

    def client(self, session: Any, base_url: str, token: str, device_id: str | None = None):
        self.tokens_seen.append(token)
        return _Client(self)


class _Client:
    def __init__(self, server: Server) -> None:
        self._server = server
        self.token_scope = "read_write"

    async def async_check_credentials(self) -> None:
        return None

    async def async_get_tree(self) -> list[dict[str, Any]]:
        return [dict(node) for node in self._server.nodes]

    async def async_get_my_day(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._server.my_day]

    async def async_get_inbox_id(self) -> str:
        return ACCOUNT

    async def async_complete(self, node_id: str) -> None:
        if self._server.refuse_completion:
            raise ProgressCoveError("unreachable")
        self._server.completed.append(node_id)

    async def async_node_exists(self, node_id: str) -> bool:
        if node_id in self._server.deleted:
            raise ProgressCoveNotFound(node_id)
        return True

    async def async_claim_pairing_code(self, code: str) -> None:
        return None


def list_subentry(node_id: str, title: str) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"node_id": node_id}, subentry_type="list", title=title,
        unique_id=f"list_{node_id}",
    )
