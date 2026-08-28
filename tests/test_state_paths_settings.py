from __future__ import annotations

import json
from pathlib import Path
import os
import tempfile
import unittest

from module_loader import load


const = load("const")
models = load("models")
paths = load("paths")
settings = load("settings")
state_module = load("state")


class StateTests(unittest.TestCase):
    def test_state_never_persists_raw_event_identifier(self) -> None:
        raw_identifier = "fixture-file-identifier"
        digest = __import__("hashlib").sha256(raw_identifier.encode()).hexdigest()
        state = state_module.BackupState.initial(100)
        filename = f"xiaomi_lock_20260828T000000000Z_{digest[:12]}.mp4"
        state.record_success(digest, filename, 200, 300)
        serialized = repr(state.to_dict())
        self.assertNotIn(raw_identifier, serialized)
        self.assertIn(digest, serialized)
        self.assertEqual(200, state.cursor_ms)

    def test_third_failure_quarantines_and_advances_watermark(self) -> None:
        digest = "a" * 64
        state = state_module.BackupState.initial(10)
        self.assertFalse(state.record_failure(digest, 20))
        self.assertFalse(state.record_failure(digest, 20))
        self.assertTrue(state.record_failure(digest, 20))
        self.assertTrue(state.has_seen(digest))
        self.assertNotIn(digest, state.failures)
        self.assertEqual(20, state.cursor_ms)

    def test_state_rejects_non_code_diagnostic_text(self) -> None:
        with self.assertRaises(models.BackupError):
            state_module.BackupState.from_dict(
                {
                    "cursor_ms": 1,
                    "last_run_status": "unsafe detail with spaces",
                    "last_error_code": "none",
                }
            )

    def test_loaded_collections_are_bounded(self) -> None:
        digests = [f"{index:064x}" for index in range(const.MAX_SEEN_IDENTIFIERS + 5)]
        loaded = state_module.BackupState.from_dict(
            {
                "cursor_ms": 1,
                "seen": digests,
                "failures": {digest: 1 for digest in digests},
                "managed_files": {
                    f"xiaomi_lock_20260828T000000000Z_{index:012x}.mp4": index
                    for index in range(const.MAX_MANAGED_FILES)
                },
                "last_run_status": "ok",
            }
        )
        self.assertEqual(const.MAX_SEEN_IDENTIFIERS, len(loaded.seen))
        self.assertEqual(const.MAX_SEEN_IDENTIFIERS, len(loaded.failures))
        self.assertEqual(const.MAX_MANAGED_FILES, len(loaded.managed_files))

    def test_managed_file_capacity_fails_without_dropping_authority(self) -> None:
        state = state_module.BackupState.initial(1)
        state.managed_files = {
            f"xiaomi_lock_20260828T000000000Z_{index:012x}.mp4": index
            for index in range(const.MAX_MANAGED_FILES)
        }
        with self.assertRaises(models.BackupError) as raised:
            state.require_managed_capacity(
                "xiaomi_lock_20260828T000000000Z_ffffffffffff.mp4"
            )
        self.assertEqual("STATE_MANAGED_CAPACITY_REACHED", raised.exception.code)
        self.assertEqual(const.MAX_MANAGED_FILES, len(state.managed_files))


class PathTests(unittest.TestCase):
    def test_output_is_confined_to_existing_media_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory).resolve()
            output = paths.ensure_output_directory(media_root, "lock_backup")
            self.assertEqual(media_root / "lock_backup", output)
            self.assertTrue(output.is_dir())
            with self.assertRaises(models.BackupError):
                paths.ensure_output_directory(media_root, "../escape")
            with self.assertRaises(models.BackupError):
                paths.ensure_output_directory(media_root, "missing/leaf")

    def test_retention_unlinks_only_safe_managed_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            managed = output / "xiaomi_lock_20260828T000000000Z_abcdef123456.mp4"
            managed.write_bytes(b"fixture")
            self.assertTrue(paths.unlink_managed_file(output, output, managed.name))
            self.assertFalse(managed.exists())
            unmanaged = output / "unmanaged.mp4"
            unmanaged.write_bytes(b"fixture")
            with self.assertRaises(models.BackupError):
                paths.unlink_managed_file(output, output, unmanaged.name)
            self.assertTrue(unmanaged.exists())

    def test_retention_rejects_extra_hard_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            managed = output / "xiaomi_lock_20260828T000000000Z_abcdef123456.mp4"
            alternate = output / "alternate.bin"
            managed.write_bytes(b"fixture")
            os.link(managed, alternate)
            with self.assertRaises(models.BackupError):
                paths.unlink_managed_file(output, output, managed.name)
            self.assertTrue(managed.exists())
            self.assertTrue(alternate.exists())

    def test_output_rejects_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            outside = root / "outside"
            outside.mkdir()
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks unavailable")
            with self.assertRaises(models.BackupError):
                paths.ensure_output_directory(root, "linked/leaf")


class SettingsTests(unittest.TestCase):
    def test_version_surfaces_match(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (
                project_root
                / "custom_components"
                / "xiaomi_lock_cloud_backup"
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        release_version = (project_root / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(const.INTEGRATION_VERSION, manifest["version"])
        self.assertEqual(f"V{manifest['version']}", release_version)

    def test_defaults_normalize_without_credentials(self) -> None:
        normalized = settings.validate_settings({})
        self.assertEqual("03:30:00", normalized[const.CONF_SCHEDULE_TIME])
        self.assertNotIn("username", normalized)
        self.assertNotIn("password", normalized)

    def test_invalid_model_schedule_and_output_are_rejected(self) -> None:
        cases = (
            {const.CONF_TARGET_MODEL: "Bad Model"},
            {const.CONF_SCHEDULE_TIME: "25:00:00"},
            {const.CONF_OUTPUT_SUBDIRECTORY: "../outside"},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(models.BackupError):
                settings.validate_settings(value)

    def test_options_cannot_rebind_target_or_output_directory(self) -> None:
        configured = settings.validate_settings(
            {
                const.CONF_TARGET_MODEL: "xiaomi.lock.s1",
                const.CONF_OUTPUT_SUBDIRECTORY: "fixed_output",
            }
        )
        options = settings.options_from_mappings(
            configured,
            {
                const.CONF_TARGET_MODEL: "fixture.other",
                const.CONF_OUTPUT_SUBDIRECTORY: "other_output",
                const.CONF_SCHEDULE_TIME: "04:00:00",
            },
        )
        self.assertEqual("xiaomi.lock.s1", options.target_model)
        self.assertEqual("fixed_output", options.output_subdirectory)
        self.assertEqual(4, options.schedule_time.hour)


if __name__ == "__main__":
    unittest.main()
