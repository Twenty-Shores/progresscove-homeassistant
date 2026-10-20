"""A list's bar follows the same rule the apps use, so a life area does not show one by default.

A life area never completes, so "Household: 80% done" says something untrue; the apps hide its bar
unless the person turned it on, and show every other tier's bar unless they turned it off. The card
on a wall tablet should not disagree with the phone in the same house.
"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
if "progresscove" not in sys.modules:
    _pkg = types.ModuleType("progresscove")
    _pkg.__path__ = [str(COMPONENT)]
    sys.modules["progresscove"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "progresscove.progress_bar", COMPONENT / "progress_bar.py"
)
_progress_bar = importlib.util.module_from_spec(_spec)
sys.modules["progresscove.progress_bar"] = _progress_bar
_spec.loader.exec_module(_progress_bar)
progress_bar_hidden = _progress_bar.progress_bar_hidden


class TierDefaultTest(unittest.TestCase):
    def test_a_life_area_hides_its_bar(self):
        self.assertTrue(progress_bar_hidden({"depth": 1, "hide_progress_bar": None}))

    def test_every_tier_below_shows_it(self):
        for depth in (2, 3, 4):
            with self.subTest(depth):
                self.assertFalse(progress_bar_hidden({"depth": depth, "hide_progress_bar": None}))

    def test_a_server_from_before_the_setting_reads_as_the_default(self):
        self.assertTrue(progress_bar_hidden({"depth": 1}))
        self.assertFalse(progress_bar_hidden({"depth": 2}))


class ChoiceTest(unittest.TestCase):
    def test_the_persons_choice_wins(self):
        self.assertFalse(progress_bar_hidden({"depth": 1, "hide_progress_bar": False}))
        self.assertTrue(progress_bar_hidden({"depth": 2, "hide_progress_bar": True}))

    def test_a_value_that_is_not_a_boolean_reads_as_the_default(self):
        for value in (0, 1, "false", "true"):
            with self.subTest(value):
                self.assertTrue(progress_bar_hidden({"depth": 1, "hide_progress_bar": value}))
                self.assertFalse(progress_bar_hidden({"depth": 2, "hide_progress_bar": value}))


if __name__ == "__main__":
    unittest.main()
