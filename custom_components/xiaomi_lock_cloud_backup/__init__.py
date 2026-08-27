"""Xiaomi Lock Cloud Video Backup integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from .const import DOMAIN, SERVICE_RUN_BACKUP
from .manager import BackupManager
from .models import BackupError


SERVICE_SCHEMA = vol.Schema({vol.Optional("dry_run", default=False): bool})


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register the single response-capable service."""
    hass.data.setdefault(DOMAIN, {})

    async def async_handle_run_backup(call: ServiceCall) -> dict[str, object]:
        managers = hass.data.get(DOMAIN, {})
        loaded = [item for item in managers.values() if isinstance(item, BackupManager)]
        if len(loaded) != 1:
            raise HomeAssistantError("BACKUP_ENTRY_NOT_READY")
        try:
            return await loaded[0].async_run(dry_run=bool(call.data["dry_run"]))
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
    return True


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
