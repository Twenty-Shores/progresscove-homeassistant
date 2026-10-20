"""Serve the Lovelace cards and register them with the frontend, so a fresh install has them in
the card picker with nothing to copy and no resource to add by hand.
"""

# The cards live in the component because HACS installs `custom_components/<domain>/` and nothing
# else; one left in `config/www/` would never reach a user.

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.lovelace.resources import ResourceStorageCollection

_LOGGER = logging.getLogger(__name__)

# Present while the cards are registered, so a second account does not register them again, and a
# new account after the last was removed does. Holds the extra module urls added this run, which
# have to be taken out again by hand: Home Assistant keeps them until it stops.
_CARDS_REGISTERED: HassKey[list[str]] = HassKey(f"{DOMAIN}_cards_registered")

# Which dashboard resources this integration added, by card file: the url it added, or None once
# the person removed that resource. Kept across restarts, because a resource that is missing could
# be one never added or one the person deleted, and only the second must stay deleted.
_RESOURCES_STORAGE_KEY = f"{DOMAIN}.resources"
_RESOURCES_STORAGE_VERSION = 1
type _AddedResources = dict[str, str | None]

URL_BASE = f"/{DOMAIN}/frontend"
CARDS = (
    "progresscove-add-card.js",
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


async def async_register_static_path(hass: HomeAssistant) -> None:
    """Serve the card files. Once per run: registering the same path twice raises."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "frontend"))]
    )


async def async_register_cards(hass: HomeAssistant) -> None:
    """Offer the cards to the frontend, unless they already are."""
    if _CARDS_REGISTERED in hass.data:
        return
    integration = await async_get_integration(hass, DOMAIN)
    version = await hass.async_add_executor_job(
        _cache_key, str(integration.version or "0")
    )

    # Home Assistant serves a registered static path with a month-long cache header, so the url
    # is the only thing that can retire an old copy.
    urls = {card: f"{URL_BASE}/{card}?v={version}" for card in CARDS}

    # A Lovelace RESOURCE, not just an extra module url. The frontend loads its resources before it
    # builds a dashboard; an extra module url is fetched alongside everything else, with nothing
    # ordering it against the card lookup. Losing that race renders every card as "Custom element
    # doesn't exist", which comes out as a configuration error and clears on the next reload,
    # because by then the module has arrived.
    #
    # Only one of the two: registering a url as BOTH a resource and an extra module asks the
    # frontend to load the same module down two paths that are not ordered against each other, and
    # a card can end up asked for before either has finished. The extra module url is the fallback,
    # used only when there is no resource list to write to.
    resources = await _writable_resources(hass)
    if resources is None:
        _LOGGER.debug("No dashboard resources to write to; cards load as extra modules")
        for url in urls.values():
            add_extra_js_url(hass, url)
        hass.data[_CARDS_REGISTERED] = list(urls.values())
        return
    await _sync_resources(hass, resources, urls)
    hass.data[_CARDS_REGISTERED] = []


async def async_remove_cards(hass: HomeAssistant) -> None:
    """Take the cards back out, once no account is left to use them.

    Only what this integration added: the resources it recorded adding, and the extra module urls
    of this run. Resources declared in YAML are the person's own file, which nothing here writes.
    The record goes too, so an account added later starts afresh.
    """
    for url in hass.data.pop(_CARDS_REGISTERED, []):
        remove_extra_js_url(hass, url)
    store = _store(hass)
    resources = await _writable_resources(hass)
    if resources is not None:
        added = set((await _load_added(store)).values())
        for existing in list(resources.async_items()):
            if existing.get("url") in added:
                await resources.async_delete_item(existing["id"])
    await store.async_remove()


def _store(hass: HomeAssistant) -> Store[dict[str, _AddedResources]]:
    return Store(hass, _RESOURCES_STORAGE_VERSION, _RESOURCES_STORAGE_KEY)


async def _load_added(store: Store[dict[str, _AddedResources]]) -> _AddedResources:
    return dict((await store.async_load() or {}).get("cards", {}))


async def _writable_resources(hass: HomeAssistant) -> ResourceStorageCollection | None:
    """The dashboard resource collection, loaded, or None when there is none we may write to."""
    lovelace = hass.data.get(LOVELACE_DATA)
    # Resources declared in YAML are read from configuration.yaml and cannot be written to; the
    # collection for them has no create or delete at all.
    if lovelace is None or lovelace.resource_mode != MODE_STORAGE:
        return None
    resources = lovelace.resources
    if not resources.loaded:
        await resources.async_load()
    return resources


async def _sync_resources(
    hass: HomeAssistant, resources: ResourceStorageCollection, urls: dict[str, str]
) -> None:
    """Add each card's resource once, keep the ones we added on the current url, and leave a
    resource the person deleted deleted.

    A card never added before is added, which is also how a card new in this version arrives. One
    we added is moved to the current url when the version changes, so an upgrade does not leave an
    old copy loading beside the new one. One we added that is no longer there was removed by the
    person, and is recorded as theirs to bring back rather than re-added on every restart.
    """
    store = _store(hass)
    added = await _load_added(store)
    present = {item.get("url"): item for item in resources.async_items()}
    for card, url in urls.items():
        if card not in added:
            if url not in present:
                await resources.async_create_item({"res_type": "module", "url": url})
            added[card] = url
            continue
        ours = added[card]
        if ours is None:
            continue
        if ours not in present:
            _LOGGER.debug("The %s resource was removed from the dashboards; not adding it", card)
            added[card] = None
        elif ours != url:
            await resources.async_update_item(present[ours]["id"], {"url": url})
            added[card] = url
    await store.async_save({"cards": added})
