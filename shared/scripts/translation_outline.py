from __future__ import annotations

from dataclasses import dataclass
import html
from pathlib import Path
import re
from typing import Any


_TAG_PATTERN = re.compile(r"<[^>]+>")
_SPACE_PATTERN = re.compile(r"\s+")
_CHAPTER_HEADING = re.compile(r"^(?:chapter\s+)?(\d+)\s+(.+)$", re.I)
_SECTION_HEADING = re.compile(r"^(\d+\.(?:\d+|[A-Z]))(?:\s+(.+))?$", re.I)
_APPENDIX_SECTION_HEADING = re.compile(r"^([A-Z]\.\d+)(?:\s+(.+))?$", re.I)


@dataclass(frozen=True, slots=True)
class _Heading:
    page: int
    level: int
    text: str
    number: str
    title: str


@dataclass(frozen=True, slots=True)
class _TocEntry:
    index: int
    level: int
    title: str
    page: int
    parent_level_one: int


def catalog_plain_text(value: object) -> str:
    """Return a single-line label suitable for a Qt tree/table.

    MinerU Markdown may legitimately contain inline HTML, but a QTreeWidget is
    not a Markdown renderer.  Convert superscripts/subscripts into readable
    plain notation and never leak raw tags into catalog labels.
    """

    text = html.unescape(str(value or "").strip())
    text = text.replace("<sub>A</sub>", "A")

    def superscript(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        return ("^" + content) if content.isascii() else content

    text = re.sub(
        r"<sup>\s*([^<]+?)\s*</sup>",
        superscript,
        text,
        flags=re.I,
    )
    text = re.sub(
        r"<sub>\s*([^<]+?)\s*</sub>",
        lambda match: "_" + match.group(1).strip(),
        text,
        flags=re.I,
    )
    text = _TAG_PATTERN.sub("", text)
    repairs = {
        "ǰ��": "前言",
        "��л": "致谢",
        "Pad��": "Padé",
        "Hilbert��s": "Hilbert's",
        "Riesz��s": "Riesz's",
        "Baire��s": "Baire's",
        "������ѡ������֤���Ƿ�ֵ�����Σ�": "依赖于选择公理的证明是否值得信任？",
    }
    for damaged, repaired in repairs.items():
        text = text.replace(damaged, repaired)
    text = re.sub(r"Pad\ufffd+", "Padé", text)
    text = re.sub(r"(?<=[A-Za-z])\ufffd+s\b", "'s", text)
    text = text.replace(r"\*", "*")
    text = text.replace("$", "")
    return _SPACE_PATTERN.sub(" ", text).strip()


def _split_numbered_heading(value: object) -> tuple[str, str]:
    text = catalog_plain_text(value)
    match = _SECTION_HEADING.match(text) or _APPENDIX_SECTION_HEADING.match(text)
    if match:
        return match.group(1), str(match.group(2) or "").strip()
    match = _CHAPTER_HEADING.match(text)
    if match:
        return match.group(1), match.group(2).strip()
    return "", text


def _content_headings(content_items: list[dict[str, Any]]) -> list[_Heading]:
    headings: list[_Heading] = []
    for item in content_items:
        if str(item.get("type") or "").casefold() != "text":
            continue
        level = int(item.get("text_level") or 0)
        if level <= 0:
            continue
        text = catalog_plain_text(item.get("text") or item.get("content") or "")
        if not text:
            continue
        number, title = _split_numbered_heading(text)
        headings.append(
            _Heading(
                page=int(item.get("page_idx") or 0) + 1,
                level=level,
                text=text,
                number=number,
                title=title,
            )
        )
    return headings


def _toc_entries(document: Any) -> list[_TocEntry]:
    result: list[_TocEntry] = []
    parent_level_one = -1
    for index, raw in enumerate(document.get_toc(simple=True)):
        if len(raw) < 3:
            continue
        level = int(raw[0] or 0)
        page = int(raw[2] or 0)
        if level <= 0 or page <= 0 or page > int(document.page_count):
            continue
        if level == 1:
            parent_level_one = index
        result.append(
            _TocEntry(
                index=index,
                level=level,
                title=catalog_plain_text(raw[1]),
                page=page,
                parent_level_one=parent_level_one,
            )
        )
    return result


def _headings_on_page(headings: list[_Heading], page: int) -> list[_Heading]:
    return [heading for heading in headings if heading.page == page]


def _main_chapter_heading(
    headings: list[_Heading], page: int, expected_number: int
) -> _Heading | None:
    for heading in _headings_on_page(headings, page):
        if heading.number == str(expected_number) and heading.title:
            return heading
    return None


def _appendix_letter(
    entries: list[_TocEntry], headings: list[_Heading], entry: _TocEntry
) -> str:
    children = [
        child
        for child in entries
        if child.parent_level_one == entry.index and child.level == 2
    ]
    child_pages = {child.page for child in children}
    for heading in headings:
        if heading.page not in child_pages and heading.page != entry.page:
            continue
        match = re.match(r"^([A-Z])\.\d+$", heading.number, re.I)
        if match:
            return match.group(1).upper()
    for heading in _headings_on_page(headings, entry.page):
        match = re.match(r"^([A-Z])(?:\s+|$)", heading.text)
        if match:
            return match.group(1).upper()
    return ""


def _chapter_record(
    *,
    identifier: str,
    number: str,
    title: str,
    page_start: int,
    toc_parent: int | None,
    kind: str,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "number": number,
        "title": catalog_plain_text(title),
        "page_start": int(page_start),
        "page_end": int(page_start),
        "pdf_path": "",
        "sections": [],
        "outline_kind": kind,
        "_toc_parent": toc_parent,
    }


def _section_record(
    chapter: dict[str, Any],
    *,
    ordinal: int,
    number: str,
    title: str,
    page_start: int,
) -> dict[str, Any]:
    identifier = f"section-{catalog_plain_text(chapter['id']).removeprefix('chapter-')}-{ordinal:03d}"
    return {
        "id": identifier,
        "number": catalog_plain_text(number),
        "title": catalog_plain_text(title) or f"Section {number}",
        "page_start": int(page_start),
        "page_end": int(page_start),
        "raw_markdown": "",
        "corrected_markdown": "",
        "agent_report": "",
        "subsections": [],
        "figure_ids": [],
        "_linked_figure_ids": [],
        "_blocks": [],
    }


def _section_heading_for_entry(
    headings: list[_Heading],
    entry: _TocEntry,
    chapter_number: str,
    ordinal: int,
) -> tuple[str, str]:
    candidates = []
    for heading in _headings_on_page(headings, entry.page):
        number = heading.number
        if re.fullmatch(rf"{re.escape(chapter_number)}\.(?:\d+|[A-Z])", number, re.I):
            candidates.append(heading)
    if candidates:
        candidate = candidates[0]
        number = candidate.number
    elif chapter_number.isdigit():
        number = f"{chapter_number}.{ordinal}"
    else:
        number = f"{chapter_number}.{ordinal}"
    bookmark_title = catalog_plain_text(entry.title)
    candidate_title = candidates[0].title if candidates else ""
    title = bookmark_title or candidate_title
    if not title or title == number or "�" in title:
        title = candidate_title or f"Section {number}"
    return number, title


def _front_matter_chapter(
    headings: list[_Heading], first_main_page: int
) -> dict[str, Any] | None:
    if first_main_page <= 1:
        return None
    chapter_heading = next(
        (
            heading
            for heading in headings
            if heading.number == "0"
            and 1 <= heading.page < first_main_page
            and catalog_plain_text(heading.title)
        ),
        None,
    )
    chapter = _chapter_record(
        identifier="chapter-front",
        number="0",
        # Preserve the actual source heading (often ``Preface``). A synthetic
        # implementation label must not replace a real title and leak into the
        # translated book.
        title=(chapter_heading.title if chapter_heading is not None else "Front Matter"),
        page_start=1,
        toc_parent=None,
        kind="front_matter",
    )
    section_headings: list[_Heading] = []
    seen_numbers: set[str] = set()
    for heading in headings:
        if not 1 <= heading.page < first_main_page:
            continue
        if not re.fullmatch(r"0\.\d+", heading.number):
            continue
        if heading.number in seen_numbers:
            continue
        seen_numbers.add(heading.number)
        section_headings.append(heading)
    initial_end = min((heading.page for heading in section_headings), default=first_main_page) - 1
    if initial_end >= 1:
        chapter["sections"].append(
            _section_record(
                chapter,
                ordinal=1,
                number="0.0",
                title="Cover and Contents",
                page_start=1,
            )
        )
    for heading in section_headings:
        chapter["sections"].append(
            _section_record(
                chapter,
                ordinal=len(chapter["sections"]) + 1,
                number=heading.number,
                title=heading.title or f"Section {heading.number}",
                page_start=heading.page,
            )
        )
    if not chapter["sections"]:
        chapter["sections"].append(
            _section_record(
                chapter,
                ordinal=1,
                number="0.1",
                title="Front Matter",
                page_start=1,
            )
        )
    return chapter


def _subsections_for_section(
    section: dict[str, Any], headings: list[_Heading]
) -> list[dict[str, Any]]:
    section_number = str(section["number"])
    matcher = re.compile(rf"^{re.escape(section_number)}\.(\d+)$", re.I)
    candidates: list[_Heading] = []
    seen: set[str] = set()
    for heading in headings:
        if not int(section["page_start"]) <= heading.page <= int(section["page_end"]):
            continue
        if not matcher.fullmatch(heading.number) or heading.number in seen:
            continue
        seen.add(heading.number)
        candidates.append(heading)
    result: list[dict[str, Any]] = []
    for index, heading in enumerate(candidates):
        next_page = (
            candidates[index + 1].page
            if index + 1 < len(candidates)
            else int(section["page_end"]) + 1
        )
        result.append(
            {
                "id": f"subsection-{section['id'].removeprefix('section-')}-{index + 1:03d}",
                "number": heading.number,
                "title": heading.title or f"Subsection {heading.number}",
                "page_start": heading.page,
                "page_end": max(heading.page, next_page - 1),
            }
        )
    return result


def _finalize_ranges(
    chapters: list[dict[str, Any]], page_count: int, headings: list[_Heading]
) -> None:
    for chapter_index, chapter in enumerate(chapters):
        next_chapter_page = (
            int(chapters[chapter_index + 1]["page_start"])
            if chapter_index + 1 < len(chapters)
            else page_count + 1
        )
        chapter["page_end"] = next_chapter_page - 1
        sections = list(chapter.get("sections") or [])
        for section_index, section in enumerate(sections):
            next_section_page = (
                int(sections[section_index + 1]["page_start"])
                if section_index + 1 < len(sections)
                else int(chapter["page_end"]) + 1
            )
            section["page_end"] = max(int(section["page_start"]), next_section_page - 1)
            section["subsections"] = _subsections_for_section(section, headings)
        chapter.pop("_toc_parent", None)


def validate_import_outline(outline: list[dict[str, Any]], page_count: int) -> None:
    sections = [
        section
        for chapter in outline
        for section in chapter.get("sections") or []
    ]
    if not sections:
        raise ValueError("目录没有任何可导入节。")
    ids = [str(section.get("id") or "") for section in sections]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise ValueError("目录包含重复或空的节 ID。")
    numbers = [str(section.get("number") or "") for section in sections]
    if len(numbers) != len(set(numbers)) or any(not value for value in numbers):
        raise ValueError("目录包含重复或空的节编号。")
    expected_page = 1
    for section in sections:
        start = int(section.get("page_start") or 0)
        end = int(section.get("page_end") or 0)
        if start != expected_page or end < start:
            raise ValueError(
                f"节级目录页码不连续：期望从第 {expected_page} 页开始，"
                f"实际为 {section.get('number')} 第 {start}-{end} 页。"
            )
        expected_page = end + 1
        label = f"{section.get('number')} {section.get('title')}"
        if _TAG_PATTERN.search(label):
            raise ValueError(f"目录标签仍含 HTML：{label}")
        if re.match(
            r"^(?:Remark|Theorem|Proposition|Definition|Lemma)\s+"
            r"(?:[A-Z]?\d+(?:\.\d+)*)\b",
            str(section.get("title") or ""),
            re.I,
        ):
            raise ValueError(f"定理环境标题被误识别为节：{label}")
    if expected_page != page_count + 1:
        raise ValueError(
            f"节级目录没有覆盖整本 PDF：结束于第 {expected_page - 1} 页，"
            f"原书共 {page_count} 页。"
        )


def _contents_rows(value: object) -> list[tuple[str, str]]:
    """Join wrapped printed-contents lines and split their terminal page labels."""

    rows: list[tuple[str, str]] = []
    pending = ""
    for raw_line in str(value or "").splitlines():
        line = _SPACE_PATTERN.sub(" ", raw_line).strip()
        if not line:
            continue
        pending = f"{pending} {line}".strip()
        match = re.match(r"^(.+?)\s+(\d+|[ivxlcdm]+)$", pending, re.I)
        if match:
            rows.append((catalog_plain_text(match.group(1)), match.group(2)))
            pending = ""
    return rows


def _contents_body_number(value: str) -> tuple[tuple[int, ...], str]:
    match = re.match(r"^(\d+(?:\.\d+)?)\.?\s+(.+)$", catalog_plain_text(value))
    if not match:
        return (), ""
    return tuple(int(part) for part in match.group(1).split(".")), match.group(2).strip()


def _finalize_printed_contents_ranges(chapters: list[dict[str, Any]], page_count: int) -> None:
    chapters.sort(key=lambda chapter: int(chapter["page_start"]))
    for chapter_index, chapter in enumerate(chapters):
        next_chapter_page = (
            int(chapters[chapter_index + 1]["page_start"])
            if chapter_index + 1 < len(chapters)
            else page_count + 1
        )
        chapter["page_end"] = next_chapter_page - 1
        sections = list(chapter.get("sections") or [])
        for section_index, section in enumerate(sections):
            next_section_page = (
                int(sections[section_index + 1]["page_start"])
                if section_index + 1 < len(sections)
                else int(chapter["page_end"]) + 1
            )
            section["page_end"] = max(int(section["page_start"]), next_section_page - 1)
            subsections = list(section.get("subsections") or [])
            for subsection_index, subsection in enumerate(subsections):
                next_subsection_page = (
                    int(subsections[subsection_index + 1]["page_start"])
                    if subsection_index + 1 < len(subsections)
                    else int(section["page_end"]) + 1
                )
                subsection["page_end"] = max(
                    int(subsection["page_start"]), next_subsection_page - 1
                )
        chapter.pop("_toc_parent", None)


def build_mineru_contents_outline(
    content_items: list[dict[str, Any]],
    page_count: int,
) -> list[dict[str, Any]]:
    """Build an exact outline from a printed Contents block when bookmarks are absent."""

    headings = _content_headings(content_items)
    candidates: list[tuple[int, list[tuple[str, str]]]] = []
    for item in content_items:
        if str(item.get("type") or "").casefold() != "text":
            continue
        text = str(item.get("text") or item.get("content") or "")
        rows = _contents_rows(text)
        chapter_count = sum(
            bool(re.match(r"^Chapter\s+\d+\.?\s+", body, re.I))
            for body, _page in rows
        )
        if chapter_count >= 2:
            candidates.append((chapter_count, rows))
    if not candidates:
        return []
    rows = max(candidates, key=lambda candidate: (candidate[0], len(candidate[1])))[1]

    parsed: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for body, page_label in rows:
        chapter_match = re.match(r"^Chapter\s+(\d+)\.?\s*(.*)$", body, re.I)
        if chapter_match and page_label.isdigit():
            current = {
                "kind": "chapter",
                "number": chapter_match.group(1),
                "title": chapter_match.group(2).strip() or body,
                "printed_page": int(page_label),
                "children": [],
            }
            parsed.append(current)
            continue
        appendix_match = re.match(r"^Appendix\s*:?\s*(.*)$", body, re.I)
        if appendix_match and page_label.isdigit():
            current = {
                "kind": "appendix",
                "number": "A",
                "title": "Appendix"
                + (f": {appendix_match.group(1).strip()}" if appendix_match.group(1).strip() else ""),
                "printed_page": int(page_label),
                "children": [],
            }
            parsed.append(current)
            continue
        back_token = {
            "notes and references": "REF",
            "references": "REF",
            "bibliography": "BIB",
            "symbol glossary": "GLO",
            "glossary": "GLO",
            "index": "IDX",
        }.get(body.casefold())
        if back_token and page_label.isdigit():
            current = None
            parsed.append(
                {
                    "kind": "back_matter",
                    "number": back_token,
                    "title": body,
                    "printed_page": int(page_label),
                    "children": [],
                }
            )
            continue
        parts, title = _contents_body_number(body)
        if current is not None and parts and page_label.isdigit():
            current["children"].append(
                {
                    "parts": parts,
                    "title": title,
                    "printed_page": int(page_label),
                }
            )

    main = [row for row in parsed if row["kind"] == "chapter"]
    if not main:
        return []
    first = main[0]
    first_number = str(first["number"])
    first_title = re.sub(r"\W+", "", str(first["title"]), flags=re.UNICODE).casefold()
    anchor = next(
        (
            heading
            for heading in headings
            if heading.level == 1
            and heading.number == first_number
            and first_title
            and first_title in re.sub(r"\W+", "", heading.title, flags=re.UNICODE).casefold()
        ),
        None,
    )
    if anchor is None:
        return []
    page_offset = int(anchor.page) - int(first["printed_page"])

    def physical_page(printed_page: int) -> int:
        return int(printed_page) + page_offset

    if physical_page(int(first["printed_page"])) <= 1:
        return []

    chapters: list[dict[str, Any]] = []
    front = _chapter_record(
        identifier="chapter-front",
        number="0",
        title="Front Matter",
        page_start=1,
        toc_parent=None,
        kind="front_matter",
    )
    front["sections"].append(
        _section_record(front, ordinal=1, number="0.1", title="Front Matter", page_start=1)
    )
    front_titles = ("foreword", "preface", "contents")
    seen_front_pages = {1}
    for heading in headings:
        lowered = heading.text.casefold()
        if heading.page >= physical_page(int(first["printed_page"])):
            continue
        if not any(lowered.startswith(title) for title in front_titles):
            continue
        if heading.page in seen_front_pages:
            continue
        seen_front_pages.add(heading.page)
        front["sections"].append(
            _section_record(
                front,
                ordinal=len(front["sections"]) + 1,
                number=f"0.{len(front['sections']) + 1}",
                title=heading.text,
                page_start=heading.page,
            )
        )
    chapters.append(front)

    for parsed_chapter in parsed:
        start = physical_page(int(parsed_chapter["printed_page"]))
        if start < 1 or start > int(page_count):
            return []
        number = str(parsed_chapter["number"])
        kind = str(parsed_chapter["kind"])
        identifier = (
            f"chapter-{int(number):03d}"
            if number.isdigit()
            else f"chapter-{number.casefold()}"
        )
        chapter = _chapter_record(
            identifier=identifier,
            number=number,
            title=str(parsed_chapter["title"]),
            page_start=start,
            toc_parent=None,
            kind=kind,
        )
        if kind == "back_matter":
            chapter["sections"].append(
                _section_record(
                    chapter,
                    ordinal=1,
                    number=f"{number}.1",
                    title=str(parsed_chapter["title"]),
                    page_start=start,
                )
            )
            chapters.append(chapter)
            continue

        sections_by_local_number: dict[int, dict[str, Any]] = {}
        for child in parsed_chapter["children"]:
            parts = tuple(child["parts"])
            child_page = physical_page(int(child["printed_page"]))
            if len(parts) == 1:
                local_number = int(parts[0])
                section = _section_record(
                    chapter,
                    ordinal=len(chapter["sections"]) + 1,
                    number=f"{number}.{local_number}",
                    title=str(child["title"]),
                    page_start=(start if not chapter["sections"] else child_page),
                )
                chapter["sections"].append(section)
                sections_by_local_number[local_number] = section
            elif len(parts) == 2:
                section = sections_by_local_number.get(int(parts[0]))
                if section is None:
                    continue
                subsection_number = f"{number}.{parts[0]}.{parts[1]}"
                section["subsections"].append(
                    {
                        "id": "subsection-"
                        + "-".join(
                            (
                                _safe_number_component(number),
                                f"{parts[0]:03d}",
                                f"{parts[1]:03d}",
                            )
                        ),
                        "number": subsection_number,
                        "title": str(child["title"]),
                        "page_start": child_page,
                        "page_end": child_page,
                    }
                )
        if not chapter["sections"]:
            chapter["sections"].append(
                _section_record(
                    chapter,
                    ordinal=1,
                    number=f"{number}.1",
                    title=str(parsed_chapter["title"]),
                    page_start=start,
                )
            )
        chapters.append(chapter)

    _finalize_printed_contents_ranges(chapters, int(page_count))
    validate_import_outline(chapters, int(page_count))
    return chapters


def _safe_number_component(value: object) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "-", str(value or "")).strip("-")
    return text.casefold() or "unit"


