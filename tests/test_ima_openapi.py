from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from backend import ima_openapi


class ImaOpenApiTests(unittest.TestCase):
    def test_user_config_reader_strips_utf8_bom_for_header_safe_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / ".config" / "ima"
            config_dir.mkdir(parents=True)
            (config_dir / "client_id").write_bytes(b"\xef\xbb\xbfcid-from-skill\n")
            with patch.object(Path, "home", return_value=Path(temp_dir)):
                self.assertEqual(ima_openapi.read_user_config("client_id"), "cid-from-skill")

    def test_http_error_surfaces_ima_business_message(self) -> None:
        class FakeResponse:
            status_code = 403
            text = '{"code":200005,"msg":"请求超量，请明日再试","data":{}}'
            reason = "Forbidden"

            def json(self):
                return {"code": 200005, "msg": "请求超量，请明日再试", "data": {}}

        with patch.object(ima_openapi.requests, "post", return_value=FakeResponse()):
            with self.assertRaisesRegex(RuntimeError, "IMA OpenAPI 200005: 请求超量，请明日再试"):
                ima_openapi.ima_openapi_request(
                    "openapi/wiki/v1/search_knowledge_base",
                    {"query": "", "limit": 1},
                    config={"client_id": "cid", "api_key": "secret"},
                )

    def test_status_includes_ima_business_error_message(self) -> None:
        with patch.object(ima_openapi, "list_ima_knowledge_bases", side_effect=RuntimeError("IMA OpenAPI 200005: 请求超量，请明日再试")):
            status = ima_openapi.ima_status({"client_id": "cid", "api_key": "secret"})

        self.assertEqual(status["status"], "offline")
        self.assertIn("请求超量，请明日再试", status["message"])

    def test_browse_ima_knowledge_list_recurses_media_type_99_folders(self) -> None:
        def fake_request(api_path: str, body: dict, timeout: int | None = None, config: dict | None = None) -> dict:
            self.assertEqual(api_path, "openapi/wiki/v1/get_knowledge_list")
            if "folder_id" not in body:
                return {
                    "knowledge_list": [
                        {"media_id": "secret-folder-media-id", "title": "Folder from knowledge_list", "media_type": 99},
                        {"media_id": "secret-root-media-id", "title": "Root document.md", "media_type": 7},
                    ],
                    "is_end": True,
                }
            self.assertEqual(body["folder_id"], "secret-folder-media-id")
            return {
                "knowledge_list": [{"media_id": "secret-nested-media-id", "title": "Nested document.md", "media_type": 7}],
                "is_end": True,
            }

        with patch.object(ima_openapi, "ima_openapi_request", side_effect=fake_request):
            items = ima_openapi.search_or_list_kb_items(
                {"id": "secret-kb-id", "name": "Daily Research", "description": ""},
                "",
                5,
                {"client_id": "cid", "api_key": "secret", "content_fetch_limit": 0},
            )
        self.assertEqual([item["title"] for item in items], ["Root document.md", "Nested document.md"])

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

    def test_docx_to_text_extracts_paragraphs(self) -> None:
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
            <w:p><w:r><w:t>Second</w:t></w:r><w:r><w:t> paragraph</w:t></w:r></w:p>
          </w:body>
        </w:document>
        """
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        self.assertEqual(ima_openapi.docx_to_text(buffer.getvalue()), "First paragraph\nSecond paragraph")

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
