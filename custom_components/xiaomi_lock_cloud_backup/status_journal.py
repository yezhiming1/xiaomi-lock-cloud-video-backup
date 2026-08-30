"""Append-only, de-identified status handoff for local consumers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

from .models import BackupError


STATUS_JOURNAL_NAME = ".xiaomi_lock_backup_status.jsonl"
MAX_STATUS_JOURNAL_BYTES = 16 * 1024 * 1024
_STATES = {"retrying", "downloaded", "failed"}


def status_report_key(entry_key: str, sequence: int, operation: str) -> str:
    """Return an opaque stable key without exposing the Home Assistant entry id."""
    if not entry_key or sequence < 1 or operation not in {"incremental", "history"}:
        raise BackupError("STATUS_REPORT_INVALID")
    material = f"{entry_key}\0{sequence}\0{operation}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def append_status_report(
    output_directory: Path,
    *,
    report_key: str,
    state: str,
    attempts: int,
    error_code: str,
) -> None:
    """Append one bounded JSON line and durably flush it without following links."""
    if (
        len(report_key) != 64
        or any(character not in "0123456789abcdef" for character in report_key)
        or state not in _STATES
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or not 0 <= attempts <= 100
        or not _is_code(error_code)
    ):
        raise BackupError("STATUS_REPORT_INVALID")
    if not output_directory.is_dir() or output_directory.is_symlink():
        raise BackupError("STATUS_DIRECTORY_UNSAFE")

    payload = {
        "attempts": attempts,
        "error_code": error_code,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "report_key": report_key,
        "schema_version": 1,
        "source": "xiaomi_lock_cloud_backup",
        "state": state,
    }
    encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )
    journal = output_directory / STATUS_JOURNAL_NAME
    if journal.is_symlink():
        raise BackupError("STATUS_JOURNAL_UNSAFE")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(journal, flags, 0o640)
    except OSError:
        raise BackupError("STATUS_JOURNAL_OPEN_FAILED") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupError("STATUS_JOURNAL_UNSAFE")
        if metadata.st_size + len(encoded) > MAX_STATUS_JOURNAL_BYTES:
            raise BackupError("STATUS_JOURNAL_CAPACITY_REACHED")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise BackupError("STATUS_JOURNAL_WRITE_FAILED")
            view = view[written:]
        os.fsync(descriptor)
    except BackupError:
        raise
    except OSError:
        raise BackupError("STATUS_JOURNAL_WRITE_FAILED") from None
    finally:
        os.close(descriptor)


def _is_code(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and all(
        character.isascii() and (character.isalnum() or character == "_")
        for character in value
    )
