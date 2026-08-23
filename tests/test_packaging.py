"""What a HACS install actually delivers.

HACS copies `custom_components/<domain>/` and nothing else, so a file outside it never reaches a
user however well it works locally. These assert the shape of the shipped directory rather than any
behaviour, because every other test passes with the files in the wrong place.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"
CARDS = ("progresscove-card.js", "progresscove-icon-card.js", "progresscove-myday-card.js")


class ShippedLayoutTest(unittest.TestCase):
    def test_every_card_is_inside_the_component(self):
        for card in CARDS:
            with self.subTest(card):
                self.assertTrue(
                    (COMPONENT / "frontend" / card).is_file(),
                    f"{card} is outside custom_components/progresscove/; HACS will not ship it",
                )

    def test_no_card_is_left_outside_the_component(self):
        """A stray copy in a sibling directory is worse than none: it works locally and ships
        nothing, which is exactly how this was missed the first time."""
        strays = [p for p in ROOT.glob("www/*.js")] + [p for p in ROOT.glob("*.js")]
        self.assertEqual(strays, [], f"card files outside the component: {strays}")

    def test_the_frontend_module_lists_exactly_what_is_on_disk(self):
        """A card added to the folder but not to the list is served and never loaded; one listed
        but missing is a 404 on every dashboard."""
        source = (COMPONENT / "frontend.py").read_text()
        listed = {card for card in CARDS if f'"{card}"' in source}
        on_disk = {p.name for p in (COMPONENT / "frontend").glob("*.js")}
        self.assertEqual(listed, on_disk)

    def test_the_manifest_depends_on_what_the_frontend_registration_imports(self):
        """`frontend` and `http` are imported at setup. Without the declaration Home Assistant may
        set us up before either is loaded."""
        manifest = json.loads((COMPONENT / "manifest.json").read_text())
        self.assertIn("frontend", manifest.get("dependencies", []))
        self.assertIn("http", manifest.get("dependencies", []))

    def test_the_manifest_carries_everything_hacs_requires(self):
        manifest = json.loads((COMPONENT / "manifest.json").read_text())
        for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
            with self.subTest(key):
                self.assertTrue(manifest.get(key), f"manifest is missing {key}")

    def test_hacs_and_the_manifest_agree_on_the_minimum_version(self):
        """HACS refuses to install below its floor, and the integration refuses to set up below
        its own. Two different numbers would mean an install that succeeds and then fails."""
        from_const = (COMPONENT / "const.py").read_text()
        hacs = json.loads((ROOT / "hacs.json").read_text())
        major, minor = hacs["homeassistant"].split(".")[:2]
        self.assertIn(f"MIN_HA_VERSION = ({major}, {minor})", from_const)

    def test_the_brand_icons_are_the_sizes_home_assistant_expects(self):
        import struct

        for name, expected in (("icon.png", 256), ("icon@2x.png", 512)):
            with self.subTest(name):
                header = (COMPONENT / "brand" / name).read_bytes()[:24]
                width, height = struct.unpack(">II", header[16:24])
                self.assertEqual((width, height), (expected, expected))


if __name__ == "__main__":
    unittest.main()
