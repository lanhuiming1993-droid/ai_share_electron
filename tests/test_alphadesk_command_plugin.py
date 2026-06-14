from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load_plugin_module():
    plugin_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "plugins"
        / "alphadesk-command"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("alphadesk_command_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_event(text: str, platform: str = "weixin"):
    return SimpleNamespace(
        text=text,
        message_id="msg-1",
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            user_id="user-1",
            user_name="tester",
            chat_id="chat-1",
            chat_type="dm",
        ),
    )


class AlphaDeskCommandPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "alphadesk-command.audit.jsonl"
        self.state_path = Path(self.tmp.name) / "alphadesk-command.state.json"
        self.env_patch = patch.dict(
            os.environ,
            {
                "ALPHADESK_COMMAND_AUDIT_PATH": str(self.audit_path),
                "ALPHADESK_COMMAND_STATE_PATH": str(self.state_path),
            },
        )
        self.env_patch.start()
        self.plugin = load_plugin_module()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_audits_exact_weixin_report_command_without_rewrite(self) -> None:
        result = self.plugin._pre_gateway_dispatch(make_event("采集近30天数据并生成报告"))

        self.assertEqual(result, {"action": "rewrite", "text": "/alphadesk-report --days 30"})
        payload = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["platform"], "weixin")
        self.assertEqual(payload["intent"], "report")
        self.assertEqual(payload["days"], 30)
        self.assertEqual(payload["query"], "")

    def test_audits_lightclawbot_command_with_spacing_and_punctuation(self) -> None:
        result = self.plugin._pre_gateway_dispatch(
            make_event("请帮我采集近 30 天的数据，并生成分析报告。", "lightclawbot")
        )

        self.assertEqual(result, {"action": "rewrite", "text": "/alphadesk-report --days 30"})
        payload = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["platform"], "lightclawbot")
        self.assertEqual(payload["intent"], "report")
        self.assertEqual(payload["days"], 30)

    def test_clamps_supported_report_window_to_thirty_days(self) -> None:
        result = self.plugin._pre_gateway_dispatch(make_event("采集近99天数据并生成报告"))

        self.assertEqual(result, {"action": "rewrite", "text": "/alphadesk-report --days 30"})
        payload = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["days"], 30)

    def test_writes_audit_record_when_report_command_matches(self) -> None:
        self.plugin._pre_gateway_dispatch(make_event("采集近30天数据并生成报告"))

        payload = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["platform"], "weixin")
        self.assertEqual(payload["command"], "alphadesk-report")
        self.assertEqual(payload["intent"], "report")
        self.assertEqual(payload["days"], 30)
        self.assertEqual(payload["content_preview"], "采集近30天数据并生成报告")
        self.assertEqual(payload["chat_id"], "chat-1")

    def test_audits_stock_analysis_request_as_alphadesk_base_intent(self) -> None:
        result = self.plugin._pre_gateway_dispatch(make_event("分析一下长光华芯"))

        self.assertEqual(result, {"action": "rewrite", "text": "/alphadesk-report --days 30 --query '长光华芯'"})
        payload = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["platform"], "weixin")
        self.assertEqual(payload["intent"], "analysis")
        self.assertEqual(payload["days"], 30)
        self.assertEqual(payload["query"], "长光华芯")

    def test_audits_suffix_analysis_request_with_market_hint(self) -> None:
        result = self.plugin._pre_gateway_dispatch(make_event("A股机器人板块怎么看"))

        self.assertEqual(result, {"action": "rewrite", "text": "/alphadesk-report --days 30 --query 'A股机器人板块'"})
        payload = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["intent"], "analysis")
        self.assertEqual(payload["query"], "A股机器人板块")

    def test_ignores_non_gateway_platform_and_unrelated_text(self) -> None:
        self.assertIsNone(self.plugin._pre_gateway_dispatch(make_event("采集近30天数据并生成报告", "cli")))
        self.assertIsNone(self.plugin._pre_gateway_dispatch(make_event("你有什么技能", "weixin")))

    def test_ignores_hermes_runtime_and_tooling_meta_questions(self) -> None:
        meta_questions = [
            "你在这里进行分析时，调用了哪些Skill/插件/工具？",
            "为什么后台日志还是走 deepseek 供应商，而不是 jojo？",
            "Hermes 的 config.yaml 现在配置的是哪个模型？",
            "这个回答用了哪些 MCP 或 Skill？",
        ]

        for question in meta_questions:
            with self.subTest(question=question):
                self.assertIsNone(self.plugin._classify_alphadesk_request(question))
                self.assertIsNone(self.plugin._pre_gateway_dispatch(make_event(question, "weixin")))

    def test_rewrites_werss_management_requests_before_alphadesk_analysis(self) -> None:
        cases = [
            ("公众号订阅状态", "/alphadesk-werss status"),
            ("查看现有订阅公众号", "/alphadesk-werss status"),
            ("查看订阅公众号", "/alphadesk-werss status"),
            ("搜索公众号订阅 半导体", "/alphadesk-werss search '半导体'"),
            ("新增公众号订阅 2", "/alphadesk-werss add 2"),
            ("移除公众号订阅 半导体观察", "/alphadesk-werss remove '半导体观察'"),
            ("补采公众号 全部", "/alphadesk-werss backfill '全部'"),
            ("微信公众号授权", "/alphadesk-werss login"),
        ]

        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(self.plugin._pre_gateway_dispatch(make_event(message)), {"action": "rewrite", "text": expected})

    def test_run_werss_calls_source_auth_script_and_returns_output(self) -> None:
        async def fake_run(command: list[str], **kwargs):
            self.assertEqual(command[-3:], ["werss-search", "--query", "半导体"])
            return 0, "WeRSS 搜索“半导体”找到 1 个候选：\n1. 半导体观察 | id=mp-1\n"

        script_path = Path(self.tmp.name) / "source_auth.py"
        script_path.write_text("# test", encoding="utf-8")
        with (
            patch.object(self.plugin, "SOURCE_AUTH_SCRIPT", script_path),
            patch.object(self.plugin, "_run_subprocess", side_effect=fake_run),
        ):
            result = __import__("asyncio").run(self.plugin._run_werss("search 半导体"))

        self.assertIn("半导体观察", result)

    def test_run_werss_keeps_qr_media_on_authorization_failure(self) -> None:
        async def fake_run(command: list[str], **kwargs):
            self.assertEqual(command[-1], "werss-status")
            return 2, "WeRSS 微信授权不可用，已生成二维码。\nMEDIA:/home/ubuntu/.hermes/alphadesk-auth/werss-login.png\n"

        script_path = Path(self.tmp.name) / "source_auth.py"
        script_path.write_text("# test", encoding="utf-8")
        with (
            patch.object(self.plugin, "SOURCE_AUTH_SCRIPT", script_path),
            patch.object(self.plugin, "_run_subprocess", side_effect=fake_run),
        ):
            result = __import__("asyncio").run(self.plugin._run_werss("status"))

        self.assertIn("MEDIA:/home/ubuntu/.hermes/alphadesk-auth/werss-login.png", result)

    def test_extracts_command_options_with_query(self) -> None:
        days, query = self.plugin._extract_command_options('--days 7 --query "长光华芯"')

        self.assertEqual(days, 7)
        self.assertEqual(query, "长光华芯")

    def test_command_text_for_classification_quotes_query(self) -> None:
        text = self.plugin._command_text_for_classification({"intent": "analysis", "days": 7, "query": "长光 华芯"})

        self.assertEqual(text, "/alphadesk-report --days 7 --query '长光 华芯'")

    def test_subprocess_env_loads_hermes_dotenv_for_external_skills(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text(
            "\n".join(
                [
                    "IWENCAI_BASE_URL=https://openapi.iwencai.com",
                    "IWENCAI_API_KEY='test-key'",
                    "IGNORED_LINE",
                ]
            ),
            encoding="utf-8",
        )

        with patch.object(self.plugin, "HERMES_ENV_PATH", env_path):
            env = self.plugin._subprocess_env()

        self.assertEqual(env["IWENCAI_BASE_URL"], "https://openapi.iwencai.com")
        self.assertEqual(env["IWENCAI_API_KEY"], "test-key")

    def test_pre_llm_call_records_alphadesk_session_and_injects_pdf_context(self) -> None:
        result = self.plugin._pre_llm_call(
            platform="lightclawbot",
            session_id="session-1",
            user_message="分析一下卓胜微",
        )

        self.assertIsInstance(result, dict)
        self.assertIn("collect_report.py --days 30 --query \"卓胜微\"", result["context"])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["sessions"]["session-1"]["query"], "卓胜微")
        self.assertTrue(state["sessions"]["session-1"]["require_pdf"])

    def test_transform_llm_output_renders_text_to_pdf_media_reply(self) -> None:
        self.plugin._remember_session_intent(
            "session-2",
            {"intent": "analysis", "days": 30, "query": "卓胜微"},
            "分析一下卓胜微",
            "lightclawbot",
        )
        fake_pdf = Path(self.tmp.name) / "report.pdf"

        def fake_render(response_text: str, state: dict):
            self.assertIn("深度分析", response_text)
            self.assertEqual(state["query"], "卓胜微")
            fake_pdf.write_bytes(b"%PDF-1.4")
            return f"已生成 PDF 版报告，便于阅读和保存。\nMEDIA:{fake_pdf}"

        with patch.object(self.plugin, "_render_response_pdf", side_effect=fake_render):
            result = self.plugin._transform_llm_output(
                platform="lightclawbot",
                session_id="session-2",
                response_text="卓胜微 深度分析正文",
            )

        self.assertEqual(result, f"已生成 PDF 版报告，便于阅读和保存。\nMEDIA:{fake_pdf}")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("session-2", state["sessions"])

    def test_transform_llm_output_leaves_existing_pdf_media_unchanged(self) -> None:
        self.plugin._remember_session_intent(
            "session-3",
            {"intent": "analysis", "days": 30, "query": "卓胜微"},
            "分析一下卓胜微",
            "lightclawbot",
        )

        result = self.plugin._transform_llm_output(
            platform="lightclawbot",
            session_id="session-3",
            response_text="已生成。\nMEDIA:/tmp/report.pdf",
        )

        self.assertIsNone(result)

    def test_extract_final_response_from_run_agent_stdout(self) -> None:
        stdout = "logs\n🎯 FINAL RESPONSE:\n------------------------------\n<html>ok</html>\n\n👋 Agent execution completed!"

        self.assertEqual(self.plugin._extract_final_response(stdout), "<html>ok</html>")

    def test_valid_structured_html_requires_report_classes(self) -> None:
        valid = """
        ```html
        <div class="container">
          <div class="card">
            <span class="source-tag source-high">AlphaDesk</span>
            <ul>
              <li><span class="fact">事实</span> A</li>
              <li><span class="infer">推断</span> B</li>
              <li><span class="unverified">待核验</span> C</li>
            </ul>
          </div>
        </div>
        ```
        """

        self.assertIsNotNone(self.plugin._valid_structured_html(valid))
        self.assertIsNone(self.plugin._valid_structured_html("尚未完成 HTML 文件保存与 PDF 渲染。"))
        process_text_with_tag_examples = """
        已加载 AlphaDesk 三信源报告生成规范。
        顶层包含 `<div class="container">`，每个主题使用 `<div class="card">`。
        每个 card 开头包含 `<span class="source-tag">`，主证据使用 `<span class="source-tag source-high">`。
        每条要点标注 `<span class="fact">事实</span>`、`<span class="infer">推断</span>` 或
        `<span class="unverified">待核验</span>`。
        由于工具调用次数已达上限，我无法继续执行保存 HTML 文件或调用 render_report_pdf.py。
        """
        self.assertIsNone(self.plugin._valid_structured_html(process_text_with_tag_examples))

    def test_analysis_prompt_strips_embedded_report_requirements(self) -> None:
        evidence = """AlphaDesk 三信源证据包已就绪。
Job: job-1
Selected evidence:
[1] zsxq | 2026-06-14 | title
content

Report requirements:
- 默认交付 PDF：先把完整报告保存为 /tmp/alphadesk-report-job-1.html
- 最终聊天回复必须发送 MEDIA:/absolute/path/to/report.pdf
"""

        prompt = self.plugin._analysis_prompt(evidence, days=30, query="卓胜微")

        self.assertIn("Selected evidence:", prompt)
        self.assertIn("content", prompt)
        self.assertNotIn("默认交付 PDF", prompt)
        self.assertNotIn("MEDIA:/absolute/path", prompt)

    def test_analysis_prompt_keeps_cross_validation_after_embedded_requirements(self) -> None:
        evidence = """AlphaDesk 三信源证据包已就绪。
Selected evidence:

Report requirements:
- 不应进入最终分析提示。

# Hermes Cross-Validation Evidence
## announcement-search
query=卓胜微 最新公告
exit=0
result_status=ok_with_output
output=卓胜微：第三届董事会第十八次会议决议公告
"""

        prompt = self.plugin._analysis_prompt(evidence, days=1, query="卓胜微")

        self.assertIn("# Hermes Cross-Validation Evidence", prompt)
        self.assertIn("result_status=ok_with_output", prompt)
        self.assertIn("第三届董事会第十八次会议决议公告", prompt)
        self.assertNotIn("不应进入最终分析提示", prompt)

    def test_run_report_collects_analyzes_and_returns_pdf_media(self) -> None:
        async def fake_collect(days: int, query: str):
            self.assertEqual(days, 7)
            self.assertEqual(query, "卓胜微")
            return 0, "evidence"

        async def fake_generate(evidence: str, *, days: int, query: str):
            self.assertIn("evidence", evidence)
            self.assertIn("# Hermes Cross-Validation Evidence", evidence)
            return 0, "<div class='container'><div class='card'><span class='source-tag'>AlphaDesk</span><ul><li><span class='fact'>事实</span> ok</li></ul></div></div>"

        async def fake_render(text: str, *, days: int, query: str, is_html: bool):
            self.assertTrue(is_html)
            return "已生成 PDF 版报告，便于阅读和保存。\nMEDIA:/tmp/report.pdf"

        async def fake_cross_validation(query: str):
            self.assertEqual(query, "卓胜微")
            return "# Hermes Cross-Validation Evidence\n## announcement-search\nresult_status=ok_with_output"

        with (
            patch.object(self.plugin, "_collect_evidence", side_effect=fake_collect),
            patch.object(self.plugin, "_collect_cross_validation_evidence", side_effect=fake_cross_validation),
            patch.object(self.plugin, "_generate_html_with_hermes", side_effect=fake_generate),
            patch.object(self.plugin, "_render_text_to_pdf", side_effect=fake_render),
        ):
            result = __import__("asyncio").run(self.plugin._run_report("--days 7 --query 卓胜微"))

        self.assertEqual(result, "已生成 PDF 版报告，便于阅读和保存。\nMEDIA:/tmp/report.pdf")

    def test_run_report_falls_back_to_structured_html_when_model_returns_process_text(self) -> None:
        evidence = """AlphaDesk 三信源证据包已就绪。
Job: job-1
Status: partial_completed
Lookback days: 30
Research query: 卓胜微
Source runs:
- wechat-mp-rss: deduplicated
- ima-knowledge: failed; note=IMA OpenAPI 200005: 请求超量
- zsxq: completed; used=91

Snapshot coverage:
- wechat-mp-rss: 0 snapshots attached
- ima-knowledge: 0 snapshots attached
- zsxq: 91 snapshots attached

Selected evidence:
[1] zsxq | 2026-06-14 | 知识星球 | 半导体设备材料去日化
半导体材料、设备国产化线索升温，但未直接指向卓胜微。
"""

        async def fake_collect(days: int, query: str):
            return 0, evidence

        async def fake_generate(evidence_text: str, *, days: int, query: str):
            return 0, "已加载并核对 AlphaDesk 报告生成规范，但尚未完成 HTML 文件保存与 PDF 渲染。"

        async def fake_render(text: str, *, days: int, query: str, is_html: bool):
            self.assertTrue(is_html)
            self.assertIn('class="container"', text)
            self.assertIn('class="card"', text)
            self.assertIn("逐信源状态", text)
            self.assertIn("精选证据摘要", text)
            self.assertIn("Hermes 模型本轮未返回合格结构化 HTML", text)
            self.assertNotIn("尚未完成 HTML 文件保存", text)
            return "已生成 PDF 版报告，便于阅读和保存。\nMEDIA:/tmp/fallback.pdf"

        async def fake_cross_validation(query: str):
            self.assertEqual(query, "卓胜微")
            return "# Hermes Cross-Validation Evidence\n## announcement-search\nresult_status=ok_with_output"

        with (
            patch.object(self.plugin, "_collect_evidence", side_effect=fake_collect),
            patch.object(self.plugin, "_collect_cross_validation_evidence", side_effect=fake_cross_validation),
            patch.object(self.plugin, "_generate_html_with_hermes", side_effect=fake_generate),
            patch.object(self.plugin, "_render_text_to_pdf", side_effect=fake_render),
        ):
            result = __import__("asyncio").run(self.plugin._run_report("--days 30 --query 卓胜微"))

        self.assertEqual(result, "已生成 PDF 版报告，便于阅读和保存。\nMEDIA:/tmp/fallback.pdf")


if __name__ == "__main__":
    unittest.main()
