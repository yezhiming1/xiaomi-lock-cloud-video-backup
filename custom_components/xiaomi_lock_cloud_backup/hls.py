"""Bounded, privacy-safe downloader for Xiaomi AES-128 HLS recordings."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import requests

from .const import INTEGRATION_VERSION
from .models import BackupError, DownloadResult


MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_RESOURCE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_SEGMENT_BYTES = 192 * 1024 * 1024
MAX_FINAL_BYTES = 512 * 1024 * 1024
MAX_SEGMENTS = 128
MAX_REDIRECTS = 3
MAX_PROBE_OUTPUT_BYTES = 64 * 1024


def validate_remote_url(url: str) -> None:
    """Accept HTTPS Internet URLs and reject obvious local-network targets."""
    if not isinstance(url, str) or len(url) > 16_384:
        raise BackupError("UNSAFE_MEDIA_URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BackupError("UNSAFE_MEDIA_URL")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        raise BackupError("UNSAFE_MEDIA_HOST")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise BackupError("UNSAFE_MEDIA_HOST")


def _read_bounded(
    response: requests.Response,
    *,
    max_bytes: int,
    too_large_code: str,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            parsed_length = int(content_length)
        except ValueError:
            raise BackupError("MEDIA_CONTENT_LENGTH_INVALID") from None
        if parsed_length < 0:
            raise BackupError("MEDIA_CONTENT_LENGTH_INVALID")
        if parsed_length > max_bytes:
            raise BackupError(too_large_code)
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > max_bytes:
            raise BackupError(too_large_code)
        chunks.append(chunk)
    return b"".join(chunks)


def _request_without_unsafe_redirects(
    session: requests.Session,
    remote_url: str,
) -> tuple[requests.Response, str]:
    current = remote_url
    for redirect_count in range(MAX_REDIRECTS + 1):
        validate_remote_url(current)
        response = session.get(
            current,
            allow_redirects=False,
            stream=True,
            timeout=(10, 30),
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.raise_for_status()
            return response, current
        location = response.headers.get("Location")
        response.close()
        if not location or redirect_count >= MAX_REDIRECTS:
            raise BackupError("MEDIA_REDIRECT_REJECTED")
        current = urljoin(current, location)
    raise BackupError("MEDIA_REDIRECT_REJECTED")


def _validate_xiaomi_status(response: requests.Response) -> None:
    raw_status = response.headers.get("x-xiaomi-status-code")
    if raw_status is None:
        return
    try:
        status = float(raw_status)
    except (TypeError, ValueError):
        raise BackupError("XIAOMI_MEDIA_STATUS_INVALID") from None
    if not math.isfinite(status) or status < 0:
        raise BackupError("XIAOMI_MEDIA_STATUS_INVALID")
    if status >= 400:
        raise BackupError("XIAOMI_MEDIA_STATUS_REJECTED")


def _fetch_payload(
    session: requests.Session,
    remote_url: str,
    *,
    max_bytes: int,
    too_large_code: str,
    fetch_failure_code: str,
) -> tuple[bytes, str]:
    response: requests.Response | None = None
    try:
        response, final_url = _request_without_unsafe_redirects(session, remote_url)
        _validate_xiaomi_status(response)
        return (
            _read_bounded(response, max_bytes=max_bytes, too_large_code=too_large_code),
            final_url,
        )
    except BackupError:
        raise
    except requests.RequestException:
        raise BackupError(fetch_failure_code) from None
    finally:
        if response is not None:
            response.close()


@dataclass(frozen=True, slots=True)
class _EncryptedSegment:
    media_url: str
    key_url: str
    iv: bytes


_ATTRIBUTE_PATTERN = re.compile(
    r"(?:^|,)\s*(?P<name>[A-Z0-9-]+)="
    r"(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<plain>[^,]*))"
)


def _parse_attributes(value: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for match in _ATTRIBUTE_PATTERN.finditer(value):
        parsed = match.group("double")
        if parsed is None:
            parsed = match.group("single")
        if parsed is None:
            parsed = match.group("plain")
        attributes[match.group("name")] = (parsed or "").strip()
    return attributes


def parse_encrypted_playlist(
    playlist_text: str,
    base_url: str,
) -> tuple[_EncryptedSegment, ...]:
    """Parse the deliberately small HLS subset used by tested lock recordings."""
    normalized = playlist_text.lstrip("\ufeff \t\r\n")
    first_line = normalized.splitlines()[0].strip() if normalized else ""
    if first_line != "#EXTM3U":
        raise BackupError("PLAYLIST_NOT_HLS")

    media_sequence = 0
    segment_index = 0
    current_key_url: str | None = None
    current_iv: bytes | None = None
    segments: list[_EncryptedSegment] = []
    for original_line in normalized.splitlines():
        line = original_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            raise BackupError("MASTER_PLAYLIST_UNSUPPORTED")
        if line.startswith("#EXT-X-MAP"):
            raise BackupError("PLAYLIST_MAP_UNSUPPORTED")
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except (IndexError, ValueError):
                raise BackupError("MEDIA_SEQUENCE_INVALID") from None
            if media_sequence < 0 or media_sequence >= 1 << 128:
                raise BackupError("MEDIA_SEQUENCE_INVALID")
            continue
        if line.startswith("#EXT-X-KEY:"):
            attributes = _parse_attributes(line.split(":", 1)[1])
            if attributes.get("METHOD") != "AES-128":
                raise BackupError("PLAYLIST_KEY_UNSUPPORTED")
            if attributes.get("KEYFORMAT") not in (None, "identity"):
                raise BackupError("PLAYLIST_KEY_UNSUPPORTED")
            raw_key_url = attributes.get("URI")
            if not raw_key_url:
                raise BackupError("PLAYLIST_KEY_URI_MISSING")
            current_key_url = urljoin(base_url, raw_key_url)
            validate_remote_url(current_key_url)
            raw_iv = attributes.get("IV")
            if raw_iv is None:
                current_iv = None
            elif re.fullmatch(r"0[xX][0-9a-fA-F]{32}", raw_iv):
                current_iv = bytes.fromhex(raw_iv[2:])
            else:
                raise BackupError("PLAYLIST_IV_INVALID")
            continue
        if line.startswith("#"):
            continue
        if current_key_url is None:
            raise BackupError("PLAYLIST_KEY_MISSING")
        if len(segments) >= MAX_SEGMENTS:
            raise BackupError("PLAYLIST_SEGMENT_LIMIT")
        media_url = urljoin(base_url, line)
        validate_remote_url(media_url)
        sequence = media_sequence + segment_index
        if sequence >= 1 << 128:
            raise BackupError("MEDIA_SEQUENCE_INVALID")
        iv = current_iv if current_iv is not None else sequence.to_bytes(16, "big")
        segments.append(_EncryptedSegment(media_url, current_key_url, iv))
        segment_index += 1
    if not segments:
        raise BackupError("PLAYLIST_NO_SEGMENTS")
    return tuple(segments)


def decrypt_aes128_cbc(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt and unpad one HLS segment without placing key material on disk."""
    if len(key) != 16:
        raise BackupError("PLAYLIST_KEY_SIZE_INVALID")
    if len(iv) != 16 or not ciphertext or len(ciphertext) % 16:
        raise BackupError("SEGMENT_CIPHERTEXT_INVALID")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except (TypeError, ValueError):
        raise BackupError("SEGMENT_DECRYPT_FAILED") from None


