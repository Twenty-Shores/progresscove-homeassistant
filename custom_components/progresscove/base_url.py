"""Validations for the server address, so a token is never sent somewhere it should not go.

http is not outright refused, but it is flagged as insecure and the user must confirm that
they consider the destination safe enough.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def validate(raw: str) -> tuple[str, str | None, bool]:
    """Return (normalised_url, error_key, insecure).

    `error_key` names a problem the user must fix and matches the config flow's error catalogue.
    `insecure` is True for a usable http address: not an error, but something to confirm before
    the token is sent.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return url, "invalid_url", False

    parts = urlsplit(url)
    # Includes the no-scheme case, where urlsplit puts everything in `path` and a bare
    # "api.progresscove.com" would otherwise be requested as a relative path.
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return url, "invalid_url", False
    return url, None, parts.scheme == "http"
