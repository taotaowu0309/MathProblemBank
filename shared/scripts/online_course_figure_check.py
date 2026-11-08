"""Deterministic layout checks for textbook figures placed in lecture notes.

A textbook figure enters the notes as the exact original crop (``% MPB-TEXTBOOK-FIGURE``).
Books are scanned at different scales, so printing every crop at its native size gives labels
from 1.3 mm to 3 mm. The importer therefore scales each figure so that its letters print at
about ``TARGET_LABEL_MM``, the height of a capital in the 10.5 pt body text, within bounds. This module
measures the letters, computes that size, and finds stray scan marks, so that Work can correct
a figure before import instead of paging through the compiled PDF. Nothing here changes the
original file; a stray mark can be covered at typesetting time with ``clean=marks``.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Mapping

import fitz
import numpy as np

# A4 with 22 mm side margins (latex/preamble/geometry.tex of the lecture projects).
TEXT_WIDTH_BP = 595.28 - 2 * 22 / 25.4 * 72
MAX_FIGURE_HEIGHT_BP = 330.0
MAX_FIGURE_WIDTH_FRACTION = 0.70
TARGET_LABEL_MM = 2.5
MIN_SCALE, MAX_SCALE = 0.7, 1.6
SMALL_LABEL_WARNING_MM = 1.8
MAIN_LABEL_MIN_MM = 1.8
_LABEL_DPI = 300
LABEL_METHOD_VERSION = 2
MIN_FIGURES_PER_BOOK = 3
# A mark is "stray" when it is at most this large and this far from every other stroke.
STRAY_MARK_MAX_MM = 1.1
STRAY_MARK_MIN_FILL = 0.5
STRAY_MARK_ISOLATION_MM = 3.0
_DPI = 100


def natural_size_bp(path: Path) -> tuple[float, float]:
    with fitz.open(path) as document:
        rectangle = document[0].rect
    return float(rectangle.width), float(rectangle.height)


def label_height_mm(path: Path) -> float | None:
    """Height of the main labels (capital letters) in a figure, at native size.

    Letters come in two sizes, main labels and subscripts, and a few other marks (braces,
    arrowheads) look letter-like too. Main labels repeat, so the most frequent height among
    marks of at least ``MAIN_LABEL_MIN_MM`` is taken, in 0.2 mm bins."""
    with fitz.open(path) as document:
        pixmap = document[0].get_pixmap(dpi=_LABEL_DPI, colorspace=fitz.csGRAY)
    ink = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width) < 150
    per_mm = _LABEL_DPI / 25.4
    heights = []
    for members in _components(ink):
        height = (members[:, 0].max() - members[:, 0].min() + 1) / per_mm
        width = (members[:, 1].max() - members[:, 1].min() + 1) / per_mm
        fill = len(members) / (height * width * per_mm * per_mm)
        # Letters are 1-6 mm tall, not much wider than tall, and neither lines nor solid blots.
        if 1.0 <= height <= 6.0 and 0.4 <= width <= 1.6 * height and 0.12 <= fill <= 0.75:
            heights.append(height)
    if len(heights) < 2:
        return None
    main = [height for height in heights if height >= MAIN_LABEL_MIN_MM]
    pool = np.array(main if len(main) >= 2 else heights)
    bins = np.round(pool / 0.2).astype(int)
    values, counts = np.unique(bins, return_counts=True)
    best = values[counts == counts.max()].max()
    return float(np.median(pool[bins == best]))


def book_label_heights(assets: Mapping[str, Path], cache_path: Path) -> dict[str, float]:
    """Label height per source book, as the median over all of that book's indexed figures.

    One book is scanned at one scale, so a single height per book is far steadier than a
    per-figure estimate, which a few braces or lowercase letters can mislead. Figures are
    measured once and cached by hash."""
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if cache.get("version") != LABEL_METHOD_VERSION:
        cache = {"version": LABEL_METHOD_VERSION, "labels": {}}
    labels: dict[str, Any] = cache["labels"]
    by_book: dict[str, list[float]] = defaultdict(list)
    changed = False
    for digest, path in assets.items():
        try:
            source = str(json.loads(path.with_suffix(".source.json").read_text(encoding="utf-8"))["source_sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if digest not in labels:
            labels[digest] = label_height_mm(path)
            changed = True
        if labels[digest]:
            by_book[source].append(float(labels[digest]))
    if changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(cache, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, cache_path)
    return {book: float(np.median(values)) for book, values in by_book.items()
            if len(values) >= MIN_FIGURES_PER_BOOK}


def figure_label_mm(path: Path, book_labels: Mapping[str, float]) -> tuple[float | None, str]:
    try:
        source = str(json.loads(path.with_suffix(".source.json").read_text(encoding="utf-8"))["source_sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        source = ""
    if source in book_labels:
        return book_labels[source], "book"
    return label_height_mm(path), "figure"


def printed_size(natural: tuple[float, float], width_fraction: float,
                 label_mm: float | None = None) -> dict[str, Any]:
    """Scale that prints the labels at ``TARGET_LABEL_MM``, bounded by width, height and limits."""
    width, height = natural
    wanted = 1.0 if not label_mm else min(MAX_SCALE, max(MIN_SCALE, TARGET_LABEL_MM / label_mm))
    width_limit = min(width_fraction, MAX_FIGURE_WIDTH_FRACTION) * TEXT_WIDTH_BP
    scale = min(wanted, width_limit / width, MAX_FIGURE_HEIGHT_BP / height) if width and height else 0.0
    return {
        "natural_width_bp": round(width, 2), "natural_height_bp": round(height, 2),
        "label_mm": round(label_mm, 2) if label_mm else None,
        "scale": round(scale, 3), "printed_width_bp": round(width * scale, 2),
        "printed_width_mm": round(width * scale / 72 * 25.4, 1),
        "printed_height_mm": round(height * scale / 72 * 25.4, 1),
        "printed_label_mm": round(label_mm * scale, 2) if label_mm else None,
    }


def _grow(mask: np.ndarray, radius: int) -> np.ndarray:
    counts = np.pad(mask.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    height, width = mask.shape
    rows, columns = np.arange(height), np.arange(width)
    top, bottom = np.clip(rows - radius, 0, height), np.clip(rows + radius + 1, 0, height)
    left, right = np.clip(columns - radius, 0, width), np.clip(columns + radius + 1, 0, width)
    total = (counts[bottom][:, right] - counts[top][:, right]
             - counts[bottom][:, left] + counts[top][:, left])
    return total > 0


def _components(mask: np.ndarray) -> list[np.ndarray]:
    height, width = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    groups = []
    for start in zip(*np.nonzero(mask)):
        if seen[start]:
            continue
        seen[start] = True
        queue, members = deque([start]), [start]
        while queue:
            y, x = queue.popleft()
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    queue.append((ny, nx))
                    members.append((ny, nx))
        groups.append(np.array(members))
    return groups


def stray_marks(path: Path) -> list[dict[str, float]]:
    """Small isolated specks (scan dirt), in mm from the figure's top-left corner."""
    with fitz.open(path) as document:
        pixmap = document[0].get_pixmap(dpi=_DPI, colorspace=fitz.csGRAY)
    pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)
    ink = pixels < 128
    # Thin anti-aliased vector lines are light grey; they still count as nearby strokes.
    strokes = pixels < 230
    if not int(ink.sum()):
        return []
    per_mm = _DPI / 25.4
    groups = _components(_grow(strokes, int(round(STRAY_MARK_ISOLATION_MM / 2 * per_mm))))
    if len(groups) < 2:
        return []
    marks = []
    for members in groups:
        drawn = members[strokes[members[:, 0], members[:, 1]]]
        dark = members[ink[members[:, 0], members[:, 1]]]
        if not len(dark):
            continue
        top, left = drawn.min(axis=0)
        bottom, right = drawn.max(axis=0) + 1
        # Scan dirt is a tiny solid blot; labels, circles and node dots are larger or hollow.
        if (max(bottom - top, right - left) > STRAY_MARK_MAX_MM * per_mm
                or len(drawn) < STRAY_MARK_MIN_FILL * (bottom - top) * (right - left)):
            continue
        marks.append({key: round(float(value) / per_mm, 2) for key, value in
                      (("left_mm", left), ("top_mm", top), ("right_mm", right), ("bottom_mm", bottom))})
    return sorted(marks, key=lambda mark: (mark["top_mm"], mark["left_mm"]))


