"""My Day carries no list in its state; the card reads the items and asks for the rest."""

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest

MY_DAY = "todo.progresscove_my_day"


async def test_the_state_carries_no_list(hass: HomeAssistant, loaded):
    attributes = hass.states.get(MY_DAY).attributes
    assert attributes["read_only"] is False
    assert "items" not in attributes
    assert "sections" not in attributes


async def test_the_items_come_from_get_items_in_the_servers_order(hass: HomeAssistant, loaded):
    answer = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": MY_DAY}, blocking=True,
        return_response=True,
    )
    assert [item["summary"] for item in answer[MY_DAY]["items"]] == ["Chores", "Garden"]


async def test_the_details_answer_what_an_item_cannot_carry(hass: HomeAssistant, loaded):
    answer = await hass.services.async_call(
        "progresscove", "get_my_day_details", {}, target={"entity_id": MY_DAY}, blocking=True,
        return_response=True,
    )
    assert answer == {MY_DAY: {"items": [
        {"uid": "chores", "section": "due", "time_utc": "2026-10-19T07:00:00+00:00",
         "path": "Home › Chores", "emoji": "🧹"},
        {"uid": "garden", "section": "ongoing", "time_utc": None, "path": "Home › Garden",
         "emoji": None},
    ]}}


async def test_the_details_refuse_another_list(hass: HomeAssistant, loaded):
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call(
            "progresscove", "get_my_day_details", {},
            target={"entity_id": "todo.progresscove_home_shopping_groceries"}, blocking=True,
            return_response=True,
        )
    assert caught.value.translation_key == "not_my_day"
