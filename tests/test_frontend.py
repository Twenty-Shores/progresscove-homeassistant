"""How the cards reach a browser: the url that retires a cached copy, and the path that loads it.

Both rules here were learned from cards that half-worked. Home Assistant serves a registered static
path with a month-long cache header, so the url is the only thing that can retire an old copy; and
a module registered down two unordered paths can be asked for before either has finished, which
renders the card as an unknown type until the page is reloaded.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

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
_stub("homeassistant.components.frontend", add_extra_js_url=lambda hass, url: None)
_stub("homeassistant.components.http", StaticPathConfig=object)
_stub("homeassistant.components.lovelace")
_stub("homeassistant.components.lovelace.const", LOVELACE_DATA="lovelace")
_stub("homeassistant.loader", async_get_integration=None)

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


class SingleRegistrationTest(unittest.TestCase):
    """A card is offered to the frontend once, not twice.

    Registering the same url as a dashboard resource AND an extra module url asks the frontend to
    load it down two paths that are not ordered against each other, and a card can be asked for
    before either has finished. The resource list is the one a dashboard waits on, so the extra
    module url is only for an install with nowhere to put a resource.
    """

    def test_the_extra_module_url_is_the_fallback_not_a_companion(self):
        source = (COMPONENT / "frontend.py").read_text()
        body = source[source.index("async def async_register"):source.index("async def _register")]
        guard = body.index("if not registered:")
        self.assertLess(
            guard, body.index("add_extra_js_url(hass, url)"),
            "add_extra_js_url runs unguarded, so every card is registered twice",
        )


if __name__ == "__main__":
    unittest.main()
