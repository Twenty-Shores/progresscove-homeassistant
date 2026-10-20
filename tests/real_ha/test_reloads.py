"""One action, one reload, however many changes it makes.

Each case holds a completion first: its flush makes the reload's unload wait, which is exactly when
every further change used to start a reload of its own.
"""

from homeassistant.core import HomeAssistant
from stand_in import Server


async def _hold_a_completion(hass: HomeAssistant, entry) -> None:
    await hass.services.async_call("progresscove", "complete", {"node_id": "chores"},
                                   blocking=True)
    assert entry.runtime_data.pending.is_pending("chores")


def _bulk(server: Server, count: int) -> list[str]:
    ids = [f"bulk-{index:02d}" for index in range(count)]
    server.nodes += [{"id": node_id, "name": node_id, "parent_id": "root", "depth": 2}
                     for node_id in ids]
    return ids


async def _manage(hass: HomeAssistant, entry, lists: list[str]) -> None:
    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "manage_lists"), context={"source": "user"}
    )
    await hass.config_entries.subentries.async_configure(form["flow_id"], {"lists": lists})
    await hass.async_block_till_done()


async def test_thirty_lists_added_at_once_reload_once(hass, loaded, server, setups):
    bulk = _bulk(server, 30)
    await loaded.runtime_data.async_refresh()
    await _hold_a_completion(hass, loaded)
    await _manage(hass, loaded, ["groceries", "garden", *bulk])
    assert len(loaded.subentries) == 32
    assert setups.count == 1


async def test_thirty_lists_removed_at_once_reload_once(hass, loaded, server, setups):
    bulk = _bulk(server, 30)
    await loaded.runtime_data.async_refresh()
    await _manage(hass, loaded, ["groceries", "garden", *bulk])
    setups.count = 0
    await _hold_a_completion(hass, loaded)
    await _manage(hass, loaded, ["groceries"])
    assert len(loaded.subentries) == 1
    assert setups.count == 1


async def test_a_prune_pass_reloads_once(hass, loaded, server, setups):
    bulk = _bulk(server, 30)
    await loaded.runtime_data.async_refresh()
    await _manage(hass, loaded, ["groceries", "garden", *bulk])
    hass.config_entries.async_update_entry(loaded, options={"auto_prune": True})
    await hass.async_block_till_done()
    server.nodes = [node for node in server.nodes if node["id"] not in bulk]
    server.deleted = set(bulk)
    await _hold_a_completion(hass, loaded)
    setups.count = 0
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert setups.count == 1
    assert len(loaded.subentries) < 32, "nothing was pruned"


async def test_a_retitled_row_does_not_reload(hass, loaded, server, setups):
    server.nodes = [{**node, "name": "Garden beds"} if node["id"] == "garden" else node
                    for node in server.nodes]
    garden = next(s for s in loaded.subentries.values() if s.data["node_id"] == "garden")
    hass.config_entries.async_update_subentry(loaded, garden, title="Garden beds")
    await hass.async_block_till_done()
    assert setups.count == 0


async def test_reconfigure_reloads_once(hass: HomeAssistant, loaded, setups):
    flow = await hass.config_entries.flow.async_init(
        "progresscove", context={"source": "reconfigure", "entry_id": loaded.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"url": "https://pc.example.test"}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert loaded.data["url"] == "https://pc.example.test"
    assert setups.count == 1


async def test_reauth_reloads_once(hass: HomeAssistant, loaded, setups):
    result = await _reauth(hass, loaded, "pat_new")
    assert result["reason"] == "reauth_successful"
    assert loaded.data["token"] == "pat_new"
    assert setups.count == 1


async def test_reauth_with_the_same_token_once_paired_reloads_once(hass, loaded, setups):
    result = await _reauth(hass, loaded, "pat_x")
    assert result["reason"] == "reauth_successful"
    assert setups.count == 1


async def _reauth(hass: HomeAssistant, entry, token: str):
    flow = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"token": token})
    await hass.async_block_till_done()
    return result
