"""Config flow: paste a token, choose which nodes become lists.

Also holds the options flow and the pickers behind "Manage lists", "Add a list", "Add a button",
"Add a switch" and "Add a progress sensor".
"""

# NO NETWORK DISCOVERY, DELIBERATELY. There is no `zeroconf` or `dhcp` key in the manifest and
# there must not be one: mDNS is unauthenticated, so anything on the LAN can answer as a
# ProgressCove server, and a "found a server, set it up?" prompt is a fine way to get someone to
# paste their token into a stranger's box. Do not add discovery without deciding, out loud, how a
# rogue responder is rejected before the token field is ever shown.

from __future__ import annotations

import logging
import secrets
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import (
    ProgressCoveAuthError,
    ProgressCoveClient,
    ProgressCoveError,
    ProgressCoveSubscriptionInactive,
)
from .base_url import validate as validate_base_url
from .const import (
    CONF_AUTO_PRUNE,
    CONF_COMPLETE_EARLY,
    CONF_DEVICE_ID,
    CONF_LISTS,
    CONF_MAX_DEPTH,
    CONF_MIN_DEPTH,
    CONF_NODE_ID,
    CONF_NODE_SORT,
    CONF_TOKEN_SCOPE,
    DEFAULT_AUTO_PRUNE,
    DEFAULT_BASE_URL,
    DEFAULT_COMPLETE_EARLY,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MIN_DEPTH,
    DEFAULT_NODE_SORT,
    DOMAIN,
    MAX_TIER_DEPTH,
    SCOPE_READ,
    SORT_BY_LEAF,
    SORT_BY_ROOT,
    SUBENTRY_BUTTON,
    SUBENTRY_LIST,
    SUBENTRY_MANAGE_LISTS,
    SUBENTRY_SENSOR,
    SUBENTRY_SWITCH,
)
from .names import display_name
from .picker_tree import selectable_ids, tree_labels
from .tree import Tree

_LOGGER = logging.getLogger(__name__)


def _already_holds_items(nodes: list[dict[str, Any]]) -> list[str]:
    """The nodes to pre-tick: those that already have something to check off.

    The picker OFFERS every node, a childless task is a list the user is about to fill, and that
    is their call. But pre-ticking every node would mint an entity per shopping item on first
    connect, so the default is the set that reads as a list today. A starting point, not a rule.
    """
    with_children = {n.get("parent_id") for n in nodes if n.get("parent_id")}
    return [n["id"] for n in nodes if n["id"] in with_children]


def _without_inbox(nodes: list[dict[str, Any]], inbox_id: str | None) -> list[dict[str, Any]]:
    """The nodes a list can be made from: all but the inbox and what is filed under it.

    The inbox has its own entity, keyed exactly as a list of the same node would be, so a list of
    it collides with that entity. What sits in it is waiting to be sorted, not a list to keep.
    """
    children: dict[str | None, list[str]] = {}
    for node in nodes:
        children.setdefault(node.get("parent_id"), []).append(node["id"])
    inbox_branch: set[str] = set()
    waiting = [inbox_id] if inbox_id else []
    while waiting:
        node_id = waiting.pop()
        # A parent chain that loops would otherwise walk forever.
        if node_id not in inbox_branch:
            inbox_branch.add(node_id)
            waiting.extend(children.get(node_id, []))
    return [node for node in nodes if node["id"] not in inbox_branch]


def _row_title(node_id: str, tree: Tree) -> str:
    """What a subentry's row on the integration page is called: the node's name, or its path when
    the name has nothing printable to show.

    The name, not a picker's label: setup retitles every row from the tree, and a label would
    change format the first time it did.
    """
    return display_name(tree.by_id.get(node_id, {}).get("name")) or tree.path_of(node_id)