def _write_private_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()


def _tool_environment() -> dict[str, str]:
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _local_path_argument(value: Path) -> str:
    if not value.is_absolute() or "://" in str(value):
        raise BackupError("LOCAL_MEDIA_PATH_REQUIRED")
    return str(value)


def build_segment_command(
    input_path: Path,
    output_path: Path,
    ffmpeg_binary: str,
    *,
    keep_audio: bool,
) -> tuple[str, ...]:
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-fflags",
        "+genpts",
        "-i",
        _local_path_argument(input_path),
        "-map",
        "0:v:0",
    ]
    if keep_audio:
        command.extend(("-map", "0:a?"))
    else:
        command.append("-an")
    command.extend(
        (
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-n",
            _local_path_argument(output_path),
        )
    )
    return tuple(command)


def build_concat_command(
    concat_list_path: Path,
    output_path: Path,
    ffmpeg_binary: str,
    *,
    keep_audio: bool,
) -> tuple[str, ...]:
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-fflags",
        "+genpts",
        "-f",
        "concat",
        "-safe",
        "1",
        "-i",
        _local_path_argument(concat_list_path),
        "-map",
        "0:v:0",
    ]
    if keep_audio:
        command.extend(("-map", "0:a?"))
    else:
        command.append("-an")
    command.extend(
        (
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            "-n",
            _local_path_argument(output_path),
        )
    )
    return tuple(command)


