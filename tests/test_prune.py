"""Pruning deletes a user's configured entity, so every guard on it is tested.

The failure that matters is not "a dead entity survived", it is "a live one was deleted because a
poll went wrong". Only an explicit 404 removes anything; everything else, including what looks like
an empty account, leaves the subentries alone. That holds for both ways a removal happens: on its
own when the person turned that on, and from the repair they confirm when they did not.
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import ha_stubs

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    mod.__path__ = getattr(mod, "__path__", [])
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _IssueSeverity:
    WARNING = "warning"


_created: dict = {}
_deleted: list = []


def _create_issue(hass, domain, issue_id, **kwargs):
    _created[issue_id] = kwargs


def _delete_issue(hass, domain, issue_id):
    _deleted.append(issue_id)
    _created.pop(issue_id, None)


_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module(
    "homeassistant.helpers.issue_registry",
    async_create_issue=_create_issue,
    async_delete_issue=_delete_issue,
    IssueSeverity=_IssueSeverity,
)
_module("aiohttp", ClientError=type("ClientError", (Exception,), {}),
        ClientSession=object, ClientResponse=object)
_module("homeassistant.const",
        Platform=type("Platform", (), {"TODO": "todo", "BUTTON": "button",
                                       "SWITCH": "switch", "SENSOR": "sensor"}))
_module("homeassistant.exceptions",
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}),
        HomeAssistantError=ha_stubs.HomeAssistantError,
        ServiceValidationError=ha_stubs.ServiceValidationError)
_module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
_module("homeassistant.helpers.update_coordinator",
        CoordinatorEntity=type("CoordinatorEntity", (), {
            "__class_getitem__": classmethod(lambda c, i: c)}),
        DataUpdateCoordinator=type("DataUpdateCoordinator", (), {
            "__class_getitem__": classmethod(lambda c, i: c)}),
        UpdateFailed=ha_stubs.UpdateFailed)
_module("homeassistant.helpers.config_validation", string=str)
_module("voluptuous", Schema=lambda *a, **k: None, Required=lambda *a, **k: None)


class _RepairsFlow:
    """Records what a step returned, the way the real flow manager reads it."""

    def async_show_form(self, *, step_id, data_schema=None, description_placeholders=None,
                        errors=None):
        return {"type": "form", "step_id": step_id, "errors": errors,
                "description_placeholders": description_placeholders}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}

    def async_create_entry(self, *, data):
        return {"type": "create_entry"}


_module("homeassistant.data_entry_flow", FlowResult=dict)
_module("homeassistant.components.repairs", RepairsFlow=_RepairsFlow)

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


api = _load("api")
_load("const")
prune = _load("prune")
_load("helpers")
coordinator_module = _load("coordinator")
subscription_issue = sys.modules["progresscove.subscription_issue"]
repairs = _load("repairs")

AUTO = {"auto_prune": True}

ALIVE = "alive-node"
GONE = "gone-node"
LOADED = ha_stubs.ConfigEntryState.LOADED


class _Subentry:
    def __init__(self, node_id, title, subentry_type="button"):
        self.data = {"node_id": node_id}
        self.title = title
        self.subentry_type = subentry_type
        self.subentry_id = f"sub-{node_id}"


class _Entry:
    entry_id = "entry-1"
    state = LOADED

    def __init__(self, subentries, options=None):
        self.subentries = {s.subentry_id: s for s in subentries}
        self.options = options or {}


class _Entries:
    def __init__(self, entry=None):
        self.removed = []
        self._entry = entry

    def async_remove_subentry(self, entry, subentry_id):
        self.removed.append(subentry_id)
        entry.subentries.pop(subentry_id)

    def async_get_entry(self, entry_id):
        return self._entry if self._entry and self._entry.entry_id == entry_id else None


class _Hass:
    def __init__(self, entry=None):
        self.config_entries = _Entries(entry)


class _Client:
    """Answers the targeted existence check however the test needs."""

    def __init__(self, alive=(), raises=None):
        self.alive = set(alive)
        self.raises = raises
        self.asked = []

    async def async_node_exists(self, node_id):
        self.asked.append(node_id)
        if self.raises is not None:
            raise self.raises
        if node_id in self.alive:
            return True
        raise api.ProgressCoveNotFound(f"not found: /nodes/{node_id}")


def _tree(*ids):
    return types.SimpleNamespace(by_id={n: {"id": n} for n in ids})


def _run(hass, entry, client, tree, verdicts=None):
    return asyncio.run(prune.async_prune_deleted(
        hass, entry, client, tree, {} if verdicts is None else verdicts
    ))


class PruneTest(unittest.TestCase):
    def setUp(self):
        _created.clear()
        _deleted.clear()

    def test_removing_on_its_own_is_off_by_default(self):
        """Removing an entity can break an automation or dashboard that names it, so the person
        is asked first unless they chose otherwise."""
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")])
        _run(hass, entry, _Client(), _tree(ALIVE))
        self.assertEqual(hass.config_entries.removed, [])
        issue = _created["stale_entities_entry-1"]
        self.assertTrue(issue["is_fixable"])
        self.assertEqual(issue["data"], {"entry_id": "entry-1"})
        self.assertIn("Bulk pickup", issue["translation_placeholders"]["names"])

    def test_turned_on_a_confirmed_404_removes_the_subentry(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")], options=AUTO)
        _run(hass, entry, _Client(), _tree(ALIVE))
        self.assertEqual(hass.config_entries.removed, [f"sub-{GONE}"])
        self.assertNotIn("stale_entities_entry-1", _created)

    def test_a_node_still_in_the_tree_is_never_even_asked_about(self):
        hass, entry = _Hass(), _Entry([_Subentry(ALIVE, "Water the plants")], options=AUTO)
        client = _Client(alive=(ALIVE,))
        _run(hass, entry, client, _tree(ALIVE))
        self.assertEqual(client.asked, [])
        self.assertEqual(hass.config_entries.removed, [])

    def test_a_200_keeps_a_subentry_missing_from_a_truncated_tree(self):
        """The node vanished from the bulk response but the server says it is there. That is a bad
        tree, not a deletion, and deleting on it would be the whole failure mode."""
        hass, entry = _Hass(), _Entry([_Subentry(ALIVE, "Water the plants")], options=AUTO)
        client = _Client(alive=(ALIVE,))
        _run(hass, entry, client, _tree("something-else"))
        self.assertEqual(client.asked, [ALIVE])
        self.assertEqual(hass.config_entries.removed, [])
        self.assertNotIn("stale_entities_entry-1", _created)

    def test_a_network_failure_removes_nothing_and_raises_its_own_repair(self):
        """Not the fixable one: there is nothing confirmed to remove."""
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")], options=AUTO)
        client = _Client(raises=api.ProgressCoveError("connection reset"))
        _run(hass, entry, client, _tree(ALIVE))
        self.assertEqual(hass.config_entries.removed, [])
        self.assertFalse(_created["unconfirmed_entities_entry-1"]["is_fixable"])
        self.assertNotIn("stale_entities_entry-1", _created)

    def test_an_auth_failure_removes_nothing(self):
        """It should never get here (a failed refresh returns early), so this pins the belt as well
        as the braces: an auth error is not a 404 and cannot delete anything."""
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")], options=AUTO)
        client = _Client(raises=api.ProgressCoveAuthError("401"))
        _run(hass, entry, client, _tree(ALIVE))
        self.assertEqual(hass.config_entries.removed, [])

    def test_an_empty_tree_is_never_read_as_everything_was_deleted(self):
        """The single most destructive misreading available: an empty or lost payload looks exactly
        like an emptied account, and acting on it would delete every entity the user configured."""
        hass = _Hass()
        entry = _Entry([_Subentry(GONE, "A"), _Subentry(ALIVE, "B")], options=AUTO)
        client = _Client()
        _run(hass, entry, client, _tree())
        self.assertEqual(client.asked, [])
        self.assertEqual(hass.config_entries.removed, [])

    def test_the_repairs_clear_once_nothing_is_stale(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")], options=AUTO)
        _run(hass, entry, _Client(), _tree(ALIVE))
        # The subentry is gone now, so a second pass has nothing to report.
        _run(hass, entry, _Client(), _tree(ALIVE))
        self.assertIn("stale_entities_entry-1", _deleted)
        self.assertIn("unconfirmed_entities_entry-1", _deleted)

    def test_a_burst_of_deletions_is_rate_limited(self):
        """Deleting a 40-task project in the app must not fire 40 requests in one poll."""
        subs = [_Subentry(f"gone-{i}", f"Task {i}") for i in range(20)]
        hass, entry = _Hass(), _Entry(subs, options=AUTO)
        client = _Client()
        _run(hass, entry, client, _tree(ALIVE))
        self.assertEqual(len(client.asked), prune.MAX_CHECKS_PER_REFRESH)

    def test_all_confirmed_removals_happen_after_the_checks(self):
        """Each removal notifies the update listener, which reloads the entry, and a reload cancels
        every completion still inside its undo window. Removing inside the loop would reload once
        per entity while later checks were still running against the entry being torn down."""
        subs = [_Subentry(f"gone-{i}", f"Task {i}") for i in range(3)]
        hass, entry = _Hass(), _Entry(subs, options=AUTO)

        order = []
        client = _Client()
        real_ask = client.async_node_exists

        async def watched(node_id):
            order.append(("ask", node_id))
            return await real_ask(node_id)

        client.async_node_exists = watched
        real_remove = hass.config_entries.async_remove_subentry
        hass.config_entries.async_remove_subentry = lambda e, sid: (
            order.append(("remove", sid)), real_remove(e, sid))[1]

        _run(hass, entry, client, _tree(ALIVE))
        kinds = [kind for kind, _ in order]
        self.assertEqual(kinds, ["ask"] * 3 + ["remove"] * 3, order)

    def test_a_list_follows_the_same_rule_when_removing_on_its_own(self):
        hass = _Hass()
        entry = _Entry([_Subentry(GONE, "Groceries", subentry_type="list")], options=AUTO)
        client = _Client()
        _run(hass, entry, client, _tree(ALIVE))
        self.assertEqual(client.asked, [GONE])
        self.assertEqual(hass.config_entries.removed, [f"sub-{GONE}"])

    def test_a_list_is_named_in_the_repair_when_asked_first(self):
        hass = _Hass()
        entry = _Entry([_Subentry(GONE, "Groceries", subentry_type="list")])
        _run(hass, entry, _Client(), _tree(ALIVE))
        self.assertEqual(hass.config_entries.removed, [])
        issue = _created["stale_entities_entry-1"]
        self.assertTrue(issue["is_fixable"])
        self.assertEqual(issue["translation_placeholders"]["names"], "Groceries")

    def test_a_list_the_server_still_has_is_kept(self):
        hass = _Hass()
        entry = _Entry([_Subentry(ALIVE, "Groceries", subentry_type="list")], options=AUTO)
        _run(hass, entry, _Client(alive=(ALIVE,)), _tree("something-else"))
        self.assertEqual(hass.config_entries.removed, [])


class SettledTest(unittest.TestCase):
    """What the prune reports back, which decides whether an unchanged poll runs it again."""

    def test_settled_when_every_missing_subentry_was_answered(self):
        entry = _Entry([_Subentry(GONE, "Bulk pickup")])
        self.assertTrue(_run(_Hass(), entry, _Client(), _tree(ALIVE)))

    def test_owed_when_the_server_could_not_answer(self):
        entry = _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _Client(raises=api.ProgressCoveError("timed out"))
        self.assertFalse(_run(_Hass(), entry, client, _tree(ALIVE)))

    def test_owed_when_some_were_left_for_a_later_pass(self):
        count = prune.MAX_CHECKS_PER_REFRESH + 1
        subs = [_Subentry(f"gone-{i}", f"Task {i}") for i in range(count)]
        self.assertFalse(_run(_Hass(), _Entry(subs), _Client(), _tree(ALIVE)))


class BacklogTest(unittest.TestCase):
    """More missing than one pass may ask about. Each pass takes the ones left waiting longest, so
    the whole backlog is asked about in turn, still never more than the cap per pass."""

    COUNT = prune.MAX_CHECKS_PER_REFRESH * 2 + 2

    def setUp(self):
        _created.clear()
        _deleted.clear()
        self.subs = [_Subentry(f"gone-{i}", f"Task {i:02d}") for i in range(self.COUNT)]
        self.entry = _Entry(self.subs)
        self.verdicts = {}

    def _pass(self, client):
        return _run(_Hass(), self.entry, client, _tree(ALIVE), self.verdicts)

    def test_every_suspect_is_asked_about_across_passes(self):
        client = _Client()
        self._pass(client)
        self._pass(client)
        self._pass(client)
        self.assertEqual(set(client.asked), {s.data["node_id"] for s in self.subs})

    def test_no_pass_asks_more_than_the_cap(self):
        for _ in range(4):
            client = _Client()
            self._pass(client)
            self.assertLessEqual(len(client.asked), prune.MAX_CHECKS_PER_REFRESH)

    def test_the_repair_names_every_one_confirmed_so_far(self):
        for _ in range(3):
            self._pass(_Client())
        names = _created["stale_entities_entry-1"]["translation_placeholders"]["names"]
        self.assertEqual(names, prune.subentry_names(self.subs))

    def test_owed_until_every_one_has_an_answer_then_settled(self):
        self.assertFalse(self._pass(_Client()))
        self.assertFalse(self._pass(_Client()))
        self.assertTrue(self._pass(_Client()))

    def test_a_pass_with_room_to_spare_asks_again_the_longest_waiting(self):
        cap = prune.MAX_CHECKS_PER_REFRESH
        ids = [s.data["node_id"] for s in self.subs]
        self._pass(_Client())
        self._pass(_Client())
        client = _Client()
        self._pass(client)
        self.assertEqual(client.asked, ids[2 * cap:] + ids[:cap - 2])
        client = _Client()
        self._pass(client)
        self.assertEqual(client.asked, ids[cap - 2:2 * cap - 2])

    def test_one_the_server_could_not_answer_waits_its_turn_again(self):
        first = _Client(raises=api.ProgressCoveError("timed out"))
        self.assertFalse(self._pass(first))
        client = _Client()
        self._pass(client)
        self.assertNotIn(first.asked[0], client.asked)
        self.assertIn("unconfirmed_entities_entry-1", _created)

    def test_one_back_in_the_tree_is_forgotten(self):
        self._pass(_Client())
        back = self.subs[0].data["node_id"]
        _run(_Hass(), self.entry, _Client(), _tree(ALIVE, back), self.verdicts)
        self.assertNotIn(self.subs[0].subentry_id, self.verdicts)
        names = _created["stale_entities_entry-1"]["translation_placeholders"]["names"]
        self.assertNotIn(self.subs[0].title, names)

    def test_a_404_alone_still_decides_and_a_200_is_never_named(self):
        alive = [s.data["node_id"] for s in self.subs[::2]]
        for _ in range(3):
            self._pass(_Client(alive=alive))
        names = _created["stale_entities_entry-1"]["translation_placeholders"]["names"]
        self.assertEqual(names, prune.subentry_names(self.subs[1::2]))

    def test_removing_on_its_own_works_through_the_backlog_too(self):
        hass = _Hass()
        entry = _Entry(self.subs, options=AUTO)
        for _ in range(3):
            _run(hass, entry, _Client(), _tree(ALIVE), self.verdicts)
        self.assertEqual(sorted(hass.config_entries.removed),
                         sorted(s.subentry_id for s in self.subs))
        self.assertEqual(self.verdicts, {})


class _PollingClient(_Client):
    """A client the coordinator can poll. Like the real one, an unchanged endpoint hands back the
    very list it returned last time."""

    def __init__(self, tree_ids, alive=(), raises=None, fetch_error=None):
        super().__init__(alive=alive, raises=raises)
        self.token_scope = None
        self.fetch_error = fetch_error
        self.nodes = [{"id": node_id, "name": node_id} for node_id in tree_ids]
        self.my_day = []

    async def async_get_tree(self):
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.nodes

    async def async_get_my_day(self):
        return self.my_day


def _polling(hass, entry, client):
    coordinator = coordinator_module.ProgressCoveCoordinator.__new__(
        coordinator_module.ProgressCoveCoordinator
    )
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.client = client
    coordinator._nodes = coordinator._my_day = None
    coordinator._prune_owed = False
    coordinator.prune_verdicts = {}
    return coordinator


def _poll(coordinator):
    """One refresh, kept as the coordinator's data the way the real base class keeps it."""
    coordinator.data = asyncio.run(coordinator._async_update_data())
    return coordinator.data


