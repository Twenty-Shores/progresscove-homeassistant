"""Constants for the ProgressCove integration."""

DOMAIN = "progresscove"

MIN_HA_VERSION = (2026, 3)

CONF_BASE_URL = "base_url"
CONF_TOKEN = "token"
CONF_PROJECTS = "projects"
# Stable across restarts: the server binds a token to the first installation that uses it, so a
# regenerated id would lock this Home Assistant out of its own token.
CONF_DEVICE_ID = "device_id"
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

# Remove an entity once the server confirms its task is gone. On by default, because people delete
# tasks in the app, where Home Assistant cannot see it; off raises a repair instead.
CONF_AUTO_PRUNE = "auto_prune"
DEFAULT_AUTO_PRUNE = True

# Completing an occurrence that has not arrived rolls a repeat past the real one, so it is refused
# by default. Opt in if you want a task completable whenever you like, early or not.
CONF_COMPLETE_EARLY = "complete_early"
DEFAULT_COMPLETE_EARLY = False

DEFAULT_SCAN_MINUTES = 1
MIN_SCAN_MINUTES = 1
MAX_SCAN_MINUTES = 60

# Named by position, not by tier: a list entity is whatever node the user picked, so "tasks" and
# "subtasks" would only be true when it happened to be a T2.
ATTR_NESTED_DONE = "nested_items_done"
ATTR_NESTED_TOTAL = "nested_items_total"
ATTR_NODE_ID = "node_id"
ATTR_ITEMS_DONE = "items_done"
ATTR_ITEMS_TOTAL = "items_total"
# Derived deliberately: a state trigger compares one attribute to a fixed value, so without these
# every "is it finished" automation needed a template condition.
ATTR_ITEMS_COMPLETE = "items_complete"
ATTR_ITEMS_PCT = "items_percent"
