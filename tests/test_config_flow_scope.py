"""The setup, reconfigure and reauth conversations around a token's scope and its account.

A read-only token is a legitimate choice, so setup warns and carries on. Reconfigure is how it is
swapped for a read and write one, which is only safe if the new token is paired like the first and
belongs to the same account. The account is named by its inbox id, which keys the entry.
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


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    mod.__path__ = getattr(mod, "__path__", [])
    for key, value in attrs.items():
        if not hasattr(mod, key):
            setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _AbortFlow(Exception):
    """Home Assistant's AbortFlow: raised from a step, and shown as the abort reason's text."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _ConfigFlow:
    # The unique ids of the entries that already exist.
    configured: set[str] = set()

    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_update_and_abort(self, entry, *, data):
        """Saves as Home Assistant does: the listener of a loaded entry hears about a change."""
        if data != entry.data:
            entry.data = data
            if entry.state == ha_stubs.ConfigEntryState.LOADED:
                entry.listener_calls += 1
        return {"type": "abort", "reason": "updated", "entry": entry, "data": data}

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        if self.unique_id in _ConfigFlow.configured:
            raise _AbortFlow("already_configured")

    def _abort_if_unique_id_mismatch(self, *, reason="unique_id_mismatch"):
        if self.entry.unique_id != self.unique_id:
            raise _AbortFlow(reason)

    def _get_reconfigure_entry(self):
        return self.entry

    def _get_reauth_entry(self):
        return self.entry


class _Selector:
    def __init__(self, *args, **kwargs):
        pass


_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=object, ConfigFlow=_ConfigFlow,
        ConfigSubentryFlow=object, OptionsFlow=object, SubentryFlowResult=dict,
        ConfigFlowResult=dict, ConfigSubentry=ha_stubs.ConfigSubentry, ConfigSubentryData=dict)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
_module("homeassistant.helpers.selector", SelectSelector=_Selector, SelectSelectorConfig=_Selector,
        SelectOptionDict=dict, BooleanSelector=_Selector,
        SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown", LIST="list"),
        TextSelector=_Selector, TextSelectorConfig=_Selector,
        TextSelectorType=types.SimpleNamespace(PASSWORD="password"))
_module("voluptuous", Schema=lambda *a, **k: None, Required=lambda *a, **k: None,
        Optional=lambda *a, **k: None, All=lambda *a, **k: None, Coerce=lambda *a, **k: None,
        Range=lambda *a, **k: None)
_module("aiohttp", ClientError=type("ClientError", (Exception,), {}),
        ClientSession=object, ClientResponse=object)

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


const = _load("const")
CONF_TOKEN = sys.modules["homeassistant.const"].CONF_TOKEN
CONF_URL = sys.modules["homeassistant.const"].CONF_URL
api = _load("api")
_load("names")
_load("base_url")
_load("picker_tree")
config_flow = _load("config_flow")

OLD_TOKEN = "pat_old_token_0000"
NEW_TOKEN = "pat_new_token_1111"
SERVER = "https://api.progresscove.com"
CREDENTIALS = {CONF_URL: SERVER, CONF_TOKEN: OLD_TOKEN}
NODES = [{"id": "home", "name": "Home"}, {"id": "bins", "name": "Bins", "parent_id": "home"}]
ACCOUNT = "0b9d7a52-55a4-4c3e-9d0f-2f3c1c9e6a11"
OTHER_ACCOUNT = "7e1c2f40-8d3b-4f6a-a2c5-9b8e0d4f3a22"


class _World:
    """What the server would say: which tokens authenticate, their scope, what they can see, and
    the inbox id of the account behind them."""

    def __init__(self, scopes, nodes=NODES, account=ACCOUNT, inbox_error=None, refusal=None):
        self.scopes = scopes
        self.refusal = refusal
        self.nodes = nodes
        self.account = account
        self.inbox_error = inbox_error


class _Client:
    world = _World({})
    created = []
    claimed = []

    def __init__(self, session, base_url, token, device_id=None):
        self.token = token
        self.device_id = device_id
        self.token_scope = None
        _Client.created.append(self)

    async def async_check_credentials(self):
        if _Client.world.refusal is not None:
            raise _Client.world.refusal
        if self.token not in _Client.world.scopes:
            raise api.ProgressCoveAuthError("not accepted")
        self.token_scope = _Client.world.scopes[self.token]

    async def async_get_tree(self):
        return _Client.world.nodes

    async def async_get_inbox_id(self):
        if _Client.world.inbox_error is not None:
            raise _Client.world.inbox_error
        return _Client.world.account

    async def async_claim_pairing_code(self, code):
        _Client.claimed.append(code)