class PruneOnRefreshTest(unittest.TestCase):
    """The coordinator prunes as part of a successful fetch, and only then."""

    def setUp(self):
        _created.clear()
        _deleted.clear()

    def test_a_successful_fetch_prunes_before_the_update_returns(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")], options=AUTO)
        tree = _poll(_polling(hass, entry, _PollingClient([ALIVE])))
        self.assertEqual(hass.config_entries.removed, [f"sub-{GONE}"])
        self.assertIn(ALIVE, tree.by_id)

    def test_a_failed_fetch_prunes_nothing(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")], options=AUTO)
        client = _PollingClient([ALIVE], fetch_error=api.ProgressCoveError("unreachable"))
        with self.assertRaises(coordinator_module.UpdateFailed):
            _poll(_polling(hass, entry, client))
        self.assertEqual(client.asked, [])
        self.assertEqual(hass.config_entries.removed, [])

    def test_a_prune_that_breaks_is_not_swallowed(self):
        """A bug escapes to the coordinator, which logs it and reports the poll as failed,
        rather than leaving stale entities in place with nothing said."""
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")], options=AUTO)
        client = _PollingClient([ALIVE], raises=RuntimeError("bug"))
        with self.assertRaises(RuntimeError):
            _poll(_polling(hass, entry, client))
        self.assertEqual(hass.config_entries.removed, [])


class LapsedSubscriptionTest(unittest.TestCase):
    """A lapsed subscription is not a bad token: a reauth flow could never finish, so the update
    fails, keeps retrying, and a repair says what to do."""

    def setUp(self):
        _created.clear()
        _deleted.clear()
        registry = types.SimpleNamespace(async_create_issue=_create_issue,
                                         async_delete_issue=_delete_issue,
                                         IssueSeverity=types.SimpleNamespace(ERROR="error"))
        patcher = mock.patch.object(subscription_issue, "ir", registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lapsed(self):
        client = _PollingClient([ALIVE], fetch_error=api.ProgressCoveSubscriptionInactive("403"))
        coordinator = _polling(_Hass(), _Entry([]), client)
        with self.assertRaises(coordinator_module.UpdateFailed) as caught:
            _poll(coordinator)
        return coordinator, client, caught.exception

    def test_it_fails_the_update_rather_than_asking_for_a_token(self):
        _, _, error = self._lapsed()
        self.assertNotIsInstance(error, coordinator_module.ConfigEntryAuthFailed)
        self.assertEqual(error.translation_key, "subscription_inactive")

    def test_it_tries_again_by_itself_without_hammering_the_server(self):
        _, _, error = self._lapsed()
        self.assertEqual(error.retry_after, 300)

    def test_a_repair_says_why_and_how_to_renew(self):
        self._lapsed()
        issue = _created["subscription_inactive_entry-1"]
        self.assertEqual(issue["translation_key"], "subscription_inactive")
        self.assertFalse(issue["is_fixable"])

    def test_the_repair_goes_once_access_is_back(self):
        coordinator, client, _ = self._lapsed()
        client.fetch_error = None
        _poll(coordinator)
        self.assertNotIn("subscription_inactive_entry-1", _created)
        self.assertIn("subscription_inactive_entry-1", _deleted)

    def test_a_rejected_token_still_asks_for_a_new_one(self):
        client = _PollingClient([ALIVE], fetch_error=api.ProgressCoveAuthError("401"))
        with self.assertRaises(coordinator_module.ConfigEntryAuthFailed):
            _poll(_polling(_Hass(), _Entry([]), client))
        self.assertNotIn("subscription_inactive_entry-1", _created)


class UnchangedPollTest(unittest.TestCase):
    """A 304 on both reads: the same Tree comes back, rebuilt from nothing, and the prune, which
    reads only the tree, is not run again unless it still owes a look."""

    def setUp(self):
        _created.clear()
        _deleted.clear()

    def test_the_same_tree_comes_back_and_the_prune_is_skipped(self):
        entry = _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _PollingClient([ALIVE])
        coordinator = _polling(_Hass(), entry, client)
        first = _poll(coordinator)
        self.assertIs(_poll(coordinator), first)
        self.assertEqual(client.asked, [GONE])

    def test_a_new_my_day_rebuilds_the_tree_without_a_prune(self):
        entry = _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _PollingClient([ALIVE])
        coordinator = _polling(_Hass(), entry, client)
        first = _poll(coordinator)
        client.my_day = [{"id": ALIVE}]
        second = _poll(coordinator)
        self.assertIsNot(second, first)
        self.assertEqual(second.my_day, [{"id": ALIVE}])
        self.assertEqual(client.asked, [GONE])

    def test_a_new_tree_is_pruned(self):
        entry = _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _PollingClient([ALIVE])
        coordinator = _polling(_Hass(), entry, client)
        _poll(coordinator)
        client.nodes = [*client.nodes, {"id": "new", "name": "new"}]
        _poll(coordinator)
        self.assertEqual(client.asked, [GONE, GONE])

    def test_an_owed_prune_runs_on_an_unchanged_poll(self):
        entry = _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _PollingClient([ALIVE], raises=api.ProgressCoveError("timed out"))
        coordinator = _polling(_Hass(), entry, client)
        _poll(coordinator)
        client.raises = None
        _poll(coordinator)
        self.assertEqual(client.asked, [GONE, GONE])
        _poll(coordinator)
        self.assertEqual(client.asked, [GONE, GONE])

    def test_a_prune_that_raised_runs_again_on_an_unchanged_poll(self):
        entry = _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _PollingClient([ALIVE], raises=RuntimeError("bug"))
        coordinator = _polling(_Hass(), entry, client)
        with self.assertRaises(RuntimeError):
            _poll(coordinator)
        client.raises = None
        _poll(coordinator)
        self.assertEqual(client.asked, [GONE, GONE])


class StaleIssueCleanupTest(unittest.TestCase):
    def test_clearing_drops_both_repairs_for_that_entry(self):
        _created["stale_entities_entry-1"] = {}
        _created["unconfirmed_entities_entry-1"] = {}
        prune.clear_stale_issues(None, _Entry([]))
        self.assertNotIn("stale_entities_entry-1", _created)
        self.assertNotIn("unconfirmed_entities_entry-1", _created)


def _fix_flow(subentries, tree_ids, client, verdicts=None):
    entry = _Entry(subentries)
    entry.runtime_data = types.SimpleNamespace(
        data=_tree(*tree_ids), client=client, prune_verdicts=verdicts or {}
    )
    hass = _Hass(entry)
    flow = asyncio.run(repairs.async_create_fix_flow(
        hass, "stale_entities_entry-1", {"entry_id": entry.entry_id}))
    flow.hass = hass
    return flow, hass, entry


def _step(flow, step, user_input=None):
    return asyncio.run(getattr(flow, f"async_step_{step}")(user_input))


class RepairFlowTest(unittest.TestCase):
    """The one-step fix. It removes exactly what the server confirms is gone, at the moment the
    person confirms, and nothing on a partial answer."""

    def setUp(self):
        _created.clear()
        _deleted.clear()

    def test_it_names_the_entities_then_removes_exactly_those(self):
        subs = [_Subentry(GONE, "Bulk pickup"), _Subentry(ALIVE, "Water the plants"),
                _Subentry("truncated", "Feed the cat")]
        client = _Client(alive=(ALIVE, "truncated"))
        flow, hass, entry = _fix_flow(subs, [ALIVE], client)
        shown = _step(flow, "init")
        self.assertEqual(shown["step_id"], "confirm")
        self.assertEqual(shown["description_placeholders"], {"names": "Bulk pickup"})
        done = _step(flow, "confirm", {})
        self.assertEqual(done["type"], "create_entry")
        self.assertEqual(hass.config_entries.removed, [f"sub-{GONE}"])
        self.assertIn("stale_entities_entry-1", _deleted)

    def test_a_task_restored_before_the_confirm_keeps_its_entity(self):
        client = _Client()
        flow, hass, _ = _fix_flow([_Subentry(GONE, "Bulk pickup")], [ALIVE], client)
        _step(flow, "init")
        client.alive.add(GONE)
        _step(flow, "confirm", {})
        self.assertEqual(hass.config_entries.removed, [])

    def test_a_task_restored_before_the_repair_opens_ends_it(self):
        flow, hass, _ = _fix_flow([_Subentry(GONE, "Bulk pickup")], [ALIVE], _Client(alive=(GONE,)))
        self.assertEqual(_step(flow, "init"), {"type": "abort", "reason": "nothing_to_remove"})
        self.assertEqual(hass.config_entries.removed, [])
        self.assertIn("stale_entities_entry-1", _deleted)

    def test_a_check_that_fails_at_confirm_removes_nothing_and_says_so(self):
        subs = [_Subentry(GONE, "Bulk pickup"), _Subentry("also-gone", "Recycling")]
        client = _Client()
        flow, hass, _ = _fix_flow(subs, [ALIVE], client)
        _step(flow, "init")
        client.raises = api.ProgressCoveError("timed out")
        shown = _step(flow, "confirm", {})
        self.assertEqual(shown["errors"], {"base": "cannot_connect"})
        self.assertEqual(hass.config_entries.removed, [])
        self.assertNotIn("stale_entities_entry-1", _deleted)

    def test_a_check_that_fails_when_it_opens_removes_nothing(self):
        client = _Client(raises=api.ProgressCoveError("timed out"))
        flow, hass, _ = _fix_flow([_Subentry(GONE, "Bulk pickup")], [ALIVE], client)
        self.assertEqual(_step(flow, "init"), {"type": "abort", "reason": "cannot_connect"})
        self.assertEqual(hass.config_entries.removed, [])

    def test_one_removed_meanwhile_is_not_removed_twice(self):
        subs = [_Subentry(GONE, "Bulk pickup"), _Subentry("also-gone", "Recycling")]
        flow, hass, entry = _fix_flow(subs, [ALIVE], _Client())
        _step(flow, "init")
        entry.subentries.pop(f"sub-{GONE}")
        _step(flow, "confirm", {})
        self.assertEqual(hass.config_entries.removed, ["sub-also-gone"])

    def test_an_entry_that_is_not_loaded_is_not_touched(self):
        flow, hass, entry = _fix_flow([_Subentry(GONE, "Bulk pickup")], [ALIVE], _Client())
        entry.state = ha_stubs.ConfigEntryState.SETUP_RETRY
        self.assertEqual(_step(flow, "init"), {"type": "abort", "reason": "not_loaded"})
        self.assertEqual(hass.config_entries.removed, [])

    def test_the_ones_the_repair_named_are_checked_first(self):
        subs = [_Subentry(f"gone-{i}", f"Task {i}") for i in range(20)]
        named = {s.subentry_id: prune.Verdict.GONE for s in subs[-3:]}
        client = _Client()
        flow, _, _ = _fix_flow(subs, [ALIVE], client, verdicts=named)
        _step(flow, "init")
        self.assertEqual(client.asked[:3], [s.data["node_id"] for s in subs[-3:]])

    def test_it_asks_about_no_more_than_a_poll_would(self):
        subs = [_Subentry(f"gone-{i}", f"Task {i}") for i in range(20)]
        client = _Client()
        flow, _, _ = _fix_flow(subs, [ALIVE], client)
        _step(flow, "init")
        self.assertEqual(len(client.asked), prune.MAX_CHECKS_PER_REFRESH)


class MalformedNodeIdTest(unittest.TestCase):
    """The prune is the one caller that does NOT check its id against the live tree first.

    It asks about ids precisely when they are missing from it, read back out of .storage, and the
    id is interpolated into a URL path. A value carrying `../` or a query string would address an
    endpoint nobody meant to call, so api refuses it at the boundary.
    """

    def test_the_client_refuses_a_traversal_attempt(self):
        client = api.ProgressCoveClient(object(), "https://x", "t", None)
        for probe in ("../../admin", "abc/../nodes", "x?admin=1", "a b", ""):
            with self.subTest(probe):
                with self.assertRaises(api.ProgressCoveError):
                    asyncio.run(client.async_node_exists(probe))

    def test_every_path_built_from_an_id_refuses_one(self):
        client = api.ProgressCoveClient(object(), "https://x", "t", None)
        calls = {
            "complete": lambda node_id: client.async_complete(node_id),
            "uncomplete": lambda node_id: client.async_uncomplete(node_id),
            "update": lambda node_id: client.async_update_task(node_id, name="x"),
            "delete": lambda node_id: client.async_delete(node_id),
        }
        for label, call in calls.items():
            with self.subTest(label):
                with self.assertRaises(api.ProgressCoveError):
                    asyncio.run(call("../../admin"))

    def test_a_refusal_is_never_read_as_a_deletion(self):
        """It raises the plain error, not NotFound, so a corrupt stored id keeps its entity and
        surfaces as a repair rather than silently deleting it."""
        _created.clear()
        hass = _Hass()
        entry = _Entry([_Subentry("../../admin", "Suspicious")], options=AUTO)
        client = _Client(raises=api.ProgressCoveError("refusing malformed"))
        _run(hass, entry, client, _tree(ALIVE))
        self.assertEqual(hass.config_entries.removed, [])
        self.assertIn("unconfirmed_entities_entry-1", _created)


if __name__ == "__main__":
    unittest.main()