def _classify_media_tool_failure(stderr: bytes, stage: str) -> str:
    detail = stderr[:MAX_PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace").lower()
    if any(marker in detail for marker in ("matches no streams", "does not contain any stream")):
        return f"{stage}_NO_VIDEO_STREAM"
    if any(marker in detail for marker in ("could not write header", "error muxing a packet")):
        return f"{stage}_MUX_FAILED"
    if any(marker in detail for marker in ("invalid data found", "error opening input")):
        return f"{stage}_INPUT_INVALID"
    return f"{stage}_FAILED"


def _run_ffmpeg(command: tuple[str, ...], *, timeout_seconds: int, stage: str) -> None:
    if any(argument.startswith(("http://", "https://", "crypto+")) for argument in command):
        raise BackupError("NON_LOCAL_FFMPEG_INPUT")
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            env=_tool_environment(),
        )
    except subprocess.TimeoutExpired:
        raise BackupError(f"{stage}_TIMEOUT") from None
    except OSError:
        raise BackupError(f"{stage}_EXEC_FAILED") from None
    if completed.returncode:
        raise BackupError(_classify_media_tool_failure(completed.stderr or b"", stage))


def _probe_output(output_path: Path, ffprobe_binary: str) -> DownloadResult:
    command = (
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name",
        "-of",
        "json",
        _local_path_argument(output_path),
    )
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=_tool_environment(),
        )
    except subprocess.TimeoutExpired:
        raise BackupError("FFPROBE_TIMEOUT") from None
    except OSError:
        raise BackupError("FFPROBE_EXEC_FAILED") from None
    if completed.returncode or len(completed.stdout) > MAX_PROBE_OUTPUT_BYTES:
        raise BackupError("FFPROBE_FAILED")
    try:
        data = json.loads(completed.stdout)
        streams = data.get("streams") or []
        videos = [item for item in streams if item.get("codec_type") == "video"]
        audios = [item for item in streams if item.get("codec_type") == "audio"]
        duration = float((data.get("format") or {}).get("duration"))
        codec = str(videos[0].get("codec_name") or "unknown")
    except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        raise BackupError("FFPROBE_RESULT_INVALID") from None
    if len(videos) != 1 or not 0 < duration <= 86_400:
        raise BackupError("FFPROBE_RESULT_INVALID")
    if not re.fullmatch(r"[a-z0-9_.-]{1,32}", codec):
        codec = "unknown"
    return DownloadResult(
        size_bytes=output_path.stat().st_size,
        duration_seconds=round(duration, 3),
        video_codec=codec,
        audio_present=bool(audios),
    )


def inspect_local_output(output_path: Path, ffprobe_binary: str) -> DownloadResult:
    """Validate an already-published local output after an interrupted state save."""
    if (
        not output_path.is_absolute()
        or not output_path.is_file()
        or output_path.is_symlink()
        or output_path.stat().st_nlink != 1
    ):
        raise BackupError("OUTPUT_RECOVERY_UNSAFE")
    size = output_path.stat().st_size
    if size <= 0 or size > MAX_FINAL_BYTES:
        raise BackupError("OUTPUT_SIZE_INVALID")
    return _probe_output(output_path, ffprobe_binary)


