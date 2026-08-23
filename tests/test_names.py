"""Node names are typed by a user, so they are untrusted wherever they reach a surface.

A path is joined with a separator and split apart again by the cards, so a name carrying that
separator invents a level that does not exist, and a card following one node pulls in another's
tasks.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
if "progresscove" not in sys.modules:
    _pkg = types.ModuleType("progresscove")
    _pkg.__path__ = [str(COMPONENT)]
    sys.modules["progresscove"] = _pkg
_spec = importlib.util.spec_from_file_location("progresscove.names", COMPONENT / "names.py")
_names = importlib.util.module_from_spec(_spec)
sys.modules["progresscove.names"] = _names
_spec.loader.exec_module(_names)
display_name = _names.display_name
PATH_SEPARATOR = _names.PATH_SEPARATOR


class OrdinaryNameTest(unittest.TestCase):
    def test_a_normal_name_is_untouched(self):
        for probe in ("Milk", "Take the bins out", "Réunion", "牛乳", "Bread & butter"):
            with self.subTest(probe):
                self.assertEqual(display_name(probe), probe)

    def test_missing_and_empty_names_are_empty_strings(self):
        for probe in (None, "", "   "):
            with self.subTest(probe):
                self.assertEqual(display_name(probe), "")


class SeparatorCollisionTest(unittest.TestCase):
    def test_the_separator_cannot_survive_in_a_name(self):
        self.assertNotIn("›", display_name("Shopping › Frozen"))

    def test_a_faked_level_no_longer_matches_a_real_one(self):
        """The whole point: these two produced identical paths before."""
        faked = PATH_SEPARATOR.join(["Home", display_name("Shopping › Frozen")])
        real = PATH_SEPARATOR.join([display_name("Home"), display_name("Shopping"),
                                    display_name("Frozen")])
        self.assertNotEqual(faked, real)

    def test_a_path_splits_into_the_number_of_nodes_it_has(self):
        path = PATH_SEPARATOR.join(display_name(n) for n in ("Home", "Shopping › Frozen", "Milk"))
        self.assertEqual(len(path.split(PATH_SEPARATOR)), 3)


class ControlCharacterTest(unittest.TestCase):
    """These can reorder or truncate a line wherever it is rendered."""

    def test_bidi_overrides_are_removed(self):
        self.assertEqual(display_name("safe‮reversed"), "safereversed")

    def test_line_and_paragraph_separators_are_removed(self):
        for probe in ("a b", "a b", "a\rb", "a\nb"):
            with self.subTest(probe):
                out = display_name(probe)
                self.assertNotIn(" ", out)
                self.assertNotIn("\n", out)
                self.assertNotIn("\r", out)

    def test_a_zero_width_joiner_is_removed(self):
        self.assertEqual(display_name("a​b"), "ab")

    def test_runs_of_whitespace_collapse(self):
        self.assertEqual(display_name("  too    many   spaces "), "too many spaces")


if __name__ == "__main__":
    unittest.main()
