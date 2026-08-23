"""Completing a task at ANY depth, including one no built-in service can reach.

`todo.update_item` resolves its item against the entity's own items, so anything nested is
unreachable. These services take a node id instead.
"""
import asyncio
import re
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"


def _stub_homeassistant() -> None:
    def module(name, **attrs):
        # Reuse a module another test already stubbed, but ADD what it lacks: the other module
        # stubs homeassistant.core without ServiceCall, and returning it untouched left this one
        # importing a name that was never defined.
        mod = sys.modules.get(name) or types.ModuleType(name)
        mod.__path__ = getattr(mod, "__path__", [])
        for key, value in attrs.items():
            if not hasattr(mod, key):
                setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    module("homeassistant")
    module(
        "homeassistant.core",
        HomeAssistant=object,
        ServiceCall=object,
        ServiceResponse=dict,
        SupportsResponse=type("SupportsResponse", (), {"ONLY": "only"}),
        callback=lambda f: f,
    )
    module(
        "homeassistant.exceptions",
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
        ServiceValidationError=type("ServiceValidationError", (Exception,), {}),
    )
    module("homeassistant.helpers")
    module("homeassistant.helpers.config_validation", string=str)
    module("aiohttp", ClientError=type("ClientError", (Exception,), {}),
           ClientSession=object, ClientResponse=object)
    module("voluptuous", Schema=lambda *a, **k: None, Required=lambda *a, **k: None)

# Stubbed unconditionally, before the HA stubs: another test module in the same run may already
# have populated sys.modules["homeassistant"], and a guard on that name would skip these too.
for _name, _attrs in (
    ("voluptuous", {"Schema": lambda *a, **k: None, "Required": lambda *a, **k: None}),
    ("aiohttp", {"ClientError": type("ClientError", (Exception,), {}),
                 "ClientSession": object, "ClientResponse": object}),
):
    if _name not in sys.modules:
        _module = types.ModuleType(_name)
        for _key, _value in _attrs.items():
            setattr(_module, _key, _value)
        sys.modules[_name] = _module

_stub_homeassistant()


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg

pending = _load("pending")
STATUS_COMPLETED = _load("const").STATUS_COMPLETED
_load("api")
_load("helpers")
services = _load("services")



class FakeClient:
    def __init__(self, nodes):
        self.nodes = nodes
        self.completed, self.reopened = [], []

    async def async_complete(self, node_id):
        self.completed.append(node_id)
        self.nodes[node_id]["status"] = STATUS_COMPLETED

    async def async_uncomplete(self, node_id):
        self.reopened.append(node_id)
        self.nodes[node_id]["status"] = None


def _world(status=None, rule=None, window=0.05):
    pending.UNDO_WINDOW_SECONDS = window
    nodes = {"milk": {"id": "milk", "name": "Milk", "status": status, "recurrence_rule": rule}}

    class Data:
        by_id = nodes

    class Coord:
        data = Data()

    coord = Coord()
    coord.pending = pending.PendingCompletions()
    coord.client = FakeClient(nodes)
    # The real DataUpdateCoordinator has this; it is what repaints every entity at once. Counted
    # here because forgetting to call it is invisible in a unit test and shows up as a checkbox
    # that takes a poll cycle to move.
    coord.repaints = []
    coord.async_update_listeners = lambda: coord.repaints.append(1)

    async def refresh():
        return None

    coord.async_refresh = refresh

    class Entry:
        runtime_data = coord

    class Entries:
        def async_entries(self, domain):
            return [Entry()]

    class Hass:
        config_entries = Entries()

    class Call:
        hass = Hass()
        data = {services.ATTR_NODE_ID: "milk"}

    return coord, Call(), nodes


