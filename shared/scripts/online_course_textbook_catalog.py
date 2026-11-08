"""Reusable course-wide textbook inventory and writer-reviewed figure groups.

Uses existing imported sections and original crops; never parses PDFs again,
calls a model, changes recordings, or mistakes extraction for writer review.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import fitz

from .online_course_textbook_excerpt import crop_original_figure, verified_source_crop


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reviewed_section_annotations(root: Path) -> dict[int, dict[str, Any]]:
    """Return version-bound writer notes; callers must match both source hashes."""
    target = root / "TEXTBOOK_CATALOG.json"
    if not target.is_file():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    return {int(a["part_id"]): a for a in data.get("work_review", {}).get("section_annotations", [])}


def save_page_recoveries(service: Any, course_id: int, material_id: int,
                         source_sha256: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    from .online_course_service import _atomic_json, _sha256_file
    material = next((m for m in service.reference_materials(course_id)
                     if int(m["id"]) == material_id), None)
    if not material or material["source_sha256"] != source_sha256:
        raise ValueError("Page recovery must name an unchanged imported source.")
    source = Path(str(material["stored_path"]))
    if _sha256_file(source) != source_sha256:
        raise ValueError("Page recovery source hash changed.")
    root = Path(str(service.course(course_id)["storage_dir"])) / "reference_materials"
    chunks = json.loads((root / "chunks" / f"material_{material_id:04d}.json").read_text(encoding="utf-8"))
    occupied = {int(c["page_number"]) for c in chunks if str(c.get("content") or "").strip()
                and c.get("extraction_method") != "work_visual_transcription"}
    numbers = [int(p["page_number"]) for p in pages]
    if not numbers or len(set(numbers)) != len(numbers):
        raise ValueError("Page recovery requires unique page numbers.")
    for item in pages:
        number = int(item["page_number"])
        if number in occupied or not 1 <= number <= int(material["page_count"]):
            raise ValueError("Only an empty, in-range extracted page can be supplemented.")
        if not 32 <= len(str(item.get("content") or "").strip()) <= 60000 or not item.get("visual_evidence"):
            raise ValueError("Page recovery needs substantial transcription and original-page visual evidence.")
    target = root / "page_recoveries" / (source_sha256 + ".json")
    previous = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    merged = {int(p["page_number"]): p for p in previous.get("pages", [])}
    merged.update({int(p["page_number"]): p for p in pages})
    payload = {"source_sha256": source_sha256, "material_id": material_id,
               "method": "work_visual_transcription", "pages": [merged[n] for n in sorted(merged)],
               "status": "saved_requires_reference_reanalysis", "model_calls": 0}
    if previous and previous != payload:
        _atomic_json(root / "page_recovery_history" / (_digest(previous) + ".json"), previous)
    _atomic_json(target, payload)
    if json.loads(target.read_text(encoding="utf-8")) != payload:
        raise RuntimeError("Page recovery readback failed.")
    return {"recovery_file": str(target), "page_numbers": sorted(merged),
            "source_sha256": source_sha256, "model_calls": 0, "readback_verified": True,
            "next_step": "reanalyze_online_course_reference_materials reuses MinerU checkpoints and applies this labelled overlay"}


def apply_page_recoveries(chunks: list[dict[str, Any]], source: Path,
                          page_count: int, reference_root: Path) -> list[dict[str, Any]]:
    from .online_course_service import _sha256_file
    sha = _sha256_file(source)
    target = reference_root / "page_recoveries" / (sha + ".json")
    if not target.is_file():
        return chunks
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("source_sha256") != sha or payload.get("method") != "work_visual_transcription":
        raise ValueError("Page recovery provenance mismatch.")
    occupied = {int(c.get("page_number") or 0) for c in chunks
                if not c.get("source_span_recoveries")}
    recovered = []
    for item in payload["pages"]:
        number = int(item["page_number"])
        if number in occupied or not 1 <= number <= page_count:
            raise ValueError("Recovery cannot overwrite a nonempty MinerU page.")
        for content in re.split(r"(?m)(?=^#{1,6}\s)", str(item["content"])):
            if content.strip():
                recovered.append({"page_number": number, "content": content.strip(),
                    "locator": f"PDF page {number} (Work original-page transcription; MinerU returned empty text)",
                    "extraction_method": "work_visual_transcription", "source_sha256": sha,
                    "supplement_to_recovered_ocr": any(int(c.get("page_number") or 0) == number for c in chunks),
                    "recovery_sha256": _digest(item), "visual_evidence": item["visual_evidence"],
                    "quality_notes": item.get("quality_notes", ""), "multimodal_elements": []})
    return sorted([*chunks, *recovered], key=lambda c: int(c.get("page_number") or 0))


def prepare_catalog(service: Any, course_id: int, review_file: str = "",
                    preview_figure_ids: list[str] | None = None,
                    discover_inline_diagrams: bool = False) -> dict[str, Any]:
    from .online_course_service import _atomic_json, _sha256_file

    course = service.course(course_id)
    root = Path(str(course["storage_dir"])) / "reference_materials"
    materials = service.reference_materials(course_id)
    books, figures = [], {}
    fingerprint_entries = []
    for material in materials:
        material_id = int(material["id"])
        source = Path(str(material["stored_path"]))
        if not source.is_file() or _sha256_file(source) != material["source_sha256"]:
            raise ValueError("Textbook source is missing or changed; catalog not updated.")
        chunks_path = root / "chunks" / f"material_{material_id:04d}.json"
        chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
        with service.connect(rows=True) as connection:
            parts = [dict(r) for r in connection.execute(
                "SELECT * FROM course_reference_parts WHERE course_id=? AND material_id=? ORDER BY part_order,id",
                (int(course_id), material_id)).fetchall()]
        sections = []
        block_owners: dict[str, set[int]] = {}
        for part in parts:
            content = Path(str(part["content_path"]))
            if _sha256_file(content) != part["content_sha256"]:
                raise ValueError("Textbook section content changed; catalog not updated.")
            keys = json.loads(str(part["chunk_keys_json"]))
            selected = [c for c in chunks if c["chunk_key"] in keys]
            for chunk in selected:
                for block_id in chunk.get("mineru_block_ids", []):
                    block_owners.setdefault(block_id, set()).add(int(part["id"]))
            sections.append({"part_id": int(part["id"]), "title": part["title"],
                "physical_pages": sorted({int(c.get("page_number") or 0) for c in selected}),
                "chunk_ids": keys, "content_file": content.relative_to(root).as_posix(),
                "content_sha256": part["content_sha256"]})
            context = content.with_name(content.stem + "_FIGURE_CONTEXT.json")
            if context.is_file():
                for figure in json.loads(context.read_text(encoding="utf-8")).get("figures", []):
                    item = dict(figure)
                    figure_id = str(item["figure_id"])
                    asset = content.parent / str(item.get("source_asset_file") or "")
                    valid = (asset.is_file() and _sha256_file(asset) == item.get("packaged_asset_sha256")
                             and verified_source_crop(asset))
                    item.update(material_id=material_id, source_sha256=material["source_sha256"],
                        part_ids=[int(part["id"])], original_crop_verified=valid,
                        review_status="unreviewed")
                    item["asset_file"] = asset.relative_to(root).as_posix() if asset.is_file() else ""
                    if figure_id in figures:
                        figures[figure_id]["part_ids"].append(int(part["id"]))
                    else:
                        figures[figure_id] = item
        discovery_file = root / "discovered_figures" / (str(material["source_sha256"]) + ".json")
        if discover_inline_diagrams:
            from .online_course_reference_mineru import _chunks_from_content_list
            from .application_paths import resolve_stored_path
            manifests = sorted((root / "mineru_cache" / str(material["source_sha256"])).glob(
                "*/MPB_MINERU_MANIFEST.json"), key=lambda p:p.stat().st_mtime_ns, reverse=True)
            if not manifests:
                raise ValueError("Inline diagram discovery requires an existing complete MinerU cache.")
            metadata = json.loads(manifests[0].read_text(encoding="utf-8"))
            content_list = resolve_stored_path(str(metadata["content_list_path"]))
            if (not metadata.get("complete") or metadata.get("source_sha256") != material["source_sha256"]
                    or _sha256_file(content_list) != metadata["content_list_sha256"]):
                raise ValueError("Inline diagram cache provenance changed; recognition was not launched.")
            _, projected, _ = _chunks_from_content_list(content_list,
                                                       source_sha256=str(material["source_sha256"]))
            discoveries = {}
            for chunk in projected:
                for raw in chunk.get("multimodal_elements", []):
                    fid = str(raw["figure_id"])
                    if fid in figures or fid in discoveries:
                        continue
                    item = {k:v for k,v in raw.items() if k != "source_asset_path"}
                    asset = root / "catalog_figures" / (fid + ".pdf")
                    provenance = crop_original_figure(source, str(material["source_sha256"]), item, asset)
                    owners = block_owners.get(str(item.get("mineru_block_id") or ""), set())
                    item.update(material_id=material_id, source_sha256=material["source_sha256"],
                        part_ids=sorted(owners), asset_file=asset.relative_to(root).as_posix(),
                        packaged_asset_sha256=provenance["sha256"], original_crop_verified=True,
                        original_crop_provenance=provenance, review_status="unreviewed",
                        discovery_method="local_projection_of_existing_mineru_cache")
                    discoveries[fid] = item
            _atomic_json(discovery_file, {"source_sha256":material["source_sha256"],
                "content_list_sha256":metadata["content_list_sha256"],
                "figures":list(discoveries.values()), "model_calls":0, "recognition_runs":0})
        if discovery_file.is_file():
            discovered = json.loads(discovery_file.read_text(encoding="utf-8"))
            if discovered["source_sha256"] != material["source_sha256"]:
                raise ValueError("Discovered figure source changed.")
            for item in discovered.get("figures", []):
                asset = (root / str(item["asset_file"])).resolve()
                if not asset.is_relative_to(root.resolve()):
                    raise ValueError("Discovered figure escapes the reference folder.")
                item["original_crop_verified"] = (asset.is_file() and
                    _sha256_file(asset) == item["packaged_asset_sha256"] and verified_source_crop(asset))
                figures.setdefault(str(item["figure_id"]), item)
        books.append({"material_id": material_id, "filename": material["original_filename"],
            "source_sha256": material["source_sha256"], "page_count": material["page_count"],
            "extraction_status": material["extraction_status"],
            "extraction_error": material["extraction_error"], "sections": sections,
            "chunk_count": len(chunks), "review_status": "unreviewed",
            "pages_without_chunks": sorted(set(range(1, int(material["page_count"]) + 1)) -
                {int(c.get("page_number") or 0) for c in chunks}),
            "work_recovered_pages": sorted({int(c["page_number"]) for c in chunks
                if c.get("extraction_method") == "work_visual_transcription"}),
            "empty_body_region_pages": sorted({int(p) for c in chunks
                for p in (c.get("mineru_manifest", {}).get("coverage", {}).get("empty_body_pages") or [])})})
        fingerprint_entries.append([material_id, material["source_sha256"], _sha256_file(chunks_path),
                                    [(s["part_id"], s["content_sha256"]) for s in sections]])
    fingerprint = _digest([fingerprint_entries, list(figures.values())])
    material_fingerprints = {str(b[0]): _digest([b, [f for f in figures.values()
        if f["material_id"] == b[0]]]) for b in fingerprint_entries}
    target = root / "TEXTBOOK_CATALOG.json"
    previous = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    old_fingerprints = previous.get("material_fingerprints", {})
    unchanged_materials = {int(k) for k, value in old_fingerprints.items()
                           if material_fingerprints.get(k) == value}
    review = {}
    if previous.get("source_fingerprint") == fingerprint:
        review = previous.get("work_review", {})
    elif unchanged_materials:
        review = json.loads(json.dumps(previous.get("work_review", {})))
        review["book_reviews"] = [b for b in review.get("book_reviews", [])
                                  if int(b["material_id"]) in unchanged_materials]
        review["additional_figures"] = [f for f in review.get("additional_figures", [])
                                        if int(f["material_id"]) in unchanged_materials]
        stable_figures = {f["figure_id"] for f in previous.get("figures", [])
                          if int(f["material_id"]) in unchanged_materials}
        stable_parts = {s["part_id"] for b in books if b["material_id"] in unchanged_materials
                        for s in b["sections"]}
        review["section_annotations"] = [a for a in review.get("section_annotations", [])
                                        if int(a["part_id"]) in stable_parts]
        groups = []
        for group in review.get("figure_groups", []):
            group["members"] = [m for m in group.get("members", []) if m["figure_id"] in stable_figures]
            if group.get("preferred_figure_id") not in stable_figures:
                group["preferred_figure_id"] = ""
            if group["members"]:
                groups.append(group)
        review["figure_groups"] = groups
        for mapping in review.get("topic_map", []):
            mapping["part_ids"] = [p for p in mapping.get("part_ids", []) if p in stable_parts]
        review["topic_map"] = [m for m in review.get("topic_map", []) if m["part_ids"]]
    if review_file:
        review = json.loads(Path(review_file).read_text(encoding="utf-8"))
        if review.get("source_fingerprint") != fingerprint:
            raise ValueError("Writer review targets a stale textbook inventory.")
    material_lookup = {int(m["id"]): m for m in materials}
    # Small inline diagrams can be misclassified as equations by OCR. A writer
    # who located one on the original page can register that exact region here,
    # without editing locked extraction results or launching another parser.
    for entry in review.get("additional_figures", []):
        material = material_lookup.get(int(entry["material_id"]))
        if not material or entry.get("source_sha256") != material["source_sha256"]:
            raise ValueError("Additional figure does not match an imported source.")
        if not entry.get("discovery_reason"):
            raise ValueError("Additional figure requires an original-page discovery reason.")
        figure_id = "original-" + _digest([entry["source_sha256"], entry["page_number"], entry["bbox"]])[:24]
        item = {**entry, "figure_id": figure_id, "kind": "image", "part_ids": [],
                "bbox_coordinate_space": "normalized_1000", "review_status": "unreviewed"}
        asset = root / "catalog_figures" / (figure_id + ".pdf")
        provenance = crop_original_figure(Path(str(material["stored_path"])),
                                         str(material["source_sha256"]), item, asset)
        item.update(asset_file=asset.relative_to(root).as_posix(),
                    packaged_asset_sha256=provenance["sha256"],
                    original_crop_verified=verified_source_crop(asset),
                    original_crop_provenance=provenance)
        if figure_id in figures:
            raise ValueError("Repeated additional original figure.")
        figures[figure_id] = item
    reviewed_ids = set()
    for group in review.get("figure_groups", []):
        if not group.get("topic") or not group.get("mathematical_meaning"):
            raise ValueError("Figure groups require a topic and mathematical explanation.")
        members = group.get("members", [])
        ids = {str(m["figure_id"]) for m in members}
        if not ids or not ids <= figures.keys() or reviewed_ids.intersection(ids):
            raise ValueError("Unknown, repeated or empty writer figure group.")
        preferred = group.get("preferred_figure_id")
        if preferred and preferred not in ids:
            raise ValueError("Preferred figure must belong to its semantic group.")
        for member in members:
            if not member.get("reason") or member.get("relationship") not in {
                    "preferred", "equivalent", "complementary", "distinct", "administrative", "not_a_figure", "source_error"}:
                raise ValueError("Each figure needs a reason and an explicit semantic relationship.")
            figures[str(member["figure_id"])]["review_status"] = "reviewed"
        reviewed_ids.update(ids)
    book_lookup = {b["material_id"]: b for b in books}
    for item in review.get("book_reviews", []):
        book = book_lookup.get(int(item["material_id"]))
        if not book or item.get("source_sha256") != book["source_sha256"]:
            raise ValueError("Book review source does not match this inventory.")
        if not item.get("structure_notes") or not item.get("quality_notes"):
            raise ValueError("Book review requires structure and extraction quality notes.")
        book["review_status"] = "reviewed"
        book["book_ref"] = str(item.get("book_ref") or "")
        blanks = set(item.get("confirmed_blank_pages", []))
        if not blanks <= set(book["pages_without_chunks"]):
            raise ValueError("Blank-page review names a page that contains extracted content.")
        book["unresolved_missing_pages"] = sorted(set(book["pages_without_chunks"]) - blanks)
    part_ids = {s["part_id"] for b in books for s in b["sections"]}
    section_lookup = {s["part_id"]: (b, s) for b in books for s in b["sections"]}
    annotated_ids = set()
    for annotation in review.get("section_annotations", []):
        pid = int(annotation["part_id"])
        if pid not in section_lookup or pid in annotated_ids:
            raise ValueError("Section annotation is unknown or repeated.")
        book, section = section_lookup[pid]
        if (annotation.get("source_sha256") != book["source_sha256"] or
                annotation.get("content_sha256") != section["content_sha256"] or
                not annotation.get("reason")):
            raise ValueError("Section annotation must match unchanged source and section content.")
        section["work_annotation"] = annotation
        annotated_ids.add(pid)
    for item in review.get("topic_map", []):
        if not item.get("topic") or not set(item.get("part_ids", [])) <= part_ids:
            raise ValueError("Topic mapping names unknown reference sections.")
    ready = bool(books) and all(b["review_status"] == "reviewed" and
        b["extraction_status"] in {"ready", "ready_unmapped"} and
        not b.get("unresolved_missing_pages", b["pages_without_chunks"]) for b in books)
    ready = ready and len(reviewed_ids) == len(figures) and all(
        f["original_crop_verified"] for f in figures.values()) and bool(review.get("topic_map"))
    payload = {"schema_version": 1, "course_id": course_id, "source_fingerprint": fingerprint,
        "material_fingerprints": material_fingerprints,
        "status": "reviewed_inventory" if ready else "needs_work_review",
        "books": books, "figures": list(figures.values()), "work_review": review,
        "selection_policy": "Choose for the current lecture; preserve originals and complementary alternatives. No fixed image count; never redraw by default.",
        "counts": {"books": len(books), "sections": sum(len(b["sections"]) for b in books),
                   "figures": len(figures), "reviewed_figures": len(reviewed_ids)}, "model_calls": 0}
    if target.is_file() and previous != payload:
        _atomic_json(root / "catalog_history" / (_sha256_file(target) + ".json"), previous)
    _atomic_json(target, payload)
    if json.loads(target.read_text(encoding="utf-8")) != payload:
        raise RuntimeError("Textbook catalog readback failed.")
    guide = root / "TEXTBOOK_GUIDE.md"
    lines = ["# 教材预处理与选图指南", "", f"课程 {course_id}；状态 `{payload['status']}`。",
        "按课堂主线选读对应节和图；保留所有原件，不要求全部插图。这里的预处理完成不表示全书已写入讲义。",
        "正式来源、完整节目录、图裁框及哈希见 [TEXTBOOK_CATALOG.json](TEXTBOOK_CATALOG.json)。", ""]
    for b in books:
        note = next((n for n in review.get("book_reviews", []) if int(n["material_id"]) == b["material_id"]), {})
        lines += [f"## {b.get('book_ref') or b['material_id']} — {b['filename']}", "",
            f"{b['page_count']} 页；{len(b['sections'])} 节；{b['review_status']}。",
            str(note.get("structure_notes", "尚待Work核对")), str(note.get("quality_notes", "")), ""]
    lines += ["## 按主题读教材", ""]
    for mapping in review.get("topic_map", []):
        lines += [f"### {mapping['topic']}", "", str(mapping.get("writing_guidance", ""))]
        for pid in mapping["part_ids"]:
            b, s = section_lookup[pid]
            title = s.get("work_annotation", {}).get("title") or s["title"]
            lines.append(f"- {b.get('book_ref') or b['material_id']}：[{title}]({s['content_file']})（物理页 {min(s['physical_pages'])}–{max(s['physical_pages'])}）")
        lines.append("")
    lines += ["## 原图按数学含义分组", ""]
    for group in review.get("figure_groups", []):
        lines += [f"### {group['topic']}", "", group["mathematical_meaning"]]
        for member in group["members"]:
            f = figures[member["figure_id"]]
            lines.append(f"- [{member['relationship']}] [资料{f['material_id']}第{f['page_number']}页原图]({f['asset_file']})：{member['reason']}")
        lines.append("")
    guide.write_text("\n".join(lines) + "\n", encoding="utf-8")
    previews = []
    ids = list(dict.fromkeys(preview_figure_ids or []))
    if len(ids) > 4 or not set(ids) <= figures.keys():
        raise ValueError("Preview requires at most four known, precisely located figures.")
    for figure_id in ids:
        figure = figures[figure_id]
        asset = root / figure["asset_file"]
        if not verified_source_crop(asset):
            raise ValueError("Only a verified original crop can be previewed.")
        preview = root / "figure_previews" / (figure["packaged_asset_sha256"] + ".png")
        if not preview.is_file():
            preview.parent.mkdir(parents=True, exist_ok=True)
            with fitz.open(asset) as document:
                document[0].get_pixmap(dpi=200, alpha=False).save(preview)
        previews.append({"kind":"image", "name":figure_id, "path":str(preview),
                         "sha256":_sha256_file(preview), "mime_type":"image/png",
                         "page_number":figure["page_number"], "material_id":figure["material_id"],
                         "bbox":figure.get("bbox", []), "caption":figure.get("caption", ""),
                         "heading_path":figure.get("heading_path", []),
                         "context_excerpt":" ".join(figure.get("context_before", []) +
                             figure.get("context_after", []))[:1800]})
    return {"course_id": course_id, "catalog_file": str(target), "guide_file": str(guide), "source_fingerprint": fingerprint,
            "status": payload["status"], "counts": payload["counts"],
            "visual_evidence": previews,
            "model_calls": 0, "readback_verified": True}
