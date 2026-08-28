"""Read-only Xiaomi cloud access through a loaded hass-xiaomi-miot session."""

from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import urlencode

from .const import EVENT_PAGE_LIMIT, MAX_EVENT_PAGES, XIAOMI_MIOT_DOMAIN
from .models import BackupError, CloudEvent, CloudTarget


_CAMERA_HOST = "business.smartcamera.api.io.mi.com"
_EVENTLIST_API = "common/app/get/eventlist"
_M3U8_API = "common/app/m3u8"


def _supports_required_cloud_api(cloud: object) -> bool:
    return all(
        callable(getattr(cloud, name, None))
        for name in (
            "async_get_devices",
            "async_request_api",
            "get_api_by_host",
            "json_encode",
            "rc4_params",
        )
    )


def _target_did(value: object) -> str | None:
    """Normalize one in-memory device id without exposing it."""
    did = str(value or "")
    if not did:
        return None
    if len(did) > 512:
        raise BackupError("CLOUD_DEVICE_ID_INVALID")
    return did


def _select_single_target(matches: dict[str, CloudTarget]) -> CloudTarget:
    """Select one physical target after de-duplicating by in-memory id."""
    if not matches:
        raise BackupError("TARGET_MATCH_NONE")
    if len(matches) > 1:
        raise BackupError("TARGET_MATCH_MULTIPLE")
    return next(iter(matches.values()))


async def async_find_single_target(hass: Any, model: str) -> CloudTarget:
    """Find exactly one model match without reading Xiaomi auth storage."""
    xiaomi_data = getattr(hass, "data", {}).get(XIAOMI_MIOT_DOMAIN)
    sessions = xiaomi_data.get("sessions") if isinstance(xiaomi_data, dict) else None
    if not isinstance(sessions, dict) or not sessions:
        raise BackupError("XIAOMI_MIOT_SESSION_UNAVAILABLE")

    matches: dict[str, CloudTarget] = {}
    visited: set[int] = set()
    for cloud in sessions.values():
        if cloud is None or id(cloud) in visited:
            continue
        visited.add(id(cloud))
        if not _supports_required_cloud_api(cloud):
            continue
        try:
            devices = await cloud.async_get_devices(renew=False) or []
        except Exception:
            raise BackupError("CLOUD_DEVICE_QUERY_FAILED") from None
        if not isinstance(devices, list):
            raise BackupError("CLOUD_DEVICE_RESPONSE_INVALID")
        for device in devices:
            if not isinstance(device, dict) or device.get("model") != model:
                continue
            did = _target_did(device.get("did"))
            if did is None:
                raise BackupError("CLOUD_DEVICE_ID_INVALID")
            matches.setdefault(did, CloudTarget(cloud=cloud, did=did, model=model))

    if matches:
        return _select_single_target(matches)

    entities = xiaomi_data.get("entities")
    if isinstance(entities, dict):
        for entity in entities.values():
            try:
                if str(getattr(entity, "model", None) or "") != model:
                    continue
                cloud = getattr(entity, "xiaomi_cloud", None)
                did = _target_did(getattr(entity, "miot_did", None))
            except BackupError:
                raise
            except Exception:
                continue
            if (
                did is None
                or cloud is None
                or id(cloud) not in visited
                or not _supports_required_cloud_api(cloud)
            ):
                continue
            matches.setdefault(did, CloudTarget(cloud=cloud, did=did, model=model))

    return _select_single_target(matches)


def event_digest(model: str, file_id: str) -> str:
    """Create a stable identifier that cannot reveal the upstream file id."""
    if not model or not file_id:
        raise BackupError("EVENT_ID_INVALID")
    return hashlib.sha256(f"{model}\0{file_id}".encode("utf-8")).hexdigest()


