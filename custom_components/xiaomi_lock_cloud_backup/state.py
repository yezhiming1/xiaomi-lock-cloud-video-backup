"""Bounded, privacy-safe state for incremental backups."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import MAX_FAILURES_PER_EVENT, MAX_MANAGED_FILES, MAX_SEEN_IDENTIFIERS
from .models import BackupError
from .paths import is_managed_filename


@dataclass(slots=True)
class BackupState:
    """Persistent state containing hashes and generated filenames only."""

    cursor_ms: int
    seen: list[str] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    managed_files: dict[str, int] = field(default_factory=dict)
    last_run_status: str = "never"
    last_error_code: str = "none"

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
        seen = value.get("seen", [])
        failures = value.get("failures", {})
        managed = value.get("managed_files", {})
        status = value.get("last_run_status", "unknown")
        error_code = value.get("last_error_code", "none")
        if not isinstance(cursor, int) or cursor < 0:
            raise BackupError("STATE_CURSOR_INVALID")
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
        if not _is_code(status):
            raise BackupError("STATE_STATUS_INVALID")
        if not _is_code(error_code):
            raise BackupError("STATE_ERROR_CODE_INVALID")
        return cls(
            cursor_ms=cursor,
            seen=list(dict.fromkeys(seen[-MAX_SEEN_IDENTIFIERS:])),
            failures=dict(list(failures.items())[-MAX_SEEN_IDENTIFIERS:]),
            managed_files=dict(managed),
            last_run_status=status,
            last_error_code=error_code,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor_ms": self.cursor_ms,
            "failures": dict(self.failures),
            "last_error_code": self.last_error_code,
            "last_run_status": self.last_run_status,
            "managed_files": dict(self.managed_files),
            "seen": list(self.seen),
        }

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
        self.cursor_ms = max(self.cursor_ms, event_time_ms)

    def require_managed_capacity(self, filename: str) -> None:
        if not is_managed_filename(filename):
            raise BackupError("STATE_MANAGED_INVALID")
        if (
            filename not in self.managed_files
            and len(self.managed_files) >= MAX_MANAGED_FILES
        ):
            raise BackupError("STATE_MANAGED_CAPACITY_REACHED")

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
        if event_digest not in self.seen:
            self.seen.append(event_digest)
        self.seen = self.seen[-MAX_SEEN_IDENTIFIERS:]
        self.cursor_ms = max(self.cursor_ms, event_time_ms)
        return True

    def advance_cursor(self, timestamp_ms: int) -> None:
        if timestamp_ms < self.cursor_ms:
            return
        self.cursor_ms = timestamp_ms


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_code(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and all(
        character.isascii() and (character.isalnum() or character == "_")
        for character in value
    )


def _require_digest(value: str) -> None:
    if not _is_digest(value):
        raise BackupError("EVENT_DIGEST_INVALID")
