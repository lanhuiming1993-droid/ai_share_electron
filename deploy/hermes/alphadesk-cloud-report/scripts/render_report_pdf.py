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


@dataclass
class SourceBadge:
    text: str
    high: bool = False


@dataclass
class StructuredItem:
    text: str
    label: str = ""
    label_kind: str = ""


@dataclass
class ReportCard:
    sources: list[SourceBadge]
    paragraphs: list[str]
    items: list[StructuredItem]
    risk: bool = False


@dataclass
class ReportElement:
    kind: str
    text: str = ""
    level: int = 0
    badges: list[SourceBadge] | None = None
    card: ReportCard | None = None


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


def _class_set(attrs: list[tuple[str, str | None]]) -> set[str]:
    for key, value in attrs:
        if key.lower() == "class" and value:
            return {part.strip().lower() for part in value.split() if part.strip()}
    return set()


def _style_text(attrs: list[tuple[str, str | None]]) -> str:
    for key, value in attrs:
        if key.lower() == "style" and value:
            return value.lower()
    return ""


class StructuredReportHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[ReportElement] = []
        self._skip_depth = 0
        self._heading_tag = ""
        self._heading_parts: list[str] = []
        self._meta_depth = 0
        self._meta_span_parts: list[str] | None = None
        self._card_depth = 0
        self._card: ReportCard | None = None
        self._paragraph_parts: list[str] | None = None
        self._li_parts: list[str] | None = None
        self._badge_parts: list[str] | None = None
        self._badge_high = False
        self._label_parts: list[str] | None = None
        self._label_active = False
        self._label_kind = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        classes = _class_set(attrs)
        if tag in {"script", "style", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in {"h1", "h2", "h3"}:
            self._heading_tag = tag
            self._heading_parts = []
            return
        if "meta" in classes:
            self._meta_depth += 1
            return
        if tag == "span" and self._meta_depth:
            self._meta_span_parts = []
            return
        if tag == "div" and "card" in classes:
            self._card_depth = 1
            style = _style_text(attrs)
            self._card = ReportCard([], [], [], risk="#fef2f2" in style or "#fecaca" in style)
            return
        if self._card is not None:
            if tag == "div":
                self._card_depth += 1
            if tag == "p":
                self._paragraph_parts = []
            elif tag == "li":
                self._li_parts = []
            elif tag == "span" and "source-tag" in classes:
                self._badge_parts = []
                self._badge_high = "source-high" in classes
            elif tag == "span" and classes.intersection({"fact", "infer", "unverified"}):
                self._label_parts = []
                self._label_active = True
                self._label_kind = next(kind for kind in ("fact", "infer", "unverified") if kind in classes)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._heading_tag and tag == self._heading_tag:
            text = clean_text("".join(self._heading_parts))
            if text:
                self.elements.append(ReportElement("heading", text=text, level=int(self._heading_tag[1])))
            self._heading_tag = ""
            self._heading_parts = []
            return
        if tag == "span" and self._meta_span_parts is not None:
            text = clean_text("".join(self._meta_span_parts))
            if text:
                self.elements.append(ReportElement("meta", text=text))
            self._meta_span_parts = None
            return
        if self._meta_depth and tag in {"div", "p"}:
            self._meta_depth = max(0, self._meta_depth - 1)
            return
        if self._card is not None:
            if tag == "span" and self._badge_parts is not None:
                text = clean_text("".join(self._badge_parts))
                if text:
                    self._card.sources.append(SourceBadge(text=text, high=self._badge_high))
                self._badge_parts = None
                self._badge_high = False
                return
            if tag == "span" and self._label_parts is not None:
                self._label_parts = [clean_text("".join(self._label_parts))]
                self._label_active = False
                return
            if tag == "p" and self._paragraph_parts is not None:
                text = clean_text("".join(self._paragraph_parts))
                if text:
                    self._card.paragraphs.append(text)
                self._paragraph_parts = None
                return
            if tag == "li" and self._li_parts is not None:
                text = clean_text("".join(self._li_parts))
                label = clean_text("".join(self._label_parts or []))
                if text:
                    self._card.items.append(StructuredItem(text=text, label=label, label_kind=self._label_kind))
                self._li_parts = None
                self._label_parts = None
                self._label_active = False
                self._label_kind = ""
                return
            if tag == "div":
                self._card_depth -= 1
                if self._card_depth <= 0:
                    if self._card.sources or self._card.paragraphs or self._card.items:
                        self.elements.append(ReportElement("card", card=self._card))
                    self._card = None
                    self._card_depth = 0

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._heading_tag:
            self._heading_parts.append(data)
        elif self._meta_span_parts is not None:
            self._meta_span_parts.append(data)
        elif self._badge_parts is not None:
            self._badge_parts.append(data)
        elif self._label_active and self._label_parts is not None and self._li_parts is not None:
            self._label_parts.append(data)
        elif self._li_parts is not None:
            self._li_parts.append(data)
        elif self._paragraph_parts is not None:
            self._paragraph_parts.append(data)


def structured_html_elements(html: str) -> list[ReportElement]:
    parser = StructuredReportHtmlParser()
    parser.feed(html)
    parser.close()
    return parser.elements


def has_structured_report_elements(elements: list[ReportElement]) -> bool:
    return any(
        element.kind == "card"
        and element.card is not None
        and (element.card.sources or any(item.label_kind for item in element.card.items))
        for element in elements
    )


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


def badge_markup(badge: SourceBadge) -> str:
    color = "#92400e" if badge.high else "#1d4ed8"
    prefix = "高权重 " if badge.high else ""
    return f'<font color="{color}"><b>[{escape(prefix + clean_text(badge.text))}]</b></font>'


def label_markup(label: str, kind: str) -> str:
    label_text = clean_text(label) or {"fact": "事实", "infer": "推断", "unverified": "待核验"}.get(kind, "")
    color = {"fact": "#15803d", "infer": "#b45309", "unverified": "#b91c1c"}.get(kind, "#374151")
    if not label_text:
        return ""
    return f'<font color="{color}"><b>{escape(label_text)}</b></font> '


def render_structured_pdf(report_text: str, output_path: Path, title: str = DEFAULT_TITLE) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    elements = structured_html_elements(report_text)
    font_name = register_font()
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "AlphaStructuredBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.2,
        leading=15.5,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=5,
    )
    title_style = ParagraphStyle("AlphaStructuredTitle", parent=base, fontSize=18, leading=24, textColor=colors.HexColor("#0f172a"), spaceAfter=8)
    meta_style = ParagraphStyle("AlphaStructuredMeta", parent=base, fontSize=8.8, leading=12.5, textColor=colors.HexColor("#475569"))
    h1_style = ParagraphStyle("AlphaStructuredH1", parent=base, fontSize=16, leading=22, textColor=colors.HexColor("#0f172a"), spaceBefore=8, spaceAfter=6)
    h2_style = ParagraphStyle("AlphaStructuredH2", parent=base, fontSize=13.5, leading=19, textColor=colors.HexColor("#1d4ed8"), spaceBefore=14, spaceAfter=7)
    h3_style = ParagraphStyle("AlphaStructuredH3", parent=base, fontSize=11.4, leading=16.5, textColor=colors.HexColor("#1e3a5f"), spaceBefore=9, spaceAfter=5)
    badge_style = ParagraphStyle("AlphaStructuredBadges", parent=base, fontSize=8.6, leading=13, spaceAfter=6)
    bullet_style = ParagraphStyle("AlphaStructuredBullet", parent=base, leftIndent=9, firstLineIndent=0, spaceAfter=4)
    card_text_style = ParagraphStyle("AlphaStructuredCardText", parent=base, spaceAfter=4)

    story: list[object] = [Paragraph(paragraph_markup(title), title_style)]
    pending_meta: list[str] = []
    content_width = A4[0] - 32 * mm

    def flush_meta() -> None:
        if not pending_meta:
            return
        rows = [[Paragraph(paragraph_markup(item), meta_style)] for item in pending_meta]
        story.append(
            Table(
                rows,
                colWidths=[content_width],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                ),
            )
        )
        story.append(Spacer(1, 4 * mm))
        pending_meta.clear()

    for element in elements:
        if element.kind == "meta":
            pending_meta.append(element.text)
            continue
        flush_meta()
        if element.kind == "heading":
            style = {1: h1_style, 2: h2_style, 3: h3_style}.get(element.level, h3_style)
            story.append(Paragraph(paragraph_markup(element.text), style))
            continue
        if element.kind != "card" or element.card is None:
            continue
        card = element.card
        card_flowables: list[object] = []
        if card.sources:
            card_flowables.append(Paragraph(" ".join(badge_markup(badge) for badge in card.sources), badge_style))
        for paragraph in card.paragraphs:
            card_flowables.append(Paragraph(paragraph_markup(paragraph), card_text_style))
        for item in card.items:
            text = label_markup(item.label, item.label_kind) + paragraph_markup(item.text)
            card_flowables.append(Paragraph(text, bullet_style, bulletText="-"))
        background = "#fef2f2" if card.risk else "#f8fafc"
        border = "#fecaca" if card.risk else "#e2e8f0"
        rows = [[flowable] for flowable in (card_flowables or [Paragraph(" ", base)])]
        story.append(
            Table(
                rows,
                colWidths=[content_width],
                splitByRow=1,
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(background)),
                        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(border)),
                        ("LEFTPADDING", (0, 0), (-1, -1), 10),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("TOPPADDING", (0, 0), (-1, 0), 8),
                        ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
                    ]
                ),
            )
        )
        story.append(Spacer(1, 4 * mm))
    flush_meta()

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
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"{safe_filename(title)} page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output_path


def render_pdf(report_text: str, output_path: Path, title: str = DEFAULT_TITLE, input_format: str = "auto") -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    text = clean_text(report_text)
    if input_format == "html" or (input_format == "auto" and looks_like_html(text)):
        structured_elements = structured_html_elements(text)
        if has_structured_report_elements(structured_elements):
            return render_structured_pdf(text, output_path, title=title)

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
