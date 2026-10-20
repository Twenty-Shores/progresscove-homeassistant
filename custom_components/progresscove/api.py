"""Thin async client for the ProgressCove REST API.

Deliberately its own layer: everything Home-Assistant-shaped lives in coordinator.py and todo.py,
and everything ProgressCove-shaped lives here. When the API changes, one file moves.

Auth is a personal access token minted in the web app (Settings → Advanced → API tokens). It is
sent as a bearer token exactly like a session, and the server tells the two apart by the `pat_`
prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, NamedTuple

import aiohttp

from .const import (
    ERROR_CODE_HEADER,
    ERROR_CODE_READ_ONLY,
    ERROR_CODE_SUBSCRIPTION_INACTIVE,
    READ_ONLY_MESSAGE,
    SCOPE_READ,
    SCOPE_READ_WRITE,
    TOKEN_SCOPE_HEADER,
)

# Required: the server answers 400 without it.
API_VERSION_HEADER = {"X-API-Version": "1"}

# Long enough for a slow server to answer the whole tree, short enough that a hung connection is
# reported as a failure rather than holding a poll, a setup or a config flow open indefinitely.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

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


class ProgressCoveSubscriptionInactive(ProgressCoveError):
    """The account's subscription is not active, so the server refuses to read or write anything.

    Not an auth failure: the token is fine, and a new one would be refused the same way, so a
    reauth flow could never succeed. The server also answers this while it cannot confirm a
    subscription at all, which is one more reason it is retried rather than escalated.
    """


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


class ProgressCoveReadOnly(ProgressCoveRejected):
    """A write was refused because the token can only read.

    A rejection rather than an auth failure: the token is valid and the owner chose its scope, so
    asking for a new token through reauth would be the wrong remedy.
    """

    def __init__(self) -> None:
        super().__init__(READ_ONLY_MESSAGE)


class _Answer(NamedTuple):
    status: int
    etag: str | None
    body: Any


@dataclass(frozen=True)
class _Cached:
    """The last full answer from an endpoint that tags its answers, and the tag it came with."""

    etag: str
    items: list[dict[str, Any]]


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
        # None until the server has answered once, and again for a server that sends no scope.
        self.token_scope: str | None = None
        # Stable across restarts: the server binds a token to the first installation that uses
        # it, so a regenerated id locks this Home Assistant out of its own token.
        if device_id:
            self._headers["X-Device-Id"] = device_id
        # Per path. A tag is only meaningful to the token and session it was issued to, so it
        # lives exactly as long as this client: a reauth or a reload builds a new one, empty.
        self._cached: dict[str, _Cached] = {}

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
        """The decoded body of one unconditional request. See `_send` for what each status
        becomes."""
        return (await self._send(method, path, **kwargs)).body

    async def _send(
        self, method: str, path: str, if_none_match: str | None = None, **kwargs: Any
    ) -> _Answer:
        """One request, with every status the API can answer mapped to an exception.

        403 with X-Error-Code pat_read_only
                  ProgressCoveReadOnly. The token is fine; it cannot write.
        403 with code subscription_required in the body
                  ProgressCoveSubscriptionInactive. The token is fine; the account is not entitled.
        401, 403  ProgressCoveAuthError. The token was rejected, which a new token fixes, so Home
                  Assistant opens a reauth flow rather than showing a transient outage.
        404       ProgressCoveNotFound. Means gone, never forbidden: the server answers 404 for a
                  node owned by someone else so that it discloses nothing about what exists.
        422       ProgressCoveRejected, carrying the server's own words about what was wrong.
        4xx, 5xx  ProgressCoveError, via raise_for_status.
        204       No body. The write succeeded and there is nothing to read.
        304       No body, and only when `if_none_match` was sent: what we hold is still
                  current. Unasked for, it is a protocol error, because there is nothing to reuse.
        2xx       The decoded JSON body, with the response's ETag if it had one.

        A transport failure (timeout, DNS, connection reset) is also ProgressCoveError, and so is a
        2xx whose body is not JSON, so a caller never sees a bare aiohttp, timeout or decode
        exception.
        """
        url = f"{self._base}/api/v1{path}"
        headers = self._headers
        if if_none_match is not None:
            headers = {**headers, "If-None-Match": if_none_match}
        try:
            async with self._session.request(
                method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
            ) as resp:
                self._note_scope(resp)
                await self._raise_if_refused(resp, path)
                if resp.status == 304:
                    if if_none_match is None:
                        raise ProgressCoveError(
                            f"{method} {path} answered 304 Not Modified to a request that was "
                            "not conditional"
                        )
                    return _Answer(resp.status, if_none_match, None)
                resp.raise_for_status()
                etag = resp.headers.get("ETag")
                if resp.status == 204:
                    return _Answer(resp.status, etag, None)
                return _Answer(resp.status, etag, await resp.json())
        except TimeoutError as err:
            raise ProgressCoveError(f"{method} {path} timed out") from err
        except (aiohttp.ClientError, ValueError) as err:
            raise ProgressCoveError(f"{method} {path} failed: {err}") from err

    async def _raise_if_refused(self, resp: aiohttp.ClientResponse, path: str) -> None:
        """The refusals that have their own exception, as `_send` lists them."""
        if resp.status == 403 and resp.headers.get(ERROR_CODE_HEADER) == ERROR_CODE_READ_ONLY:
            self.token_scope = SCOPE_READ
            raise ProgressCoveReadOnly()
        if resp.status == 403 and await _error_code(resp) == ERROR_CODE_SUBSCRIPTION_INACTIVE:
            raise ProgressCoveSubscriptionInactive(f"403 from {path}: subscription not active")
        if resp.status in (401, 403):
            raise ProgressCoveAuthError(f"{resp.status} from {path}: token rejected")
        if resp.status == 404:
            raise ProgressCoveNotFound(f"not found: {path}")
        if resp.status == 422:
            raise ProgressCoveRejected(await _rejection_reason(resp))

    def _note_scope(self, resp: aiohttp.ClientResponse) -> None:
        """Remember the scope the server reports for this token.

        Anything other than the one value that grants writes is treated as read-only: a scope this
        code has never heard of must not unlock features it cannot vouch for.
        """
        reported = resp.headers.get(TOKEN_SCOPE_HEADER)
        if reported is None:
            return
        self.token_scope = SCOPE_READ_WRITE if reported == SCOPE_READ_WRITE else SCOPE_READ

    async def async_check_credentials(self) -> None:
        """Cheapest call that proves the token works. Used by the config flow before an entry is
        created, so a typo'd token fails at setup rather than at the first poll."""
        await self._request("GET", "/nodes", params={"limit": 1})

    @staticmethod
    def _path_safe(node_id: str) -> str:
        """A node id, or a refusal.

        Every id we hold came from the server and is a uuid, so this never fires in normal use. But
        ids also arrive from service calls and from .storage, and the prune asks about ids
        precisely when they are absent from the live tree. An id is interpolated into a URL path,
        so a value carrying `../`, a query string or whitespace would address an endpoint we did
        not mean to call. Every path built from an id goes through here, rather than trusting
        where the id came from.
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

    async def _get_items(
        self, path: str, key: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """One list from a GET the server may answer with 304 Not Modified.

        Unchanged, it is the very list object returned last time, unparsed and unrebuilt, which is
        how a caller knows nothing moved. A server that sends no ETag is asked unconditionally
        every time and answers in full, as one that has never heard of conditional requests does.
        """
        cached = self._cached.get(path)
        answer = await self._send(
            "GET", path, if_none_match=cached.etag if cached else None, params=params
        )
        if answer.status == 304 and cached is not None:
            return cached.items
        items = answer.body.get(key, [])
        if answer.etag:
            self._cached[path] = _Cached(answer.etag, items)
        else:
            self._cached.pop(path, None)
        return items

    async def async_get_tree(self) -> list[dict[str, Any]]:
        """Every node the token can see, with reminders attached.

        `include=reminders` is opt-in server-side and batched into one query, so this is one round
        trip for the whole tree rather than one per node. Conditional: see `_get_items`.
        """
        return await self._get_items("/nodes", "nodes", params={"include": "reminders"})

    async def async_get_my_day(self) -> list[dict[str, Any]]:
        """Today, computed server-side.

        Every ProgressCove surface reads this same endpoint, so "today" cannot drift between
        them. Conditional: see `_get_items`.
        """
        return await self._get_items("/widget/myday", "items")

    async def async_get_inbox_id(self) -> str:
        """The id of the account's inbox node, which also identifies the account.

        Every account has exactly one inbox, assigned when the account is made and never deleted,
        so its id is the one value that names the account and nothing else. It becomes the config
        entry's unique id and the prefix of every entity's, so anything but a uuid is refused here
        rather than stored.
        """
        inbox = await self._request("GET", "/nodes/inbox")
        inbox_id = inbox.get("id") if isinstance(inbox, dict) else None
        if not isinstance(inbox_id, str) or not _UUID.fullmatch(inbox_id):
            raise ProgressCoveError(
                f"the server returned an inbox without a valid id: {inbox_id!r}"
            )
        return inbox_id

    async def async_capture(self, name: str, client_id: str) -> dict[str, Any]:
        """Add one task to the inbox, without naming a list. The text is the task's name.

        Idempotent on `client_id`: a retry after a timeout returns the task the first attempt
        made rather than a second one. That matters more here than anywhere else in this client,
        because a capture the user believes succeeded has no other surface to reappear on.
        """
        return await self._request(
            "POST", "/widget/tasks", json={"name": name, "client_id": client_id}
        )

    async def async_complete(self, node_id: str) -> None:
        await self._request(
            "POST",
            f"/nodes/{self._path_safe(node_id)}/complete",
            params={"surface": COMPLETION_SURFACE},
        )

    async def async_uncomplete(self, node_id: str) -> None:
        await self._request("POST", f"/nodes/{self._path_safe(node_id)}/uncomplete")

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
        return await self._request("PATCH", f"/nodes/{self._path_safe(node_id)}", json=body)

    async def async_delete(self, node_id: str) -> None:
        await self._request("DELETE", f"/nodes/{self._path_safe(node_id)}")


async def _error_code(resp: aiohttp.ClientResponse) -> str | None:
    """The `code` of the server's error body, or None for a body that has none or is not JSON."""
    try:
        body = await resp.json()
    except (aiohttp.ClientError, ValueError):
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


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
