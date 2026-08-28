"""Deterministic data contracts for textbook exercise-companion lectures.

MinerU owns the source inventory. The user imports directories and LaTeX written
in ChatGPT; this module only parses directory text and computes writing progress.
The workflow never generates lecture content or creates a material ZIP.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

COMPANION_MODE = "textbook_exercise_companion"
COMPANION_SCHEMA_VERSION = 2
EXERCISE_INVENTORY_FILENAME = "exercise_inventory.json"
EXERCISE_COVERAGE_FILENAME = "coverage_manifest.json"
COMPANION_METADATA_FILENAME = "textbook_exercise_companion.json"
INVENTORY_SCOPE = "chapter_end_exercise_lists_only"

_CHAPTER_HEADING = re.compile(r"(?im)^(?:#{1,6}\s*)?Chapter\s+(\d+)\s*$")
_EXERCISES_HEADING = re.compile(r"(?im)^#{1,6}\s+(?:Exercises?|Problems?)\s*$")
_EXERCISE_START = re.compile(r"(?im)^\s*(?:Exercise\s+)?(?:(\d+)\.)?(\d+)\.\s+")
_EXERCISE_ENVIRONMENT = re.compile(
    r"\\begin\s*\{exercise\}(?:\[[^\]]*\])?(.*?)"
    r"\\end\s*\{exercise\}",
    re.DOTALL,
)


def _chapter_title(text: str, marker_end: int, chapter_number: int) -> str:
    tail = str(text)[marker_end:]
    for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", tail):
        title = " ".join(match.group(1).split())
        if not re.fullmatch(
            rf"(?i)Chapter\s+{int(chapter_number)}|Exercises?|Problems?", title
        ):
            return title
    for line in tail.splitlines():
        title = " ".join(line.strip().lstrip("#").split())
        if title:
            return title
    return f"Chapter {int(chapter_number)}"


def _source_page(locator: str) -> int:
    match = re.search(r"(?i)PDF page\s+(\d+)", str(locator or ""))
    return int(match.group(1)) if match else 0


def build_mineru_exercise_inventory(
    chunks: list[dict[str, Any]],
    *,
    source_filename: str = "",
    source_sha256: str = "",
) -> dict[str, Any]:
    """Inventory only each chapter's dedicated end-of-chapter exercise list."""
    ordered = sorted(
        (dict(item) for item in chunks),
        key=lambda item: int(item.get("chunk_order") or 0),
    )
    chapter_starts: list[dict[str, Any]] = []
    for index, item in enumerate(ordered):
        content = str(item.get("content") or "")
        marker = _CHAPTER_HEADING.search(content)
        if marker is None:
            continue
        chapter_starts.append(
            {
                "number": int(marker.group(1)),
                "index": index,
                "title": _chapter_title(content, marker.end(), int(marker.group(1))),
            }
        )
    if not chapter_starts:
        raise ValueError("MinerU 内容中没有识别到 Chapter 标题。")
    chapter_numbers = [int(item["number"]) for item in chapter_starts]
    if len(chapter_numbers) != len(set(chapter_numbers)):
        raise ValueError("MinerU 内容中出现重复 Chapter 编号。")
    expected_chapters = list(range(chapter_numbers[0], chapter_numbers[-1] + 1))
    if chapter_numbers != expected_chapters:
        raise ValueError(
            "MinerU 内容中的 Chapter 编号不连续："
            f"识别到 {chapter_numbers}。"
        )

    chapters: list[dict[str, Any]] = []
    for offset, chapter in enumerate(chapter_starts):
        start = int(chapter["index"])
        stop = (
            int(chapter_starts[offset + 1]["index"])
            if offset + 1 < len(chapter_starts)
            else len(ordered)
        )
        exercise_started = False
        candidates: list[dict[str, Any]] = []
        for chunk_index in range(start, stop):
            item = ordered[chunk_index]
            content = str(item.get("content") or "")
            if not exercise_started:
                heading = _EXERCISES_HEADING.search(content)
                if heading is None:
                    continue
                exercise_started = True
                content = content[heading.end() :]
            matches = list(_EXERCISE_START.finditer(content))
            for match_index, match in enumerate(matches):
                qualified_chapter = int(match.group(1) or 0)
                number = int(match.group(2))
                if qualified_chapter and qualified_chapter != int(chapter["number"]):
                    continue
                end = (
                    matches[match_index + 1].start()
                    if match_index + 1 < len(matches)
                    else len(content)
                )
                statement = re.sub(r"\s+", " ", content[match.start() : end]).strip()
                candidates.append(
                    {
                        "number": number,
                        "source_locator": str(item.get("locator") or ""),
                        "source_page": int(item.get("page_number") or 0)
                        or _source_page(str(item.get("locator") or "")),
                        "statement_excerpt": statement[:600],
                    }
                )
        if not exercise_started:
            raise ValueError(
                f"MinerU 内容中没有找到 Chapter {int(chapter['number'])} 的 Exercises 标题。"
            )
        exercises: list[dict[str, Any]] = []
        expected = 1
        for candidate in candidates:
            number = int(candidate["number"])
            if number < expected:
                continue
            if number > expected:
                raise ValueError(
                    f"Chapter {int(chapter['number'])} 的 MinerU 习题编号不连续："
                    f"期望 {expected}，实际遇到 {number}。"
                )
            exercises.append(candidate)
            expected += 1
        if not exercises:
            raise ValueError(
                f"MinerU 内容中没有识别到 Chapter {int(chapter['number'])} 的习题题号。"
            )
        chapters.append(
            {
                "chapter_number": int(chapter["number"]),
                "chapter_title": str(chapter["title"]),
                "exercise_count": len(exercises),
                "exercises": exercises,
            }
        )
    return {
        "schema_version": COMPANION_SCHEMA_VERSION,
        "source": "mineru_chunks",
        "inventory_scope": INVENTORY_SCOPE,
        "source_filename": str(source_filename),
        "source_sha256": str(source_sha256),
        "chapter_count": len(chapters),
        "exercise_count": sum(int(chapter["exercise_count"]) for chapter in chapters),
        "chapters": chapters,
    }


