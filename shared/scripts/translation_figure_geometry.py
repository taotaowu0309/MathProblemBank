from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


FIGURE_GEOMETRY_SCHEMA_VERSION = 1
DEFAULT_AUDIT_DPI = 96
DEFAULT_BENCHMARK_PAGES = 40


@dataclass(frozen=True, slots=True)
class PdfRasterGeometry:
    """Independent mapping between a normalized PDF page view and its raster."""

    page_x0: float
    page_y0: float
    page_width_pt: float
    page_height_pt: float
    pixel_width: int
    pixel_height: int
    render_dpi: int
    original_rotation: int

    @property
    def x_scale(self) -> float:
        return self.pixel_width / self.page_width_pt

    @property
    def y_scale(self) -> float:
        return self.pixel_height / self.page_height_pt

    def page_points_to_pixels(self, box: Iterable[float]) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = [float(value) for value in box]
        return (
            int(round((x0 - self.page_x0) * self.x_scale)),
            int(round((y0 - self.page_y0) * self.y_scale)),
            int(round((x1 - self.page_x0) * self.x_scale)),
            int(round((y1 - self.page_y0) * self.y_scale)),
        )


@contextmanager
def normalized_page_rotation(page: Any) -> Iterator[None]:
    """Temporarily render/crop in the unrotated page view and always restore it."""

    original_rotation = int(page.rotation or 0)
    try:
        if original_rotation:
            page.set_rotation(0)
        yield
    finally:
        if original_rotation:
            page.set_rotation(original_rotation)


def source_bbox_to_page_points(
    page: Any,
    bbox: Iterable[float],
    coordinate_size: Iterable[float],
    *,
    padding: bool = True,
) -> tuple[float, float, float, float]:
    """Map a top-left source raster box to normalized PyMuPDF page points."""

    values = [float(value) for value in bbox]
    size = [float(value) for value in coordinate_size]
    if len(values) < 4 or len(size) < 2 or size[0] <= 0 or size[1] <= 0:
        raise ValueError("图片 bbox 或坐标空间无效。")
    sx0, sy0, sx1, sy1 = values[:4]
    if sx1 <= sx0 or sy1 <= sy0:
        raise ValueError("图片 bbox 无效。")
    with normalized_page_rotation(page):
        rect = page.rect
        x0 = float(rect.x0) + float(rect.width) * sx0 / size[0]
        y0 = float(rect.y0) + float(rect.height) * sy0 / size[1]
        x1 = float(rect.x0) + float(rect.width) * sx1 / size[0]
        y1 = float(rect.y0) + float(rect.height) * sy1 / size[1]
        if padding:
            pad_x = min(12.0, max(2.0, (x1 - x0) * 0.015))
            pad_y = min(12.0, max(2.0, (y1 - y0) * 0.015))
            x0 -= pad_x
            y0 -= pad_y
            x1 += pad_x
            y1 += pad_y
        return (
            max(float(rect.x0), x0),
            max(float(rect.y0), y0),
            min(float(rect.x1), x1),
            min(float(rect.y1), y1),
        )


def render_normalized_page(page: Any, *, dpi: int = DEFAULT_AUDIT_DPI) -> tuple[Any, PdfRasterGeometry]:
    """Render one page with rotation normalized and retain exact realized scales."""

    dpi = max(36, min(int(dpi), 600))
    original_rotation = int(page.rotation or 0)
    import fitz  # type: ignore

    with normalized_page_rotation(page):
        rect = page.rect
        pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        geometry = PdfRasterGeometry(
            page_x0=float(rect.x0),
            page_y0=float(rect.y0),
            page_width_pt=float(rect.width),
            page_height_pt=float(rect.height),
            pixel_width=int(pixmap.width),
            pixel_height=int(pixmap.height),
            render_dpi=dpi,
            original_rotation=original_rotation,
        )
    return pixmap, geometry


def save_normalized_pdf_clip(
    page: Any,
    clip: Iterable[float],
    output: Path,
    *,
    dpi: int,
) -> PdfRasterGeometry:
    """Crop in the same normalized coordinate frame used by bbox conversion."""

    import fitz  # type: ignore

    dpi = max(144, min(int(dpi), 600))
    original_rotation = int(page.rotation or 0)
    with normalized_page_rotation(page):
        rect = page.rect
        clip_rect = fitz.Rect(*[float(value) for value in clip]) & rect
        if clip_rect.is_empty or clip_rect.is_infinite:
            raise ValueError("图片裁切框不在 PDF 页面内。")
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(dpi / 72, dpi / 72),
            clip=clip_rect,
            alpha=False,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(output)
        return PdfRasterGeometry(
            page_x0=float(rect.x0),
            page_y0=float(rect.y0),
            page_width_pt=float(rect.width),
            page_height_pt=float(rect.height),
            pixel_width=max(1, int(round(float(rect.width) * dpi / 72))),
            pixel_height=max(1, int(round(float(rect.height) * dpi / 72))),
            render_dpi=dpi,
            original_rotation=original_rotation,
        )


