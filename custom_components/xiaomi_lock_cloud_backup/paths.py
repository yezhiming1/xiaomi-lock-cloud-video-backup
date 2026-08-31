"""Output-path confinement and project-owned retention helpers."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
from datetime import datetime, timedelta, timezone

from .models import BackupError


_LEGACY_MANAGED_FILENAME_PATTERN = re.compile(
    r"xiaomi_lock_\d{8}T\d{9}Z_[0-9a-f]{12}\.mp4"
)
_CURRENT_MANAGED_FILENAME_PATTERN = re.compile(
    r"xiaomi_lock_\d{8}T\d{6}(?:-\d{2,3})?\.mp4"
)
_LEGACY_DETAILS_PATTERN = re.compile(
    r"xiaomi_lock_(?P<stamp>\d{8}T\d{9}Z)_(?P<digest>[0-9a-f]{12})\.mp4"
)
_BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


def validate_output_subdirectory(value: str) -> str:
    """Validate a relative POSIX path intended to live below `/media`."""
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise BackupError("OUTPUT_SUBDIRECTORY_INVALID")
    if "\\" in value or "\x00" in value:
        raise BackupError("OUTPUT_SUBDIRECTORY_INVALID")
    relative = PurePosixPath(value.strip())
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise BackupError("OUTPUT_SUBDIRECTORY_INVALID")
    return relative.as_posix()


def resolve_output_directory(media_root: Path, subdirectory: str) -> Path:
    """Resolve a configured path while keeping it below the media root."""
    normalized = validate_output_subdirectory(subdirectory)
    try:
        if media_root.is_symlink():
            raise BackupError("MEDIA_ROOT_UNSAFE")
        root = media_root.resolve(strict=True)
        candidate = root.joinpath(*PurePosixPath(normalized).parts)
        parent = _resolve_without_symlink(root, candidate.parent)
    except FileNotFoundError:
        raise BackupError("OUTPUT_PARENT_MISSING") from None
    except OSError:
        raise BackupError("OUTPUT_RESOLUTION_FAILED") from None
    try:
        parent.relative_to(root)
    except ValueError:
        raise BackupError("OUTPUT_OUTSIDE_MEDIA_ROOT") from None
    if candidate.exists() and (candidate.is_symlink() or not candidate.is_dir()):
        raise BackupError("OUTPUT_DIRECTORY_UNSAFE")
    return candidate


def ensure_output_directory(media_root: Path, subdirectory: str) -> Path:
    """Create only the final project-owned leaf below an existing parent."""
    candidate = resolve_output_directory(media_root, subdirectory)
    if not candidate.exists():
        candidate.mkdir(mode=0o750)
    if candidate.is_symlink() or not candidate.is_dir():
        raise BackupError("OUTPUT_DIRECTORY_UNSAFE")
    return candidate


def safe_managed_path(
    media_root: Path,
    output_directory: Path,
    filename: str,
) -> Path:
    """Return one managed file path without following a filename escape."""
    if not is_managed_filename(filename):
        raise BackupError("MANAGED_FILENAME_INVALID")
    _validate_existing_directory(media_root, output_directory)
    candidate = output_directory / filename
    if candidate.is_symlink():
        raise BackupError("MANAGED_FILE_UNSAFE")
    return candidate


def unlink_managed_file(
    media_root: Path,
    output_directory: Path,
    filename: str,
) -> bool:
    """Delete exactly one regular project-owned file without following links."""
    candidate = safe_managed_path(media_root, output_directory, filename)
    if all(function in os.supports_dir_fd for function in (os.open, os.stat, os.unlink)):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(output_directory, flags)
        try:
            try:
                stat_result = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_nlink != 1:
                raise BackupError("MANAGED_FILE_UNSAFE")
            os.unlink(filename, dir_fd=descriptor)
            return True
        finally:
            os.close(descriptor)
    try:
        stat_result = candidate.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not candidate.is_file() or candidate.is_symlink() or stat_result.st_nlink != 1:
        raise BackupError("MANAGED_FILE_UNSAFE")
    os.unlink(candidate)
    return True


def is_managed_filename(value: object) -> bool:
    return isinstance(value, str) and bool(
        _LEGACY_MANAGED_FILENAME_PATTERN.fullmatch(value)
        or _CURRENT_MANAGED_FILENAME_PATTERN.fullmatch(value)
    )


def is_legacy_managed_filename(value: object) -> bool:
    return isinstance(value, str) and bool(
        _LEGACY_MANAGED_FILENAME_PATTERN.fullmatch(value)
    )


def current_filename(event_time_ms: int, sequence: int = 1) -> str:
    if (
        not isinstance(event_time_ms, int)
        or isinstance(event_time_ms, bool)
        or event_time_ms < 0
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 1 <= sequence <= 999
    ):
        raise BackupError("MANAGED_FILENAME_INVALID")
    stamp = datetime.fromtimestamp(
        event_time_ms / 1000,
        tz=timezone.utc,
    ).astimezone(_BEIJING).strftime("%Y%m%dT%H%M%S")
    suffix = "" if sequence == 1 else f"-{sequence:02d}"
    return f"xiaomi_lock_{stamp}{suffix}.mp4"


def legacy_filename_details(value: str) -> tuple[int, str] | None:
    match = _LEGACY_DETAILS_PATTERN.fullmatch(value)
    if not match:
        return None
    try:
        parsed = datetime.strptime(
            match.group("stamp"),
            "%Y%m%dT%H%M%S%fZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000), match.group("digest")


def build_filename_migration(filenames: list[str]) -> dict[str, str]:
    """Build a deterministic no-overwrite legacy-to-Beijing mapping."""
    legacy: list[tuple[int, str]] = []
    occupied = {name for name in filenames if not is_legacy_managed_filename(name)}
    for name in filenames:
        details = legacy_filename_details(name)
        if details is not None:
            legacy.append((details[0], name))
    mapping: dict[str, str] = {}
    for event_time_ms, name in sorted(legacy):
        for sequence in range(1, 1000):
            candidate = current_filename(event_time_ms, sequence)
            if candidate not in occupied:
                mapping[name] = candidate
                occupied.add(candidate)
                break
        else:
            raise BackupError("FILENAME_COLLISION_CAPACITY_REACHED")
    return mapping


def migrate_managed_filenames(
    media_root: Path,
    output_directory: Path,
    mapping: dict[str, str],
) -> None:
    """Rename regular managed files without overwriting any destination."""
    _validate_existing_directory(media_root, output_directory)
    items = list(mapping.items())
    for old_name, new_name in items:
        if not is_legacy_managed_filename(old_name) or not is_managed_filename(new_name):
            raise BackupError("FILENAME_MIGRATION_INVALID")
        old_path = safe_managed_path(media_root, output_directory, old_name)
        new_path = safe_managed_path(media_root, output_directory, new_name)
        try:
            metadata = old_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            raise BackupError("FILENAME_MIGRATION_SOURCE_MISSING") from None
        if not stat.S_ISREG(metadata.st_mode) or old_path.is_symlink():
            raise BackupError("FILENAME_MIGRATION_SOURCE_UNSAFE")
        if new_path.exists() or new_path.is_symlink():
            raise BackupError("FILENAME_MIGRATION_TARGET_EXISTS")
    completed: list[tuple[str, str]] = []
    try:
        for old_name, new_name in items:
            old_path = output_directory / old_name
            new_path = output_directory / new_name
            os.link(old_path, new_path, follow_symlinks=False)
            if old_path.stat(follow_symlinks=False).st_ino != new_path.stat(
                follow_symlinks=False
            ).st_ino:
                raise BackupError("FILENAME_MIGRATION_LINK_MISMATCH")
            os.unlink(old_path)
            completed.append((old_name, new_name))
        _fsync_directory(output_directory)
    except Exception:
        _rollback_filename_items(output_directory, completed)
        raise


def rollback_managed_filenames(
    media_root: Path,
    output_directory: Path,
    mapping: dict[str, str],
) -> None:
    _validate_existing_directory(media_root, output_directory)
    _rollback_filename_items(output_directory, list(mapping.items()))


def _rollback_filename_items(
    output_directory: Path,
    items: list[tuple[str, str]],
) -> None:
    for old_name, new_name in reversed(items):
        old_path = output_directory / old_name
        new_path = output_directory / new_name
        if old_path.exists() and new_path.exists():
            if os.path.samefile(old_path, new_path):
                os.unlink(new_path)
                continue
            raise BackupError("FILENAME_MIGRATION_ROLLBACK_CONFLICT")
        if old_path.exists():
            continue
        if not new_path.exists() or new_path.is_symlink():
            raise BackupError("FILENAME_MIGRATION_ROLLBACK_MISSING")
        os.link(new_path, old_path, follow_symlinks=False)
        os.unlink(new_path)
    _fsync_directory(output_directory)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows does not expose directory handles through os.open; the target
        # Home Assistant runtime is Linux, where directory fsync is mandatory.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _resolve_without_symlink(root: Path, candidate: Path) -> Path:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise BackupError("OUTPUT_OUTSIDE_MEDIA_ROOT") from None
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise BackupError("OUTPUT_DIRECTORY_UNSAFE")
    resolved_root = root.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=True)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        raise BackupError("OUTPUT_OUTSIDE_MEDIA_ROOT") from None
    return resolved_candidate


def _validate_existing_directory(media_root: Path, output_directory: Path) -> None:
    if media_root.is_symlink():
        raise BackupError("MEDIA_ROOT_UNSAFE")
    try:
        root = media_root.absolute()
        resolved = _resolve_without_symlink(root, output_directory.absolute())
        resolved.relative_to(root.resolve(strict=True))
    except FileNotFoundError:
        raise BackupError("OUTPUT_DIRECTORY_INVALID") from None
    except OSError:
        raise BackupError("OUTPUT_RESOLUTION_FAILED") from None
    if not output_directory.is_dir() or output_directory.is_symlink():
        raise BackupError("OUTPUT_DIRECTORY_UNSAFE")
