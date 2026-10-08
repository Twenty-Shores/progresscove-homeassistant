"""A token that can only read: what the client notices, and what the integration then withholds.

The server is the real enforcement, since it bounces every write a read-only token attempts. These
cover the part this side owns: noticing the scope on every response, never mistaking the refusal
for a dead token, and not offering controls that cannot work.
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


class _CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator


class _IssueSeverity:
    WARNING = "warning"


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.todo", TodoItem=_TodoItem, TodoItemStatus=_TodoItemStatus,
        TodoListEntity=_TodoListEntity, TodoListEntityFeature=_TodoListEntityFeature)
_module("homeassistant.components.button", ButtonEntity=object)
_module("homeassistant.components.switch", SwitchEntity=object)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions",
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
_module("homeassistant.helpers.issue_registry", async_create_issue=None, async_delete_issue=None,
        IssueSeverity=_IssueSeverity)
_updates = _module(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=_CoordinatorEntity,
    DataUpdateCoordinator=type(
        "DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)}),
    UpdateFailed=type("UpdateFailed", (Exception,), {}),
)
if _updates.CoordinatorEntity.__init__ is object.__init__:
    _updates.CoordinatorEntity = _CoordinatorEntity
_module("aiohttp", ClientError=type("ClientError", (Exception,), {}),
        ClientSession=object, ClientResponse=object)

_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


const = _load("const")
api = _load("api")
helpers = _load("helpers")
pending = _load("pending")
read_only_module = _load("read_only")
coordinator_module = _load("coordinator")
todo = _load("todo")
my_day = _load("my_day")
button = _load("button")
switch = _load("switch")

HomeAssistantError = helpers.HomeAssistantError
UpdateFailed = coordinator_module.UpdateFailed
ConfigEntryAuthFailed = coordinator_module.ConfigEntryAuthFailed
Tree = coordinator_module.Tree


class _Response:
    def __init__(self, status, headers=None, body=None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def json(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise sys.modules["aiohttp"].ClientError(self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, *responses):
        self._responses = list(responses)

    def request(self, method, url, headers=None, **kwargs):
        return self._responses.pop(0)


def _client(*responses):
    return api.ProgressCoveClient(_Session(*responses), "https://example.test", "pat_x")


def _get(client):
    return asyncio.run(client.async_check_credentials())


class ScopeHeaderTest(unittest.TestCase):
    def test_nothing_is_known_before_the_first_answer(self):
        self.assertIsNone(_client().token_scope)

    def test_read_write_is_remembered(self):
        client = _client(_Response(200, {const.TOKEN_SCOPE_HEADER: "read_write"}, {}))
        _get(client)
        self.assertEqual(client.token_scope, const.SCOPE_READ_WRITE)

    def test_read_is_remembered(self):
        client = _client(_Response(200, {const.TOKEN_SCOPE_HEADER: "read"}, {}))
        _get(client)
        self.assertEqual(client.token_scope, const.SCOPE_READ)

    def test_a_scope_nobody_has_heard_of_is_treated_as_read_only(self):
        client = _client(_Response(200, {const.TOKEN_SCOPE_HEADER: "admin"}, {}))
        _get(client)
        self.assertEqual(client.token_scope, const.SCOPE_READ)

    def test_a_later_answer_replaces_the_scope(self):
        client = _client(
            _Response(200, {const.TOKEN_SCOPE_HEADER: "read"}, {}),
            _Response(200, {const.TOKEN_SCOPE_HEADER: "read_write"}, {}),
        )
        _get(client)
        _get(client)
        self.assertEqual(client.token_scope, const.SCOPE_READ_WRITE)

    def test_an_answer_without_the_header_leaves_what_was_known(self):
        client = _client(
            _Response(200, {const.TOKEN_SCOPE_HEADER: "read"}, {}),
            _Response(200, {}, {}),
        )
        _get(client)
        _get(client)
        self.assertEqual(client.token_scope, const.SCOPE_READ)


class RefusedWriteTest(unittest.TestCase):
    def _refused(self, headers):
        client = _client(_Response(403, headers))
        with self.assertRaises(api.ProgressCoveError) as caught:
            _get(client)
        return client, caught.exception

    def test_the_read_only_code_is_its_own_error(self):
        _, error = self._refused({const.ERROR_CODE_HEADER: const.ERROR_CODE_READ_ONLY})
        self.assertIsInstance(error, api.ProgressCoveReadOnly)

    def test_it_is_not_an_auth_failure(self):
        """An auth failure opens the reauth flow, which asks for a token the user already has."""
        _, error = self._refused({const.ERROR_CODE_HEADER: const.ERROR_CODE_READ_ONLY})
        self.assertNotIsInstance(error, api.ProgressCoveAuthError)

    def test_it_says_what_to_do(self):
        _, error = self._refused({const.ERROR_CODE_HEADER: const.ERROR_CODE_READ_ONLY})
        self.assertEqual(str(error), const.READ_ONLY_MESSAGE)

    def test_the_refusal_alone_marks_the_token_read_only(self):
        client, _ = self._refused({const.ERROR_CODE_HEADER: const.ERROR_CODE_READ_ONLY})
        self.assertEqual(client.token_scope, const.SCOPE_READ)

    def test_any_other_403_is_still_an_auth_failure(self):
        _, error = self._refused({})
        self.assertIsInstance(error, api.ProgressCoveAuthError)

    def test_another_error_code_on_a_403_is_still_an_auth_failure(self):
        _, error = self._refused({const.ERROR_CODE_HEADER: "subscription_inactive"})
        self.assertIsInstance(error, api.ProgressCoveAuthError)

    def test_a_person_is_shown_the_message_in_full(self):
        with self.assertRaises(HomeAssistantError) as caught:
            with helpers.surfaced("complete that task"):
                raise api.ProgressCoveReadOnly()
        self.assertEqual(str(caught.exception), const.READ_ONLY_MESSAGE)


class _Entry:
    entry_id = "entry-1"

    def __init__(self, **data):
        self.data = dict(data)
        self.subentries = {}


class _Hass:
    def __init__(self):
        class ConfigEntries:
            def async_update_entry(entries, entry, data):
                entry.data = data

        self.config_entries = ConfigEntries()


class _Registry:
    def __init__(self):
        self.created = {}
        self.deleted = []

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created[issue_id] = kwargs

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(issue_id)
        self.created.pop(issue_id, None)

    IssueSeverity = _IssueSeverity


class _Client:
    def __init__(self, scope, error=None):
        self.token_scope = scope
        self.error = error

    async def async_get_tree(self):
        if self.error:
            raise self.error
        return []

    async def async_get_my_day(self):
        return []


def _coordinator(scope, stored=None, error=None):
    registry = _Registry()
    read_only_module.ir = registry
    coord = coordinator_module.ProgressCoveCoordinator.__new__(
        coordinator_module.ProgressCoveCoordinator
    )
    coord.client = _Client(scope, error)
    coord.hass = _Hass()
    coord.config_entry = _Entry(**({const.CONF_TOKEN_SCOPE: stored} if stored else {}))
    coord.read_only = stored == const.SCOPE_READ
    return coord, registry


def _refresh(coord):
    return asyncio.run(coord._async_update_data())


class CoordinatorScopeTest(unittest.TestCase):
    def test_a_read_token_makes_the_entry_read_only_and_raises_the_repair(self):
        coord, registry = _coordinator(const.SCOPE_READ)
        _refresh(coord)
        self.assertTrue(coord.read_only)
        self.assertEqual(
            registry.created["read_only_entry-1"]["translation_key"], "read_only"
        )

    def test_the_scope_is_stored_on_the_entry(self):
        coord, _ = _coordinator(const.SCOPE_READ)
        _refresh(coord)
        self.assertEqual(coord.config_entry.data[const.CONF_TOKEN_SCOPE], const.SCOPE_READ)

    def test_a_read_and_write_token_clears_the_repair(self):
        coord, registry = _coordinator(const.SCOPE_READ_WRITE, stored=const.SCOPE_READ)
        registry.created["read_only_entry-1"] = {}
        _refresh(coord)
        self.assertFalse(coord.read_only)
        self.assertNotIn("read_only_entry-1", registry.created)
        self.assertEqual(
            coord.config_entry.data[const.CONF_TOKEN_SCOPE], const.SCOPE_READ_WRITE
        )

    def test_a_scope_that_did_not_change_leaves_the_entry_alone(self):
        coord, _ = _coordinator(const.SCOPE_READ, stored=const.SCOPE_READ)
        before = coord.config_entry.data
        _refresh(coord)
        self.assertIs(coord.config_entry.data, before)

    def test_a_server_that_reports_no_scope_changes_nothing(self):
        coord, registry = _coordinator(None, stored=const.SCOPE_READ)
        _refresh(coord)
        self.assertTrue(coord.read_only)
        self.assertEqual(registry.created, {})
        self.assertEqual(registry.deleted, [])

    def test_the_stored_scope_is_where_a_new_coordinator_starts(self):
        coord, _ = _coordinator(None, stored=const.SCOPE_READ)
        self.assertTrue(coord.read_only)

    def test_a_refused_write_never_opens_reauthentication(self):
        coord, _ = _coordinator(const.SCOPE_READ, error=api.ProgressCoveReadOnly())
        with self.assertRaises(UpdateFailed):
            _refresh(coord)

    def test_a_dead_token_still_does(self):
        coord, _ = _coordinator(None, error=api.ProgressCoveAuthError("revoked"))
        with self.assertRaises(ConfigEntryAuthFailed):
            _refresh(coord)

    def test_removing_the_entry_removes_the_repair(self):
        registry = _Registry()
        read_only_module.ir = registry
        registry.created["read_only_entry-1"] = {}
        read_only_module.clear_issue(None, _Entry())
        self.assertEqual(registry.created, {})


class _Pending:
    def is_pending(self, node_id):
        return False


def _world(read_only, nodes=None):
    nodes = nodes or [{"id": "list", "name": "Shopping", "depth": 2, "parent_id": None}]

    class Coord:
        data = Tree.from_nodes(nodes)
        pending = _Pending()

    coord = Coord()
    coord.read_only = read_only
    return coord


class EntityFeatureTest(unittest.TestCase):
    def _list(self, read_only):
        coord = _world(read_only)
        return todo.ProgressCoveTodoListEntity(coord, "entry", coord.data.by_id["list"])

    def test_a_list_offers_no_controls_when_read_only(self):
        self.assertEqual(self._list(True)._attr_supported_features, 0)

    def test_a_list_offers_create_update_and_delete_otherwise(self):
        features = self._list(False)._attr_supported_features
        for name in ("CREATE_TODO_ITEM", "UPDATE_TODO_ITEM", "DELETE_TODO_ITEM"):
            with self.subTest(name):
                self.assertTrue(features & getattr(_TodoListEntityFeature, name))

    def test_a_list_tells_a_card_it_is_read_only(self):
        self.assertTrue(self._list(True).extra_state_attributes[const.ATTR_READ_ONLY])
        self.assertFalse(self._list(False).extra_state_attributes[const.ATTR_READ_ONLY])

    def test_the_inbox_follows_the_same_rule(self):
        coord = _world(True)
        inbox = todo.ProgressCoveInboxEntity(coord, "entry", coord.data.by_id["list"])
        self.assertEqual(inbox._attr_supported_features, 0)

    def test_my_day_offers_no_controls_when_read_only(self):
        entity = my_day.ProgressCoveMyDayEntity(_world(True), "e")
        self.assertEqual(entity._attr_supported_features, 0)

    def test_my_day_can_be_ticked_otherwise(self):
        entity = my_day.ProgressCoveMyDayEntity(_world(False), "e")
        self.assertEqual(entity._attr_supported_features, _TodoListEntityFeature.UPDATE_TODO_ITEM)

    def test_my_day_tells_a_card_it_is_read_only(self):
        entity = my_day.ProgressCoveMyDayEntity(_world(True), "e")
        self.assertTrue(entity.extra_state_attributes[const.ATTR_READ_ONLY])


class PlatformSetupTest(unittest.TestCase):
    def _added(self, platform, subentry_type, read_only):
        coord = _world(read_only, [{"id": "task", "name": "Bins", "depth": 3, "parent_id": None}])
        entry = _Entry()
        entry.runtime_data = coord
        entry.subentries = {
            "sub-1": types.SimpleNamespace(subentry_type=subentry_type, data={"node_id": "task"})
        }
        added = []
        asyncio.run(platform.async_setup_entry(
            None, entry, lambda entities, config_subentry_id=None: added.extend(entities)
        ))
        return added

    def test_no_button_is_created_when_read_only(self):
        self.assertEqual(self._added(button, "button", True), [])

    def test_no_switch_is_created_when_read_only(self):
        self.assertEqual(self._added(switch, "switch", True), [])

    def test_a_button_is_created_otherwise(self):
        self.assertEqual(len(self._added(button, "button", False)), 1)

    def test_a_switch_is_created_otherwise(self):
        self.assertEqual(len(self._added(switch, "switch", False)), 1)


class CompleteRefusalTest(unittest.TestCase):
    def test_completing_is_refused_before_anything_is_scheduled(self):
        coord = _world(True)
        coord.pending = pending.PendingCompletions()
        with self.assertRaises(HomeAssistantError) as caught:
            pending.complete(coord, "list", lambda: None)
        self.assertEqual(str(caught.exception), const.READ_ONLY_MESSAGE)
        self.assertFalse(coord.pending.is_pending("list"))


if __name__ == "__main__":
    unittest.main()
