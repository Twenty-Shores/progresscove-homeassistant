"""A repeating task must advance exactly one interval per completion, however it is tapped.

Completing a repeat returns it to open on its next occurrence, so a test of "was it already done?"
that only asked whether the status was COMPLETED was permanently False for one: the box sprang back
after every tap, and the next tap rolled the date again.

These run without Home Assistant installed, against fakes, because the value is in the completion
arithmetic rather than in HA's plumbing.
"""
import asyncio
import enum
from pathlib import Path
import sys
import types
import unittest

import ha_stubs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))


notifications: list[tuple] = []


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

    class TodoListEntityFeature(enum.IntFlag):
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
            HomeAssistantError=ha_stubs.HomeAssistantError,
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
            UpdateFailed=ha_stubs.UpdateFailed)
    _module("aiohttp", ClientSession=object, ClientResponse=object,
            ClientError=type("ClientError", (Exception,), {}))


_install_ha_stubs()


def _install_notification_stub() -> None:
    """Recorded rather than rendered: the assertion is on what the user would have been shown."""
    components = sys.modules["homeassistant.components"]
    stub = types.ModuleType("homeassistant.components.persistent_notification")
    stub.async_create = lambda hass, message, title=None, notification_id=None: (
        notifications.append((notification_id, title, message))
    )
    sys.modules[stub.__name__] = stub
    components.persistent_notification = stub


_install_notification_stub()

# Import the modules directly rather than through the package __init__, which pulls in HA's
# config-entry machinery that has nothing to do with completion arithmetic.
import importlib.util  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"progresscove.{name}", ROOT / "custom_components" / "progresscove" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = module
    spec.loader.exec_module(module)
    return module


ha_stubs.fill_shared()

_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(ROOT / "custom_components" / "progresscove")]
sys.modules["progresscove"] = _pkg

# Load `pending` FIRST and leave it in sys.modules: todo.py imports it by name, and a second copy
# would give the test a different class than the entity actually uses.
_pending = _load("pending")
PendingCompletions = _pending.PendingCompletions
Tree = _load("coordinator").Tree
ProgressCoveTodoListEntity = _load("todo").ProgressCoveTodoListEntity
_api = sys.modules["progresscove.api"]
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
        pending = PendingCompletions(None, ha_stubs.BackgroundEntry())
        read_only = False

    coord = Coordinator()
    coord.client = client
    coord.hass = types.SimpleNamespace(config=types.SimpleNamespace(language="en"))

    async def refresh():
        coord.data = Tree.from_nodes([parent, task])
        entity.coordinator = coord
        entity._apply()

    coord.async_refresh = refresh

    entity = ProgressCoveTodoListEntity.__new__(ProgressCoveTodoListEntity)
    entity.hass = types.SimpleNamespace(config=types.SimpleNamespace())
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


class FailedCompletionTest(unittest.TestCase):
    def _fail_with(self, error):
        async def run():
            entity, client, task = _entity()

            async def refuse(node_id):
                raise error

            client.async_complete = refuse
            notifications.clear()
            await entity.async_update_todo_item(_tick(entity, True))
            await asyncio.sleep(0.2)
            return task
        return asyncio.run(run())

    def test_a_network_failure_is_shown_to_the_user(self):
        task = self._fail_with(_api.ProgressCoveError("unreachable"))
        self.assertEqual(len(notifications), 1)
        notification_id, _, message = notifications[0]
        self.assertEqual(notification_id, f"progresscove_complete_failed_{TASK_ID}")
        self.assertIn("Change AC filter", message)
        self.assertIn("not saved", message)
        self.assertEqual(task["due_at"][:10], "2026-08-14")

    def test_a_refused_write_is_shown_to_the_user(self):
        self._fail_with(_api.ProgressCoveRejected("read-only token"))
        self.assertEqual(len(notifications), 1)

    def test_an_unclassified_error_is_shown_to_the_user(self):
        self._fail_with(RuntimeError("boom"))
        self.assertEqual(len(notifications), 1)

    def test_the_notification_is_in_the_houses_language(self):
        """It is the only place the person learns the completion never happened, so it has to be
        readable by them, not only by an English speaker."""
        asked = []

        async def turkish(hass, language, category, integrations):
            asked.append((language, category, set(integrations)))
            return {"component.progresscove.exceptions.complete_not_sent.message":
                    '"{name}" tamamlanamadı.'}

        original = _pending.async_get_translations
        _pending.async_get_translations = turkish
        try:
            async def run():
                entity, client, _ = _entity()
                entity.coordinator.hass.config.language = "tr"

                async def refuse(node_id):
                    raise _api.ProgressCoveError("unreachable")

                client.async_complete = refuse
                notifications.clear()
                await entity.async_update_todo_item(_tick(entity, True))
                await asyncio.sleep(0.2)
            asyncio.run(run())
        finally:
            _pending.async_get_translations = original
        self.assertEqual(asked, [("tr", "exceptions", {"progresscove"})])
        self.assertEqual(notifications[0][2], '"Change AC filter" tamamlanamadı.')

    def test_repeated_failures_reuse_one_notification_id(self):
        self._fail_with(RuntimeError("boom"))
        self._fail_with(RuntimeError("boom"))
        self.assertEqual({n[0] for n in notifications}, {f"progresscove_complete_failed_{TASK_ID}"})

    def test_a_successful_completion_raises_no_notification(self):
        async def run():
            entity, _, _ = _entity()
            notifications.clear()
            await entity.async_update_todo_item(_tick(entity, True))
            await asyncio.sleep(0.2)
        asyncio.run(run())
        self.assertEqual(notifications, [])


