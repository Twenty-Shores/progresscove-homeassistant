"""A lapsed subscription, as the server sends it, through the real client.

The server answers it with a 403 whose body carries `code: subscription_required`, the same on
production and on main. It must never open a reauth flow: a new token would be refused the same way.
"""

from datetime import timedelta
from http import HTTPStatus

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from stand_in import ACCOUNT, MY_DAY, SERVER, TREE

API = f"{SERVER}/api/v1"
LAPSED = {"detail": "An active subscription is required.", "code": "subscription_required"}


def _serve(aioclient_mock: AiohttpClientMocker, status: HTTPStatus = HTTPStatus.OK) -> None:
    aioclient_mock.clear_requests()
    if status is HTTPStatus.OK:
        aioclient_mock.get(f"{API}/nodes", params={"include": "reminders"}, json={"nodes": TREE})
        aioclient_mock.get(f"{API}/widget/myday", json={"items": MY_DAY})
        aioclient_mock.get(f"{API}/nodes/inbox", json={"id": ACCOUNT})
        return
    body = LAPSED if status is HTTPStatus.FORBIDDEN else {"detail": "Invalid or expired token."}
    for path in ("/nodes", "/widget/myday", "/nodes/inbox"):
        aioclient_mock.get(f"{API}{path}", status=status, json=body)


def _issue(hass: HomeAssistant, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        "progresscove", f"subscription_inactive_{entry.entry_id}"
    )


def _reauths(hass: HomeAssistant, entry) -> list:
    return list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


@pytest.fixture
def real_client(no_frontend: None, aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    return aioclient_mock


async def test_lapsed_at_setup_retries_by_itself_and_says_why(hass, entry, real_client):
    _serve(real_client, HTTPStatus.FORBIDDEN)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, entry)
    assert issue is not None and not issue.is_fixable
    assert issue.translation_key == "subscription_inactive"
    assert _reauths(hass, entry) == []

    _serve(real_client)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    # Home Assistant retries a setup in a background task.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(hass, entry) is None


async def test_lapsing_while_running_marks_the_lists_unavailable_and_recovers(
    hass, entry, real_client
):
    _serve(real_client)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    my_day = "todo.progresscove_my_day"
    assert hass.states.get(my_day).state != "unavailable"

    _serve(real_client, HTTPStatus.FORBIDDEN)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(my_day).state == "unavailable"
    assert _issue(hass, entry) is not None
    assert _reauths(hass, entry) == []
    assert entry.state is ConfigEntryState.LOADED

    _serve(real_client)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(my_day).state != "unavailable"
    assert _issue(hass, entry) is None


async def test_a_rejected_token_still_asks_for_a_new_one(hass, entry, real_client):
    _serve(real_client, HTTPStatus.UNAUTHORIZED)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauths(hass, entry)) == 1
    assert _issue(hass, entry) is None
