"""The integration inside a real Home Assistant, with only the ProgressCove server stood in for.

The stubbed suite next door checks each module against what it expects Home Assistant to do. This
one runs the flows, the reloads and the shutdown in Home Assistant itself, which is where a
reload storm or a reauth that can never finish actually shows.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from stand_in import ACCOUNT, SERVER, Server, list_subentry

from custom_components import progresscove as integration


@dataclass
class Setups:
    """How many times the entry has been set up since the count was last reset."""

    count: int = 0


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Every test here loads the integration from this repository."""


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def setups() -> Generator[Setups]:
    counted = Setups()
    real = integration.async_setup_entry

    async def counting(hass: HomeAssistant, entry: Any) -> bool:
        counted.count += 1
        return await real(hass, entry)

    with patch.object(integration, "async_setup_entry", counting):
        yield counted


@pytest.fixture
def no_frontend(hass: HomeAssistant) -> Generator[None]:
    """No web frontend to serve cards to.

    The integration depends on http and frontend, which need a listening server; they are marked
    as set up instead, and registering the cards is left to the tests that are about it.
    """
    hass.config.components.update({"http", "frontend", "lovelace"})

    async def nothing(*args: Any) -> None:
        return None

    with (
        patch.object(integration, "async_register_static_path", nothing),
        patch.object(integration, "async_register_cards", nothing),
        patch.object(integration, "async_remove_cards", nothing),
    ):
        yield


@pytest.fixture
def stand_in_server(no_frontend: None, server: Server) -> Generator[Server]:
    """The stand-in server in place of the real client."""
    with (
        patch.object(integration, "ProgressCoveClient", server.client),
        patch("custom_components.progresscove.config_flow.ProgressCoveClient", server.client),
    ):
        yield server


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """An account with Groceries and Garden as lists, not set up yet."""
    config_entry = MockConfigEntry(
        domain="progresscove",
        title="ProgressCove",
        unique_id=ACCOUNT,
        data={CONF_URL: SERVER, CONF_TOKEN: "pat_x", "device_id": "device-1",
              "token_scope": "read_write"},
        subentries_data=[list_subentry("groceries", "Groceries"),
                         list_subentry("garden", "Garden")],
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
async def loaded(
    hass: HomeAssistant, entry: MockConfigEntry, stand_in_server: Server, setups: Setups
) -> MockConfigEntry:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    setups.count = 0
    return entry