def _subentry_unique_id(subentry_type: str, node_id: str) -> str:
    """Unique among the entry's subentries, so Home Assistant itself refuses a second one.

    Qualified by type because Home Assistant compares a subentry's unique id against every other
    subentry of the entry, whatever its type, and one node may be a list and a button at once.
    """
    return f"{subentry_type}_{node_id}"


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
    """The tiers, by name, as a dropdown that always shows what is chosen.

    Named after the shipped tier names. A user can rename those in the app, but the tree endpoint
    does not carry them. Past T4 the app has no distinct name either, so the number is the label.
    """
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(depth) for depth in range(1, MAX_TIER_DEPTH + 1)],
            mode=selector.SelectSelectorMode.DROPDOWN,
            sort=False,
            translation_key="tier",
        )
    )


def _token_selector() -> selector.TextSelector:
    """A token field that does not show the token to whoever is looking at the screen."""
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
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


def _leaf_first(node: dict[str, Any], tree: Tree) -> str:
    """`Milk (Home › Shopping › Groceries)`, the name to search for, then where it lives.

    For someone who knows what they want and types it. The path stays, in brackets, because two
    nodes may share a name and only their branch tells them apart.
    """
    parent_path = tree.path_of(node["parent_id"]) if node.get("parent_id") else ""
    name = display_name(node.get("name"))
    return f"{name} ({parent_path})" if parent_path else name


def _node_labels(tree: Tree, node_sort: str) -> dict[str, str]:
    """Label every node the way this entry asked to see them."""
    if node_sort == SORT_BY_ROOT:
        return {node_id: tree.path_of(node_id) for node_id in tree.by_id}
    return {node_id: _leaf_first(node, tree) for node_id, node in tree.by_id.items()}


class ProgressCoveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup conversation."""

    VERSION = 1

    def __init__(self) -> None:
        self._base_url: str = DEFAULT_BASE_URL
        self._token: str = ""
        self._all_nodes: list[dict[str, Any]] = []
        self._pairing_code: str | None = None
        self._device_id: str | None = None
        self._token_scope: str | None = None
        self._account_id: str | None = None
        # Where pairing returns to. Setup and reauth both need the step, and it cannot
        # guess which one is running it.
        self._after_pairing = "lists"

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
            account_id = await client.async_get_inbox_id()
        except ProgressCoveAuthError:
            return {"base": "invalid_auth"}
        except ProgressCoveSubscriptionInactive:
            return {"base": "subscription_inactive"}
        except ProgressCoveError:
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error while checking the ProgressCove token")
            return {"base": "unknown"}

        self._all_nodes = nodes
        self._token_scope = client.token_scope
        self._account_id = account_id
        return {} if nodes else {"base": "no_projects"}

    async def _async_after_credentials(self) -> ConfigFlowResult:
        """Where a working token leads: the lists, unless it can only read, in which case say so
        first so the missing controls are not a surprise later.

        An account already set up stops here, before the lists are offered, rather than after the
        user has picked them: a second entry would duplicate every entity of the first.
        """
        await self.async_set_unique_id(self._account_id)
        self._abort_if_unique_id_configured()
        if self._token_scope == SCOPE_READ:
            return await self.async_step_read_only()
        return await self.async_step_lists()

    async def async_step_read_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warn that this token cannot change anything, and let the user carry on regardless.

        A read-only token is a legitimate choice, for a dashboard nobody should tap by accident, so
        this informs rather than blocks.
        """
        if user_input is not None:
            return await self.async_step_lists()
        return self.async_show_form(step_id="read_only", data_schema=vol.Schema({}))

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: where the server is, and the token to reach it with."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._base_url, url_error, insecure = validate_base_url(user_input[CONF_URL])
            self._token = user_input[CONF_TOKEN].strip()
            if url_error:
                errors = {CONF_URL: url_error}
            elif insecure:
                return await self.async_step_insecure()
            else:
                errors = await self._async_load_projects()
            if not errors:
                return await self._async_after_credentials()
            # A fresh token is dormant until the owner confirms this installation, which looks
            # exactly like a bad one, so offer pairing rather than calling it invalid.
            if errors.get("base") == "invalid_auth":
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=self._base_url): str,
                    vol.Required(CONF_TOKEN): _token_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_insecure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """HTTP needs to be explicitly confirmed, because the token is sent in the clear.
        """
        if user_input is not None:
            errors = await self._async_load_projects()
            if not errors:
                return await self._async_after_credentials()
            if errors.get("base") == "invalid_auth":
                return await self.async_step_pair()
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_URL, default=self._base_url): str,
                        vol.Required(CONF_TOKEN, default=self._token): _token_selector(),
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
                    return await self._async_save_reauth_token()
            elif self._after_pairing == "reconfigure":
                errors = await self._async_load_projects()
                if not errors:
                    return await self._async_save_reconfigured()
            else:
                errors = await self._async_load_projects()
                if not errors:
                    return await self._async_after_credentials()
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

    async def async_step_lists(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: which nodes become to-do lists.

        Each one is created as a list subentry of the new entry, the same thing "Manage lists"
        adds later, so a list chosen here has its own row and its own delete from the start.
        """
        offered = _without_inbox(self._all_nodes, self._account_id)
        if user_input is not None:
            chosen = set(user_input[CONF_LISTS])
            if not chosen <= {node["id"] for node in offered}:
                return self._show_lists(offered, {CONF_LISTS: "not_selectable"})
            tree = Tree.from_nodes(self._all_nodes)
            return self.async_create_entry(
                title="ProgressCove",
                data={
                    CONF_URL: self._base_url,
                    CONF_TOKEN: self._token,
                    CONF_DEVICE_ID: self._device_id,
                    CONF_TOKEN_SCOPE: self._token_scope,
                },
                subentries=[
                    ConfigSubentryData(
                        data={CONF_NODE_ID: node["id"]},
                        subentry_type=SUBENTRY_LIST,
                        title=_row_title(node["id"], tree),
                        unique_id=_subentry_unique_id(SUBENTRY_LIST, node["id"]),
                    )
                    for node in offered
                    if node["id"] in chosen
                ],
            )
        return self._show_lists(offered, {})

    def _show_lists(
        self, offered: list[dict[str, Any]], errors: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="lists",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LISTS,
                        default=_already_holds_items(offered),
                    ): _list_selector(tree_labels(offered)),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server address, or swap in a different token, without starting over.

        Leaving the token empty keeps the current one. A new token is dormant until paired, so one
        the server does not accept yet goes through the same pairing step setup uses. Which lists
        are shown is "Manage lists" on the integration page.
        """
        entry = self._get_reconfigure_entry()
        if user_input is None:
            self._base_url = entry.data[CONF_URL]
            return self._show_reconfigure({})

        self._base_url, url_error, insecure = validate_base_url(user_input[CONF_URL])
        self._token = user_input.get(CONF_TOKEN, "").strip() or entry.data[CONF_TOKEN]
        self._device_id = entry.data.get(CONF_DEVICE_ID)
        if url_error:
            return self._show_reconfigure({CONF_URL: url_error})
        if insecure:
            return await self.async_step_reconfigure_insecure()
        return await self._async_finish_reconfigure(await self._async_load_projects())

    def _show_reconfigure(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=self._base_url): str,
                    vol.Optional(CONF_TOKEN): _token_selector(),
                }
            ),
            errors=errors,
        )

    async def _async_finish_reconfigure(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Save when the credentials work; pair when a NEW token is merely not confirmed yet."""
        if not errors:
            return await self._async_save_reconfigured()
        entry = self._get_reconfigure_entry()
        if errors.get("base") == "invalid_auth" and self._token != entry.data[CONF_TOKEN]:
            self._after_pairing = "reconfigure"
            return await self.async_step_pair()
        return self._show_reconfigure(errors)

    async def _async_save_reconfigured(self) -> ConfigFlowResult:
        """Save a working address and token, unless they reach a different account.

        The configured lists, buttons and switches name nodes of ONE account. Another account's
        token would leave every one of them pointing at nothing, and the prune would then remove
        them as deleted, so the swap is refused up front rather than undone entity by entity.
        """
        await self.async_set_unique_id(self._account_id)
        self._abort_if_unique_id_mismatch(reason="wrong_account")
        return self._save_credentials(self._get_reconfigure_entry())

    async def async_step_reconfigure_insecure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm an http address on reconfigure, for the same reason setup does."""
        if user_input is not None:
            return await self._async_finish_reconfigure(await self._async_load_projects())

        return self.async_show_form(
            step_id="reconfigure_insecure",
            data_schema=vol.Schema({}),
            description_placeholders={"url": self._base_url},
        )

    def _save_credentials(self, entry: ConfigEntry) -> ConfigFlowResult:
        """Write the address and token this flow ended with, leaving everything else as it was."""
        return self._update_and_reload(
            entry,
            {
                **entry.data,
                CONF_URL: self._base_url,
                CONF_TOKEN: self._token,
                CONF_DEVICE_ID: self._device_id,
                CONF_TOKEN_SCOPE: self._token_scope,
            },
        )

    def _update_and_reload(self, entry: ConfigEntry, data: dict[str, Any]) -> ConfigFlowResult:
        """Save the entry's data and reload it exactly once.

        A loaded entry whose data changes is reloaded by its own update listener, so also reloading
        here, as `async_update_reload_and_abort` does, would set it up twice. An entry not loaded
        has no listener, and unchanged data (a token that only needed pairing) gives the listener
        nothing to act on, so those two are reloaded here.
        """
        reload_here = entry.state is not ConfigEntryState.LOADED or entry.data == data
        result = self.async_update_and_abort(entry, data=data)
        if reload_here:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """A revoked or expired token lands here rather than leaving the entry dead."""
        self._base_url = entry_data.get(CONF_URL, DEFAULT_BASE_URL)
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
                return await self._async_save_reauth_token()
            if errors.get("base") == "invalid_auth":
                self._after_pairing = "reauth"
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): _token_selector()}),
            errors=errors,
        )

    async def _async_save_reauth_token(self) -> ConfigFlowResult:
        """Store the replacement token, and the id it is now bound to, if it reaches this account.

        Refused for another account's token for the same reason reconfigure refuses it: every
        configured list and button names a node the other account does not have.
        """
        await self.async_set_unique_id(self._account_id)
        self._abort_if_unique_id_mismatch(reason="wrong_account")
        entry = self._get_reauth_entry()
        return self._update_and_reload(
            entry,
            {
                **entry.data,
                CONF_TOKEN: self._token,
                CONF_DEVICE_ID: self._device_id,
                CONF_TOKEN_SCOPE: self._token_scope,
            },
        )

    async def _async_check_token(self, device_id: str | None) -> dict[str, str]:
        """Whether the current address and token can read anything, as a form error dict."""
        client = ProgressCoveClient(
            async_get_clientsession(self.hass), self._base_url, self._token, device_id
        )
        try:
            await client.async_check_credentials()
            account_id = await client.async_get_inbox_id()
        except ProgressCoveAuthError:
            return {"base": "invalid_auth"}
        except ProgressCoveSubscriptionInactive:
            return {"base": "subscription_inactive"}
        except ProgressCoveError:
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error while checking the ProgressCove token")
            return {"base": "unknown"}
        self._token_scope = client.token_scope
        self._account_id = account_id
        return {}

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """What a user adds from the integration card: one node at a time, or lists in one form.

        Home Assistant draws a button at the top of the integration page per key, in this order.
        "Manage lists" only adds and removes subentries of the list type, which are what "Add a
        list" creates, so every list row is the same kind of row whichever button made it.

        Deliberately not automatic: every node could be a list and every task a button, which on a
        real account is hundreds of entities nobody asked for, and entity ids are sticky, so the
        churn outlives the mistake.
        """
        return {
            SUBENTRY_MANAGE_LISTS: ManageListsFlowHandler,
            SUBENTRY_LIST: ListSubentryFlowHandler,
            SUBENTRY_BUTTON: ButtonSubentryFlowHandler,
            SUBENTRY_SWITCH: SwitchSubentryFlowHandler,
            SUBENTRY_SENSOR: SensorSubentryFlowHandler,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ProgressCoveOptionsFlow()


class ProgressCoveOptionsFlow(OptionsFlow):
    """The CONFIGURE button: how the pickers behave, whether to complete early, whether to
    prune."""

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
                            options=[SORT_BY_LEAF, SORT_BY_ROOT],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            sort=False,
                            translation_key="node_sort",
                        )
                    ),
                    vol.Optional(
                        CONF_COMPLETE_EARLY,
                        default=options.get(
                            CONF_COMPLETE_EARLY, DEFAULT_COMPLETE_EARLY
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_AUTO_PRUNE,
                        default=options.get(CONF_AUTO_PRUNE, DEFAULT_AUTO_PRUNE),
                    ): selector.BooleanSelector(),
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
            entry.data[CONF_URL],
            entry.data[CONF_TOKEN],
            entry.data.get(CONF_DEVICE_ID),
        )
        return await client.async_get_tree()

    def _already_added(self, subentry_type: str) -> set[str]:
        """Nodes that already have one of these, so the picker does not offer a duplicate."""
        return {
            sub.data.get(CONF_NODE_ID)
            for sub in self._get_entry().subentries.values()
            if sub.subentry_type == subentry_type
        }


