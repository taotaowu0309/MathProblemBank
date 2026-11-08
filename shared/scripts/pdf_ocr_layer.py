"""A text layer for scanned PDF pages, from RapidOCR, cached per page.

A scanned textbook has no text layer, so selecting, looking up words, marking
"不懂", searching and reference previews had nothing to work with.  For such a
page the reader runs the project's existing RapidOCR engine once, stores the
recognised lines in ``ReadingHelp/ocr_cache/<pdf sha256>/p<page>.json`` (a
derived cache; deleting it only means the page is recognised again), and builds
from them the same structures the PDF text layer gives: a PyMuPDF-style
``rawdict`` for the glyph geometry and word tuples for search.

Character boxes inside a recognised line are spread evenly over its width, so
selection is approximate within a line.  RapidOCR sometimes drops spaces
between English words; such words stay joined.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

OCR_CACHE_VERSION = 1
SCANNED_TEXT_THRESHOLD = 48


def page_is_scanned(page: Any) -> bool:
    """A page with (almost) no text but with an image: a scan."""

    visible = re.sub(r"\s+", "", page.get_text("text") or "")
    if len(visible) >= SCANNED_TEXT_THRESHOLD:
        return False
    try:
        return bool(page.get_images(full=False))
    except Exception:
        return False


def run_page_ocr(page: Any, *, scale: float = 2.0) -> list[dict[str, Any]]:
    """Recognise one page; lines in PDF point coordinates, top to bottom."""

    from shared.scripts.ai_agent_ocr import OCR_MIN_CONFIDENCE, _engine, fitz, np

    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    result, _elapsed = _engine()(image)
    lines: list[dict[str, Any]] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        text = str(item[1] or "").strip()
        try:
            confidence = float(item[2])
            xs = [float(point[0]) / scale for point in item[0]]
            ys = [float(point[1]) / scale for point in item[0]]
        except (TypeError, ValueError, IndexError):
            continue
        if not text or confidence < OCR_MIN_CONFIDENCE:
            continue
        lines.append({"text": text, "bbox": [min(xs), min(ys), max(xs), max(ys)], "confidence": round(confidence, 3)})
    lines.sort(key=lambda line: (round(line["bbox"][1] / 4), line["bbox"][0]))
    return lines


def group_blocks(lines: Sequence[dict[str, Any]]) -> list[int]:
    """Paragraph number per line: a line joins the previous one when it follows closely below it
    and the previous line runs (nearly) the full column width."""

    blocks: list[int] = []
    block = -1
    previous: list[float] | None = None
    widths = sorted(float(line["bbox"][2]) - float(line["bbox"][0]) for line in lines)
    full_width = widths[int(len(widths) * 0.75)] if widths else 0.0
    for line in lines:
        x0, y0, x1, y1 = line["bbox"]
        height = max(1.0, y1 - y0)
        joins = (
            previous is not None
            and 0 <= y0 - previous[3] < 0.9 * height
            and min(x1, previous[2]) - max(x0, previous[0]) > 0.3 * min(x1 - x0, previous[2] - previous[0])
            # A short line ends its paragraph ("... we want to use."), even when the next heading follows closely.
            and not (previous[2] - previous[0] < 0.6 * full_width and abs(x0 - previous[0]) < 0.5 * height)
        )
        if not joins:
            block += 1
        blocks.append(block)
        previous = [x0, y0, x1, y1]
    return blocks


def _char_boxes(text: str, bbox: Sequence[float]) -> list[tuple[float, float, float, float]]:
    x0, y0, x1, y1 = (float(value) for value in bbox)
    step = (x1 - x0) / max(1, len(text))
    return [(x0 + step * index, y0, x0 + step * (index + 1), y1) for index in range(len(text))]


def ocr_rawdict(lines: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """PyMuPDF ``get_text("rawdict")``-shaped data for ``pdf_word_geometry.build_page_geometry``."""

    numbers = group_blocks(lines)
    blocks: dict[int, dict[str, Any]] = {}
    for line, number in zip(lines, numbers):
        x0, y0, x1, y1 = (float(value) for value in line["bbox"])
        height = max(1.0, y1 - y0)
        baseline = y1 - 0.22 * height
        chars = [
            {"c": char, "bbox": box, "origin": (box[0], baseline)}
            for char, box in zip(line["text"], _char_boxes(line["text"], line["bbox"]))
        ]
        span = {
            "font": "OCR", "size": height * 0.8, "flags": 0, "ascender": 0.9, "descender": -0.22,
            "origin": (x0, baseline), "bbox": (x0, y0, x1, y1), "chars": chars,
        }
        block = blocks.setdefault(number, {"type": 0, "number": number, "bbox": [x0, y0, x1, y1], "lines": []})
        block["bbox"] = [min(block["bbox"][0], x0), min(block["bbox"][1], y0), max(block["bbox"][2], x1), max(block["bbox"][3], y1)]
        block["lines"].append({"dir": (1.0, 0.0), "wmode": 0, "bbox": (x0, y0, x1, y1), "spans": [span]})
    return {"blocks": [blocks[key] for key in sorted(blocks)]}


def ocr_words(lines: Sequence[dict[str, Any]]) -> list[tuple]:
    """Word tuples ``(x0, y0, x1, y1, text, block, line, word)`` like PyMuPDF ``get_text("words")``."""

    numbers = group_blocks(lines)
    words: list[tuple] = []
    line_in_block: dict[int, int] = {}
    for line, block in zip(lines, numbers):
        line_number = line_in_block.get(block, -1) + 1
        line_in_block[block] = line_number
        boxes = _char_boxes(line["text"], line["bbox"])
        for word_number, match in enumerate(re.finditer(r"\S+", line["text"])):
            first, last = boxes[match.start()], boxes[match.end() - 1]
            words.append((first[0], first[1], last[2], last[3], match.group(0), block, line_number, word_number))
    return words


def blocks_text(lines: Sequence[dict[str, Any]]) -> list[tuple]:
    """``get_text("blocks")``-shaped tuples, for the reference preview clip."""

    numbers = group_blocks(lines)
    grouped: dict[int, list[dict[str, Any]]] = {}
    for line, number in zip(lines, numbers):
        grouped.setdefault(number, []).append(line)
    result = []
    for number, members in sorted(grouped.items()):
        boxes = [member["bbox"] for member in members]
        result.append((
            min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes),
            "\n".join(member["text"] for member in members), number, 0,
        ))
    return result


class OcrCache:
    """Recognised lines of scanned pages, one JSON file per page."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, pdf_sha256: str, page_index: int) -> Path:
        return self.root / pdf_sha256 / f"p{int(page_index):05d}.json"

    def load(self, pdf_sha256: str, page_index: int) -> list[dict[str, Any]] | None:
        path = self._path(pdf_sha256, page_index)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != OCR_CACHE_VERSION:
            return None
        lines = data.get("lines")
        return lines if isinstance(lines, list) else None

    def save(self, pdf_sha256: str, page_index: int, lines: Sequence[dict[str, Any]]) -> None:
        path = self._path(pdf_sha256, page_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": OCR_CACHE_VERSION, "page": int(page_index), "lines": list(lines)}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)

    def cached_pages(self, pdf_sha256: str) -> set[int]:
        folder = self.root / pdf_sha256
        pages: set[int] = set()
        for path in folder.glob("p*.json") if folder.is_dir() else []:
            try:
                pages.add(int(path.stem[1:]))
            except ValueError:
                continue
        return pages

    def recognise(self, pdf_sha256: str, page: Any, page_index: int) -> list[dict[str, Any]]:
        cached = self.load(pdf_sha256, page_index)
        if cached is not None:
            return cached
        lines = run_page_ocr(page)
        self.save(pdf_sha256, page_index, lines)
        return lines
