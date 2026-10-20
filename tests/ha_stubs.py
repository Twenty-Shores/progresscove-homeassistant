"""Stand-ins for the Home Assistant pieces more than one test file needs to agree on.

Shared rather than redefined per file: a module loaded by one file raises the class another file
catches, so two copies of an exception class would make `assertRaises` miss. The exceptions take
the same translation keywords as the real ones and render the English message from strings.json,
so a test asserts on the words a person would read.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import enum
import json
from pathlib import Path
import sys
import types
from typing import Any
import uuid

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
STRINGS = COMPONENT / "strings.json"


class HomeAssistantError(Exception):
    def __init__(self, *args, translation_domain=None, translation_key=None,
                 translation_placeholders=None):
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders

    def __str__(self):
        if self.args or not self.translation_key:
            return super().__str__()
        catalogue = json.loads(STRINGS.read_text())
        template = catalogue["exceptions"][self.translation_key]["message"]
        return template.format(**(self.translation_placeholders or {}))


class ServiceValidationError(HomeAssistantError):
    pass


class UpdateFailed(HomeAssistantError):
    """Takes the real one's retry_after, which a running coordinator waits out before polling."""

    def __init__(self, *args, retry_after=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ConfigEntryState:
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"
    SETUP_RETRY = "setup_retry"


@dataclass(frozen=True, kw_only=True)
class ConfigSubentry:
    """The real one's fields, with an id minted the way Home Assistant mints one."""

    data: Any
    subentry_type: str
    title: str
    unique_id: str | None
    subentry_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class HassJob:
    """Holds what a shutdown job runs, as the real one does."""

    def __init__(self, target):
        self.target = target


class ClientTimeout:
    def __init__(self, total=None):
        self.total = total


class BackgroundEntry:
    """A config entry's background-task hook, run on the test's own loop."""

    def __init__(self, options=None):
        self.options = options or {}
        self.task_names: list[str] = []

    def async_create_background_task(self, hass, target, name, eager_start=True):
        self.task_names.append(name)
        return asyncio.get_running_loop().create_task(target, name=name)


async def async_get_translations(hass, language, category, integrations=None):
    """The English catalogue, flattened the way Home Assistant hands it to an integration."""
    catalogue = json.loads(STRINGS.read_text())
    flattened = {}

    def flatten(prefix, node):
        for key, child in node.items():
            if isinstance(child, dict):
                flatten(f"{prefix}{key}.", child)
            else:
                flattened[f"{prefix}{key}"] = child

    for domain in integrations or ():
        flatten(f"component.{domain}.{category}.", catalogue.get(category, {}))
    return flattened


def _house_now() -> datetime:
    """Now in the house's zone, read when called, as the real `dt_util.now` reads it, so a test
    that swaps the zone moves "now" with it."""
    return datetime.now(sys.modules["homeassistant.util.dt"].get_default_time_zone())


def give_coordinator_availability(test, entity_class) -> None:
    """Make the stubbed CoordinatorEntity report availability the way the real one does.

    The real base answers from the coordinator's last poll. The per-file stubs do not define it at
    all, so it is attached for one test and taken away again.
    """
    base = next(
        cls for cls in entity_class.__mro__ if cls.__name__.lstrip("_") == "CoordinatorEntity"
    )
    if "available" in vars(base):
        return
    base.available = property(lambda entity: entity.coordinator.last_update_success)
    test.addCleanup(delattr, base, "available")


def fill(name, **attrs):
    """Give a stubbed module what it lacks, creating it if nobody has yet."""
    module = sys.modules.get(name) or types.ModuleType(name)
    module.__path__ = getattr(module, "__path__", [])
    for key, value in attrs.items():
        if not hasattr(module, key):
            setattr(module, key, value)
    sys.modules[name] = module
    return module


def fill_todo_platform() -> None:
    """The to-do platform's names, for a file that only needs the modules naming them to load.

    Not part of fill_shared: the files that test the entities define richer ones first, and these
    would take their place in a file that called fill_shared before defining its own.
    """
    fill("homeassistant.components.todo",
         TodoItem=dict, TodoListEntity=object,
         TodoItemStatus=types.SimpleNamespace(COMPLETED="completed", NEEDS_ACTION="needs_action"),
         TodoListEntityFeature=enum.IntFlag("TodoListEntityFeature", [
             "CREATE_TODO_ITEM", "DELETE_TODO_ITEM", "UPDATE_TODO_ITEM", "MOVE_TODO_ITEM",
             "SET_DUE_DATE_ON_ITEM", "SET_DUE_DATETIME_ON_ITEM", "SET_DESCRIPTION_ON_ITEM",
         ]))
    fill("homeassistant.helpers.update_coordinator",
         CoordinatorEntity=type("CoordinatorEntity", (), {
             "__class_getitem__": classmethod(lambda cls, item: cls)}),
         DataUpdateCoordinator=type("DataUpdateCoordinator", (), {
             "__class_getitem__": classmethod(lambda cls, item: cls)}),
         UpdateFailed=UpdateFailed)
    fill("homeassistant.exceptions",
         ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (HomeAssistantError,), {}))


def fill_shared() -> None:
    """The names the component imports that no test file asserts anything about."""
    fill("homeassistant")
    fill("homeassistant.const", PERCENTAGE="%", CONF_TOKEN="token", CONF_URL="url")
    fill("homeassistant.config_entries", ConfigEntry=object, ConfigEntryState=ConfigEntryState,
         ConfigSubentry=ConfigSubentry, ConfigSubentryData=dict)
    fill("homeassistant.core", HomeAssistant=object, HassJob=HassJob, callback=lambda f: f,
         ServiceCall=object, ServiceResponse=dict,
         SupportsResponse=types.SimpleNamespace(ONLY="only"))
    fill("homeassistant.exceptions", HomeAssistantError=HomeAssistantError,
         ServiceValidationError=ServiceValidationError)
    components = fill("homeassistant.components")
    notifications = fill("homeassistant.components.persistent_notification",
                         async_create=lambda *args, **kwargs: None)
    if not hasattr(components, "persistent_notification"):
        components.persistent_notification = notifications
    helpers = fill("homeassistant.helpers")
    issues = fill("homeassistant.helpers.issue_registry",
                  async_create_issue=lambda *args, **kwargs: None,
                  async_delete_issue=lambda *args, **kwargs: None,
                  IssueSeverity=types.SimpleNamespace(WARNING="warning", ERROR="error"))
    if not hasattr(helpers, "issue_registry"):
        helpers.issue_registry = issues
    translation = fill("homeassistant.helpers.translation",
                       async_get_translations=async_get_translations)
    if not hasattr(helpers, "translation"):
        helpers.translation = translation
    fill("homeassistant.helpers.entity", Entity=object)
    fill("homeassistant.helpers.service",
         async_register_platform_entity_service=lambda hass, *args, **kwargs: None)
    fill("homeassistant.components.todo", DOMAIN="todo")
    fill("homeassistant.helpers.device_registry", DeviceInfo=dict,
         DeviceEntryType=types.SimpleNamespace(SERVICE="service"))
    util = fill("homeassistant.util")
    house_time = fill("homeassistant.util.dt", get_default_time_zone=lambda: timezone.utc,
                      now=_house_now)
    if not hasattr(util, "dt"):
        util.dt = house_time
    fill("aiohttp", ClientTimeout=ClientTimeout)
