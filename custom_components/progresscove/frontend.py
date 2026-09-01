"""Serve the Lovelace cards and register them with the frontend, so a fresh install has them in
the card picker with nothing to copy and no resource to add by hand.
"""

# The cards live in the component because HACS installs `custom_components/<domain>/` and nothing
# else; one left in `config/www/` would never reach a user.

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from homeassistant.components.lovelace.const import LOVELACE_DATA

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


def _cache_key(version: str) -> str:
    """The version, plus a digest of the cards themselves.

    A released version alone is not enough: the files change many times under one version while
    they are being worked on, and a browser told to cache them for a month has no reason to ask
    again. Keying on the contents means an edited card is a different url and an unchanged one
    stays cached.
    """
    digest = hashlib.sha256()
    for card in CARDS:
        digest.update((Path(__file__).parent / "frontend" / card).read_bytes())
    return f"{version}.{digest.hexdigest()[:8]}"


async def async_register(hass: HomeAssistant) -> None:
    """Called once per config entry, and safe to call again: the path is registered only the
    first time, and the frontend keeps its module urls in a set."""
    integration = await async_get_integration(hass, DOMAIN)
    version = await hass.async_add_executor_job(
        _cache_key, str(integration.version or "0")
    )
    if not hass.data.get(_REGISTERED):
        hass.data[_REGISTERED] = True
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "frontend"))]
        )

    # Home Assistant serves a registered static path with a month-long cache header, so the url
    # is the only thing that can retire an old copy.
    urls = [f"{URL_BASE}/{card}?v={version}" for card in CARDS]

    # A Lovelace RESOURCE, not just an extra module url. The frontend loads its resources before it
    # builds a dashboard; an extra module url is fetched alongside everything else, with nothing
    # ordering it against the card lookup. Losing that race renders every card as "Custom element
    # doesn't exist", which comes out as a configuration error and clears on the next reload,
    # because by then the module has arrived.
    registered = await _register_resources(hass, urls)

    # Only one of the two: registering a url as BOTH a resource and an extra module asks the
    # frontend to load the same module down two paths that are not ordered against each other, and
    # a card can end up asked for before either has finished. The resource list is the one the
    # dashboard waits on, so the extra module url is the fallback, used only when there is no
    # resource store to write to.
    if not registered:
        for url in urls:
            add_extra_js_url(hass, url)
    _LOGGER.debug("Registered %d ProgressCove cards at %s", len(CARDS), URL_BASE)


async def _register_resources(hass: HomeAssistant, urls: list[str]) -> bool:
    """Put the cards in the dashboard resource list, replacing any url we wrote before.

    Ours are recognised by their path, so an upgrade retires the previous version's url rather
    than leaving a stale one behind to load an old card alongside the new one.
    """
    resources = hass.data.get(LOVELACE_DATA)
    resources = getattr(resources, "resources", None)
    if resources is None:
        _LOGGER.debug("No dashboard resource store; cards load as extra modules only")
        return False
    if not resources.loaded:
        await resources.async_load()

    wanted = set(urls)
    for existing in list(resources.async_items()):
        url = existing.get("url", "")
        if not url.startswith(f"{URL_BASE}/"):
            continue
        if url in wanted:
            wanted.discard(url)
        else:
            await resources.async_delete_item(existing["id"])

    for url in sorted(wanted):
        await resources.async_create_item({"res_type": "module", "url": url})
    return True