class ServiceTest(unittest.TestCase):
    def test_completing_a_subtask_reaches_the_server(self):
        async def run():
            coord, call, nodes = _world()
            await services._complete(call)
            await asyncio.sleep(0.2)
            self.assertEqual(coord.client.completed, ["milk"])
            self.assertEqual(nodes["milk"]["status"], STATUS_COMPLETED)
        asyncio.run(run())

    def test_a_plain_task_is_sent_at_once(self):
        """No window for a task that reopens fine: holding it just made the box look stuck."""
        async def run():
            coord, call, nodes = _world(window=5)   # a window long enough to be obvious if used
            await services._complete(call)
            await asyncio.sleep(0.05)
            self.assertEqual(coord.client.completed, ["milk"])
        asyncio.run(run())

    def test_a_repeat_is_held_for_the_window(self):
        """The one case the window protects: completing a repeat rolls its date irreversibly."""
        async def run():
            coord, call, _ = _world(rule="RRULE:FREQ=WEEKLY", window=5)
            await services._complete(call)
            await asyncio.sleep(0.05)
            self.assertEqual(coord.client.completed, [], "a repeat must not be sent immediately")
            self.assertTrue(coord.pending.is_pending("milk"))
        asyncio.run(run())

    def test_the_tick_appears_immediately_not_after_the_window(self):
        """The card must repaint on the tap, not when the undo window closes. Without this the box
        sat unchanged for ten seconds (or until the next 60s poll), which reads as broken."""
        async def run():
            coord, call, _ = _world(window=5)      # long window: nothing else can repaint in time
            await services._complete(call)
            self.assertTrue(coord.repaints, "no repaint was pushed to the entities")
        asyncio.run(run())

    def test_reopen_inside_the_window_never_sends(self):
        async def run():
            coord, call, nodes = _world()
            await services._complete(call)
            await services._reopen(call)
            await asyncio.sleep(0.2)
            self.assertEqual(coord.client.completed, [])
            self.assertIsNone(nodes["milk"]["status"])
        asyncio.run(run())

    def test_completing_twice_completes_once(self):
        """Two calls inside the window are one completion, deduped by node id."""
        async def run():
            coord, call, _ = _world()
            await services._complete(call)
            await services._complete(call)
            await asyncio.sleep(0.2)
            self.assertEqual(len(coord.client.completed), 1)
        asyncio.run(run())

    def test_an_already_completed_task_is_not_completed_again(self):
        """The guard the pending set cannot provide: a task the SERVER already has as done must not
        be re-sent, or an automation firing on a stale condition rolls a repeat a second time."""
        async def run():
            coord, call, _ = _world(status=STATUS_COMPLETED)
            await services._complete(call)
            await asyncio.sleep(0.2)
            self.assertEqual(coord.client.completed, [])
        asyncio.run(run())

    def test_a_repeat_cannot_be_reopened_after_the_window(self):
        async def run():
            coord, call, _ = _world(status=STATUS_COMPLETED, rule="RRULE:FREQ=WEEKLY")
            with self.assertRaises(Exception):
                await services._reopen(call)
            self.assertEqual(coord.client.reopened, [])
        asyncio.run(run())

    def test_an_unknown_node_is_refused_not_ignored(self):
        async def run():
            _, call, _ = _world()
            call.data = {services.ATTR_NODE_ID: "does-not-exist"}
            with self.assertRaises(Exception):
                await services._complete(call)
        asyncio.run(run())


class MinimumVersionTest(unittest.TestCase):
    """Below the floor the config flow fails on a missing attribute, which tells the user nothing.

    Refusing with a sentence is the whole point, so the boundary is worth pinning. These drive the
    real guard with the running version patched, rather than restating its comparison.
    """

    def _too_old_when_running(self, major, minor):
        """The real `_too_old`, run against a pretended HA version.

        Compiled out of __init__.py rather than imported: importing it pulls in the whole
        config-entry setup path, which has nothing to do with comparing two numbers.
        """
        source = (COMPONENT / "__init__.py").read_text()
        start = source.index("def _too_old()")
        body = source[start:source.index("\nasync def", start)]
        scope = {"MAJOR_VERSION": major, "MINOR_VERSION": minor,
                 "MIN_HA_VERSION": sys.modules["progresscove.const"].MIN_HA_VERSION}
        exec(compile(body, "__init__.py", "exec"), scope)
        return scope["_too_old"]()

    def test_the_boundary_is_where_it_claims_to_be(self):
        major, minor = sys.modules["progresscove.const"].MIN_HA_VERSION
        self.assertEqual(self._too_old_when_running(major, minor - 1), f"{major}.{minor - 1}")
        self.assertIsNone(self._too_old_when_running(major, minor),
                          "the stated minimum must itself be allowed")
        self.assertIsNone(self._too_old_when_running(major, minor + 1))

    def test_hacs_and_the_code_agree(self):
        """Two places state the floor; they drift silently if nobody checks."""
        import json
        hacs = json.load(open(ROOT / "hacs.json"))
        declared = tuple(int(p) for p in hacs["homeassistant"].split(".")[:2])
        self.assertEqual(declared, sys.modules["progresscove.const"].MIN_HA_VERSION)


