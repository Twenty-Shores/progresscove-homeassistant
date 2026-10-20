"""Lists: "Manage lists" over the whole tree, "Add a list" one at a time, the same rows either way.

The manage form starts from the lists the entry has now. Ticking adds a list subentry, unticking
removes that list's subentry, and anything the form did not show is left alone. "Add a list" picks
one node and creates one list subentry, and nothing else.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

import ha_stubs

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"


class _Selector:
    def __init__(self, *args, **kwargs):
        pass


class _Schema:
    """Keeps what the form was built with, so a test can read what is pre-ticked."""

    def __init__(self, fields):
        self.fields = fields


class _Required:
    """A schema key that remembers its default."""

    def __init__(self, key, default=None):
        self.key = key
        self.default = default


ha_stubs.fill("homeassistant.config_entries", ConfigFlow=type("ConfigFlow", (), {
    "__init_subclass__": classmethod(lambda cls, domain=None, **kwargs: None)}),
    ConfigSubentryFlow=object, OptionsFlow=object, SubentryFlowResult=dict, ConfigFlowResult=dict)
ha_stubs.fill("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
ha_stubs.fill("homeassistant.helpers.selector", SelectSelector=_Selector,
              SelectSelectorConfig=_Selector, SelectOptionDict=dict, BooleanSelector=_Selector,
              SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown", LIST="list"),
              TextSelector=_Selector, TextSelectorConfig=_Selector,
              TextSelectorType=types.SimpleNamespace(PASSWORD="password"))
ha_stubs.fill("voluptuous", Schema=_Schema, Required=_Required, Optional=_Required)
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
api = _load("api")
_load("names")
_load("base_url")
_load("picker_tree")
config_flow = _load("config_flow")
# Whichever file stubbed voluptuous first owns it, so the form's schema is read through this one.
config_flow.vol = types.SimpleNamespace(Schema=_Schema, Required=_Required, Optional=_Required)
# The labels, rather than a selector, so a test can read which rows were offered.
config_flow._list_selector = lambda labels: labels

INBOX = "3f2b8c1e-6a4d-4e9b-8c7f-1d2e3a4b5c6d"

NODES = [
    {"id": "home", "name": "Home", "depth": 1},
    {"id": "shopping", "name": "Shopping", "parent_id": "home", "depth": 2},
    {"id": "groceries", "name": "Groceries", "parent_id": "shopping", "depth": 3},
    {"id": "milk", "name": "Milk", "parent_id": "groceries", "depth": 4},
    {"id": "garden", "name": "Garden", "parent_id": "home", "depth": 2},
    {"id": INBOX, "name": "Unsorted", "parent_id": "home", "depth": 2},
    {"id": "captured", "name": "Call the plumber", "parent_id": INBOX, "depth": 3},
]


def _list(node_id, title=None):
    return ha_stubs.ConfigSubentry(
        data={const.CONF_NODE_ID: node_id}, subentry_type=const.SUBENTRY_LIST,
        title=title or node_id, unique_id=None, subentry_id=f"sub-{node_id}",
    )


class _Entry:
    unique_id = INBOX

    def __init__(self, subentries, options=None):
        self.data = {"url": "https://api.progresscove.com", "token": "pat_x"}
        self.options = options or {}
        self.subentries = {subentry.subentry_id: subentry for subentry in subentries}


class _ConfigEntries:
    """Home Assistant's add and remove, recorded, and applied to the entry as the real ones are."""

    def __init__(self):
        self.calls = []

    def async_add_subentry(self, entry, subentry):
        self.calls.append(("add", subentry.data[const.CONF_NODE_ID]))
        entry.subentries = {**entry.subentries, subentry.subentry_id: subentry}
        return True

    def async_remove_subentry(self, entry, subentry_id):
        self.calls.append(("remove", entry.subentries[subentry_id].data[const.CONF_NODE_ID]))
        entry.subentries = {
            key: subentry for key, subentry in entry.subentries.items() if key != subentry_id
        }
        return True


class _Client:
    fail = False

    def __init__(self, *args, **kwargs):
        pass

    async def async_get_tree(self):
        if _Client.fail:
            raise api.ProgressCoveError("unreachable")
        return NODES


def _flow(entry, handler=config_flow.ManageListsFlowHandler):
    flow = handler()
    flow.hass = types.SimpleNamespace(config_entries=_ConfigEntries())
    flow._get_entry = lambda: entry
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    flow.async_abort = lambda **kwargs: {"type": "abort", **kwargs}
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    return flow


def _ticked(form):
    [field] = form["data_schema"].fields
    assert field.key == const.CONF_LISTS
    return field.default


def _offered(form):
    [labels] = form["data_schema"].fields.values()
    return set(labels)


