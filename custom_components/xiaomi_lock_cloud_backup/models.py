"""Privacy-safe data models for the integration."""

from __future__ import annotations

from dataclasses import dataclass


class BackupError(RuntimeError):
    """An error whose public representation is a fixed code only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CloudEvent:
    """An in-memory cloud event. Raw identifiers must never be persisted."""

    event_time_ms: int
    file_id: str
    is_alarm: bool


@dataclass(frozen=True, slots=True)
class CloudEventPage:
    """One bounded event-list response and its older-page cursor."""

    events: tuple[CloudEvent, ...]
    next_end_ms: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class CloudTarget:
    """An in-memory target binding."""

    cloud: object
    did: str
    model: str


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Safe metadata for one validated MP4."""

    size_bytes: int
    duration_seconds: float
    video_codec: str
    audio_present: bool
