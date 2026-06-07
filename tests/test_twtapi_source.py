from __future__ import annotations

import json
import unittest

from backend.twtapi_source import collect_twtapi_search, public_twtapi_config, twtapi_status


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout})
        if url.endswith("/Search"):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "tweets": [
                            {
                                "id": "123",
                                "text": "长飞光纤订单与产能讨论",
                                "created_at": "2026-06-04T12:00:00+08:00",
                                "user": {"id": "u1", "screen_name": "market_watch", "name": "Market Watch"},
                            }
                        ]
                    },
                }
            )
        return FakeResponse({"code": 404, "message": "unexpected endpoint"})


class TwtApiSourceTests(unittest.TestCase):
    def test_public_config_masks_api_key(self) -> None:
        config = public_twtapi_config({"api_base": "api.twtapi.com", "api_key": "secret-token", "default_queries": ["A股"]})
        self.assertEqual(config["api_base"], "https://api.twtapi.com/api/v1/twitter")
        self.assertEqual(config["api_key"], "****************")
        self.assertTrue(config["api_key_configured"])

    def test_collects_search_results_without_leaking_key(self) -> None:
        session = FakeSession()
        snapshots = collect_twtapi_search(
            "x-twtapi",
            {"window_start": "2026-06-01T00:00:00+08:00", "window_end": "2026-06-04T13:00:00+08:00"},
            "长飞光纤 601869",
            {"api_base": "https://api.twtapi.com/api/v1/twitter", "api_key": "secret-token", "default_queries": []},
            session=session,
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(session.calls[0]["url"], "https://api.twtapi.com/api/v1/twitter/Search")
        self.assertEqual(session.calls[0]["params"], {"q": "长飞光纤 601869", "type": "Latest", "count": 20})
        self.assertEqual(session.calls[0]["headers"]["X-API-Key"], "secret-token")
        self.assertEqual(session.calls[0]["headers"]["X-Lang"], "zh")
        self.assertNotIn("secret-token", snapshots[0]["content"])
        self.assertNotIn("secret-token", snapshots[0]["source_url"])
        payload = json.loads(snapshots[0]["content"])
        self.assertEqual(payload["platform"], "x_twtapi")
        self.assertEqual(payload["tweets"][0]["text"], "长飞光纤订单与产能讨论")

    def test_status_verifies_search_endpoint(self) -> None:
        session = FakeSession()
        status = twtapi_status({"api_key": "secret-token", "default_queries": ["机器人"]}, session=session)
        self.assertEqual(status["status"], "online")
        self.assertEqual(status["sample_query"], "机器人")
        self.assertTrue(status["sample_available"])


if __name__ == "__main__":
    unittest.main()
