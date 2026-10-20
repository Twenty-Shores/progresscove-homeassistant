"""Who an entity belongs to: its unique id, its device, and where its name comes from.

Every id is derived from the account (its inbox id, which is the config entry's unique id), never
from the config entry's own id, so removing the integration and adding the same account again
brings back the same entities with their history, areas and dashboard references intact.
"""
import asyncio
import enum
import importlib.util
from pathlib import Path
import sys
import types
import unittest

import ha_stubs

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"

ACCOUNT = "0b9d7a52-55a4-4c3e-9d0f-2f3c1c9e6a11"
ENTRY_ID = "01JCONFIGENTRYID0000000000"


class _CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator


class _TodoListEntityFeature(enum.IntFlag):
    CREATE_TODO_ITEM = 1
    DELETE_TODO_ITEM = 2
    UPDATE_TODO_ITEM = 4
    SET_DUE_DATE_ON_ITEM = 16
    SET_DUE_DATETIME_ON_ITEM = 32


class _TodoItemStatus:
    NEEDS_ACTION = "needs_action"
    COMPLETED = "completed"


class _TodoItem:
    def __init__(self, uid=None, summary=None, status=None, due=None, description=None):
        self.uid, self.summary, self.status, self.due = uid, summary, status, due


class _TodoListEntity:
    """Real HA exposes todo_items as a property over _attr_todo_items. Other files load the todo
    platform against whichever stub is registered first, so this one has to be as faithful."""

    _attr_todo_items = None

    @property
    def todo_items(self):
        return self._attr_todo_items


ha_stubs.fill("homeassistant.components.todo", TodoItem=_TodoItem,
              TodoItemStatus=_TodoItemStatus, TodoListEntity=_TodoListEntity,
              TodoListEntityFeature=_TodoListEntityFeature)
