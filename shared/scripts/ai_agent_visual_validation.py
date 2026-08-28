from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz
import numpy as np
from PIL import Image


def _image_metrics(image: Image.Image) -> dict[str, Any]:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    gray = rgb.mean(axis=2)
    ink = gray < 245
    ink_pixels = int(ink.sum())
    total = max(1, width * height)
    if ink_pixels:
        ys, xs = np.where(ink)
        bbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    else:
        bbox = [0, 0, 0, 0]
    margins = {
        "left": bbox[0],
        "top": bbox[1],
        "right": width - bbox[2],
        "bottom": height - bbox[3],
    }
    issues: list[str] = []
    if width < 500 or height < 350:
        issues.append("渲染分辨率偏低，文字或细线可能不清晰。")
    if ink_pixels / total < 0.002:
        issues.append("页面几乎为空白，没有检测到足够的图形内容。")
    if ink_pixels / total > 0.75:
        issues.append("图形内容过密，可能存在大面积填充或渲染异常。")
    if ink_pixels and min(margins.values()) <= 2:
        issues.append("图形内容触及页面边缘，可能发生裁切。")
    if width / max(1, height) > 5 or height / max(1, width) > 5:
        issues.append("画布纵横比极端，可能导致图形在正文中缩得过小。")
    return {
        "width": width,
        "height": height,
        "ink_ratio": round(ink_pixels / total, 6),
        "content_bbox": bbox,
        "margins": margins,
        "issues": issues,
    }


def _overlapping_text_blocks(page: fitz.Page) -> list[dict[str, Any]]:
    def is_axis_tick_block(text: str) -> bool:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        compact = "".join(lines)
        if not compact:
            return False
        allowed = set("0123456789-+−–—.√π/() ")
        return all(character in allowed for character in compact) and (
            len(lines) >= 2 or len(compact) <= 4
        )

    blocks = [
        block
        for block in page.get_text("blocks")
        if str(block[4]).strip() and not is_axis_tick_block(str(block[4]))
    ]
    overlaps: list[dict[str, Any]] = []
    for index, left in enumerate(blocks):
        a = fitz.Rect(left[:4])
        for right in blocks[index + 1 :]:
            b = fitz.Rect(right[:4])
            intersection = a & b
            if intersection.is_empty:
                continue
            smaller = min(max(a.get_area(), 1.0), max(b.get_area(), 1.0))
            if intersection.get_area() / smaller >= 0.18:
                overlaps.append(
                    {
                        "first": str(left[4]).strip()[:120],
                        "second": str(right[4]).strip()[:120],
                        "overlap_ratio": round(intersection.get_area() / smaller, 4),
                    }
                )
    return overlaps[:20]


def validate_math_figure(
    path: str,
    *,
    page_number: int = 1,
    expected_labels: list[str] | None = None,
) -> dict[str, Any]:
    target = Path(str(path or "")).expanduser().resolve()
    if not target.is_file():
        raise ValueError("待检查的图像或 PDF 文件不存在。")
    suffix = target.suffix.casefold()
    text_overlaps: list[dict[str, Any]] = []
    rendered_text = ""
    if suffix == ".pdf":
        with fitz.open(target) as document:
            if document.page_count <= 0:
                raise ValueError("PDF 没有可检查的页面。")
            index = max(0, min(int(page_number) - 1, document.page_count - 1))
            page = document[index]
            rendered_text = " ".join(page.get_text("text").split())
            text_overlaps = _overlapping_text_blocks(page)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            selected_page = index + 1
            page_count = document.page_count
    elif suffix in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
        with Image.open(target) as opened:
            image = opened.convert("RGB")
        selected_page = 1
        page_count = 1
    else:
        raise ValueError("视觉检查只支持 PDF、PNG、JPEG、BMP、WebP 和 TIFF。")
    metrics = _image_metrics(image)
    issues = list(metrics.pop("issues"))
    if text_overlaps:
        issues.append("检测到可能互相覆盖的文字标签，需要人工查看重叠位置。")
    normalized_rendered = "".join(rendered_text.split()).casefold()
    checked_labels = [str(item or "").strip() for item in expected_labels or [] if str(item or "").strip()]
    missing_labels = [
        label
        for label in checked_labels
        if "".join(label.split()).casefold() not in normalized_rendered
    ]
    if missing_labels:
        issues.append("渲染结果中没有识别到预期标签：" + "、".join(missing_labels[:8]))
    return {
        "path": str(target),
        "page_number": selected_page,
        "page_count": page_count,
        **metrics,
        "text_overlap_candidates": text_overlaps,
        "checked_labels": checked_labels,
        "missing_labels": missing_labels,
        "passed": not issues,
        "issues": issues,
        "verification": "local_rendered_visual_checks",
        "limitations": "自动检查能发现空白、裁切、极端尺寸、文字块重叠和缺失的显式标签；数学透视仍需结合符号坐标核验。",
    }


def validate_pdf_near_text(path: str, anchor_text: str) -> dict[str, Any]:
    target = Path(str(path or "")).expanduser().resolve()
    query = " ".join(str(anchor_text or "").split())[:160]
    if not query:
        return {
            "passed": False,
            "issues": ["没有图题或可定位文字，无法自动确定新图所在 PDF 页。"],
            "verification": "visual_page_location_inconclusive",
        }
    with fitz.open(target) as document:
        selected = 0
        normalized_query = query.casefold()
        for index, page in enumerate(document):
            text = " ".join(page.get_text("text").split()).casefold()
            if normalized_query in text or normalized_query[:40] in text:
                selected = index + 1
                break
    if not selected:
        return {
            "passed": False,
            "issues": ["正式 PDF 中没有定位到图题文字，无法自动确定新图页。"],
            "verification": "visual_page_location_inconclusive",
        }
    result = validate_math_figure(str(target), page_number=selected)
    result["located_by_text"] = query
    return result
