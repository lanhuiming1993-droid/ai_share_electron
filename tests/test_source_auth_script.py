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
