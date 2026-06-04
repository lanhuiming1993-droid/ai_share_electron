from __future__ import annotations

import json
import unittest

from backend.itick_source import collect_itick_market_data, itick_status, public_itick_config


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
        if url.endswith("/stock/quote"):
            return FakeResponse({"code": 0, "data": {"code": params["code"], "region": params["region"], "last": 188.8, "timestamp": 1780570800000}})
        if url.endswith("/stock/kline"):
            return FakeResponse({"code": 0, "data": [{"open": 180, "close": 188.8, "time": 1780570800000}]})
        return FakeResponse({"code": 404, "message": "unexpected endpoint"})


class ItickSourceTests(unittest.TestCase):
    def test_public_config_masks_api_key(self) -> None:
        config = public_itick_config({"api_base": "api0.itick.org", "api_key": "secret-token", "default_symbols": ["HK:700"]})
        self.assertEqual(config["api_base"], "https://api0.itick.org")
        self.assertEqual(config["api_key"], "****************")
        self.assertTrue(config["api_key_configured"])

    def test_collects_stock_quote_and_kline_without_leaking_token(self) -> None:
        session = FakeSession()
        snapshots = collect_itick_market_data(
            "itick",
            {"window_start": "2026-06-01T00:00:00+08:00", "window_end": "2026-06-04T00:00:00+08:00"},
            "请补证 600519",
            {"api_base": "https://api0.itick.org", "api_key": "secret-token", "default_symbols": []},
            session=session,
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(session.calls[0]["params"], {"region": "SH", "code": "600519"})
        self.assertEqual(session.calls[0]["headers"]["token"], "secret-token")
        self.assertEqual(session.calls[1]["params"]["kType"], 2)
        self.assertNotIn("secret-token", snapshots[0]["content"])
        self.assertNotIn("secret-token", snapshots[0]["source_url"])
        payload = json.loads(snapshots[0]["content"])
        self.assertEqual(payload["platform"], "itick_market_data")
        self.assertEqual(payload["symbol"], {"region": "SH", "code": "600519"})
        self.assertEqual(payload["quote"]["data"]["last"], 188.8)

    def test_status_uses_first_default_symbol(self) -> None:
        session = FakeSession()
        status = itick_status({"api_base": "https://api0.itick.org", "api_key": "secret-token", "default_symbols": ["US:AAPL"]}, session=session)
        self.assertEqual(status["status"], "online")
        self.assertEqual(status["sample_symbol"], {"region": "US", "code": "AAPL"})


if __name__ == "__main__":
    unittest.main()
