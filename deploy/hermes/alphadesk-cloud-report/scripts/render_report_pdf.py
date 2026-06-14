#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_OUTPUT_DIR = Path.home() / ".hermes" / "alphadesk-reports"
DEFAULT_TITLE = "AlphaDesk Report"
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
)
SYMBOL_REPLACEMENTS = {
    "\ufeff": "",
    "\u200b": "",
    "\ufe0f": "",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u2022": "-",
    "\u26a0": "[warning]",
    "\U0001f4ca": "",
    "\U0001f4c8": "",
    "\U0001f4c9": "",
}


@dataclass
class Block:
    kind: str
    text: str
    level: int = 0


class HtmlToMarkdownish(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._list_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in {"h1", "h2", "h3"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "blockquote", "tr"}:
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n" + "  " * self._list_depth + "- ")
        elif tag in {"ul", "ol"}:
            self._list_depth += 1
            self.parts.append("\n")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"h1", "h2", "h3", "p", "div", "section", "article", "blockquote", "li", "tr"}:
            self.parts.append("\n")
        elif tag in {"ul", "ol"}:
            self._list_depth = max(0, self._list_depth - 1)
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def clean_text(value: str) -> str:
    text = str(value or "")
    for source, target in SYMBOL_REPLACEMENTS.items():
        text = text.replace(source, target)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"<(?:html|body|h[1-6]|p|div|section|article|ul|ol|li|br)\b", text, flags=re.I))


def html_to_markdownish(html: str) -> str:
    parser = HtmlToMarkdownish()
    parser.feed(html)
    parser.close()
    return clean_text(parser.text())


def markdown_blocks(text: str) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(Block("paragraph", " ".join(part.strip() for part in paragraph if part.strip())))
            paragraph.clear()

    def flush_code() -> None:
        if code_lines:
            blocks.append(Block("code", "\n".join(code_lines).strip()))
            code_lines.clear()

    for raw_line in clean_text(text).splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_paragraph()
            blocks.append(Block("rule", ""))
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            blocks.append(Block("heading", heading.group(2).strip(), len(heading.group(1))))
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            flush_paragraph()
            blocks.append(Block("bullet", bullet.group(1).strip()))
            continue
        ordered = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
        if ordered:
            flush_paragraph()
            blocks.append(Block("bullet", f"{ordered.group(1)}. {ordered.group(2).strip()}"))
            continue
        quote = re.match(r"^>\s*(.+)$", stripped)
        if quote:
            flush_paragraph()
            blocks.append(Block("quote", quote.group(1).strip()))
            continue
        paragraph.append(stripped)
    flush_paragraph()
    flush_code()
    return blocks


def safe_filename(value: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value or DEFAULT_TITLE).strip(" ._")
    stem = re.sub(r"\s+", "-", stem)[:80].strip("-")
    return stem or "alphadesk-report"


def find_font() -> Path | None:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def register_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    font_path = find_font()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("AlphaDeskCJK", str(font_path), subfontIndex=0))
            return "AlphaDeskCJK"
        except TypeError:
            pdfmetrics.registerFont(TTFont("AlphaDeskCJK", str(font_path)))
            return "AlphaDeskCJK"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception:
        return "Helvetica"


def paragraph_markup(text: str) -> str:
    marked = escape(clean_text(text)).replace("\n", "<br/>")
    marked = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", marked)
    marked = re.sub(r"`([^`]+)`", r"\1", marked)
    return marked or " "


def render_pdf(report_text: str, output_path: Path, title: str = DEFAULT_TITLE, input_format: str = "auto") -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    text = clean_text(report_text)
    if input_format == "html" or (input_format == "auto" and looks_like_html(text)):
        text = html_to_markdownish(text)

    font_name = register_font()
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "AlphaBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor("#1f2933"),
        spaceAfter=6,
    )
    title_style = ParagraphStyle(
        "AlphaTitle",
        parent=base,
        fontSize=18,
        leading=24,
        textColor=colors.HexColor("#111827"),
        spaceAfter=14,
    )
    heading_styles = {
        1: ParagraphStyle("AlphaH1", parent=base, fontSize=15, leading=21, textColor=colors.HexColor("#0f4c81"), spaceBefore=12, spaceAfter=8),
        2: ParagraphStyle("AlphaH2", parent=base, fontSize=13, leading=19, textColor=colors.HexColor("#14532d"), spaceBefore=10, spaceAfter=6),
        3: ParagraphStyle("AlphaH3", parent=base, fontSize=11.5, leading=17, textColor=colors.HexColor("#374151"), spaceBefore=8, spaceAfter=5),
        4: ParagraphStyle("AlphaH4", parent=base, fontSize=10.5, leading=16, textColor=colors.HexColor("#374151"), spaceBefore=6, spaceAfter=4),
    }
    bullet_style = ParagraphStyle("AlphaBullet", parent=base, leftIndent=12, firstLineIndent=0, spaceAfter=4)
    quote_style = ParagraphStyle("AlphaQuote", parent=base, leftIndent=10, textColor=colors.HexColor("#4b5563"))
    code_style = ParagraphStyle("AlphaCode", parent=base, fontSize=9, leading=13, leftIndent=8, backColor=colors.HexColor("#f3f4f6"))

    story: list[object] = [Paragraph(paragraph_markup(title), title_style), Spacer(1, 3 * mm)]
    for block in markdown_blocks(text):
        if block.kind == "heading":
            story.append(Paragraph(paragraph_markup(block.text), heading_styles.get(block.level, heading_styles[4])))
        elif block.kind == "bullet":
            story.append(Paragraph(paragraph_markup(block.text), bullet_style, bulletText="-"))
        elif block.kind == "quote":
            story.append(Paragraph(paragraph_markup(block.text), quote_style))
        elif block.kind == "code":
            story.append(Paragraph(paragraph_markup(block.text), code_style))
        elif block.kind == "rule":
            story.append(Spacer(1, 5 * mm))
        else:
            story.append(Paragraph(paragraph_markup(block.text), base))
        if len(story) % 95 == 0:
            story.append(PageBreak())

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=title,
        author="AlphaDesk / Hermes",
    )

    def footer(canvas, document) -> None:  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#6b7280"))
        canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"{safe_filename(title)} page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output_path


def read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render an AlphaDesk report to a WeChat-sendable PDF.")
    parser.add_argument("--input", required=True, help="UTF-8 report file path, or '-' for stdin")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--format", choices=("auto", "markdown", "html"), default="auto")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    report_text = read_input(args.input)
    output_path = args.output
    if output_path is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = args.output_dir / f"{safe_filename(args.title)}-{stamp}.pdf"
    pdf_path = render_pdf(report_text, output_path, title=args.title, input_format=args.format)
    print(json.dumps({"pdf_path": str(pdf_path), "bytes": pdf_path.stat().st_size, "title": args.title}, ensure_ascii=False))
    print(f"MEDIA:{pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
