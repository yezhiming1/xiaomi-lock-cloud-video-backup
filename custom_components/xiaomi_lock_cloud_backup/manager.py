"""Scheduled incremental backup orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import logging
from pathlib import Path
import shutil
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store

from .cloud import (
    async_find_single_target,
    async_get_events,
    async_get_history_page,
    event_digest,
    signed_playlist_url,
)
from .const import (
    INTEGRATION_VERSION,
    MAX_EVENT_PAGES,
    MAX_HISTORY_DOWNLOADS_PER_RUN,
    MEDIA_ROOT,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .hls import download_hls_once, inspect_local_output
from .models import BackupError, CloudEvent, CloudTarget
from .paths import ensure_output_directory, safe_managed_path, unlink_managed_file
from .settings import BackupOptions, options_from_mappings
from .state import BackupState


_LOGGER = logging.getLogger(__name__)
_DAY_MS = 86_400_000


@dataclass(slots=True)
class _RunCounts:
    downloaded: int = 0
    recovered: int = 0
    failed: int = 0
    quarantined: int = 0
    deleted: int = 0
    retention_missing: int = 0
    retention_failures: int = 0
    last_failure_code: str = "none"


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    output_directory: Path
    ffmpeg_binary: str
    ffprobe_binary: str


class BackupManager:
    """One config entry's bounded state, timer, and serialized runs."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        media_root: Path = MEDIA_ROOT,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.media_root = media_root
        self.options: BackupOptions = options_from_mappings(entry.data, entry.options)
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{entry.entry_id}",
        )
        self._state: BackupState | None = None
        self._lock = asyncio.Lock()
        self._remove_schedule: Any = None
        self._accept_runs = True

    async def async_initialize(self) -> None:
        """Load bounded state and install the daily local-time schedule."""
        loaded = await self._store.async_load()
        if loaded is None:
            self._state = BackupState.initial(int(time.time() * 1000))
            await self._save_state()
        else:
            self._state = BackupState.from_dict(loaded)
        self._install_schedule()

    async def async_shutdown(self) -> bool:
        """Stop new work only when no media operation is still running."""
        self._accept_runs = False
        if self._lock.locked():
            self._accept_runs = True
            return False
        if self._remove_schedule is not None:
            self._remove_schedule()
            self._remove_schedule = None
        return True

    def safe_diagnostics(self) -> dict[str, object]:
        state = self._require_state()
        return {
            "integration_version": INTEGRATION_VERSION,
            "last_error_code": state.last_error_code,
            "last_run_status": state.last_run_status,
            "seen_count": len(state.seen),
            "pending_failure_count": len(state.failures),
            "managed_file_count": len(state.managed_files),
            "history_complete": state.history_complete,
            "history_pages_completed": state.history_pages_completed,
            "keep_audio": self.options.keep_audio,
            "retention_days": self.options.retention_days,
            "max_downloads_per_run": self.options.max_downloads_per_run,
            "schedule_configured": True,
        }

    def _install_schedule(self) -> None:
        if self._remove_schedule is not None:
            self._remove_schedule()
        schedule = self.options.schedule_time

        @callback
        def scheduled_run(_now: datetime) -> None:
            self.hass.async_create_task(
                self._async_scheduled_run(),
                f"xiaomi_lock_cloud_backup_{self.entry.entry_id}",
            )

        self._remove_schedule = async_track_time_change(
            self.hass,
            scheduled_run,
            hour=schedule.hour,
            minute=schedule.minute,
            second=schedule.second,
        )

    async def _async_scheduled_run(self) -> None:
        try:
            await self.async_run(dry_run=False)
        except BackupError as exc:
            _LOGGER.error("Scheduled backup failed with code=%s", exc.code)
        except Exception:
            _LOGGER.error("Scheduled backup failed with code=BACKUP_UNEXPECTED")

    async def async_run(self, *, dry_run: bool) -> dict[str, object]:
        """Run one serialized backup and return counts plus fixed status codes."""
        if not self._accept_runs:
            raise BackupError("BACKUP_MANAGER_STOPPED")
        async with self._lock:
            if not self._accept_runs:
                raise BackupError("BACKUP_MANAGER_STOPPED")
            state = self._require_state()
            run_now_ms = int(time.time() * 1000)
            try:
                target = await async_find_single_target(
                    self.hass, self.options.target_model
                )
                events = await async_get_events(
                    target,
                    state.cursor_ms,
                    max(run_now_ms, state.cursor_ms + 1),
                )
            except BackupError as exc:
                if not dry_run:
                    state.last_run_status = exc.code
                    state.last_error_code = exc.code
                    await self._save_state()
                raise
            except Exception:
                if not dry_run:
                    state.last_run_status = "BACKUP_UNEXPECTED"
                    state.last_error_code = "BACKUP_UNEXPECTED"
                    await self._save_state()
                raise BackupError("BACKUP_UNEXPECTED") from None

            unseen_events = [
                event
                for event in events
                if not state.has_seen(event_digest(target.model, event.file_id))
            ]
            selected_events = unseen_events[: self.options.max_downloads_per_run]
            if dry_run:
                return {
                    "status": "dry_run_ok",
                    "dry_run": True,
                    "available": len(unseen_events),
                    "selected": len(selected_events),
                }

            context, counts = await self._async_prepare_execution(run_now_ms)
            await self._async_download_events(
                target,
                selected_events,
                context,
                counts,
            )

            if counts.failed or counts.retention_missing or counts.retention_failures:
                status = "partial"
            elif len(unseen_events) > len(selected_events):
                status = "limit_reached"
            else:
                status = "ok"
            self._apply_final_error(counts)
            state.last_run_status = status
            await self._save_state()
            result = {
                "status": status,
                "dry_run": False,
                "available": len(unseen_events),
                "selected": len(selected_events),
                "downloaded": counts.downloaded,
                "recovered": counts.recovered,
                "failed": counts.failed,
                "quarantined": counts.quarantined,
                "deleted": counts.deleted,
                "retention_missing": counts.retention_missing,
                "retention_failures": counts.retention_failures,
                "last_failure_code": counts.last_failure_code,
            }
            _LOGGER.info(
                "Backup completed status=%s downloaded=%d recovered=%d failed=%d deleted=%d",
                status,
                counts.downloaded,
                counts.recovered,
                counts.failed,
                counts.deleted,
            )
            return result

    async def async_run_history_backfill(
        self,
        *,
        dry_run: bool,
        max_downloads: int,
    ) -> dict[str, object]:
        """Resume a serialized oldest-page search without rewinding incrementals."""
        if (
            not isinstance(max_downloads, int)
            or isinstance(max_downloads, bool)
            or not 1 <= max_downloads <= MAX_HISTORY_DOWNLOADS_PER_RUN
        ):
            raise BackupError("HISTORY_DOWNLOAD_LIMIT_INVALID")
        if not self._accept_runs:
            raise BackupError("BACKUP_MANAGER_STOPPED")
        async with self._lock:
            if not self._accept_runs:
                raise BackupError("BACKUP_MANAGER_STOPPED")
            state = self._require_state()
            if state.history_complete:
                return self._completed_history_response(dry_run=dry_run)

            run_now_ms = int(time.time() * 1000)
            try:
                target = await async_find_single_target(
                    self.hass,
                    self.options.target_model,
                )
                if dry_run:
                    return await self._async_history_dry_run(
                        target,
                        run_now_ms,
                        max_downloads,
                    )

                state.begin_history(run_now_ms)
                await self._save_state()
                context, counts = await self._async_prepare_execution(
                    run_now_ms,
                    apply_retention=False,
                )
                result = await self._async_execute_history_pages(
                    target,
                    context,
                    counts,
                    max_downloads,
                )
            except BackupError as exc:
                if not dry_run:
                    state.last_run_status = exc.code
                    state.last_error_code = exc.code
                    await self._save_state()
                raise
            except Exception:
                if not dry_run:
                    state.last_run_status = "BACKUP_UNEXPECTED"
                    state.last_error_code = "BACKUP_UNEXPECTED"
                    await self._save_state()
                raise BackupError("BACKUP_UNEXPECTED") from None

            _LOGGER.info(
                "History backfill completed status=%s downloaded=%d "
                "recovered=%d failed=%d pages=%d",
                result["status"],
                result["downloaded"],
                result["recovered"],
                result["failed"],
                result["pages_scanned"],
            )
            return result

    async def _async_history_dry_run(
        self,
        target: CloudTarget,
        run_now_ms: int,
        max_downloads: int,
    ) -> dict[str, object]:
        state = self._require_state()
        scan_end_ms = state.history_end_ms or run_now_ms
        discovered: set[str] = set()
        pages_scanned = 0
        scan_complete = False
        for _page_number in range(MAX_EVENT_PAGES):
            page = await async_get_history_page(target, scan_end_ms)
            pages_scanned += 1
            for event in page.events:
                digest = event_digest(target.model, event.file_id)
                if not state.has_seen(digest):
                    discovered.add(digest)
            if len(discovered) >= max_downloads:
                break
            if page.complete:
                scan_complete = True
                break
            if page.next_end_ms is None:
                raise BackupError("EVENTLIST_CONTINUATION_INVALID")
            scan_end_ms = page.next_end_ms

        available = len(discovered)
        if scan_complete and available == 0:
            status = "dry_run_history_complete"
        elif available >= max_downloads:
            status = "dry_run_history_limit_reached"
        elif scan_complete:
            status = "dry_run_history_pending"
        else:
            status = "dry_run_history_scan_limit_reached"
        return {
            "status": status,
            "dry_run": True,
            "history_backfill": True,
            "history_complete": scan_complete and available == 0,
            "pages_scanned": pages_scanned,
            "available": available,
            "selected": min(available, max_downloads),
        }

    async def _async_execute_history_pages(
        self,
        target: CloudTarget,
        context: _ExecutionContext,
        counts: _RunCounts,
        max_downloads: int,
    ) -> dict[str, object]:
        state = self._require_state()
        discovered: set[str] = set()
        pages_scanned = 0
        available = 0
        selected_count = 0
        page_has_remaining = False

        for _page_number in range(MAX_EVENT_PAGES):
            if state.history_complete:
                break
            if state.history_end_ms is None:
                raise BackupError("STATE_HISTORY_INVALID")
            page = await async_get_history_page(target, state.history_end_ms)
            pages_scanned += 1

            unseen_events: list[CloudEvent] = []
            for event in page.events:
                digest = event_digest(target.model, event.file_id)
                if state.has_seen(digest) or digest in discovered:
                    continue
                discovered.add(digest)
                unseen_events.append(event)
            available += len(unseen_events)

            remaining_capacity = max_downloads - selected_count
            selected_events = unseen_events[:remaining_capacity]
            selected_count += len(selected_events)
            if selected_events:
                await self._async_download_events(
                    target,
                    selected_events,
                    context,
                    counts,
                )

            page_has_remaining = any(
                not state.has_seen(event_digest(target.model, event.file_id))
                for event in page.events
            )
            if page_has_remaining:
                break

            if page.complete:
                state.complete_history()
            else:
                if page.next_end_ms is None:
                    raise BackupError("EVENTLIST_CONTINUATION_INVALID")
                state.advance_history(page.next_end_ms)
            await self._save_state()

            if counts.failed or selected_count >= max_downloads:
                break

        if counts.failed or counts.retention_missing or counts.retention_failures:
            status = "partial"
        elif state.history_complete:
            status = "history_complete"
        elif page_has_remaining or selected_count >= max_downloads:
            status = "history_limit_reached"
        elif pages_scanned >= MAX_EVENT_PAGES:
            status = "history_scan_limit_reached"
        else:
            status = "history_pending"

        self._apply_final_error(counts)
        state.last_run_status = status
        await self._save_state()
        return {
            "status": status,
            "dry_run": False,
            "history_backfill": True,
            "history_complete": state.history_complete,
            "pages_scanned": pages_scanned,
            "available": available,
            "selected": selected_count,
            "downloaded": counts.downloaded,
            "recovered": counts.recovered,
            "failed": counts.failed,
            "quarantined": counts.quarantined,
            "deleted": counts.deleted,
            "retention_missing": counts.retention_missing,
            "retention_failures": counts.retention_failures,
            "last_failure_code": counts.last_failure_code,
        }

    def _completed_history_response(self, *, dry_run: bool) -> dict[str, object]:
        status = "dry_run_history_complete" if dry_run else "history_complete"
        result: dict[str, object] = {
            "status": status,
            "dry_run": dry_run,
            "history_backfill": True,
            "history_complete": True,
            "pages_scanned": 0,
            "available": 0,
            "selected": 0,
        }
        if not dry_run:
            result.update(
                {
                    "downloaded": 0,
                    "recovered": 0,
                    "failed": 0,
                    "quarantined": 0,
                    "deleted": 0,
                    "retention_missing": 0,
                    "retention_failures": 0,
                    "last_failure_code": "none",
                }
            )
        return result

    async def _async_prepare_execution(
        self,
        run_now_ms: int,
        *,
        apply_retention: bool = True,
    ) -> tuple[_ExecutionContext, _RunCounts]:
        state = self._require_state()
        ffmpeg_binary, ffprobe_binary = await self.hass.async_add_executor_job(
            _find_media_toolchain
        )
        if not ffmpeg_binary or not ffprobe_binary:
            state.last_run_status = "MEDIA_TOOLCHAIN_UNAVAILABLE"
            state.last_error_code = "MEDIA_TOOLCHAIN_UNAVAILABLE"
            await self._save_state()
            raise BackupError("MEDIA_TOOLCHAIN_UNAVAILABLE")
        try:
            output_directory = await self.hass.async_add_executor_job(
                ensure_output_directory,
                self.media_root,
                self.options.output_subdirectory,
            )
        except BackupError as exc:
            state.last_run_status = exc.code
            state.last_error_code = exc.code
            await self._save_state()
            raise
        except Exception:
            state.last_run_status = "OUTPUT_PREPARATION_FAILED"
            state.last_error_code = "OUTPUT_PREPARATION_FAILED"
            await self._save_state()
            raise BackupError("OUTPUT_PREPARATION_FAILED") from None

        counts = _RunCounts()
        if apply_retention:
            (
                counts.deleted,
                counts.retention_missing,
                counts.retention_failures,
            ) = await self._async_apply_retention(output_directory, run_now_ms)
            await self._save_state()
        return (
            _ExecutionContext(
                output_directory=output_directory,
                ffmpeg_binary=ffmpeg_binary,
                ffprobe_binary=ffprobe_binary,
            ),
            counts,
        )

    async def _async_download_events(
        self,
        target: CloudTarget,
        selected_events: list[CloudEvent],
        context: _ExecutionContext,
        counts: _RunCounts,
    ) -> None:
        state = self._require_state()
        for event in selected_events:
            digest = event_digest(target.model, event.file_id)
            filename = _output_filename(event.event_time_ms, digest)
            try:
                state.require_managed_capacity(filename)
            except BackupError as exc:
                state.last_run_status = exc.code
                state.last_error_code = exc.code
                await self._save_state()
                raise
            output_path = await self.hass.async_add_executor_job(
                safe_managed_path,
                self.media_root,
                context.output_directory,
                filename,
            )
            completed_ms = int(time.time() * 1000)
            playlist_url = ""
            try:
                output_exists = await self.hass.async_add_executor_job(
                    output_path.exists
                )
                if output_exists:
                    await self.hass.async_add_executor_job(
                        inspect_local_output,
                        output_path,
                        context.ffprobe_binary,
                    )
                    counts.recovered += 1
                else:
                    playlist_url = signed_playlist_url(target, event)
                    await self.hass.async_add_executor_job(
                        partial(
                            download_hls_once,
                            playlist_url,
                            output_path,
                            context.ffmpeg_binary,
                            context.ffprobe_binary,
                            keep_audio=self.options.keep_audio,
                        )
                    )
                    counts.downloaded += 1
                state.record_success(
                    digest,
                    filename,
                    event.event_time_ms,
                    completed_ms,
                )
                state.last_run_status = "running"
                await self._save_state()
            except BackupError as exc:
                counts.failed += 1
                counts.last_failure_code = exc.code
                if state.record_failure(digest, event.event_time_ms):
                    counts.quarantined += 1
                state.last_run_status = "event_failed"
                state.last_error_code = exc.code
                await self._save_state()
                if not state.has_seen(digest):
                    break
            except Exception:
                counts.failed += 1
                counts.last_failure_code = "EVENT_UNEXPECTED"
                if state.record_failure(digest, event.event_time_ms):
                    counts.quarantined += 1
                state.last_run_status = "event_failed"
                state.last_error_code = "EVENT_UNEXPECTED"
                await self._save_state()
                if not state.has_seen(digest):
                    break
            finally:
                playlist_url = ""

    def _apply_final_error(self, counts: _RunCounts) -> None:
        state = self._require_state()
        if not counts.failed and counts.retention_failures:
            counts.last_failure_code = "RETENTION_FAILED"
            state.last_error_code = counts.last_failure_code
        elif not counts.failed and counts.retention_missing:
            counts.last_failure_code = "RETENTION_FILE_MISSING"
            state.last_error_code = counts.last_failure_code
        elif not counts.failed and not counts.retention_failures:
            state.last_error_code = "none"

    async def _async_apply_retention(
        self,
        output_directory: Path,
        run_now_ms: int,
    ) -> tuple[int, int, int]:
        state = self._require_state()
        cutoff_ms = run_now_ms - self.options.retention_days * _DAY_MS
        deleted = 0
        missing = 0
        failures = 0
        for filename, completed_ms in list(state.managed_files.items()):
            if completed_ms > cutoff_ms:
                continue
            try:
                was_deleted = await self.hass.async_add_executor_job(
                    unlink_managed_file,
                    self.media_root,
                    output_directory,
                    filename,
                )
                state.managed_files.pop(filename, None)
                if was_deleted:
                    deleted += 1
                else:
                    missing += 1
            except BackupError:
                failures += 1
            except Exception:
                failures += 1
        return deleted, missing, failures

    async def _save_state(self) -> None:
        await self._store.async_save(self._require_state().to_dict())

    def _require_state(self) -> BackupState:
        if self._state is None:
            raise BackupError("STATE_NOT_INITIALIZED")
        return self._state


def _output_filename(event_time_ms: int, digest: str) -> str:
    timestamp = datetime.fromtimestamp(
        event_time_ms / 1000,
        tz=timezone.utc,
    ).strftime("%Y%m%dT%H%M%S")
    milliseconds = event_time_ms % 1000
    return f"xiaomi_lock_{timestamp}{milliseconds:03d}Z_{digest[:12]}.mp4"


def _find_media_toolchain() -> tuple[str | None, str | None]:
    return shutil.which("ffmpeg"), shutil.which("ffprobe")
