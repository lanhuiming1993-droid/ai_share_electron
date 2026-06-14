from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import unittest


def load_source_auth_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "alphadesk-cloud-report"
        / "scripts"
        / "source_auth.py"
    )
    spec = importlib.util.spec_from_file_location("alphadesk_source_auth", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script_path.parent))
    spec.loader.exec_module(module)
    return module


class AlphaDeskSourceAuthScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = load_source_auth_module()

    def test_check_status_calls_three_source_endpoints(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_request(method: str, _base_url: str, path: str, *args, **kwargs):
            calls.append((method, path))
            if path.endswith("component-status"):
                return {"status": "pending", "wechat_authorized": False, "subscription_count": 0}
            return {"status": "online", "message": "ok"}

        with patch.object(self.script, "request", side_effect=fake_request):
            status = self.script.check_status("http://api")

        self.assertEqual(
            calls,
            [
                ("GET", "/api/channels/wechat-mp-rss/component-status"),
                ("POST", "/api/channels/ima-knowledge/check"),
                ("POST", "/api/channels/zsxq/check"),
            ],
        )
        self.assertFalse(status["wechat-mp-rss"]["wechat_authorized"])

    def test_start_werss_login_saves_qr_image(self) -> None:
        def fake_request(method: str, _base_url: str, path: str, *args, **kwargs):
            if path.endswith("wechat-login"):
                return {"qr_image_url": "/api/channels/wechat-mp-rss/qr-image"}
            if path.endswith("qr-image"):
                return b"png-bytes"
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp, patch.object(self.script, "request", side_effect=fake_request):
            path = self.script.start_werss_login("http://api", Path(tmp))

            self.assertTrue(path.name.startswith("werss-login-"))
            self.assertEqual(path.read_bytes(), b"png-bytes")

    def test_werss_search_caches_candidates(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_request(method: str, _base_url: str, path: str, *args, **kwargs):
            calls.append((method, path))
            return {"items": [{"id": "mp-1", "name": "半导体观察"}], "count": 1}

        with tempfile.TemporaryDirectory() as tmp, patch.object(self.script, "request", side_effect=fake_request):
            cache_path = Path(tmp) / "cache.json"
            result = self.script.search_werss_subscriptions("http://api", "半导体", cache_path)
            cache_text = cache_path.read_text(encoding="utf-8")

        self.assertEqual(result["count"], 1)
        self.assertEqual(calls, [("GET", "/api/channels/wechat-mp-rss/subscriptions/search?q=%E5%8D%8A%E5%AF%BC%E4%BD%93")])
        self.assertIn("半导体观察", cache_text)

    def test_werss_add_uses_cached_candidate_number(self) -> None:
        payloads: list[dict] = []

        def fake_request(method: str, _base_url: str, path: str, payload=None, **kwargs):
            payloads.append(payload or {})
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/api/channels/wechat-mp-rss/subscriptions")
            return {
                "subscription": {"id": "mp-2", "name": "产业研究"},
                "subscriptions": [{"id": "mp-2", "name": "产业研究"}],
                "subscription_count": 1,
                "wechat_authorized": True,
                "status": "online",
                "message": "ok",
            }

        with tempfile.TemporaryDirectory() as tmp, patch.object(self.script, "request", side_effect=fake_request):
            cache_path = Path(tmp) / "cache.json"
            cache_path.write_text(
                '{"items":[{"id":"mp-1","name":"半导体观察"},{"id":"mp-2","name":"产业研究"}]}',
                encoding="utf-8",
            )
            result = self.script.add_werss_subscription_from_value("http://api", "2", cache_path)

        self.assertEqual(payloads[0], {"id": "mp-2", "name": "产业研究"})
        self.assertEqual(result["subscription"]["id"], "mp-2")

    def test_werss_remove_and_backfill_match_existing_subscription(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def fake_request(method: str, _base_url: str, path: str, payload=None, **kwargs):
            calls.append((method, path, payload))
            if path.endswith("/subscriptions") and method == "GET":
                return {
                    "subscriptions": [{"id": "mp-1", "name": "半导体观察"}],
                    "subscription_count": 1,
                    "wechat_authorized": True,
                    "status": "online",
                    "message": "ok",
                }
            if method == "DELETE":
                return {"subscriptions": [], "subscription_count": 0, "wechat_authorized": True, "status": "online", "message": "ok"}
            if path.endswith("/backfill"):
                return {"submitted_count": 1, "failed_count": 0, "results": [{"id": "mp-1", "status": "submitted"}]}
            raise AssertionError((method, path))

        with patch.object(self.script, "request", side_effect=fake_request):
            self.script.remove_werss_subscription("http://api", "半导体观察")
            self.script.backfill_werss_subscriptions("http://api", "半导体", 0, 1)

        self.assertIn(("DELETE", "/api/channels/wechat-mp-rss/subscriptions/mp-1", None), calls)
        self.assertIn(
            ("POST", "/api/channels/wechat-mp-rss/subscriptions/backfill", {"start_page": 0, "end_page": 1, "subscription_ids": ["mp-1"]}),
            calls,
        )

    def test_werss_action_emits_qr_when_authorization_expired(self) -> None:
        def fake_search(*_args, **_kwargs):
            raise RuntimeError("WeRSS 微信搜索授权已失效，请重新扫码授权")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(self.script, "search_werss_subscriptions", side_effect=fake_search),
            patch.object(self.script, "emit_werss_login_hint", return_value=Path(tmp) / "qr.png") as emit,
            patch.object(sys, "argv", ["source_auth.py", "werss-search", "--query", "半导体", "--output-dir", tmp]),
        ):
            code = self.script.main()

        self.assertEqual(code, 2)
        self.assertTrue(emit.called)

    def test_configure_ima_and_zsxq_send_secrets_without_printing_them(self) -> None:
        payloads: list[dict] = []

        def fake_request(_method: str, _base_url: str, _path: str, payload=None, **_kwargs):
            payloads.append(payload or {})
            return {"status": "saved"}

        with patch.object(self.script, "request", side_effect=fake_request):
            self.script.configure_ima("http://api", "client", "secret-key", "")
            self.script.configure_zsxq("http://api", "https://mcp.example/?api_key=secret", False)

        self.assertEqual(payloads[0], {"client_id": "client", "api_key": "secret-key"})
        self.assertEqual(payloads[1], {"mcp_url": "https://mcp.example/?api_key=secret", "include_comments": False})


if __name__ == "__main__":
    unittest.main()
