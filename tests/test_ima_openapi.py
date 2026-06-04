from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend import ima_openapi


class ImaOpenApiTests(unittest.TestCase):
    def test_browse_ima_knowledge_base_recurses_into_folders(self) -> None:
        def fake_request(api_path: str, body: dict, timeout: int | None = None, config: dict | None = None) -> dict:
            self.assertEqual(api_path, "openapi/wiki/v1/get_knowledge_list")
            if "folder_id" not in body:
                return {
                    "folder_info_list": [{"folder_id": "secret-folder-id", "name": "Research folder", "file_number": 1}],
                    "knowledge_info_list": [],
                    "is_end": True,
                }
            self.assertEqual(body["folder_id"], "secret-folder-id")
            return {
                "folder_info_list": [],
                "knowledge_info_list": [{"media_id": "secret-media-id", "title": "Nested document"}],
                "is_end": True,
            }

        with patch.object(ima_openapi, "ima_openapi_request", side_effect=fake_request):
            items = ima_openapi.search_or_list_kb_items(
                {"id": "secret-kb-id", "name": "Daily Research", "description": ""},
                "",
                5,
                {"client_id": "cid", "api_key": "secret", "content_fetch_limit": 0},
            )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Nested document")

    def test_collect_ima_knowledge_base_reads_note_content_without_leaking_ids(self) -> None:
        def fake_request(api_path: str, body: dict, timeout: int | None = None, config: dict | None = None) -> dict:
            self.assertEqual(config["client_id"], "cid")
            self.assertEqual(config["api_key"], "secret")
            if api_path == "openapi/wiki/v1/search_knowledge_base":
                return {
                    "info_list": [
                        {
                            "kb_id": "secret-kb-id",
                            "kb_name": "Daily Research",
                            "description": "Private IMA knowledge base",
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
                            "media_type": 11,
                            "title": "RF front-end memo",
                            "highlight_content": "<em>RF</em> module update",
                        }
                    ],
                    "is_end": True,
                }
            if api_path == "openapi/wiki/v1/get_media_info":
                self.assertEqual(body["media_id"], "secret-media-id")
                return {"media_type": 11, "notebook_ext_info": {"notebook_id": "secret-note-id"}}
            if api_path == "openapi/note/v1/get_doc_content":
                self.assertEqual(body, {"note_id": "secret-note-id", "target_content_format": 0})
                return {"content": "Full private note body for downstream analysis."}
            raise AssertionError(api_path)

        with patch.object(ima_openapi, "ima_openapi_request", side_effect=fake_request):
            snapshots = ima_openapi.collect_ima_knowledge_base(
                "ima-knowledge",
                {"window_start": "2026-06-01T00:00:00+00:00", "window_end": "2026-06-02T00:00:00+00:00"},
                "RF",
                {"client_id": "cid", "api_key": "secret"},
            )
        self.assertEqual(len(snapshots), 1)
        payload = json.loads(snapshots[0]["content"])
        self.assertEqual(payload["knowledge_base"]["name"], "Daily Research")
        self.assertEqual(payload["results"][0]["title"], "RF front-end memo")
        self.assertEqual(payload["results"][0]["content"], "Full private note body for downstream analysis.")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-kb-id", serialized)
        self.assertNotIn("secret-media-id", serialized)
        self.assertNotIn("secret-note-id", serialized)
        self.assertNotIn("secret-kb-id", snapshots[0]["source_url"])


if __name__ == "__main__":
    unittest.main()
