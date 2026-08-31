from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import MethodType, ModuleType, SimpleNamespace
import unittest

from module_loader import load


def _install_home_assistant_stubs() -> None:
    try:
        __import__("homeassistant")
        return
    except ModuleNotFoundError:
        pass

    homeassistant = ModuleType("homeassistant")
    config_entries = ModuleType("homeassistant.config_entries")
    core = ModuleType("homeassistant.core")
    helpers = ModuleType("homeassistant.helpers")
    event = ModuleType("homeassistant.helpers.event")
    storage = ModuleType("homeassistant.helpers.storage")

    config_entries.ConfigEntry = object
    core.HomeAssistant = object
    core.callback = lambda function: function
    event.async_call_later = lambda *_args, **_kwargs: None
    event.async_track_state_change_event = lambda *_args, **_kwargs: None
    event.async_track_time_change = lambda *_args, **_kwargs: None
    storage.Store = object

    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.event"] = event
    sys.modules["homeassistant.helpers.storage"] = storage


_install_home_assistant_stubs()
cloud_module = load("cloud")
manager_module = load("manager")
models = load("models")
state_module = load("state")


def _fixture_manager(state):
    manager = object.__new__(manager_module.BackupManager)
    manager._state = state

    async def save_state(_self):
        return None

    async def download_events(_self, target, events, _context, counts):
        for event in events:
            digest = cloud_module.event_digest(target.model, event.file_id)
            state.record_success(
                digest,
                manager_module._output_filename(event.event_time_ms, digest),
                event.event_time_ms,
                6000,
            )
            counts.downloaded += 1

    manager._save_state = MethodType(save_state, manager)
    manager._async_download_events = MethodType(download_events, manager)
    return manager


class HistoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_second_reservations_use_collision_suffixes(self) -> None:
        class FixtureHass:
            @staticmethod
            async def async_add_executor_job(function, *args):
                return function(*args)

        state = state_module.BackupState.initial(1)
        manager = object.__new__(manager_module.BackupManager)
        manager.hass = FixtureHass()

        async def save_state(_self):
            return None

        manager._save_state = MethodType(save_state, manager)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            manager.media_root = output
            first = await manager._async_reserve_filename(
                state,
                output,
                1_788_134_400_000,
                "a" * 64,
            )
            second = await manager._async_reserve_filename(
                state,
                output,
                1_788_134_400_500,
                "b" * 64,
            )
        self.assertEqual("xiaomi_lock_20260831T080000.mp4", first)
        self.assertEqual("xiaomi_lock_20260831T080000-02.mp4", second)

    async def test_event_changes_debounce_for_two_minutes(self) -> None:
        manager = object.__new__(manager_module.BackupManager)
        manager.hass = object()
        manager.options = SimpleNamespace(
            event_entity_ids=("event.fixture_pass", "event.fixture_stay"),
            event_delay_seconds=120,
        )
        manager._remove_event_listener = None
        manager._cancel_event_delay = None
        captured: dict[str, object] = {}
        cancelled = 0

        def track(_hass, entity_ids, action):
            captured["entity_ids"] = tuple(entity_ids)
            captured["action"] = action
            return lambda: None

        def call_later(_hass, delay, action):
            nonlocal cancelled
            captured["delay"] = delay
            captured["delayed_action"] = action

            def cancel():
                nonlocal cancelled
                cancelled += 1

            return cancel

        original_track = manager_module.async_track_state_change_event
        original_later = manager_module.async_call_later
        manager_module.async_track_state_change_event = track
        manager_module.async_call_later = call_later
        try:
            manager._install_event_triggers()
            action = captured["action"]
            event = SimpleNamespace(
                data={
                    "old_state": SimpleNamespace(state="old", attributes={}),
                    "new_state": SimpleNamespace(state="new", attributes={}),
                }
            )
            action(event)
            action(event)
        finally:
            manager_module.async_track_state_change_event = original_track
            manager_module.async_call_later = original_later

        self.assertEqual(
            ("event.fixture_pass", "event.fixture_stay"),
            captured["entity_ids"],
        )
        self.assertEqual(120, captured["delay"])
        self.assertEqual(1, cancelled)

    async def test_infrastructure_failure_is_final_only_on_third_run(self) -> None:
        state = state_module.BackupState.initial(5000)
        manager = _fixture_manager(state)
        reports: list[dict[str, object]] = []

        async def append_status(_self, report_key, **values):
            reports.append({"report_key": report_key, **values})

        manager._async_append_status_report = MethodType(append_status, manager)
        for attempt in range(1, 4):
            await manager._async_record_run_failure(
                "a" * 64,
                "EVENTLIST_REQUEST_FAILED",
            )
            self.assertEqual(attempt, state.consecutive_run_failures)

        self.assertEqual(
            ["retrying", "retrying", "failed"],
            [str(report["state"]) for report in reports],
        )
        self.assertEqual([1, 2, 3], [report["attempts"] for report in reports])

    async def test_history_download_limit_is_bounded(self) -> None:
        manager = _fixture_manager(state_module.BackupState.initial(5000))
        for value in (0, 101):
            with self.subTest(value=value), self.assertRaises(
                models.BackupError
            ) as raised:
                await manager.async_run_history_backfill(
                    dry_run=True,
                    max_downloads=value,
                )
            self.assertEqual("HISTORY_DOWNLOAD_LIMIT_INVALID", raised.exception.code)

    async def test_complete_history_does_not_rewind_incremental_cursor(self) -> None:
        state = state_module.BackupState.initial(5000)
        state.begin_history(5000)
        manager = _fixture_manager(state)
        target = models.CloudTarget(object(), "fixture-device", "xiaomi.lock.s1")
        pages = [
            models.CloudEventPage(
                events=(
                    models.CloudEvent(4400, "fixture-a", True),
                    models.CloudEvent(4500, "fixture-b", False),
                ),
                next_end_ms=3999,
                complete=False,
            ),
            models.CloudEventPage(events=(), next_end_ms=None, complete=True),
        ]
        requested_ends: list[int] = []

        async def get_page(_target, end_time_ms):
            requested_ends.append(end_time_ms)
            return pages.pop(0)

        original = manager_module.async_get_history_page
        manager_module.async_get_history_page = get_page
        try:
            result = await manager._async_execute_history_pages(
                target,
                manager_module._ExecutionContext(Path("."), "ffmpeg", "ffprobe"),
                manager_module._RunCounts(),
                10,
            )
        finally:
            manager_module.async_get_history_page = original

        self.assertEqual([5000, 3999], requested_ends)
        self.assertEqual("history_complete", result["status"])
        self.assertTrue(result["history_complete"])
        self.assertEqual(2, result["downloaded"])
        self.assertEqual(5000, state.cursor_ms)
        self.assertEqual(2, state.history_pages_completed)

    async def test_partial_page_is_requeried_instead_of_skipped(self) -> None:
        state = state_module.BackupState.initial(5000)
        state.begin_history(5000)
        manager = _fixture_manager(state)
        target = models.CloudTarget(object(), "fixture-device", "xiaomi.lock.s1")
        page = models.CloudEventPage(
            events=(
                models.CloudEvent(4400, "fixture-a", True),
                models.CloudEvent(4500, "fixture-b", False),
            ),
            next_end_ms=3999,
            complete=False,
        )

        async def get_page(_target, _end_time_ms):
            return page

        original = manager_module.async_get_history_page
        manager_module.async_get_history_page = get_page
        try:
            result = await manager._async_execute_history_pages(
                target,
                manager_module._ExecutionContext(Path("."), "ffmpeg", "ffprobe"),
                manager_module._RunCounts(),
                1,
            )
        finally:
            manager_module.async_get_history_page = original

        self.assertEqual("history_limit_reached", result["status"])
        self.assertFalse(result["history_complete"])
        self.assertEqual(5000, state.history_end_ms)
        self.assertEqual(0, state.history_pages_completed)
        self.assertEqual(1, result["downloaded"])


if __name__ == "__main__":
    unittest.main()
