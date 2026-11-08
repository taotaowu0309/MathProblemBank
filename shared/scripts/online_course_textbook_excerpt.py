"""Local, page-bounded textbook supplements for existing course writing tasks.

This does not reconstruct recordings or invoke a model. Original PDF artwork is
clipped without resampling, with source/page/rectangle and exact-byte provenance.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import fitz

from shared.scripts.application_paths import resolve_stored_path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Packaging a book crops every figure from the same source PDF (up to 100 MB);
# hash each unchanged source once per process instead of once per figure.
_SOURCE_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def _source_sha(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key not in _SOURCE_SHA_CACHE:
        _SOURCE_SHA_CACHE[key] = _sha(path)
    return _SOURCE_SHA_CACHE[key]


def crop_original_figure(source: Path, source_sha256: str, figure: dict[str, Any],
                         target: Path) -> dict[str, Any]:
    """Use MinerU's page-local normalized box without rasterizing the source."""
    number = int(figure["page_number"])
    box = list(figure["bbox"])
    if len(box) != 4 or any(not 0 <= float(v) <= 1000 for v in box):
        raise ValueError("MinerU figure needs a valid normalized 0..1000 bbox.")
    if _source_sha(source) != source_sha256:
        raise ValueError("Original textbook source hash changed.")
    with fitz.open(source) as document:
        if not 1 <= number <= len(document):
            raise ValueError("Original figure page falls outside the source PDF.")
        page = document[number - 1]
        if page.rotation:
            raise ValueError("Rotated source pages require a reviewed original-page crop.")
        visible = fitz.Rect(page.rect.width * float(box[0]) / 1000,
                            page.rect.height * float(box[1]) / 1000,
                            page.rect.width * float(box[2]) / 1000,
                            page.rect.height * float(box[3]) / 1000)
        # MinerU sees the rotated display; show_pdf_page clips unrotated space.
        rectangle = visible * page.derotation_matrix
        if rectangle.is_empty or not page.mediabox.contains(rectangle):
            raise ValueError("Original figure bbox falls outside its source page.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not verified_source_crop(target):
                raise ValueError("An existing original crop failed provenance verification.")
            previous = json.loads(target.with_suffix(".source.json").read_text(encoding="utf-8"))
            if (previous["source_sha256"] != source_sha256 or
                    previous["physical_page"] != number or previous["bbox"] != list(rectangle)):
                raise ValueError("An existing crop belongs to another source region.")
            return previous
        if not target.exists():
            with fitz.open() as output:
                crop = output.new_page(width=visible.width, height=visible.height)
                crop.show_pdf_page(crop.rect, document, number - 1,
                                   clip=rectangle)
                output.save(target, garbage=4, deflate=True)
    provenance = {"kind": "original_pdf_crop", "source_pdf": str(source),
                  "source_sha256": source_sha256, "physical_page": number,
                  "bbox": list(rectangle), "sha256": _sha(target),
                  "title": str(figure.get("caption") or figure["figure_id"]),
                  "resampled": False}
    target.with_suffix(".source.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return provenance


def prepare_excerpt(service: Any, subsection_id: int, source_pdf: str,
                    pages: list[int], *, title: str, coverage: str,
                    figures: list[dict[str, Any]] | None = None,
                    book_ref: str = "", subject_name: str = "") -> dict[str, Any]:
    pages = list(dict.fromkeys(int(p) for p in pages))
    if not pages or len(pages) > 4 or min(pages) < 1:
        raise ValueError("Each textbook excerpt must name 1–4 physical PDF pages.")
    task = service.chatgpt_work_task(subsection_id)
    if not task["task_ready"]:
        raise ValueError("Prepare the existing writing task before attaching an excerpt.")
    source = Path(source_pdf).resolve(strict=True)
    if book_ref:
        from .ai_agent_repository import GlobalProblemRepository
        books = GlobalProblemRepository().list_textbooks(subject_name, book_ref)["textbooks"]
        matches = [book for book in books if book["book_code"] == book_ref]
        if len(matches) != 1 or Path(matches[0]["pdf_path"]).resolve() != source:
            raise ValueError("The selected PDF does not match the registered textbook binding.")
    source_hash = _sha(source)
    figures = figures or []
    with fitz.open(source) as document:
        if max(pages) > len(document):
            raise ValueError("Textbook page number is outside the source PDF.")
        for figure in figures:
            page_number = int(figure["page"])
            if page_number not in pages:
                raise ValueError("A figure must belong to one of the selected pages.")
            rectangle = fitz.Rect(figure["bbox"])
            if rectangle.is_empty or not document[page_number - 1].rect.contains(rectangle):
                raise ValueError("Figure rectangle is outside the source page.")
        identity = hashlib.sha256(json.dumps(
            [source_hash, pages, figures, title, coverage], sort_keys=True,
            ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
        task_dir = Path(task["task_dir"])
        reference_root = task_dir / "reference_materials"
        target = reference_root / ("excerpt_" + identity)
        target.mkdir(parents=True, exist_ok=True)
        excerpt = target / "SOURCE_PAGES.pdf"
        if not excerpt.exists():
            with fitz.open() as output:
                for number in pages:
                    output.insert_pdf(document, from_page=number - 1, to_page=number - 1)
                output.save(excerpt, garbage=4, deflate=True)
        text_file = target / "CONTENT.md"
        text_file.write_text(
            f"# {title}\n\nSource: {source}\nSHA256: {source_hash}\n\n"
            f"Explicitly assigned coverage: {coverage}\n\n" + "\n\n".join(
                f"## Physical PDF page {number}\n\n{document[number-1].get_text()}"
                for number in pages), encoding="utf-8")
        assets = []
        for index, figure in enumerate(figures, 1):
            rectangle = fitz.Rect(figure["bbox"])
            number = int(figure["page"])
            asset = target / f"figure_{index:02}.pdf"
            if not asset.exists():
                with fitz.open() as output:
                    page = output.new_page(width=rectangle.width, height=rectangle.height)
                    page.show_pdf_page(page.rect, document, number - 1, clip=rectangle)
                    output.save(asset, garbage=4, deflate=True)
            provenance = {
                "kind": "original_pdf_crop", "source_pdf": str(source),
                "source_sha256": source_hash, "physical_page": number,
                # The page excerpt beside the crop lets the other machine, which
                # cannot open this machine's absolute source path, verify it.
                "excerpt_file": excerpt.name, "excerpt_sha256": _sha(excerpt),
                "excerpt_page_index": pages.index(number),
                "bbox": list(rectangle), "sha256": _sha(asset),
                "title": str(figure.get("title") or title),
                "resampled": False,
            }
            asset.with_suffix(".source.json").write_text(
                json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
            assets.append({**provenance, "file": str(asset)})
    index_file = reference_root / "REFERENCE_INDEX.json"
    payload = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {"parts": []}
    part = {
        "part_id": "local_excerpt_" + identity, "title": title,
        "physical_pages": pages, "coverage": coverage,
        "source_sha256": source_hash,
        "book_ref": book_ref, "subject_name": subject_name,
        "content_file": text_file.relative_to(task_dir).as_posix(),
        "binary_file": excerpt.relative_to(task_dir).as_posix(),
        "figure_asset_files": [Path(a["file"]).relative_to(task_dir).as_posix() for a in assets],
    }
    payload["parts"] = [p for p in payload.get("parts", []) if p.get("part_id") != part["part_id"]] + [part]
    index_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    templates = Path(__file__).resolve().parents[1] / "templates"
    for source_name, relative in [
        ("online_course_latex_drawing_rules.txt", "LATEX_DRAWING_RULES.txt"),
        ("online_course_reference_coverage_prompt.txt", "reference_materials/REFERENCE_COVERAGE_PROMPT.txt"),
    ]:
        (task_dir / relative).write_text((templates / source_name).read_text(encoding="utf-8"), encoding="utf-8")
    task_file = Path(task["task_file"])
    task_text = task_file.read_text(encoding="utf-8")
    inventory = re.findall(r"^- `([^`]+)`", task_text, re.M)
    inventory += [p.relative_to(task_dir).as_posix() for p in reference_root.rglob("*") if p.is_file()]
    task_text = task_text.split("FILE INVENTORY", 1)[0]
    task_file.write_text(task_text + "FILE INVENTORY\n\n" + "\n".join(
        f"- `{name}`" for name in sorted(set(inventory))) + "\n", encoding="utf-8")
    manifest_file = task_dir / "package_manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["local_textbook_excerpts"] = payload["parts"]
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if json.loads(index_file.read_text(encoding="utf-8"))["parts"][-1] != part:
        raise RuntimeError("Textbook excerpt readback failed.")
    return {"subsection_id": subsection_id, "index_file": str(index_file),
            "part": part, "figures": assets, "readback_verified": True,
            "model_calls": 0}


def verified_source_crop(path: Path) -> bool:
    """Recognize only a locally indexed, unchanged crop of an unchanged source."""
    provenance_file = path.with_suffix(".source.json")
    if not provenance_file.is_file():
        return False
    try:
        item = json.loads(provenance_file.read_text(encoding="utf-8"))
        if item.get("kind") != "original_pdf_crop" or item.get("resampled") is not False:
            return False
        if _sha(path) != item["sha256"]:
            return False
        source = resolve_stored_path(str(item["source_pdf"]))
        number = int(item["physical_page"])
        if source.is_file():
            # A source that changed since the crop was taken is rejected.
            if _sha(source) != item["source_sha256"]:
                return False
        else:
            excerpt = path.parent / str(item.get("excerpt_file") or "")
            if not (item.get("excerpt_file") and excerpt.is_file()
                    and _sha(excerpt) == item.get("excerpt_sha256")):
                return False
            source, number = excerpt, int(item["excerpt_page_index"]) + 1
        with fitz.open(source) as original, fitz.open(path) as crop:
            rectangle = fitz.Rect(item["bbox"])
            return (0 < number <= len(original) and not rectangle.is_empty
                    and original[number-1].rect.contains(rectangle)
                    and len(crop) == 1
                    and abs(crop[0].rect.width - rectangle.width) < .01
                    and abs(crop[0].rect.height - rectangle.height) < .01)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False
