"""Config flow: paste a token, choose which nodes become lists.

Also holds the options flow and the pickers behind "Add a button", "Add a switch" and "Add a
progress sensor".
"""

# NO NETWORK DISCOVERY, DELIBERATELY. There is no `zeroconf` or `dhcp` key in the manifest and
# there must not be one: mDNS is unauthenticated, so anything on the LAN can answer as a
# ProgressCove server, and a "found a server, set it up?" prompt is a fine way to get someone to
# paste their token into a stranger's box. Do not add discovery without deciding, out loud, how a
# rogue responder is rejected before the token field is ever shown.

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import ProgressCoveAuthError, ProgressCoveClient, ProgressCoveError
from .base_url import validate as validate_base_url
from .names import PATH_SEPARATOR, display_name
from .picker_tree import selectable_ids, tree_labels
from .const import (
    CONF_BASE_URL,
    CONF_DEVICE_ID,
    CONF_MAX_DEPTH,
    CONF_MIN_DEPTH,
    CONF_NODE_SORT,
    CONF_PROJECTS,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MIN_DEPTH,
    DEFAULT_NODE_SORT,
    CONF_AUTO_PRUNE,
    DEFAULT_AUTO_PRUNE,
    DEFAULT_SCAN_MINUTES,
    MAX_SCAN_MINUTES,
    MIN_SCAN_MINUTES,
    DOMAIN,
    MAX_TIER_DEPTH,
    SORT_BY_LEAF,
    SORT_BY_ROOT,
)

_LOGGER = logging.getLogger(__name__)


def _already_holds_items(nodes: list[dict[str, Any]]) -> list[str]:
    """The nodes to pre-tick: those that already have something to check off.

    The picker OFFERS every node, a childless task is a list the user is about to fill, and that
    is their call. But pre-ticking every node would mint an entity per shopping item on first
    connect, so the default is the set that reads as a list today. A starting point, not a rule.
    """
    with_children = {n.get("parent_id") for n in nodes if n.get("parent_id")}
    return [n["id"] for n in nodes if n["id"] in with_children]


# The shipped tier names. A user can rename these in the app, but the tree endpoint does not carry
# them. Past T4 the app has no distinct name either, so the number is the honest label.
_TIER_CHOICES = {
    **{1: "1 - Domain", 2: "2 - Project", 3: "3 - Task", 4: "4 - Subtask"},
    **{depth: f"{depth} - Tier {depth}" for depth in range(5, MAX_TIER_DEPTH + 1)},
}


def _node_selector(choices: dict[str, str]) -> selector.SelectSelector:
    """One node, labelled name-first so typing the name filters the list down to it.

    `Milk (Home › Shopping › Groceries)`, not the path first: the box matches on a prefix, so a
    path-first label means typing "Home" before "milk" narrows anything. The path stays in brackets
    to tell two nodes of the same name apart.
    """
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=node_id, label=label)
                for node_id, label in sorted(choices.items(), key=lambda kv: kv[1].lower())
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
            sort=False,
        )
    )


