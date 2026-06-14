from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
import unittest


def load_render_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "alphadesk-cloud-report"
        / "scripts"
        / "render_report_pdf.py"
    )
    spec = importlib.util.spec_from_file_location("alphadesk_render_report_pdf", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AlphaDeskRenderReportPdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = load_render_module()

    def test_markdown_blocks_preserve_report_structure(self) -> None:
        blocks = self.script.markdown_blocks(
            "# AlphaDesk\n\n## Core\n\n- first point\n- second point\n\nPlain paragraph."
        )

        self.assertEqual(blocks[0].kind, "heading")
        self.assertEqual(blocks[0].level, 1)
        self.assertEqual(blocks[1].kind, "heading")
        self.assertEqual([block.kind for block in blocks[2:]], ["bullet", "bullet", "paragraph"])

    def test_html_is_converted_to_markdownish_text(self) -> None:
        text = self.script.html_to_markdownish(
            "<html><body><h1>Title</h1><p>Hello <b>world</b></p><ul><li>One</li></ul></body></html>"
        )

        self.assertIn("# Title", text)
        self.assertIn("Hello world", text)
        self.assertIn("- One", text)

    def test_structured_html_preserves_sources_and_information_labels(self) -> None:
        elements = self.script.structured_html_elements(
            """
            <html><body>
              <div class="meta"><span>窗口：近 1 天</span></div>
              <h2>一、AI 算力</h2>
              <div class="card">
                <p>
                  <span class="source-tag source-high">知识星球：长期信源</span>
                  <span class="source-tag">WeRSS：盘前纪要</span>
                </p>
                <ul>
                  <li><span class="fact">事实</span> 光模块订单继续释放。</li>
                  <li><span class="infer">推断</span> 算力链景气度仍在扩散。</li>
                  <li><span class="unverified">待核验</span> 价格传闻需要官方确认。</li>
                </ul>
              </div>
            </body></html>
            """
        )

        self.assertTrue(self.script.has_structured_report_elements(elements))
        card = next(element.card for element in elements if element.kind == "card")
        self.assertEqual(card.sources[0].text, "知识星球：长期信源")
        self.assertTrue(card.sources[0].high)
        self.assertEqual(card.sources[1].text, "WeRSS：盘前纪要")
        self.assertEqual([item.label_kind for item in card.items], ["fact", "infer", "unverified"])
        self.assertIn("光模块订单", card.items[0].text)

    def test_render_pdf_creates_pdf_file_when_reportlab_is_available(self) -> None:
        try:
            import reportlab  # noqa: F401
        except Exception:
            self.skipTest("reportlab is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.pdf"
            result = self.script.render_pdf(
                "# AlphaDesk\n\n## Core conclusion\n\n- PDF delivery works.",
                output,
                title="AlphaDesk Test Report",
                input_format="markdown",
            )

            self.assertEqual(result, output)
            self.assertGreater(output.stat().st_size, 500)
            self.assertEqual(output.read_bytes()[:4], b"%PDF")

    def test_render_pdf_creates_structured_pdf_from_card_html(self) -> None:
        try:
            import reportlab  # noqa: F401
        except Exception:
            self.skipTest("reportlab is not installed")

        html = """
        <html><body>
          <div class="header">
            <h1>近 1 天信源聚合报告</h1>
            <div class="meta"><span>窗口：近 1 天</span><span>主权重信源：知识星球</span></div>
          </div>
          <h2>一、AI 算力基础设施</h2>
          <div class="card">
            <p><span class="source-tag source-high">知识星球</span><span class="source-tag">WeRSS</span></p>
            <ul>
              <li><span class="fact">事实</span> 企业级 SSD 供给偏紧。</li>
              <li><span class="infer">推断</span> 存储链可能进入上行周期。</li>
              <li><span class="unverified">待核验</span> 单一传闻需要交叉验证。</li>
            </ul>
          </div>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "structured.pdf"
            result = self.script.render_pdf(html, output, title="AlphaDesk Structured", input_format="html")

            self.assertEqual(result, output)
            self.assertGreater(output.stat().st_size, 500)
            self.assertEqual(output.read_bytes()[:4], b"%PDF")

    def test_render_pdf_splits_tall_structured_cards(self) -> None:
        try:
            import reportlab  # noqa: F401
        except Exception:
            self.skipTest("reportlab is not installed")

        items = "\n".join(
            f"<li><span class='fact'>Fact</span> Evidence item {index}: "
            f"{'long structured evidence text ' * 12}</li>"
            for index in range(1, 45)
        )
        html = f"""
        <html><body>
          <div class="header">
            <h1>AlphaDesk Long Structured Report</h1>
            <div class="meta"><span>Window: 30 days</span></div>
          </div>
          <h2>Evidence Summary</h2>
          <div class="card">
            <p><span class="source-tag source-high">ZSXQ</span><span class="source-tag">WeRSS</span></p>
            <ul>{items}</ul>
          </div>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "long-structured.pdf"
            result = self.script.render_pdf(html, output, title="AlphaDesk Long Structured", input_format="html")

            self.assertEqual(result, output)
            self.assertGreater(output.stat().st_size, 500)
            self.assertEqual(output.read_bytes()[:4], b"%PDF")


if __name__ == "__main__":
    unittest.main()