class _Entry:
    unique_id = ACCOUNT
    entry_id = "entry-1"

    def __init__(self, state=ha_stubs.ConfigEntryState.LOADED):
        self.data = {
            CONF_URL: SERVER,
            CONF_TOKEN: OLD_TOKEN,
            const.CONF_DEVICE_ID: "device-1",
        }
        self.state = state
        # Each one is a reload: the update listener reloads a loaded entry whose data changed.
        self.listener_calls = 0


class _ConfigEntries:
    def __init__(self):
        self.reloads = []

    def async_schedule_reload(self, entry_id):
        self.reloads.append(entry_id)


def _flow(entry=None):
    flow = config_flow.ProgressCoveConfigFlow()
    flow.hass = types.SimpleNamespace(config_entries=_ConfigEntries())
    flow.entry = entry
    return flow


def _reloads(flow, entry):
    """How many times saving reloads the entry, whichever side starts each one."""
    return len(flow.hass.config_entries.reloads) + entry.listener_calls


def _run(coroutine):
    return asyncio.run(coroutine)


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self._real_client = config_flow.ProgressCoveClient
        config_flow.ProgressCoveClient = _Client
        _Client.created = []
        _Client.claimed = []
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ_WRITE})
        _ConfigFlow.configured = set()

    def tearDown(self):
        config_flow.ProgressCoveClient = self._real_client


class SetupWarningTest(FlowTestCase):
    def _connect(self, scope):
        _Client.world = _World({OLD_TOKEN: scope})
        flow = _flow()
        result = _run(flow.async_step_user(CREDENTIALS))
        return flow, result

    def test_a_read_only_token_gets_the_warning_step(self):
        _, result = self._connect(const.SCOPE_READ)
        self.assertEqual(result["step_id"], "read_only")

    def test_the_warning_lets_the_user_continue_to_the_lists(self):
        flow, _ = self._connect(const.SCOPE_READ)
        result = _run(flow.async_step_read_only({}))
        self.assertEqual(result["step_id"], "lists")

    def test_the_warning_is_shown_before_the_user_has_answered_it(self):
        flow, _ = self._connect(const.SCOPE_READ)
        result = _run(flow.async_step_read_only())
        self.assertEqual(result["step_id"], "read_only")

    def test_the_scope_is_stored_on_the_entry(self):
        flow, _ = self._connect(const.SCOPE_READ)
        _run(flow.async_step_read_only({}))
        created = _run(flow.async_step_lists({const.CONF_LISTS: ["bins"]}))
        self.assertEqual(created["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ)

    def test_a_read_and_write_token_goes_straight_to_the_lists(self):
        _, result = self._connect(const.SCOPE_READ_WRITE)
        self.assertEqual(result["step_id"], "lists")

    def test_a_read_and_write_scope_is_stored_too(self):
        flow, _ = self._connect(const.SCOPE_READ_WRITE)
        created = _run(flow.async_step_lists({const.CONF_LISTS: ["bins"]}))
        self.assertEqual(created["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ_WRITE)

    def test_a_server_that_reports_no_scope_gets_no_warning(self):
        _, result = self._connect(None)
        self.assertEqual(result["step_id"], "lists")

    def test_a_token_that_pairs_later_is_warned_after_pairing(self):
        _Client.world = _World({})
        flow = _flow()
        paired = _run(flow.async_step_user(CREDENTIALS))
        self.assertEqual(paired["step_id"], "pair")
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ})
        result = _run(flow.async_step_pair({}))
        self.assertEqual(result["step_id"], "read_only")


class ListsAtSetupTest(FlowTestCase):
    """The lists chosen at setup are list subentries of the new entry, the same thing "Manage
    lists" adds later, and nothing about them is kept in the entry's own data."""

    NODES = [
        {"id": "home", "name": "Home"},
        {"id": "shop", "name": "Shop \u203a ping", "parent_id": "home"},
        {"id": "bins", "name": "Bins", "parent_id": "home"},
        {"id": "bin-day", "name": "Bin day", "parent_id": "bins"},
        # The inbox, which is the account, holding a capture: a node with items.
        {"id": ACCOUNT, "name": "Unsorted", "parent_id": "home"},
        {"id": "captured", "name": "Call the plumber", "parent_id": ACCOUNT},
    ]

    def _flow(self):
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ_WRITE}, nodes=self.NODES)
        flow = _flow()
        return flow, _run(flow.async_step_user(CREDENTIALS))

    def _create(self, chosen):
        flow, _ = self._flow()
        return _run(flow.async_step_lists({const.CONF_LISTS: chosen}))

    def _form_field(self):
        """(pre-ticked, offered) from the form, read through stand-ins for the schema pieces."""
        captured = {}
        real_vol, real_selector = config_flow.vol, config_flow._list_selector

        def required(key, default=None):
            captured["ticked"] = default
            return key

        def offered(labels):
            captured["offered"] = set(labels)

        config_flow.vol = types.SimpleNamespace(Schema=lambda fields: fields, Required=required)
        config_flow._list_selector = offered
        try:
            flow, _ = self._flow()
            _run(flow.async_step_lists())
        finally:
            config_flow.vol, config_flow._list_selector = real_vol, real_selector
        return captured["ticked"], captured["offered"]

    def test_the_inbox_is_not_pre_ticked_even_when_it_holds_items(self):
        ticked, _ = self._form_field()
        self.assertEqual(sorted(ticked), ["bins", "home"])

    def test_the_inbox_and_what_sits_in_it_are_not_offered(self):
        _, offered = self._form_field()
        self.assertNotIn(ACCOUNT, offered)
        self.assertNotIn("captured", offered)
        self.assertIn("bin-day", offered)

    def test_the_inbox_is_refused_if_submitted_anyway(self):
        for node_id in (ACCOUNT, "captured"):
            with self.subTest(node_id):
                result = self._create(["bins", node_id])
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {const.CONF_LISTS: "not_selectable"})

    def test_each_chosen_node_becomes_a_list_subentry(self):
        created = self._create(["bins", "shop"])
        self.assertEqual(created["subentries"], [
            {"data": {const.CONF_NODE_ID: "shop"}, "subentry_type": const.SUBENTRY_LIST,
             "title": "Shop / ping", "unique_id": "list_shop"},
            {"data": {const.CONF_NODE_ID: "bins"}, "subentry_type": const.SUBENTRY_LIST,
             "title": "Bins", "unique_id": "list_bins"},
        ])

    def test_the_entry_data_holds_no_list_choice(self):
        created = self._create(["bins"])
        self.assertNotIn(const.CONF_LISTS, created["data"])
        self.assertNotIn("projects", created["data"])

    def test_choosing_none_creates_no_subentries(self):
        self.assertEqual(self._create([])["subentries"], [])