def parse_chapter_outline_text(
    chapter_number: int,
    outline_text: str,
) -> dict[str, Any]:
    """Parse a user-pasted plain-text Chapter/Section/Subsection directory."""
    chapter_number = int(chapter_number)
    chapter_title = ""
    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] | None = None
    unrecognized: list[str] = []

    def clean_title(value: str) -> str:
        return " ".join(value.strip().strip("*_` ").split())

    def add_section(number: int, title: str) -> None:
        nonlocal current_section
        current_section = {
            "section_number": int(number),
            "title": clean_title(title),
            "subsections": [],
        }
        sections.append(current_section)

    def add_subsection(section_number: int, number: int, title: str) -> None:
        if current_section is None:
            raise ValueError("Subsection 前必须先写所属的 Section。")
        if int(current_section["section_number"]) != int(section_number):
            raise ValueError(
                f"Subsection 所属 Section 为 {section_number}，"
                f"但当前 Section 为 {current_section['section_number']}。"
            )
        current_section["subsections"].append(
            {"subsection_number": int(number), "title": clean_title(title)}
        )

    for raw_line in str(outline_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*+]\s+", "", line)
        line = line.strip().strip("*_` ")

        latex = re.fullmatch(
            r"\\(chapter|section|subsection)\*?\{(.+)\}",
            line,
            flags=re.IGNORECASE,
        )
        if latex:
            level, title = latex.group(1).casefold(), latex.group(2)
            if level == "chapter":
                chapter_title = clean_title(title)
            elif level == "section":
                add_section(len(sections) + 1, title)
            else:
                if current_section is None:
                    raise ValueError("Subsection 前必须先写所属的 Section。")
                add_subsection(
                    int(current_section["section_number"]),
                    len(current_section["subsections"]) + 1,
                    title,
                )
            continue

        chapter_match = re.fullmatch(
            r"(?i:Chapter)\s+(\d+)\s*(?:[:：.\-–—]\s*)?(.+)", line
        ) or re.fullmatch(r"第\s*(\d+)\s*章\s*(?:[:：.\-–—]\s*)?(.+)", line)
        if chapter_match:
            supplied_chapter = int(chapter_match.group(1))
            if supplied_chapter != chapter_number:
                raise ValueError(
                    f"目录 Chapter 编号错误：期望 {chapter_number}，实际 {supplied_chapter}。"
                )
            chapter_title = clean_title(chapter_match.group(2))
            continue

        named_subsection = re.fullmatch(
            r"(?i:Subsection)\s+(\d+(?:\.\d+){0,2})\s*(?:[:：.\-–—]\s*)?(.+)",
            line,
        ) or re.fullmatch(
            r"第\s*(\d+)\s*小节\s*(?:[:：.\-–—]\s*)?(.+)", line
        )
        if named_subsection:
            parts = [int(value) for value in named_subsection.group(1).split(".")]
            if len(parts) == 3:
                supplied_chapter, section_number, subsection_number = parts
            elif len(parts) == 2:
                supplied_chapter = chapter_number
                section_number, subsection_number = parts
            else:
                supplied_chapter = chapter_number
                if current_section is None:
                    raise ValueError("Subsection 前必须先写所属的 Section。")
                section_number = int(current_section["section_number"])
                subsection_number = parts[0]
            if supplied_chapter != chapter_number:
                raise ValueError(
                    f"目录 Chapter 编号错误：期望 {chapter_number}，实际 {supplied_chapter}。"
                )
            add_subsection(
                section_number,
                subsection_number,
                named_subsection.group(2),
            )
            continue

        numeric_subsection = re.fullmatch(
            r"(\d+)\.(\d+)\.(\d+)\s*(?:[:：.\-–—]\s*)?(.+)", line
        )
        if numeric_subsection:
            supplied_chapter = int(numeric_subsection.group(1))
            if supplied_chapter != chapter_number:
                raise ValueError(
                    f"目录 Chapter 编号错误：期望 {chapter_number}，实际 {supplied_chapter}。"
                )
            add_subsection(
                int(numeric_subsection.group(2)),
                int(numeric_subsection.group(3)),
                numeric_subsection.group(4),
            )
            continue

        named_section = re.fullmatch(
            r"(?i:Section)\s+(\d+(?:\.\d+)?)\s*(?:[:：.\-–—]\s*)?(.+)", line
        ) or re.fullmatch(
            r"第\s*(\d+)\s*节\s*(?:[:：.\-–—]\s*)?(.+)", line
        )
        if named_section:
            parts = [int(value) for value in named_section.group(1).split(".")]
            if len(parts) == 2:
                supplied_chapter, section_number = parts
            else:
                supplied_chapter, section_number = chapter_number, parts[0]
            if supplied_chapter != chapter_number:
                raise ValueError(
                    f"目录 Chapter 编号错误：期望 {chapter_number}，实际 {supplied_chapter}。"
                )
            add_section(section_number, named_section.group(2))
            continue

        numeric_section = re.fullmatch(
            r"(\d+)\.(\d+)\s*(?:[:：.\-–—]\s*)?(.+)", line
        )
        if numeric_section:
            supplied_chapter = int(numeric_section.group(1))
            if supplied_chapter != chapter_number:
                raise ValueError(
                    f"目录 Chapter 编号错误：期望 {chapter_number}，实际 {supplied_chapter}。"
                )
            add_section(int(numeric_section.group(2)), numeric_section.group(3))
            continue
        unrecognized.append(raw_line.strip())

    if unrecognized:
        preview = "；".join(unrecognized[:3])
        raise ValueError(f"以下目录行无法识别：{preview}")
    if not chapter_title:
        raise ValueError("目录缺少 Chapter 标题行。")
    return {
        "chapter_number": chapter_number,
        "chapter_title": chapter_title,
        "sections": sections,
    }


