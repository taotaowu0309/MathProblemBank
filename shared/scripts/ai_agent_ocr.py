from __future__ import annotations

import re
import threading
from typing import Any

import fitz
import numpy as np


OCR_TEXT_THRESHOLD = 48
OCR_MIN_CONFIDENCE = 0.45
_ENGINE: Any | None = None
_ENGINE_LOCK = threading.Lock()


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as error:
                raise RuntimeError("当前环境缺少扫描 PDF OCR 组件 rapidocr_onnxruntime。") from error
            _ENGINE = RapidOCR()
    return _ENGINE


def text_is_sparse(text: str, threshold: int = OCR_TEXT_THRESHOLD) -> bool:
    visible = re.sub(r"\s+", "", str(text or ""))
    return len(visible) < max(1, int(threshold))


def ocr_pdf_page(page: fitz.Page, *, scale: float = 2.0) -> tuple[str, float]:
    """OCR one rendered PDF page and return text plus mean confidence."""

    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(max(1.0, min(float(scale), 3.0)), max(1.0, min(float(scale), 3.0))),
        colorspace=fitz.csRGB,
        alpha=False,
    )
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    result, _elapsed = _engine()(image)
    if not result:
        return "", 0.0
    lines: list[str] = []
    confidences: list[float] = []
    for item in result:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        text = str(item[1] or "").strip()
        try:
            confidence = float(item[2])
        except (TypeError, ValueError):
            confidence = 0.0
        if text and confidence >= OCR_MIN_CONFIDENCE:
            lines.append(text)
            confidences.append(confidence)
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n".join(lines), round(mean_confidence, 4)


def extract_pdf_page_text(page: fitz.Page, *, allow_ocr: bool = True) -> dict[str, Any]:
    native = page.get_text("text", sort=True).strip()
    if not allow_ocr or not text_is_sparse(native):
        return {"text": native, "method": "text_layer", "ocr_confidence": None}
    try:
        ocr_text, confidence = ocr_pdf_page(page)
    except (RuntimeError, OSError, ValueError) as error:
        return {
            "text": native,
            "method": "unreadable",
            "ocr_confidence": None,
            "ocr_error": str(error),
        }
    if text_is_sparse(ocr_text, threshold=12):
        return {"text": native, "method": "unreadable", "ocr_confidence": confidence}
    return {"text": ocr_text, "method": "ocr", "ocr_confidence": confidence}
