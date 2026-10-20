"""Due dates between Home Assistant and the API.

The API refuses a datetime without a timezone, and a bare day like "2026-10-07" is not an instant
at all. HA hands over either a date or a datetime, so every due leaves as an instant with its
offset, and a due day with no time is stored the way the apps store one: the end of that day in
the house's timezone, marked by .999 seconds.
"""
from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

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


_module("homeassistant")
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions",
        HomeAssistantError=ha_stubs.HomeAssistantError,
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}))
_module("aiohttp", ClientSession=object, ClientResponse=object,
        ClientError=type("ClientError", (Exception,), {}))

ha_stubs.fill_shared()

if "progresscove" not in sys.modules:
    _pkg = types.ModuleType("progresscove")
    _pkg.__path__ = [str(COMPONENT)]
    sys.modules["progresscove"] = _pkg


def _load(name):
    existing = sys.modules.get(f"progresscove.{name}")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = module
    spec.loader.exec_module(module)
    return module


helpers = _load("helpers")
NEW_YORK = ZoneInfo("America/New_York")


class DueForApiTest(unittest.TestCase):
    def test_a_due_day_is_the_end_of_that_day_in_the_house(self):
        sent = helpers.due_for_api(date(2026, 10, 7), NEW_YORK)
        self.assertEqual(sent, "2026-10-07T23:59:59.999000-04:00")

    def test_an_aware_datetime_keeps_its_offset(self):
        due = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(helpers.due_for_api(due, NEW_YORK), "2026-10-07T09:30:00+00:00")

    def test_a_wall_clock_time_is_read_in_the_house(self):
        self.assertEqual(
            helpers.due_for_api(datetime(2026, 10, 7, 9, 30), NEW_YORK),
            "2026-10-07T09:30:00-04:00",
        )

    def test_no_due_stays_none(self):
        self.assertIsNone(helpers.due_for_api(None, NEW_YORK))

    def test_every_due_sent_has_an_offset(self):
        dues = (date(2026, 1, 1), datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 8, tzinfo=NEW_YORK))
        for due in dues:
            self.assertIsNotNone(datetime.fromisoformat(helpers.due_for_api(due, NEW_YORK)).tzinfo)


class HouseZoneTest(unittest.TestCase):
    def test_the_house_zone_is_the_one_home_assistant_holds(self):
        """Building a zone reads the zone files, which blocks the event loop."""
        today_in_new_york = datetime.now(NEW_YORK).date().isoformat()
        node = {"id": "n", "due_at": f"{today_in_new_york}T12:00:00-04:00"}
        with mock.patch.object(helpers.dt_util, "get_default_time_zone", lambda: NEW_YORK), \
                mock.patch.object(helpers, "ZoneInfo", side_effect=AssertionError("built a zone")):
            self.assertTrue(helpers.can_complete(node))


class ParseDueTest(unittest.TestCase):
    def test_a_due_day_comes_back_as_that_day(self):
        """Read back in the house, not in UTC, where it would already be the next day."""
        stored = "2026-10-08T03:59:59.999000+00:00"
        self.assertEqual(helpers.parse_due(stored, NEW_YORK), date(2026, 10, 7))

    def test_a_due_day_round_trips(self):
        sent = helpers.due_for_api(date(2026, 10, 7), NEW_YORK)
        self.assertEqual(helpers.parse_due(sent, NEW_YORK), date(2026, 10, 7))

    def test_a_time_of_day_stays_an_instant(self):
        parsed = helpers.parse_due("2026-10-07T13:30:00+00:00", NEW_YORK)
        self.assertEqual(parsed, datetime(2026, 10, 7, 13, 30, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