async def async_get_events(
    target: CloudTarget,
    begin_time_ms: int,
    end_time_ms: int | None = None,
) -> tuple[CloudEvent, ...]:
    """Fetch newer recording events using bounded descending pagination."""
    end_time_ms = end_time_ms or int(time.time() * 1000)
    if begin_time_ms < 0 or end_time_ms <= begin_time_ms:
        raise BackupError("EVENT_WINDOW_INVALID")
    cloud = target.cloud
    try:
        api = cloud.get_api_by_host(_CAMERA_HOST, _EVENTLIST_API)
        language = str(getattr(cloud, "locale", None) or "en_US")
        region = str(getattr(cloud, "default_server", "") or "cn").upper()
    except Exception:
        raise BackupError("CLOUD_SESSION_INVALID") from None

    events_by_id: dict[str, CloudEvent] = {}
    page_end_ms = end_time_ms
    exhausted = False
    for _page_number in range(MAX_EVENT_PAGES):
        request = {
            "did": target.did,
            "model": target.model,
            "doorBell": True,
            "eventType": "Default",
            "needMerge": True,
            "sortType": "DESC",
            "region": region,
            "language": language,
            "beginTime": begin_time_ms,
            "endTime": page_end_ms,
            "limit": EVENT_PAGE_LIMIT,
        }
        try:
            response = await cloud.async_request_api(
                api,
                request,
                method="GET",
                crypt=True,
                debug=False,
                timeout=30,
                raise_timeout=True,
            ) or {}
        except Exception:
            raise BackupError("EVENTLIST_REQUEST_FAILED") from None
        if not isinstance(response, dict) or response.get("code") not in (None, 0):
            raise BackupError("EVENTLIST_REJECTED")
        data = response.get("data") or {}
        units = data.get("thirdPartPlayUnits") if isinstance(data, dict) else None
        if units is None:
            units = []
        if not isinstance(units, list):
            raise BackupError("EVENTLIST_RESPONSE_INVALID")
        if not units:
            exhausted = True
            break

        oldest_ms: int | None = None
        for unit in units:
            if not isinstance(unit, dict):
                raise BackupError("EVENTLIST_RESPONSE_INVALID")
            try:
                event_time_ms = int(unit.get("createTime"))
            except (TypeError, ValueError):
                raise BackupError("EVENT_TIME_INVALID") from None
            oldest_ms = (
                event_time_ms if oldest_ms is None else min(oldest_ms, event_time_ms)
            )
            if event_time_ms < begin_time_ms or event_time_ms > end_time_ms:
                continue
            file_id = str(unit.get("fileId") or "")
            if not file_id or len(file_id) > 1024:
                raise BackupError("EVENT_FILE_ID_INVALID")
            events_by_id.setdefault(
                file_id,
                CloudEvent(
                    event_time_ms=event_time_ms,
                    file_id=file_id,
                    is_alarm=bool(unit.get("isAlarm")),
                ),
            )

        if len(units) < EVENT_PAGE_LIMIT or oldest_ms is None or oldest_ms <= begin_time_ms:
            exhausted = True
            break
        next_page_end_ms = oldest_ms - 1
        if next_page_end_ms >= page_end_ms:
            raise BackupError("EVENTLIST_PAGINATION_STALLED")
        page_end_ms = next_page_end_ms

    if not exhausted:
        raise BackupError("EVENTLIST_PAGE_LIMIT")
    return tuple(
        sorted(events_by_id.values(), key=lambda item: (item.event_time_ms, item.file_id))
    )


def signed_playlist_url(target: CloudTarget, event: CloudEvent) -> str:
    """Build one signed URL in memory; callers must never log or persist it."""
    cloud = target.cloud
    service_token = getattr(cloud, "service_token", None)
    if not isinstance(service_token, str) or not service_token:
        raise BackupError("CLOUD_SESSION_UNAVAILABLE")
    payload = {
        "did": target.did,
        "model": target.model,
        "fileId": event.file_id,
        "isAlarm": event.is_alarm,
        "videoCodec": "H265",
    }
    try:
        api = cloud.get_api_by_host(_CAMERA_HOST, _M3U8_API)
        signed = cloud.rc4_params(
            "GET",
            api,
            {"data": cloud.json_encode(payload)},
        )
    except Exception:
        raise BackupError("PLAYLIST_SIGNING_FAILED") from None
    if not isinstance(signed, dict):
        raise BackupError("PLAYLIST_SIGNING_FAILED")
    signed["yetAnotherServiceToken"] = service_token
    return f"{api}?{urlencode(signed)}"
