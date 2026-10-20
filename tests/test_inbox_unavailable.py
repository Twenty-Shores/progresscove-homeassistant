"""An inbox the server will not hand over is shown to the person, not logged where nobody looks.

The capture list exists only when the inbox resolves at setup. For as long as the server refused
personal access tokens on that route, every install went without one, and the only trace was a
debug line.
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

import ha_stubs

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    mod.__path__ = getattr(mod, "__path__", [])
    for key, value in attrs.items():
        if not hasattr(mod, key):
            setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _IssueSeverity:
    WARNING = "warning"


_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions",
        HomeAssistantError=ha_stubs.HomeAssistantError,
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))
_module("homeassistant.helpers")
_module("homeassistant.helpers.issue_registry", async_create_issue=None, async_delete_issue=None,
        IssueSeverity=_IssueSeverity)
_module("homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=type(
            "DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)}),
        UpdateFailed=ha_stubs.UpdateFailed)
_module("aiohttp", ClientSession=object, ClientResponse=object,
        ClientError=type("ClientError", (Exception,), {}))

ha_stubs.fill_shared()

_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = module
    spec.loader.exec_module(module)
    return module


api = _load("api")
# Before the coordinator, which binds this module's functions when it is imported.
inbox_issue = _load("inbox_issue")
coordinator_module = _load("coordinator")

ISSUE_ID = "inbox_unavailable_entry-1"


class _Entry:
    entry_id = "entry-1"
    data = {}


class _Registry:
    def __init__(self):
        self.created = {}

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created[issue_id] = kwargs

    def async_delete_issue(self, hass, domain, issue_id):
        self.created.pop(issue_id, None)

    IssueSeverity = _IssueSeverity


INBOX_ID = "3f2b8c1e-6a4d-4e9b-8c7f-1d2e3a4b5c6d"


class _Client(api.ProgressCoveClient):
    """The real client with the network replaced, so the inbox id is validated as it is live."""

    def __init__(self, inbox=None, error=None):
        self.inbox = inbox
        self.error = error

    async def _request(self, method, path, **kwargs):
        if self.error:
            raise self.error
        return self.inbox


def _coordinator(client):
    registry = _Registry()
    inbox_issue.ir = registry
    coord = coordinator_module.ProgressCoveCoordinator.__new__(
        coordinator_module.ProgressCoveCoordinator
    )
    coord.client = client
    coord.hass = object()
    coord.config_entry = _Entry()
    coord.inbox_id = None
    return coord, registry


def _resolve(coord):
    asyncio.run(coord.async_resolve_inbox())


class InboxRefusedTest(unittest.TestCase):
    def test_a_refused_token_raises_the_repair(self):
        coord, registry = _coordinator(
            _Client(error=api.ProgressCoveAuthError("401 from /nodes/inbox: token rejected")))
        with self.assertLogs(coordinator_module._LOGGER, level="WARNING"):
            _resolve(coord)
        self.assertIsNone(coord.inbox_id)
        self.assertEqual(registry.created[ISSUE_ID]["translation_key"], "inbox_unavailable")
        self.assertEqual(registry.created[ISSUE_ID]["severity"], _IssueSeverity.WARNING)

    def test_the_repair_says_what_the_server_said(self):
        coord, registry = _coordinator(_Client(error=api.ProgressCoveError("500 from server")))
        with self.assertLogs(coordinator_module._LOGGER, level="WARNING"):
            _resolve(coord)
        self.assertEqual(
            registry.created[ISSUE_ID]["translation_placeholders"], {"reason": "500 from server"}
        )

    def test_a_resolved_inbox_clears_the_repair(self):
        coord, registry = _coordinator(_Client(inbox={"id": INBOX_ID}))
        registry.created[ISSUE_ID] = {}
        _resolve(coord)
        self.assertEqual(coord.inbox_id, INBOX_ID)
        self.assertNotIn(ISSUE_ID, registry.created)

    def test_an_inbox_without_a_valid_id_raises_the_repair(self):
        for inbox in ({"id": "../nodes"}, {"id": 7}, {}, None, []):
            with self.subTest(inbox=inbox):
                coord, registry = _coordinator(_Client(inbox=inbox))
                with self.assertLogs(coordinator_module._LOGGER, level="WARNING"):
                    _resolve(coord)
                self.assertIsNone(coord.inbox_id)
                self.assertIn(ISSUE_ID, registry.created)

    def test_removing_the_entry_removes_the_repair(self):
        registry = _Registry()
        inbox_issue.ir = registry
        registry.created[ISSUE_ID] = {}
        inbox_issue.clear_issue(None, _Entry())
        self.assertEqual(registry.created, {})


if __name__ == "__main__":
    unittest.main()