def _submit(entry, chosen):
    flow = _flow(entry)
    result = asyncio.run(flow.async_step_user({const.CONF_LISTS: chosen}))
    return result, flow.hass.config_entries.calls


def _lists(entry):
    return sorted(
        subentry.data[const.CONF_NODE_ID]
        for subentry in entry.subentries.values()
        if subentry.subentry_type == const.SUBENTRY_LIST
    )


class ManageListsTest(unittest.TestCase):
    def setUp(self):
        self._real_client = config_flow.ProgressCoveClient
        config_flow.ProgressCoveClient = _Client
        _Client.fail = False

    def tearDown(self):
        config_flow.ProgressCoveClient = self._real_client

    def test_the_form_starts_with_the_current_lists_ticked(self):
        entry = _Entry([_list("groceries"), _list("garden")])
        form = asyncio.run(_flow(entry).async_step_user())
        self.assertEqual(form["step_id"], "user")
        self.assertEqual(sorted(_ticked(form)), ["garden", "groceries"])

    def test_buttons_and_switches_are_not_ticked_as_lists(self):
        button = ha_stubs.ConfigSubentry(
            data={const.CONF_NODE_ID: "shopping"}, subentry_type=const.SUBENTRY_BUTTON,
            title="Shopping", unique_id=None,
        )
        form = asyncio.run(_flow(_Entry([button, _list("garden")])).async_step_user())
        self.assertEqual(_ticked(form), ["garden"])

    def test_ticking_adds_a_list_subentry_titled_with_its_name(self):
        entry = _Entry([_list("garden")])
        result, calls = _submit(entry, ["garden", "groceries"])
        self.assertEqual(result, {"type": "abort", "reason": "lists_updated"})
        self.assertEqual(calls, [("add", "groceries")])
        added = next(
            s for s in entry.subentries.values() if s.data[const.CONF_NODE_ID] == "groceries"
        )
        self.assertEqual(added.subentry_type, const.SUBENTRY_LIST)
        self.assertEqual(added.title, "Groceries")
        self.assertEqual(added.unique_id, "list_groceries")

    def test_unticking_removes_that_lists_subentry(self):
        entry = _Entry([_list("garden"), _list("groceries")])
        result, calls = _submit(entry, ["garden"])
        self.assertEqual(result["reason"], "lists_updated")
        self.assertEqual(calls, [("remove", "groceries")])
        self.assertEqual(_lists(entry), ["garden"])

    def test_adding_and_removing_in_one_submit(self):
        entry = _Entry([_list("garden"), _list("groceries")])
        _, calls = _submit(entry, ["groceries", "shopping", "home"])
        self.assertEqual(sorted(calls), [("add", "home"), ("add", "shopping"),
                                         ("remove", "garden")])
        self.assertEqual(_lists(entry), ["groceries", "home", "shopping"])

    def test_unticking_everything_removes_every_list(self):
        entry = _Entry([_list("garden"), _list("groceries")])
        _submit(entry, [])
        self.assertEqual(_lists(entry), [])

    def test_an_unchanged_submit_changes_nothing_and_says_so(self):
        entry = _Entry([_list("garden")])
        result, calls = _submit(entry, ["garden"])
        self.assertEqual(result, {"type": "abort", "reason": "lists_unchanged"})
        self.assertEqual(calls, [])

    def test_a_list_outside_the_tiers_on_offer_is_kept(self):
        """Not shown, so not unticked: hiding a tier in the options is not a way to lose lists."""
        entry = _Entry([_list("milk"), _list("garden")])
        form = asyncio.run(_flow(entry).async_step_user())
        self.assertEqual(_ticked(form), ["garden"])
        _, calls = _submit(entry, [])
        self.assertEqual(calls, [("remove", "garden")])
        self.assertEqual(_lists(entry), ["milk"])

    def test_a_list_whose_node_is_gone_is_left_to_the_prune(self):
        entry = _Entry([_list("deleted-in-the-app"), _list("garden")])
        _submit(entry, ["garden", "groceries"])
        self.assertEqual(_lists(entry), ["deleted-in-the-app", "garden", "groceries"])

    def test_a_row_outside_the_tiers_on_offer_is_refused(self):
        entry = _Entry([])
        result, calls = _submit(entry, ["milk"])
        self.assertEqual(result["errors"], {const.CONF_LISTS: "not_selectable"})
        self.assertEqual(calls, [])

    def test_an_unknown_id_is_refused(self):
        entry = _Entry([])
        result, calls = _submit(entry, ["not-in-this-account"])
        self.assertEqual(result["errors"], {const.CONF_LISTS: "not_selectable"})
        self.assertEqual(calls, [])

    def test_the_inbox_and_what_sits_in_it_are_not_offered(self):
        """The inbox has its own entity, keyed exactly as a list of it would be."""
        form = asyncio.run(_flow(_Entry([])).async_step_user())
        self.assertNotIn(INBOX, _offered(form))
        self.assertNotIn("captured", _offered(form))
        self.assertIn("garden", _offered(form))

    def test_the_inbox_is_refused_if_submitted_anyway(self):
        for node_id in (INBOX, "captured"):
            with self.subTest(node_id):
                entry = _Entry([])
                result, calls = _submit(entry, [node_id])
                self.assertEqual(result["errors"], {const.CONF_LISTS: "not_selectable"})
                self.assertEqual(calls, [])

    def test_an_unreachable_server_changes_nothing(self):
        _Client.fail = True
        entry = _Entry([_list("garden")])
        result, calls = _submit(entry, [])
        self.assertEqual(result, {"type": "abort", "reason": "cannot_connect"})
        self.assertEqual(calls, [])


