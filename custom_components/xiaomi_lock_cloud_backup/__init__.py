"""Xiaomi Lock Cloud Video Backup integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from .const import (
    DEFAULT_HISTORY_MAX_DOWNLOADS,
    DOMAIN,
    MAX_HISTORY_DOWNLOADS_PER_RUN,
    SERVICE_RUN_BACKUP,
    SERVICE_RUN_HISTORY_BACKFILL,
)
from .manager import BackupManager
from .models import BackupError


SERVICE_SCHEMA = vol.Schema({vol.Optional("dry_run", default=False): bool})
HISTORY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("dry_run", default=False): bool,
        vol.Optional(
            "max_downloads",
            default=DEFAULT_HISTORY_MAX_DOWNLOADS,
        ): vol.All(
            vol.Coerce(int),
            vol.Range(min=1, max=MAX_HISTORY_DOWNLOADS_PER_RUN),
        ),
    }
)


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register response-capable incremental and historical services."""
    hass.data.setdefault(DOMAIN, {})

    async def async_handle_run_backup(call: ServiceCall) -> dict[str, object]:
        manager = _single_loaded_manager(hass)
        try:
            return await manager.async_run(dry_run=bool(call.data["dry_run"]))
        except BackupError as exc:
            raise HomeAssistantError(exc.code) from None
        except Exception:
            raise HomeAssistantError("BACKUP_UNEXPECTED") from None

    async def async_handle_history_backfill(call: ServiceCall) -> dict[str, object]:
        manager = _single_loaded_manager(hass)
        try:
            return await manager.async_run_history_backfill(
                dry_run=bool(call.data["dry_run"]),
                max_downloads=int(call.data["max_downloads"]),
            )
        except BackupError as exc:
            raise HomeAssistantError(exc.code) from None
        except Exception:
            raise HomeAssistantError("BACKUP_UNEXPECTED") from None

    if not hass.services.has_service(DOMAIN, SERVICE_RUN_BACKUP):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_BACKUP,
            async_handle_run_backup,
            schema=SERVICE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_HISTORY_BACKFILL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_HISTORY_BACKFILL,
            async_handle_history_backfill,
            schema=HISTORY_SERVICE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    return True


def _single_loaded_manager(hass: HomeAssistant) -> BackupManager:
    managers = hass.data.get(DOMAIN, {})
    loaded = [item for item in managers.values() if isinstance(item, BackupManager)]
    if len(loaded) != 1:
        raise HomeAssistantError("BACKUP_ENTRY_NOT_READY")
    return loaded[0]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one configuration entry without accessing credential storage."""
    manager = BackupManager(hass, entry)
    await manager.async_initialize()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(manager, BackupManager) and not await manager.async_shutdown():
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
