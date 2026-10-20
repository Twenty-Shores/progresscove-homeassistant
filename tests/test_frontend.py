"""How the cards reach a browser: the url that retires a cached copy, and the path that loads it.

Both rules here were learned from cards that half-worked. Home Assistant serves a registered static
path with a month-long cache header, so the url is the only thing that can retire an old copy; and
a module registered down two unordered paths can be asked for before either has finished, which
renders the card as an unknown type until the page is reloaded.
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

import ha_stubs

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"


def _stub(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    module.__path__ = getattr(module, "__path__", [])
    for key, value in attrs.items():
        if not hasattr(module, key):
            setattr(module, key, value)
    sys.modules[name] = module
    return module


_stub("homeassistant")
_stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_stub("homeassistant.components")
_stub("homeassistant.components.frontend", add_extra_js_url=lambda hass, url: None,
      remove_extra_js_url=lambda hass, url: None)
_stub("homeassistant.components.http", StaticPathConfig=object)
_stub("homeassistant.components.lovelace")
_stub("homeassistant.components.lovelace.const", LOVELACE_DATA="lovelace", MODE_STORAGE="storage")
_stub("homeassistant.loader", async_get_integration=None)
_stub("homeassistant.helpers")
_stub("homeassistant.helpers.storage", Store=object)
_stub("homeassistant.util")
_stub("homeassistant.util.hass_dict", HassKey=str)

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


_load("const")
frontend = _load("frontend")


class CacheKeyTest(unittest.TestCase):
    """The url has to change when a card does, and only then.

    The released version alone was not enough: the files change many times under one version while
    they are being worked on, and a browser told to hold them for a month has no reason to ask
    again. That is what left an edited card stale until a hard reload.
    """

    def test_the_key_changes_when_a_card_changes(self):
        card = COMPONENT / "frontend" / frontend.CARDS[0]
        original = card.read_bytes()
        before = frontend._cache_key("1.0")
        card.write_bytes(original + b"\n// touched\n")
        try:
            self.assertNotEqual(before, frontend._cache_key("1.0"))
        finally:
            card.write_bytes(original)
        self.assertEqual(before, frontend._cache_key("1.0"), "the key did not settle back")

    def test_an_unchanged_card_keeps_its_key(self):
        """Otherwise every restart would retire a copy the browser could have kept."""
        self.assertEqual(frontend._cache_key("1.0"), frontend._cache_key("1.0"))

    def test_the_version_is_part_of_the_key(self):
        self.assertNotEqual(frontend._cache_key("1.0"), frontend._cache_key("1.1"))


class _Store:
    """Home Assistant's Store, kept on the hass object so it outlives one call, as .storage does."""

    def __init__(self, hass, version, key):
        self._hass = hass
        self._key = key

    async def async_load(self):
        return self._hass.storage.get(self._key)

    async def async_save(self, data):
        self._hass.storage[self._key] = data

    async def async_remove(self):
        self._hass.storage.pop(self._key, None)


class _Resources:
    """The storage-mode resource collection: it can be listed, written, updated and deleted."""

    def __init__(self, items=()):
        self.loaded = True
        self.items = [dict(item) for item in items]
        self.created = 0

    def async_items(self):
        return list(self.items)

    async def async_create_item(self, item):
        self.created += 1
        self.items.append({"id": f"new-{self.created}", **item})

    async def async_update_item(self, item_id, updates):
        self.items = [{**item, **updates} if item["id"] == item_id else item
                      for item in self.items]

    async def async_delete_item(self, item_id):
        self.items = [item for item in self.items if item["id"] != item_id]

    def urls(self):
        return sorted(item["url"] for item in self.items)


class _YamlResources:
    """The YAML-mode collection, which has no create or delete at all."""

    loaded = True

    def async_items(self):
        return [{"id": "declared", "url": f"{frontend.URL_BASE}/progresscove-card.js"}]


class _Http:
    async def async_register_static_paths(self, configs):
        return None


class _Hass:
    def __init__(self, lovelace):
        self.lovelace = lovelace
        self.storage = {}
        self.data = {}
        self.restart()
        self.http = _Http()

    def restart(self):
        """What survives a restart is .storage and the dashboards; hass.data does not."""
        self.data = {} if self.lovelace is None else {frontend.LOVELACE_DATA: self.lovelace}

    async def async_add_executor_job(self, target, *args):
        return target(*args)


class _Frontend:
    """The frontend calls swapped for recorders, and the integration's version settable."""

    def __init__(self, test, version="1.0"):
        self.version = version
        self.extra: list[str] = []
        originals = (frontend.async_get_integration, frontend.add_extra_js_url,
                     frontend.remove_extra_js_url, frontend.Store)

        async def integration(hass, domain):
            return types.SimpleNamespace(version=self.version)

        frontend.async_get_integration = integration
        frontend.add_extra_js_url = lambda hass, url: self.extra.append(url)
        frontend.remove_extra_js_url = lambda hass, url: self.extra.remove(url)
        frontend.Store = _Store

        def restore():
            (frontend.async_get_integration, frontend.add_extra_js_url,
             frontend.remove_extra_js_url, frontend.Store) = originals

        test.addCleanup(restore)

    @staticmethod
    def register(hass):
        asyncio.run(frontend.async_register_cards(hass))

    @staticmethod
    def remove(hass):
        asyncio.run(frontend.async_remove_cards(hass))


