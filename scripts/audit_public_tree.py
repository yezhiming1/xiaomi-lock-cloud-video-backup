"""Fail closed when public-tree candidates contain private or runtime material."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
FORBIDDEN_SUFFIXES = {
    ".7z",
    ".aac",
    ".apk",
    ".db",
    ".gif",
    ".gz",
    ".har",
    ".heic",
    ".jpeg",
    ".jpg",
    ".key",
    ".log",
    ".m3u8",
    ".mov",
    ".mp4",
    ".p12",
    ".pcap",
    ".pem",
    ".pfx",
    ".png",
    ".sqlite",
    ".tar",
    ".ts",
    ".wav",
    ".webp",
    ".zip",
}
FORBIDDEN_PARTS = {".storage", "__pycache__", "dist", "media"}
CONTENT_RULES = (
    (
        "private_ipv4",
        re.compile(
            r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
            r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
        ),
    ),
    ("local_ha_hostname", re.compile(r"(?i)\bhomeassistant\.local\b")),
    ("windows_user_path", re.compile(r"(?i)\b[A-Z]:\\Users\\")),
    (
        "provider_secret_prefix",
        re.compile(r"\b(?:ghp_|github_pat_|glpat-|sk-[A-Za-z])[A-Za-z0-9_-]{12,}"),
    ),
    (
        "jwt_literal",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "authorization_literal",
        re.compile(r"(?i)Authorization\s*[:=]\s*['\"]Bearer\s+[A-Za-z0-9._-]{8,}"),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)(?:password|cookie|access[_-]?token|service[_-]?token)"
            r"\s*[:=]\s*['\"][^'\"]{8,}['\"]"
        ),
    ),
)


def _git_paths() -> list[Path]:
    completed = subprocess.run(
        ("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("git_inventory_failed")
    return [ROOT / item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]


def _record(failures: list[tuple[str, str]], path: Path, rule: str) -> None:
    failures.append((path.relative_to(ROOT).as_posix(), rule))


def main() -> int:
    failures: list[tuple[str, str]] = []
    paths = _git_paths()
    for path in paths:
        relative = path.relative_to(ROOT)
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts & FORBIDDEN_PARTS:
            _record(failures, path, "runtime_path")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.lower().startswith(".env"):
            _record(failures, path, "forbidden_artifact_type")
            continue
        if path.is_symlink():
            _record(failures, path, "symlink_not_allowed")
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            _record(failures, path, "file_unreadable")
            continue
        if len(payload) > MAX_TEXT_FILE_BYTES:
            _record(failures, path, "file_too_large")
            continue
        if b"\0" in payload:
            _record(failures, path, "binary_file")
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeError:
            _record(failures, path, "non_utf8_text")
            continue
        if any(line.endswith((" ", "\t")) for line in text.splitlines()):
            _record(failures, path, "trailing_whitespace")
        if path.resolve() != SELF:
            for name, pattern in CONTENT_RULES:
                if pattern.search(text):
                    _record(failures, path, name)
        if path.suffix.lower() == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError:
                _record(failures, path, "invalid_json")

    diff_check = subprocess.run(
        ("git", "diff", "--check"),
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if diff_check.returncode:
        failures.append((".", "git_diff_check"))
    if failures:
        for relative, rule in sorted(set(failures)):
            print(f"FAIL path={relative} rule={rule}")
        return 1
    print(f"PASS files={len(paths)} rules={len(CONTENT_RULES) + 9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
