"""Constants for the ProgressCove integration."""

from datetime import timedelta

DOMAIN = "progresscove"

# The list pickers' multi-select. A choice is stored as list subentries, never under this key.
CONF_LISTS = "lists"
# Stable across restarts: the server binds a token to the first installation that uses it, so a
# regenerated id would lock this Home Assistant out of its own token.
CONF_DEVICE_ID = "device_id"
CONF_TOKEN_SCOPE = "token_scope"
CONF_NODE_ID = "node_id"
# How the button/switch pickers order and label their options. "leaf" leads with the task's own
# name, what someone types; "root" leads with the path, what someone browsing a branch reads.
CONF_NODE_SORT = "node_sort"
SORT_BY_LEAF = "leaf"
SORT_BY_ROOT = "root"
DEFAULT_NODE_SORT = SORT_BY_LEAF
CONF_MIN_DEPTH = "min_depth"
CONF_MAX_DEPTH = "max_depth"
# 1-3 covers domains, projects and tasks, the tiers a list is usually drawn from. T4 subtasks are
# reachable by widening, rather than filling the picker by default.
DEFAULT_MIN_DEPTH = 1
DEFAULT_MAX_DEPTH = 3
# The deepest tier the product defines (T8), so the picker never silently caps a legitimate tree.
MAX_TIER_DEPTH = 8
# The API's NodeStatus. 2 is completed; anything else is still open.
STATUS_COMPLETED = 2
# Closed without being done: the server sets it on a still-open child when an ancestor is
# completed over it. Not open any more, so a surface that only knows COMPLETED draws it as an
# unticked box under a ticked parent.
STATUS_CANCELLED = 4

# A row is drawn ticked for either: the distinction matters to the app, not to a checkbox.
CLOSED_STATUSES = frozenset({STATUS_COMPLETED, STATUS_CANCELLED})

DEFAULT_BASE_URL = "https://api.progresscove.com"

SUBENTRY_LIST = "list"
# A header button rather than a kind of row: its flow adds and removes list subentries and never
# creates one of its own.
SUBENTRY_MANAGE_LISTS = "manage_lists"
SUBENTRY_BUTTON = "button"
SUBENTRY_SWITCH = "switch"
SUBENTRY_SENSOR = "sensor"

# Remove an entity once the server confirms its task is gone. Off by default: removing an entity can
# break an automation or dashboard that names it, so the person is asked through a repair first.
CONF_AUTO_PRUNE = "auto_prune"
DEFAULT_AUTO_PRUNE = False

# Completing an occurrence that has not arrived rolls a repeat past the real one, so it is refused
# by default. Opt in if you want a task completable whenever you like, early or not.
CONF_COMPLETE_EARLY = "complete_early"
DEFAULT_COMPLETE_EARLY = False

# How soon a change made in the app shows here. Writes from Home Assistant never wait for it.
UPDATE_INTERVAL = timedelta(minutes=1)
# How often a lapsed subscription is asked about again once the entry is running. Long enough not
# to poll a closed account every minute for weeks, short enough that a renewal shows up while the
# person who renewed is still looking.
SUBSCRIPTION_RETRY = timedelta(minutes=5)

# Named by position, not by tier: a list entity is whatever node the user picked, so "tasks" and
# "subtasks" would only be true when it happened to be a T2.
ATTR_NESTED_DONE = "nested_items_done"
ATTR_NESTED_TOTAL = "nested_items_total"
ATTR_NODE_ID = "node_id"
# What a card reads to draw itself without tap targets.
ATTR_READ_ONLY = "read_only"
ATTR_ITEMS_DONE = "items_done"
ATTR_ITEMS_TOTAL = "items_total"
# Derived deliberately: a state trigger compares one attribute to a fixed value, so without these
# every "is it finished" automation needed a template condition.
ATTR_ITEMS_COMPLETE = "items_complete"
ATTR_ITEMS_PCT = "items_percent"
# What the projects card reads to leave a list's bar out, by the same rule as the apps.
ATTR_PROGRESS_BAR_HIDDEN = "progress_bar_hidden"

TOKEN_SCOPE_HEADER = "X-Token-Scope"
ERROR_CODE_HEADER = "X-Error-Code"
ERROR_CODE_READ_ONLY = "pat_read_only"
# The `code` in the body of a 403, not a header: it is how the server says the account's
# subscription is not active, and what it answers while it cannot confirm one either.
ERROR_CODE_SUBSCRIPTION_INACTIVE = "subscription_required"
SCOPE_READ = "read"
SCOPE_READ_WRITE = "read_write"

READ_ONLY_MESSAGE = (
    "This ProgressCove token is read-only, so nothing was changed. Create a read and write "
    "token and paste it under Reconfigure on the integration page."
)
