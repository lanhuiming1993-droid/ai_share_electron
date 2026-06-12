from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.zsxq_mcp_source import _parse_event_stream, _tool_result_payload, collect_zsxq_mcp, normalize_zsxq_group_ids


class FakeResponse:
    def __init__(self, content: bytes, content_type: str = "text/event-stream") -> None:
        self.content = content
        self.headers = {"content-type": content_type}


class ZsxqMcpSourceTests(unittest.TestCase):
    def test_parse_event_stream_decodes_json_rpc_data_events(self) -> None:
        events = _parse_event_stream(
            FakeResponse(
                b'event: message\n'
                b'data: {"jsonrpc":"2.0","id":"tool-call","result":{"content":[{"type":"text","text":"ok"}]}}\n\n'
            )
        )
        self.assertEqual(events[0]["id"], "tool-call")

    def test_parse_event_stream_repairs_unprefixed_multiline_text(self) -> None:
        response = FakeResponse(
            (
                'event: message\n'
                'data: {"jsonrpc":"2.0","id":"tool-call","result":{"content":[{"type":"text","text":"{\\n  \\"success\\": true,\\n  \\"topics_brief\\": [{\\"topic_id\\": \\"1\\", \\"content\\": \\"第一行\n'
                '第二行\\"}]\\n}"}]}}\n\n'
            ).encode("utf-8")
        )
        events = _parse_event_stream(response)
        payload = _tool_result_payload(events[0]["result"])
        self.assertEqual(payload["topics_brief"][0]["content"], "第一行\n第二行")

    def test_group_ids_are_limited_to_long_term_source(self) -> None:
        self.assertEqual(normalize_zsxq_group_ids(["88882281482852", "28888222124181"]), ["28888222124181"])
        self.assertEqual(normalize_zsxq_group_ids(["88882281482852"]), ["28888222124181"])

    def test_collect_pages_until_window_boundary(self) -> None:
        page_one = {
            "success": True,
            "topics_brief": [
                {
                    "topic_id": "topic-1",
                    "title": "午盘",
                    "type": "talk",
                    "create_time": "2026-06-12T10:33:08.636+0800",
                    "content": "风险偏好回升",
                    "owner": {"user_id": "u1", "name": "橙子不糊涂"},
                    "group": {"group_id": "28888222124181", "name": "橙子不糊涂的科技花园"},
                    "counts": {"likes": 1, "comments": 0},
                }
            ],
            "has_more": True,
            "next_end_time": "2026-06-12T10:33:08.636+0800",
        }
        page_two = {
            "success": True,
            "topics_brief": [
                {
                    "topic_id": "old-topic",
                    "title": "旧内容",
                    "type": "talk",
                    "create_time": "2026-06-10T10:33:08.636+0800",
                    "content": "窗口外",
                    "owner": {"user_id": "u1", "name": "橙子不糊涂"},
                    "group": {"group_id": "28888222124181", "name": "橙子不糊涂的科技花园"},
                }
            ],
            "has_more": False,
        }
        calls = []

        def fake_tool_call(_config, tool_name, arguments):
            self.assertEqual(tool_name, "get_group_topics")
            calls.append(arguments)
            return page_one if len(calls) == 1 else page_two

        channel = {
            "id": "zsxq",
            "group_ids": ["88882281482852", "28888222124181"],
            "request_config": {
                "mcp_url": "https://mcp.example.test/topic/mcp?api_key=secret",
                "page_limit": 1,
                "max_pages": 5,
            },
        }
        window = {
            "window_start": "2026-06-12T00:00:00+08:00",
            "window_end": "2026-06-12T23:59:59+08:00",
        }
        with patch("backend.zsxq_mcp_source.mcp_tool_call", side_effect=fake_tool_call):
            snapshots = collect_zsxq_mcp(channel, window, "半导体")

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(calls[0]["group_id"], "28888222124181")
        self.assertEqual(calls[1]["end_time"], "2026-06-12T10:33:08.636+0800")
        self.assertEqual(snapshots[0]["source_url"], "zsxq://group/28888222124181/topic/topic-1")
        payload = json.loads(snapshots[0]["content"])
        self.assertEqual(payload["platform"], "zsxq_mcp")
        self.assertEqual(payload["topic"]["content"], "风险偏好回升")


if __name__ == "__main__":
    unittest.main()
