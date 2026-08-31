"""Pinned Home Assistant lifecycle check with synthetic, privacy-safe fixtures."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from homeassistant import loader
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

import custom_components.xiaomi_lock_cloud_backup
import custom_components.xiaomi_lock_cloud_backup.cloud
import custom_components.xiaomi_lock_cloud_backup.config_flow
import custom_components.xiaomi_lock_cloud_backup.diagnostics
import custom_components.xiaomi_lock_cloud_backup.hls
import custom_components.xiaomi_lock_cloud_backup.manager
from custom_components.xiaomi_lock_cloud_backup import (
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.xiaomi_lock_cloud_backup.config_flow import (
    _options_schema,
    _schema,
)
from custom_components.xiaomi_lock_cloud_backup.const import (
    CONF_EVENT_DELAY_SECONDS,
    CONF_EVENT_ENTITY_IDS,
    CONF_RETENTION_DAYS,
    DOMAIN,
    INTEGRATION_VERSION,
    MUTABLE_OPTION_KEYS,
    default_options,
)
from custom_components.xiaomi_lock_cloud_backup.settings import validate_settings


class FixtureCloud:
    default_server = "cn"
    locale = "en_US"

    async def async_get_devices(self, renew: bool = False) -> list[object]:
        del renew
        return []

    async def async_request_api(self, *_args: object, **_kwargs: object) -> dict:
        return {
            "code": 0,
            "data": {
                "thirdPartPlayUnits": [],
                "isContinue": False,
                "nextTime": 0,
            },
        }

    @staticmethod
    def get_api_by_host(host: str, api: str) -> str:
        return f"https://{host}/{api}"

    @staticmethod
    def json_encode(value: object) -> str:
        return json.dumps(value)

    @staticmethod
    def rc4_params(_method: str, _url: str, params: dict) -> dict:
        return params


class FixtureEntity:
    model = "xiaomi.lock.s1"
    miot_did = "fixture-device"

    def __init__(self, cloud: FixtureCloud) -> None:
        self.xiaomi_cloud = cloud


async def main() -> None:
    manifest = json.loads(
        Path(
            "/work/custom_components/xiaomi_lock_cloud_backup/manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["domain"] == DOMAIN
    assert manifest["version"] == INTEGRATION_VERSION == "0.0.8"

    candidate = default_options()
    candidate[CONF_RETENTION_DAYS] = "0"
    candidate[CONF_EVENT_DELAY_SECONDS] = 120
    candidate[CONF_EVENT_ENTITY_IDS] = [
        "event.fixture_pass",
        "event.fixture_stay",
    ]
    assert _schema(candidate)(candidate)[CONF_RETENTION_DAYS] == "0"
    option_candidate = {key: candidate[key] for key in MUTABLE_OPTION_KEYS}
    assert (
        _options_schema(option_candidate)(option_candidate)[CONF_RETENTION_DAYS]
        == "0"
    )
    assert validate_settings(candidate)[CONF_RETENTION_DAYS] == 0

    config_directory = Path("/tmp/ha-smoke")
    component_link = config_directory / "custom_components" / DOMAIN
    component_link.parent.mkdir(parents=True)
    os.symlink(
        "/work/custom_components/xiaomi_lock_cloud_backup",
        component_link,
        target_is_directory=True,
    )
    hass = HomeAssistant(str(config_directory))
    loader.async_setup(hass)
    await hass.async_start()
    integration = await loader.async_get_integration(hass, DOMAIN)
    assert integration.name == "Xiaomi Lock Cloud Video Backup"
    assert integration.version == "0.0.8"

    cloud = FixtureCloud()
    hass.data["xiaomi_miot"] = {
        "sessions": {"fixture": cloud},
        "entities": {"lock.fixture": FixtureEntity(cloud)},
    }
    assert await async_setup(hass, {})
    entry = ConfigEntry(
        data=default_options(),
        disabled_by=None,
        discovery_keys={},
        domain=DOMAIN,
        entry_id="fixture_entry",
        minor_version=1,
        options={},
        pref_disable_new_entities=False,
        pref_disable_polling=False,
        source="user",
        subentries_data=(),
        title="Fixture",
        unique_id=DOMAIN,
        version=1,
    )
    assert await async_setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, "run_backup")
    assert hass.services.has_service(DOMAIN, "run_history_backfill")
    assert hass.services.has_service(DOMAIN, "migrate_filenames")

    response = await hass.services.async_call(
        DOMAIN,
        "run_backup",
        {"dry_run": True},
        blocking=True,
        return_response=True,
    )
    assert response == {
        "status": "dry_run_ok",
        "dry_run": True,
        "available": 0,
        "selected": 0,
    }
    normal_response = await hass.services.async_call(
        DOMAIN,
        "run_backup",
        {"dry_run": False},
        blocking=True,
        return_response=True,
    )
    assert normal_response == {
        "status": "ok",
        "dry_run": False,
        "available": 0,
        "selected": 0,
        "downloaded": 0,
        "recovered": 0,
        "failed": 0,
        "quarantined": 0,
        "deleted": 0,
        "retention_missing": 0,
        "retention_failures": 0,
        "last_failure_code": "none",
    }
    journal = Path(
        "/media/xiaomi_lock_cloud_backup/.xiaomi_lock_backup_status.jsonl"
    )
    journal_text = journal.read_text(encoding="utf-8")
    report = json.loads(journal_text.splitlines()[-1])
    assert report["schema_version"] == 1
    assert report["source"] == DOMAIN
    assert report["state"] == "downloaded"
    assert report["attempts"] == 0
    assert len(report["report_key"]) == 64
    assert "fixture_entry" not in journal_text

    history_response = await hass.services.async_call(
        DOMAIN,
        "run_history_backfill",
        {"dry_run": True, "max_downloads": 10},
        blocking=True,
        return_response=True,
    )
    assert history_response == {
        "status": "dry_run_history_complete",
        "dry_run": True,
        "history_backfill": True,
        "history_complete": True,
        "pages_scanned": 1,
        "available": 0,
        "selected": 0,
    }
    migration_response = await hass.services.async_call(
        DOMAIN,
        "migrate_filenames",
        {"dry_run": True},
        blocking=True,
        return_response=True,
    )
    assert migration_response == {
        "status": "dry_run_ok",
        "dry_run": True,
        "eligible": 0,
        "unchanged": 0,
    }

    manager = hass.data[DOMAIN][entry.entry_id]
    diagnostics = manager.safe_diagnostics()
    assert diagnostics["integration_version"] == "0.0.8"
    assert diagnostics["history_complete"] is False
    assert diagnostics["history_pages_completed"] == 0
    assert diagnostics["event_trigger_count"] == 2
    assert diagnostics["event_delay_seconds"] == 120
    await manager._lock.acquire()
    try:
        assert not await manager.async_shutdown()
    finally:
        manager._lock.release()
    assert await async_unload_entry(hass, entry)
    await hass.async_stop()
    print("HA_LIFECYCLE_OK")


if __name__ == "__main__":
    asyncio.run(main())