class AccountIdTest(FlowTestCase):
    """One entry per account, keyed by the account's inbox id rather than anything the user typed:
    a second token or another address for the same account is still the same account."""

    def test_the_entry_is_keyed_by_the_inbox_id(self):
        flow = _flow()
        _run(flow.async_step_user(CREDENTIALS))
        self.assertEqual(flow.unique_id, ACCOUNT)

    def test_an_account_already_set_up_is_refused_before_the_lists(self):
        _ConfigFlow.configured = {ACCOUNT}
        with self.assertRaises(_AbortFlow) as caught:
            _run(_flow().async_step_user(CREDENTIALS))
        self.assertEqual(caught.exception.reason, "already_configured")

    def test_an_account_already_set_up_is_refused_after_pairing_too(self):
        _ConfigFlow.configured = {ACCOUNT}
        _Client.world = _World({})
        flow = _flow()
        _run(flow.async_step_user(CREDENTIALS))
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ_WRITE})
        with self.assertRaises(_AbortFlow) as caught:
            _run(flow.async_step_pair({}))
        self.assertEqual(caught.exception.reason, "already_configured")

    def test_another_account_on_the_same_server_is_set_up(self):
        _ConfigFlow.configured = {OTHER_ACCOUNT}
        result = _run(_flow().async_step_user(CREDENTIALS))
        self.assertEqual(result["step_id"], "lists")

    def test_an_unreachable_inbox_is_a_connection_error_and_sets_no_id(self):
        _Client.world = _World(
            {OLD_TOKEN: const.SCOPE_READ_WRITE}, inbox_error=api.ProgressCoveError("500")
        )
        flow = _flow()
        result = _run(flow.async_step_user(CREDENTIALS))
        self.assertEqual(result["step_id"], "user")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertFalse(hasattr(flow, "unique_id"))

    def test_a_refused_inbox_is_treated_like_a_refused_token(self):
        _Client.world = _World(
            {OLD_TOKEN: const.SCOPE_READ_WRITE}, inbox_error=api.ProgressCoveAuthError("401")
        )
        flow = _flow()
        result = _run(flow.async_step_user(CREDENTIALS))
        self.assertEqual(result["step_id"], "pair")
        self.assertFalse(hasattr(flow, "unique_id"))


