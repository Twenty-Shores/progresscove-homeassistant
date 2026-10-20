"""Progress as a number, so "how far along" can go on a graph.

A state trigger compares one value to a fixed one, so a percentage answers directly what otherwise
needed a template condition comparing two counts.
"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest

import ha_stubs

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
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
_module(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=type(
        "CoordinatorEntity", (), {"__class_getitem__": classmethod(lambda c, i: c)}
    ),
    DataUpdateCoordinator=type(
        "DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda c, i: c)}
    ),
    UpdateFailed=ha_stubs.UpdateFailed,
)
_module("homeassistant.exceptions",
        HomeAssistantError=ha_stubs.HomeAssistantError,
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))

ha_stubs.fill_shared()

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
ProgressCoveProgressSensor = _load("sensor").ProgressCoveProgressSensor



def _sensor(children_done):
    """A parent with len(children_done) children, each done or not."""
    nodes = [{"id": "p", "name": "Project", "parent_id": None, "depth": 2}]
    for index, done in enumerate(children_done):
        nodes.append({
            "id": f"c{index}", "name": f"Task {index}", "parent_id": "p", "depth": 3,
            "status": STATUS_COMPLETED if done else None,
        })

    class Coord:
        data = Tree.from_nodes(nodes)

    sensor = ProgressCoveProgressSensor.__new__(ProgressCoveProgressSensor)
    sensor.coordinator = Coord()
    sensor._node_id = "p"
    sensor._attr_name = "Project"
    return sensor


class ProgressSensorTest(unittest.TestCase):
    def test_percent_is_children_done_over_total(self):
        self.assertEqual(_sensor([True, True, True, False]).native_value, 75)

    def test_finished_reads_one_hundred(self):
        """The value an automation triggers on: above 99 must mean genuinely finished."""
        self.assertEqual(_sensor([True, True]).native_value, 100)

    def test_an_empty_node_is_none_not_zero(self):
        """0% would let a "below 50" trigger fire on a project nobody has started, which is not the
        same statement as "barely begun"."""
        self.assertIsNone(_sensor([]).native_value)

    def test_the_counts_are_structured_values_not_sentences(self):
        """An English sentence in an attribute cannot follow the house's language; a template or
        card words the counts itself."""
        attributes = _sensor([True, False, False]).extra_state_attributes
        self.assertEqual((attributes["done"], attributes["total"]), (1, 3))
        self.assertNotIn("summary", attributes)

    def test_the_unit_is_home_assistants_percentage(self):
        self.assertEqual(ProgressCoveProgressSensor._attr_native_unit_of_measurement,
                         sys.modules["homeassistant.const"].PERCENTAGE)

    def test_a_failing_poll_makes_it_unavailable(self):
        ha_stubs.give_coordinator_availability(self, ProgressCoveProgressSensor)
        sensor = _sensor([True, False])
        sensor.coordinator.last_update_success = False
        self.assertFalse(sensor.available)
        sensor.coordinator.last_update_success = True
        self.assertTrue(sensor.available)

    def test_a_node_gone_from_the_account_makes_it_unavailable(self):
        ha_stubs.give_coordinator_availability(self, ProgressCoveProgressSensor)
        sensor = _sensor([True])
        sensor.coordinator.last_update_success = True
        sensor._node_id = "gone"
        self.assertFalse(sensor.available)


class TreePathTest(unittest.TestCase):
    """`path_of` walks parent links, and runs on every poll for every entity."""

    def test_path_reads_from_the_root_down(self):
        tree = Tree.from_nodes([
            {"id": "h", "name": "Home", "parent_id": None},
            {"id": "s", "name": "Shopping", "parent_id": "h"},
            {"id": "g", "name": "Groceries", "parent_id": "s"},
        ])
        self.assertEqual(tree.path_of("g"), "Home › Shopping › Groceries")

    def test_a_looping_parent_chain_terminates(self):
        """Unguarded this appends names until the process dies. The server should never send a
        cycle, which is why nothing else would catch it; the channel is what is untrusted."""
        tree = Tree.from_nodes([
            {"id": "a", "name": "A", "parent_id": "b"},
            {"id": "b", "name": "B", "parent_id": "a"},
        ])
        self.assertEqual(tree.path_of("a"), "B › A")

    def test_an_unknown_id_is_empty_not_an_error(self):
        self.assertEqual(Tree.from_nodes([]).path_of("nope"), "")


if __name__ == "__main__":
    unittest.main()