class UnloadFlushTest(unittest.TestCase):
    """An entry unloads on every reload, and every subentry change reloads it. A completion held
    in its undo window at that moment is sent there and then, never dropped: the person saw it
    ticked."""

    def _held(self, window=5):
        entity, client, task = _entity(window=window)
        refreshes = []
        real_refresh = entity.coordinator.async_refresh

        async def counted():
            refreshes.append(True)
            await real_refresh()

        entity.coordinator.async_refresh = counted
        notifications.clear()
        return entity, client, task, refreshes

    def test_a_held_completion_is_sent_on_unload(self):
        async def run():
            entity, client, task, refreshes = self._held()
            await entity.async_update_todo_item(_tick(entity, True))
            refreshes.clear()
            await entity.coordinator.pending.async_flush()
            self.assertEqual(client.completions, 1)
            self.assertEqual(task["due_at"][:10], "2026-11-14")
            self.assertFalse(entity.coordinator.pending.is_pending(TASK_ID))
            self.assertEqual(refreshes, [], "an entry being torn down is not refreshed")
            self.assertEqual(notifications, [])
        asyncio.run(run())

    def test_a_failed_send_on_unload_tells_the_person(self):
        async def run():
            entity, client, task, _ = self._held()

            async def refuse(node_id):
                raise _api.ProgressCoveError("unreachable")

            client.async_complete = refuse
            await entity.async_update_todo_item(_tick(entity, True))
            await entity.coordinator.pending.async_flush()
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0][0], f"progresscove_complete_failed_{TASK_ID}")
            self.assertIn("Change AC filter", notifications[0][2])
            self.assertEqual(task["due_at"][:10], "2026-08-14")
        asyncio.run(run())

    def test_unloading_with_nothing_held_sends_nothing(self):
        async def run():
            entity, client, _, refreshes = self._held()
            await entity.coordinator.pending.async_flush()
            self.assertEqual(client.completions, 0)
            self.assertEqual(refreshes, [])
            self.assertEqual(notifications, [])
        asyncio.run(run())

    def test_without_an_unload_the_window_still_holds_and_undoes(self):
        async def run():
            entity, client, _, _ = self._held(window=0.3)
            await entity.async_update_todo_item(_tick(entity, True))
            await asyncio.sleep(0.1)
            self.assertEqual(client.completions, 0)
            self.assertTrue(entity.coordinator.pending.is_pending(TASK_ID))
            await entity.async_update_todo_item(_tick(entity, False))
            await asyncio.sleep(0.4)
            self.assertEqual(client.completions, 0)
        asyncio.run(run())

    def test_a_task_ticked_again_after_an_undo_is_still_flushed(self):
        """The undone timer's cleanup must not forget the new one, or the unload would miss it."""
        async def run():
            entity, client, _, _ = self._held()
            await entity.async_update_todo_item(_tick(entity, True))
            # Started and waiting out its window, so cancelling it runs its cleanup.
            await asyncio.sleep(0.01)
            await entity.async_update_todo_item(_tick(entity, False))
            await entity.async_update_todo_item(_tick(entity, True))
            await asyncio.sleep(0.01)
            self.assertTrue(entity.coordinator.pending.is_pending(TASK_ID))
            await entity.coordinator.pending.async_flush()
            self.assertEqual(client.completions, 1)
        asyncio.run(run())

    def test_held_completions_are_sent_together(self):
        """A slow server costs one wait at unload, not one per held task."""
        async def run():
            pending = PendingCompletions(None, ha_stubs.BackgroundEntry())
            _pending.UNDO_WINDOW_SECONDS = 5
            sent = []

            def slow_send(node_id):
                async def send():
                    await asyncio.sleep(0.2)
                    sent.append(node_id)
                return send

            for node_id in ("a", "b", "c"):
                pending.schedule(node_id, slow_send(node_id), lambda: None)
            loop = asyncio.get_running_loop()
            started = loop.time()
            await pending.async_flush()
            self.assertEqual(sorted(sent), ["a", "b", "c"])
            self.assertLess(loop.time() - started, 0.4)
        asyncio.run(run())

    def test_a_tap_during_the_unload_is_sent_without_a_window(self):
        async def run():
            pending = PendingCompletions(None, ha_stubs.BackgroundEntry())
            _pending.UNDO_WINDOW_SECONDS = 5
            sent = []

            async def send_late():
                sent.append("late")

            async def send_first():
                pending.schedule("late", send_late, lambda: None)

            pending.schedule("first", send_first, lambda: None)
            await asyncio.wait_for(pending.async_flush(), 1)
            self.assertEqual(sent, ["late"])
        asyncio.run(run())


