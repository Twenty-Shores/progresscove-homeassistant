"""Pruning deletes a user's configured entity, so every guard on it is tested.

The failure that matters is not "a dead entity survived", it is "a live one was deleted because a
poll went wrong". Only an explicit 404 removes anything; everything else, including what looks like
an empty account, leaves the subentries alone.
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
_module("homeassistant.const", MAJOR_VERSION=2026, MINOR_VERSION=8,
        Platform=type("Platform", (), {"TODO": "todo", "BUTTON": "button",
                                       "SWITCH": "switch", "SENSOR": "sensor"}))
_module("homeassistant.exceptions",
        ConfigEntryError=type("ConfigEntryError", (Exception,), {}),
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}),
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
        ServiceValidationError=type("ServiceValidationError", (Exception,), {}))
_module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
_module("homeassistant.helpers.update_coordinator",
        CoordinatorEntity=type("CoordinatorEntity", (), {
            "__class_getitem__": classmethod(lambda c, i: c)}),
        DataUpdateCoordinator=type("DataUpdateCoordinator", (), {
            "__class_getitem__": classmethod(lambda c, i: c)}),
        UpdateFailed=type("UpdateFailed", (Exception,), {}))
_module("homeassistant.helpers.config_validation", string=str)
_module("voluptuous", Schema=lambda *a, **k: None, Required=lambda *a, **k: None)

_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_MISSING = object()

api = _load("api")
_load("const")
prune = _load("prune")
helpers = _load("helpers")

ALIVE = "alive-node"
GONE = "gone-node"


class _Subentry:
    def __init__(self, node_id, title, subentry_type="button"):
        self.data = {"node_id": node_id}
        self.title = title
        self.subentry_type = subentry_type
        self.subentry_id = f"sub-{node_id}"


class _Entry:
    entry_id = "entry-1"

    def __init__(self, subentries, options=None):
        self.subentries = {s.subentry_id: s for s in subentries}
        self.options = options or {}


class _Entries:
    def __init__(self):
        self.removed = []

    def async_remove_subentry(self, entry, subentry_id):
        self.removed.append(subentry_id)
        entry.subentries.pop(subentry_id, None)


class _Hass:
    def __init__(self):
        self.config_entries = _Entries()


class _Client:
    """Answers the targeted existence check however the test needs."""

    def __init__(self, alive=(), raises=None):
        self._alive = set(alive)
        self._raises = raises
        self.asked = []

    async def async_node_exists(self, node_id):
        self.asked.append(node_id)
        if self._raises is not None:
            raise self._raises
        if node_id in self._alive:
            return True
        raise api.ProgressCoveNotFound(f"not found: /nodes/{node_id}")


class _Coordinator:
    def __init__(self, tree_ids, client, success=True):
        self.client = client
        self.last_update_success = success
        self.data = types.SimpleNamespace(by_id={n: {"id": n} for n in tree_ids})


def _run(hass, entry, coordinator):
    asyncio.run(prune.async_prune_deleted(hass, entry, coordinator))


class PruneTest(unittest.TestCase):
    def setUp(self):
        _created.clear()
        _deleted.clear()

    def test_a_confirmed_404_removes_the_subentry(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _Client(alive=())
        _run(hass, entry, _Coordinator([ALIVE], client))
        self.assertEqual(hass.config_entries.removed, [f"sub-{GONE}"])

    def test_a_node_still_in_the_tree_is_never_even_asked_about(self):
        hass, entry = _Hass(), _Entry([_Subentry(ALIVE, "Water the plants")])
        client = _Client(alive=(ALIVE,))
        _run(hass, entry, _Coordinator([ALIVE], client))
        self.assertEqual(client.asked, [])
        self.assertEqual(hass.config_entries.removed, [])

    def test_a_200_keeps_a_subentry_missing_from_a_truncated_tree(self):
        """The node vanished from the bulk response but the server says it is there. That is a bad
        tree, not a deletion, and deleting on it would be the whole failure mode."""
        hass, entry = _Hass(), _Entry([_Subentry(ALIVE, "Water the plants")])
        client = _Client(alive=(ALIVE,))
        _run(hass, entry, _Coordinator(["something-else"], client))
        self.assertEqual(client.asked, [ALIVE])
        self.assertEqual(hass.config_entries.removed, [])

    def test_a_network_failure_removes_nothing_and_raises_a_repair(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _Client(raises=api.ProgressCoveError("connection reset"))
        _run(hass, entry, _Coordinator([ALIVE], client))
        self.assertEqual(hass.config_entries.removed, [])
        self.assertIn("stale_entities_entry-1", _created)

    def test_an_auth_failure_removes_nothing(self):
        """It should never get here (a failed refresh returns early), so this pins the belt as well
        as the braces: an auth error is not a 404 and cannot delete anything."""
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _Client(raises=api.ProgressCoveAuthError("401"))
        _run(hass, entry, _Coordinator([ALIVE], client))
        self.assertEqual(hass.config_entries.removed, [])

    def test_a_failed_refresh_prunes_nothing(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")])
        client = _Client(alive=())
        _run(hass, entry, _Coordinator([ALIVE], client, success=False))
        self.assertEqual(client.asked, [])
        self.assertEqual(hass.config_entries.removed, [])

    def test_an_empty_tree_is_never_read_as_everything_was_deleted(self):
        """The single most destructive misreading available: an empty or lost payload looks exactly
        like an emptied account, and acting on it would delete every entity the user configured."""
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "A"), _Subentry(ALIVE, "B")])
        client = _Client(alive=())
        _run(hass, entry, _Coordinator([], client))
        self.assertEqual(client.asked, [])
        self.assertEqual(hass.config_entries.removed, [])

    def test_auto_prune_off_keeps_it_and_says_so(self):
        hass = _Hass()
        entry = _Entry([_Subentry(GONE, "Bulk pickup")], options={"auto_prune": False})
        _run(hass, entry, _Coordinator([ALIVE], _Client(alive=())))
        self.assertEqual(hass.config_entries.removed, [])
        self.assertIn("Bulk pickup", _created["stale_entities_entry-1"]
                      ["translation_placeholders"]["names"])

    def test_the_repair_clears_once_nothing_is_stale(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Bulk pickup")])
        _run(hass, entry, _Coordinator([ALIVE], _Client(alive=())))
        # The subentry is gone now, so a second pass has nothing to report.
        _run(hass, entry, _Coordinator([ALIVE], _Client(alive=())))
        self.assertIn("stale_entities_entry-1", _deleted)

    def test_a_burst_of_deletions_is_rate_limited(self):
        """Deleting a 40-task project in the app must not fire 40 requests in one poll."""
        subs = [_Subentry(f"gone-{i}", f"Task {i}") for i in range(20)]
        hass, entry = _Hass(), _Entry(subs)
        client = _Client(alive=())
        _run(hass, entry, _Coordinator([ALIVE], client))
        self.assertEqual(len(client.asked), prune.MAX_CHECKS_PER_REFRESH)

    def test_all_confirmed_removals_happen_after_the_checks(self):
        """Each removal notifies the update listener, which reloads the entry, and a reload cancels
        every completion still inside its undo window. Removing inside the loop would reload once
        per entity while later checks were still running against the entry being torn down."""
        subs = [_Subentry(f"gone-{i}", f"Task {i}") for i in range(3)]
        hass, entry = _Hass(), _Entry(subs)

        order = []
        client = _Client(alive=())
        real_ask = client.async_node_exists

        async def watched(node_id):
            order.append(("ask", node_id))
            return await real_ask(node_id)

        client.async_node_exists = watched
        real_remove = hass.config_entries.async_remove_subentry
        hass.config_entries.async_remove_subentry = lambda e, sid: (
            order.append(("remove", sid)), real_remove(e, sid))[1]

        _run(hass, entry, _Coordinator([ALIVE], client))
        kinds = [kind for kind, _ in order]
        self.assertEqual(kinds, ["ask"] * 3 + ["remove"] * 3, order)

    def test_a_list_subentry_is_left_to_the_todo_platform(self):
        hass, entry = _Hass(), _Entry([_Subentry(GONE, "Groceries", subentry_type="list")])
        client = _Client(alive=())
        _run(hass, entry, _Coordinator([ALIVE], client))
        self.assertEqual(client.asked, [])
        self.assertEqual(hass.config_entries.removed, [])


if __name__ == "__main__":
    unittest.main()


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

    def test_a_refusal_is_never_read_as_a_deletion(self):
        """It raises the plain error, not NotFound, so a corrupt stored id keeps its entity and
        surfaces as a repair rather than silently deleting it."""
        hass, entry = _Hass(), _Entry([_Subentry("../../admin", "Suspicious")])
        client = _Client(raises=api.ProgressCoveError("refusing malformed"))
        _run(hass, entry, _Coordinator([ALIVE], client))
        self.assertEqual(hass.config_entries.removed, [])
        self.assertIn("stale_entities_entry-1", _created)


class ScanMinutesTest(unittest.TestCase):
    """The options flow bounds the FORM. This bounds what is read back out of .storage.

    A hand-edited 0 would poll continuously and a string would take setup down inside timedelta,
    so the value is clamped where it is consumed rather than only where it is typed.
    """

    def _minutes(self, stored):
        return helpers.scan_minutes({} if stored is _MISSING else {"scan_minutes": stored})

    def test_a_sane_value_is_kept(self):
        self.assertEqual(self._minutes(15), 15)

    def test_zero_and_negatives_cannot_produce_a_hot_loop(self):
        for bad in (0, -1, -3600):
            with self.subTest(bad):
                self.assertGreaterEqual(self._minutes(bad), 1)

    def test_an_absurd_value_is_capped(self):
        self.assertEqual(self._minutes(10**9), 60)

    def test_unreadable_values_fall_back_rather_than_crashing_setup(self):
        for bad in ("abc", None, [], {"x": 1}):
            with self.subTest(bad):
                self.assertEqual(self._minutes(bad), 1)

    def test_a_numeric_string_is_accepted(self):
        self.assertEqual(self._minutes("5"), 5)
