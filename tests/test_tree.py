"""Paths are remembered per node and reused for descendants, which must not change what they say.

The pickers ask for the path of every node in the account, so a path is built once and its
descendants extend it. A reused prefix that differed from a freshly walked one would label two
rows of the same branch differently.
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


def _load(name):
    spec = importlib.util.spec_from_file_location(f"progresscove.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"progresscove.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("names")
Tree = _load("tree").Tree

NODES = [
    {"id": "home", "name": "Home"},
    {"id": "shop", "name": "Shopping", "parent_id": "home"},
    {"id": "groc", "name": "Groceries", "parent_id": "shop"},
    {"id": "milk", "name": "Milk", "parent_id": "groc"},
]


class PathTest(unittest.TestCase):
    def test_a_descendant_asked_after_its_ancestor_reads_the_same_as_one_asked_first(self):
        ancestor_first = Tree.from_nodes(NODES)
        ancestor_first.path_of("shop")
        descendant_first = Tree.from_nodes(NODES)
        self.assertEqual(ancestor_first.path_of("milk"), descendant_first.path_of("milk"))
        self.assertEqual(descendant_first.path_of("milk"), "Home › Shopping › Groceries › Milk")
        self.assertEqual(descendant_first.path_of("shop"), "Home › Shopping")

    def test_a_looping_parent_chain_still_ends(self):
        tree = Tree.from_nodes([
            {"id": "a", "name": "A", "parent_id": "b"},
            {"id": "b", "name": "B", "parent_id": "a"},
            {"id": "c", "name": "C", "parent_id": "a"},
        ])
        self.assertEqual(tree.path_of("a"), "B › A")
        self.assertEqual(tree.path_of("c"), "B › A › C")

    def test_an_unknown_node_has_no_path(self):
        self.assertEqual(Tree.from_nodes(NODES).path_of("nope"), "")


if __name__ == "__main__":
    unittest.main()
