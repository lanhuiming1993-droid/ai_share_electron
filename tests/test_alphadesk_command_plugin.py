from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


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
        source=SimpleNamespace(platform=SimpleNamespace(value=platform)),
    )


class AlphaDeskCommandPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = load_plugin_module()

    def test_rewrites_exact_weixin_report_command(self) -> None:
        result = self.plugin._pre_gateway_dispatch(make_event("采集近30天数据并生成报告"))

        self.assertEqual(
            result,
            {
                "action": "rewrite",
                "text": "/alphadesk-report --days 30 --original 采集近30天数据并生成报告",
            },
        )

    def test_rewrites_lightclawbot_command_with_spacing_and_punctuation(self) -> None:
        result = self.plugin._pre_gateway_dispatch(
            make_event("请帮我采集近 30 天的数据，并生成分析报告。", "lightclawbot")
        )

        self.assertEqual(result["action"], "rewrite")
        self.assertIn("/alphadesk-report --days 30", result["text"])

    def test_clamps_supported_report_window_to_thirty_days(self) -> None:
        result = self.plugin._pre_gateway_dispatch(make_event("采集近99天数据并生成报告"))

        self.assertIn("--days 30", result["text"])

    def test_ignores_non_gateway_platform_and_unrelated_text(self) -> None:
        self.assertIsNone(self.plugin._pre_gateway_dispatch(make_event("采集近30天数据并生成报告", "cli")))
        self.assertIsNone(self.plugin._pre_gateway_dispatch(make_event("你有什么技能", "weixin")))


if __name__ == "__main__":
    unittest.main()