class ManageListsFlowHandler(_NodePickerSubentryFlow):
    """Manage lists: the account's tree with the current lists ticked.

    Every list is its own subentry, with its own row and its own delete. This form only saves a
    trip through "Add a list" per list: ticking adds one, unticking removes one, and the flow then
    ends without creating a subentry of its own, since the ones it changed are already saved.
    """

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        try:
            nodes = _without_inbox(await self._async_tree(), entry.unique_id)
        except ProgressCoveError:
            return self.async_abort(reason="cannot_connect")

        min_depth = entry.options.get(CONF_MIN_DEPTH, DEFAULT_MIN_DEPTH)
        max_depth = entry.options.get(CONF_MAX_DEPTH, DEFAULT_MAX_DEPTH)
        selectable = selectable_ids(nodes, min_depth, max_depth)
        # Only what the form shows can be unticked. A list outside the tiers on offer, or one whose
        # node is gone from the tree, keeps its subentry: its own row removes it, and the prune
        # removes one the server confirms was deleted.
        shown = {
            node_id: subentry_id
            for node_id, subentry_id in self._current_lists().items()
            if node_id in selectable
        }

        if user_input is None:
            return self._show_lists(nodes, min_depth, max_depth, list(shown))

        chosen = set(user_input[CONF_LISTS])
        if not chosen <= selectable:
            return self._show_lists(
                nodes, min_depth, max_depth, list(user_input[CONF_LISTS]),
                errors={CONF_LISTS: "not_selectable"},
            )
        added = [node["id"] for node in nodes if node["id"] in chosen - shown.keys()]
        removed = [subentry_id for node_id, subentry_id in shown.items() if node_id not in chosen]
        if not added and not removed:
            return self.async_abort(reason="lists_unchanged")
        tree = Tree.from_nodes(nodes)
        for node_id in added:
            self.hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({CONF_NODE_ID: node_id}),
                    subentry_type=SUBENTRY_LIST,
                    title=_row_title(node_id, tree),
                    unique_id=_subentry_unique_id(SUBENTRY_LIST, node_id),
                ),
            )
        for subentry_id in removed:
            self.hass.config_entries.async_remove_subentry(entry, subentry_id)
        return self.async_abort(reason="lists_updated")

    def _current_lists(self) -> dict[str, str]:
        """`node id → subentry id` for every list this entry has."""
        return {
            subentry.data[CONF_NODE_ID]: subentry_id
            for subentry_id, subentry in self._get_entry().subentries.items()
            if subentry.subentry_type == SUBENTRY_LIST
        }

    def _show_lists(
        self,
        nodes: list[dict[str, Any]],
        min_depth: int,
        max_depth: int,
        ticked: list[str],
        errors: dict[str, str] | None = None,
    ) -> SubentryFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LISTS, default=ticked): _list_selector(
                        tree_labels(nodes, min_depth, max_depth)
                    )
                }
            ),
            errors=errors,
        )


