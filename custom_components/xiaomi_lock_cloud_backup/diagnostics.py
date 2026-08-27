"""Redacted diagnostics for Xiaomi Lock Cloud Video Backup."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .manager import BackupManager


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, object]:
    """Return counts and fixed codes only, never upstream identifiers."""
    manager = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(manager, BackupManager):
        return {"status": "not_loaded"}
    return manager.safe_diagnostics()