class CopyMatchesBehaviourTest(unittest.TestCase):
    """User-facing copy drifts silently when behaviour changes under it.

    A dialog that describes the old behaviour fails nothing: the words are simply wrong, and only a
    reader notices. These assert that the copy still matches what the code does.
    """

    def _catalogues(self):
        import json
        base = ROOT / "custom_components" / "progresscove"
        for path in (base / "strings.json",
                     base / "translations" / "en.json",
                     base / "translations" / "tr.json"):
            yield path.name, json.load(open(path))

    def test_no_catalogue_names_a_renamed_attribute(self):
        for name, catalogue in self._catalogues():
            blob = json.dumps(catalogue, ensure_ascii=False)
            for dead in ("project_complete", "project_done", "project_total", "project_percent"):
                self.assertNotIn(dead, blob, f"{name} still names {dead}")

    def test_nothing_promises_items_on_the_attributes(self):
        """They moved to todo.get_items when a long list was measured past HA's attribute cap."""
        base = ROOT / "custom_components" / "progresscove"
        text = (ROOT / "README.md").read_text() + (base / "services.yaml").read_text()
        # The COUNTS are still attributes and are named as such; only the bulk lists left. So this
        # looks for the bare names, not for any word starting with them.
        for line in text.splitlines():
            for bare in (r"`nested_items`", r"`items`"):
                if re.search(re.escape(bare), line) and "attribute" in line.lower():
                    self.fail(f"copy still calls it an attribute: {line.strip()}")
        entity = (base / "todo.py").read_text()
        self.assertNotIn("ATTR_ITEMS:", entity, "todo.py publishes items on the attributes again")
        self.assertNotIn("ATTR_NESTED:", entity, "todo.py publishes nested_items again")

    def test_no_em_dashes_anywhere_that_ships(self):
        """House style. A comma, a colon or a full stop always fits, and an em dash is the first
        thing to look wrong in a narrow dialog.

        Covers code as well as copy: the rule used to be checked on user-facing strings only, so
        118 lines of comments and docstrings drifted the other way unnoticed.

        picker_tree.CONTEXT_ONLY is exempt by name. It is a box-drawing glyph marking a row that
        exists only to hold its children, not prose.
        """
        base = ROOT / "custom_components" / "progresscove"
        catalogues = [base / "strings.json", base / "services.yaml",
                      ROOT / "README.md"]
        catalogues += sorted((base / "translations").glob("*.json"))
        sources = sorted(base.glob("*.py")) + sorted((ROOT / "www").glob("*.js"))
        for path in catalogues + sources:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "\u2014" in line and "CONTEXT_ONLY" not in line:
                    self.fail(f"{path.name}:{number} contains an em dash: {line.strip()[:70]}")

    def test_the_undo_promise_is_scoped_to_repeats(self):
        """"ten seconds" unqualified reads as "every task waits", which stopped being true."""
        for name, catalogue in self._catalogues():
            blob = json.dumps(catalogue, ensure_ascii=False)
            if "ten seconds" in blob:
                self.assertIn("repeating", blob,
                              f"{name} promises ten seconds without saying it is repeats only")
            if "on saniye" in blob:
                self.assertIn("Tekrarlanan", blob,
                              f"{name} promises ten seconds without saying it is repeats only")


