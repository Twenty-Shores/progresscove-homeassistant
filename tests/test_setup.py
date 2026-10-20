"""What the integration sets up once, what each entry sets up, and what removing an entry clears.

Actions belong to the integration rather than to an entry: one registered per entry vanished while
that entry was unloaded, and an automation naming it failed as "unknown action" instead of saying
the account was unavailable.
"""
import asyncio
import importlib.util
import inspect
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import ha_stubs

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"

CONFIG_ENTRY_ONLY = object()

ha_stubs.fill("homeassistant")
ha_stubs.fill("homeassistant.const",
              Platform=types.SimpleNamespace(TODO="todo", BUTTON="button", SWITCH="switch",
                                             SENSOR="sensor"))
ha_stubs.fill("homeassistant.core", ServiceCall=object, ServiceResponse=dict,
              SupportsResponse=types.SimpleNamespace(ONLY="only"))
ha_stubs.fill("homeassistant.exceptions",
              ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))
ha_stubs.fill("homeassistant.helpers")
ha_stubs.fill("homeassistant.helpers.config_validation", string=str,
              config_entry_only_config_schema=lambda domain: CONFIG_ENTRY_ONLY)
ha_stubs.fill("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
ha_stubs.fill("homeassistant.helpers.typing", ConfigType=dict)
ha_stubs.fill("homeassistant.helpers.update_coordinator",
              CoordinatorEntity=type("CoordinatorEntity", (), {
                  "__class_getitem__": classmethod(lambda c, i: c),
                  "__init__": lambda self, coordinator: setattr(self, "coordinator", coordinator),
              }),
              DataUpdateCoordinator=type("DataUpdateCoordinator", (), {
                  "__class_getitem__": classmethod(lambda c, i: c)}),
              UpdateFailed=ha_stubs.UpdateFailed)
ha_stubs.fill("homeassistant.components.frontend", add_extra_js_url=lambda hass, url: None,
              remove_extra_js_url=lambda hass, url: None)
ha_stubs.fill("homeassistant.helpers.storage", Store=object)
ha_stubs.fill("homeassistant.util.hass_dict", HassKey=str)
ha_stubs.fill("homeassistant.components.http", StaticPathConfig=object)
ha_stubs.fill("homeassistant.components.lovelace")
ha_stubs.fill("homeassistant.components.lovelace.const", LOVELACE_DATA="lovelace",
              MODE_STORAGE="storage")
ha_stubs.fill("homeassistant.loader", async_get_integration=None)
ha_stubs.fill("voluptuous", Schema=lambda *a, **k: None, Required=lambda *a, **k: None)
ha_stubs.fill("aiohttp", ClientError=type("ClientError", (Exception,), {}),
              ClientSession=object, ClientResponse=object)
ha_stubs.fill_todo_platform()
ha_stubs.fill_shared()

_pkg = types.ModuleType("progresscove")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["progresscove"] = _pkg


def _load(name, module_name=None):
    module_name = module_name or f"progresscove.{name}"
    spec = importlib.util.spec_from_file_location(module_name, COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


inbox_issue = _load("inbox_issue")
read_only = _load("read_only")
prune = _load("prune")
services = _load("services")
integration = _load("__init__", "progresscove")


class _Services:
    def __init__(self):
        self.registered = []
        self.removed = []

    def async_register(self, domain, service, handler, schema=None, supports_response=None):
        self.registered.append((domain, service))

    def async_remove(self, domain, service):
        self.removed.append((domain, service))


class _frontend_recorded:
    """Swap the frontend calls the integration makes for ones that only note they happened."""

    def __enter__(self):
        self.calls = []
        names = ("async_register_static_path", "async_register_cards", "async_remove_cards")
        self._originals = {name: getattr(integration, name) for name in names}
        for name in names:
            setattr(integration, name, self._recorder(name))
        return self.calls

    def _recorder(self, name):
        async def record(hass):
            self.calls.append(name)
        return record

    def __exit__(self, *exc):
        for name, original in self._originals.items():
            setattr(integration, name, original)


class _Registry:
    def __init__(self):
        self.deleted = []

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(issue_id)


class ActionRegistrationTest(unittest.TestCase):
    def test_the_configuration_is_config_entry_only(self):
        self.assertIs(integration.CONFIG_SCHEMA, CONFIG_ENTRY_ONLY)

    def test_setup_registers_every_action_once(self):
        hass = types.SimpleNamespace(services=_Services())
        with _frontend_recorded(), mock.patch.object(
            services.service, "async_register_platform_entity_service",
            lambda hass, domain, name, **kwargs: hass.services.registered.append((domain, name)),
        ):
            self.assertTrue(asyncio.run(integration.async_setup(hass, {})))
        self.assertEqual(sorted(service for _, service in hass.services.registered),
                         ["complete", "get_my_day_details", "get_nested_items", "reopen"])
        self.assertEqual({domain for domain, _ in hass.services.registered}, {"progresscove"})

    def test_an_entry_neither_registers_nor_removes_actions(self):
        setup_entry = inspect.getsource(integration.async_setup_entry)
        unload_entry = inspect.getsource(integration.async_unload_entry)
        self.assertNotIn("async_register_services", setup_entry)
        self.assertNotIn("services", unload_entry)

    def test_unloading_an_entry_leaves_the_actions_in_place(self):
        hass = types.SimpleNamespace(services=_Services())

        async def unload_platforms(entry, platforms):
            return True

        hass.config_entries = types.SimpleNamespace(async_unload_platforms=unload_platforms)
        flushed = []

        async def flush():
            flushed.append(True)

        entry = types.SimpleNamespace(runtime_data=types.SimpleNamespace(
            pending=types.SimpleNamespace(async_flush=flush)))
        self.assertTrue(asyncio.run(integration.async_unload_entry(hass, entry)))
        self.assertEqual(hass.services.removed, [])
        self.assertEqual(flushed, [True])


class _ShutdownHass:
    """Records shutdown jobs the way Home Assistant keeps them: removable until it stops, and the
    reloads the integration asks for."""

    def __init__(self, while_platforms_set_up=None):
        self.shutdown_jobs = []
        self.reloads = []
        self._while_platforms_set_up = while_platforms_set_up
        self.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=self._forward,
            async_schedule_reload=self.reloads.append,
        )

    async def _forward(self, entry, platforms):
        if self._while_platforms_set_up:
            self._while_platforms_set_up(entry)

    def async_add_shutdown_job(self, job):
        self.shutdown_jobs.append(job)
        return lambda: self.shutdown_jobs.remove(job)


class _Coordinator:
    def __init__(self, hass, entry, client):
        self.data = types.SimpleNamespace(by_id={})
        self.setup_inputs = None
        self.reload_scheduled = False
        self.flushed = []

        async def flush():
            self.flushed.append(True)

        self.pending = types.SimpleNamespace(async_flush_before_shutdown=flush)

    async def async_config_entry_first_refresh(self):
        return None

    async def async_resolve_inbox(self):
        return None


class _SetupEntry:
    entry_id = "entry-1"

    def __init__(self):
        self.data = {"url": "https://api.progresscove.com", "token": "pat_x"}
        self.options = {}
        self.subentries = {}
        self.on_unload = []
        self.listeners = []

    def async_on_unload(self, func):
        self.on_unload.append(func)

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return lambda: None

    def changed(self, hass):
        """Home Assistant calls every update listener once per change to an entry or subentry."""
        for listener in self.listeners:
            asyncio.run(listener(hass, self))


def _list(node_id, title="List"):
    return ha_stubs.ConfigSubentry(data={"node_id": node_id}, subentry_type="list", title=title,
                                   unique_id=None)


def _set_up(hass=None):
    hass, entry = hass or _ShutdownHass(), _SetupEntry()
    real = integration.ProgressCoveCoordinator, integration.ProgressCoveClient
    integration.ProgressCoveCoordinator = _Coordinator
    integration.ProgressCoveClient = lambda *args: None
    try:
        with _frontend_recorded():
            assert asyncio.run(integration.async_setup_entry(hass, entry))
    finally:
        integration.ProgressCoveCoordinator, integration.ProgressCoveClient = real
    return hass, entry


class ShutdownFlushTest(unittest.TestCase):
    """Home Assistant does not unload entries when it stops, and cancels every background task
    before its stop event, so held completions are flushed by a shutdown job instead."""

    def test_setting_up_registers_a_flush_for_shutdown(self):
        hass, entry = _set_up()
        [job] = hass.shutdown_jobs
        asyncio.run(job.target())
        self.assertEqual(entry.runtime_data.flushed, [True])

    def test_unloading_takes_the_shutdown_job_away(self):
        hass, entry = _set_up()
        for func in entry.on_unload:
            func()
        self.assertEqual(hass.shutdown_jobs, [])


class ReloadTest(unittest.TestCase):
    """Every change to an entry or a subentry calls the update listener, and a reload per call set
    the whole entry up again for each list added, removed or pruned."""

    def test_a_setup_that_matches_its_entry_does_not_reload_itself(self):
        hass, _ = _set_up()
        self.assertEqual(hass.reloads, [])

    def test_thirty_changes_from_one_action_reload_once(self):
        hass, entry = _set_up()
        for index in range(30):
            entry.subentries = {**entry.subentries, f"sub-{index}": _list(f"node-{index}")}
            entry.changed(hass)
        self.assertEqual(hass.reloads, ["entry-1"])

    def test_an_add_and_a_remove_reload_once(self):
        hass, entry = _set_up()
        entry.subentries = {"added": _list("node-a")}
        entry.changed(hass)
        entry.subentries = {}
        entry.changed(hass)
        self.assertEqual(hass.reloads, ["entry-1"])

    def test_a_retitled_row_does_not_reload(self):
        hass, entry = _set_up()
        entry.subentries = {"sub": _list("node-a", title="Groceries")}
        entry.runtime_data.setup_inputs = integration.SetupInputs.of(entry)
        entry.subentries = {"sub": _list("node-a", title="Food")}
        entry.changed(hass)
        self.assertEqual(hass.reloads, [])

    def test_changed_options_reload(self):
        hass, entry = _set_up()
        entry.options = {"auto_prune": True}
        entry.changed(hass)
        self.assertEqual(hass.reloads, ["entry-1"])

    def test_a_change_made_while_the_platforms_were_set_up_is_not_lost(self):
        def add_a_list(entry):
            entry.subentries = {"late": _list("node-late")}

        hass, _ = _set_up(_ShutdownHass(while_platforms_set_up=add_a_list))
        self.assertEqual(hass.reloads, ["entry-1"])


class SetupOnceTest(unittest.TestCase):
    def test_the_cards_are_served_and_registered_by_the_integration(self):
        hass = types.SimpleNamespace(services=_Services())
        with _frontend_recorded() as calls:
            asyncio.run(integration.async_setup(hass, {}))
        self.assertEqual(calls, ["async_register_static_path", "async_register_cards"])

    def test_an_entry_never_registers_the_static_path(self):
        """Registering it twice raises, so a second account would fail its setup."""
        self.assertNotIn("async_register_static_path",
                         inspect.getsource(integration.async_setup_entry))


def _hass_with_entries(remaining):
    return types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_entries=lambda domain: list(remaining)))


class RemoveEntryTest(unittest.TestCase):
    def _remove(self, remaining=()):
        registry = _Registry()
        originals = (inbox_issue.ir, read_only.ir, prune.ir)
        inbox_issue.ir = read_only.ir = prune.ir = registry
        try:
            entry = types.SimpleNamespace(entry_id="entry-1")
            with _frontend_recorded() as calls:
                asyncio.run(integration.async_remove_entry(_hass_with_entries(remaining), entry))
        finally:
            inbox_issue.ir, read_only.ir, prune.ir = originals
        return registry, calls

    def test_removing_the_last_entry_takes_the_cards_with_it(self):
        _, calls = self._remove()
        self.assertEqual(calls, ["async_remove_cards"])

    def test_another_account_keeps_the_cards(self):
        _, calls = self._remove(remaining=[types.SimpleNamespace(entry_id="entry-2")])
        self.assertEqual(calls, [])

    def test_removing_an_entry_clears_every_repair_it_raised(self):
        registry, _ = self._remove()
        self.assertEqual(sorted(registry.deleted), [
            "inbox_unavailable_entry-1", "read_only_entry-1", "stale_entities_entry-1",
            "unconfirmed_entities_entry-1",
        ])


if __name__ == "__main__":
    unittest.main()