class _SingleNodeSubentryFlow(_NodePickerSubentryFlow):
    """Pick one node.

    List, button, switch and sensor differ only in which nodes they offer and what they make of the
    one picked, and two copies of this form is how they drift apart.
    """

    _subentry_type: str

    def _offered(self, nodes: list[dict[str, Any]]) -> set[str]:
        """Every node, at any tier: a wall button is often a subtask."""
        return {node["id"] for node in nodes}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        try:
            nodes = await self._async_tree()
        except ProgressCoveError:
            return self.async_abort(reason="cannot_connect")

        tree = Tree.from_nodes(nodes)
        taken = self._already_added(self._subentry_type)
        node_sort = self._get_entry().options.get(CONF_NODE_SORT, DEFAULT_NODE_SORT)
        labels = _node_labels(tree, node_sort)
        choices = {
            node_id: labels[node_id]
            for node_id in self._offered(nodes)
            if node_id not in taken
        }

        if user_input is not None:
            node_id = user_input[CONF_NODE_ID]
            # Checked again on submit, against the tree and the subentries as they are now: another
            # flow may have added this node since the form was drawn, or the app removed it.
            if node_id in taken:
                return self.async_abort(reason="already_configured")
            if node_id in choices:
                return self.async_create_entry(
                    title=_row_title(node_id, tree),
                    data={CONF_NODE_ID: node_id},
                    unique_id=_subentry_unique_id(self._subentry_type, node_id),
                )
            if choices:
                return self._show_choices(choices, {CONF_NODE_ID: "unavailable"})

        if not choices:
            return self.async_abort(reason="no_tasks_left")
        return self._show_choices(choices, {})

    def _show_choices(
        self, choices: dict[str, str], errors: dict[str, str]
    ) -> SubentryFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NODE_ID): _node_selector(choices)}),
            errors=errors,
        )


