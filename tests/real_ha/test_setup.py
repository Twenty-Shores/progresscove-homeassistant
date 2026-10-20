"""Setting up from the config flow, and what an entry is once it is up."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from stand_in import ACCOUNT, SERVER


async def test_each_list_picked_at_setup_is_a_subentry(hass: HomeAssistant, stand_in_server):
    flow = await hass.config_entries.flow.async_init("progresscove", context={"source": "user"})
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"url": SERVER, "token": "pat_x"}
    )
    assert flow["step_id"] == "lists"
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"lists": ["groceries", "garden"]}
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries("progresscove")[0]
    assert entry.state is ConfigEntryState.LOADED
    lists = {s.data["node_id"]: s for s in entry.subentries.values()}
    assert sorted(lists) == ["garden", "groceries"]
    assert {s.unique_id for s in lists.values()} == {"list_garden", "list_groceries"}
    registry = er.async_get(hass)
    rows = {
        e.unique_id: e.config_subentry_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "todo"
    }
    assert rows[f"{ACCOUNT}-groceries"] == lists["groceries"].subentry_id
    assert rows[f"{ACCOUNT}-{ACCOUNT}"] is None


async def test_unloading_sends_a_completion_still_inside_its_window(hass, loaded, server):
    await hass.services.async_call("progresscove", "complete", {"node_id": "chores"},
                                   blocking=True)
    assert loaded.runtime_data.pending.is_pending("chores")
    assert server.completed == []
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert server.completed == ["chores"]


async def test_stopping_home_assistant_sends_it_without_waiting_out_the_window(
    hass, loaded, server
):
    await hass.services.async_call("progresscove", "complete", {"node_id": "chores"},
                                   blocking=True)
    started = hass.loop.time()
    await hass.async_stop()
    assert server.completed == ["chores"]
    assert hass.loop.time() - started < 5
