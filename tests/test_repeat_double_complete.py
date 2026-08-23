"""A repeating task must advance exactly one interval per completion, however it is tapped.

Completing a repeat returns it to open on its next occurrence, so a test of "was it already done?"
that only asked whether the status was COMPLETED was permanently False for one: the box sprang back
after every tap, and the next tap rolled the date again.

These run without Home Assistant installed, against fakes, because the value is in the completion
arithmetic rather than in HA's plumbing.
"""
import asyncio
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))


def _install_ha_stubs() -> None:
    """Minimal stand-ins for the Home Assistant modules the entity imports.

    Guarded on the submodule this file actually needs, not on `homeassistant`: another test file
    stubbing the top-level package would otherwise turn this into a no-op and leave todo.py with
    nothing to import from.
    """
    if "homeassistant.components.todo" in sys.modules:
        return

    class TodoItemStatus:
        NEEDS_ACTION = "needs_action"
        COMPLETED = "completed"

    class TodoItem:
        def __init__(self, uid=None, summary=None, status=None, due=None, description=None):
            self.uid, self.summary, self.status, self.due = uid, summary, status, due
            self.description = description

    class TodoListEntityFeature:
        CREATE_TODO_ITEM = 1
        DELETE_TODO_ITEM = 2
        UPDATE_TODO_ITEM = 4
        MOVE_TODO_ITEM = 8
        SET_DUE_DATE_ON_ITEM = 16
        SET_DUE_DATETIME_ON_ITEM = 32
        SET_DESCRIPTION_ON_ITEM = 64

    def _module(name, **attrs):
        mod = types.ModuleType(name)
        # __path__ makes it a package, so `homeassistant.const` resolves as a submodule.
        mod.__path__ = []
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    class Platform:
        TODO = "todo"
        BUTTON = "button"
        SWITCH = "switch"

    _module("homeassistant")
    _module("homeassistant.const", Platform=Platform)
    _module("homeassistant.components")
    _module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
    class TodoListEntity:
        """Real HA exposes todo_items as a property over _attr_todo_items."""
        _attr_todo_items = None

        @property
        def todo_items(self):
            return self._attr_todo_items

    _module("homeassistant.components.todo", TodoItem=TodoItem, TodoItemStatus=TodoItemStatus,
            TodoListEntity=TodoListEntity, TodoListEntityFeature=TodoListEntityFeature)
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _module("homeassistant.exceptions",
            HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
            ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
    _module("homeassistant.helpers.update_coordinator",
            CoordinatorEntity=type(
                "CoordinatorEntity", (), {"__class_getitem__": classmethod(lambda c, i: c)}
            ),
            DataUpdateCoordinator=type(
                "DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)}
            ),
            UpdateFailed=type("UpdateFailed", (Exception,), {}))
    _module("aiohttp", ClientSession=object, ClientResponse=object,
            ClientError=type("ClientError", (Exception,), {}))


_install_ha_stubs()

# Import the modules directly rather than through the package __init__, which pulls in HA's
# config-entry machinery that has nothing to do with completion arithmetic.
import importlib.util                                          # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"progresscove.{name}", ROOT / "custom_components" / "progresscove" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = module
    spec.loader.exec_module(module)
    return module


_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(ROOT / "custom_components" / "progresscove")]
sys.modules["progresscove"] = _pkg

# Load `pending` FIRST and leave it in sys.modules: todo.py imports it by name, and a second copy
# would give the test a different class than the entity actually uses.
_pending = _load("pending")
PendingCompletions = _pending.PendingCompletions
Tree = _load("coordinator").Tree
ProgressCoveTodoListEntity = _load("todo").ProgressCoveTodoListEntity
from homeassistant.components.todo import TodoItem, TodoItemStatus  # noqa: E402

QUARTERLY = "RRULE:FREQ=MONTHLY;INTERVAL=3"
PARENT_ID = "parent-1"
TASK_ID = "task-1"