def _evenly_spaced(values: list[int], count: int) -> list[int]:
    if count <= 0 or not values:
        return []
    if len(values) <= count:
        return values
    if count == 1:
        return [values[len(values) // 2]]
    indexes = {round(index * (len(values) - 1) / (count - 1)) for index in range(count)}
    return [values[index] for index in sorted(indexes)]


def select_benchmark_pages(
    content_items: list[dict[str, Any]],
    page_count: int,
    *,
    maximum_pages: int = DEFAULT_BENCHMARK_PAGES,
) -> list[dict[str, Any]]:
    """Select a stable 30–50-page-style corpus without running a second model."""

    maximum_pages = max(1, min(int(maximum_pages), 50, int(page_count)))
    page_stats = {
        page: {"figure_count": 0, "equation_count": 0, "small_diagram_candidate": False}
        for page in range(1, int(page_count) + 1)
    }
    for item in content_items:
        page_number = int(item.get("page_idx") or 0) + 1
        if page_number not in page_stats:
            continue
        kind = str(item.get("type") or "").casefold()
        if kind in {"image", "chart"}:
            page_stats[page_number]["figure_count"] += 1
            bbox = item.get("bbox") or []
            if isinstance(bbox, list) and len(bbox) >= 4:
                area = max(0.0, float(bbox[2]) - float(bbox[0])) * max(
                    0.0, float(bbox[3]) - float(bbox[1])
                )
                page_stats[page_number]["small_diagram_candidate"] = area <= 30_000
        elif kind == "equation":
            page_stats[page_number]["equation_count"] += 1
    figure_pages = [page for page, row in page_stats.items() if row["figure_count"]]
    equation_only = [
        page
        for page, row in page_stats.items()
        if not row["figure_count"] and row["equation_count"] >= 2
    ]
    ordinary = [
        page
        for page, row in page_stats.items()
        if not row["figure_count"] and page not in set(equation_only)
    ]
    figure_budget = min(len(figure_pages), max(1, math.ceil(maximum_pages * 0.6)))
    equation_budget = min(len(equation_only), max(0, math.ceil(maximum_pages * 0.2)))
    selected = _evenly_spaced(figure_pages, figure_budget)
    selected += _evenly_spaced(equation_only, min(equation_budget, maximum_pages - len(selected)))
    selected += _evenly_spaced(ordinary, maximum_pages - len(selected))
    if len(selected) < maximum_pages:
        remaining = [page for page in page_stats if page not in set(selected)]
        selected += _evenly_spaced(remaining, maximum_pages - len(selected))
    return [
        {"page_number": page, **page_stats[page]}
        for page in sorted(set(selected))[:maximum_pages]
    ]


def intersection_over_union(left: Iterable[float], right: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = [float(value) for value in left]
    bx0, by0, bx1, by1 = [float(value) for value in right]
    width = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    height = max(0.0, min(ay1, by1) - max(ay0, by0))
    intersection = width * height
    left_area = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    right_area = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def evaluate_detector(
    ground_truth: list[dict[str, Any]],
    detections: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    """Evaluate one detector once human ground-truth boxes have been entered."""

    unmatched = set(range(len(detections)))
    matched = 0
    clipped = 0
    small_total = 0
    small_matched = 0
    for truth in ground_truth:
        is_small = bool(truth.get("small_diagram"))
        small_total += int(is_small)
        candidates = sorted(
            (
                (intersection_over_union(truth.get("bbox") or [], detection.get("bbox") or []), index)
                for index, detection in enumerate(detections)
                if index in unmatched
            ),
            reverse=True,
        )
        score, index = candidates[0] if candidates else (0.0, -1)
        if score >= iou_threshold:
            unmatched.discard(index)
            matched += 1
            small_matched += int(is_small)
            clipped += int(score < 0.75)
    total = len(ground_truth)
    return {
        "ground_truth_count": total,
        "detection_count": len(detections),
        "matched_count": matched,
        "figure_recall": matched / total if total else None,
        "false_positive_count": len(unmatched),
        "clipped_match_count": clipped,
        "small_diagram_recall": small_matched / small_total if small_total else None,
        "caption_contamination_count": sum(
            int(bool(item.get("caption_contamination"))) for item in detections
        ),
        "formula_misclassification_count": sum(
            int(bool(item.get("formula_misclassification"))) for item in detections
        ),
        "iou_threshold": float(iou_threshold),
    }


def write_layout_audit_and_benchmark(
    source_pdf: Path,
    content_items: list[dict[str, Any]],
    figures: list[dict[str, Any]],
    output_root: Path,
    *,
    source_sha256: str,
    audit_dpi: int = DEFAULT_AUDIT_DPI,
    maximum_pages: int = DEFAULT_BENCHMARK_PAGES,
) -> dict[str, Any]:
    """Persist MinerU overlays and a future DocLayout benchmark contract."""

    import fitz  # type: ignore
    from PIL import Image, ImageDraw, ImageFont

    output_root.mkdir(parents=True, exist_ok=True)
    overlay_dir = output_root / "audit"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(source_pdf) as document:
        selected = select_benchmark_pages(
            content_items,
            int(document.page_count),
            maximum_pages=maximum_pages,
        )
        figures_by_page: dict[int, list[dict[str, Any]]] = {}
        for figure in figures:
            figures_by_page.setdefault(int(figure["page_number"]), []).append(figure)
        selected_by_page = {int(row["page_number"]): row for row in selected}
        overlay_pages = sorted(set(selected_by_page) | set(figures_by_page))
        page_records: list[dict[str, Any]] = []
        overlay_by_page: dict[int, str] = {}
        for page_number in overlay_pages:
            page = document.load_page(page_number - 1)
            pixmap, geometry = render_normalized_page(page, dpi=audit_dpi)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default()
            page_figures = figures_by_page.get(page_number, [])
            for figure in page_figures:
                pixel_box = geometry.page_points_to_pixels(figure["pdf_clip"])
                draw.rectangle(pixel_box, outline=(0, 190, 70), width=3)
                draw.text(
                    (pixel_box[0] + 4, max(2, pixel_box[1] + 4)),
                    str(figure["figure_id"]),
                    fill=(0, 120, 40),
                    font=font,
                )
            if not page_figures:
                draw.text(
                    (8, 8),
                    "NO MINERU FIGURE CANDIDATE",
                    fill=(190, 30, 30),
                    font=font,
                )
            overlay_path = overlay_dir / f"page-{page_number:04d}-layout.png"
            image.save(overlay_path, format="PNG", optimize=True)
            overlay_relative = overlay_path.relative_to(output_root.parent).as_posix()
            overlay_by_page[page_number] = overlay_relative
            mineru_detections = [
                {
                    "figure_id": figure["figure_id"],
                    "bbox": list(figure["pdf_clip"]),
                    "bbox_source": figure["bbox_source"],
                }
                for figure in page_figures
            ]
            selection = selected_by_page.get(page_number)
            if selection is not None:
                page_records.append(
                    {
                        **selection,
                        "audit_overlay": overlay_relative,
                        "mineru": {"status": "complete", "detections": mineru_detections},
                        "doclayout": {"status": "not_run", "detections": []},
                        "ground_truth": [],
                        "annotation_status": "pending",
                    }
                )
        for figure in figures:
            figure["audit_overlay"] = overlay_by_page.get(int(figure["page_number"]), "")
            figure["geometry"] = {
                "schema_version": FIGURE_GEOMETRY_SCHEMA_VERSION,
                "coordinate_frame": "normalized_unrotated_page_points_top_left",
                "original_page_rotation": int(
                    document.load_page(int(figure["page_number"]) - 1).rotation or 0
                ),
            }
    benchmark = {
        "schema_version": 1,
        "source_sha256": source_sha256,
        "status": "awaiting_ground_truth_and_optional_doclayout",
        "selection_policy": (
            "up_to_40_pages_prioritizing_MinerU_figures_then_equation_only_and_evenly_spaced_controls"
        ),
        "comparison": ["mineru", "optional_doclayout"],
        "metrics": [
            "figure_recall",
            "false_positive_count",
            "clipped_match_count",
            "caption_contamination_count",
            "formula_misclassification_count",
            "small_diagram_recall",
        ],
        "dependency_policy": (
            "No DocLayout dependency is installed automatically. Populate its detections only "
            "after a labelled corpus justifies an optional fallback runtime."
        ),
        "license_note": (
            "PDFMathTranslate, PDFMathTranslate-next, BabelDOC and DocLayout-YOLO were studied "
            "as AGPL-3.0 reference implementations; no source code was copied."
        ),
        "pages": page_records,
    }
    benchmark_path = output_root / "benchmark.json"
    temporary = benchmark_path.with_name(f"{benchmark_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, benchmark_path)
    return {
        "benchmark_path": benchmark_path,
        "audit_overlay_count": len(overlay_pages),
        "selected_page_count": len(page_records),
        "status": benchmark["status"],
    }


__all__ = [
    "DEFAULT_AUDIT_DPI",
    "DEFAULT_BENCHMARK_PAGES",
    "FIGURE_GEOMETRY_SCHEMA_VERSION",
    "PdfRasterGeometry",
    "evaluate_detector",
    "intersection_over_union",
    "normalized_page_rotation",
    "render_normalized_page",
    "save_normalized_pdf_clip",
    "select_benchmark_pages",
    "source_bbox_to_page_points",
    "write_layout_audit_and_benchmark",
]
