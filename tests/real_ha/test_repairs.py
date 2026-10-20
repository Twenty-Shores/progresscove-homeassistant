"""The deleted-tasks repair, through Home Assistant's own repairs flow manager."""

from homeassistant.components.repairs import issue_handler, repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from stand_in import ACCOUNT


async def test_confirming_the_repair_removes_the_deleted_list(
    hass: HomeAssistant, loaded, server, setups
):
    server.nodes = [node for node in server.nodes if node["id"] not in ("groceries", "milk")]
    server.deleted = {"groceries"}
    await loaded.runtime_data.async_refresh()
    issue_id = f"stale_entities_{loaded.entry_id}"
    issue = ir.async_get(hass).async_get_issue("progresscove", issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders == {"names": "Groceries"}

    hass.data["repairs"] = {}
    issue_handler.async_setup(hass)
    await issue_handler.async_process_repairs_platforms(hass)
    manager = repairs_flow_manager(hass)
    form = await manager.async_init("progresscove", data={"issue_id": issue_id})
    assert form["step_id"] == "confirm"
    result = await manager.async_configure(form["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "groceries" not in {s.data["node_id"] for s in loaded.subentries.values()}
    assert er.async_get(hass).async_get_entity_id(
        "todo", "progresscove", f"{ACCOUNT}-groceries"
    ) is None
    assert ir.async_get(hass).async_get_issue("progresscove", issue_id) is None
    assert setups.count == 1
