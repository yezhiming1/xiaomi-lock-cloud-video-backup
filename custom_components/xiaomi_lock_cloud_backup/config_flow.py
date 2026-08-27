"""Credential-free configuration flow."""

from __future__ import annotations

from typing import Any, Mapping

from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol

from .const import (
    CONF_KEEP_AUDIO,
    CONF_MAX_DOWNLOADS_PER_RUN,
    CONF_OUTPUT_SUBDIRECTORY,
    CONF_RETENTION_DAYS,
    CONF_SCHEDULE_TIME,
    CONF_TARGET_MODEL,
    DOMAIN,
    MUTABLE_OPTION_KEYS,
    XIAOMI_MIOT_DOMAIN,
    default_options,
)
from .models import BackupError
from .settings import validate_settings


def _schema(defaults: Mapping[str, object]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_TARGET_MODEL,
                default=defaults[CONF_TARGET_MODEL],
            ): str,
            vol.Required(
                CONF_SCHEDULE_TIME,
                default=defaults[CONF_SCHEDULE_TIME],
            ): str,
            vol.Required(
                CONF_OUTPUT_SUBDIRECTORY,
                default=defaults[CONF_OUTPUT_SUBDIRECTORY],
            ): str,
            vol.Required(
                CONF_RETENTION_DAYS,
                default=defaults[CONF_RETENTION_DAYS],
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
            vol.Required(
                CONF_MAX_DOWNLOADS_PER_RUN,
                default=defaults[CONF_MAX_DOWNLOADS_PER_RUN],
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required(
                CONF_KEEP_AUDIO,
                default=defaults[CONF_KEEP_AUDIO],
            ): bool,
        }
    )


def _options_schema(defaults: Mapping[str, object]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SCHEDULE_TIME,
                default=defaults[CONF_SCHEDULE_TIME],
            ): str,
            vol.Required(
                CONF_RETENTION_DAYS,
                default=defaults[CONF_RETENTION_DAYS],
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
            vol.Required(
                CONF_MAX_DOWNLOADS_PER_RUN,
                default=defaults[CONF_MAX_DOWNLOADS_PER_RUN],
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required(
                CONF_KEEP_AUDIO,
                default=defaults[CONF_KEEP_AUDIO],
            ): bool,
        }
    )


class XiaomiLockCloudBackupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create the only config entry; no account fields are accepted."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            xiaomi_data = self.hass.data.get(XIAOMI_MIOT_DOMAIN)
            sessions = (
                xiaomi_data.get("sessions") if isinstance(xiaomi_data, dict) else None
            )
            if not isinstance(sessions, dict) or not sessions:
                errors["base"] = "xiaomi_miot_not_ready"
            else:
                try:
                    normalized = validate_settings(user_input)
                except BackupError:
                    errors["base"] = "invalid_settings"
                else:
                    return self.async_create_entry(
                        title="Xiaomi Lock Cloud Video Backup",
                        data=normalized,
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or default_options()),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return XiaomiLockCloudBackupOptionsFlow(config_entry)


class XiaomiLockCloudBackupOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {
            **default_options(),
            **dict(self._entry.data),
            **dict(self._entry.options),
        }
        if user_input is not None:
            try:
                normalized = validate_settings({**dict(self._entry.data), **user_input})
            except BackupError:
                errors["base"] = "invalid_settings"
            else:
                return self.async_create_entry(
                    title="",
                    data={key: normalized[key] for key in MUTABLE_OPTION_KEYS},
                )
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(user_input or current),
            errors=errors,
        )
