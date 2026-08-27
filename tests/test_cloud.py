from __future__ import annotations

import json
import unittest

from module_loader import load


cloud_module = load("cloud")
models = load("models")


class FakeCloud:
    def __init__(self, devices: list[dict[str, object]]) -> None:
        self.devices = devices
        self.default_server = "cn"
        self.locale = "en_US"
        self.service_token = "".join(("fixture", "credential"))
        self.requests: list[dict[str, object]] = []

    async def async_get_devices(self, renew: bool = False):
        self.renew = renew
        return self.devices

    def get_api_by_host(self, host: str, api: str) -> str:
        return f"https://{host}/{api}"

    async def async_request_api(self, _api: str, request: dict[str, object], **_kwargs):
        self.requests.append(request)
        if len(self.requests) == 1:
            units = [
                {
                    "createTime": 2000 - index,
                    "fileId": f"fixture-{index}",
                    "isAlarm": index % 2 == 0,
                }
                for index in range(50)
            ]
        else:
            units = [{"createTime": 1500, "fileId": "fixture-older"}]
        return {"code": 0, "data": {"thirdPartPlayUnits": units}}

    @staticmethod
    def json_encode(value: object) -> str:
        return json.dumps(value, separators=(",", ":"))

    @staticmethod
    def rc4_params(_method: str, _url: str, params: dict[str, str]):
        return {"data": params["data"], "signature": "fixture-signature"}


class FakeHass:
    def __init__(self, sessions: dict[str, object]) -> None:
        self.data = {"xiaomi_miot": {"sessions": sessions}}


class CloudTests(unittest.IsolatedAsyncioTestCase):
    async def test_loaded_session_and_descending_pagination(self) -> None:
        cloud = FakeCloud([{"model": "xiaomi.lock.s1", "did": "fixture-device"}])
        target = await cloud_module.async_find_single_target(
            FakeHass({"fixture": cloud}), "xiaomi.lock.s1"
        )
        events = await cloud_module.async_get_events(target, 1000, 3000)
        self.assertEqual(51, len(events))
        self.assertEqual(1500, events[0].event_time_ms)
        self.assertEqual(2, len(cloud.requests))
        self.assertEqual(1950, cloud.requests[1]["endTime"])

    async def test_multiple_model_matches_fail_closed(self) -> None:
        first = FakeCloud([{"model": "xiaomi.lock.s1", "did": "fixture-a"}])
        second = FakeCloud([{"model": "xiaomi.lock.s1", "did": "fixture-b"}])
        with self.assertRaises(models.BackupError) as raised:
            await cloud_module.async_find_single_target(
                FakeHass({"first": first, "second": second}),
                "xiaomi.lock.s1",
            )
        self.assertEqual("TARGET_MATCH_COUNT_INVALID", raised.exception.code)

    async def test_missing_loaded_session_does_not_read_auth_storage(self) -> None:
        with self.assertRaises(models.BackupError) as raised:
            await cloud_module.async_find_single_target(FakeHass({}), "xiaomi.lock.s1")
        self.assertEqual("XIAOMI_MIOT_SESSION_UNAVAILABLE", raised.exception.code)

    async def test_digest_and_signed_url_keep_raw_values_in_memory_only(self) -> None:
        cloud = FakeCloud([{"model": "xiaomi.lock.s1", "did": "fixture-device"}])
        target = models.CloudTarget(cloud, "fixture-device", "xiaomi.lock.s1")
        event = models.CloudEvent(1234, "fixture-file", True)
        digest = cloud_module.event_digest(target.model, event.file_id)
        self.assertEqual(64, len(digest))
        self.assertNotIn(event.file_id, digest)
        signed_url = cloud_module.signed_playlist_url(target, event)
        self.assertTrue(signed_url.startswith("https://"))
        self.assertIn("yetAnotherServiceToken", signed_url)
        self.assertIn("fixture-file", signed_url)


if __name__ == "__main__":
    unittest.main()