class ShutdownFlushTest(unittest.TestCase):
    """Home Assistant stopping inside the window. Its notifications do not survive the restart, so
    whatever did not go through has to be in the log."""

    def setUp(self):
        self._seconds = _pending.SHUTDOWN_FLUSH_SECONDS
        self.addCleanup(setattr, _pending, "SHUTDOWN_FLUSH_SECONDS", self._seconds)

    def test_a_held_completion_is_sent_when_home_assistant_stops(self):
        async def run():
            entity, client, task = _entity(window=5)
            await entity.async_update_todo_item(_tick(entity, True))
            await entity.coordinator.pending.async_flush_before_shutdown()
            self.assertEqual(client.completions, 1)
            self.assertEqual(task["due_at"][:10], "2026-11-14")
        asyncio.run(run())

    def test_a_failed_send_at_shutdown_is_logged_as_an_error(self):
        async def run():
            entity, client, _ = _entity(window=5)

            async def refuse(node_id):
                raise _api.ProgressCoveError("unreachable")

            client.async_complete = refuse
            await entity.async_update_todo_item(_tick(entity, True))
            with self.assertLogs(_pending._LOGGER, level="ERROR") as logged:
                await entity.coordinator.pending.async_flush_before_shutdown()
            self.assertIn(TASK_ID, logged.output[0])
        asyncio.run(run())

    def test_an_unexpected_failure_at_shutdown_is_logged_with_its_traceback(self):
        async def run():
            entity, client, _ = _entity(window=5)

            async def broken(node_id):
                raise RuntimeError("boom")

            client.async_complete = broken
            await entity.async_update_todo_item(_tick(entity, True))
            with self.assertLogs(_pending._LOGGER, level="ERROR") as logged:
                await entity.coordinator.pending.async_flush_before_shutdown()
            self.assertIn("RuntimeError: boom", "\n".join(logged.output))
        asyncio.run(run())

    def test_one_not_confirmed_before_the_deadline_is_logged(self):
        async def run():
            _pending.SHUTDOWN_FLUSH_SECONDS = 0.05
            entity, client, _ = _entity(window=5)

            async def hang(node_id):
                await asyncio.sleep(5)

            client.async_complete = hang
            await entity.async_update_todo_item(_tick(entity, True))
            with self.assertLogs(_pending._LOGGER, level="ERROR") as logged:
                await entity.coordinator.pending.async_flush_before_shutdown()
            self.assertIn("may not have been saved", logged.output[0])
            self.assertIn(TASK_ID, logged.output[0])
            entity.coordinator.pending.undo(TASK_ID)
        asyncio.run(run())

    def test_the_deadline_beats_home_assistants_own(self):
        """Past 20 seconds Home Assistant cancels the tasks itself, and nothing says which."""
        self.assertLess(_pending.SHUTDOWN_FLUSH_SECONDS, 20)


if __name__ == "__main__":
    unittest.main()