def _storage_mode(items=()):
    resources = _Resources(items)
    return _Hass(types.SimpleNamespace(resource_mode="storage", resources=resources)), resources


def _card_urls(resources, card="progresscove-card.js"):
    return [url for url in resources.urls() if url.startswith(f"{frontend.URL_BASE}/{card}?")]


class ResourceModeTest(unittest.TestCase):
    """Setup has to work whichever way the dashboards keep their resources."""

    def test_storage_mode_writes_resources_and_no_extra_modules(self):
        """Not both: registering a url as a resource AND an extra module asks the frontend to load
        it down two paths that are not ordered against each other, and a card can be asked for
        before either has finished. The resource list is the one a dashboard waits on."""
        recorded = _Frontend(self)
        hass, resources = _storage_mode()
        recorded.register(hass)
        self.assertEqual(recorded.extra, [])
        self.assertEqual(len(resources.items), len(frontend.CARDS))

    def test_yaml_mode_falls_back_to_extra_modules_without_writing(self):
        recorded = _Frontend(self)
        recorded.register(_Hass(types.SimpleNamespace(resource_mode="yaml",
                                                      resources=_YamlResources())))
        self.assertEqual(len(recorded.extra), len(frontend.CARDS))

    def test_no_dashboards_at_all_falls_back_to_extra_modules(self):
        recorded = _Frontend(self)
        recorded.register(_Hass(None))
        self.assertEqual(len(recorded.extra), len(frontend.CARDS))


class ResourceChoiceTest(unittest.TestCase):
    """A resource the integration added is its own to keep current; one the person deleted is
    theirs, and is not put back on every restart."""

    def test_a_restart_adds_nothing_twice(self):
        recorded = _Frontend(self)
        hass, resources = _storage_mode()
        recorded.register(hass)
        hass.restart()
        recorded.register(hass)
        self.assertEqual(len(resources.items), len(frontend.CARDS))

    def test_a_new_version_moves_our_resource_to_its_url_in_place(self):
        recorded = _Frontend(self)
        hass, resources = _storage_mode()
        recorded.register(hass)
        [before] = [item for item in resources.items
                    if item["url"] in _card_urls(resources)]
        recorded.version = "1.1"
        hass.restart()
        recorded.register(hass)
        [after] = [item for item in resources.items if item["url"] in _card_urls(resources)]
        self.assertEqual(after["id"], before["id"])
        self.assertIn("?v=1.1.", after["url"])
        self.assertEqual(len(resources.items), len(frontend.CARDS))

    def test_a_resource_the_person_deleted_stays_deleted(self):
        recorded = _Frontend(self)
        hass, resources = _storage_mode()
        recorded.register(hass)
        [card] = _card_urls(resources)
        resources.items = [item for item in resources.items if item["url"] != card]
        for version in ("1.0", "1.1"):
            with self.subTest(version=version):
                recorded.version = version
                hass.restart()
                recorded.register(hass)
                self.assertEqual(_card_urls(resources), [])
                self.assertEqual(len(resources.items), len(frontend.CARDS) - 1)

    def test_a_card_new_in_this_version_is_added(self):
        recorded = _Frontend(self)
        hass, resources = _storage_mode()
        recorded.register(hass)
        del hass.storage[frontend._RESOURCES_STORAGE_KEY]["cards"]["progresscove-card.js"]
        [card] = _card_urls(resources)
        resources.items = [item for item in resources.items if item["url"] != card]
        hass.restart()
        recorded.register(hass)
        self.assertEqual(len(_card_urls(resources)), 1)

    def test_resources_that_are_not_ours_are_left_alone(self):
        recorded = _Frontend(self)
        hass, resources = _storage_mode([{"id": "theirs", "url": "/local/other.js"}])
        recorded.register(hass)
        recorded.remove(hass)
        self.assertEqual(resources.urls(), ["/local/other.js"])


class RemoveCardsTest(unittest.TestCase):
    """The last account removed takes what the integration added with it, and nothing else."""

    def test_storage_mode_deletes_ours_and_the_record_of_them(self):
        recorded = _Frontend(self)
        hass, resources = _storage_mode()
        recorded.register(hass)
        recorded.remove(hass)
        self.assertEqual(resources.items, [])
        self.assertEqual(hass.storage, {})

    def test_yaml_mode_takes_its_extra_modules_out_and_writes_nothing(self):
        """The YAML collection has no delete; reaching for one would fail the removal."""
        recorded = _Frontend(self)
        hass = _Hass(types.SimpleNamespace(resource_mode="yaml", resources=_YamlResources()))
        recorded.register(hass)
        recorded.remove(hass)
        self.assertEqual(recorded.extra, [])

    def test_no_dashboards_at_all_is_not_an_error(self):
        recorded = _Frontend(self)
        hass = _Hass(None)
        recorded.register(hass)
        recorded.remove(hass)
        self.assertEqual(recorded.extra, [])

    def test_cards_come_back_for_an_account_added_after(self):
        """async_setup does not run again for it, so registering has to be possible again, and
        the record went with the last account, so the cards are added afresh."""
        recorded = _Frontend(self)
        hass, resources = _storage_mode()
        recorded.register(hass)
        recorded.register(hass)
        self.assertEqual(len(resources.items), len(frontend.CARDS))
        recorded.remove(hass)
        self.assertEqual(resources.items, [])
        recorded.register(hass)
        self.assertEqual(len(resources.items), len(frontend.CARDS))


if __name__ == "__main__":
    unittest.main()
