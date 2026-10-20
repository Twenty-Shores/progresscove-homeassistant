"""The account's tree, indexed once so every lookup is a dict access.

Free of Home Assistant, like picker_tree: the pickers index the tree they fetch with it too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .names import PATH_SEPARATOR, display_name


@dataclass
class Tree:
    """The tree, indexed once per poll so every entity lookup is a dict access."""

    children_of: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    # As the SERVER computed it. Re-deriving today here would give this surface a different one
    # than the app shows.
    my_day: list[dict[str, Any]] = field(default_factory=list)
    _paths: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def children(self, node_id: str) -> list[dict[str, Any]]:
        return self.children_of.get(node_id, [])

    def path_of(self, node_id: str) -> str:
        """`Home › Shopping › Groceries`, so two lists of the same name are tellable apart.

        Remembered per node and reused for its descendants, since a picker asks for the path of
        every node in the account. The seen-set is not padding: a parent chain that loops appends
        names until the process dies, and this runs on every poll.
        """
        if node_id in self._paths:
            return self._paths[node_id]
        names: list[str] = []
        seen: set[str] = set()
        above: str | None = None
        current = self.by_id.get(node_id)
        while current is not None and current["id"] not in seen:
            if current["id"] in self._paths:
                above = self._paths[current["id"]]
                break
            seen.add(current["id"])
            names.append(display_name(current["name"]))
            parent = current.get("parent_id")
            current = self.by_id.get(parent) if parent else None
        if above is not None:
            names.append(above)
        path = PATH_SEPARATOR.join(reversed(names))
        self._paths[node_id] = path
        return path

    @classmethod
    def from_nodes(
        cls, nodes: list[dict[str, Any]], my_day: list[dict[str, Any]] | None = None
    ) -> Tree:
        tree = cls()
        tree.by_id = {n["id"]: n for n in nodes}
        for node in nodes:
            parent = node.get("parent_id")
            if parent:
                tree.children_of.setdefault(parent, []).append(node)

        # display_order is the user's own arrangement; the id only breaks ties, so a list does
        # not reshuffle between polls.
        def order(item: dict[str, Any]) -> tuple[float, str]:
            return (item.get("display_order") if item.get("display_order") is not None else 1e9,
                    item["id"])

        for items in tree.children_of.values():
            items.sort(key=order)
        tree.my_day = my_day or []
        return tree
