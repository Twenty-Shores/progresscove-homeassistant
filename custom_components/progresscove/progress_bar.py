"""Whether a list draws its progress bar, by the rule the ProgressCove apps use.

A life area (tier 1) never completes, so its bar is hidden unless the person turned it on; every
tier below shows its bar unless they turned it off. The server sends the choice as
`hide_progress_bar`, and null, or a server too old to send it, means the tier decides.
"""

from __future__ import annotations

from typing import Any


def progress_bar_hidden(node: dict[str, Any]) -> bool:
    choice = node.get("hide_progress_bar")
    if isinstance(choice, bool):
        return choice
    return node.get("depth") == 1
