"""Serve the Lovelace cards and register them with the frontend, so a fresh install has them in
the card picker with nothing to copy and no resource to add by hand.
"""

# The cards live in the component because HACS installs `custom_components/<domain>/` and nothing
# else; one left in `config/www/` would never reach a user.

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Set once the static path is registered: doing it twice raises, and a second account would.
_REGISTERED = f"{DOMAIN}_frontend"

URL_BASE = f"/{DOMAIN}/frontend"
CARDS = (
    "progresscove-card.js",
    "progresscove-icon-card.js",
    "progresscove-myday-card.js",
)


async def async_register(hass: HomeAssistant) -> None:
    """Called once per config entry, and safe to call again: the path is registered only the
    first time, and the frontend keeps its module urls in a set."""
    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version or "0")
    if not hass.data.get(_REGISTERED):
        hass.data[_REGISTERED] = True
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "frontend"))]
        )

    # The version is what busts the cache. These are served with long cache headers, so without it
    # a browser holds an old card after an upgrade.
    for card in CARDS:
        add_extra_js_url(hass, f"{URL_BASE}/{card}?v={version}")
    _LOGGER.debug("Registered %d ProgressCove cards at %s", len(CARDS), URL_BASE)
