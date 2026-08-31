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
        filename = "xiaomi_lock_20260828T080000.mp4"
        state.record_success(digest, filename, 200, 300)
        serialized = repr(state.to_dict())
        self.assertNotIn(raw_identifier, serialized)
        self.assertIn(digest, serialized)
        self.assertEqual(200, state.cursor_ms)

    def test_third_failure_quarantines_and_advances_watermark(self) -> None:
        digest = "a" * 64
        state = state_module.BackupState.initial(10)
        state.reserve_filename(digest, "xiaomi_lock_20260828T080000.mp4")
        self.assertFalse(state.record_failure(digest, 20))
        self.assertFalse(state.record_failure(digest, 20))
        self.assertTrue(state.record_failure(digest, 20))
        self.assertTrue(state.has_seen(digest))
        self.assertNotIn(digest, state.failures)
        self.assertNotIn(digest, state.pending_files)
        self.assertEqual(20, state.cursor_ms)

    def test_older_history_success_does_not_rewind_incremental_cursor(self) -> None:
        digest = "b" * 64
        state = state_module.BackupState.initial(5000)
        filename = "xiaomi_lock_20260826T080000.mp4"
        state.record_success(digest, filename, 2000, 6000)
        self.assertEqual(5000, state.cursor_ms)

    def test_history_cursor_is_resumable_and_legacy_state_migrates(self) -> None:
        state = state_module.BackupState.from_dict({"cursor_ms": 5000})
        self.assertIsNone(state.history_end_ms)
        self.assertFalse(state.history_complete)
        state.begin_history(4000)
        state.advance_history(3000)
        with self.assertRaises(models.BackupError):
            state.advance_history(3000)
        state.complete_history()
        restored = state_module.BackupState.from_dict(state.to_dict())
        self.assertTrue(restored.history_complete)
        self.assertIsNone(restored.history_end_ms)
        self.assertEqual(2, restored.history_pages_completed)

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
                    paths.current_filename(1_800_000_000_000 + index * 1000): index
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
            paths.current_filename(1_800_000_000_000 + index * 1000): index
            for index in range(const.MAX_MANAGED_FILES)
        }
        with self.assertRaises(models.BackupError) as raised:
            state.require_managed_capacity(
                "xiaomi_lock_20280115T080000.mp4"
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
            managed = output / "xiaomi_lock_20260828T080000.mp4"
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
            managed = output / "xiaomi_lock_20260828T080000.mp4"
            alternate = output / "alternate.bin"
            managed.write_bytes(b"fixture")
            os.link(managed, alternate)
            with self.assertRaises(models.BackupError):
                paths.unlink_managed_file(output, output, managed.name)
            self.assertTrue(managed.exists())
            self.assertTrue(alternate.exists())

    def test_legacy_names_migrate_to_beijing_time_without_collision(self) -> None:
        names = [
            "xiaomi_lock_20260828T000000000Z_aaaaaaaaaaaa.mp4",
            "xiaomi_lock_20260828T000000500Z_bbbbbbbbbbbb.mp4",
        ]
        mapping = paths.build_filename_migration(names)
        self.assertEqual("xiaomi_lock_20260828T080000.mp4", mapping[names[0]])
        self.assertEqual("xiaomi_lock_20260828T080000-02.mp4", mapping[names[1]])

    def test_filename_migration_round_trip_preserves_bytes(self) -> None:
        legacy = "xiaomi_lock_20260828T000000000Z_aaaaaaaaaaaa.mp4"
        current = "xiaomi_lock_20260828T080000.mp4"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            source = output / legacy
            source.write_bytes(b"synthetic-video")
            paths.migrate_managed_filenames(
                output,
                output,
                {legacy: current},
            )
            self.assertFalse(source.exists())
            self.assertEqual(b"synthetic-video", (output / current).read_bytes())
            paths.rollback_managed_filenames(
                output,
                output,
                {legacy: current},
            )
            self.assertEqual(b"synthetic-video", source.read_bytes())
            self.assertFalse((output / current).exists())

    def test_state_filename_migration_preserves_digest_authority(self) -> None:
        digest = "a" * 64
        legacy = "xiaomi_lock_20260828T000000000Z_aaaaaaaaaaaa.mp4"
        current = "xiaomi_lock_20260828T080000.mp4"
        state = state_module.BackupState.initial(1)
        state.record_success(digest, legacy, 2, 3)
        state.migrate_filenames({legacy: current})
        self.assertEqual({current: 3}, state.managed_files)
        self.assertEqual(current, state.filename_for_event(digest))
        self.assertEqual(current, state_module.BackupState.from_dict(state.to_dict()).filename_for_event(digest))

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
        self.assertEqual(120, normalized[const.CONF_EVENT_DELAY_SECONDS])
        self.assertEqual([], normalized[const.CONF_EVENT_ENTITY_IDS])
        self.assertNotIn("username", normalized)
        self.assertNotIn("password", normalized)

    def test_zero_retention_disables_downloader_owned_deletion(self) -> None:
        for value in (0, "0"):
            with self.subTest(value=value):
                normalized = settings.validate_settings(
                    {const.CONF_RETENTION_DAYS: value}
                )
                self.assertEqual(0, normalized[const.CONF_RETENTION_DAYS])

    def test_invalid_model_schedule_and_output_are_rejected(self) -> None:
        cases = (
            {const.CONF_TARGET_MODEL: "Bad Model"},
            {const.CONF_SCHEDULE_TIME: "25:00:00"},
            {const.CONF_OUTPUT_SUBDIRECTORY: "../outside"},
            {const.CONF_EVENT_DELAY_SECONDS: 29},
            {const.CONF_EVENT_ENTITY_IDS: ["sensor.not_an_event"]},
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
                const.CONF_EVENT_DELAY_SECONDS: 120,
                const.CONF_EVENT_ENTITY_IDS: ["event.fixture_pass", "event.fixture_stay"],
            },
        )
        self.assertEqual("xiaomi.lock.s1", options.target_model)
        self.assertEqual("fixed_output", options.output_subdirectory)
        self.assertEqual(4, options.schedule_time.hour)
        self.assertEqual(120, options.event_delay_seconds)
        self.assertEqual(
            ("event.fixture_pass", "event.fixture_stay"),
            options.event_entity_ids,
        )


if __name__ == "__main__":
    unittest.main()