class ListSubentryFlowHandler(_SingleNodeSubentryFlow):
    """Add a list: one node, whose direct children become the list's items.

    Offered from the same nodes as "Manage lists": not the inbox, which has its own entity, and
    only the tiers the options allow.
    """

    _subentry_type = SUBENTRY_LIST

    def _offered(self, nodes: list[dict[str, Any]]) -> set[str]:
        entry = self._get_entry()
        return selectable_ids(
            _without_inbox(nodes, entry.unique_id),
            entry.options.get(CONF_MIN_DEPTH, DEFAULT_MIN_DEPTH),
            entry.options.get(CONF_MAX_DEPTH, DEFAULT_MAX_DEPTH),
        )


class ButtonSubentryFlowHandler(_SingleNodeSubentryFlow):
    """Add one wall button for a task, press to complete today's occurrence."""

    _subentry_type = SUBENTRY_BUTTON


class SwitchSubentryFlowHandler(_SingleNodeSubentryFlow):
    """Add one switch for a node, readable state for automations, off to complete."""

    _subentry_type = SUBENTRY_SWITCH


class SensorSubentryFlowHandler(_SingleNodeSubentryFlow):
    """Add one progress sensor, percent complete, so "finished" is a numeric trigger."""

    _subentry_type = SUBENTRY_SENSOR
