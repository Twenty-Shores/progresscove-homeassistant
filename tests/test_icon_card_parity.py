"""A switch tile and a button tile must dim on the same rule.

`actionable` decides lit-vs-dimmed and is computed separately in button.py and switch.py, so this
asserts the two agree node-for-node rather than trusting they were written the same way.
"""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"

for _name, _attrs in (
    ("voluptuous", {"Schema": lambda *a, **k: None, "Required": lambda *a, **k: None}),
    ("aiohttp", {"ClientError": type("ClientError", (Exception,), {}),
                 "ClientSession": object, "ClientResponse": object}),
):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        for _k, _v in _attrs.items():
            setattr(_m, _k, _v)
        sys.modules[_name] = _m


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    mod.__path__ = getattr(mod, "__path__", [])
    for key, value in attrs.items():
        if not hasattr(mod, key):
            setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _StateClass:
    MEASUREMENT = "measurement"


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.sensor", SensorEntity=object, SensorStateClass=_StateClass)
_module("homeassistant.components.button", ButtonEntity=object)
_module("homeassistant.components.switch", SwitchEntity=object)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
_module(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=type("CoordinatorEntity", (), {
        "__class_getitem__": classmethod(lambda c, i: c),
        # The real base stores the coordinator; entities here are built through super().__init__.
        "__init__": lambda self, coordinator: setattr(self, "coordinator", coordinator),
    }),
    DataUpdateCoordinator=type(
        "DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)}
    ),
    UpdateFailed=type("UpdateFailed", (Exception,), {}),
)
_module("homeassistant.exceptions",
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))

_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


STATUS_COMPLETED = _load("const").STATUS_COMPLETED
Tree = _load("coordinator").Tree
ProgressCoveTaskButton = _load("button").ProgressCoveTaskButton
ProgressCoveNodeSwitch = _load("switch").ProgressCoveNodeSwitch
helpers = _load("helpers")

ZONE = "Europe/Istanbul"


class _Hass:
    class config:
        time_zone = ZONE


class _Pending:
    def __init__(self):
        self._held = set()

    def is_pending(self, node_id):
        return node_id in self._held


def _at(days_from_today):
    """A due_at as the API sends it: UTC ISO, offset from today in the house timezone."""
    day = datetime.now(ZoneInfo(ZONE)).date() + timedelta(days=days_from_today)
    stamp = datetime(day.year, day.month, day.day, 12, 0, tzinfo=ZoneInfo(ZONE))
    return stamp.astimezone(timezone.utc).isoformat()


def _pair(node, pending=False):
    """The same node as a button and as a switch, sharing one coordinator."""
    held = _Pending()
    if pending:
        held._held.add(node["id"])

    class Coord:
        data = Tree.from_nodes([node])
        pending = held

    coord = Coord()
    button = ProgressCoveTaskButton(coord, "entry", node["id"])
    switch = ProgressCoveNodeSwitch(coord, "entry", node["id"])
    button.hass = switch.hass = _Hass()
    return button, switch


CASES = {
    "due today": {"id": "n", "name": "Bins", "due_at": _at(0)},
    "due tomorrow": {"id": "n", "name": "Bins", "due_at": _at(1)},
    "due next week": {"id": "n", "name": "Bins", "due_at": _at(7)},
    "overdue": {"id": "n", "name": "Bins", "due_at": _at(-3)},
    "no due date": {"id": "n", "name": "Bins", "due_at": None},
    "completed": {"id": "n", "name": "Bins", "due_at": _at(0), "status": STATUS_COMPLETED},
    "repeat due today": {
        "id": "n", "name": "Bins", "due_at": _at(0), "recurrence_rule": "FREQ=WEEKLY",
    },
}


class IconCardParity(unittest.TestCase):
    def test_actionable_matches_button_for_every_case(self):
        for label, node in CASES.items():
            with self.subTest(label):
                button, switch = _pair(dict(node))
                self.assertEqual(
                    switch.extra_state_attributes["actionable"],
                    button.extra_state_attributes["actionable"],
                    f"{label}: switch tile would dim differently from the button tile",
                )

    def test_tile_attributes_match(self):
        """Everything the card reads off a tile, not just the dimming flag."""
        for label, node in CASES.items():
            with self.subTest(label):
                button, switch = _pair(dict(node))
                for key in ("node_id", "emoji", "due_date", "due_today", "days_until"):
                    self.assertEqual(
                        switch.extra_state_attributes.get(key),
                        button.extra_state_attributes.get(key),
                        f"{label}: {key} differs between switch and button",
                    )

    def test_due_today_is_actionable(self):
        """Guards the parity assertions above from passing by agreeing on the wrong answer."""
        _, switch = _pair(dict(CASES["due today"]))
        self.assertTrue(switch.extra_state_attributes["actionable"])
        _, later = _pair(dict(CASES["due next week"]))
        self.assertFalse(later.extra_state_attributes["actionable"])



class UndatedTaskTest(unittest.TestCase):
    """A task with no due date is completable, and never "due".

    These are different questions and were once the same function. An undated task, the most
    ordinary kind, became a button that could never be pressed: the guard asked "is it due today",
    a task with no date answered no, and it answered no forever.
    """

    UNDATED = {"id": "n", "name": "Tidy the shed"}

    def test_an_undated_task_can_be_completed(self):
        _, switch = _pair(dict(self.UNDATED))
        self.assertTrue(helpers.can_complete(switch._node, switch.hass))

    def test_an_undated_task_is_not_reported_as_due(self):
        """The card dims it: there is no day to light up for."""
        _, switch = _pair(dict(self.UNDATED))
        self.assertFalse(helpers.is_due(switch._node, switch.hass))

    def test_a_future_task_still_cannot_be_completed(self):
        node = {"id": "n", "name": "Bins", "due_at": _at(7)}
        _, switch = _pair(node)
        self.assertFalse(helpers.can_complete(switch._node, switch.hass))

    def test_a_task_due_today_or_overdue_can_be_completed(self):
        for label, offset in (("today", 0), ("overdue", -3)):
            with self.subTest(label):
                _, switch = _pair({"id": "n", "name": "Bins", "due_at": _at(offset)})
                self.assertTrue(helpers.can_complete(switch._node, switch.hass))

    def test_a_completed_task_cannot_be_completed_again(self):
        node = {"id": "n", "name": "Bins", "status": STATUS_COMPLETED}
        _, switch = _pair(node)
        self.assertFalse(helpers.can_complete(switch._node, switch.hass))

if __name__ == "__main__":
    unittest.main()
