"""Both ways to change the lists, and the one kind of row either produces."""

from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
import pytest
from stand_in import ACCOUNT


def _todo_rows(hass: HomeAssistant, entry) -> dict[str, str | None]:
    return {
        e.unique_id: e.config_subentry_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "todo"
    }


def _lists(entry) -> list[str]:
    return sorted(s.data["node_id"] for s in entry.subentries.values()
                  if s.subentry_type == "list")


async def _start(hass: HomeAssistant, entry, kind: str):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": "user"}
    )


def _options(form) -> set[str]:
    field = next(iter(form["data_schema"].schema))
    return {option["value"] for option in form["data_schema"].schema[field].config["options"]}


async def test_the_header_shows_manage_lists_then_add_a_list(hass: HomeAssistant, loaded):
    """The frontend draws one header button per key, in this order, labelled from here."""
    assert list(loaded.supported_subentry_types)[:2] == ["manage_lists", "list"]
    words = await async_get_translations(hass, "en", "config_subentries", ["progresscove"])
    prefix = "component.progresscove.config_subentries"
    assert words[f"{prefix}.manage_lists.initiate_flow.user"] == "Manage lists"
    assert words[f"{prefix}.list.initiate_flow.user"] == "Add a list"
    assert words[f"{prefix}.list.entry_type"] == "List"


async def test_manage_lists_adds_and_removes_in_one_submit(hass: HomeAssistant, loaded, setups):
    form = await _start(hass, loaded, "manage_lists")
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {"lists": ["groceries", "shopping"]}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "lists_updated"
    assert _lists(loaded) == ["groceries", "shopping"]
    rows = _todo_rows(hass, loaded)
    assert f"{ACCOUNT}-garden" not in rows
    assert rows[f"{ACCOUNT}-shopping"] is not None
    assert setups.count == 1


async def test_add_a_list_makes_one_ordinary_list_row(hass: HomeAssistant, loaded, setups):
    form = await _start(hass, loaded, "list")
    offered = _options(form)
    assert offered == {"root", "shopping", "chores"}, "lists, the inbox and tier 4 are not offered"
    before = set(loaded.subentries)
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {"node_id": "shopping"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    [made] = [s for key, s in loaded.subentries.items() if key not in before]
    assert (made.subentry_type, made.unique_id, dict(made.data), made.title) == (
        "list", "list_shopping", {"node_id": "shopping"}, "Shopping"
    )
    assert _todo_rows(hass, loaded)[f"{ACCOUNT}-shopping"] == made.subentry_id
    assert before <= set(loaded.subentries)
    assert setups.count == 1

    hass.config_entries.async_remove_subentry(loaded, made.subentry_id)
    await hass.async_block_till_done()
    assert f"{ACCOUNT}-shopping" not in _todo_rows(hass, loaded)


async def test_add_a_list_refuses_a_node_added_while_its_form_was_open(
    hass: HomeAssistant, loaded
):
    form = await _start(hass, loaded, "list")
    manage = await _start(hass, loaded, "manage_lists")
    await hass.config_entries.subentries.async_configure(
        manage["flow_id"], {"lists": ["groceries", "garden", "shopping"]}
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {"node_id": "shopping"}
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")
    assert _lists(loaded) == ["garden", "groceries", "shopping"]


async def test_home_assistant_itself_refuses_a_second_list_for_a_node(hass, loaded):
    with pytest.raises(AbortFlow, match="already_configured"):
        hass.config_entries.async_add_subentry(loaded, ConfigSubentry(
            data=MappingProxyType({"node_id": "garden"}), subentry_type="list",
            title="Garden", unique_id="list_garden",
        ))


async def test_a_node_can_be_a_list_and_a_button_at_once(hass: HomeAssistant, loaded):
    form = await _start(hass, loaded, "button")
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {"node_id": "garden"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {s.unique_id for s in loaded.subentries.values()} >= {"list_garden", "button_garden"}
