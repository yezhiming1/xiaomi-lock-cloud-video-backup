from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import urlsplit

from module_loader import load


hls = load("hls")
models = load("models")


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str = "application/octet-stream") -> None:
        self.payload = payload
        self.status_code = 200
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": content_type,
            "x-xiaomi-status-code": "200",
        }

    def raise_for_status(self) -> None:
        return

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]

    def close(self) -> None:
        return


class HlsTests(unittest.TestCase):
    def test_private_or_plain_http_urls_are_rejected(self) -> None:
        for remote_url in (
            "http://media.example/index.m3u8",
            "https://127.0.0.1/index.m3u8",
            "https://192.0.2.1/index.m3u8",
            "https://service.local/index.m3u8",
        ):
            with self.subTest(remote_url=remote_url), self.assertRaises(
                models.BackupError
            ):
                hls.validate_remote_url(remote_url)

    def test_playlist_derives_iv_and_rejects_unsupported_map(self) -> None:
        segments = hls.parse_encrypted_playlist(
            (
                "\ufeff\n#EXTM3U\n"
                "#EXT-X-MEDIA-SEQUENCE:7\n"
                "#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
                "segment.ts\n"
            ),
            "https://media.example/path/index.m3u8",
        )
        self.assertEqual((7).to_bytes(16, "big"), segments[0].iv)
        self.assertEqual("https://media.example/path/key.bin", segments[0].key_url)
        with self.assertRaises(models.BackupError) as raised:
            hls.parse_encrypted_playlist(
                (
                    "#EXTM3U\n"
                    "#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
                    "#EXT-X-MAP:URI=\"init.mp4\"\n"
                    "segment.m4s\n"
                ),
                "https://media.example/index.m3u8",
            )
        self.assertEqual("PLAYLIST_MAP_UNSUPPORTED", raised.exception.code)

    def test_xiaomi_status_is_reduced_to_fixed_codes(self) -> None:
        response = FakeResponse(b"fixture")
        response.headers["x-xiaomi-status-code"] = "503"
        with self.assertRaises(models.BackupError) as raised:
            hls._validate_xiaomi_status(response)
        self.assertEqual("XIAOMI_MEDIA_STATUS_REJECTED", raised.exception.code)
        response.headers["x-xiaomi-status-code"] = "upstream-private-detail"
        with self.assertRaises(models.BackupError) as raised:
            hls._validate_xiaomi_status(response)
        self.assertEqual("XIAOMI_MEDIA_STATUS_INVALID", raised.exception.code)
        response.headers["x-xiaomi-status-code"] = "nan"
        with self.assertRaises(models.BackupError) as raised:
            hls._validate_xiaomi_status(response)
        self.assertEqual("XIAOMI_MEDIA_STATUS_INVALID", raised.exception.code)

    def test_hls_magic_requires_a_complete_header_line(self) -> None:
        with self.assertRaises(models.BackupError) as raised:
            hls.parse_encrypted_playlist(
                "#EXTM3U-not-a-header\n",
                "https://media.example/index.m3u8",
            )
        self.assertEqual("PLAYLIST_NOT_HLS", raised.exception.code)

    def test_atomic_publish_never_overwrites_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            partial = root / "partial.mp4"
            output = root / "output.mp4"
            partial.write_bytes(b"new")
            output.write_bytes(b"existing")
            with self.assertRaises(models.BackupError) as raised:
                hls._atomic_publish(partial, output)
            self.assertEqual("OUTPUT_ALREADY_EXISTS", raised.exception.code)
            self.assertEqual(b"existing", output.read_bytes())
            self.assertEqual(b"new", partial.read_bytes())

    def test_non_hls_response_never_publishes_output(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.trust_env = True
                self.headers: dict[str, str] = {}

            def get(self, _url: str, **_kwargs):
                return FakeResponse(b'{"error":"fixture"}', "application/json")

            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "sample.mp4"
            original_session = hls.requests.Session
            hls.requests.Session = FakeSession
            try:
                with self.assertRaises(models.BackupError) as raised:
                    hls.download_hls_once(
                        "https://fixture.example/index.m3u8",
                        output,
                        "ffmpeg",
                        "ffprobe",
                        keep_audio=True,
                        timeout_seconds=5,
                    )
            finally:
                hls.requests.Session = original_session
            self.assertEqual("PLAYLIST_NOT_HLS", raised.exception.code)
            self.assertFalse(output.exists())
            self.assertEqual([], list(output.parent.glob(".*.partial.*")))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg toolchain unavailable",
    )
    def test_encrypted_hls_is_decrypted_and_media_commands_stay_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture_directory = root / "fixture"
            fixture_directory.mkdir()
            source_playlist = fixture_directory / "index.m3u8"
            generated = subprocess.run(
                (
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x240:r=10:d=1.2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1.2",
                    "-shortest",
                    "-c:v",
                    "mpeg2video",
                    "-g",
                    "5",
                    "-c:a",
                    "aac",
                    "-f",
                    "hls",
                    "-hls_time",
                    "0.5",
                    "-hls_playlist_type",
                    "vod",
                    "-hls_segment_filename",
                    str(fixture_directory / "segment%d.ts"),
                    str(source_playlist),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, generated.returncode, generated.stderr.decode(errors="replace"))

            key = b"0123456789abcdef"
            iv = bytes.fromhex("00112233445566778899aabbccddeeff")
            lines = source_playlist.read_text(encoding="utf-8").splitlines()
            lines.insert(
                1,
                "#EXT-X-KEY:METHOD=AES-128,"
                'URI="https://fixture.example/key.bin",'
                f"IV=0x{iv.hex()}",
            )
            payloads: dict[str, bytes] = {
                "index.m3u8": ("\n".join(lines) + "\n").encode(),
                "key.bin": key,
            }
            for segment_path in sorted(fixture_directory.glob("segment*.ts")):
                padder = hls.padding.PKCS7(hls.algorithms.AES.block_size).padder()
                padded = padder.update(segment_path.read_bytes()) + padder.finalize()
                encryptor = hls.Cipher(
                    hls.algorithms.AES(key), hls.modes.CBC(iv)
                ).encryptor()
                payloads[segment_path.name] = encryptor.update(padded) + encryptor.finalize()

            class FakeSession:
                def __init__(self) -> None:
                    self.trust_env = True
                    self.headers: dict[str, str] = {}

                def get(self, remote_url: str, **_kwargs):
                    name = Path(urlsplit(remote_url).path).name
                    return FakeResponse(payloads[name])

                def close(self) -> None:
                    return

            output = root / "sample.mp4"
            original_session = hls.requests.Session
            original_run = hls.subprocess.run
            captured_commands: list[tuple[str, ...]] = []

            def capture_run(*args, **kwargs):
                captured_commands.append(tuple(args[0]))
                return original_run(*args, **kwargs)

            hls.requests.Session = FakeSession
            hls.subprocess.run = capture_run
            try:
                result = hls.download_hls_once(
                    "https://fixture.example/index.m3u8",
                    output,
                    shutil.which("ffmpeg") or "ffmpeg",
                    shutil.which("ffprobe") or "ffprobe",
                    keep_audio=True,
                    timeout_seconds=60,
                )
            finally:
                hls.requests.Session = original_session
                hls.subprocess.run = original_run

            self.assertTrue(output.is_file())
            self.assertEqual(output.stat().st_size, result.size_bytes)
            self.assertTrue(result.audio_present)
            rendered = "\n".join(" ".join(command) for command in captured_commands)
            self.assertNotIn("https://", rendered)
            self.assertNotIn(key.hex(), rendered)
            self.assertNotIn(iv.hex(), rendered)


if __name__ == "__main__":
    unittest.main()
