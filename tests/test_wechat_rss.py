from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.wechat_rss import (
    add_werss_subscription,
    check_werss,
    collect_werss,
    delete_werss_subscription,
    fetch_werss_qr_image,
    fetch_werss_subscriptions,
    managed_werss_status,
    parse_werss_feed,
    public_werss_config,
    search_werss_public_accounts,
    start_managed_werss,
    start_werss_wechat_login,
    werss_wechat_login_status,
    werss_headers,
)


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>WeRSS</title>
    <item>
      <id>3865156629-article-1</id>
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
    def __init__(self, text: str, url: str = "http://127.0.0.1:8001/feed/all.rss?limit=100&offset=0", json_payload=None, content_type: str = "application/xml") -> None:
        self.text = text
        self.url = url
        self.status_code = 200
        self.json_payload = json_payload
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.json_payload


class FakeSession:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.text, url)


def rss_xml(entries: list[tuple[str, str, str]]) -> str:
    items = "".join(
        f"""
        <item>
          <guid>{article_id}</guid>
          <title>{article_id}</title>
          <pubDate>{published_at}</pubDate>
          <link>https://mp.weixin.qq.com/s/{article_id}</link>
          <description>{article_id}</description>
          <content:encoded>{content}</content:encoded>
        </item>
        """
        for article_id, published_at, content in entries
    )
    return f'<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>{items}</channel></rss>'