ha_stubs.fill("homeassistant.components.button", ButtonEntity=object)
ha_stubs.fill("homeassistant.components.switch", SwitchEntity=object)
ha_stubs.fill("homeassistant.components.sensor", SensorEntity=object,
              SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"))
ha_stubs.fill("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
_updates = ha_stubs.fill(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=_CoordinatorEntity,
    DataUpdateCoordinator=type(
        "DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)}),
    UpdateFailed=ha_stubs.UpdateFailed,
)
if _updates.CoordinatorEntity.__init__ is object.__init__:
    _updates.CoordinatorEntity = _CoordinatorEntity
ha_stubs.fill("homeassistant.exceptions",
              ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))
ha_stubs.fill("aiohttp", ClientError=type("ClientError", (Exception,), {}),
              ClientSession=object, ClientResponse=object)
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


const = _load("const")
coordinator_module = _load("coordinator")
todo = _load("todo")
button = _load("button")
switch = _load("switch")
sensor = _load("sensor")

INBOX = "3f2b8c1e-6a4d-4e9b-8c7f-1d2e3a4b5c6d"
NODES = [
    {"id": "root", "name": "Home"},
    {"id": INBOX, "name": "Unsorted", "parent_id": "root"},
    {"id": "list", "name": "Shopping", "parent_id": "root"},
    # Beside the list rather than in it: an item makes the list read the house's time zone from
    # `hass`, which an entity never added to Home Assistant does not have.
    {"id": "milk", "name": "Milk", "parent_id": "root"},
    {"id": "nameless", "name": "\u200b", "parent_id": "root"},
]


def _entities(node_for_subentries="milk"):
    """Every entity the four platforms create for one account."""
    coord = types.SimpleNamespace(
        data=coordinator_module.Tree.from_nodes(NODES),
        read_only=False,
        inbox_id=INBOX,
        account_id=ACCOUNT,
        pending=types.SimpleNamespace(is_pending=lambda node_id: False),
    )
    entry = types.SimpleNamespace(
        entry_id=ENTRY_ID,
        unique_id=ACCOUNT,
        runtime_data=coord,
        data={},
        subentries={
            "sub-list": types.SimpleNamespace(subentry_type="list", data={"node_id": "list"}),
            **{
                f"sub-{kind}": types.SimpleNamespace(
                    subentry_type=kind, data={"node_id": node_for_subentries}
                )
                for kind in ("button", "switch", "sensor")
            },
        },
    )
    added = []
    for platform in (todo, button, switch, sensor):
        asyncio.run(platform.async_setup_entry(
            None, entry, lambda entities, config_subentry_id=None: added.extend(entities)
        ))
    return added


def _by_class(entities):
    return {type(entity).__name__: entity for entity in entities}


class UniqueIdTest(unittest.TestCase):
    def test_every_platform_contributes(self):
        self.assertEqual(set(_by_class(_entities())), {
            "ProgressCoveTodoListEntity", "ProgressCoveMyDayEntity", "ProgressCoveInboxEntity",
            "ProgressCoveTaskButton", "ProgressCoveNodeSwitch", "ProgressCoveProgressSensor",
        })

    def test_every_unique_id_is_derived_from_the_account(self):
        for entity in _entities():
            with self.subTest(type(entity).__name__):
                self.assertTrue(entity._attr_unique_id.startswith(f"{ACCOUNT}-"),
                                entity._attr_unique_id)
                self.assertNotIn(ENTRY_ID, entity._attr_unique_id)

    def test_unique_ids_do_not_collide(self):
        ids = [entity._attr_unique_id for entity in _entities()]
        self.assertEqual(len(ids), len(set(ids)))


class DeviceTest(unittest.TestCase):
    def test_every_entity_sits_under_the_account_service_device(self):
        for entity in _entities():
            with self.subTest(type(entity).__name__):
                device = entity._attr_device_info
                self.assertEqual(device["identifiers"], {(const.DOMAIN, ACCOUNT)})
                self.assertEqual(device["entry_type"], "service")
                self.assertEqual(device["name"], "ProgressCove")
                self.assertTrue(device["manufacturer"])

    def test_entities_name_themselves_under_the_device(self):
        for entity in _entities():
            with self.subTest(type(entity).__name__):
                self.assertTrue(entity._attr_has_entity_name)


class NameTest(unittest.TestCase):
    def test_my_day_and_the_inbox_are_named_by_translation(self):
        entities = _by_class(_entities())
        for name, key in (("ProgressCoveMyDayEntity", "my_day"),
                          ("ProgressCoveInboxEntity", "inbox")):
            with self.subTest(name):
                self.assertEqual(entities[name]._attr_translation_key, key)
                self.assertFalse(hasattr(entities[name], "_attr_name"),
                                 "a name would override the translation")

    def test_a_list_keeps_its_path_as_its_name(self):
        entity = _by_class(_entities())["ProgressCoveTodoListEntity"]
        self.assertEqual(entity._attr_name, "Home › Shopping")

    def test_a_task_entity_keeps_the_task_name(self):
        for name in ("ProgressCoveTaskButton", "ProgressCoveNodeSwitch",
                     "ProgressCoveProgressSensor"):
            with self.subTest(name):
                self.assertEqual(_by_class(_entities())[name]._attr_name, "Milk")

    def test_a_task_with_nothing_to_show_falls_back_to_the_translated_name(self):
        entities = _by_class(_entities(node_for_subentries="nameless"))
        for name, key in (("ProgressCoveTaskButton", "task"),
                          ("ProgressCoveNodeSwitch", "task"),
                          ("ProgressCoveProgressSensor", "progress")):
            with self.subTest(name):
                self.assertEqual(entities[name]._attr_translation_key, key)
                self.assertFalse(hasattr(entities[name], "_attr_name"))


def _todo_by_subentry(subentries):
    """What the to-do platform adds for `(node, type)` subentries, as (entity, subentry id)."""
    coord = types.SimpleNamespace(
        data=coordinator_module.Tree.from_nodes(NODES), read_only=False, inbox_id=INBOX,
        account_id=ACCOUNT, pending=types.SimpleNamespace(is_pending=lambda node_id: False),
    )
    entry = types.SimpleNamespace(
        runtime_data=coord,
        subentries={
            f"sub-{node_id}": types.SimpleNamespace(subentry_type=kind, data={"node_id": node_id})
            for node_id, kind in subentries
        },
    )
    added = []
    asyncio.run(todo.async_setup_entry(
        None, entry, lambda entities, config_subentry_id=None: added.extend(
            (entity, config_subentry_id) for entity in entities
        ),
    ))
    return added


def _lists(added):
    """`node → subentry id` for the list entities among them."""
    return {
        entity._project_id: subentry_id
        for entity, subentry_id in added
        if type(entity).__name__ == "ProgressCoveTodoListEntity"
    }


class ListSubentryTest(unittest.TestCase):
    """A list is a subentry, and its entity is tied to it, so deleting the row deletes exactly that
    entity. My Day and the inbox belong to the account and are tied to no subentry."""

    def test_each_list_subentry_is_one_entity_tied_to_it(self):
        added = _todo_by_subentry([("list", "list"), ("milk", "list")])
        self.assertEqual(_lists(added), {"list": "sub-list", "milk": "sub-milk"})

    def test_my_day_and_the_inbox_belong_to_the_account(self):
        added = _todo_by_subentry([("list", "list")])
        owners = {type(entity).__name__: subentry_id for entity, subentry_id in added}
        self.assertIsNone(owners["ProgressCoveMyDayEntity"])
        self.assertIsNone(owners["ProgressCoveInboxEntity"])

    def test_other_subentries_make_no_lists(self):
        self.assertEqual(_lists(_todo_by_subentry([("milk", "button")])), {})

    def test_a_removed_list_subentry_leaves_no_entity_after_the_reload(self):
        before = _todo_by_subentry([("list", "list"), ("milk", "list")])
        after = _todo_by_subentry([("list", "list")])
        self.assertIn("milk", _lists(before))
        self.assertEqual(_lists(after), {"list": "sub-list"})

    def test_a_list_whose_node_is_gone_is_left_to_the_prune(self):
        self.assertEqual(_lists(_todo_by_subentry([("deleted-in-the-app", "list")])), {})

    def test_the_unique_id_stays_account_based(self):
        [(entity, _)] = [
            row for row in _todo_by_subentry([("list", "list")])
            if type(row[0]).__name__ == "ProgressCoveTodoListEntity"
        ]
        self.assertEqual(entity._attr_unique_id, f"{ACCOUNT}-list")

if __name__ == "__main__":
    unittest.main()
