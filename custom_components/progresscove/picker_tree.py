"""Draw the account's tree in the label text of a multi-select.

A select option is `{value, label}` and nothing else, so the structure has to live inside the
label. Box-drawing characters rather than leading spaces, which HTML rendering collapses.
"""

from __future__ import annotations

from typing import Any

from .names import display_name

# A node that already holds things to check off, versus one that does not yet.
HOLDS_ITEMS = "\U0001f4c1"
EMPTY = "·"
# A row shown only so its children have somewhere to hang. There is no disabled flag on an option,
# so the marker is the only hint, and the flow refuses these on submit rather than trusting it.
CONTEXT_ONLY = "—"


def _children_of(nodes: list[dict[str, Any]], parent_id: str | None) -> list[dict[str, Any]]:
    """Direct children, ordered by name so the tree is stable between openings of the picker."""
    return sorted(
        (n for n in nodes if n.get("parent_id") == parent_id),
        key=lambda n: display_name(n.get("name")).lower(),
    )


def tree_labels(
    nodes: list[dict[str, Any]],
    min_depth: int | None = None,
    max_depth: int | None = None,
) -> dict[str, str]:
    """`id → label`, in tree order, ready for a select with `sort=False`.

    Order is load-bearing: re-sorting the options would leave the connectors pointing at the wrong
    rows.

    `min_depth`/`max_depth` narrow which tiers are OFFERED, not which are walked: a list under a
    hidden tier stays reachable, and counts keep reporting the real number of children.
    """
    labels: dict[str, str] = {}

    def selectable(node: dict[str, Any]) -> bool:
        depth = node.get("depth")
        if depth is None:
            return True
        if min_depth is not None and depth < min_depth:
            return False
        return not (max_depth is not None and depth > max_depth)

    def leads_anywhere(node: dict[str, Any]) -> bool:
        """Whether this node or anything beneath it can be picked. A branch with nothing
        selectable is dropped entirely, since it would answer no question."""
        if selectable(node):
            return True
        return any(leads_anywhere(child) for child in _children_of(nodes, node["id"]))

    def render(parent_id: str | None, prefix: str, at_root: bool) -> None:
        # Only rows that will be DRAWN decide the connectors, or a dropped branch leaves a trunk
        # running down to nothing.
        children = [n for n in _children_of(nodes, parent_id) if leads_anywhere(n)]
        for index, node in enumerate(children):
            is_last = index == len(children) - 1
            # A root has nothing above it to hang from, so it gets no connector.
            connector = "" if at_root else prefix + ("└─ " if is_last else "├─ ")
            count = len(_children_of(nodes, node["id"]))
            if selectable(node):
                marker = HOLDS_ITEMS if count else EMPTY
                suffix = f"  ({count} {'child' if count == 1 else 'children'})" if count else ""
            else:
                # Context: it keeps its children reachable and readable, and nothing more.
                marker = CONTEXT_ONLY
                suffix = ""
            labels[node["id"]] = f"{connector}{marker} {display_name(node.get('name'))}{suffix}"
            render(
                node["id"],
                prefix if at_root else prefix + ("   " if is_last else "│  "),
                False,
            )

    render(None, "", True)

    # A node whose parent is missing from the payload never gets walked, and a scoped token can
    # produce one. Falling out of the tree is not a reason to vanish from the picker; being
    # excluded by the depth filter is, since the user asked not to see it.
    reachable = _reachable_ids(nodes)
    for node in nodes:
        if node["id"] not in labels and node["id"] not in reachable and selectable(node):
            labels[node["id"]] = f"{EMPTY} {display_name(node.get('name'))}"
    return labels


def selectable_ids(
    nodes: list[dict[str, Any]],
    min_depth: int | None = None,
    max_depth: int | None = None,
) -> set[str]:
    """The ids a submission may actually contain, context rows are drawn but not choosable."""
    def ok(node: dict[str, Any]) -> bool:
        depth = node.get("depth")
        if depth is None:
            return True
        if min_depth is not None and depth < min_depth:
            return False
        return not (max_depth is not None and depth > max_depth)

    return {n["id"] for n in nodes if ok(n)}


def _reachable_ids(nodes: list[dict[str, Any]]) -> set[str]:
    """Every node the tree walk visits, i.e. one whose ancestors are all present in the payload."""
    by_id = {n["id"]: n for n in nodes}
    reachable: set[str] = set()
    for node in nodes:
        chain, current = [], node
        # `reachable` alone does not terminate a walk: it holds nodes already proven reachable, so
        # a cycle among nodes not yet in it loops forever. `walked` is this walk's own history.
        walked: set[str] = set()
        while current is not None and current["id"] not in walked:
            if current["id"] in reachable:
                break
            walked.add(current["id"])
            chain.append(current["id"])
            parent = current.get("parent_id")
            current = by_id.get(parent) if parent else None
            if parent and current is None:
                chain = []          # parent named but absent: this branch never gets walked
                break
        reachable.update(chain)
    return reachable