class PagedFeedSession:
    def __init__(self, pages: dict[int, str]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        offset = int(kwargs.get("params", {}).get("offset", 0))
        return FakeResponse(self.pages.get(offset, rss_xml([])), url)


class AdaptiveFeedSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        limit = int(kwargs.get("params", {}).get("limit", 0))
        if limit > 5:
            return FakeResponse("x" * (8 * 1024 * 1024 + 1), url)
        return FakeResponse(rss_xml([("article-1", "Sun, 31 May 2026 11:30:00 +0800", "full text")]), url)


class WerssApiSession:
    def __init__(self, persistent_authorization: bool = False, qr_login_status: bool = True) -> None:
        self.calls: list[dict] = []
        self.persistent_authorization = persistent_authorization
        self.qr_login_status = qr_login_status
        self.qr_generated = False

    def post(self, url: str, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        if url.endswith("/auth/login"):
            payload = {"code": 0, "data": {"access_token": "admin-token"}}
        elif url.endswith("/mps"):
            payload = {"code": 0, "data": {"id": "MP_WXS_1", "mp_name": kwargs["json"]["mp_name"], "status": 1}}
        else:
            payload = {"code": 0, "data": {}}
        return FakeResponse("", url, payload)

    def get(self, url: str, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        if url.endswith("/auth/qr/code"):
            self.qr_generated = True
            payload = {"code": 0, "data": {"code": "/static/wx_qrcode.png?t=1", "is_exists": True}}
        elif url.endswith("/auth/qr/status"):
            payload = {"code": 0, "data": {"login_status": self.persistent_authorization or (self.qr_login_status and self.qr_generated), "qr_code": self.qr_generated}}
        elif "/mps/search/" in url:
            payload = {
                "code": 0,
                "data": {
                    "list": [{"fakeid": "ZmFrZS1pZA==", "nickname": "产业研究", "alias": "industry-research"}],
                    "total": 1,
                },
            } if self.persistent_authorization else {"code": 50001, "message": "请重新扫码授权"}
        elif url.endswith("/mps"):
            payload = {"code": 0, "data": {"list": [{"id": "MP_WXS_1", "mp_name": "产业研究", "status": 1}], "total": 1}}
        else:
            payload = {"code": 0, "data": {}}
        return FakeResponse("", url, payload)

    def delete(self, url: str, **kwargs):
        self.calls.append({"method": "DELETE", "url": url, **kwargs})
        return FakeResponse("", url, {"code": 0, "data": None})


class WechatRssTests(unittest.TestCase):
    def test_managed_sidecar_enables_rate_limited_full_text_collection(self) -> None:
        compose = (Path(__file__).parents[1] / "integrations" / "werss" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('GATHER.CONTENT: "True"', compose)
        self.assertIn('GATHER.CONTENT_AUTO_CHECK: "True"', compose)
        self.assertIn('GATHER.CONTENT_AUTO_INTERVAL: "15"', compose)
        self.assertIn('RSS_FULL_CONTEXT: "True"', compose)

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

    def test_collect_werss_expands_all_feed_and_preserves_account_attribution(self) -> None:
        session = FakeSession(RSS_XML)
        with patch("backend.wechat_rss.browser_http_session", return_value=session), patch(
            "backend.wechat_rss.fetch_werss_subscriptions",
            return_value=[{"id": "MP_WXS_3865156629", "name": "调研纪要", "enabled": True}],
        ):
            snapshots = collect_werss(
                {"id": "wechat-mp-rss", "request_config": {"feed_ids": ["all"]}},
                {"window_start": "2026-05-01T00:00:00+08:00", "window_end": "2026-06-01T00:00:00+08:00"},
            )
        payload = json.loads(snapshots[0]["content"])
        self.assertIn("/feed/MP_WXS_3865156629.rss", session.calls[0]["url"])
        self.assertEqual(payload["source_account"], {"id": "MP_WXS_3865156629", "name": "调研纪要"})
        self.assertEqual(payload["article"]["source_account_name"], "调研纪要")

    def test_collect_werss_pages_until_strict_window_boundary(self) -> None:
        recent = [(f"article-{index}", "Sun, 31 May 2026 11:30:00 +0800", "full text") for index in range(10)]
        boundary = [
            ("article-10", "Wed, 20 May 2026 11:30:00 +0800", "full text"),
            ("article-11", "Tue, 19 May 2026 11:30:00 +0800", "full text"),
            ("article-old", "Wed, 01 Apr 2026 11:30:00 +0800", "old"),
        ]
        session = PagedFeedSession({0: rss_xml(recent), 10: rss_xml(boundary)})
        with patch("backend.wechat_rss.browser_http_session", return_value=session):
            snapshots = collect_werss(
                {"id": "wechat-mp-rss", "request_config": {"max_items_per_feed": 50}},
                {"window_start": "2026-05-01T00:00:00+08:00", "window_end": "2026-06-01T00:00:00+08:00"},
            )
        self.assertEqual(len(snapshots), 12)
        self.assertEqual([call["params"]["offset"] for call in session.calls], [0, 10])

    def test_collect_werss_reduces_page_size_when_full_text_page_exceeds_limit(self) -> None:
        session = AdaptiveFeedSession()
        with patch("backend.wechat_rss.browser_http_session", return_value=session):
            snapshots = collect_werss(
                {"id": "wechat-mp-rss", "request_config": {"max_items_per_feed": 50}},
                {"window_start": "2026-05-01T00:00:00+08:00", "window_end": "2026-06-01T00:00:00+08:00"},
            )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual([call["params"]["limit"] for call in session.calls], [10, 5])

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
        self.assertEqual(config["admin_password"], "****************")

    def test_public_config_hides_compose_internal_base_url(self) -> None:
        config = public_werss_config({"base_url": "http://werss:8001"})
        self.assertEqual(config["base_url"], "")
        self.assertEqual(config["management_url"], "")

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
        with patch("backend.wechat_rss.browser_http_session", return_value=session), patch("backend.wechat_rss.fetch_werss_subscriptions", return_value=[{"id": "MP_WXS_1", "name": "产业研究"}]), patch("backend.wechat_rss.shutil.which", return_value=None):
            result = managed_werss_status()
        self.assertTrue(result["service_online"])
        self.assertTrue(result["rss_online"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["subscription_count"], 1)
        self.assertFalse(result["docker_available"])
        self.assertEqual(result["wechat_status_url"], "http://127.0.0.1:8001/wechat-status")
        self.assertEqual(result["add_subscription_url"], "http://127.0.0.1:8001/add-subscription")

    def test_qr_login_and_subscription_sync_use_managed_admin_session(self) -> None:
        session = WerssApiSession()
        config = {"base_url": "http://127.0.0.1:8123"}
        qr = start_werss_wechat_login(config, session=session)
        status = werss_wechat_login_status(config, session=session)
        subscriptions = fetch_werss_subscriptions(config, session=session)
        self.assertEqual(qr["qr_image_url"], "http://127.0.0.1:8123/static/wx_qrcode.png?t=1")
        self.assertTrue(status["authorized"])
        self.assertEqual(subscriptions[0]["name"], "产业研究")
        login_call = next(call for call in session.calls if call["method"] == "POST")
        self.assertEqual(login_call["data"]["username"], "admin")
        self.assertEqual(login_call["data"]["password"], "admin@123")

    def test_qr_image_is_fetched_from_werss_for_same_origin_proxy(self) -> None:
        session = FakeSession("png-bytes")
        with patch.object(session, "get", return_value=FakeResponse("png-bytes", content_type="image/png")):
            content, content_type = fetch_werss_qr_image({"base_url": "http://127.0.0.1:8123"}, session=session)
        self.assertEqual(content, b"png-bytes")
        self.assertEqual(content_type, "image/png")

    def test_qr_image_proxy_retries_while_werss_generates_the_file(self) -> None:
        missing = FakeResponse("missing", content_type="text/plain")
        missing.status_code = 404
        ready = FakeResponse("png-bytes", content_type="image/png")
        session = FakeSession("png-bytes")
        with patch.object(session, "get", side_effect=[missing, ready]) as get, patch("backend.wechat_rss.time.sleep") as sleep:
            content, content_type = fetch_werss_qr_image({"base_url": "http://127.0.0.1:8126"}, session=session)
        self.assertEqual(content, b"png-bytes")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_persistent_authorization_avoids_replacing_successful_login_with_new_qr_code(self) -> None:
        session = WerssApiSession(persistent_authorization=True, qr_login_status=False)
        config = {"base_url": "http://127.0.0.1:8124"}
        qr = start_werss_wechat_login(config, session=session)
        status = werss_wechat_login_status(config, session=session)
        self.assertTrue(qr["authorized"])
        self.assertEqual(qr["qr_image_url"], "")
        self.assertTrue(status["authorized"])
        self.assertFalse(any(call["url"].endswith("/auth/qr/code") for call in session.calls))

    def test_search_add_and_delete_subscription_are_proxied_through_werss(self) -> None:
        session = WerssApiSession(persistent_authorization=True)
        config = {"base_url": "http://127.0.0.1:8125"}
        results = search_werss_public_accounts(config, "产业", session=session)
        subscription = add_werss_subscription(config, results[0], session=session)
        removed = delete_werss_subscription(config, subscription["id"], session=session)
        self.assertEqual(results[0]["name"], "产业研究")
        self.assertEqual(subscription["name"], "产业研究")
        self.assertEqual(removed["id"], "MP_WXS_1")
        add_call = next(call for call in session.calls if call["method"] == "POST" and call["url"].endswith("/mps"))
        self.assertEqual(add_call["json"]["mp_id"], "ZmFrZS1pZA==")
        delete_call = next(call for call in session.calls if call["method"] == "DELETE")
        self.assertTrue(delete_call["url"].endswith("/mps/MP_WXS_1"))

    def test_managed_start_explains_missing_docker(self) -> None:
        with patch("backend.wechat_rss.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Docker"):
                start_managed_werss()

    def test_managed_start_explains_stopped_docker_engine(self) -> None:
        with patch("backend.wechat_rss.shutil.which", return_value="docker"), patch("backend.wechat_rss.docker_engine_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "引擎未运行"):
                start_managed_werss()

    def test_compose_runtime_disables_nested_docker_start(self) -> None:
        with patch("backend.wechat_rss.WERSS_RUNTIME_MODE", "compose"):
            with self.assertRaisesRegex(RuntimeError, "Docker Compose"):
                start_managed_werss()


if __name__ == "__main__":
    unittest.main()
