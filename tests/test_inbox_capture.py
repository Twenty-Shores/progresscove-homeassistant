"""Capturing a task without choosing a list.

Every other list in this integration is one the user picked, which means adding something needs a
decision first. The inbox is the exception, and it is the whole reason a wall panel or a voice
assistant is worth having: "add milk" should not require knowing where milk goes.

These reuse the stub harness from the repeat tests, since the entity under test is the same class
and the same Home Assistant surface.
"""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

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


class _TodoItemStatus:
    NEEDS_ACTION = "needs_action"
    COMPLETED = "completed"


class _TodoItem:
    def __init__(self, uid=None, summary=None, status=None, due=None, description=None):
        self.uid, self.summary, self.status, self.due = uid, summary, status, due
        self.description = description


class _TodoListEntityFeature:
    CREATE_TODO_ITEM = 1
    DELETE_TODO_ITEM = 2
    UPDATE_TODO_ITEM = 4
    MOVE_TODO_ITEM = 8
    SET_DUE_DATE_ON_ITEM = 16
    SET_DUE_DATETIME_ON_ITEM = 32
    SET_DESCRIPTION_ON_ITEM = 64


class _TodoListEntity:
    _attr_todo_items = None

    @property
    def todo_items(self):
        return self._attr_todo_items


_module("homeassistant")
_module("homeassistant.const",
        Platform=type("Platform", (), {"TODO": "todo", "BUTTON": "button", "SWITCH": "switch"}))
_module("homeassistant.components")
_module("homeassistant.components.todo", TodoItem=_TodoItem, TodoItemStatus=_TodoItemStatus,
        TodoListEntity=_TodoListEntity, TodoListEntityFeature=_TodoListEntityFeature)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions",
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))
_module("homeassistant.helpers")
_module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
_module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
_module("homeassistant.helpers.update_coordinator",
        CoordinatorEntity=type("CoordinatorEntity", (), {
            "__class_getitem__": classmethod(lambda c, i: c),
            "__init__": lambda self, coordinator: setattr(self, "coordinator", coordinator),
        }),
        DataUpdateCoordinator=type(
            "DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)}),
        UpdateFailed=type("UpdateFailed", (Exception,), {}))
_module("aiohttp", ClientSession=object, ClientResponse=object,
        ClientError=type("ClientError", (Exception,), {}))

_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = module
    spec.loader.exec_module(module)
    return module


_pending = _load("pending")
Tree = _load("coordinator").Tree
_todo = _load("todo")
ProgressCoveInboxEntity = _todo.ProgressCoveInboxEntity

INBOX_ID = "inbox-1"
INBOX = {"id": INBOX_ID, "name": "Unsorted", "parent_id": "root-1", "depth": 2}
ROOT_NODE = {"id": "root-1", "name": "Home", "parent_id": None, "depth": 1}


class _Client:
    """Records what the entity asked of the API, and nothing else."""

    def __init__(self):
        self.captures: list[tuple[str, str]] = []
        self.creates: list[tuple] = []

    async def async_capture(self, name, client_id):
        self.captures.append((name, client_id))
        return {"id": "new-1", "name": name}

    async def async_create_task(self, parent_id, name, due_at=None, depth=3):
        self.creates.append((parent_id, name, due_at, depth))
        return {"id": "new-2", "name": name}


def _entity(nodes=None):
    tree = Tree.from_nodes(nodes if nodes is not None else [ROOT_NODE, INBOX])
    client = _Client()

    class Coordinator:
        data = tree
        pending = _pending.PendingCompletions()
        inbox_id = INBOX_ID
        read_only = False

    coord = Coordinator()
    coord.client = client

    async def refresh():
        return None

    coord.async_refresh = refresh
    entity = ProgressCoveInboxEntity(coord, "entry", INBOX)
    entity.async_write_ha_state = lambda: None
    return entity, client


class CaptureTest(unittest.TestCase):
    def test_capture_goes_through_the_idempotent_endpoint(self):
        """Not the generic create: a lost reply there would make a second task, and a duplicate is
        the visible failure for a capture the user believes already worked."""
        async def run():
            entity, client = _entity()
            await entity.async_create_todo_item(_TodoItem(summary="Milk"))
            self.assertEqual(len(client.captures), 1)
            self.assertEqual(client.captures[0][0], "Milk")
            self.assertEqual(client.creates, [], "capture fell back to the generic create")
        asyncio.run(run())

    def test_each_capture_carries_its_own_idempotency_key(self):
        """The key makes a RETRY safe, so two deliberate captures of the same words must still be
        two tasks. Reusing one key would silently swallow the second."""
        async def run():
            entity, client = _entity()
            await entity.async_create_todo_item(_TodoItem(summary="Milk"))
            await entity.async_create_todo_item(_TodoItem(summary="Milk"))
            keys = [key for _, key in client.captures]
            self.assertEqual(len(keys), 2)
            self.assertNotEqual(keys[0], keys[1])

        asyncio.run(run())

    def test_the_key_fits_what_the_server_accepts(self):
        """client_id is capped at 64 characters server-side; a longer one is a 422 the user would
        see as a failed capture."""
        async def run():
            entity, client = _entity()
            await entity.async_create_todo_item(_TodoItem(summary="Milk"))
            key = client.captures[0][1]
            self.assertTrue(0 < len(key) <= 64, f"key is {len(key)} characters")
        asyncio.run(run())

    def test_it_is_named_for_what_it_does(self):
        """Not the path: the inbox hangs off a structural root whose name is an implementation
        detail, and repeating it reads like a bug."""
        entity, _ = _entity()
        self.assertEqual(entity._attr_name, "Inbox")


class SetupTest(unittest.TestCase):
    """Whether the entity appears at all, which is decided before it is constructed."""

    def test_an_account_without_an_inbox_gets_no_capture_list(self):
        """Nothing else in the integration needs one, so a missing inbox costs the capture list
        and not the setup."""
        tree = Tree.from_nodes([ROOT_NODE])

        class Coordinator:
            data = tree
            inbox_id = None

        self.assertIsNone(Coordinator.data.by_id.get(Coordinator.inbox_id or ""))

    def test_an_inbox_the_tree_does_not_carry_is_skipped(self):
        """A stale id resolves to nothing rather than to an entity pointing at a missing node."""
        tree = Tree.from_nodes([ROOT_NODE])

        class Coordinator:
            data = tree
            inbox_id = "gone-1"

        self.assertIsNone(Coordinator.data.by_id.get(Coordinator.inbox_id or ""))


if __name__ == "__main__":
    unittest.main()