class FakeClient:
    """Rolls the due date forward on complete, exactly as the server does."""

    def __init__(self, node):
        self.node = node
        self.completions = 0
        self.uncompletes = 0

    async def async_complete(self, node_id):
        self.completions += 1
        month = int(self.node["due_at"][5:7]) + 3
        year = int(self.node["due_at"][:4]) + (month - 1) // 12
        self.node["due_at"] = f"{year}-{(month - 1) % 12 + 1:02d}-14T00:00:00+00:00"
        self.node["status"] = 0            # Back to TODO: a rolled repeat is never left COMPLETED

    async def async_uncomplete(self, node_id):
        self.uncompletes += 1


def _entity(window=0.05):
    _pending.UNDO_WINDOW_SECONDS = window

    task = {"id": TASK_ID, "name": "Change AC filter", "parent_id": PARENT_ID, "depth": 3,
            "status": 0, "due_at": "2026-08-14T00:00:00+00:00", "recurrence_rule": QUARTERLY}
    parent = {"id": PARENT_ID, "name": "Repeating home tasks", "parent_id": None, "depth": 2}
    tree = Tree.from_nodes([parent, task])
    client = FakeClient(task)

    class Coordinator:
        data = tree
        pending = PendingCompletions()

    coord = Coordinator()
    coord.client = client

    async def refresh():
        coord.data = Tree.from_nodes([parent, task])
        entity.coordinator = coord
        entity._apply()

    coord.async_refresh = refresh

    entity = ProgressCoveTodoListEntity.__new__(ProgressCoveTodoListEntity)
    entity.coordinator = coord
    entity._project_id = PARENT_ID
    entity._attr_name = parent["name"]
    entity.async_write_ha_state = lambda: None
    entity._apply()
    return entity, client, task


def _tick(entity, done):
    """What HA sends: the FULL item, carrying the due it was shown."""
    shown = next(i for i in entity.todo_items if i.uid == TASK_ID)
    return TodoItem(uid=TASK_ID, summary=shown.summary, due=shown.due,
                    status=TodoItemStatus.COMPLETED if done else TodoItemStatus.NEEDS_ACTION)


def _box(entity):
    return next(i for i in entity.todo_items if i.uid == TASK_ID).status


class RepeatCompletionTest(unittest.TestCase):
    def test_the_box_stays_ticked_while_the_undo_window_runs(self):
        async def run():
            entity, _, _ = _entity()
            await entity.async_update_todo_item(_tick(entity, True))
            # It springing back to unticked is what invited the second tap that rolled it twice.
            self.assertEqual(_box(entity), TodoItemStatus.COMPLETED)
        asyncio.run(run())

    def test_tapping_twice_completes_once(self):
        async def run():
            entity, client, task = _entity()
            await entity.async_update_todo_item(_tick(entity, True))
            await entity.async_update_todo_item(_tick(entity, True))
            await asyncio.sleep(0.2)
            self.assertEqual(client.completions, 1)
            self.assertEqual(task["due_at"][:10], "2026-11-14")
        asyncio.run(run())

    def test_untick_inside_the_window_never_reaches_the_server(self):
        async def run():
            entity, client, task = _entity()
            await entity.async_update_todo_item(_tick(entity, True))
            await entity.async_update_todo_item(_tick(entity, False))
            await asyncio.sleep(0.2)
            self.assertEqual(client.completions, 0)
            self.assertEqual(client.uncompletes, 0)
            self.assertEqual(task["due_at"][:10], "2026-08-14")   # untouched
        asyncio.run(run())

    def test_a_real_completion_advances_exactly_one_interval(self):
        async def run():
            entity, client, task = _entity()
            await entity.async_update_todo_item(_tick(entity, True))
            await asyncio.sleep(0.2)
            self.assertEqual(task["due_at"][:10], "2026-11-14")
            self.assertEqual(client.completions, 1)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
