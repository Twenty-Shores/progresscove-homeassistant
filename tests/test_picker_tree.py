"""The picker's labels ARE the tree drawing, so the connectors have to be right.

A select option is {value, label} and nothing more, so the structure lives in characters. A branch
hanging from the wrong row is worse than no drawing at all.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

# Loaded as part of a stub package rather than as a bare file: picker_tree needs nothing from Home
# Assistant, but it does import a sibling, and a relative import needs a parent to resolve against.
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
if "progresscove" not in sys.modules:
    _pkg = types.ModuleType("progresscove")
    _pkg.__path__ = [str(COMPONENT)]
    sys.modules["progresscove"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("names")
_picker_tree = _load("picker_tree")
tree_labels = _picker_tree.tree_labels
selectable_ids = _picker_tree.selectable_ids


def _node(node_id, name, parent=None, depth=None):
    return {"id": node_id, "name": name, "parent_id": parent, "depth": depth}


class PickerTreeTest(unittest.TestCase):
    def test_a_root_has_no_connector_to_hang_from(self):
        labels = tree_labels([_node("a", "Home")])
        self.assertEqual(labels["a"], "· Home")

    def test_children_hang_off_their_parent_and_the_last_one_closes(self):
        nodes = [
            _node("home", "Home"),
            _node("shop", "Shopping", "home"),
            _node("tasks", "Repeating", "home"),
        ]
        labels = tree_labels(nodes)
        # Alphabetical within a level: Repeating before Shopping, so Shopping closes the branch.
        self.assertEqual(labels["tasks"], "├─ · Repeating")
        self.assertEqual(labels["shop"], "└─ · Shopping")

    def test_a_deeper_branch_keeps_the_trunk_drawn(self):
        nodes = [
            _node("home", "Home"),
            _node("shop", "Shopping", "home"),
            _node("tasks", "Repeating", "home"),
            _node("milk", "Milk", "shop"),
        ]
        labels = tree_labels(nodes)
        # Shopping is last under Home, so nothing continues below it: three spaces, not a pipe.
        self.assertEqual(labels["milk"], "   └─ · Milk")

    def test_a_trunk_continues_past_a_branch_that_is_not_last(self):
        nodes = [
            _node("home", "Home"),
            _node("a", "Alpha", "home"),
            _node("z", "Zulu", "home"),
            _node("a1", "Item", "a"),
        ]
        labels = tree_labels(nodes)
        # Zulu still comes below, so Alpha's subtree keeps the trunk.
        self.assertEqual(labels["a1"], "│  └─ · Item")

    def test_a_node_holding_items_is_marked_and_counted(self):
        nodes = [_node("home", "Home"), _node("milk", "Milk", "home")]
        labels = tree_labels(nodes)
        self.assertTrue(labels["home"].startswith("\U0001f4c1 Home"))
        self.assertTrue(labels["home"].endswith("(1 child)"))
        # A childless node is offered too: it is a list the user may be about to fill.
        self.assertEqual(labels["milk"], "└─ · Milk")

    def test_every_node_gets_a_label_even_with_a_missing_parent(self):
        """A scoped token can return a child whose parent it cannot see. Falling out of the tree
        walk is not a reason to disappear from the picker."""
        nodes = [_node("orphan", "Orphan", "not-in-payload")]
        labels = tree_labels(nodes)
        self.assertEqual(labels["orphan"], "· Orphan")


if __name__ == "__main__":
    unittest.main()


class DepthRangeTest(unittest.TestCase):
    """A tier range narrows what can be PICKED without hiding where a list sits."""

    def _tree(self):
        return [
            _node("home", "Home", None, 1),
            _node("shop", "Shopping", "home", 2),
            _node("groc", "Groceries", "shop", 3),
            _node("milk", "Milk", "groc", 4),
            _node("bare", "Bare Domain", None, 1),
        ]

    def test_a_tier_above_the_range_is_kept_as_context(self):
        labels = tree_labels(self._tree(), 3, 4)
        # Home and Shopping are not pickable, but without them Groceries floats with no context.
        self.assertTrue(labels["home"].startswith("—"))
        self.assertIn("Shopping", labels["shop"])
        self.assertNotIn("home", selectable_ids(self._tree(), 3, 4))
        self.assertIn("groc", selectable_ids(self._tree(), 3, 4))

    def test_a_branch_with_nothing_selectable_is_dropped_entirely(self):
        # "Bare Domain" has no descendants in 3-4, so it answers no question and is not drawn.
        labels = tree_labels(self._tree(), 3, 4)
        self.assertNotIn("bare", labels)

    def test_a_tier_below_the_range_is_simply_gone(self):
        labels = tree_labels(self._tree(), 1, 3)
        self.assertNotIn("milk", labels)

    def test_counts_report_the_account_not_the_filter(self):
        """A count answers "is there anything in here?" about the tree, not about this picker."""
        labels = tree_labels(self._tree(), 1, 3)
        self.assertIn("(1 child)", labels["groc"])   # Milk is hidden but still exists

    def test_a_node_without_a_depth_is_never_filtered_out(self):
        nodes = [_node("x", "No depth", None, None)]
        self.assertIn("x", tree_labels(nodes, 3, 4))
        self.assertIn("x", selectable_ids(nodes, 3, 4))


class OffscreenSelectionTest(unittest.TestCase):
    """Narrowing the tier range must not delete lists it simply cannot draw."""

    NODES = [
        _node("home", "Home", None, 1),
        _node("shop", "Shopping", "home", 2),
        _node("groc", "Groceries", "shop", 3),
    ]

    def test_a_chosen_list_outside_the_range_is_not_on_screen_to_keep(self):
        # Home (T1) cannot be ticked at 3-4 (there is no row for it), so the form alone would
        # report it as unchosen and the submit would drop it.
        selectable = selectable_ids(self.NODES, 3, 4)
        existing = ["home", "groc"]
        self.assertNotIn("home", selectable)
        offscreen = [p for p in existing if p not in selectable]
        onscreen = [p for p in existing if p in selectable]
        # What the flow saves: what came back, plus what was never shown.
        saved = list(dict.fromkeys(onscreen + offscreen))
        self.assertEqual(set(saved), set(existing))

    def test_unticking_an_on_screen_list_still_removes_it(self):
        selectable = selectable_ids(self.NODES, 3, 4)
        existing = ["home", "groc"]
        offscreen = [p for p in existing if p not in selectable]
        saved = list(dict.fromkeys([] + offscreen))     # user unticked Groceries
        self.assertEqual(saved, ["home"])


class CyclicTreeTest(unittest.TestCase):
    """A parent chain that loops must not hang the picker.

    The server should never send one. That is exactly why nothing downstream would catch it, and
    why the guard belongs here: inbound data is untrusted because the channel is attackable, not
    because the server is suspect. Unguarded, each of these walks appends forever until the process
    dies, with the config flow open and no way out.
    """

    CYCLE = [
        {"id": "a", "name": "A", "parent_id": "b", "depth": 2},
        {"id": "b", "name": "B", "parent_id": "a", "depth": 2},
    ]

    def test_reachable_ids_terminates(self):
        self.assertEqual(_picker_tree._reachable_ids(self.CYCLE), {"a", "b"})

    def test_tree_labels_terminates(self):
        """Empty, not partial: a cycle hangs off no root, so nothing is drawable."""
        self.assertEqual(tree_labels(self.CYCLE), {})

    def test_a_node_dangling_off_a_cycle_does_not_hang(self):
        nodes = self.CYCLE + [{"id": "c", "name": "C", "parent_id": "a", "depth": 3}]
        self.assertEqual(tree_labels(nodes), {})