def build_pdf_catalog_outline(
    source_pdf: str | Path,
    content_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build import units from PDF bookmarks, using MinerU only to recover numbers.

    PDF bookmarks decide structural boundaries.  MinerU heading levels are too
    noisy to be authoritative, but are useful for recovering section numbers
    omitted by bookmark titles and for real numbered subsections.
    """

    try:
        import fitz  # type: ignore
    except ImportError as error:
        raise RuntimeError("缺少 PyMuPDF，无法读取原书目录。") from error

    headings = _content_headings(content_items)
    with fitz.open(Path(source_pdf)) as document:
        page_count = int(document.page_count)
        entries = _toc_entries(document)
    if not entries:
        return []

    level_ones = [entry for entry in entries if entry.level == 1]
    accepted: list[dict[str, Any]] = []
    expected_chapter = 1
    accepted_parent_indexes: set[int] = set()
    for entry in level_ones:
        heading = _main_chapter_heading(headings, entry.page, expected_chapter)
        if heading is None:
            continue
        accepted.append(
            _chapter_record(
                identifier=f"chapter-{expected_chapter:03d}",
                number=str(expected_chapter),
                title=entry.title or heading.title,
                page_start=entry.page,
                toc_parent=entry.index,
                kind="chapter",
            )
        )
        accepted_parent_indexes.add(entry.index)
        expected_chapter += 1

    if not accepted:
        return []
    first_main_page = int(accepted[0]["page_start"])
    last_main_page = int(accepted[-1]["page_start"])
    front = _front_matter_chapter(headings, first_main_page)
    chapters: list[dict[str, Any]] = [front] if front is not None else []
    chapters.extend(accepted)

    for entry in level_ones:
        if entry.index in accepted_parent_indexes or entry.page <= last_main_page:
            continue
        lowered = entry.title.casefold()
        if lowered in {"index", "references", "bibliography", "glossary"}:
            token = {
                "index": "IDX",
                "references": "REF",
                "bibliography": "BIB",
                "glossary": "GLO",
            }[lowered]
            chapter = _chapter_record(
                identifier=f"chapter-{lowered}",
                number=token,
                title=entry.title.title(),
                page_start=entry.page,
                toc_parent=entry.index,
                kind="back_matter",
            )
            chapter["sections"].append(
                _section_record(
                    chapter,
                    ordinal=1,
                    number=f"{token}.1",
                    title=entry.title.title(),
                    page_start=entry.page,
                )
            )
            chapters.append(chapter)
            accepted_parent_indexes.add(entry.index)
            continue
        letter = _appendix_letter(entries, headings, entry)
        if not letter:
            continue
        appendix_heading = next(
            (
                heading
                for heading in _headings_on_page(headings, entry.page)
                if heading.text.startswith(letter + " ")
            ),
            None,
        )
        appendix_title = (
            appendix_heading.text[len(letter) :].strip()
            if appendix_heading is not None
            else ""
        )
        chapter = _chapter_record(
            identifier=f"chapter-appendix-{letter.casefold()}",
            number=letter,
            title=(
                f"Appendix {letter}: {appendix_title}"
                if appendix_title
                else f"Appendix {letter}"
            ),
            page_start=entry.page,
            toc_parent=entry.index,
            kind="appendix",
        )
        chapters.append(chapter)
        accepted_parent_indexes.add(entry.index)

    chapters.sort(key=lambda chapter: int(chapter["page_start"]))
    for chapter in chapters:
        if chapter["sections"]:
            continue
        parent_index = chapter.get("_toc_parent")
        children = [
            entry
            for entry in entries
            if entry.parent_level_one == parent_index and entry.level == 2
        ]
        if chapter["outline_kind"] == "appendix":
            next_number = 1
            for child in children:
                page_headings = _headings_on_page(headings, child.page)
                exact = next(
                    (
                        heading
                        for heading in page_headings
                        if re.fullmatch(
                            rf"{re.escape(str(chapter['number']))}\.\d+",
                            heading.number,
                            re.I,
                        )
                    ),
                    None,
                )
                if exact is not None:
                    number = exact.number
                    match = re.search(r"\.(\d+)$", number)
                    next_number = int(match.group(1)) if match else next_number
                    title = exact.title or "Appendix section"
                else:
                    number = f"{chapter['number']}.{next_number}"
                    title = child.title or "Appendix section"
                chapter["sections"].append(
                    _section_record(
                        chapter,
                        ordinal=len(chapter["sections"]) + 1,
                        number=number,
                        title=title,
                        page_start=child.page,
                    )
                )
                next_number += 1
            continue
        for ordinal, child in enumerate(children, start=1):
            number, title = _section_heading_for_entry(
                headings,
                child,
                str(chapter["number"]),
                ordinal,
            )
            chapter["sections"].append(
                _section_record(
                    chapter,
                    ordinal=ordinal,
                    number=number,
                    title=title,
                    page_start=child.page,
                )
            )
        if not chapter["sections"]:
            chapter["sections"].append(
                _section_record(
                    chapter,
                    ordinal=1,
                    number=f"{chapter['number']}.1",
                    title=str(chapter["title"]),
                    page_start=int(chapter["page_start"]),
                )
            )

    _finalize_ranges(chapters, page_count, headings)
    validate_import_outline(chapters, page_count)
    return chapters


__all__ = [
    "build_mineru_contents_outline",
    "build_pdf_catalog_outline",
    "catalog_plain_text",
    "validate_import_outline",
]
