"""Node names, made safe to show. The user types them, so they are untrusted wherever they reach
a surface.

Its own module rather than part of helpers, which imports Home Assistant: picker_tree needs this
and is deliberately free of it.
"""

from __future__ import annotations

# Awkward to type on purpose, so a real name is unlikely to contain one.
PATH_SEPARATOR = " › "


def display_name(name: str | None) -> str:
    """A node's name, safe to show or to build a path out of.

    The separator is replaced because a path is split apart again by the cards: "Shopping › Frozen"
    under "Home" would otherwise be indistinguishable from "Frozen" under "Shopping", and a card
    following one would pull in the other's tasks. Control characters go because they can reorder
    or truncate a line wherever it is rendered.
    """
    cleaned = name or ""
    # Checked whole first: almost every name is clean, and a character-by-character pass over every
    # name on a large account is what made the pickers slow.
    if not cleaned.isprintable():
        cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return " ".join(cleaned.replace("›", "/").split())
