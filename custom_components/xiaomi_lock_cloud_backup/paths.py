"""Output-path confinement and project-owned retention helpers."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat

from .models import BackupError


_MANAGED_FILENAME_PATTERN = re.compile(
    r"xiaomi_lock_\d{8}T\d{9}Z_[0-9a-f]{12}\.mp4"
)


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
    return isinstance(value, str) and bool(_MANAGED_FILENAME_PATTERN.fullmatch(value))


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
