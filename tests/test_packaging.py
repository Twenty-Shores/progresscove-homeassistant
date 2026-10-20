"""What a HACS install actually delivers.

HACS copies `custom_components/<domain>/` and nothing else, so a file outside it never reaches a
user however well it works locally. These assert the shape of the shipped directory rather than any
behaviour, because every other test passes with the files in the wrong place.
"""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "progresscove"
CARDS = (
    "progresscove-add-card.js",
    "progresscove-card.js",
    "progresscove-icon-card.js",
    "progresscove-myday-card.js",
)


def _catalogues():
    paths = [COMPONENT / "strings.json"] + sorted((COMPONENT / "translations").glob("*.json"))
    return {path.relative_to(COMPONENT).as_posix(): json.loads(path.read_text()) for path in paths}


def _sources():
    return "\n".join(path.read_text() for path in COMPONENT.glob("*.py"))


class CatalogueCoverageTest(unittest.TestCase):
    """A key the code uses but no catalogue defines shows the person a raw key, or nothing.

    Derived from the code rather than listed by hand, so a new abort or a new error that forgets
    its text fails here instead of in front of someone.
    """

    def test_english_is_the_source_catalogue_verbatim(self):
        self.assertEqual(
            (COMPONENT / "strings.json").read_text(),
            (COMPONENT / "translations" / "en.json").read_text(),
        )

    def test_every_language_has_every_string(self):
        def keys(tree, prefix=""):
            found = set()
            for key, child in tree.items():
                if isinstance(child, dict):
                    found |= keys(child, f"{prefix}{key}.")
                else:
                    found.add(prefix + key)
            return found

        catalogues = _catalogues()
        english = keys(catalogues["strings.json"])
        for name, catalogue in catalogues.items():
            with self.subTest(name):
                self.assertEqual(english ^ keys(catalogue), set())

    def test_every_config_flow_abort_has_text(self):
        """Named in the code, plus the ones Home Assistant raises on our behalf: a duplicate
        account, a second flow for one account, and a finished reauth or reconfigure."""
        reasons = set(re.findall(r'reason="([a-z_]+)"', (COMPONENT / "config_flow.py").read_text()))
        self.assertIn("cannot_connect", reasons, "found no aborts, so this asserts nothing")
        self.assertIn("wrong_account", reasons)
        reasons |= {
            "already_configured", "already_in_progress", "reauth_successful",
            "reconfigure_successful",
        }
        for name, catalogue in _catalogues().items():
            known = set(catalogue["config"]["abort"])
            for subentry in catalogue["config_subentries"].values():
                known |= set(subentry.get("abort", {}))
            with self.subTest(name):
                self.assertEqual(reasons - known, set())

    def test_the_main_flow_can_say_it_could_not_connect(self):
        """The pairing step aborts the main flow, not a subentry flow, with this reason."""
        for name, catalogue in _catalogues().items():
            with self.subTest(name):
                self.assertIn("cannot_connect", catalogue["config"]["abort"])

    def test_every_raised_error_has_a_message_in_every_language(self):
        source = _sources()
        keys = set(re.findall(r'translated_error\(\s*"([a-z_]+)"', source))
        keys |= set(re.findall(r'surfaced\("([a-z_]+)"\)', source))
        self.assertIn("read_only", keys, "found no translated errors, so this asserts nothing")
        catalogues = _catalogues()
        english = catalogues["strings.json"]["exceptions"]
        for name, catalogue in catalogues.items():
            with self.subTest(name):
                self.assertEqual(keys - set(catalogue["exceptions"]), set())
                for key, entry in catalogue["exceptions"].items():
                    self.assertEqual(
                        set(re.findall(r"{(\w+)}", entry["message"])),
                        set(re.findall(r"{(\w+)}", english[key]["message"])),
                        f"{name}: {key} does not carry the same placeholders as English",
                    )


    def test_every_entity_translation_key_has_a_name(self):
        """A key with no name renders as the bare device name, which reads like a bug."""
        found = 0
        for path in COMPONENT.glob("*.py"):
            source = path.read_text()
            keys = re.findall(r'_attr_translation_key = "([a-z_]+)"', source)
            if not keys:
                continue
            platforms = set(re.findall(
                r"^from homeassistant\.components\.(todo|button|switch|sensor) import", source, re.M
            ))
            self.assertEqual(len(platforms), 1, f"{path.name}: cannot tell which platform")
            platform = platforms.pop()
            for name, catalogue in _catalogues().items():
                for key in keys:
                    found += 1
                    with self.subTest(name, key=key):
                        self.assertTrue(catalogue["entity"][platform][key]["name"])
        self.assertTrue(found, "found no translation keys, so this asserts nothing")

    def test_icons_come_from_icons_json_not_the_code(self):
        """Every entity translation key and every action has an icon there, and none is set in
        code, where it could not follow a state."""
        icons = json.loads((COMPONENT / "icons.json").read_text())
        keyed = {}
        for path in COMPONENT.glob("*.py"):
            source = path.read_text()
            self.assertNotIn("_attr_icon", source, path.name)
            platform = re.search(
                r"^from homeassistant\.components\.(todo|button|switch|sensor) import", source, re.M
            )
            for key in re.findall(r'_attr_translation_key = "([a-z_]+)"', source):
                keyed.setdefault(platform.group(1), set()).add(key)
        self.assertTrue(keyed, "found no translation keys, so this asserts nothing")
        self.assertEqual({platform: set(keys) for platform, keys in icons["entity"].items()}, keyed)
        actions = set(re.findall(r"^(\w+):", (COMPONENT / "services.yaml").read_text(), re.M))
        self.assertEqual(set(icons["services"]), actions)

    def test_every_selector_translation_key_names_every_option(self):
        flow = (COMPONENT / "config_flow.py").read_text()
        keys = set(re.findall(r'translation_key="([a-z_]+)"', flow))
        self.assertEqual(keys, {"tier", "node_sort"})
        const = (COMPONENT / "const.py").read_text()
        deepest = int(re.search(r"^MAX_TIER_DEPTH = (\d+)", const, re.M).group(1))
        sorts = set(re.findall(r'^SORT_BY_\w+ = "([a-z_]+)"', const, re.M))
        expected = {"tier": {str(depth) for depth in range(1, deepest + 1)}, "node_sort": sorts}
        for name, catalogue in _catalogues().items():
            for key, options in expected.items():
                with self.subTest(name, key=key):
                    self.assertEqual(set(catalogue["selector"][key]["options"]), options)

    def test_every_action_and_field_is_named_and_described(self):
        """services.yaml holds only the schema; the words live in the catalogues, so they are
        translated like everything else."""
        yaml = (COMPONENT / "services.yaml").read_text()
        self.assertIsNone(re.search(r"^\s*(name|description):", yaml, re.M),
                          "services.yaml still carries text that belongs in the catalogues")
        actions = re.findall(r"^([a-z_]+):$", yaml, re.M)
        # Only what sits under `fields:`; an action aimed at an entity has a `target:` instead.
        fields = {
            action: re.findall(r"^    ([a-z_]+):$", block.partition("  fields:")[2], re.M)
            for action, block in zip(actions, re.split(r"^[a-z_]+:$", yaml, flags=re.M)[1:])
        }
        self.assertEqual(sorted(actions),
                         ["complete", "get_my_day_details", "get_nested_items", "reopen"])
        for name, catalogue in _catalogues().items():
            with self.subTest(name):
                self.assertEqual(set(catalogue["services"]), set(actions))
            for action, action_fields in fields.items():
                text = catalogue["services"][action]
                with self.subTest(name, action=action):
                    self.assertTrue(text["name"] and text["description"])
                    self.assertEqual(set(text.get("fields", {})), set(action_fields))
                    for field in action_fields:
                        self.assertTrue(text["fields"][field]["name"])
                        self.assertTrue(text["fields"][field]["description"])

    def test_no_subentry_carries_a_flow_title(self):
        """hassfest warns on it: the dialog takes its title from the step, not the subentry."""
        for name, catalogue in _catalogues().items():
            for kind, subentry in catalogue["config_subentries"].items():
                with self.subTest(name, kind=kind):
                    self.assertNotIn("title", subentry)


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

    def test_every_component_we_import_is_declared(self):
        """hassfest fails the build on an undeclared one, and Home Assistant may otherwise set us
        up before it has loaded.

        Derived from the imports rather than a hand-kept list: adding one and forgetting the
        manifest is exactly the mistake this catches. A component we provide a platform for
        (button.py, repairs.py) is exempt, as hassfest exempts it: Home Assistant loads our
        platform from that component, not the other way round.
        """
        manifest = json.loads((COMPONENT / "manifest.json").read_text())
        declared = set(manifest.get("dependencies", [])) | set(
            manifest.get("after_dependencies", [])
        )
        platforms = {path.stem for path in COMPONENT.glob("*.py")}
        imported = {
            match.group(1)
            for path in COMPONENT.glob("*.py")
            for match in re.finditer(
                r"^from homeassistant\.components\.([a-z_]+)", path.read_text(), re.M
            )
        } - platforms
        self.assertTrue(imported, "found no component imports, so this asserts nothing")
        self.assertEqual(
            imported - declared, set(), "imported but missing from the manifest"
        )

    def test_the_manifest_carries_everything_hacs_requires(self):
        manifest = json.loads((COMPONENT / "manifest.json").read_text())
        for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
            with self.subTest(key):
                self.assertTrue(manifest.get(key), f"manifest is missing {key}")

    def test_the_brand_icons_are_the_sizes_home_assistant_expects(self):
        import struct

        for name, expected in (("icon.png", 256), ("icon@2x.png", 512)):
            with self.subTest(name):
                header = (COMPONENT / "brand" / name).read_bytes()[:24]
                width, height = struct.unpack(">II", header[16:24])
                self.assertEqual((width, height), (expected, expected))


if __name__ == "__main__":
    unittest.main()
