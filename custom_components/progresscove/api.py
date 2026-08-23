"""Thin async client for the ProgressCove REST API.

Deliberately its own layer: everything Home-Assistant-shaped lives in coordinator.py and todo.py,
and everything ProgressCove-shaped lives here. When the API changes, one file moves.

Auth is a personal access token minted in the web app (Settings → Advanced → API tokens). It is
sent as a bearer token exactly like a session, and the server tells the two apart by the `pat_`
prefix.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Required: the server answers 400 without it.
API_VERSION_HEADER = {"X-API-Version": "1"}

# Attribution on the completion record: a plugin claiming to be a browser would corrupt the only
# signal there is about where work gets done. Anything the server does not know is a 422.
COMPLETION_SURFACE = "integration"


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# "Not given", so that None can mean null. A PATCH only touches the fields it carries, and Python
# cannot otherwise tell `due_at=None` from an omitted argument.
UNSET = object()


class ProgressCoveError(Exception):
    """Any failure talking to the API."""


class ProgressCoveAuthError(ProgressCoveError):
    """The token was rejected. Distinct from ProgressCoveError because Home Assistant treats it
    differently: this one starts a reauth flow rather than just marking the entry unavailable."""


class ProgressCoveNotFound(ProgressCoveError):
    """The server said this node does not exist for us: deleted, or never ours.

    Its own type because it is the ONLY evidence that authorises deleting a user's configured
    entity. A timeout, a 500 or an unreachable server must never reach that branch, and they all
    raise plain ProgressCoveError. Note the server returns 404 for a node owned by someone else
    too (no existence disclosure), so "no longer ours" and "deleted" are deliberately the same
    answer here.
    """


class ProgressCoveRejected(ProgressCoveError):
    """The server refused the write and said why. The message is meant to be read by a person."""


class ProgressCoveClient:
    """One instance per config entry."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        device_id: str | None = None,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._token = token
        self._headers = {**API_VERSION_HEADER, "Authorization": f"Bearer {token}"}
        # Stable across restarts: the server binds a token to the first installation that uses
        # it, so a regenerated id locks this Home Assistant out of its own token.
        if device_id:
            self._headers["X-Device-Id"] = device_id

    async def async_claim_pairing_code(self, code: str) -> None:
        """Tell the server which code this installation is showing its user.

        Sent BEFORE the token can authenticate: it is the one call a dormant token may make. The
        server always answers 204, so this proves nothing about whether the token is real; the
        pairing only completes when the account owner confirms the code in the app.
        """
        await self._request(
            "POST", "/tokens/claim", json={"token": self._token, "pairing_code": code}
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request, with every status the API can answer mapped to an exception.

        401, 403  ProgressCoveAuthError. The token was rejected or the subscription is inactive.
                  Both are the user's to fix and neither is retryable, so Home Assistant opens a
                  reauth flow rather than showing a transient outage.
        404       ProgressCoveNotFound. Means gone, never forbidden: the server answers 404 for a
                  node owned by someone else so that it discloses nothing about what exists.
        422       ProgressCoveRejected, carrying the server's own words about what was wrong.
        4xx, 5xx  ProgressCoveError, via raise_for_status.
        204       None. The write succeeded and there is no body to read.
        2xx       The decoded JSON body.

        A transport failure (timeout, DNS, connection reset) is also ProgressCoveError, so a caller
        never sees a bare aiohttp exception.
        """
        url = f"{self._base}/api/v1{path}"
        try:
            async with self._session.request(
                method, url, headers=self._headers, **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    raise ProgressCoveAuthError(
                        f"{resp.status} from {path}: token rejected or subscription inactive"
                    )
                if resp.status == 404:
                    raise ProgressCoveNotFound(f"not found: {path}")
                if resp.status == 422:
                    raise ProgressCoveRejected(await _rejection_reason(resp))
                resp.raise_for_status()
                if resp.status == 204:
                    return None
                return await resp.json()
        except aiohttp.ClientError as err:
            raise ProgressCoveError(f"{method} {path} failed: {err}") from err

    async def async_check_credentials(self) -> None:
        """Cheapest call that proves the token works. Used by the config flow before an entry is
        created, so a typo'd token fails at setup rather than at the first poll."""
        await self._request("GET", "/nodes", params={"limit": 1})

    @staticmethod
    def _path_safe(node_id: str) -> str:
        """A node id, or a refusal.

        Every id we hold came from the server and is a uuid, so this never fires in normal use. It
        is here because this is the one call whose argument is NOT checked against the live tree
        first: the prune asks about ids precisely when they are absent from it, reading them back
        out of .storage. An id is interpolated into a URL path, so a value carrying `../`, a query
        string or whitespace would address an endpoint we did not mean to call. Validate at the
        boundary rather than trusting where it was stored.
        """
        if not _UUID.fullmatch(node_id):
            raise ProgressCoveError(f"refusing to request a malformed node id: {node_id!r}")
        return node_id

    async def async_node_exists(self, node_id: str) -> bool:
        """Ask the server directly whether one node is still there.

        The bulk tree cannot answer this: a node missing from it might be deleted, or the response
        might have been truncated, empty or malformed. This is a targeted question with three
        outcomes, and only one of them is "gone". A 404 raises ProgressCoveNotFound; anything
        else either returns True or propagates, so an outage can never be read as a deletion.
        """
        await self._request("GET", f"/nodes/{self._path_safe(node_id)}")
        return True

    async def async_get_tree(self) -> list[dict[str, Any]]:
        """Every node the token can see, with reminders attached.

        `include=reminders` is opt-in server-side and batched into one query, so this is one round
        trip for the whole tree rather than one per node.
        """
        payload = await self._request("GET", "/nodes", params={"include": "reminders"})
        return payload.get("nodes", [])

    async def async_get_my_day(self) -> list[dict[str, Any]]:
        """Today, computed server-side.

        Every ProgressCove surface reads this same endpoint, so "today" cannot drift between
        them.
        """
        payload = await self._request("GET", "/widget/myday")
        return payload.get("items", [])

    async def async_complete(self, node_id: str) -> None:
        await self._request(
            "POST", f"/nodes/{node_id}/complete", params={"surface": COMPLETION_SURFACE}
        )

    async def async_uncomplete(self, node_id: str) -> None:
        await self._request("POST", f"/nodes/{node_id}/uncomplete")

    async def async_create_task(
        self, parent_id: str, name: str, due_at: str | None = None, depth: int = 3
    ) -> dict[str, Any]:
        """A new item in a list, one tier below the list itself.

        A list can be a project whose tasks are the items, or a task whose subtasks are the items.
        Both are how people really keep lists, so the depth follows the parent rather than being
        fixed."""
        body: dict[str, Any] = {"name": name, "depth": depth, "parent_id": parent_id}
        if due_at:
            body["due_at"] = due_at
        return await self._request("POST", "/nodes", json=body)

    async def async_update_task(
        self,
        node_id: str,
        *,
        name: str | None = None,
        due_at: str | None | object = UNSET,
    ) -> dict[str, Any]:
        """Patch a task. Only the arguments given here are sent, and only those change.

        Leave `due_at` out to keep the existing due date; pass None to clear it.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if due_at is not UNSET:
            body["due_at"] = due_at
        return await self._request("PATCH", f"/nodes/{node_id}", json=body)

    async def async_delete(self, node_id: str) -> None:
        await self._request("DELETE", f"/nodes/{node_id}")


async def _rejection_reason(resp: aiohttp.ClientResponse) -> str:
    """Turn the server's validation rejection into something worth showing a person.

    The server is the only authority on why a write was refused, so its own words are used rather
    than a guess made here. A body we cannot parse still yields a message rather than silence.
    """
    try:
        detail = (await resp.json()).get("detail")
    except (aiohttp.ClientError, ValueError):
        return "The server rejected that change."
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict) and (msg := first.get("msg")):
            field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "input"
            return f"{field}: {msg}"
    return "The server rejected that change."
