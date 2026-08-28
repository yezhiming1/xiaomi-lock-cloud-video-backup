$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$image = "ghcr.io/home-assistant/home-assistant@sha256:6e8225ea9de2cfe9292b634e554ebbf439118ca0c823221d794298e7a74404bb"
$lifecycleCheck = @"
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
from custom_components.xiaomi_lock_cloud_backup.const import (
    DOMAIN,
    INTEGRATION_VERSION,
    default_options,
)

manifest = json.loads(
    Path('/work/custom_components/xiaomi_lock_cloud_backup/manifest.json').read_text()
)
assert manifest['domain'] == 'xiaomi_lock_cloud_backup'
assert manifest['version'] == INTEGRATION_VERSION == '0.0.3'

class FixtureCloud:
    default_server = 'cn'
    locale = 'en_US'

    async def async_get_devices(self, renew=False):
        return []

    async def async_request_api(self, *_args, **_kwargs):
        return {
            'code': 0,
            'data': {
                'thirdPartPlayUnits': [],
                'isContinue': False,
                'nextTime': 0,
            },
        }

    @staticmethod
    def get_api_by_host(host, api):
        return f'https://{host}/{api}'

    @staticmethod
    def json_encode(value):
        return json.dumps(value)

    @staticmethod
    def rc4_params(_method, _url, params):
        return params

class FixtureEntity:
    model = 'xiaomi.lock.s1'
    miot_did = 'fixture-device'

    def __init__(self, cloud):
        self.xiaomi_cloud = cloud

async def main():
    config_directory = Path('/tmp/ha-smoke')
    component_link = config_directory / 'custom_components' / DOMAIN
    component_link.parent.mkdir(parents=True)
    os.symlink(
        '/work/custom_components/xiaomi_lock_cloud_backup',
        component_link,
        target_is_directory=True,
    )
    hass = HomeAssistant(str(config_directory))
    loader.async_setup(hass)
    await hass.async_start()
    integration = await loader.async_get_integration(hass, DOMAIN)
    assert integration.name == 'Xiaomi Lock Cloud Video Backup'
    assert integration.version == '0.0.3'
    cloud = FixtureCloud()
    hass.data['xiaomi_miot'] = {
        'sessions': {'fixture': cloud},
        'entities': {'lock.fixture': FixtureEntity(cloud)},
    }
    assert await async_setup(hass, {})
    entry = ConfigEntry(
        data=default_options(),
        disabled_by=None,
        discovery_keys={},
        domain=DOMAIN,
        entry_id='fixture_entry',
        minor_version=1,
        options={},
        pref_disable_new_entities=False,
        pref_disable_polling=False,
        source='user',
        subentries_data=(),
        title='Fixture',
        unique_id=DOMAIN,
        version=1,
    )
    assert await async_setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, 'run_backup')
    assert hass.services.has_service(DOMAIN, 'run_history_backfill')
    response = await hass.services.async_call(
        DOMAIN,
        'run_backup',
        {'dry_run': True},
        blocking=True,
        return_response=True,
    )
    assert response == {
        'status': 'dry_run_ok',
        'dry_run': True,
        'available': 0,
        'selected': 0,
    }
    history_response = await hass.services.async_call(
        DOMAIN,
        'run_history_backfill',
        {'dry_run': True, 'max_downloads': 10},
        blocking=True,
        return_response=True,
    )
    assert history_response == {
        'status': 'dry_run_history_complete',
        'dry_run': True,
        'history_backfill': True,
        'history_complete': True,
        'pages_scanned': 1,
        'available': 0,
        'selected': 0,
    }
    manager = hass.data[DOMAIN][entry.entry_id]
    diagnostics = manager.safe_diagnostics()
    assert diagnostics['integration_version'] == '0.0.3'
    assert diagnostics['history_complete'] is False
    assert diagnostics['history_pages_completed'] == 0
    await manager._lock.acquire()
    try:
        assert not await manager.async_shutdown()
    finally:
        manager._lock.release()
    assert await async_unload_entry(hass, entry)
    await hass.async_stop()
    print('HA_LIFECYCLE_OK')

asyncio.run(main())
"@

$dockerArguments = @(
    "run",
    "--rm",
    "--network", "none",
    "--env", "PYTHONDONTWRITEBYTECODE=1",
    "--env", "PYTHONPATH=/work",
    "--volume", "${projectRoot}:/work:ro",
    "--entrypoint", "python",
    $image,
    "-c", $lifecycleCheck
)

& docker @dockerArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$unitArguments = @(
    "run",
    "--rm",
    "--network", "none",
    "--env", "PYTHONDONTWRITEBYTECODE=1",
    "--volume", "${projectRoot}:/work:ro",
    "--workdir", "/work",
    "--entrypoint", "python",
    $image,
    "-m", "unittest", "discover",
    "-s", "tests",
    "-p", "test_*.py",
    "-v"
)

& docker @unitArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
