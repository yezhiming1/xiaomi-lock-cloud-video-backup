from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from module_loader import load


models = load("models")
state_module = load("state")
status_journal = load("status_journal")


class StatusJournalTests(unittest.TestCase):
    def test_status_handoff_is_append_only_and_deidentified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            entry_key = "private-home-assistant-entry"
            first_key = status_journal.status_report_key(entry_key, 1, "incremental")
            second_key = status_journal.status_report_key(entry_key, 2, "incremental")

            status_journal.append_status_report(
                output,
                report_key=first_key,
                state="retrying",
                attempts=1,
                error_code="SEGMENT_FETCH_FAILED",
            )
            status_journal.append_status_report(
                output,
                report_key=second_key,
                state="downloaded",
                attempts=0,
                error_code="none",
            )

            journal = output / status_journal.STATUS_JOURNAL_NAME
            raw = journal.read_text(encoding="utf-8")
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual([first_key, second_key], [row["report_key"] for row in rows])
            self.assertNotIn(entry_key, raw)
            self.assertEqual({"retrying", "downloaded"}, {row["state"] for row in rows})

    def test_invalid_report_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(models.BackupError, "STATUS_REPORT_INVALID"):
                status_journal.append_status_report(
                    Path(directory),
                    report_key="not-a-digest",
                    state="failed",
                    attempts=3,
                    error_code="SEGMENT_FETCH_FAILED",
                )
            self.assertFalse(
                (Path(directory) / status_journal.STATUS_JOURNAL_NAME).exists()
            )

    def test_existing_hard_link_is_rejected_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            target = output / "unrelated"
            target.write_text("unchanged", encoding="utf-8")
            journal = output / status_journal.STATUS_JOURNAL_NAME
            os.link(target, journal)
            with self.assertRaisesRegex(models.BackupError, "STATUS_JOURNAL_UNSAFE"):
                status_journal.append_status_report(
                    output,
                    report_key="a" * 64,
                    state="failed",
                    attempts=3,
                    error_code="SEGMENT_FETCH_FAILED",
                )
            self.assertEqual("unchanged", target.read_text(encoding="utf-8"))

    def test_existing_symbolic_link_is_rejected_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            target = output / "unrelated"
            target.write_text("unchanged", encoding="utf-8")
            journal = output / status_journal.STATUS_JOURNAL_NAME
            try:
                os.symlink(target, journal)
            except (NotImplementedError, OSError):
                self.skipTest("symbolic links are unavailable")
            with self.assertRaisesRegex(models.BackupError, "STATUS_JOURNAL_UNSAFE"):
                status_journal.append_status_report(
                    output,
                    report_key="a" * 64,
                    state="failed",
                    attempts=3,
                    error_code="SEGMENT_FETCH_FAILED",
                )
            self.assertEqual("unchanged", target.read_text(encoding="utf-8"))

    def test_legacy_state_gains_bounded_run_failure_tracking(self) -> None:
        state = state_module.BackupState.from_dict(
            {
                "cursor_ms": 1,
                "seen": [],
                "failures": {},
                "managed_files": {},
                "last_run_status": "ok",
                "last_error_code": "none",
            }
        )
        self.assertEqual(1, state.begin_status_report())
        self.assertEqual(1, state.record_run_failure())
        state.record_run_success()
        restored = state_module.BackupState.from_dict(state.to_dict())
        self.assertEqual(1, restored.status_report_sequence)
        self.assertEqual(0, restored.consecutive_run_failures)


if __name__ == "__main__":
    unittest.main()