def _atomic_publish(partial_path: Path, output_path: Path) -> None:
    """Publish without overwriting a destination that appears during the run."""
    try:
        with partial_path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError:
        raise BackupError("OUTPUT_SYNC_FAILED") from None
    try:
        os.link(partial_path, output_path, follow_symlinks=False)
    except FileExistsError:
        raise BackupError("OUTPUT_ALREADY_EXISTS") from None
    except OSError:
        raise BackupError("ATOMIC_PUBLISH_UNAVAILABLE") from None
    partial_path.unlink()
    try:
        directory_descriptor = os.open(
            output_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        pass


def download_hls_once(
    remote_playlist_url: str,
    output_path: Path,
    ffmpeg_binary: str,
    ffprobe_binary: str,
    *,
    keep_audio: bool,
    timeout_seconds: int = 300,
) -> DownloadResult:
    """Decrypt one recording in temporary storage and atomically publish an MP4."""
    validate_remote_url(remote_playlist_url)
    if output_path.exists():
        raise BackupError("OUTPUT_ALREADY_EXISTS")
    if not output_path.is_absolute() or not output_path.parent.is_dir():
        raise BackupError("OUTPUT_DIRECTORY_INVALID")
    if output_path.parent.is_symlink():
        raise BackupError("OUTPUT_DIRECTORY_UNSAFE")
    if any(output_path.parent.glob(".*.partial.*")):
        raise BackupError("UNRESOLVED_PARTIAL_EXISTS")
    partial_path = output_path.parent / (
        f".{output_path.stem}.partial.{secrets.token_hex(8)}.mp4"
    )

    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {"User-Agent": f"xiaomi-lock-cloud-video-backup/{INTEGRATION_VERSION}"}
    )
    key_cache: dict[str, bytes] = {}
    deadline = time.monotonic() + timeout_seconds
    try:
        playlist_payload, final_playlist_url = _fetch_payload(
            session,
            remote_playlist_url,
            max_bytes=MAX_PLAYLIST_BYTES,
            too_large_code="PLAYLIST_TOO_LARGE",
            fetch_failure_code="PLAYLIST_FETCH_FAILED",
        )
        if not playlist_payload:
            raise BackupError("PLAYLIST_EMPTY")
        try:
            playlist_text = playlist_payload.decode("utf-8-sig", errors="strict")
        except UnicodeError:
            raise BackupError("PLAYLIST_DECODE_FAILED") from None
        segments = parse_encrypted_playlist(playlist_text, final_playlist_url)
        playlist_payload = b""
        playlist_text = ""

        temp_root = "/tmp" if Path("/tmp").is_dir() else None
        total_encrypted_bytes = 0
        total_remuxed_bytes = 0
        remuxed_names: list[str] = []
        with tempfile.TemporaryDirectory(
            prefix="xiaomi-lock-backup-", dir=temp_root
        ) as directory:
            temp_directory = Path(directory)
            for index, segment in enumerate(segments):
                if time.monotonic() >= deadline:
                    raise BackupError("DOWNLOAD_TIMEOUT")
                key = key_cache.get(segment.key_url)
                if key is None:
                    key, _ = _fetch_payload(
                        session,
                        segment.key_url,
                        max_bytes=17,
                        too_large_code="PLAYLIST_KEY_SIZE_INVALID",
                        fetch_failure_code="PLAYLIST_KEY_FETCH_FAILED",
                    )
                    if len(key) != 16:
                        raise BackupError("PLAYLIST_KEY_SIZE_INVALID")
                    key_cache[segment.key_url] = key

                encrypted, _ = _fetch_payload(
                    session,
                    segment.media_url,
                    max_bytes=MAX_RESOURCE_BYTES,
                    too_large_code="RESOURCE_TOO_LARGE",
                    fetch_failure_code="SEGMENT_FETCH_FAILED",
                )
                total_encrypted_bytes += len(encrypted)
                if total_encrypted_bytes > MAX_TOTAL_SEGMENT_BYTES:
                    raise BackupError("TOTAL_SEGMENT_SIZE_INVALID")
                decrypted = decrypt_aes128_cbc(encrypted, key, segment.iv)
                encrypted = b""

                input_path = temp_directory / f"segment_{index:03d}.media"
                output_name = f"segment_{index:03d}.mp4"
                remuxed_path = temp_directory / output_name
                _write_private_bytes(input_path, decrypted)
                decrypted = b""
                try:
                    _run_ffmpeg(
                        build_segment_command(
                            input_path,
                            remuxed_path,
                            ffmpeg_binary,
                            keep_audio=keep_audio,
                        ),
                        timeout_seconds=max(1, int(deadline - time.monotonic())),
                        stage="SEGMENT",
                    )
                finally:
                    if input_path.exists():
                        input_path.unlink()
                if not remuxed_path.is_file() or remuxed_path.stat().st_size <= 0:
                    raise BackupError("SEGMENT_OUTPUT_INVALID")
                total_remuxed_bytes += remuxed_path.stat().st_size
                if total_remuxed_bytes > MAX_FINAL_BYTES:
                    raise BackupError("OUTPUT_SIZE_INVALID")
                remuxed_names.append(output_name)

            concat_path = temp_directory / "segments.txt"
            concat_payload = "".join(
                f"file '{name}'\n" for name in remuxed_names
            ).encode("ascii")
            _write_private_bytes(concat_path, concat_payload)
            _run_ffmpeg(
                build_concat_command(
                    concat_path,
                    partial_path,
                    ffmpeg_binary,
                    keep_audio=keep_audio,
                ),
                timeout_seconds=max(1, int(deadline - time.monotonic())),
                stage="CONCAT",
            )

        if not partial_path.is_file():
            raise BackupError("OUTPUT_MISSING")
        size = partial_path.stat().st_size
        if size <= 0 or size > MAX_FINAL_BYTES:
            raise BackupError("OUTPUT_SIZE_INVALID")
        result = _probe_output(partial_path, ffprobe_binary)
        os.chmod(partial_path, 0o640)
        _atomic_publish(partial_path, output_path)
        return result
    except BackupError:
        raise
    except (OSError, ValueError):
        raise BackupError("LOCAL_IO_FAILED") from None
    finally:
        key_cache.clear()
        session.close()
        if partial_path.exists():
            partial_path.unlink()
