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


if __name__ == "__main__":
    unittest.main()
