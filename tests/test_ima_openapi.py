from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend import ima_openapi


class ImaOpenApiTests(unittest.TestCase):
    def test_collect_ima_knowledge_base_uses_public_metadata_only(self) -> None:
        def fake_request(api_path: str, body: dict, timeout: int | None = None, config: dict | None = None) -> dict:
            self.assertEqual(config["client_id"], "cid")
            self.assertEqual(config["api_key"], "secret")
            if api_path == "openapi/wiki/v1/search_knowledge_base":
                return {
                    "info_list": [
                        {
                            "kb_id": "secret-kb-id",
                            "kb_name": "每日复盘更新",
                            "description": "每日AI自动抓取复盘",
                            "content_count": "278",
                        }
                    ],
                    "is_end": True,
                }
            if api_path == "openapi/wiki/v1/search_knowledge":
                self.assertEqual(body["knowledge_base_id"], "secret-kb-id")
                return {
                    "info_list": [
                        {
                            "media_id": "secret-media-id",
                            "title": "卓胜微纪要",
                            "highlight_content": "<em>卓胜微</em> 光模块更新",
                        }
                    ],
                    "is_end": True,
                }
            raise AssertionError(api_path)

        with patch.object(ima_openapi, "ima_openapi_request", side_effect=fake_request):
            snapshots = ima_openapi.collect_ima_knowledge_base(
                "ima-knowledge",
                {"window_start": "2026-06-01T00:00:00+00:00", "window_end": "2026-06-02T00:00:00+00:00"},
                "卓胜微",
                {"client_id": "cid", "api_key": "secret"},
            )
        self.assertEqual(len(snapshots), 1)
        payload = json.loads(snapshots[0]["content"])
        self.assertEqual(payload["knowledge_base"]["name"], "每日复盘更新")
        self.assertEqual(payload["results"][0]["title"], "卓胜微纪要")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-kb-id", serialized)
        self.assertNotIn("secret-media-id", serialized)
        self.assertNotIn("secret-kb-id", snapshots[0]["source_url"])


if __name__ == "__main__":
    unittest.main()