def _tier_selector() -> selector.SelectSelector:
    """The tiers, by name, as a dropdown that always shows what is chosen."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=str(depth), label=label)
                for depth, label in _TIER_CHOICES.items()
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
            sort=False,
        )
    )


def _list_selector(labels: dict[str, str]) -> selector.SelectSelector:
    """A searchable multi-select over the tree.

    `sort=False` is load-bearing, not a preference: the labels are box-drawing connectors that only
    line up if the rows stay in tree order.
    """
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=node_id, label=label)
                for node_id, label in labels.items()
            ],
            multiple=True,
            mode=selector.SelectSelectorMode.LIST,
            sort=False,
        )
    )


def _leaf_first(node_id: str, nodes: list[dict[str, Any]]) -> str:
    """`Milk (Home › Shopping › Groceries)`, the name to search for, then where it lives.

    For someone who knows what they want and types it. The path stays, in brackets, because two
    nodes may share a name and only their branch tells them apart.
    """
    by_id = {n["id"]: n for n in nodes}
    node = by_id.get(node_id)
    if node is None:
        return node_id
    parent_path = _path_of(node.get("parent_id"), nodes) if node.get("parent_id") else ""
    name = display_name(node.get("name"))
    return f"{name} ({parent_path})" if parent_path else name


def _node_labels(nodes: list[dict[str, Any]], node_sort: str) -> dict[str, str]:
    """Label every node the way this entry asked to see them."""
    if node_sort == SORT_BY_ROOT:
        return {n["id"]: _path_of(n["id"], nodes) for n in nodes}
    return {n["id"]: _leaf_first(n["id"], nodes) for n in nodes}


def _path_of(node_id: str, nodes: list[dict[str, Any]]) -> str:
    """`Home › Shopping › Groceries`, two lists both called "Groceries" have to be tellable apart.

    Guarded against a looping parent chain for the same reason Tree.path_of is: an unguarded walk
    appends names until the process dies, and here it would hang the config flow with no way out.
    """
    by_id = {n["id"]: n for n in nodes}
    names: list[str] = []
    seen: set[str] = set()
    current = by_id.get(node_id)
    while current is not None and current["id"] not in seen:
        seen.add(current["id"])
        names.append(display_name(current["name"]))
        parent = current.get("parent_id")
        current = by_id.get(parent) if parent else None
    return PATH_SEPARATOR.join(reversed(names))


class ProgressCoveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup conversation."""

    VERSION = 1

    def __init__(self) -> None:
        self._base_url: str = DEFAULT_BASE_URL
        self._token: str = ""
        self._all_nodes: list[dict[str, Any]] = []
        self._pairing_code: str | None = None
        self._device_id: str | None = None
        # Where pairing returns to. Setup and reauth both need the step, and it cannot
        # guess which one is running it.
        self._after_pairing = "projects"

    async def _async_load_projects(self) -> dict[str, str]:
        """Prove the credentials work and remember what they can see.

        Runs before an entry is written rather than at the first poll, so a mistyped token fails
        while the user is still looking at the field they typed it into.
        """
        client = ProgressCoveClient(
            async_get_clientsession(self.hass), self._base_url, self._token, self._device_id
        )
        try:
            await client.async_check_credentials()
            nodes = await client.async_get_tree()
        except ProgressCoveAuthError:
            return {"base": "invalid_auth"}
        except ProgressCoveError:
            return {"base": "cannot_connect"}

        self._all_nodes = nodes
        return {} if nodes else {"base": "no_projects"}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: where the server is, and the token to reach it with."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._base_url, url_error, insecure = validate_base_url(user_input[CONF_BASE_URL])
            self._token = user_input[CONF_TOKEN].strip()
            if url_error:
                errors = {CONF_BASE_URL: url_error}
            elif insecure:
                return await self.async_step_insecure()
            else:
                errors = await self._async_load_projects()
            if not errors:
                return await self.async_step_projects()
            # A fresh token is dormant until the owner confirms this installation, which looks
            # exactly like a bad one, so offer pairing rather than calling it invalid.
            if errors.get("base") == "invalid_auth":
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL, default=self._base_url): str,
                    vol.Required(CONF_TOKEN): str,
                }
            ),
            errors=errors,
            description_placeholders={"tokens_url": "Settings → Advanced → API tokens"},
        )

    async def async_step_insecure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """HTTP needs to be explicitly confirmed, because the token is sent in the clear.
        """
        if user_input is not None:
            errors = await self._async_load_projects()
            if not errors:
                return await self.async_step_projects()
            if errors.get("base") == "invalid_auth":
                return await self.async_step_pair()
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_BASE_URL, default=self._base_url): str,
                        vol.Required(CONF_TOKEN, default=self._token): str,
                    }
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="insecure",
            data_schema=vol.Schema({}),
            description_placeholders={"url": self._base_url},
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a code, tell the server we are showing it, wait for the owner to confirm it.

        Every token is minted dormant and reads nothing until this happens, so possession of the
        secret is not enough: the account owner has to consciously turn THIS installation on.

        Serves both setup and reauth, which is why `_after_pairing` says where to go next.
        """
        errors: dict[str, str] = {}

        if self._pairing_code is None:
            self._pairing_code = f"{secrets.randbelow(1_000_000):06d}"
            # An entry keeps one installation id for its lifetime, so reauth pairs with the id it
            # already has. A fresh one is minted only when there is nothing to reuse.
            self._device_id = self._device_id or secrets.token_urlsafe(16)
            client = ProgressCoveClient(
                async_get_clientsession(self.hass), self._base_url, self._token, self._device_id
            )
            try:
                await client.async_claim_pairing_code(self._pairing_code)
            except ProgressCoveError:
                # The claim is fire-and-forget by design (the server always answers 204), so a
                # failure here is the network, not a verdict on the token.
                self._pairing_code = None
                return self.async_abort(reason="cannot_connect")

        if user_input is not None:
            if self._after_pairing == "reauth":
                errors = await self._async_check_token(self._device_id)
                if not errors:
                    return self._save_reauth_token()
            else:
                errors = await self._async_load_projects()
                if not errors:
                    return await self.async_step_projects()
            # A 401 here means the owner has not confirmed the code yet, which is the ordinary
            # case on this screen. Anything else is its own problem and says so: reporting an
            # outage as "not paired yet" leaves someone re-entering a code at an unreachable
            # server.
            if errors.get("base") == "invalid_auth":
                errors = {"base": "not_paired_yet"}

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"code": self._pairing_code},
        )

    async def async_step_projects(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: which projects become to-do lists."""
        if user_input is not None:
            # One entry per account, not per project: re-running setup should reconfigure the
            # existing integration rather than quietly creating a second copy of everything.
            await self.async_set_unique_id(f"{self._base_url}::{self._token[:12]}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="ProgressCove",
                data={
                    CONF_BASE_URL: self._base_url,
                    CONF_TOKEN: self._token,
                    CONF_PROJECTS: user_input[CONF_PROJECTS],
                    CONF_DEVICE_ID: self._device_id,
                },
            )

        return self.async_show_form(
            step_id="projects",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROJECTS,
                        default=_already_holds_items(self._all_nodes),
                    ): _list_selector(self._list_labels()),
                }
            ),
        )

    def _list_labels(self) -> dict[str, str]:
        """The account's tree, drawn as label text, see picker_tree."""
        return tree_labels(self._all_nodes)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server address, without starting over.

        Only the address. A token change goes through reauth, which pairs; pointing an entry at a
        DIFFERENT account is a delete-and-re-add, since nothing else about the entry would still
        apply. Which lists are shown is "Add lists" on the integration page.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            self._base_url, url_error, insecure = validate_base_url(user_input[CONF_BASE_URL])
            self._token = entry.data[CONF_TOKEN]
            if url_error:
                errors = {CONF_BASE_URL: url_error}
            elif insecure:
                return await self.async_step_reconfigure_insecure()
            else:
                errors = await self._async_load_projects()
            if not errors:
                return self._save_credentials(entry)
        else:
            self._base_url = entry.data[CONF_BASE_URL]

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL, default=self._base_url): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_insecure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm an http address on reconfigure, for the same reason setup does."""
        if user_input is not None:
            errors = await self._async_load_projects()
            if not errors:
                return self._save_credentials(self._get_reconfigure_entry())
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_BASE_URL, default=self._base_url): str,
                        vol.Required(CONF_TOKEN, default=self._token): str,
                    }
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="reconfigure_insecure",
            data_schema=vol.Schema({}),
            description_placeholders={"url": self._base_url},
        )

    def _save_credentials(self, entry: ConfigEntry) -> ConfigFlowResult:
        """Write the new address, leaving the token and everything else as it was.

        The unique id moves with the address because it embeds both, and no mismatch guard: the
        same account behind a new address is exactly what this flow is for.
        """
        return self.async_update_reload_and_abort(
            entry,
            unique_id=f"{self._base_url}::{self._token[:12]}",
            data={**entry.data, CONF_BASE_URL: self._base_url},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """A revoked or expired token lands here rather than leaving the entry dead."""
        self._base_url = entry_data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a replacement token, then pair it.

        Every token is minted dormant until the account owner confirms this installation.
        Accepting it without pairing rejected every replacement token
        as invalid, which is the one thing this flow exists to do.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            self._token = user_input[CONF_TOKEN].strip()
            # The entry's own id, not a new one: the server pins an unbound token to the first id
            # it sees, including none, so a fresh id here would bind the token to a stranger.
            self._device_id = self._get_reauth_entry().data.get(CONF_DEVICE_ID)
            errors = await self._async_check_token(self._device_id)
            if not errors:
                return self._save_reauth_token()
            if errors.get("base") == "invalid_auth":
                self._after_pairing = "reauth"
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )

    def _save_reauth_token(self) -> ConfigFlowResult:
        """Store the replacement token, and the id it is now bound to."""
        return self.async_update_reload_and_abort(
            self._get_reauth_entry(),
            data_updates={CONF_TOKEN: self._token, CONF_DEVICE_ID: self._device_id},
        )

    async def _async_check_token(self, device_id: str | None) -> dict[str, str]:
        """Whether the current address and token can read anything, as a form error dict."""
        client = ProgressCoveClient(
            async_get_clientsession(self.hass), self._base_url, self._token, device_id
        )
        try:
            await client.async_check_credentials()
        except ProgressCoveAuthError:
            return {"base": "invalid_auth"}
        except ProgressCoveError:
            return {"base": "cannot_connect"}
        return {}

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """What a user ADDS from the integration card, one at a time.

        Deliberately not automatic: every node could be a list and every task a button, which on a
        real account is hundreds of entities nobody asked for, and entity ids are sticky, so the
        churn outlives the mistake.
        """
        return {
            "list": ListSubentryFlowHandler,
            "button": ButtonSubentryFlowHandler,
            "switch": SwitchSubentryFlowHandler,
            "sensor": SensorSubentryFlowHandler,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return ProgressCoveOptionsFlow()


class ProgressCoveOptionsFlow(OptionsFlow):
    """The CONFIGURE button: how the pickers behave, how often we poll, whether to prune.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # A select hands back the option's value as a string.
            min_depth = int(user_input[CONF_MIN_DEPTH])
            max_depth = max(int(user_input[CONF_MAX_DEPTH]), min_depth)
            return self.async_create_entry(
                data={
                    **user_input,
                    CONF_MIN_DEPTH: min_depth,
                    CONF_MAX_DEPTH: max_depth,
                }
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MIN_DEPTH,
                        default=str(options.get(CONF_MIN_DEPTH, DEFAULT_MIN_DEPTH)),
                    ): _tier_selector(),
                    vol.Required(
                        CONF_MAX_DEPTH,
                        default=str(options.get(CONF_MAX_DEPTH, DEFAULT_MAX_DEPTH)),
                    ): _tier_selector(),
                    vol.Required(
                        CONF_NODE_SORT,
                        default=options.get(CONF_NODE_SORT, DEFAULT_NODE_SORT),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=SORT_BY_LEAF, label="Task name first"
                                ),
                                selector.SelectOptionDict(
                                    value=SORT_BY_ROOT, label="Full path"
                                ),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            sort=False,
                        )
                    ),
                    vol.Optional(
                        CONF_AUTO_PRUNE,
                        default=options.get(CONF_AUTO_PRUNE, DEFAULT_AUTO_PRUNE),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "scan_minutes",
                        default=options.get("scan_minutes", DEFAULT_SCAN_MINUTES),
                        # Coerce(int) BEFORE Range: the form hands back a float, and 1.5 must
                        # become 1 rather than pass a float range check and reach timedelta.
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_MINUTES, max=MAX_SCAN_MINUTES),
                    ),
                }
            ),
        )


