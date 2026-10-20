"""The two polled reads, against a server that tags its answers and one that does not.

A server that sends an ETag is asked If-None-Match next time and may answer 304 with no body, which
means "what you hold is current". The current server sends no tag and always answers in full, and
the integration must keep working against it exactly as before. A 304 the client did not ask for
has nothing behind it to reuse, so it is a failure, never an empty account.
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

import ha_stubs

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"

ha_stubs.fill("aiohttp", ClientError=type("ClientError", (Exception,), {}),
              ClientSession=object, ClientResponse=object)
ha_stubs.fill_shared()

_pkg = sys.modules.get("progresscove") or types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = module
    spec.loader.exec_module(module)
    return module


_load("const")
api = _load("api")

TREE = "/api/v1/nodes"
MY_DAY = "/api/v1/widget/myday"


class _Response:
    def __init__(self, status, body=None, etag=None):
        self.status = status
        self.headers = {"ETag": etag} if etag else {}
        self._body = body

    async def json(self):
        if self.status == 304:
            raise AssertionError("a 304 has no body to read")
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise sys.modules["aiohttp"].ClientError(self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Serves the queued answers per path and records the headers each request carried."""

    def __init__(self, **answers):
        self._answers = {path: list(queue) for path, queue in answers.items()}
        self.sent: list[tuple[str, dict]] = []

    def request(self, method, url, headers=None, **kwargs):
        path = url.removeprefix("https://example.test")
        self.sent.append((path, dict(headers or {})))
        return self._answers[path].pop(0)

    def conditions(self, path):
        return [headers.get("If-None-Match") for sent, headers in self.sent if sent == path]


def _client(session):
    return api.ProgressCoveClient(session, "https://example.test", "pat_x")


def _tree(client):
    return asyncio.run(client.async_get_tree())


def _nodes(*ids):
    return {"nodes": [{"id": node_id} for node_id in ids]}


class ServerWithoutTagsTest(unittest.TestCase):
    """The server in production today: no ETag, a full answer every time."""

    def test_every_poll_is_unconditional_and_read_in_full(self):
        session = _Session(**{TREE: [_Response(200, _nodes("a")), _Response(200, _nodes("b"))]})
        client = _client(session)
        self.assertEqual(_tree(client), [{"id": "a"}])
        self.assertEqual(_tree(client), [{"id": "b"}])
        self.assertEqual(session.conditions(TREE), [None, None])

    def test_an_identical_answer_is_still_a_new_object(self):
        """Identity is how the coordinator tells a 304 apart, so a full answer must never be
        mistaken for one however alike the two bodies are."""
        session = _Session(**{TREE: [_Response(200, _nodes("a")), _Response(200, _nodes("a"))]})
        client = _client(session)
        self.assertIsNot(_tree(client), _tree(client))


class ServerWithTagsTest(unittest.TestCase):
    def test_a_304_reuses_the_last_answer_and_the_tag_is_sent(self):
        session = _Session(**{TREE: [_Response(200, _nodes("a"), etag='"v1"'), _Response(304)]})
        client = _client(session)
        first = _tree(client)
        second = _tree(client)
        self.assertIs(second, first)
        self.assertEqual(session.conditions(TREE), [None, '"v1"'])

    def test_a_changed_answer_replaces_the_cache_and_its_tag(self):
        session = _Session(**{TREE: [
            _Response(200, _nodes("a"), etag='"v1"'),
            _Response(200, _nodes("a", "b"), etag='"v2"'),
            _Response(304),
        ]})
        client = _client(session)
        _tree(client)
        changed = _tree(client)
        self.assertEqual(changed, [{"id": "a"}, {"id": "b"}])
        self.assertIs(_tree(client), changed)
        self.assertEqual(session.conditions(TREE), [None, '"v1"', '"v2"'])

    def test_an_answer_without_a_tag_drops_the_old_one(self):
        """A tag the server no longer stands behind must not be sent again."""
        session = _Session(**{TREE: [
            _Response(200, _nodes("a"), etag='"v1"'),
            _Response(200, _nodes("b")),
            _Response(200, _nodes("c")),
        ]})
        client = _client(session)
        for _ in range(3):
            _tree(client)
        self.assertEqual(session.conditions(TREE), [None, '"v1"', None])

    def test_each_endpoint_keeps_its_own_tag(self):
        session = _Session(**{
            TREE: [_Response(200, _nodes("a"), etag='"tree"'), _Response(304)],
            MY_DAY: [_Response(200, {"items": []}, etag='"day"'), _Response(304)],
        })
        client = _client(session)
        for _ in range(2):
            asyncio.run(client.async_get_tree())
            asyncio.run(client.async_get_my_day())
        self.assertEqual(session.conditions(TREE), [None, '"tree"'])
        self.assertEqual(session.conditions(MY_DAY), [None, '"day"'])

    def test_a_new_client_starts_without_tags(self):
        """Reauth and reload both build a new client: a tag belongs to one token and session."""
        first = _Session(**{TREE: [_Response(200, _nodes("a"), etag='"v1"')]})
        _tree(_client(first))
        second = _Session(**{TREE: [_Response(200, _nodes("a"), etag='"v1"')]})
        _tree(_client(second))
        self.assertEqual(second.conditions(TREE), [None])

    def test_a_failure_keeps_the_cache_for_the_next_poll(self):
        session = _Session(**{TREE: [
            _Response(200, _nodes("a"), etag='"v1"'),
            _Response(500),
            _Response(304),
        ]})
        client = _client(session)
        first = _tree(client)
        with self.assertRaises(api.ProgressCoveError):
            _tree(client)
        self.assertIs(_tree(client), first)
        self.assertEqual(session.conditions(TREE), [None, '"v1"', '"v1"'])


class UnaskedNotModifiedTest(unittest.TestCase):
    def test_a_304_with_nothing_cached_is_a_failure_not_an_empty_tree(self):
        client = _client(_Session(**{TREE: [_Response(304)]}))
        with self.assertRaises(api.ProgressCoveError):
            _tree(client)

    def test_a_304_to_a_plain_request_is_a_failure(self):
        client = _client(_Session(**{TREE: [_Response(304)]}))
        with self.assertRaises(api.ProgressCoveError):
            asyncio.run(client.async_check_credentials())


if __name__ == "__main__":
    unittest.main()