class GetNestedItemsTest(unittest.TestCase):
    """The one question `todo.get_items` cannot answer.

    Named for POSITION, not tier: one level below a list entity is a T3 under a domain and a T5
    under a section, so "subtasks" was only true when the entity happened to be a T2.

    The items themselves left the entity's attributes when a ~200-task list was measured past
    Home Assistant's 16 KB attribute ceiling; `get_items` carries them instead, at any size. It
    stops at one level, because a TodoItem has no children, so the second level, which is our own
    concept, needs this.
    """

    def _call(self, tree, node_id):
        class Data:
            by_id = {n["id"]: n for n in tree}

            def children(self, parent_id):
                return [n for n in tree if n.get("parent_id") == parent_id]

        class Coord:
            data = Data()

        class Entry:
            runtime_data = Coord()

        class Hass:
            config_entries = type("E", (), {"async_entries": lambda self, d: [Entry()]})()

        return type("Call", (), {"hass": Hass(), "data": {services.ATTR_NODE_ID: node_id}})()

    TREE = [
        {"id": "shopping", "name": "Shopping"},
        {"id": "groceries", "name": "Groceries", "parent_id": "shopping"},
        {"id": "milk", "name": "Milk", "parent_id": "groceries", "status": 2},
        {"id": "bread", "name": "Bread", "parent_id": "groceries"},
        {"id": "frozen", "name": "Frozen", "parent_id": "shopping"},
    ]

    def test_returns_grandchildren_keyed_by_their_parent(self):
        out = asyncio.run(services._get_nested_items(self._call(self.TREE, "shopping")))
        self.assertEqual(set(out["nested_items"]), {"groceries", "frozen"})
        self.assertEqual(
            [s["summary"] for s in out["nested_items"]["groceries"]], ["Milk", "Bread"]
        )

    def test_completion_travels_as_a_boolean(self):
        out = asyncio.run(services._get_nested_items(self._call(self.TREE, "shopping")))
        done = {s["summary"]: s["done"] for s in out["nested_items"]["groceries"]}
        self.assertEqual(done, {"Milk": True, "Bread": False})

    def test_a_childless_item_gets_an_empty_list_not_a_missing_key(self):
        """The card indexes by uid; a missing key would be an undefined it has to guard."""
        out = asyncio.run(services._get_nested_items(self._call(self.TREE, "shopping")))
        self.assertEqual(out["nested_items"]["frozen"], [])

    def test_an_unknown_node_is_refused_rather_than_answered_empty(self):
        with self.assertRaises(services.ServiceValidationError):
            asyncio.run(services._get_nested_items(self._call(self.TREE, "nope")))


class PatchBodyTest(unittest.TestCase):
    """A PATCH only changes the fields it carries, so what is IN the body is the whole contract.

    Home Assistant sends the full item back on every edit, including a plain checkbox tick. If a
    tick built a body carrying `due_at: null`, completing a task would silently erase its due date.
    """

    def _body(self, **kwargs):
        api = sys.modules["progresscove.api"]
        sent = {}

        class Client(api.ProgressCoveClient):
            def __init__(self):
                pass

            async def _request(self, method, path, **rest):
                sent.update(rest.get("json", {}))
                return {}

        asyncio.run(Client().async_update_task("node-1", **kwargs))
        return sent

    def test_a_rename_leaves_the_due_date_alone(self):
        self.assertEqual(self._body(name="Bins"), {"name": "Bins"})

    def test_passing_none_clears_the_due_date(self):
        self.assertEqual(self._body(due_at=None), {"due_at": None})

    def test_a_due_date_is_sent_as_given(self):
        stamp = "2026-09-01T09:00:00+00:00"
        self.assertEqual(self._body(due_at=stamp), {"due_at": stamp})

    def test_nothing_given_sends_an_empty_body(self):
        self.assertEqual(self._body(), {})


if __name__ == "__main__":
    unittest.main()