class _OneList:
    """"Add a list": the node selector's choices, read rather than drawn."""

    def __init__(self, entry):
        self.flow = _flow(entry, config_flow.ListSubentryFlowHandler)

    def form(self):
        return asyncio.run(self.flow.async_step_user())

    def offered(self):
        [choices] = self.form()["data_schema"].fields.values()
        return set(choices)

    def submit(self, node_id):
        return asyncio.run(self.flow.async_step_user({const.CONF_NODE_ID: node_id}))


class AddAListTest(unittest.TestCase):
    def setUp(self):
        self._real = config_flow.ProgressCoveClient, config_flow._node_selector
        config_flow.ProgressCoveClient = _Client
        config_flow._node_selector = lambda choices: choices
        _Client.fail = False

    def tearDown(self):
        config_flow.ProgressCoveClient, config_flow._node_selector = self._real

    def test_it_offers_what_manage_lists_offers_less_the_lists_there_are(self):
        offered = _OneList(_Entry([_list("garden")])).offered()
        self.assertEqual(offered, {"home", "shopping", "groceries"})

    def test_picking_one_creates_one_list_subentry_and_changes_nothing_else(self):
        flow = _OneList(_Entry([_list("garden")]))
        result = flow.submit("groceries")
        self.assertEqual(result, {
            "type": "create_entry", "title": "Groceries",
            "data": {const.CONF_NODE_ID: "groceries"}, "unique_id": "list_groceries",
        })
        self.assertEqual(flow.flow.hass.config_entries.calls, [])

    def test_a_list_added_since_the_form_was_drawn_is_refused(self):
        entry = _Entry([])
        flow = _OneList(entry)
        flow.form()
        entry.subentries = {"sub-groceries": _list("groceries")}
        self.assertEqual(flow.submit("groceries"), {"type": "abort",
                                                    "reason": "already_configured"})

    def test_a_node_gone_from_the_tree_since_the_form_was_drawn_asks_again(self):
        result = _OneList(_Entry([])).submit("deleted-in-the-app")
        self.assertEqual(result["errors"], {const.CONF_NODE_ID: "unavailable"})

    def test_with_every_node_a_list_there_is_nothing_to_add(self):
        entry = _Entry([_list(node_id) for node_id in ("home", "shopping", "groceries", "garden")])
        self.assertEqual(_OneList(entry).form(), {"type": "abort", "reason": "no_tasks_left"})

    def test_an_unreachable_server_says_so(self):
        _Client.fail = True
        self.assertEqual(_OneList(_Entry([])).form(), {"type": "abort",
                                                       "reason": "cannot_connect"})


class HeaderButtonsTest(unittest.TestCase):
    def test_manage_lists_comes_first_then_add_a_list(self):
        """Home Assistant draws the header buttons in the order of these keys."""
        types_ = config_flow.ProgressCoveConfigFlow.async_get_supported_subentry_types(None)
        self.assertEqual(list(types_), ["manage_lists", "list", "button", "switch", "sensor"])
        self.assertIs(types_["manage_lists"], config_flow.ManageListsFlowHandler)
        self.assertIs(types_["list"], config_flow.ListSubentryFlowHandler)

    def test_both_buttons_are_labelled_and_both_kinds_of_row_say_list(self):
        catalogue = json.loads((COMPONENT / "strings.json").read_text())["config_subentries"]
        self.assertEqual(catalogue["manage_lists"]["initiate_flow"]["user"], "Manage lists")
        self.assertEqual(catalogue["list"]["initiate_flow"]["user"], "Add a list")
        self.assertEqual(catalogue["list"]["entry_type"], "List")


if __name__ == "__main__":
    unittest.main()