def parse_textbook_outline_text(outline_text: str) -> list[dict[str, Any]]:
    """Split and parse an editable multi-chapter plain-text directory."""
    chapter_blocks: list[tuple[int, list[str]]] = []
    current_number = 0
    current_lines: list[str] = []
    leading_content: list[str] = []

    for raw_line in str(outline_text or "").splitlines():
        line = raw_line.strip()
        comparable = re.sub(r"^#{1,6}\s*", "", line)
        comparable = re.sub(r"^[-*+]\s+", "", comparable)
        comparable = comparable.strip().strip("*_` ")
        chapter_match = re.fullmatch(
            r"(?i:Chapter)\s+(\d+)\s*(?:[:：.\-–—]\s*)?(.+)", comparable
        ) or re.fullmatch(
            r"第\s*(\d+)\s*章\s*(?:[:：.\-–—]\s*)?(.+)", comparable
        )
        if chapter_match:
            if current_lines:
                chapter_blocks.append((current_number, current_lines))
            current_number = int(chapter_match.group(1))
            current_lines = [raw_line]
            continue
        if current_lines:
            current_lines.append(raw_line)
        elif line and not line.startswith("```"):
            leading_content.append(line)

    if current_lines:
        chapter_blocks.append((current_number, current_lines))
    if leading_content:
        raise ValueError(
            "第一个 Chapter 标题前存在无法识别的内容："
            + "；".join(leading_content[:3])
        )
    if not chapter_blocks:
        raise ValueError("目录不能为空，且必须包含至少一个 Chapter 标题。")
    numbers = [number for number, _lines in chapter_blocks]
    if len(numbers) != len(set(numbers)):
        raise ValueError("目录中存在重复的 Chapter 编号。")
    return [
        parse_chapter_outline_text(number, "\n".join(lines))
        for number, lines in chapter_blocks
    ]


