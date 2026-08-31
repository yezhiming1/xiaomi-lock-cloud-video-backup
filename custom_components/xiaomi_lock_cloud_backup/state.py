"""Bounded, privacy-safe state for incremental backups."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import MAX_FAILURES_PER_EVENT, MAX_MANAGED_FILES, MAX_SEEN_IDENTIFIERS
from .models import BackupError
from .paths import is_managed_filename, legacy_filename_details


@dataclass(slots=True)
class BackupState:
    """Persistent state containing hashes and generated filenames only."""

    cursor_ms: int
    history_end_ms: int | None = None
    history_complete: bool = False
    history_pages_completed: int = 0
    seen: list[str] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    managed_files: dict[str, int] = field(default_factory=dict)
    event_files: dict[str, str] = field(default_factory=dict)
    pending_files: dict[str, str] = field(default_factory=dict)
    last_run_status: str = "never"
    last_error_code: str = "none"
    consecutive_run_failures: int = 0
    status_report_sequence: int = 0

    @classmethod
    def initial(cls, cursor_ms: int) -> "BackupState":
        if cursor_ms < 0:
            raise BackupError("STATE_CURSOR_INVALID")
        return cls(cursor_ms=cursor_ms)

    @classmethod
    def from_dict(cls, value: object) -> "BackupState":
        if not isinstance(value, dict):
            raise BackupError("STATE_INVALID")
        cursor = value.get("cursor_ms")
        history_end = value.get("history_end_ms")
        history_complete = value.get("history_complete", False)
        history_pages = value.get("history_pages_completed", 0)
        seen = value.get("seen", [])
        failures = value.get("failures", {})
        managed = value.get("managed_files", {})
        event_files = value.get("event_files", {})
        pending_files = value.get("pending_files", {})
        status = value.get("last_run_status", "unknown")
        error_code = value.get("last_error_code", "none")
        run_failures = value.get("consecutive_run_failures", 0)
        report_sequence = value.get("status_report_sequence", 0)
        if not isinstance(cursor, int) or cursor < 0:
            raise BackupError("STATE_CURSOR_INVALID")
        if history_end is not None and (
            not isinstance(history_end, int)
            or isinstance(history_end, bool)
            or history_end <= 0
        ):
            raise BackupError("STATE_HISTORY_CURSOR_INVALID")
        if not isinstance(history_complete, bool):
            raise BackupError("STATE_HISTORY_COMPLETE_INVALID")
        if (
            not isinstance(history_pages, int)
            or isinstance(history_pages, bool)
            or history_pages < 0
        ):
            raise BackupError("STATE_HISTORY_PAGES_INVALID")
        if history_complete and history_end is not None:
            raise BackupError("STATE_HISTORY_INVALID")
        if not isinstance(seen, list) or any(not _is_digest(item) for item in seen):
            raise BackupError("STATE_SEEN_INVALID")
        if not isinstance(failures, dict) or any(
            not _is_digest(key) or not isinstance(count, int) or count < 1
            for key, count in failures.items()
        ):
            raise BackupError("STATE_FAILURES_INVALID")
        if not isinstance(managed, dict) or any(
            not is_managed_filename(name)
            or not isinstance(created_ms, int)
            or created_ms < 0
            for name, created_ms in managed.items()
        ):
            raise BackupError("STATE_MANAGED_INVALID")
        if len(managed) > MAX_MANAGED_FILES:
            raise BackupError("STATE_MANAGED_INVALID")
        if not _is_file_mapping(event_files) or any(
            filename not in managed for filename in event_files.values()
        ):
            raise BackupError("STATE_EVENT_FILES_INVALID")
        if not _is_file_mapping(pending_files):
            raise BackupError("STATE_PENDING_FILES_INVALID")
        if not _is_code(status):
            raise BackupError("STATE_STATUS_INVALID")
        if not _is_code(error_code):
            raise BackupError("STATE_ERROR_CODE_INVALID")
        if (
            not isinstance(run_failures, int)
            or isinstance(run_failures, bool)
            or not 0 <= run_failures <= 100
        ):
            raise BackupError("STATE_RUN_FAILURES_INVALID")
        if (
            not isinstance(report_sequence, int)
            or isinstance(report_sequence, bool)
            or report_sequence < 0
        ):
            raise BackupError("STATE_REPORT_SEQUENCE_INVALID")
        return cls(
            cursor_ms=cursor,
            history_end_ms=history_end,
            history_complete=history_complete,
            history_pages_completed=history_pages,
            seen=list(dict.fromkeys(seen[-MAX_SEEN_IDENTIFIERS:])),
            failures=dict(list(failures.items())[-MAX_SEEN_IDENTIFIERS:]),
            managed_files=dict(managed),
            event_files=dict(list(event_files.items())[-MAX_MANAGED_FILES:]),
            pending_files=dict(list(pending_files.items())[-MAX_MANAGED_FILES:]),
            last_run_status=status,
            last_error_code=error_code,
            consecutive_run_failures=run_failures,
            status_report_sequence=report_sequence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor_ms": self.cursor_ms,
            "failures": dict(self.failures),
            "history_complete": self.history_complete,
            "history_end_ms": self.history_end_ms,
            "history_pages_completed": self.history_pages_completed,
            "last_error_code": self.last_error_code,
            "last_run_status": self.last_run_status,
            "managed_files": dict(self.managed_files),
            "event_files": dict(self.event_files),
            "pending_files": dict(self.pending_files),
            "seen": list(self.seen),
            "consecutive_run_failures": self.consecutive_run_failures,
            "status_report_sequence": self.status_report_sequence,
        }

    def begin_status_report(self) -> int:
        self.status_report_sequence += 1
        return self.status_report_sequence

    def record_run_failure(self) -> int:
        self.consecutive_run_failures = min(self.consecutive_run_failures + 1, 100)
        return self.consecutive_run_failures

    def record_run_success(self) -> None:
        self.consecutive_run_failures = 0

    def has_seen(self, event_digest: str) -> bool:
        _require_digest(event_digest)
        return event_digest in self.seen

    def record_success(
        self,
        event_digest: str,
        filename: str,
        event_time_ms: int,
        completed_ms: int,
    ) -> None:
        _require_digest(event_digest)
        if event_time_ms < 0 or completed_ms < 0:
            raise BackupError("STATE_TIME_INVALID")
        if not is_managed_filename(filename):
            raise BackupError("STATE_MANAGED_INVALID")
        self.require_managed_capacity(filename)
        if event_digest not in self.seen:
            self.seen.append(event_digest)
        self.seen = self.seen[-MAX_SEEN_IDENTIFIERS:]
        self.failures.pop(event_digest, None)
        self.managed_files[filename] = completed_ms
        self.event_files[event_digest] = filename
        self.pending_files.pop(event_digest, None)
        self.cursor_ms = max(self.cursor_ms, event_time_ms)

    def require_managed_capacity(self, filename: str) -> None:
        if not is_managed_filename(filename):
            raise BackupError("STATE_MANAGED_INVALID")
        if (
            filename not in self.managed_files
            and len(self.managed_files) >= MAX_MANAGED_FILES
        ):
            raise BackupError("STATE_MANAGED_CAPACITY_REACHED")

    def filename_for_event(self, event_digest: str) -> str | None:
        _require_digest(event_digest)
        return self.pending_files.get(event_digest) or self.event_files.get(event_digest)

    def reserve_filename(self, event_digest: str, filename: str) -> None:
        _require_digest(event_digest)
        if not is_managed_filename(filename):
            raise BackupError("STATE_MANAGED_INVALID")
        existing = self.filename_for_event(event_digest)
        if existing is not None and existing != filename:
            raise BackupError("STATE_FILENAME_RESERVATION_CONFLICT")
        if filename in self.managed_files and self.event_files.get(event_digest) != filename:
            raise BackupError("STATE_FILENAME_RESERVATION_CONFLICT")
        if any(
            other_digest != event_digest and other_filename == filename
            for other_digest, other_filename in self.pending_files.items()
        ):
            raise BackupError("STATE_FILENAME_RESERVATION_CONFLICT")
        if len(self.pending_files) >= MAX_MANAGED_FILES and event_digest not in self.pending_files:
            raise BackupError("STATE_PENDING_CAPACITY_REACHED")
        self.pending_files[event_digest] = filename

    def remove_managed_file(self, filename: str) -> None:
        self.managed_files.pop(filename, None)
        self.event_files = {
            digest: bound
            for digest, bound in self.event_files.items()
            if bound != filename
        }

    def migrate_filenames(self, mapping: dict[str, str]) -> None:
        if len(set(mapping.values())) != len(mapping):
            raise BackupError("STATE_FILENAME_MIGRATION_INVALID")
        updated_managed = dict(self.managed_files)
        for old_name, new_name in mapping.items():
            if old_name not in updated_managed or new_name in updated_managed:
                raise BackupError("STATE_FILENAME_MIGRATION_INVALID")
            updated_managed[new_name] = updated_managed.pop(old_name)
        self.managed_files = updated_managed
        self.event_files = {
            digest: mapping.get(filename, filename)
            for digest, filename in self.event_files.items()
        }
        self.pending_files = {
            digest: mapping.get(filename, filename)
            for digest, filename in self.pending_files.items()
        }
        for old_name, new_name in mapping.items():
            details = legacy_filename_details(old_name)
            if details is None:
                raise BackupError("STATE_FILENAME_MIGRATION_INVALID")
            prefix = details[1]
            matches = [digest for digest in self.seen if digest.startswith(prefix)]
            if len(matches) > 1:
                raise BackupError("STATE_FILENAME_MIGRATION_AMBIGUOUS")
            if matches:
                self.event_files[matches[0]] = new_name

    def record_failure(self, event_digest: str, event_time_ms: int) -> bool:
        """Record a failure; return true when the event is quarantined."""
        _require_digest(event_digest)
        if event_time_ms < 0:
            raise BackupError("STATE_TIME_INVALID")
        count = self.failures.get(event_digest, 0) + 1
        if count < MAX_FAILURES_PER_EVENT:
            self.failures[event_digest] = count
            self.failures = dict(
                list(self.failures.items())[-MAX_SEEN_IDENTIFIERS:]
            )
            return False
        self.failures.pop(event_digest, None)
        self.pending_files.pop(event_digest, None)
        if event_digest not in self.seen:
            self.seen.append(event_digest)
        self.seen = self.seen[-MAX_SEEN_IDENTIFIERS:]
        self.cursor_ms = max(self.cursor_ms, event_time_ms)
        return True

    def advance_cursor(self, timestamp_ms: int) -> None:
        if timestamp_ms < self.cursor_ms:
            return
        self.cursor_ms = timestamp_ms

    def begin_history(self, end_time_ms: int) -> None:
        """Freeze the upper boundary for a resumable one-time backfill."""
        if self.history_complete:
            raise BackupError("HISTORY_ALREADY_COMPLETE")
        if (
            not isinstance(end_time_ms, int)
            or isinstance(end_time_ms, bool)
            or end_time_ms <= 0
        ):
            raise BackupError("STATE_HISTORY_CURSOR_INVALID")
        if self.history_end_ms is None:
            self.history_end_ms = end_time_ms

    def advance_history(self, next_end_ms: int) -> None:
        """Commit one fully handled cloud page and move strictly backward."""
        current = self.history_end_ms
        if self.history_complete or current is None:
            raise BackupError("STATE_HISTORY_INVALID")
        if (
            not isinstance(next_end_ms, int)
            or isinstance(next_end_ms, bool)
            or next_end_ms <= 0
            or next_end_ms >= current
        ):
            raise BackupError("STATE_HISTORY_CURSOR_INVALID")
        self.history_end_ms = next_end_ms
        self.history_pages_completed += 1

    def complete_history(self) -> None:
        """Record the API's authoritative end-of-history result."""
        if not self.history_complete:
            self.history_pages_completed += 1
        self.history_complete = True
        self.history_end_ms = None


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_file_mapping(value: object) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_MANAGED_FILES
        and all(_is_digest(key) and is_managed_filename(filename) for key, filename in value.items())
    )


def _is_code(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and all(
        character.isascii() and (character.isalnum() or character == "_")
        for character in value
    )


def _require_digest(value: str) -> None:
    if not _is_digest(value):
        raise BackupError("EVENT_DIGEST_INVALID")