class ReconfigureTest(FlowTestCase):
    def _submit(self, entry, token=None, base_url=SERVER):
        flow = _flow(entry)
        _run(flow.async_step_reconfigure())
        user_input = {CONF_URL: base_url}
        if token is not None:
            user_input[CONF_TOKEN] = token
        return flow, _run(flow.async_step_reconfigure(user_input))

    def test_an_empty_token_keeps_the_current_one(self):
        _, result = self._submit(_Entry(), token="")
        self.assertEqual(result["data"][CONF_TOKEN], OLD_TOKEN)

    def test_a_new_address_reloads_the_entry_once(self):
        entry = _Entry()
        flow, _ = self._submit(entry, base_url="https://pc.example.test")
        self.assertEqual(_reloads(flow, entry), 1)

    def test_saving_what_is_already_there_reloads_once(self):
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ_WRITE})
        entry = _Entry()
        entry.data[const.CONF_TOKEN_SCOPE] = const.SCOPE_READ_WRITE
        flow, _ = self._submit(entry, token="")
        self.assertEqual(_reloads(flow, entry), 1)

    def test_an_omitted_token_keeps_the_current_one(self):
        _, result = self._submit(_Entry())
        self.assertEqual(result["data"][CONF_TOKEN], OLD_TOKEN)

    def test_the_address_can_still_change_on_its_own(self):
        _, result = self._submit(_Entry(), base_url="https://pc.example.test")
        self.assertEqual(result["data"][CONF_URL], "https://pc.example.test")

    def test_a_read_and_write_token_replaces_a_read_only_one(self):
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ, NEW_TOKEN: const.SCOPE_READ_WRITE})
        entry = _Entry()
        entry.data[const.CONF_TOKEN_SCOPE] = const.SCOPE_READ
        _, result = self._submit(entry, token=NEW_TOKEN)
        self.assertEqual(result["data"][CONF_TOKEN], NEW_TOKEN)
        self.assertEqual(result["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ_WRITE)

    def test_the_unique_id_is_left_alone(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE})
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertNotIn("unique_id", result)

    def test_the_entry_keeps_its_lists_and_installation_id(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE})
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertNotIn("subentries", result)
        self.assertEqual(result["data"][const.CONF_DEVICE_ID], "device-1")

    def test_the_token_is_checked_as_the_installation_it_belongs_to(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE})
        self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual({client.device_id for client in _Client.created}, {"device-1"})

    def test_a_token_that_is_not_paired_yet_goes_to_pairing(self):
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ})
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual(result["step_id"], "pair")
        self.assertEqual(len(_Client.claimed), 1)

    def test_pairing_then_saves_the_new_token(self):
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ})
        flow, _ = self._submit(_Entry(), token=NEW_TOKEN)
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ, NEW_TOKEN: const.SCOPE_READ_WRITE})
        result = _run(flow.async_step_pair({}))
        self.assertEqual(result["data"][CONF_TOKEN], NEW_TOKEN)
        self.assertEqual(result["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ_WRITE)

    def test_pairing_that_is_not_confirmed_says_so_and_saves_nothing(self):
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ})
        flow, _ = self._submit(_Entry(), token=NEW_TOKEN)
        result = _run(flow.async_step_pair({}))
        self.assertEqual(result["step_id"], "pair")
        self.assertEqual(result["errors"], {"base": "not_paired_yet"})

    def test_the_current_token_failing_is_an_error_not_a_pairing(self):
        _Client.world = _World({})
        _, result = self._submit(_Entry(), token="")
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})

    def test_a_token_for_another_account_is_refused(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE}, account=OTHER_ACCOUNT)
        with self.assertRaises(_AbortFlow) as caught:
            self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual(caught.exception.reason, "wrong_account")

    def test_a_token_for_another_account_is_refused_after_pairing_too(self):
        _Client.world = _World({})
        flow, _ = self._submit(_Entry(), token=NEW_TOKEN)
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE}, account=OTHER_ACCOUNT)
        with self.assertRaises(_AbortFlow) as caught:
            _run(flow.async_step_pair({}))
        self.assertEqual(caught.exception.reason, "wrong_account")

    def test_the_account_decides_not_the_nodes_it_holds(self):
        """Every configured node deleted in the app is still the same account."""
        _Client.world = _World(
            {NEW_TOKEN: const.SCOPE_READ_WRITE}, nodes=[{"id": "elsewhere", "name": "Other"}]
        )
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual(result["data"][CONF_TOKEN], NEW_TOKEN)

    def test_an_unreachable_inbox_saves_nothing(self):
        _Client.world = _World(
            {NEW_TOKEN: const.SCOPE_READ_WRITE}, inbox_error=api.ProgressCoveError("500")
        )
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})


