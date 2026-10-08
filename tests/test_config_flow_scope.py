"""The setup and reconfigure conversations around a token's scope.

A read-only token is a legitimate choice, so setup warns and carries on. Reconfigure is how it is
swapped for a read and write one, which is only safe if the new token is paired like the first and
belongs to the same account.
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


class _ConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_update_reload_and_abort(self, entry, **kwargs):
        return {"type": "abort", "reason": "updated", "entry": entry, **kwargs}

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None

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
        ConfigFlowResult=dict)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
_module("homeassistant.helpers.selector", SelectSelector=_Selector, SelectSelectorConfig=_Selector,
        SelectOptionDict=dict, BooleanSelector=_Selector,
        SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown", LIST="list"))
_module("voluptuous", Schema=lambda *a, **k: None, Required=lambda *a, **k: None,
        Optional=lambda *a, **k: None, All=lambda *a, **k: None, Coerce=lambda *a, **k: None,
        Range=lambda *a, **k: None)
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
_load("names")
_load("base_url")
_load("picker_tree")
config_flow = _load("config_flow")

OLD_TOKEN = "pat_old_token_0000"
NEW_TOKEN = "pat_new_token_1111"
SERVER = "https://api.progresscove.com"
CREDENTIALS = {const.CONF_BASE_URL: SERVER, const.CONF_TOKEN: OLD_TOKEN}
NODES = [{"id": "home", "name": "Home"}, {"id": "bins", "name": "Bins", "parent_id": "home"}]


class _World:
    """What the server would say: which tokens authenticate, their scope, what they can see."""

    def __init__(self, scopes, nodes=NODES):
        self.scopes = scopes
        self.nodes = nodes


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
        if self.token not in _Client.world.scopes:
            raise api.ProgressCoveAuthError("not accepted")
        self.token_scope = _Client.world.scopes[self.token]

    async def async_get_tree(self):
        return _Client.world.nodes

    async def async_claim_pairing_code(self, code):
        _Client.claimed.append(code)


class _Entry:
    def __init__(self, nodes=("bins",), subentry_nodes=()):
        self.data = {
            const.CONF_BASE_URL: SERVER,
            const.CONF_TOKEN: OLD_TOKEN,
            const.CONF_DEVICE_ID: "device-1",
            const.CONF_PROJECTS: list(nodes),
        }
        self.subentries = {
            f"sub-{index}": types.SimpleNamespace(data={"node_id": node_id})
            for index, node_id in enumerate(subentry_nodes)
        }


def _flow(entry=None):
    flow = config_flow.ProgressCoveConfigFlow()
    flow.hass = object()
    flow.entry = entry
    return flow


def _run(coroutine):
    return asyncio.run(coroutine)


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self._real_client = config_flow.ProgressCoveClient
        config_flow.ProgressCoveClient = _Client
        _Client.created = []
        _Client.claimed = []
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ_WRITE})

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
        self.assertEqual(result["step_id"], "projects")

    def test_the_warning_is_shown_before_the_user_has_answered_it(self):
        flow, _ = self._connect(const.SCOPE_READ)
        result = _run(flow.async_step_read_only())
        self.assertEqual(result["step_id"], "read_only")

    def test_the_scope_is_stored_on_the_entry(self):
        flow, _ = self._connect(const.SCOPE_READ)
        _run(flow.async_step_read_only({}))
        created = _run(flow.async_step_projects({const.CONF_PROJECTS: ["bins"]}))
        self.assertEqual(created["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ)

    def test_a_read_and_write_token_goes_straight_to_the_lists(self):
        _, result = self._connect(const.SCOPE_READ_WRITE)
        self.assertEqual(result["step_id"], "projects")

    def test_a_read_and_write_scope_is_stored_too(self):
        flow, _ = self._connect(const.SCOPE_READ_WRITE)
        created = _run(flow.async_step_projects({const.CONF_PROJECTS: ["bins"]}))
        self.assertEqual(created["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ_WRITE)

    def test_a_server_that_reports_no_scope_gets_no_warning(self):
        _, result = self._connect(None)
        self.assertEqual(result["step_id"], "projects")

    def test_a_token_that_pairs_later_is_warned_after_pairing(self):
        _Client.world = _World({})
        flow = _flow()
        paired = _run(flow.async_step_user(CREDENTIALS))
        self.assertEqual(paired["step_id"], "pair")
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ})
        result = _run(flow.async_step_pair({}))
        self.assertEqual(result["step_id"], "read_only")


class ReconfigureTest(FlowTestCase):
    def _submit(self, entry, token=None, base_url=SERVER):
        flow = _flow(entry)
        _run(flow.async_step_reconfigure())
        user_input = {const.CONF_BASE_URL: base_url}
        if token is not None:
            user_input[const.CONF_TOKEN] = token
        return flow, _run(flow.async_step_reconfigure(user_input))

    def test_an_empty_token_keeps_the_current_one(self):
        _, result = self._submit(_Entry(), token="")
        self.assertEqual(result["data"][const.CONF_TOKEN], OLD_TOKEN)

    def test_an_omitted_token_keeps_the_current_one(self):
        _, result = self._submit(_Entry())
        self.assertEqual(result["data"][const.CONF_TOKEN], OLD_TOKEN)

    def test_the_address_can_still_change_on_its_own(self):
        _, result = self._submit(_Entry(), base_url="https://pc.example.test")
        self.assertEqual(result["data"][const.CONF_BASE_URL], "https://pc.example.test")

    def test_a_read_and_write_token_replaces_a_read_only_one(self):
        _Client.world = _World({OLD_TOKEN: const.SCOPE_READ, NEW_TOKEN: const.SCOPE_READ_WRITE})
        entry = _Entry()
        entry.data[const.CONF_TOKEN_SCOPE] = const.SCOPE_READ
        _, result = self._submit(entry, token=NEW_TOKEN)
        self.assertEqual(result["data"][const.CONF_TOKEN], NEW_TOKEN)
        self.assertEqual(result["data"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ_WRITE)

    def test_the_unique_id_follows_the_new_token(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE})
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual(result["unique_id"], f"{SERVER}::{NEW_TOKEN[:12]}")

    def test_the_entry_keeps_its_lists_and_installation_id(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ_WRITE})
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual(result["data"][const.CONF_PROJECTS], ["bins"])
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
        self.assertEqual(result["data"][const.CONF_TOKEN], NEW_TOKEN)
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
        _Client.world = _World(
            {NEW_TOKEN: const.SCOPE_READ_WRITE}, nodes=[{"id": "elsewhere", "name": "Other"}]
        )
        _, result = self._submit(_Entry(), token=NEW_TOKEN)
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(result["errors"], {"base": "different_account"})

    def test_a_token_for_another_account_is_refused_after_pairing_too(self):
        _Client.world = _World({})
        flow, _ = self._submit(_Entry(), token=NEW_TOKEN)
        _Client.world = _World(
            {NEW_TOKEN: const.SCOPE_READ_WRITE}, nodes=[{"id": "elsewhere", "name": "Other"}]
        )
        result = _run(flow.async_step_pair({}))
        self.assertEqual(result["errors"], {"base": "different_account"})

    def test_buttons_count_as_belonging_to_the_account(self):
        _Client.world = _World(
            {NEW_TOKEN: const.SCOPE_READ_WRITE}, nodes=[{"id": "plants", "name": "Plants"}]
        )
        _, result = self._submit(_Entry(nodes=(), subentry_nodes=("plants",)), token=NEW_TOKEN)
        self.assertEqual(result["data"][const.CONF_TOKEN], NEW_TOKEN)

    def test_an_entry_with_nothing_chosen_has_no_account_to_compare(self):
        _Client.world = _World(
            {NEW_TOKEN: const.SCOPE_READ_WRITE}, nodes=[{"id": "elsewhere", "name": "Other"}]
        )
        _, result = self._submit(_Entry(nodes=()), token=NEW_TOKEN)
        self.assertEqual(result["data"][const.CONF_TOKEN], NEW_TOKEN)

    def test_the_same_token_is_never_called_another_account(self):
        _Client.world = _World(
            {OLD_TOKEN: const.SCOPE_READ_WRITE}, nodes=[{"id": "gone", "name": "Other"}]
        )
        _, result = self._submit(_Entry(), token="")
        self.assertEqual(result["data"][const.CONF_TOKEN], OLD_TOKEN)


class ReauthTest(FlowTestCase):
    def test_a_replacement_token_stores_its_scope(self):
        _Client.world = _World({NEW_TOKEN: const.SCOPE_READ})
        flow = _flow(_Entry())
        result = _run(flow.async_step_reauth_confirm({const.CONF_TOKEN: NEW_TOKEN}))
        self.assertEqual(result["data_updates"][const.CONF_TOKEN_SCOPE], const.SCOPE_READ)


if __name__ == "__main__":
    unittest.main()