class _NodePickerSubentryFlow(ConfigSubentryFlow):
    """Shared plumbing for the pickers. The tree is fetched live, so a node added in the app is
    offered without reconfiguring anything."""

    async def _async_tree(self) -> list[dict[str, Any]]:
        entry = self._get_entry()
        client = ProgressCoveClient(
            async_get_clientsession(self.hass),
            entry.data[CONF_BASE_URL],
            entry.data[CONF_TOKEN],
            entry.data.get(CONF_DEVICE_ID),
        )
        return await client.async_get_tree()

    def _already_added(self, subentry_type: str) -> set[str]:
        """Nodes that already have one of these, so the picker does not offer a duplicate."""
        return {
            sub.data.get("node_id")
            for sub in self._get_entry().subentries.values()
            if sub.subentry_type == subentry_type
        }


class ListSubentryFlowHandler(_NodePickerSubentryFlow):
    """Add to-do lists. Multi-select, because adding four lists should not be four trips through
    a dialog."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        try:
            nodes = await self._async_tree()
        except ProgressCoveError:
            return self.async_abort(reason="cannot_connect")

        min_depth = entry.options.get(CONF_MIN_DEPTH, DEFAULT_MIN_DEPTH)
        max_depth = entry.options.get(CONF_MAX_DEPTH, DEFAULT_MAX_DEPTH)
        candidates = nodes
        selectable = selectable_ids(candidates, min_depth, max_depth)
        existing = entry.data.get(CONF_PROJECTS, [])
        offscreen = [p for p in existing if p not in selectable]

        if user_input is not None:
            chosen = user_input[CONF_PROJECTS]
            rejected = [p for p in chosen if p not in selectable]
            if rejected:
                return self.async_show_form(
                    step_id="user",
                    errors={CONF_PROJECTS: "not_selectable"},
                    data_schema=self._schema(candidates, min_depth, max_depth, chosen),
                )
            # entry.data, where todo.py reads it. A subentry would be a second source of truth
            # that produces no entity at all.
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_PROJECTS: list(dict.fromkeys(list(chosen) + offscreen)),
                },
            )
            return self.async_abort(reason="lists_updated")

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(
                candidates, min_depth, max_depth,
                [p for p in existing if p in selectable],
            ),
        )

    def _schema(
        self,
        candidates: list[dict[str, Any]],
        min_depth: int,
        max_depth: int,
        chosen: list[str],
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_PROJECTS, default=chosen): _list_selector(
                    tree_labels(candidates, min_depth, max_depth)
                )
            }
        )


class _SingleNodeSubentryFlow(_NodePickerSubentryFlow):
    """Pick one node.

    Button, switch and sensor differ only in what they make of it, and two copies of this form is
    how they drift apart.
    """

    _subentry_type: str

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        try:
            nodes = await self._async_tree()
        except ProgressCoveError:
            return self.async_abort(reason="cannot_connect")

        taken = self._already_added(self._subentry_type)
        node_sort = self._get_entry().options.get(CONF_NODE_SORT, DEFAULT_NODE_SORT)
        # Every node, at any tier: a wall button is often a subtask.
        labels = _node_labels(nodes, node_sort)
        choices = {
            node_id: label
            for node_id, label in labels.items()
            if node_id not in taken
        }
        if not choices:
            return self.async_abort(reason="no_tasks_left")

        if user_input is not None:
            node_id = user_input["node_id"]
            node = next((n for n in nodes if n["id"] == node_id), {})
            return self.async_create_entry(
                # The name, not the picker's label: a title is stored once and would keep that
                # day's formatting.
                title=display_name(node.get("name")) or choices[node_id],
                data={"node_id": node_id},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("node_id"): _node_selector(choices)}),
        )


class ButtonSubentryFlowHandler(_SingleNodeSubentryFlow):
    """Add one wall button for a task, press to complete today's occurrence."""

    _subentry_type = "button"


class SwitchSubentryFlowHandler(_SingleNodeSubentryFlow):
    """Add one switch for a node, readable state for automations, off to complete."""

    _subentry_type = "switch"


class SensorSubentryFlowHandler(_SingleNodeSubentryFlow):
    """Add one progress sensor, percent complete, so "finished" is a numeric trigger."""

    _subentry_type = "sensor"