class ReauthTest(FlowTestCase):
    def test_a_replacement_token_stores_its_scope(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ})
        flow = _flow(_Entry())
        result = _run(flow.async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(result["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ)

    def test_a_new_token_reloads_the_entry_once(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE})
        entry = _Entry()
        flow = _flow(entry)
        _run(flow.async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(entry.data[CONF_TOKEN], NEW_TOKEN)
        self.assertEqual(_reloads(flow, entry), 1)

    def test_the_same_token_once_paired_still_reloads_once(self):
        """Pairing changes nothing in the entry, so no listener hears of it: the flow reloads."""
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ_WRITE})
        entry = _Entry()
        entry.data[const.CONF_TOKEN_SCOPE] = const.SCOPE_READ_WRITE
        flow = _flow(entry)
        _run(flow.async_step_reauth_confirm({CONF_TOKEN: OLD_TOKEN}))
        self.assertEqual(_reloads(flow, entry), 1)

    def test_an_entry_that_failed_setup_is_reloaded_once(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE})
        entry = _Entry(state=ha_stubs.ConfigEntryState.SETUP_RETRY)
        flow = _flow(entry)
        _run(flow.async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(_reloads(flow, entry), 1)

    def test_a_token_for_another_account_is_refused(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE}, account=OTHER_ACCOUNT)
        flow = _flow(_Entry())
        with self.assertRaises(_AbortFlow) as caught:
            _run(flow.async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(caught.exception.reason, "wrong_account")

    def test_a_token_for_another_account_is_refused_after_pairing_too(self):
        _Client.world = _World({})
        flow = _flow(_Entry())
        paired = _run(flow.async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(paired["step_id"], "pair")
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE}, account=OTHER_ACCOUNT)
        with self.assertRaises(_AbortFlow) as caught:
            _run(flow.async_step_pair({}))
        self.assertEqual(caught.exception.reason, "wrong_account")

    def test_an_unreachable_inbox_saves_nothing(self):
        _Client.world = _World(
            {NEW_TOKEN: const.SCOPE_READ_WRITE}, inbox_error=api.ProgressCoveError("500")
        )
        flow = _flow(_Entry())
        result = _run(flow.async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(result["step_id"], "reauth_confirm")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})



class LapsedSubscriptionFlowTest(FlowTestCase):
    """A lapsed subscription is said as such. It is not a token to pair or replace."""

    def setUp(self):
        super().setUp()
        _Client.world = _World(
            {OLD_TOKEN: const.SCOPE_READ_WRITE, NEW_TOKEN: const.SCOPE_READ_WRITE},
            refusal=api.ProgressCoveSubscriptionInactive("403"),
        )

    def test_setup_says_so_instead_of_offering_pairing(self):
        result = _run(_flow().async_step_user(CREDENTIALS))
        self.assertEqual(result["step_id"], "user")
        self.assertEqual(result["errors"], {"base": "subscription_inactive"})
        self.assertEqual(_Client.claimed, [])

    def test_reauth_says_so_instead_of_offering_pairing(self):
        result = _run(_flow(_Entry()).async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(result["step_id"], "reauth_confirm")
        self.assertEqual(result["errors"], {"base": "subscription_inactive"})

    def test_the_error_has_words(self):
        catalogue = json.loads((COMPONENT / "strings.json").read_text())
        self.assertIn("subscription_inactive", catalogue["config"]["error"])


class UnexpectedErrorTest(FlowTestCase):
    """A failure nobody classified still reaches the form, and its traceback reaches the log."""

    def _broken(self):
        _Client.world = _World(
            {OLD_TOKEN: const.SCOPE_READ_WRITE, NEW_TOKEN: const.SCOPE_READ_WRITE},
            inbox_error=KeyError("id"),
        )

    def test_setup_says_something_went_wrong(self):
        self._broken()
        flow = _flow()
        with self.assertLogs(config_flow._LOGGER, level="ERROR") as logged:
            result = _run(flow.async_step_user(CREDENTIALS))
        self.assertEqual(result["errors"], {"base": "unknown"})
        self.assertIn("Traceback", logged.output[0])
        self.assertFalse(hasattr(flow, "unique_id"))

    def test_reauth_says_something_went_wrong(self):
        self._broken()
        flow = _flow(_Entry())
        with self.assertLogs(config_flow._LOGGER, level="ERROR"):
            result = _run(flow.async_step_reauth_confirm({CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(result["step_id"], "reauth_confirm")
        self.assertEqual(result["errors"], {"base": "unknown"})

    def test_the_error_has_words(self):
        catalogue = json.loads((COMPONENT / "strings.json").read_text())
        self.assertIn("unknown", catalogue["config"]["error"])


if __name__ == "__main__":
    unittest.main()