def validate_chapter_outline(
    inventory_chapter: dict[str, Any], chapter_outline: dict[str, Any]
) -> dict[str, Any]:
    """Validate only the hierarchy and titles of one ChatGPT-written directory."""
    chapter_number = int(inventory_chapter.get("chapter_number") or 0)
    supplied_number = int(chapter_outline.get("chapter_number") or 0)
    if supplied_number != chapter_number:
        raise ValueError(
            f"目录 Chapter 编号错误：期望 {chapter_number}，实际 {supplied_number}。"
        )
    sections: list[dict[str, Any]] = []
    seen_sections: set[int] = set()
    for section_index, raw_section in enumerate(
        chapter_outline.get("sections") or [], start=1
    ):
        section = dict(raw_section)
        section_number = int(section.get("section_number") or section_index)
        if section_number in seen_sections:
            raise ValueError(f"Chapter {chapter_number} 的 Section {section_number} 重复。")
        seen_sections.add(section_number)
        section_title = " ".join(str(section.get("title") or "").split())
        if not section_title:
            raise ValueError(f"Chapter {chapter_number} 存在空 Section 标题。")
        subsections: list[dict[str, Any]] = []
        seen_subsections: set[int] = set()
        for subsection_index, raw_subsection in enumerate(
            section.get("subsections") or [], start=1
        ):
            subsection = dict(raw_subsection)
            subsection_number = int(
                subsection.get("subsection_number") or subsection_index
            )
            if subsection_number in seen_subsections:
                raise ValueError(
                    f"Chapter {chapter_number} Section {section_number} 的 "
                    f"Subsection {subsection_number} 重复。"
                )
            seen_subsections.add(subsection_number)
            title = " ".join(str(subsection.get("title") or "").split())
            if not title:
                raise ValueError(f"Chapter {chapter_number} 存在空 Subsection 标题。")
            subsections.append(
                {
                    "subsection_number": subsection_number,
                    "title": title,
                }
            )
        if not subsections:
            raise ValueError(f"Chapter {chapter_number} 存在没有 Subsection 的 Section。")
        sections.append(
            {
                "section_number": section_number,
                "title": section_title,
                "subsections": subsections,
            }
        )
    if not sections:
        raise ValueError(f"Chapter {chapter_number} 没有 Section。")
    return {
        "chapter_number": chapter_number,
        "chapter_title": str(
            chapter_outline.get("chapter_title")
            or inventory_chapter.get("chapter_title")
            or f"Chapter {chapter_number}"
        ),
        "sections": sections,
        "directory_valid": True,
    }


def exercise_environment_fingerprints(latex_source: str) -> list[str]:
    """Return stable hashes for substantive exercise environments in one TeX body."""
    hashes: list[str] = []
    for match in _EXERCISE_ENVIRONMENT.finditer(str(latex_source or "")):
        body = re.sub(r"(?m)(?<!\\)%.*$", "", match.group(1))
        normalized = re.sub(r"\s+", " ", body).strip()
        if normalized:
            hashes.append(hashlib.sha256(normalized.encode("utf-8")).hexdigest())
    return hashes


def build_exercise_progress(
    inventory: dict[str, Any], chapter_latex_sources: dict[int, list[str]]
) -> dict[str, Any]:
    """Count globally unique exercise environments and report copies as duplicates."""
    chapter_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_environments = 0
    for raw_chapter in inventory.get("chapters") or []:
        chapter = dict(raw_chapter)
        number = int(chapter.get("chapter_number") or 0)
        hashes = [
            digest
            for source in chapter_latex_sources.get(number, [])
            for digest in exercise_environment_fingerprints(source)
        ]
        total_environments += len(hashes)
        unique_here: list[str] = []
        duplicate_count = 0
        for digest in hashes:
            if digest in seen:
                duplicate_count += 1
                continue
            seen.add(digest)
            unique_here.append(digest)
        expected = int(chapter.get("exercise_count") or 0)
        written = min(expected, len(unique_here))
        chapter_rows.append(
            {
                "chapter_number": number,
                "chapter_title": str(chapter.get("chapter_title") or ""),
                "expected": expected,
                "exercise_environments": len(hashes),
                "unique_exercises_written": len(unique_here),
                "written": written,
                "unwritten": max(0, expected - written),
                "duplicate_environments": duplicate_count,
                "overflow_unique_exercises": max(0, len(unique_here) - expected),
            }
        )
    return {
        "schema_version": COMPANION_SCHEMA_VERSION,
        "mode": COMPANION_MODE,
        "inventory_ready": True,
        "chapter_count": len(chapter_rows),
        "expected": sum(int(item["expected"]) for item in chapter_rows),
        "exercise_environments": total_environments,
        "unique_exercises_written": len(seen),
        "written": sum(int(item["written"]) for item in chapter_rows),
        "unwritten": sum(int(item["unwritten"]) for item in chapter_rows),
        "duplicate_environments": total_environments - len(seen),
        "chapters": chapter_rows,
    }
