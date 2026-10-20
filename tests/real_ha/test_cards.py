"""The cards' dashboard resources, against Home Assistant's own resource collection and Store."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
import pytest

from custom_components.progresscove import frontend

CARD = "progresscove-card.js"


@pytest.fixture
async def resources(hass: HomeAssistant, hass_storage: dict[str, Any]):
    hass_storage["lovelace_resources"] = {"version": 1, "key": "lovelace_resources",
                                          "data": {"items": []}}
    collection = ResourceStorageCollection(hass, None)
    await collection.async_load()
    collection.loaded = True
    hass.data[LOVELACE_DATA] = SimpleNamespace(resource_mode="storage", resources=collection)
    return collection


async def _restart_and_register(hass: HomeAssistant, version: str = "1.0") -> None:
    """What survives a restart is .storage and the dashboards' resources, not hass.data."""
    hass.data.pop(frontend._CARDS_REGISTERED, None)
    with patch.object(frontend, "_cache_key", lambda _: f"{version}.abcd1234"):
        await frontend.async_register_cards(hass)


def _ours(collection: ResourceStorageCollection, card: str = CARD) -> list[dict[str, Any]]:
    return [item for item in collection.async_items()
            if item["url"].startswith(f"{frontend.URL_BASE}/{card}?")]


async def test_each_card_is_added_once(hass: HomeAssistant, resources):
    await _restart_and_register(hass)
    await _restart_and_register(hass)
    assert len(resources.async_items()) == len(frontend.CARDS)


async def test_a_resource_the_person_deleted_is_not_added_back(hass: HomeAssistant, resources):
    await _restart_and_register(hass)
    [card] = _ours(resources)
    await resources.async_delete_item(card["id"])
    await _restart_and_register(hass)
    await _restart_and_register(hass, version="1.1")
    assert _ours(resources) == []
    assert len(resources.async_items()) == len(frontend.CARDS) - 1


async def test_a_new_version_moves_our_resource_in_place(hass: HomeAssistant, resources):
    await _restart_and_register(hass)
    [before] = _ours(resources)
    await _restart_and_register(hass, version="1.1")
    [after] = _ours(resources)
    assert after["id"] == before["id"]
    assert after["url"].endswith("?v=1.1.abcd1234")


async def test_the_last_entry_takes_ours_and_leaves_theirs(hass: HomeAssistant, resources):
    await resources.async_create_item({"res_type": "module", "url": "/local/theirs.js"})
    await _restart_and_register(hass)
    await frontend.async_remove_cards(hass)
    assert [item["url"] for item in resources.async_items()] == ["/local/theirs.js"]
    await _restart_and_register(hass)
    assert len(resources.async_items()) == len(frontend.CARDS) + 1


async def test_yaml_mode_takes_its_extra_modules_out_again(hass: HomeAssistant):
    hass.data[LOVELACE_DATA] = SimpleNamespace(resource_mode="yaml", resources=None)
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda change, url: None, [])
    await _restart_and_register(hass)
    assert len(hass.data[DATA_EXTRA_MODULE_URL].urls) == len(frontend.CARDS)
    await frontend.async_remove_cards(hass)
    assert hass.data[DATA_EXTRA_MODULE_URL].urls == frozenset()