def inspect_figure(path: Path, width_fraction: float,
                   book_labels: Mapping[str, float] | None = None) -> dict[str, Any]:
    natural = natural_size_bp(path)
    label, basis = figure_label_mm(path, book_labels or {})
    result: dict[str, Any] = {**printed_size(natural, width_fraction, label), "label_basis": basis,
                              "stray_marks": stray_marks(path)}
    warnings = []
    if result["stray_marks"]:
        places = "；".join(f"距左 {mark['left_mm']:.1f} mm、距上 {mark['top_mm']:.1f} mm"
                          for mark in result["stray_marks"])
        warnings.append(f"原图有 {len(result['stray_marks'])} 个孤立小墨点（{places}），多为扫描残留："
                        "确认不是数学内容后，在标记行末加 clean=marks 盖白；若是内容，保留即可。")
    if result["printed_label_mm"] is None:
        warnings.append("没认出图内字母，按原书大小排版：核对正式 PDF 中的大小是否协调。")
    elif result["printed_label_mm"] < SMALL_LABEL_WARNING_MM:
        warnings.append(f"图太宽或太高，图内字母只能印到 {result['printed_label_mm']:.1f} mm（正文大写字母约 2.5 mm）："
                        "拆成两张图，或换更紧的裁切。")
    result["warnings"] = warnings
    return result
