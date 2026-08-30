"""Validation for non-secret integration settings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import re
from typing import Mapping

from .const import (
    CONF_KEEP_AUDIO,
    CONF_MAX_DOWNLOADS_PER_RUN,
    CONF_OUTPUT_SUBDIRECTORY,
    CONF_RETENTION_DAYS,
    CONF_SCHEDULE_TIME,
    CONF_TARGET_MODEL,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    MUTABLE_OPTION_KEYS,
    default_options,
)
from .models import BackupError
from .paths import validate_output_subdirectory


@dataclass(frozen=True, slots=True)
class BackupOptions:
    target_model: str
    schedule_time: time
    output_subdirectory: str
    retention_days: int
    max_downloads_per_run: int
    keep_audio: bool


def validate_settings(value: Mapping[str, object]) -> dict[str, object]:
    """Return normalized settings or a fixed-code validation failure."""
    defaults = default_options()
    merged = {**defaults, **dict(value)}

    model = merged.get(CONF_TARGET_MODEL)
    if not isinstance(model, str) or not re.fullmatch(r"[a-z0-9_.-]{3,128}", model):
        raise BackupError("TARGET_MODEL_INVALID")

    schedule_value = merged.get(CONF_SCHEDULE_TIME)
    if not isinstance(schedule_value, str):
        raise BackupError("SCHEDULE_TIME_INVALID")
    try:
        schedule = time.fromisoformat(schedule_value)
    except ValueError:
        raise BackupError("SCHEDULE_TIME_INVALID") from None
    if schedule.tzinfo is not None or schedule.microsecond:
        raise BackupError("SCHEDULE_TIME_INVALID")

    output = validate_output_subdirectory(
        str(merged.get(CONF_OUTPUT_SUBDIRECTORY) or "")
    )
    retention = _bounded_integer(
        merged.get(CONF_RETENTION_DAYS),
        MIN_RETENTION_DAYS,
        MAX_RETENTION_DAYS,
        "RETENTION_DAYS_INVALID",
    )
    maximum = _bounded_integer(
        merged.get(CONF_MAX_DOWNLOADS_PER_RUN),
        1,
        100,
        "MAX_DOWNLOADS_INVALID",
    )
    keep_audio = merged.get(CONF_KEEP_AUDIO)
    if not isinstance(keep_audio, bool):
        raise BackupError("KEEP_AUDIO_INVALID")

    return {
        CONF_TARGET_MODEL: model,
        CONF_SCHEDULE_TIME: schedule.strftime("%H:%M:%S"),
        CONF_OUTPUT_SUBDIRECTORY: output,
        CONF_RETENTION_DAYS: retention,
        CONF_MAX_DOWNLOADS_PER_RUN: maximum,
        CONF_KEEP_AUDIO: keep_audio,
    }


def options_from_mappings(
    data: Mapping[str, object],
    options: Mapping[str, object],
) -> BackupOptions:
    mutable_overrides = {
        key: options[key] for key in MUTABLE_OPTION_KEYS if key in options
    }
    normalized = validate_settings({**dict(data), **mutable_overrides})
    return BackupOptions(
        target_model=str(normalized[CONF_TARGET_MODEL]),
        schedule_time=time.fromisoformat(str(normalized[CONF_SCHEDULE_TIME])),
        output_subdirectory=str(normalized[CONF_OUTPUT_SUBDIRECTORY]),
        retention_days=int(normalized[CONF_RETENTION_DAYS]),
        max_downloads_per_run=int(normalized[CONF_MAX_DOWNLOADS_PER_RUN]),
        keep_audio=bool(normalized[CONF_KEEP_AUDIO]),
    )


def _bounded_integer(value: object, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool):
        raise BackupError(code)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise BackupError(code)
    if parsed < minimum or parsed > maximum:
        raise BackupError(code)
    return parsed
