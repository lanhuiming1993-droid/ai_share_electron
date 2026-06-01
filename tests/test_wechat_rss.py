from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.wechat_rss import (
    check_werss,
    collect_werss,
    managed_werss_status,
    parse_werss_feed,
    public_werss_config,
    start_managed_werss,
    werss_headers,
)


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>WeRSS</title>
    <item>
      <guid>article-1</guid>
      <title>产业链更新</title>
      <author>研究团队</author>
      <pubDate>Sun, 31 May 2026 11:30:00 +0800</pubDate>
      <link>https://mp.weixin.qq.com/s/article-1</link>
      <description>摘要</description>
      <content:encoded><![CDATA[<p>正文</p>]]></content:encoded>
    </item>
    <item>
      <guid>article-too-old</guid>
      <title>窗口外文章</title>
      <pubDate>Wed, 01 Apr 2026 11:30:00 +0800</pubDate>
      <link>https://mp.weixin.qq.com/s/article-too-old</link>
      <description>旧内容</description>
    </item>
  </channel>
</rss>
"""

ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>atom-1</id>
    <title>Atom 文章</title>
    <updated>2026-05-31T11:30:00+08:00</updated>
    <link rel="alternate" href="https://mp.weixin.qq.com/s/atom-1" />
    <author><name>作者</name></author>
    <content>正文</content>
  </entry>
</feed>
"""


class FakeResponse:
    def __init__(self, text: str, url: str = "http://127.0.0.1:8001/feed/all.rss?limit=100&offset=0") -> None:
        self.text = text
        self.url = url
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.text, url)


class WechatRssTests(unittest.TestCase):
    def test_collect_werss_applies_strict_timestamp_window_and_optional_auth(self) -> None:
        session = FakeSession(RSS_XML)
        with patch("backend.wechat_rss.browser_http_session", return_value=session):
            snapshots = collect_werss(
                {
                    "id": "wechat-mp-rss",
                    "request_config": {
                        "base_url": "http://127.0.0.1:8001",
                        "feed_ids": ["all"],
                        "access_key": "local-ak",
                        "secret_key": "local-sk",
                    },
                },
                {
                    "window_start": "2026-05-01T00:00:00+08:00",
                    "window_end": "2026-06-01T00:00:00+08:00",
                },
            )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["source_url"], "https://mp.weixin.qq.com/s/article-1")
        payload = json.loads(snapshots[0]["content"])
        self.assertEqual(payload["platform"], "werss_external_rss")
        self.assertEqual(payload["article"]["title"], "产业链更新")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "AK-SK local-ak:local-sk")

    def test_parse_werss_feed_supports_atom(self) -> None:
        items = parse_werss_feed(ATOM_XML)
        self.assertEqual(items[0]["id"], "atom-1")
        self.assertEqual(items[0]["link"], "https://mp.weixin.qq.com/s/atom-1")
        self.assertEqual(items[0]["author"], "作者")

    def test_public_config_masks_credentials(self) -> None:
        config = public_werss_config({"access_key": "local-ak", "secret_key": "local-sk"})
        self.assertTrue(config["credentials_configured"])
        self.assertEqual(config["access_key"], "****************")
        self.assertEqual(config["secret_key"], "****************")

    def test_headers_omit_auth_when_credentials_are_empty(self) -> None:
        self.assertNotIn("Authorization", werss_headers({"access_key": "", "secret_key": ""}))

    def test_health_check_accepts_valid_empty_feed(self) -> None:
        session = FakeSession("<rss version=\"2.0\"><channel></channel></rss>")
        with patch("backend.wechat_rss.browser_http_session", return_value=session):
            result = check_werss()
        self.assertEqual(result["status"], "online")
        self.assertIn("抽样读取 0 条文章", result["message"])

    def test_component_status_exposes_qr_login_console_without_requiring_docker(self) -> None:
        session = FakeSession("<rss version=\"2.0\"><channel></channel></rss>")
        with patch("backend.wechat_rss.browser_http_session", return_value=session), patch("backend.wechat_rss.shutil.which", return_value=None):
            result = managed_werss_status()
        self.assertTrue(result["service_online"])
        self.assertTrue(result["rss_online"])
        self.assertFalse(result["docker_available"])
        self.assertEqual(result["wechat_status_url"], "http://127.0.0.1:8001/wechat-status")
        self.assertEqual(result["add_subscription_url"], "http://127.0.0.1:8001/add-subscription")

    def test_managed_start_explains_missing_docker(self) -> None:
        with patch("backend.wechat_rss.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Docker"):
                start_managed_werss()

    def test_managed_start_explains_stopped_docker_engine(self) -> None:
        with patch("backend.wechat_rss.shutil.which", return_value="docker"), patch("backend.wechat_rss.docker_engine_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "引擎未运行"):
                start_managed_werss()


if __name__ == "__main__":
    unittest.main()
