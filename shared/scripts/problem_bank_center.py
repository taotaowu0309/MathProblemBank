from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from typing import Any

if sys.platform == "win32":
    python_home = Path(sys.executable).resolve().parent
    tcl_root = python_home / "tcl"
    os.environ.setdefault("TCL_LIBRARY", str(tcl_root / "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", str(tcl_root / "tk8.6"))

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from shared.scripts.vocabulary_manager import VocabularyManager, pdf_vocabulary_selection_candidates
from shared.scripts.pdf_word_geometry import (
    PdfPageGeometry,
    WordSelection,
    build_page_geometry,
    expand_line_wrapped_word,
    hit_test_word,
    range_cursor_from_point,
)

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # type: ignore[no-redef]
    except ImportError:
        fitz = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]


MASTERY_DB_TO_CN = {
    "unrated": "未评定",
    "mastered": "已掌握",
    "familiar": "熟悉",
    "unfamiliar": "不熟悉",
    "unknown": "未知",
}
MASTERY_CN_TO_DB = {value: key for key, value in MASTERY_DB_TO_CN.items()}
LAST_STANDARD_IMPORT_KEY = "last_standard_import"
SELECTION_CURSOR_PAGE_STRIDE = 1_000_000
PDF_VOCABULARY_TEXT_RE = re.compile(
    r"^[A-Za-z\u00C0-\u024F\u1E00-\u1EFF]+(?:[\u2019'\-\u2010\u2013][A-Za-z\u00C0-\u024F\u1E00-\u1EFF]+)*"
    r"(?:\s+[A-Za-z\u00C0-\u024F\u1E00-\u1EFF]+(?:[\u2019'\-\u2010\u2013][A-Za-z\u00C0-\u024F\u1E00-\u1EFF]+)*){0,11}$"
)
PDF_MATH_FONT_RE = re.compile(
    r"(?:math|symbol|cmsy|cmmi|cmex|msam|msbm|stmary|euler|wasy|rsfs|dsrom)",
    re.IGNORECASE,
)
PDF_VOCABULARY_WORD_RE = re.compile(
    r"[A-Za-z\u00C0-\u024F\u1E00-\u1EFF]+"
    r"(?:[\u2019'\-\u2010\u2013][A-Za-z\u00C0-\u024F\u1E00-\u1EFF]+)*"
)
def pdf_preview_outline_row_height(font_linespace: int) -> int:
    return max(20, int(font_linespace) + 6)


def pdf_outline_display_text(
    level: int,
    title: str,
    named_destination: str = "",
) -> str:
    """Use the numbering embedded by LaTeX instead of renumbering bookmarks."""

    clean_title = str(title or "").strip() or "(未命名)"
    destination = str(named_destination or "").strip()
    match = re.fullmatch(
        r"(?:chapter|section|subsection|subsubsection)\.([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)",
        destination,
        flags=re.IGNORECASE,
    )
    if match is None:
        return clean_title

    number = match.group(1)
    if int(level) == 1:
        prefix = f"Chapter {number}"
        already_numbered = re.match(
            rf"^Chapter\s+{re.escape(number)}(?:\s|$)",
            clean_title,
            flags=re.IGNORECASE,
        )
    else:
        prefix = number
        already_numbered = re.match(
            rf"^{re.escape(number)}(?:\s|$)",
            clean_title,
        )
    return clean_title if already_numbered else f"{prefix}    {clean_title}"


def qid(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def table_columns(connection: sqlite3.Connection, name: str) -> list[str]:
    if not table_exists(connection, name):
        return []
    return [row[1] for row in connection.execute(f"PRAGMA table_info({qid(name)})")]


def short(value: Any, length: int = 90) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= length else text[: length - 1] + "…"


def basic_cleanup(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_literal(text: str) -> str:
    return basic_cleanup(text).casefold()


def normalize_structure(text: str) -> str:
    text = normalize_literal(text)
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"[a-zA-Z]", "x", text)
    return text


def problem_pdf_anchor(problem_code: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(problem_code or "").strip())
    return f"problem-{safe}" if safe else "problem"


def placeholders(values: list[int] | tuple[int, ...]) -> str:
    return ",".join("?" for _ in values)


def parse_last_standard_import(connection: sqlite3.Connection) -> dict[str, Any] | None:
    if not table_exists(connection, "metadata"):
        return None
    if not {"key", "value"}.issubset(set(table_columns(connection, "metadata"))):
        return None
    row = connection.execute(
        "SELECT value FROM metadata WHERE key=?",
        (LAST_STANDARD_IMPORT_KEY,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("canonical_ids"), list):
        return None
    ids: list[int] = []
    for value in payload["canonical_ids"]:
        try:
            problem_id = int(value)
        except (TypeError, ValueError):
            continue
        if problem_id > 0:
            ids.append(problem_id)
    if not ids:
        return None
    payload["canonical_ids"] = list(dict.fromkeys(ids))
    return payload


def find_last_import_canonical_ids(connection: sqlite3.Connection) -> tuple[list[int], str]:
    latest_row = connection.execute(
        "SELECT id FROM canonical_problems ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if latest_row is None:
        return [], "empty"
    latest_id = int(latest_row[0])
    payload = parse_last_standard_import(connection)
    if payload is not None:
        tracked_ids = [int(problem_id) for problem_id in payload["canonical_ids"]]
        if tracked_ids:
            ph = placeholders(tracked_ids)
            rows = connection.execute(
                f"SELECT id FROM canonical_problems WHERE id IN ({ph}) ORDER BY id",
                tracked_ids,
            ).fetchall()
            existing_ids = [int(row[0]) for row in rows]
            if existing_ids and latest_id in existing_ids:
                return existing_ids, "tracked"
    return [latest_id], "latest_id"


SEARCH_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+")


def _normalized_search_word(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", text)
    return re.sub(r"[^a-z0-9']+", "", text)


def _remove_doubled_final_consonant(value: str) -> str:
    if len(value) >= 3 and value[-1] == value[-2] and value[-1] not in "aeiou":
        return value[:-1]
    return value


def _search_word_keys(value: str) -> set[str]:
    word = _normalized_search_word(value)
    if not word:
        return set()
    if len(word) <= 2 or not word.isalpha():
        return {word}
    keys = {word}
    if word.endswith("'s"):
        keys.add(word[:-2])
    if word.endswith("ies") and len(word) > 4:
        keys.add(word[:-3] + "y")
    if word.endswith("ves") and len(word) > 4:
        keys.add(word[:-3] + "f")
        keys.add(word[:-3] + "fe")
    if word.endswith("ses") and len(word) > 4:
        keys.add(word[:-2])
    elif word.endswith(("ches", "shes", "xes", "zes", "oes")) and len(word) > 4:
        keys.add(word[:-2])
    elif word.endswith("s") and len(word) > 3:
        keys.add(word[:-1])
    if word.endswith("ied") and len(word) > 4:
        keys.add(word[:-3] + "y")
    elif word.endswith("ed") and len(word) > 4:
        root = _remove_doubled_final_consonant(word[:-2])
        keys.add(root)
        keys.add(root + "e")
    if word.endswith("ing") and len(word) > 5:
        root = _remove_doubled_final_consonant(word[:-3])
        keys.add(root)
        keys.add(root + "e")
    if word.endswith("er") and len(word) > 4:
        root = _remove_doubled_final_consonant(word[:-2])
        keys.add(root)
        keys.add(root + "e")
    if word.endswith("est") and len(word) > 5:
        root = _remove_doubled_final_consonant(word[:-3])
        keys.add(root)
        keys.add(root + "e")
    return {key for key in keys if key}


def _query_search_tokens(query: str) -> list[set[str]]:
    tokens = SEARCH_WORD_RE.findall(query or "")
    return [keys for token in tokens if (keys := _search_word_keys(token))]


def _word_matches_query_token(word: str, query_keys: set[str]) -> bool:
    return bool(_search_word_keys(word) & query_keys)


def _group_words_by_line(words: list[dict[str, Any]]) -> list[tuple[tuple[int, int], list[dict[str, Any]]]]:
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for word in words:
        key = (int(word.get("block", 0)), int(word.get("line", 0)))
        groups.setdefault(key, []).append(word)
    return [
        (key, sorted(value, key=lambda item: (float(item["x0"]), int(item.get("word", 0)))))
        for key, value in sorted(groups.items())
    ]


def _word_match_result(page_index: int, words: list[dict[str, Any]]) -> dict[str, Any]:
    rects: list[dict[str, float]] = []
    for _line_key, line_words in _group_words_by_line(words):
        rects.append(
            {
                "x0": min(float(word["x0"]) for word in line_words),
                "y0": min(float(word["y0"]) for word in line_words),
                "x1": max(float(word["x1"]) for word in line_words),
                "y1": max(float(word["y1"]) for word in line_words),
            }
        )
    return {
        "page": page_index,
        "x0": min(rect["x0"] for rect in rects),
        "y0": min(rect["y0"] for rect in rects),
        "x1": max(rect["x1"] for rect in rects),
        "y1": max(rect["y1"] for rect in rects),
        "rects": rects,
    }


def _search_result_overlaps(
    results: list[dict[str, Any]],
    page_index: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> bool:
    area = max(1.0, (x1 - x0) * (y1 - y0))
    for result in results:
        if int(result["page"]) != page_index:
            continue
        rx0 = float(result["x0"])
        ry0 = float(result["y0"])
        rx1 = float(result["x1"])
        ry1 = float(result["y1"])
        overlap_width = max(0.0, min(x1, rx1) - max(x0, rx0))
        overlap_height = max(0.0, min(y1, ry1) - max(y0, ry0))
        if overlap_width <= 0 or overlap_height <= 0:
            continue
        other_area = max(1.0, (rx1 - rx0) * (ry1 - ry0))
        if (overlap_width * overlap_height) / min(area, other_area) >= 0.65:
            return True
    return False


def pdf_search_positions(
    pdf_path: Path,
    query: str,
) -> list[dict[str, Any]]:
    if fitz is None:
        raise RuntimeError(
            "缺少 PyMuPDF。\n"
            "请在终端执行：\n"
            "py -m pip install pymupdf"
        )
    document = fitz.open(str(pdf_path))
    results: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int, int]] = set()
    query_tokens = _query_search_tokens(query)
    try:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            rectangles = page.search_for(query) or []
            for rectangle in rectangles:
                result = {
                    "page": page_index,
                    "x0": float(rectangle.x0),
                    "y0": float(rectangle.y0),
                    "x1": float(rectangle.x1),
                    "y1": float(rectangle.y1),
                }
                key = (
                    page_index,
                    round(float(rectangle.x0)),
                    round(float(rectangle.y0)),
                    round(float(rectangle.x1)),
                    round(float(rectangle.y1)),
                )
                if key not in seen and not _search_result_overlaps(
                    results,
                    page_index,
                    float(rectangle.x0),
                    float(rectangle.y0),
                    float(rectangle.x1),
                    float(rectangle.y1),
                ):
                    seen.add(key)
                    results.append(result)
            if not query_tokens:
                continue
            raw_words = page.get_text("words") or []
            words = [
                {
                    "x0": float(word[0]),
                    "y0": float(word[1]),
                    "x1": float(word[2]),
                    "y1": float(word[3]),
                    "text": str(word[4]),
                    "block": int(word[5]) if len(word) > 5 else 0,
                    "line": int(word[6]) if len(word) > 6 else 0,
                    "word": int(word[7]) if len(word) > 7 else index,
                }
                for index, word in enumerate(raw_words)
                if len(word) >= 5 and _search_word_keys(str(word[4]))
            ]
            words.sort(
                key=lambda word: (
                    int(word.get("block", 0)),
                    int(word.get("line", 0)),
                    int(word.get("word", 0)),
                    float(word.get("y0", 0.0)),
                    float(word.get("x0", 0.0)),
                )
            )
            window_size = len(query_tokens)
            if len(words) < window_size:
                continue
            for start in range(0, len(words) - window_size + 1):
                candidate = words[start : start + window_size]
                if not all(
                    _word_matches_query_token(str(word["text"]), query_tokens[offset])
                    for offset, word in enumerate(candidate)
                ):
                    continue
                result = _word_match_result(page_index, candidate)
                key = (
                    page_index,
                    round(float(result["x0"])),
                    round(float(result["y0"])),
                    round(float(result["x1"])),
                    round(float(result["y1"])),
                )
                if key not in seen and not _search_result_overlaps(
                    results,
                    page_index,
                    float(result["x0"]),
                    float(result["y0"]),
                    float(result["x1"]),
                    float(result["y1"]),
                ):
                    seen.add(key)
                    results.append(result)
    finally:
        document.close()
    return sorted(
        results,
        key=lambda result: (
            int(result["page"]),
            float(result["y0"]),
            float(result["x0"]),
        ),
    )


def _destination_to_page_anchor(
    document: Any,
    destination: Any,
) -> tuple[int, float] | None:
    try:
        page_index = int(destination.get("page", -1))
    except (AttributeError, TypeError, ValueError):
        return None
    if not (0 <= page_index < int(document.page_count)):
        return None
    target = destination.get("to") if hasattr(destination, "get") else None
    anchor_y = 0.0
    if target is not None:
        try:
            target_y = float(target.y)
        except AttributeError:
            try:
                target_y = float(target[1])
            except (TypeError, ValueError, IndexError):
                target_y = 0.0
        page = document.load_page(page_index)
        anchor_y = max(0.0, float(page.rect.height) - target_y)
    return page_index, anchor_y


def _outline_heading_page(
    document: Any,
    title: str,
    page_index: int,
) -> int:
    """Correct a bookmark whose anchor was emitted before TeX moved its heading."""

    target = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    if not target or not (0 <= page_index < int(document.page_count)):
        return page_index

    def has_heading(index: int) -> bool:
        lines = str(document.load_page(index).get_text("text") or "").splitlines()
        return any(
            re.sub(r"\s+", " ", line).strip().casefold() == target
            for line in lines
        )

    if has_heading(page_index):
        return page_index
    next_page = page_index + 1
    if next_page < int(document.page_count) and has_heading(next_page):
        return next_page
    return page_index


class PDFPreviewWindow:
    """
    题库内部的单实例 PDF 预览窗口。

    每次渲染只临时打开 PDF，渲染完成后立即关闭，
    避免 Windows 下占用 PDF 文件而妨碍下一次重新编译。
    """

    def __init__(self, owner: Any) -> None:
        if fitz is None:
            raise RuntimeError(
                "缺少 PyMuPDF。\n"
                "请在终端执行：\n"
                "py -m pip install pymupdf"
            )

        if Image is None or ImageTk is None:
            raise RuntimeError(
                "缺少 Pillow。\n"
                "请在终端执行：\n"
                "py -m pip install pillow"
            )

        self.owner = owner
        self.window = tk.Toplevel(owner.root)
        self.window.withdraw()
        self.window.title("标准题 PDF 定位")
        self.window.geometry("1120x880")
        self.window.minsize(760, 560)
        self.window.protocol(
            "WM_DELETE_WINDOW",
            self.close,
        )

        # 与 VS Code 内置 PDF 查看器一致：
        # 左方向键切换到上一页，右方向键切换到下一页。
        # 使用 bind_all，确保焦点位于 Canvas、滚动条或工具栏时
        # 仍然可以直接翻页；窗口关闭时会解除绑定。
        self.window.bind_all(
            "<Left>",
            self._on_previous_page_key,
            add="+",
        )
        self.window.bind_all(
            "<Right>",
            self._on_next_page_key,
            add="+",
        )
        self.window.bind_all(
            "<Alt-Left>",
            self._on_pdf_navigation_back,
            add="+",
        )
        self.window.bind_all(
            "<Alt-Right>",
            self._on_pdf_navigation_forward,
            add="+",
        )

        # PDF 放大后允许使用鼠标滚轮上下滚动。
        # Windows / macOS 使用 <MouseWheel>；
        # Linux 兼容 <Button-4> 和 <Button-5>。
        self.window.bind(
            "<MouseWheel>",
            self._on_mouse_wheel,
            add="+",
        )
        if sys.platform == "win32":
            self.window.bind(
                "<Button-4>",
                self._on_pdf_navigation_back,
                add="+",
            )
            self.window.bind(
                "<Button-5>",
                self._on_pdf_navigation_forward,
                add="+",
            )
        else:
            self.window.bind(
                "<Button-4>",
                self._on_mouse_wheel_up,
                add="+",
            )
            self.window.bind(
                "<Button-5>",
                self._on_mouse_wheel_down,
                add="+",
            )
            for sequence, callback in (
                ("<Button-8>", self._on_pdf_navigation_back),
                ("<Button-9>", self._on_pdf_navigation_forward),
            ):
                try:
                    self.window.bind(sequence, callback, add="+")
                except tk.TclError:
                    pass

        self.pdf_path: Path | None = None
        self.problem_code = ""
        self.problem_title = ""
        self.page_index = 0
        self.page_count = 0
        self.anchor_y: float | None = None
        self.zoom = 2.90
        self.photo: Any = None
        self.page_photos: dict[int, Any] = {}
        self.page_image_items: dict[int, int] = {}
        self.page_layouts: list[dict[str, float]] = []
        self.pdf_links: list[dict[str, Any]] = []
        self.hover_link_index: int | None = None
        self.pressed_link_index: int | None = None
        self.link_highlight_item: int | None = None
        self.pdf_navigation_back_stack: list[dict[str, float]] = []
        self.pdf_navigation_forward_stack: list[dict[str, float]] = []
        self.page_margin = 30.0
        self.page_gap = 28.0
        self.content_width = 1.0
        self.content_height = 1.0
        self.selection_start_page: tuple[int, float, float] | None = None
        self.selection_end_page: tuple[int, float, float] | None = None
        self.selection_start_cursor: int | None = None
        self.selection_end_cursor: int | None = None
        self.selection_cursor_mode = "word"
        self.selection_start_line_index: int | None = None
        self.selection_preview_item: int | None = None
        self.selection_preview_items: list[int] = []
        self.selection_highlight_items: list[int] = []
        self.selection_char_units_cache: dict[int, list[dict[str, Any]]] = {}
        self.selection_char_override_units: list[dict[str, Any]] | None = None
        self.selection_text_override: str | None = None
        self.pdf_document_generation = 0
        self.selection_generation = 0
        self.selection_document_generation: int | None = None
        self.selection_drag_active = False
        self.selection_drag_x = 0
        self.selection_drag_y = 0
        self.selection_autoscroll_after_id: str | None = None
        self.selection_double_click_active = False
        self.selected_text = ""
        self.vocabulary_popup_frame: tk.Frame | None = None
        self.vocabulary_popup_item: int | None = None
        self.vocabulary_popup_anchor: tuple[float, float] | None = None
        self.vocabulary_agent_request_id = 0
        self.vocabulary_agent_entry: dict[str, str] | None = None
        self.vocabulary_agent_context = ""
        self.vocabulary_selection_source = ""
        self.vocabulary_manager = getattr(
            getattr(owner, "service", None),
            "vocabulary_manager",
            VocabularyManager(),
        )
        self.page_chars_cache: dict[int, list[dict[str, Any]]] = {}
        self.page_geometry_cache: dict[int, PdfPageGeometry] = {}
        self.search_query = ""
        self.search_results: list[dict[str, Any]] = []
        self.current_search_index = -1
        self.search_highlight_items: list[int] = []
        self.outline_visible = True
        self.outline_width = 513
        self.outline_entries: list[dict[str, Any]] = []
        self._outline_row_height = 0
        self._canvas_viewport_width = 0
        self._outline_selection_fade_generation = 0
        self._outline_selection_fade_after_ids: list[str] = []
        self._outline_selection_fade_item = ""

        self.problem_var = tk.StringVar(value="")
        self.page_var = tk.StringVar(value="")
        self.zoom_var = tk.StringVar(value="290%")
        self.search_var = tk.StringVar(value="")

        self.window.rowconfigure(1, weight=1)
        self.window.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(
            self.window,
            padding=(10, 8),
        )
        toolbar.grid(
            row=0,
            column=0,
            sticky=tk.EW,
        )
        toolbar.columnconfigure(14, weight=1)

        ttk.Button(
            toolbar,
            text="上一页",
            command=lambda: self.change_page(-1),
        ).grid(row=0, column=0, padx=(0, 5))

        ttk.Button(
            toolbar,
            text="下一页",
            command=lambda: self.change_page(1),
        ).grid(row=0, column=1, padx=5)

        ttk.Button(
            toolbar,
            text="缩小",
            command=lambda: self.change_zoom(-0.15),
        ).grid(row=0, column=2, padx=(14, 5))

        ttk.Button(
            toolbar,
            text="放大",
            command=lambda: self.change_zoom(0.15),
        ).grid(row=0, column=3, padx=5)

        ttk.Button(
            toolbar,
            text="重新载入",
            command=self.reload_problem,
        ).grid(row=0, column=4, padx=(14, 5))

        self.toggle_outline_button = ttk.Button(
            toolbar,
            text="收起目录",
            command=self.toggle_outline,
        )
        self.toggle_outline_button.grid(row=0, column=5, padx=5)

        ttk.Button(
            toolbar,
            text="打开位置",
            command=self.open_pdf_location,
        ).grid(row=0, column=6, padx=5)

        ttk.Button(
            toolbar,
            text="在 VS Code 中打开 TeX",
            command=self.open_pdf_in_vscode,
        ).grid(row=0, column=7, padx=5)

        self.copy_selection_button = ttk.Button(
            toolbar,
            text="复制选中",
            command=self.copy_selected_text,
            state=tk.DISABLED,
        )
        self.copy_selection_button.grid(row=0, column=8, padx=5)

        self.previous_search_button = ttk.Button(
            toolbar,
            text="上一个位置",
            command=lambda: self.change_search_result(-1),
            state=tk.DISABLED,
        )
        self.previous_search_button.grid(row=0, column=9, padx=(14, 5))

        self.next_search_button = ttk.Button(
            toolbar,
            text="下一个位置",
            command=lambda: self.change_search_result(1),
            state=tk.DISABLED,
        )
        self.next_search_button.grid(row=0, column=10, padx=5)

        ttk.Label(
            toolbar,
            textvariable=self.search_var,
        ).grid(row=0, column=11, padx=(8, 5))

        ttk.Label(
            toolbar,
            textvariable=self.page_var,
        ).grid(row=0, column=12, padx=(14, 5))

        ttk.Label(
            toolbar,
            textvariable=self.zoom_var,
        ).grid(row=0, column=13, padx=5)

        ttk.Label(
            toolbar,
            textvariable=self.problem_var,
            anchor=tk.E,
        ).grid(
            row=0,
            column=14,
            sticky=tk.E,
            padx=(16, 0),
        )

        body = ttk.Frame(self.window)
        body.grid(
            row=1,
            column=0,
            sticky=tk.NSEW,
        )
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.paned = tk.PanedWindow(
            body,
            orient=tk.HORIZONTAL,
            sashwidth=6,
            sashrelief=tk.RAISED,
            showhandle=True,
            borderwidth=0,
            background="#d7d7d7",
        )
        self.paned.grid(row=0, column=0, sticky=tk.NSEW)

        self.outline_frame = ttk.Frame(self.paned, padding=(10, 8))
        self.outline_frame.rowconfigure(1, weight=1)
        self.outline_frame.columnconfigure(0, weight=1)
        ttk.Label(self.outline_frame, text="目录").grid(row=0, column=0, sticky=tk.W)
        self.outline_style_name = "PdfPreviewOutline.Treeview"
        self.outline_style = ttk.Style(self.window)
        self._refresh_display_metrics()
        self.outline_tree = ttk.Treeview(
            self.outline_frame,
            style=self.outline_style_name,
            show="tree",
            selectmode="browse",
            height=28,
            columns=("page",),
            displaycolumns=(),
        )
        self.outline_tree.grid(row=1, column=0, sticky=tk.NSEW)
        outline_scroll = ttk.Scrollbar(
            self.outline_frame,
            orient=tk.VERTICAL,
            command=self.outline_tree.yview,
        )
        outline_scroll.grid(row=1, column=1, sticky=tk.NS)
        self.outline_tree.configure(yscrollcommand=outline_scroll.set)
        self.outline_tree.bind("<Button-1>", self._on_outline_pointer_press, add="+")
        self.outline_tree.bind("<<TreeviewSelect>>", self._on_outline_selected, add="+")

        self.pdf_body = ttk.Frame(self.paned)
        self.pdf_body.rowconfigure(0, weight=1)
        self.pdf_body.columnconfigure(0, weight=1)
        self.paned.add(self.outline_frame, minsize=300, width=self.outline_width)
        self.paned.add(self.pdf_body, minsize=420)

        self.canvas = tk.Canvas(
            self.pdf_body,
            background="#707070",
            highlightthickness=0,
        )
        self.canvas.grid(
            row=0,
            column=0,
            sticky=tk.NSEW,
        )

        vertical = ttk.Scrollbar(
            self.pdf_body,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
        )
        vertical.grid(
            row=0,
            column=1,
            sticky=tk.NS,
        )

        horizontal = ttk.Scrollbar(
            self.pdf_body,
            orient=tk.HORIZONTAL,
            command=self.canvas.xview,
        )
        horizontal.grid(
            row=1,
            column=0,
            sticky=tk.EW,
        )

        self.canvas.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        vertical.configure(command=self._canvas_yview)
        horizontal.configure(command=self._canvas_xview)
        self.canvas.bind(
            "<Configure>",
            self._on_canvas_configure,
            add="+",
        )
        self.canvas.bind("<Motion>", self._on_canvas_motion, add="+")
        self.canvas.bind("<Leave>", self._on_canvas_leave, add="+")
        self.canvas.bind("<ButtonPress-1>", self._on_selection_start, add="+")
        self.canvas.bind("<B1-Motion>", self._on_selection_drag, add="+")
        self.canvas.bind("<ButtonRelease-1>", self._on_selection_end, add="+")
        self.canvas.bind("<Double-Button-1>", self._on_selection_double_click, add="+")
        self.window.bind_all("<Up>", self._on_scroll_up_key, add="+")
        self.window.bind_all("<Down>", self._on_scroll_down_key, add="+")
        self.window.bind_all("<Control-c>", self._on_copy_key, add="+")
        self.window.bind_all("<Control-C>", self._on_copy_key, add="+")

    def _refresh_display_metrics(self) -> None:
        try:
            font_name = self.outline_style.lookup("Treeview", "font") or "TkDefaultFont"
            tree_font = tkfont.nametofont(font_name, root=self.owner.root)
            row_height = pdf_preview_outline_row_height(tree_font.metrics("linespace"))
            if row_height != self._outline_row_height:
                self.outline_style.configure(self.outline_style_name, rowheight=row_height)
                self._outline_row_height = row_height
        except (tk.TclError, TypeError, ValueError):
            pass

    def _on_canvas_configure(
        self,
        event: tk.Event | None = None,
    ) -> None:
        try:
            viewport_width = max(
                1,
                int(getattr(event, "width", self.canvas.winfo_width())),
            )
        except (tk.TclError, TypeError, ValueError):
            viewport_width = 0
        if viewport_width and viewport_width != self._canvas_viewport_width:
            self._canvas_viewport_width = viewport_width
            self._center_horizontal_view()
        self._render_visible_pages()
        self._redraw_search_highlights()
        self._redraw_selection_highlights()
        self._redraw_link_hover()

    def _center_page_image(self) -> None:
        self._render_visible_pages()

    def _canvas_yview(self, *args: Any) -> None:
        self.canvas.yview(*args)
        self._after_canvas_scroll()

    def _canvas_xview(self, *args: Any) -> None:
        self.canvas.xview(*args)
        self._after_canvas_scroll()

    def _scroll_y_units(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")
        self._after_canvas_scroll()

    def _scroll_y_pixels(
        self,
        pixels: float,
    ) -> bool:
        if not self.page_layouts:
            return False
        self.canvas.update_idletasks()
        _left, top, _right, bottom = self._visible_canvas_bounds()
        viewport_height = max(
            1.0,
            bottom - top,
            float(self.canvas.winfo_height()),
        )
        maximum_top = max(
            0.0,
            self.content_height - viewport_height,
        )
        target_top = min(
            maximum_top,
            max(0.0, top + pixels),
        )
        if abs(target_top - top) < 0.5:
            return False
        self.canvas.yview_moveto(
            target_top / max(1.0, self.content_height)
        )
        self._after_canvas_scroll()
        return True

    def _after_canvas_scroll(self) -> None:
        self._update_current_page_from_view()
        self._render_visible_pages()
        self._redraw_link_hover()

    def _center_horizontal_view(self) -> None:
        """页面宽于窗口时，默认显示页面的水平中部。"""
        self.canvas.update_idletasks()
        viewport_width = max(
            1.0,
            float(self.canvas.winfo_width()),
        )
        maximum_left = max(
            0.0,
            self.content_width - viewport_width,
        )

        if maximum_left <= 0:
            self.canvas.xview_moveto(0.0)
            return

        centered_left = maximum_left / 2.0
        self.canvas.xview_moveto(
            centered_left
            / max(1.0, self.content_width)
        )

    def exists(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def maximize_window(self) -> None:
        """Maximize the PDF preview window on the current desktop."""
        try:
            self.window.state("zoomed")
            return
        except tk.TclError:
            pass

    def apply_default_outline_width(
        self,
    ) -> None:
        if not self.outline_visible:
            return
        try:
            self.window.update_idletasks()
            self.paned.sash_place(0, self.outline_width, 0)
        except tk.TclError:
            pass
        try:
            self.window.attributes("-zoomed", True)
            return
        except tk.TclError:
            pass
        try:
            width = self.window.winfo_screenwidth()
            height = self.window.winfo_screenheight()
            self.window.geometry(f"{width}x{height}+0+0")
        except tk.TclError:
            pass

    def close(self) -> None:
        callback = getattr(self.owner, "on_pdf_preview_position_changed", None)
        if callable(callback) and self.pdf_path is not None:
            try:
                callback(self.pdf_path, self.page_index + 1, float(self.anchor_y or 0.0))
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                pass
        closed_callback = getattr(self.owner, "on_pdf_preview_closed", None)
        if callable(closed_callback) and self.pdf_path is not None:
            try:
                closed_callback(self.pdf_path, self.page_index + 1, float(self.anchor_y or 0.0))
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                pass
        self._close_vocabulary_popup()
        try:
            self.window.unbind_all("<Left>")
            self.window.unbind_all("<Right>")
            self.window.unbind_all("<Alt-Left>")
            self.window.unbind_all("<Alt-Right>")
            self.window.unbind_all("<Up>")
            self.window.unbind_all("<Down>")
            self.window.unbind_all("<Control-c>")
            self.window.unbind_all("<Control-C>")
            self.window.destroy()
        finally:
            self.owner.pdf_preview = None

    def _on_previous_page_key(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        """左方向键：上一页。"""
        self.change_page(-1)
        return "break"

    def _on_next_page_key(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        """右方向键：下一页。"""
        self.change_page(1)
        return "break"

    def _on_scroll_up_key(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        """上方向键：向上滚动当前 PDF 页。"""
        self._scroll_y_units(-6)
        return "break"

    def _on_scroll_down_key(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        """下方向键：向下滚动当前 PDF 页。"""
        self._scroll_y_units(6)
        return "break"

    def _on_copy_key(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        """Ctrl+C：复制当前拖选出的 PDF 文本。"""
        self.copy_selected_text()
        return "break"

    def _on_pdf_navigation_back(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        """鼠标返回侧键 / Alt+Left：回到上一次 PDF 跳转前的位置。"""
        self.go_pdf_navigation_back()
        return "break"

    def _on_pdf_navigation_forward(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        """鼠标前进侧键 / Alt+Right：重新进入刚撤回的 PDF 跳转。"""
        self.go_pdf_navigation_forward()
        return "break"

    def _on_mouse_wheel(
        self,
        event: tk.Event,
    ) -> str:
        """鼠标滚轮：普通滚动；按住 Ctrl 时缩放。"""
        delta = int(getattr(event, "delta", 0))

        if delta == 0:
            return "break"

        if self._event_has_control(event):
            self.change_zoom(0.15 if delta > 0 else -0.15)
            return "break"

        # Windows 常见每格为 ±120；高精度触控板可能更小。
        direction = -1 if delta > 0 else 1
        steps = max(
            1,
            abs(delta) // 120,
        )
        self._scroll_y_units(direction * steps * 3)
        return "break"

    def _on_mouse_wheel_up(
        self,
        event: tk.Event | None = None,
    ) -> str:
        """Linux 滚轮向上。"""
        if event is not None and self._event_has_control(event):
            self.change_zoom(0.15)
            return "break"
        self._scroll_y_units(-3)
        return "break"

    def _on_mouse_wheel_down(
        self,
        event: tk.Event | None = None,
    ) -> str:
        """Linux 滚轮向下。"""
        if event is not None and self._event_has_control(event):
            self.change_zoom(-0.15)
            return "break"
        self._scroll_y_units(3)
        return "break"

    def _event_has_control(
        self,
        event: tk.Event,
    ) -> bool:
        """Return True when the Control modifier is held for a Tk event."""
        return bool(int(getattr(event, "state", 0)) & 0x0004)

    def _canvas_event_to_page_point(
        self,
        event: tk.Event,
    ) -> tuple[int, float, float] | None:
        canvas_x = float(self.canvas.canvasx(event.x))
        canvas_y = float(self.canvas.canvasy(event.y))
        return self._canvas_point_to_page_point(canvas_x, canvas_y)

    def _canvas_point_to_page_point(
        self,
        canvas_x: float,
        canvas_y: float,
    ) -> tuple[int, float, float] | None:
        for page_index, layout in enumerate(self.page_layouts):
            x0 = float(layout["x"])
            y0 = float(layout["y"])
            x1 = x0 + float(layout["scaled_width"])
            y1 = y0 + float(layout["scaled_height"])
            if x0 <= canvas_x <= x1 and y0 <= canvas_y <= y1:
                return (
                    page_index,
                    (canvas_x - x0) / max(self.zoom, 0.01),
                    (canvas_y - y0) / max(self.zoom, 0.01),
                )
        return None

    def _visible_canvas_bounds(self) -> tuple[float, float, float, float]:
        left = float(self.canvas.canvasx(0))
        top = float(self.canvas.canvasy(0))
        right = float(self.canvas.canvasx(max(1, self.canvas.winfo_width())))
        bottom = float(self.canvas.canvasy(max(1, self.canvas.winfo_height())))
        return left, top, right, bottom

    def _update_current_page_from_view(self) -> None:
        if not self.page_layouts:
            return
        _left, top, _right, bottom = self._visible_canvas_bounds()
        midpoint = (top + bottom) / 2.0
        best_index = 0
        best_distance = float("inf")
        for page_index, layout in enumerate(self.page_layouts):
            page_top = float(layout["y"])
            page_bottom = page_top + float(layout["scaled_height"])
            if page_top <= midpoint <= page_bottom:
                best_index = page_index
                break
            distance = min(abs(midpoint - page_top), abs(midpoint - page_bottom))
            if distance < best_distance:
                best_index = page_index
                best_distance = distance
        changed = best_index != self.page_index
        self.page_index = best_index
        self.page_var.set(f"PDF 第 {self.page_index + 1} / {self.page_count} 页")
        if changed and self.pdf_path is not None:
            callback = getattr(self.owner, "on_pdf_preview_position_changed", None)
            if callable(callback):
                try:
                    callback(self.pdf_path, self.page_index + 1, 0.0)
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    pass

    def _clear_selection(
        self,
    ) -> None:
        self.selection_generation += 1
        self._close_vocabulary_popup()
        self.vocabulary_selection_source = ""
        self.selected_text = ""
        self.selection_start_page = None
        self.selection_end_page = None
        self.selection_start_cursor = None
        self.selection_end_cursor = None
        self.selection_cursor_mode = "word"
        self.selection_char_override_units = None
        self.selection_text_override = None
        self.selection_document_generation = None
        self.selection_start_line_index = None
        self.selection_drag_active = False
        self.selection_double_click_active = False
        self._cancel_selection_autoscroll()
        if self.selection_preview_item is not None:
            self.canvas.delete(self.selection_preview_item)
            self.selection_preview_item = None
        for item in self.selection_preview_items:
            self.canvas.delete(item)
        self.selection_preview_items = []
        for item in self.selection_highlight_items:
            self.canvas.delete(item)
        self.selection_highlight_items = []
        self.copy_selection_button.configure(state=tk.DISABLED)

    def clear_search(
        self,
    ) -> None:
        self.search_query = ""
        self.search_results = []
        self.current_search_index = -1
        self.search_var.set("")
        self.previous_search_button.configure(state=tk.DISABLED)
        self.next_search_button.configure(state=tk.DISABLED)
        self._clear_search_highlights()

    def _clear_search_highlights(
        self,
    ) -> None:
        for item in self.search_highlight_items:
            self.canvas.delete(item)
        self.search_highlight_items = []

    def _set_search_navigation_state(
        self,
    ) -> None:
        state = tk.NORMAL if self.search_results else tk.DISABLED
        self.previous_search_button.configure(state=state)
        self.next_search_button.configure(state=state)
        if not self.search_results:
            self.search_var.set("")
            return
        self.search_var.set(
            f"{self.search_query}  {self.current_search_index + 1}/{len(self.search_results)}"
        )

    def _redraw_search_highlights(
        self,
    ) -> None:
        self._clear_search_highlights()
        if not self.search_results:
            return
        for index, result in enumerate(self.search_results):
            page_index = int(result["page"])
            if not (0 <= page_index < len(self.page_layouts)):
                continue
            is_current = index == self.current_search_index
            rects = result.get("rects") or [result]
            for rect in rects:
                item = self.canvas.create_rectangle(
                    *self._page_rect_to_canvas_rect(
                        page_index,
                        float(rect["x0"]),
                        float(rect["y0"]),
                        float(rect["x1"]),
                        float(rect["y1"]),
                    ),
                    outline="#B8860B" if is_current else "#C7A600",
                    width=2 if is_current else 1,
                    fill="#FFB300" if is_current else "#FFE45C",
                    stipple="gray50",
                )
                self.search_highlight_items.append(item)

    def _selection_bounds(
        self,
    ) -> tuple[int, float, float, float, float] | None:
        if self.selection_start_page is None or self.selection_end_page is None:
            return None
        page0, x0, y0 = self.selection_start_page
        page1, x1, y1 = self.selection_end_page
        if page0 != page1:
            return None
        return (
            page0,
            min(x0, x1),
            min(y0, y1),
            max(x0, x1),
            max(y0, y1),
        )

    def _page_geometry(
        self,
        page_index: int,
    ) -> PdfPageGeometry | None:
        if page_index in self.page_geometry_cache:
            return self.page_geometry_cache[page_index]
        if self.pdf_path is None:
            return None
        document = fitz.open(str(self.pdf_path))
        try:
            if not (0 <= page_index < document.page_count):
                return None
            page = document.load_page(page_index)
            raw = page.get_text("rawdict") or {}
        finally:
            document.close()
        geometry = build_page_geometry(page_index, raw)
        self.page_geometry_cache[page_index] = geometry
        return geometry

    def _invalidate_pdf_text_state(self) -> None:
        """Invalidate every selection object tied to the previous PDF bytes."""

        self.pdf_document_generation += 1
        self._clear_selection()
        self.page_chars_cache = {}
        self.page_geometry_cache = {}
        self.selection_char_units_cache = {}

    def _page_chars(
        self,
        page_index: int,
    ) -> list[dict[str, Any]]:
        if page_index in self.page_chars_cache:
            return self.page_chars_cache[page_index]
        geometry = self._page_geometry(page_index)
        if geometry is None:
            return []
        chars: list[dict[str, Any]] = []
        for glyph in geometry.chars:
            x0, y0, x1, y1 = glyph.raw_bbox
            hit_x0, hit_y0, hit_x1, hit_y1 = glyph.hit_bbox
            highlight_x0, highlight_y0, highlight_x1, highlight_y1 = glyph.highlight_bbox
            chars.append(
                {
                    "page": page_index,
                    "char_id": glyph.char_id,
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "hit_x0": hit_x0,
                    "hit_y0": hit_y0,
                    "hit_x1": hit_x1,
                    "hit_y1": hit_y1,
                    "highlight_x0": highlight_x0,
                    "highlight_y0": highlight_y0,
                    "highlight_x1": highlight_x1,
                    "highlight_y1": highlight_y1,
                    "origin_x": glyph.origin[0],
                    "origin_y": glyph.origin[1],
                    "line_direction": glyph.line_direction,
                    "text": glyph.text,
                    "block": glyph.block_id,
                    "line": glyph.line_id,
                    "source_line": glyph.source_line_id,
                    "word": glyph.char_in_line,
                    "font": glyph.font_name,
                    "font_flags": glyph.font_flags,
                    "font_size": glyph.font_size,
                    "ascender": glyph.ascender,
                    "descender": glyph.descender,
                    "abnormal_bbox": glyph.abnormal_bbox,
                    "math_font": bool(PDF_MATH_FONT_RE.search(glyph.font_name)),
                }
            )
        self.page_chars_cache[page_index] = chars
        return chars

    @staticmethod
    def _rect_overlap_ratio(unit: dict[str, Any], block: dict[str, Any]) -> float:
        x0 = max(float(unit["x0"]), float(block["x0"]))
        y0 = max(float(unit["y0"]), float(block["y0"]))
        x1 = min(float(unit["x1"]), float(block["x1"]))
        y1 = min(float(unit["y1"]), float(block["y1"]))
        overlap = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        area = max(0.01, (float(unit["x1"]) - float(unit["x0"])) * (float(unit["y1"]) - float(unit["y0"])))
        return overlap / area

    def _selection_contains_math_font(
        self,
        units: list[dict[str, Any]],
    ) -> bool:
        """Reject character spans that touch a mathematical PDF font."""

        for unit in units:
            if bool(unit.get("math_font")):
                return True
            if "math_font" in unit or bool(unit.get("synthetic_space")):
                continue
            overlapping_chars = [
                char
                for char in self._page_chars(int(unit.get("page", self.page_index)))
                if self._rect_overlap_ratio(unit, char) >= 0.18
            ]
            if any(bool(char.get("math_font")) for char in overlapping_chars):
                return True
        return False

    def _select_visual_text_near_point(
        self,
        target: str,
        page_index: int,
        pdf_x: float,
        pdf_y: float,
    ) -> bool:
        """Select one local geometry word without reconstructing page reading order."""

        clean_target = basic_cleanup(target)
        if not clean_target or not PDF_VOCABULARY_TEXT_RE.fullmatch(clean_target):
            return False
        geometry = self._page_geometry(page_index)
        if geometry is None:
            return False
        selection = hit_test_word(geometry, pdf_x, pdf_y)
        if selection is None:
            return False
        selection = expand_line_wrapped_word(geometry, selection)
        if normalize_literal(selection.lookup_text) != normalize_literal(clean_target):
            return False
        return self._apply_word_selection(selection)

    def _apply_word_selection(self, selection: WordSelection) -> bool:
        """Apply a non-contiguous geometry selection to the existing Canvas overlay."""

        page_chars = {
            int(unit.get("char_id", -1)): unit
            for unit in self._page_chars(selection.page_index)
        }
        selected_units: list[dict[str, Any]] = []
        for char_id in selection.char_ids:
            source = page_chars.get(int(char_id))
            if source is None:
                return False
            unit = dict(source)
            unit["x0"] = float(unit.get("highlight_x0", unit["x0"]))
            unit["y0"] = float(unit.get("highlight_y0", unit["y0"]))
            unit["x1"] = float(unit.get("highlight_x1", unit["x1"]))
            unit["y1"] = float(unit.get("highlight_y1", unit["y1"]))
            unit["selection_line_index"] = int(unit.get("line", selection.band_id))
            unit["selection_page_index"] = int(char_id)
            unit["selection_index"] = (
                selection.page_index * SELECTION_CURSOR_PAGE_STRIDE + int(char_id)
            )
            selected_units.append(unit)
        if not selected_units:
            return False

        self.selection_generation += 1
        self.selection_document_generation = self.pdf_document_generation
        first = selected_units[0]
        last = selected_units[-1]
        line_index = int(first.get("selection_line_index", selection.band_id))
        self.selection_cursor_mode = "char"
        self.selection_char_override_units = list(selected_units)
        self.selection_text_override = selection.lookup_text
        self.selection_start_cursor = int(first["selection_index"])
        self.selection_end_cursor = int(last["selection_index"]) + 1
        self.selection_start_line_index = line_index
        self.selection_start_page = (
            selection.page_index,
            float(first["x0"]),
            (float(first["y0"]) + float(first["y1"])) / 2.0,
        )
        self.selection_end_page = (
            selection.page_index,
            float(last["x1"]),
            (float(last["y0"]) + float(last["y1"])) / 2.0,
        )
        self.selection_drag_active = False
        self._redraw_selection_highlights()
        self.selected_text = selection.lookup_text
        return True

    def _vocabulary_selection_validation(self) -> tuple[bool, str, str]:
        selected = basic_cleanup(str(self.selected_text or ""))
        if not selected:
            return False, "", "没有可查询的选中文本。"
        if not PDF_VOCABULARY_TEXT_RE.fullmatch(selected):
            return False, "", "选区包含公式、数字或非词汇符号；为避免误查，请只选英文词或短语。"
        if not self._selected_char_units():
            return False, "", "没有取得可验证的正文词块。"
        source = str(getattr(self, "vocabulary_selection_source", "") or "")
        if source == "pdf":
            if self._selection_contains_math_font(self._selected_char_units()):
                return False, "", "选区碰到公式字体，不能作为词汇查询。"
        else:
            return False, "", "该选区尚未形成安全的完整词汇。"
        return True, selected, ""

    def _selection_page_char_units(
        self,
        page_index: int,
    ) -> list[dict[str, Any]]:
        if page_index in self.selection_char_units_cache:
            return self.selection_char_units_cache[page_index]
        geometry = self._page_geometry(page_index)
        if geometry is None:
            return []
        source_chars = {
            int(char.get("char_id", -1)): char
            for char in self._page_chars(page_index)
        }
        units: list[dict[str, Any]] = []
        for band in geometry.bands:
            for char_id in band.char_ids:
                source = source_chars.get(int(char_id))
                if source is None:
                    continue
                page_offset = len(units)
                unit = dict(source)
                unit["x0"] = float(unit.get("highlight_x0", unit["x0"]))
                unit["y0"] = float(unit.get("highlight_y0", unit["y0"]))
                unit["x1"] = float(unit.get("highlight_x1", unit["x1"]))
                unit["y1"] = float(unit.get("highlight_y1", unit["y1"]))
                unit["selection_line_index"] = band.band_id
                unit["selection_page_index"] = page_offset
                unit["selection_index"] = page_index * SELECTION_CURSOR_PAGE_STRIDE + page_offset
                units.append(unit)
        self.selection_char_units_cache[page_index] = units
        return units

    def _selection_cursor_from_page_point(
        self,
        page_index: int,
        x: float,
        y: float,
        *,
        strict: bool = False,
    ) -> int | None:
        geometry = self._page_geometry(page_index)
        if geometry is None:
            return None
        cursor = range_cursor_from_point(geometry, x, y, strict=strict)
        if cursor is None:
            return None
        return page_index * SELECTION_CURSOR_PAGE_STRIDE + cursor.page_offset

    def _selected_char_units(
        self,
    ) -> list[dict[str, Any]]:
        if getattr(self, "selection_document_generation", None) != getattr(
            self,
            "pdf_document_generation",
            0,
        ):
            return []
        if getattr(self, "selection_cursor_mode", "char") != "char":
            return []
        override = getattr(self, "selection_char_override_units", None)
        if override is not None:
            return list(override)
        if self.selection_start_cursor is None or self.selection_end_cursor is None:
            return []
        start = int(self.selection_start_cursor)
        end = int(self.selection_end_cursor)
        if start == end:
            return []
        low, high = sorted((start, end))
        start_page, start_offset = divmod(low, SELECTION_CURSOR_PAGE_STRIDE)
        end_page, end_offset = divmod(high, SELECTION_CURSOR_PAGE_STRIDE)
        if start_page < 0 or end_page < 0:
            return []
        selected: list[dict[str, Any]] = []
        for page_index in range(start_page, end_page + 1):
            page_units = self._selection_page_char_units(page_index)
            page_low = start_offset if page_index == start_page else 0
            page_high = end_offset if page_index == end_page else len(page_units)
            if page_low >= page_high:
                continue
            selected.extend(
                unit
                for unit in page_units
                if page_low <= int(unit.get("selection_page_index", 0)) < page_high
            )
        return selected

    def _format_selected_chars(
        self,
        chars: list[dict[str, Any]],
    ) -> str:
        lines: list[str] = []
        current_key: tuple[int, int] | None = None
        current_chars: list[str] = []
        for char in chars:
            key = (
                int(char.get("page", 0)),
                int(char.get("selection_line_index", 0)),
            )
            if current_key is not None and key != current_key:
                lines.append("".join(current_chars).rstrip())
                current_chars = []
            current_key = key
            current_chars.append(str(char.get("text", "")))
        if current_chars:
            lines.append("".join(current_chars).rstrip())
        return "\n".join(line for line in lines if line).strip()

    def _native_selected_text(
        self,
    ) -> str:
        if (
            self.pdf_path is None
            or self.selection_start_page is None
            or self.selection_end_page is None
            or self.selection_start_cursor is None
            or self.selection_end_cursor is None
            or self.selection_start_cursor == self.selection_end_cursor
        ):
            return ""

        page0, x0, y0 = self.selection_start_page
        page1, x1, y1 = self.selection_end_page
        if int(self.selection_end_cursor) < int(self.selection_start_cursor):
            page0, x0, y0, page1, x1, y1 = page1, x1, y1, page0, x0, y0

        document = fitz.open(str(self.pdf_path))
        try:
            first_page = max(0, min(int(page0), int(document.page_count) - 1))
            last_page = max(0, min(int(page1), int(document.page_count) - 1))
            if first_page > last_page:
                first_page, last_page = last_page, first_page
                x0, y0, x1, y1 = x1, y1, x0, y0

            parts: list[str] = []
            for page_index in range(first_page, last_page + 1):
                page = document.load_page(page_index)
                if first_page == last_page:
                    text = page.get_text_selection((x0, y0), (x1, y1))
                elif page_index == first_page:
                    text = page.get_text_selection((x0, y0), (float(page.rect.width), float(page.rect.height)))
                elif page_index == last_page:
                    text = page.get_text_selection((0.0, 0.0), (x1, y1))
                else:
                    text = page.get_text("text")
                clean = str(text or "").strip()
                if clean:
                    parts.append(clean)
            return "\n".join(parts).strip()
        except Exception:
            return ""
        finally:
            document.close()

    def _page_rect_to_canvas_rect(
        self,
        page_index: int,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
    ) -> tuple[float, float, float, float]:
        if not (0 <= page_index < len(self.page_layouts)):
            return (0.0, 0.0, 0.0, 0.0)
        layout = self.page_layouts[page_index]
        page_offset_x = float(layout["x"])
        page_offset_y = float(layout["y"])
        return (
            page_offset_x + x0 * self.zoom,
            page_offset_y + y0 * self.zoom,
            page_offset_x + x1 * self.zoom,
            page_offset_y + y1 * self.zoom,
        )

    def _delete_selection_preview_items(
        self,
    ) -> None:
        if self.selection_preview_item is not None:
            self.canvas.delete(self.selection_preview_item)
            self.selection_preview_item = None
        for item in self.selection_preview_items:
            self.canvas.delete(item)
        self.selection_preview_items = []

    def _draw_selection_word_items(
        self,
        words: list[dict[str, Any]],
        target: list[int],
        fill: str,
        only_visible: bool = False,
    ) -> None:
        rect_units: list[dict[str, Any]] = []
        current_group: list[dict[str, Any]] = []
        current_key: tuple[int, int] | None = None
        for word in sorted(words, key=lambda unit: int(unit.get("selection_index", 0))):
            key = (
                int(word.get("page", self.page_index)),
                int(word.get("selection_line_index", int(word.get("line", 0)))),
            )
            if current_group and key != current_key:
                rect_units.append(
                    {
                        "page": current_key[0] if current_key is not None else self.page_index,
                        "x0": min(float(unit["x0"]) for unit in current_group),
                        "y0": min(float(unit["y0"]) for unit in current_group),
                        "x1": max(float(unit["x1"]) for unit in current_group),
                        "y1": max(float(unit["y1"]) for unit in current_group),
                    }
                )
                current_group = []
            current_key = key
            current_group.append(word)
        if current_group:
            rect_units.append(
                {
                    "page": current_key[0] if current_key is not None else self.page_index,
                    "x0": min(float(unit["x0"]) for unit in current_group),
                    "y0": min(float(unit["y0"]) for unit in current_group),
                    "x1": max(float(unit["x1"]) for unit in current_group),
                    "y1": max(float(unit["y1"]) for unit in current_group),
                }
            )

        if only_visible:
            left, top, right, bottom = self._visible_canvas_bounds()
            buffer = 180.0
            visible_bounds = (
                left - buffer,
                top - buffer,
                right + buffer,
                bottom + buffer,
            )
        else:
            visible_bounds = None
        for word in rect_units:
            page_index = int(word.get("page", self.page_index))
            rect = self._page_rect_to_canvas_rect(
                page_index,
                float(word["x0"]),
                float(word["y0"]),
                float(word["x1"]),
                float(word["y1"]),
            )
            if visible_bounds is not None:
                x0, y0, x1, y1 = rect
                left, top, right, bottom = visible_bounds
                if x1 < left or x0 > right or y1 < top or y0 > bottom:
                    continue
            item = self.canvas.create_rectangle(
                *rect,
                outline="",
                fill=fill,
                stipple="gray50",
            )
            target.append(item)

    def _redraw_selection_preview(
        self,
    ) -> None:
        self._delete_selection_preview_items()
        chars = self._selected_char_units()
        self._draw_selection_word_items(
            chars,
            self.selection_preview_items,
            "#8EC5FF",
            only_visible=True,
        )

    def _redraw_selection_highlights(
        self,
    ) -> None:
        for item in self.selection_highlight_items:
            self.canvas.delete(item)
        self.selection_highlight_items = []
        self._delete_selection_preview_items()
        chars = self._selected_char_units()
        self._draw_selection_word_items(
            chars,
            self.selection_highlight_items,
            "#6AA9FF",
        )
        text_override = getattr(self, "selection_text_override", None)
        self.selected_text = (
            text_override
            if text_override is not None
            else self._format_selected_chars(chars)
        )
        state = tk.NORMAL if self.selected_text else tk.DISABLED
        self.copy_selection_button.configure(state=state)

    def _update_selection_end_from_pointer(
        self,
        widget_x: int,
        widget_y: int,
    ) -> None:
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        clamped_x = min(max(0, widget_x), width - 1)
        clamped_y = min(max(0, widget_y), height - 1)
        canvas_x = float(self.canvas.canvasx(clamped_x))
        canvas_y = float(self.canvas.canvasy(clamped_y))
        point = self._canvas_point_to_page_point(canvas_x, canvas_y)
        if point is None:
            return
        page_index, x, y = point
        cursor = self._selection_cursor_from_page_point(page_index, x, y)
        if cursor is None:
            return
        self.selection_end_cursor = cursor
        self.selection_end_page = point
        self._redraw_selection_preview()

    def _cancel_selection_autoscroll(
        self,
    ) -> None:
        if self.selection_autoscroll_after_id is None:
            return
        try:
            self.window.after_cancel(self.selection_autoscroll_after_id)
        except tk.TclError:
            pass
        self.selection_autoscroll_after_id = None

    def _schedule_selection_autoscroll(
        self,
    ) -> None:
        if self.selection_autoscroll_after_id is not None:
            return
        if self._selection_autoscroll_delta_pixels() == 0.0:
            return
        self.selection_autoscroll_after_id = self.window.after(
            16,
            self._selection_autoscroll_tick,
        )

    def _selection_autoscroll_delta_pixels(
        self,
    ) -> float:
        height = max(1, self.canvas.winfo_height())
        edge = min(96.0, max(56.0, height * 0.14))
        speed_min = 2.0
        speed_max = 34.0
        if self.selection_drag_y < edge:
            ratio = min(1.0, max(0.0, (edge - self.selection_drag_y) / edge))
            return -(speed_min + (speed_max - speed_min) * ratio * ratio)
        if self.selection_drag_y > height - edge:
            ratio = min(
                1.0,
                max(0.0, (self.selection_drag_y - (height - edge)) / edge),
            )
            return speed_min + (speed_max - speed_min) * ratio * ratio
        return 0.0

    def _selection_autoscroll_tick(
        self,
    ) -> None:
        self.selection_autoscroll_after_id = None
        if not self.selection_drag_active:
            return
        delta_pixels = self._selection_autoscroll_delta_pixels()
        if delta_pixels:
            moved = self._scroll_y_pixels(delta_pixels)
            self._update_selection_end_from_pointer(
                self.selection_drag_x,
                self.selection_drag_y,
            )
            if moved:
                self._schedule_selection_autoscroll()

    def _on_selection_start(
        self,
        event: tk.Event,
    ) -> str:
        self.canvas.focus_set()
        link_index = self._link_at_event(event)
        if link_index is not None:
            self.pressed_link_index = link_index
            return "break"
        self._clear_selection()
        self.selection_start_page = self._canvas_event_to_page_point(event)
        if self.selection_start_page is None:
            return "break"
        start_page, start_x, start_y = self.selection_start_page
        geometry = self._page_geometry(start_page)
        if geometry is None:
            return "break"
        range_cursor = range_cursor_from_point(
            geometry,
            start_x,
            start_y,
            strict=True,
        )
        if range_cursor is None:
            return "break"
        self.selection_cursor_mode = "char"
        self.selection_char_override_units = None
        self.selection_text_override = None
        self.selection_document_generation = self.pdf_document_generation
        self.selection_start_line_index = range_cursor.band_id
        self.selection_start_cursor = (
            start_page * SELECTION_CURSOR_PAGE_STRIDE + range_cursor.page_offset
        )
        self.selection_end_page = self.selection_start_page
        self.selection_end_cursor = self.selection_start_cursor
        self.selection_drag_active = True
        self.selection_drag_x = int(event.x)
        self.selection_drag_y = int(event.y)
        self._redraw_selection_preview()
        self._schedule_selection_autoscroll()
        return "break"

    def _on_selection_double_click(
        self,
        event: tk.Event,
    ) -> str:
        """Select a local geometry word, independent of extraction stream order."""

        point = self._canvas_event_to_page_point(event)
        if point is None:
            return "break"
        page_index, x, y = point
        geometry = self._page_geometry(page_index)
        if geometry is None:
            return "break"
        selection = hit_test_word(geometry, x, y)
        if selection is not None:
            selection = expand_line_wrapped_word(geometry, selection)
        if selection is None or not self._apply_word_selection(selection):
            return "break"
        self.selection_double_click_active = True
        self._resolve_vocabulary_selection(event)
        return "break"

    def _on_selection_drag(
        self,
        event: tk.Event,
    ) -> str:
        if self.selection_start_cursor is None:
            return "break"
        self.selection_drag_active = True
        self.selection_drag_x = int(event.x)
        self.selection_drag_y = int(event.y)
        self._update_selection_end_from_pointer(
            self.selection_drag_x,
            self.selection_drag_y,
        )
        self._schedule_selection_autoscroll()
        return "break"

    def _on_selection_end(
        self,
        event: tk.Event,
    ) -> str:
        if self.selection_double_click_active:
            self.selection_double_click_active = False
            return "break"
        if self.pressed_link_index is not None:
            release_link_index = self._link_at_event(event)
            if release_link_index == self.pressed_link_index:
                self._jump_to_link(self.pressed_link_index)
            self.pressed_link_index = None
            return "break"
        self.selection_drag_active = False
        self._cancel_selection_autoscroll()
        if self.selection_start_cursor is None:
            return "break"
        self._update_selection_end_from_pointer(int(event.x), int(event.y))
        self._redraw_selection_highlights()
        self._resolve_vocabulary_selection(event)
        return "break"

    def _resolve_vocabulary_selection(self, event: tk.Event) -> None:
        """Resolve vocabulary entirely in-process from stable PDF prose geometry.

        The former interactive SyncTeX subprocess fallback could block, open a
        console on Windows, and still return no selection.  Formula isolation is
        now handled before hit-testing, so an external command is neither needed
        nor allowed on this mouse path.
        """

        event_x = int(event.x)
        event_y = int(event.y)
        units = self._selected_char_units()
        if not units or self.pdf_path is None:
            return
        first = units[0]
        page_index = int(first.get("page", self.page_index))
        page_point = self._canvas_event_to_page_point(event)
        if page_point is not None and int(page_point[0]) == page_index:
            _event_page, pdf_x, pdf_y = page_point
        else:
            pdf_x = (float(first["x0"]) + float(first["x1"])) / 2.0
            pdf_y = (float(first["y0"]) + float(first["y1"])) / 2.0

        raw_selected = str(self.selected_text or "")
        visual_candidate = next(
            (
                candidate
                for candidate in pdf_vocabulary_selection_candidates(raw_selected)
                if PDF_VOCABULARY_TEXT_RE.fullmatch(candidate)
            ),
            "",
        )
        self.vocabulary_selection_source = ""

        visual_ready = False
        if visual_candidate and " " not in visual_candidate.strip():
            visual_ready = self._select_visual_text_near_point(
                visual_candidate,
                page_index,
                pdf_x,
                pdf_y,
            )
        elif visual_candidate:
            selected_units = self._selected_char_units()
            visual_ready = bool(
                selected_units and not self._selection_contains_math_font(selected_units)
            )
            if visual_ready:
                self.selected_text = visual_candidate

        if visual_ready:
            self.vocabulary_selection_source = "pdf"
            try:
                self.owner.set_status(f"PDF 文字层已精确选中：{self.selected_text}")
            except (AttributeError, tk.TclError):
                pass
            self._show_vocabulary_popup_at(event_x, event_y)
            return
        try:
            self.owner.set_status(
                "该位置没有形成不含公式的完整英文词汇；未启动任何 TeX/SyncTeX 外部命令。"
            )
        except (AttributeError, tk.TclError):
            pass

    def _close_vocabulary_popup(self) -> None:
        self.vocabulary_agent_request_id += 1
        self.vocabulary_agent_entry = None
        self.vocabulary_popup_anchor = None
        item = getattr(self, "vocabulary_popup_item", None)
        self.vocabulary_popup_item = None
        if item is not None:
            try:
                self.canvas.delete(item)
            except tk.TclError:
                pass
        frame = getattr(self, "vocabulary_popup_frame", None)
        self.vocabulary_popup_frame = None
        if frame is None:
            return
        try:
            frame.destroy()
        except tk.TclError:
            pass

    def _set_tk_label_text(self, label: tk.Label, text: str, *, foreground: str | None = None) -> None:
        try:
            options: dict[str, Any] = {"text": text}
            if foreground:
                options["foreground"] = foreground
            label.configure(**options)
        except tk.TclError:
            pass

    def _dispatch_tk_callback(
        self,
        callback: Any,
        *args: Any,
    ) -> None:
        """Marshal an asynchronous result back to the Tk interpreter thread."""

        dispatcher = getattr(self.owner, "dispatch_pdf_tk_callback", None)
        if callable(dispatcher):
            dispatcher(callback, *args)
            return
        callback(*args)

    def _selected_pdf_context(self, selected: str) -> str:
        """Return a short prose window around the selected PDF word."""

        if not str(selected or "").strip():
            return ""
        units = self._selected_char_units()
        if not units:
            return ""
        page_index = int(units[0].get("page", self.page_index))
        page_chars = self._page_chars(page_index)
        if not page_chars:
            return ""
        line_ids: list[int] = []
        chars_by_line: dict[int, list[str]] = {}
        for char in page_chars:
            line_id = int(char.get("line", 0))
            if line_id not in chars_by_line:
                line_ids.append(line_id)
                chars_by_line[line_id] = []
            chars_by_line[line_id].append(str(char.get("text", "")))
        selected_line_ids = {
            int(unit.get("line", unit.get("selection_line_index", 0)))
            for unit in units
        }
        selected_positions = [
            line_ids.index(line_id)
            for line_id in selected_line_ids
            if line_id in line_ids
        ]
        if not selected_positions:
            return ""
        first = max(0, min(selected_positions) - 2)
        last = min(len(line_ids), max(selected_positions) + 3)
        context_lines = [
            "".join(chars_by_line[line_id]).strip()
            for line_id in line_ids[first:last]
        ]
        return re.sub(r"\s+", " ", " ".join(line for line in context_lines if line))[:800]

    def _query_selected_vocabulary_with_agent(
        self,
        selected: str,
        status_label: tk.Label,
        result_label: tk.Label,
        query_button: tk.Button,
        add_button: tk.Button,
    ) -> None:
        callback = getattr(self.owner, "query_pdf_vocabulary_with_agent", None)
        if not callable(callback):
            self._set_tk_label_text(status_label, "当前控制中心未提供 Agent 查询通道。", foreground="#B42318")
            return
        self.vocabulary_agent_request_id += 1
        request_id = self.vocabulary_agent_request_id
        selection_generation = self.selection_generation
        selection_document_generation = self.selection_document_generation
        self.vocabulary_agent_entry = None
        self.vocabulary_agent_context = self._selected_pdf_context(selected)
        query_button.configure(state=tk.DISABLED)
        add_button.configure(state=tk.DISABLED)
        domain = str(getattr(self.owner, "workspace", "math") or "math")
        self._set_tk_label_text(
            status_label,
            "Agent 正在判断当前英语语境中的词义与用法…" if domain == "english" else "Agent 正在查询数学语境词义…",
            foreground="#6941C6",
        )
        self._set_tk_label_text(result_label, "")

        def alive() -> bool:
            return (
                request_id == self.vocabulary_agent_request_id
                and selection_generation == self.selection_generation
                and selection_document_generation == self.selection_document_generation
                and self.vocabulary_popup_frame is not None
                and basic_cleanup(self.selected_text) == basic_cleanup(selected)
            )

        def progress(message: str) -> None:
            if alive():
                self._set_tk_label_text(status_label, short(message, 88), foreground="#6941C6")

        def success(payload: dict[str, Any]) -> None:
            if not alive():
                return
            entry = dict(payload.get("entry") or {})
            self.vocabulary_agent_entry = {
                key: str(entry.get(key) or "")
                for key in (
                    "term", "part_of_speech", "definition", "note", "source",
                    "entry_kind", "pronunciation", "definition_en",
                    "register_note", "collocations",
                )
            }
            display = str(payload.get("display") or self.vocabulary_agent_entry.get("definition") or "")
            self._set_tk_label_text(result_label, display)
            status_text = (
                "已恢复上一次相同词汇的查询结果，未再次消耗 token。"
                if payload.get("cache_reused")
                else "Agent 查询完成。"
            )
            self._set_tk_label_text(status_label, status_text, foreground="#027A48")
            query_button.configure(state=tk.NORMAL, text="重新查询")
            add_button.configure(state=tk.NORMAL if self.vocabulary_agent_entry.get("definition") else tk.DISABLED)

        def failure(message: str) -> None:
            if not alive():
                return
            self._set_tk_label_text(status_label, short(message, 120), foreground="#B42318")
            query_button.configure(state=tk.NORMAL)

        callback(
            selected,
            lambda message: self._dispatch_tk_callback(progress, message),
            lambda payload: self._dispatch_tk_callback(success, payload),
            lambda message: self._dispatch_tk_callback(failure, message),
        )

    def _analyze_selected_sentence_with_agent(
        self,
        selected: str,
        context: str,
        page_number: int,
        status_label: tk.Label,
        result_label: tk.Label,
        button: tk.Button,
    ) -> None:
        callback = getattr(self.owner, "analyze_pdf_sentence_with_agent", None)
        if not callable(callback):
            self._set_tk_label_text(status_label, "当前控制中心未提供句型分析通道。", foreground="#B42318")
            return
        selection_generation = self.selection_generation
        document_generation = self.selection_document_generation
        button.configure(state=tk.DISABLED)
        self._set_tk_label_text(status_label, "Agent 正在按旋元佑句型框架分析并保存来源…", foreground="#6941C6")

        def alive() -> bool:
            return (
                selection_generation == self.selection_generation
                and document_generation == self.selection_document_generation
                and self.vocabulary_popup_frame is not None
            )

        def progress(message: str) -> None:
            if alive():
                self._set_tk_label_text(status_label, short(message, 88), foreground="#6941C6")

        def success(payload: dict[str, Any]) -> None:
            if not alive():
                return
            self._set_tk_label_text(result_label, str(payload.get("analysis") or ""))
            self._set_tk_label_text(status_label, "句型分析已保存，并保留材料与页码。", foreground="#027A48")
            button.configure(state=tk.NORMAL, text="重新分析")

        def failure(message: str) -> None:
            if alive():
                self._set_tk_label_text(status_label, short(message, 120), foreground="#B42318")
                button.configure(state=tk.NORMAL)

        callback(
            selected, context, page_number,
            lambda message: self._dispatch_tk_callback(progress, message),
            lambda payload: self._dispatch_tk_callback(success, payload),
            lambda message: self._dispatch_tk_callback(failure, message),
        )

    def _add_agent_vocabulary_entry(
        self,
        status_label: tk.Label,
        add_button: tk.Button,
    ) -> None:
        entry = dict(self.vocabulary_agent_entry or {})
        callback = getattr(self.owner, "import_pdf_agent_vocabulary", None)
        if not entry.get("term") or not entry.get("definition") or not callable(callback):
            return
        selection_generation = self.selection_generation
        selection_document_generation = self.selection_document_generation
        add_button.configure(state=tk.DISABLED)
        self._set_tk_label_text(status_label, "等待写入确认…", foreground="#6941C6")

        def success(result: dict[str, Any]) -> None:
            if (
                selection_generation != self.selection_generation
                or selection_document_generation != self.selection_document_generation
                or self.vocabulary_popup_frame is None
            ):
                return
            affected = int(result.get("affected") or 0)
            appended = int(result.get("definitions_appended") or 0)
            new_pos = int(result.get("new_part_of_speech_entries") or 0)
            backup = Path(str(result.get("backup_path") or "")).name
            if appended:
                outcome = f"已保留原释义并追加 {appended} 个新义项"
            elif new_pos:
                outcome = f"已新增 {new_pos} 个不同词性的独立词条"
            else:
                outcome = f"已写入并回读验证 {affected} 条"
            self._set_tk_label_text(
                status_label,
                f"{outcome}；备份：{backup or '已创建'}。",
                foreground="#027A48",
            )
            add_button.configure(text="已加入词汇库", state=tk.DISABLED)

        def failure(message: str) -> None:
            if (
                selection_generation != self.selection_generation
                or selection_document_generation != self.selection_document_generation
                or self.vocabulary_popup_frame is None
            ):
                return
            self._set_tk_label_text(status_label, short(message, 120), foreground="#B42318")
            add_button.configure(state=tk.NORMAL)

        callback(
            entry,
            lambda result: self._dispatch_tk_callback(success, result),
            lambda message: self._dispatch_tk_callback(failure, message),
        )

    def _show_vocabulary_popup(self, event: tk.Event) -> None:
        self._show_vocabulary_popup_at(int(event.x), int(event.y))

    def _place_vocabulary_popup_shell(
        self,
        shell: tk.Frame,
        event_x: int,
        event_y: int,
    ) -> None:
        """Place an embedded popup beside the selection in document coordinates."""

        shell.update_idletasks()
        visible_left, visible_top, visible_right, visible_bottom = self._visible_canvas_bounds()
        visible_width = max(1.0, visible_right - visible_left)
        width = min(580, max(400, int(visible_width - 32.0)))
        height = shell.winfo_reqheight()
        pointer_x = float(self.canvas.canvasx(int(event_x)))
        pointer_y = float(self.canvas.canvasy(int(event_y)))
        selection_rects = [
            self._page_rect_to_canvas_rect(
                int(unit.get("page", self.page_index)),
                float(unit["x0"]),
                float(unit["y0"]),
                float(unit["x1"]),
                float(unit["y1"]),
            )
            for unit in self._selected_char_units()
        ]
        if selection_rects:
            pointer_x = max(rect[2] for rect in selection_rects)
            pointer_y = min(rect[1] for rect in selection_rects)
        self.vocabulary_popup_anchor = (pointer_x, pointer_y)
        x = pointer_x + 22.0
        y = pointer_y + 22.0
        if x + width > visible_right - 12.0:
            x = pointer_x - width - 22.0
        if y + height > visible_bottom - 12.0:
            y = pointer_y - height - 22.0
        x = max(visible_left + 12.0, min(x, visible_right - width - 12.0))
        y = max(visible_top + 12.0, min(y, visible_bottom - height - 12.0))
        self.vocabulary_popup_item = self.canvas.create_window(
            x,
            y,
            window=shell,
            width=width,
            anchor=tk.NW,
            tags=("vocabulary-popup",),
        )
        self.canvas.tag_raise(self.vocabulary_popup_item)

    def _show_vocabulary_popup_at(self, event_x: int, event_y: int) -> None:
        """Show database and Agent lookup beside a high-confidence PDF selection."""

        self._close_vocabulary_popup()
        valid, selected, validation_error = self._vocabulary_selection_validation()
        if not valid:
            if validation_error:
                try:
                    self.owner.set_status(validation_error)
                except (AttributeError, tk.TclError):
                    pass
            return
        try:
            shared_lookup = getattr(self.owner, "lookup_pdf_vocabulary_entries", None)
            matches = (
                shared_lookup(selected, limit=6)
                if callable(shared_lookup)
                else self.vocabulary_manager.lookup_pdf_selection(selected, limit=6)
            )
        except (OSError, sqlite3.Error, ValueError):
            return

        shell = tk.Frame(
            self.canvas,
            background="#FFFDF8",
            borderwidth=1,
            relief=tk.SOLID,
            padx=16,
            pady=14,
        )
        self.vocabulary_popup_frame = shell
        english_mode = str(getattr(self.owner, "workspace", "math") or "math") == "english"
        context = self._selected_pdf_context(selected)
        encounter_callback = getattr(self.owner, "record_pdf_vocabulary_encounter", None)
        if english_mode and callable(encounter_callback):
            try:
                encounter_callback(selected, context, self.page_index + 1)
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                pass
        tk.Label(
            shell,
            text=f"已选词汇 · {short(selected, 48)}",
            background="#FFFDF8",
            foreground="#253449",
            anchor=tk.W,
            font=("Microsoft YaHei UI", 17, "bold"),
        ).pack(fill=tk.X, pady=(0, 8))
        source_text = "PDF 文字层已精确选中"
        tk.Label(
            shell,
            text=source_text,
            background="#FFFDF8",
            foreground="#027A48",
            anchor=tk.W,
            font=("Microsoft YaHei UI", 12),
        ).pack(fill=tk.X, pady=(0, 10))
        tk.Label(
            shell,
            text="① 词汇库匹配",
            background="#FFFDF8",
            foreground="#1D4E89",
            anchor=tk.W,
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(fill=tk.X, pady=(0, 7))
        if not matches:
            tk.Label(
                shell,
                text="暂无匹配词条",
                background="#FFFDF8",
                foreground="#667085",
                anchor=tk.W,
                font=("Microsoft YaHei UI", 14),
            ).pack(fill=tk.X)
        else:
            for index, row in enumerate(matches):
                if index:
                    tk.Frame(shell, height=1, background="#E5E7EB").pack(fill=tk.X, pady=8)
                header = str(row.get("term") or "").strip()
                part_of_speech = str(row.get("part_of_speech") or "").strip()
                if part_of_speech:
                    header += f"  {part_of_speech}"
                tk.Label(
                    shell,
                    text=header,
                    background="#FFFDF8",
                    foreground="#1D4E89",
                    anchor=tk.W,
                    justify=tk.LEFT,
                    font=("Microsoft YaHei UI", 14, "bold"),
                ).pack(fill=tk.X)
                tk.Label(
                    shell,
                    text=short(row.get("definition", ""), 132),
                    background="#FFFDF8",
                    foreground="#344054",
                    anchor=tk.W,
                    justify=tk.LEFT,
                    wraplength=520,
                    font=("Microsoft YaHei UI", 14),
                ).pack(fill=tk.X, pady=(3, 0))
        tk.Frame(shell, height=2, background="#98A2B3").pack(fill=tk.X, pady=(14, 11))
        agent_header = tk.Frame(shell, background="#FFFDF8")
        agent_header.pack(fill=tk.X)
        tk.Label(
            agent_header,
            text=(
                "② Agent 释义（先回答此处含义，再给搭配、语域与构词信息）"
                if english_mode else "② Agent 释义（数学词义会补充定义/定理内容）"
            ),
            background="#FFFDF8",
            foreground="#6941C6",
            anchor=tk.W,
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        status_label = tk.Label(
            shell,
            text="尚未查询",
            background="#FFFDF8",
            foreground="#667085",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=520,
            font=("Microsoft YaHei UI", 12),
        )
        result_label = tk.Label(
            shell,
            text="",
            background="#F9F5FF",
            foreground="#344054",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=520,
            padx=12,
            pady=10,
            font=("Microsoft YaHei UI", 14),
        )
        button_row = tk.Frame(shell, background="#FFFDF8")
        button_row.pack(fill=tk.X, pady=(10, 0))
        query_button = tk.Button(
            button_row,
            text="使用 Agent 查询词义",
            font=("Microsoft YaHei UI", 12),
            padx=12,
            pady=6,
        )
        add_button = tk.Button(
            button_row,
            text="添加该词义到词汇库",
            state=tk.DISABLED,
            font=("Microsoft YaHei UI", 12),
            padx=12,
            pady=6,
        )
        query_button.configure(
            command=lambda: self._query_selected_vocabulary_with_agent(
                selected, status_label, result_label, query_button, add_button
            )
        )
        add_button.configure(command=lambda: self._add_agent_vocabulary_entry(status_label, add_button))
        query_button.pack(side=tk.LEFT)
        add_button.pack(side=tk.LEFT, padx=(10, 0))
        status_label.pack(fill=tk.X, pady=(7, 0))
        result_label.pack(fill=tk.X, pady=(7, 0))
        if english_mode:
            tk.Frame(shell, height=1, background="#D0D5DD").pack(fill=tk.X, pady=(12, 9))
            action_row = tk.Frame(shell, background="#FFFDF8")
            action_row.pack(fill=tk.X)

            def run_action(method_name: str, success_text: str) -> None:
                callback = getattr(self.owner, method_name, None)
                if not callable(callback):
                    self._set_tk_label_text(status_label, "当前控制中心未提供此操作。", foreground="#B42318")
                    return
                try:
                    callback(selected, context, self.page_index + 1)
                    self._set_tk_label_text(status_label, success_text, foreground="#027A48")
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                    self._set_tk_label_text(status_label, short(error, 120), foreground="#B42318")

            for caption, method_name, success_text in (
                ("朗读", "speak_pdf_selection", "已启动本地朗读。"),
                ("稍后查", "mark_pdf_selection_for_later", "已标记为稍后查。"),
                ("保存 Usage", "save_pdf_selection_usage", "已保存并保留来源页码。"),
                ("保存句型", "save_pdf_grammar_encounter", "已保存句型 encounter。"),
            ):
                tk.Button(
                    action_row, text=caption, font=("Microsoft YaHei UI", 11), padx=9, pady=5,
                    command=lambda name=method_name, message=success_text: run_action(name, message),
                ).pack(side=tk.LEFT, padx=(0, 7))
            analysis_button = tk.Button(
                action_row, text="Agent 句型分析", font=("Microsoft YaHei UI", 11), padx=9, pady=5,
            )
            analysis_button.configure(
                command=lambda: self._analyze_selected_sentence_with_agent(
                    selected, context, self.page_index + 1, status_label, result_label, analysis_button
                )
            )
            analysis_button.pack(side=tk.LEFT, padx=(0, 7))
        self._place_vocabulary_popup_shell(shell, event_x, event_y)

    def copy_selected_text(
        self,
    ) -> None:
        if not self.selected_text:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(self.selected_text)
        self.window.update_idletasks()

    def toggle_outline(
        self,
    ) -> None:
        self.outline_visible = not self.outline_visible
        if self.outline_visible:
            try:
                self.paned.forget(self.pdf_body)
                self.paned.add(self.outline_frame, minsize=300, width=self.outline_width)
                self.paned.add(self.pdf_body, minsize=420)
                self.window.after_idle(self.apply_default_outline_width)
            except tk.TclError:
                pass
            self.toggle_outline_button.configure(text="收起目录")
        else:
            try:
                current_width = int(self.outline_frame.winfo_width())
                if current_width > 0:
                    self.outline_width = current_width
                self.paned.forget(self.outline_frame)
            except tk.TclError:
                pass
            self.toggle_outline_button.configure(text="打开目录")

    def open_pdf_location(
        self,
    ) -> None:
        if self.pdf_path is None:
            return
        path = self.pdf_path.resolve()
        if not path.exists():
            return
        if sys.platform == "win32":
            try:
                subprocess.Popen(["explorer", "/select,", str(path)])
                return
            except OSError:
                pass
        try:
            os.startfile(str(path.parent))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    def vscode_executable(
        self,
    ) -> str | None:
        for command in ("code", "code.cmd", "Code.exe"):
            resolved = shutil.which(command)
            if resolved:
                return resolved
        if sys.platform == "win32":
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft VS Code" / "Code.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft VS Code" / "Code.exe",
            ]
            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate)
        return None

    def _vscode_project_paths(
        self,
    ) -> tuple[Path, Path | None]:
        if self.pdf_path is None:
            return Path.cwd(), None
        project_dir = self.pdf_path.resolve().parent
        main_tex = project_dir / "main.tex"
        if main_tex.is_file():
            return project_dir, main_tex

        for candidate in project_dir.glob("*.tex"):
            if candidate.is_file():
                return project_dir, candidate

        return project_dir, None

    def _vscode_problem_source(
        self,
        project_dir: Path,
        fallback_tex: Path | None,
    ) -> tuple[Path | None, int]:
        if self.problem_code:
            patterns = [
                rf"\\hypertarget\{{problem-{re.escape(self.problem_code)}\}}",
                rf"\\label\{{prob:{re.escape(self.problem_code)}\}}",
                re.escape(self.problem_code),
            ]
            source_roots = [project_dir / "chapters", project_dir]
            candidates: list[Path] = []
            for root in source_roots:
                if not root.is_dir():
                    continue
                candidates.extend(sorted(root.glob("*.tex")))
            seen: set[Path] = set()
            for path in candidates:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                for pattern in patterns:
                    for line_number, line in enumerate(lines, start=1):
                        if re.search(pattern, line):
                            return path, line_number

        if fallback_tex is not None:
            return fallback_tex, 1
        return None, 1

    def open_pdf_in_vscode(
        self,
    ) -> None:
        if self.pdf_path is None:
            return
        code = self.vscode_executable()
        if code is None:
            messagebox.showerror(
                "找不到 VS Code",
                "没有在 PATH 或常见安装目录中找到 VS Code。\n\n"
                "请确认命令行可以执行 code，或安装 VS Code 的 Shell Command。",
                parent=self.window,
            )
            return
        project_dir, main_tex = self._vscode_project_paths()
        source_path, line_number = self._vscode_problem_source(project_dir, main_tex)
        if source_path is None:
            messagebox.showerror(
                "找不到 TeX 源文件",
                f"没有在 PDF 所在目录找到可打开的 TeX 文件：\n{project_dir}",
                parent=self.window,
            )
            return
        try:
            target = f"{source_path.resolve()}:{max(1, line_number)}"
            subprocess.Popen([code, "--reuse-window", str(project_dir), "--goto", target])
            if hasattr(self.owner, "set_status"):
                self.owner.set_status(
                    f"已在 VS Code 中打开 TeX：{source_path.name}:{line_number}。请用 LaTeX Workshop 编译/预览 PDF。"
                )
        except Exception as error:
            messagebox.showerror(
                "打开 VS Code 失败",
                str(error),
                parent=self.window,
            )

    def _link_at_event(
        self,
        event: tk.Event,
    ) -> int | None:
        canvas_x = float(self.canvas.canvasx(event.x))
        canvas_y = float(self.canvas.canvasy(event.y))
        for index, link in enumerate(self.pdf_links):
            x0, y0, x1, y1 = self._page_rect_to_canvas_rect(
                int(link["page"]),
                float(link["x0"]),
                float(link["y0"]),
                float(link["x1"]),
                float(link["y1"]),
            )
            if x0 <= canvas_x <= x1 and y0 <= canvas_y <= y1:
                return index
        return None

    def _on_canvas_motion(
        self,
        event: tk.Event,
    ) -> str:
        link_index = self._link_at_event(event)
        if link_index != self.hover_link_index:
            self.hover_link_index = link_index
            self._redraw_link_hover()
        self.canvas.configure(cursor="hand2" if link_index is not None else "")
        return ""

    def _on_canvas_leave(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        self.hover_link_index = None
        self.canvas.configure(cursor="")
        self._redraw_link_hover()
        return ""

    def _redraw_link_hover(self) -> None:
        if self.link_highlight_item is not None:
            self.canvas.delete(self.link_highlight_item)
            self.link_highlight_item = None
        if self.hover_link_index is None:
            return
        if not (0 <= self.hover_link_index < len(self.pdf_links)):
            return
        link = self.pdf_links[self.hover_link_index]
        self.link_highlight_item = self.canvas.create_rectangle(
            *self._page_rect_to_canvas_rect(
                int(link["page"]),
                float(link["x0"]),
                float(link["y0"]),
                float(link["x1"]),
                float(link["y1"]),
            ),
            outline="#1D72D8",
            width=2,
            fill="#8EC5FF",
            stipple="gray50",
        )
        self.canvas.tag_raise(self.link_highlight_item)

    def _current_pdf_navigation_position(
        self,
    ) -> dict[str, float] | None:
        if not self.page_layouts:
            return None
        self._update_current_page_from_view()
        page_index = max(
            0,
            min(int(self.page_index), len(self.page_layouts) - 1),
        )
        _left, top, _right, _bottom = self._visible_canvas_bounds()
        layout = self.page_layouts[page_index]
        anchor_y = max(
            0.0,
            (top - float(layout["y"])) / max(self.zoom, 0.01),
        )
        try:
            xview = float(self.canvas.xview()[0])
            yview = float(self.canvas.yview()[0])
        except tk.TclError:
            xview = 0.0
            yview = 0.0
        return {
            "page": float(page_index),
            "anchor_y": float(anchor_y),
            "xview": xview,
            "yview": yview,
        }

    def _same_pdf_navigation_position(
        self,
        left: dict[str, float] | None,
        right: dict[str, float] | None,
    ) -> bool:
        if left is None or right is None:
            return False
        return (
            int(left.get("page", -1)) == int(right.get("page", -2))
            and abs(float(left.get("yview", 0.0)) - float(right.get("yview", 1.0))) < 0.0005
            and abs(float(left.get("xview", 0.0)) - float(right.get("xview", 1.0))) < 0.0005
        )

    def _push_pdf_navigation_back(
        self,
    ) -> None:
        position = self._current_pdf_navigation_position()
        if position is None:
            return
        if (
            self.pdf_navigation_back_stack
            and self._same_pdf_navigation_position(
                self.pdf_navigation_back_stack[-1],
                position,
            )
        ):
            return
        self.pdf_navigation_back_stack.append(position)
        del self.pdf_navigation_back_stack[:-80]
        self.pdf_navigation_forward_stack.clear()

    def _clear_pdf_navigation_history(
        self,
    ) -> None:
        self.pdf_navigation_back_stack.clear()
        self.pdf_navigation_forward_stack.clear()

    def _restore_pdf_navigation_position(
        self,
        position: dict[str, float],
    ) -> None:
        if not self.page_layouts:
            return
        page_index = max(
            0,
            min(int(position.get("page", 0)), len(self.page_layouts) - 1),
        )
        self.page_index = page_index
        self.anchor_y = float(position.get("anchor_y", 0.0))
        self.canvas.update_idletasks()
        self.canvas.xview_moveto(
            min(1.0, max(0.0, float(position.get("xview", 0.0))))
        )
        self.canvas.yview_moveto(
            min(1.0, max(0.0, float(position.get("yview", 0.0))))
        )
        self._after_canvas_scroll()

    def go_pdf_navigation_back(
        self,
    ) -> None:
        if not self.pdf_navigation_back_stack:
            return
        current = self._current_pdf_navigation_position()
        if current is not None:
            self.pdf_navigation_forward_stack.append(current)
            del self.pdf_navigation_forward_stack[:-80]
        self._restore_pdf_navigation_position(
            self.pdf_navigation_back_stack.pop()
        )

    def go_pdf_navigation_forward(
        self,
    ) -> None:
        if not self.pdf_navigation_forward_stack:
            return
        current = self._current_pdf_navigation_position()
        if current is not None:
            self.pdf_navigation_back_stack.append(current)
            del self.pdf_navigation_back_stack[:-80]
        self._restore_pdf_navigation_position(
            self.pdf_navigation_forward_stack.pop()
        )

    def _jump_to_link(
        self,
        link_index: int,
    ) -> None:
        if not (0 <= link_index < len(self.pdf_links)):
            return
        link = self.pdf_links[link_index]
        self._push_pdf_navigation_back()
        self.page_index = int(link["target_page"])
        self.anchor_y = float(link.get("target_y") or 0.0)
        self._jump_to_page_anchor(self.page_index, self.anchor_y)

    def _jump_to_page_anchor(
        self,
        page_index: int,
        anchor_y: float = 0.0,
    ) -> None:
        if not (0 <= page_index < len(self.page_layouts)):
            return
        layout = self.page_layouts[page_index]
        target_y = max(0.0, float(layout["y"]) + anchor_y * self.zoom - 12.0)
        self.canvas.yview_moveto(
            min(
                1.0,
                max(0.0, target_y / max(1.0, self.content_height)),
            )
        )
        self._after_canvas_scroll()

    def _link_target_from_pdf_link(
        self,
        document: Any,
        link: dict[str, Any],
    ) -> tuple[int, float] | None:
        target_page = int(link.get("page", -1))
        destination = None
        named = str(link.get("nameddest") or "").strip()
        if named:
            destination = (document.resolve_names() or {}).get(named)
        if destination is not None:
            resolved = _destination_to_page_anchor(document, destination)
            if resolved is not None:
                return resolved
        if not (0 <= target_page < int(document.page_count)):
            return None
        target_y = 0.0
        point = link.get("to")
        if point is not None:
            try:
                point_y = float(point.y)
            except AttributeError:
                try:
                    point_y = float(point[1])
                except (TypeError, ValueError, IndexError):
                    point_y = 0.0
            page = document.load_page(target_page)
            target_y = max(0.0, float(page.rect.height) - point_y)
        return target_page, target_y

    def _load_pdf_links(
        self,
        document: Any,
    ) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for page_index in range(int(document.page_count)):
            page = document.load_page(page_index)
            for link in page.get_links() or []:
                rectangle = link.get("from")
                target = self._link_target_from_pdf_link(document, link)
                if rectangle is None or target is None:
                    continue
                target_page, target_y = target
                links.append(
                    {
                        "page": page_index,
                        "x0": float(rectangle.x0),
                        "y0": float(rectangle.y0),
                        "x1": float(rectangle.x1),
                        "y1": float(rectangle.y1),
                        "target_page": target_page,
                        "target_y": target_y,
                    }
                )
        return links

    def _build_continuous_layout(
        self,
        document: Any,
    ) -> None:
        self.page_layouts = []
        viewport_width = max(1.0, float(self.canvas.winfo_width()))
        page_sizes = []
        for page_index in range(int(document.page_count)):
            rect = document.load_page(page_index).rect
            scaled_width = float(rect.width) * self.zoom
            scaled_height = float(rect.height) * self.zoom
            page_sizes.append((float(rect.width), float(rect.height), scaled_width, scaled_height))
        max_scaled_width = max((item[2] for item in page_sizes), default=viewport_width)
        self.content_width = max(viewport_width, max_scaled_width + self.page_margin * 2)
        cursor_y = self.page_margin
        for pdf_width, pdf_height, scaled_width, scaled_height in page_sizes:
            self.page_layouts.append(
                {
                    "x": max(self.page_margin, (self.content_width - scaled_width) / 2.0),
                    "y": cursor_y,
                    "pdf_width": pdf_width,
                    "pdf_height": pdf_height,
                    "scaled_width": scaled_width,
                    "scaled_height": scaled_height,
                }
            )
            cursor_y += scaled_height + self.page_gap
        self.content_height = max(
            float(self.canvas.winfo_height()),
            cursor_y + self.page_margin,
        )
        self.canvas.configure(scrollregion=(0, 0, self.content_width, self.content_height))

    def _draw_page_placeholders(self) -> None:
        for index, layout in enumerate(self.page_layouts):
            x0 = float(layout["x"])
            y0 = float(layout["y"])
            x1 = x0 + float(layout["scaled_width"])
            y1 = y0 + float(layout["scaled_height"])
            self.canvas.create_rectangle(
                x0 + 5,
                y0 + 5,
                x1 + 5,
                y1 + 5,
                outline="",
                fill="#505050",
                tags=(f"page-shadow-{index}", "page-background"),
            )
            self.canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                outline="#b8b8b8",
                fill="white",
                tags=(f"page-bg-{index}", "page-background"),
            )

    def _visible_page_indices(self) -> list[int]:
        if not self.page_layouts:
            return []
        _left, top, _right, bottom = self._visible_canvas_bounds()
        top -= 400.0
        bottom += 400.0
        visible: list[int] = []
        for page_index, layout in enumerate(self.page_layouts):
            page_top = float(layout["y"])
            page_bottom = page_top + float(layout["scaled_height"])
            if page_bottom >= top and page_top <= bottom:
                visible.append(page_index)
        return visible

    def _render_visible_pages(self) -> None:
        if self.pdf_path is None or not self.page_layouts:
            return
        visible = set(self._visible_page_indices())
        for page_index in list(self.page_image_items):
            if page_index not in visible:
                self.canvas.delete(self.page_image_items.pop(page_index))
                self.page_photos.pop(page_index, None)
        if not visible:
            return
        document = fitz.open(str(self.pdf_path))
        try:
            for page_index in sorted(visible):
                if page_index in self.page_image_items:
                    continue
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(self.zoom, self.zoom), alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                photo = ImageTk.PhotoImage(image, master=self.canvas)
                layout = self.page_layouts[page_index]
                item = self.canvas.create_image(
                    float(layout["x"]),
                    float(layout["y"]),
                    anchor=tk.NW,
                    image=photo,
                    tags=(f"page-image-{page_index}", "page-image"),
                )
                self.page_photos[page_index] = photo
                self.page_image_items[page_index] = item
        finally:
            document.close()
        for item in self.search_highlight_items + self.selection_highlight_items:
            self.canvas.tag_raise(item)
        if self.selection_preview_item is not None:
            self.canvas.tag_raise(self.selection_preview_item)
        if self.link_highlight_item is not None:
            self.canvas.tag_raise(self.link_highlight_item)
        if self.vocabulary_popup_item is not None:
            self.canvas.tag_raise(self.vocabulary_popup_item)

    def _load_pdf_outline(
        self,
        pdf_path: Path,
    ) -> None:
        self.outline_entries = []
        for item in self.outline_tree.get_children():
            self.outline_tree.delete(item)
        document = fitz.open(str(pdf_path))
        try:
            toc = document.get_toc(simple=False) or []
            for index, entry in enumerate(toc):
                if len(entry) < 3:
                    continue
                level = max(1, int(entry[0]))
                title = str(entry[1] or "").strip() or "(未命名)"
                page_number = int(entry[2] or 0)
                if page_number <= 0:
                    continue
                anchor_y = 0.0
                named_destination = ""
                if len(entry) >= 4 and isinstance(entry[3], dict):
                    named_destination = str(entry[3].get("nameddest") or "").strip()
                    resolved = _destination_to_page_anchor(document, entry[3])
                    if resolved is not None:
                        page_index, _destination_y = resolved
                        page_number = page_index + 1
                page_number = _outline_heading_page(
                    document,
                    title,
                    page_number - 1,
                ) + 1
                self.outline_entries.append(
                    {
                        "level": level,
                        "title": title,
                        "page": max(0, page_number - 1),
                        "anchor_y": anchor_y,
                        "named_destination": named_destination,
                    }
                )
        finally:
            document.close()

        if not self.outline_entries:
            self.outline_tree.insert("", tk.END, text="此 PDF 没有目录", values=("-1",))
            return

        parents: dict[int, str] = {}
        for index, entry in enumerate(self.outline_entries):
            level = int(entry["level"])
            parent = parents.get(level - 1, "")
            title = str(entry["title"])
            text = pdf_outline_display_text(
                level,
                title,
                str(entry.get("named_destination") or ""),
            )
            item_id = self.outline_tree.insert(parent, tk.END, iid=str(index), text=text)
            parents[level] = item_id
            for child_level in list(parents):
                if child_level > level:
                    del parents[child_level]
            if level == 1:
                self.outline_tree.item(item_id, open=True)

    def _on_outline_pointer_press(
        self,
        event: tk.Event,
    ) -> str | None:
        item_id = self.outline_tree.identify_row(event.y)
        element = self.outline_tree.identify_element(event.x, event.y)
        if item_id and "indicator" in str(element).casefold():
            is_open = bool(self.outline_tree.item(item_id, "open"))
            self.outline_tree.item(item_id, open=not is_open)
            return "break"
        if item_id and element:
            return None
        self._cancel_outline_selection_fade()
        selection = self.outline_tree.selection()
        if selection:
            self.outline_tree.selection_remove(*selection)
        self.outline_tree.focus("")
        return "break"

    def _cancel_outline_selection_fade(self) -> None:
        self._outline_selection_fade_generation += 1
        for after_id in self._outline_selection_fade_after_ids:
            try:
                self.window.after_cancel(after_id)
            except tk.TclError:
                pass
        self._outline_selection_fade_after_ids = []
        item_id = self._outline_selection_fade_item
        self._outline_selection_fade_item = ""
        if item_id and self.outline_tree.exists(item_id):
            tags = tuple(
                tag
                for tag in self.outline_tree.item(item_id, "tags")
                if tag != "outline-selection-fade"
            )
            self.outline_tree.item(item_id, tags=tags)

    def _schedule_outline_selection_fade(self, item_id: str) -> None:
        self._cancel_outline_selection_fade()
        generation = self._outline_selection_fade_generation
        self._outline_selection_fade_item = item_id

        def rgb(color: str, fallback: str) -> tuple[int, int, int]:
            try:
                return tuple(value // 257 for value in self.window.winfo_rgb(color or fallback))
            except tk.TclError:
                return tuple(value // 257 for value in self.window.winfo_rgb(fallback))

        def start_fade() -> None:
            if (
                generation != self._outline_selection_fade_generation
                or not self.outline_tree.exists(item_id)
                or item_id not in self.outline_tree.selection()
            ):
                return
            selected_background = rgb(
                str(
                    self.outline_style.lookup(
                        self.outline_style_name,
                        "background",
                        ("selected",),
                    )
                    or ""
                ),
                "#4A6984",
            )
            selected_foreground = rgb(
                str(
                    self.outline_style.lookup(
                        self.outline_style_name,
                        "foreground",
                        ("selected",),
                    )
                    or ""
                ),
                "#FFFFFF",
            )
            normal_background = rgb(
                str(self.outline_style.lookup(self.outline_style_name, "background") or ""),
                "#FFFFFF",
            )
            normal_foreground = rgb(
                str(self.outline_style.lookup(self.outline_style_name, "foreground") or ""),
                "#000000",
            )
            tags = tuple(self.outline_tree.item(item_id, "tags"))
            if "outline-selection-fade" not in tags:
                self.outline_tree.item(
                    item_id,
                    tags=tags + ("outline-selection-fade",),
                )
            self.outline_tree.selection_remove(item_id)

            steps = 8

            def apply_step(step: int) -> None:
                if (
                    generation != self._outline_selection_fade_generation
                    or not self.outline_tree.exists(item_id)
                ):
                    return
                ratio = min(1.0, max(0.0, step / steps))

                def blend(start: tuple[int, int, int], end: tuple[int, int, int]) -> str:
                    values = [
                        round(left + (right - left) * ratio)
                        for left, right in zip(start, end)
                    ]
                    return "#" + "".join(f"{value:02X}" for value in values)

                self.outline_tree.tag_configure(
                    "outline-selection-fade",
                    background=blend(selected_background, normal_background),
                    foreground=blend(selected_foreground, normal_foreground),
                )
                if step < steps:
                    after_id = self.window.after(45, lambda: apply_step(step + 1))
                    self._outline_selection_fade_after_ids.append(after_id)
                    return
                tags_after = tuple(
                    tag
                    for tag in self.outline_tree.item(item_id, "tags")
                    if tag != "outline-selection-fade"
                )
                self.outline_tree.item(item_id, tags=tags_after)
                self._outline_selection_fade_item = ""

            apply_step(0)

        after_id = self.window.after(3000, start_fade)
        self._outline_selection_fade_after_ids.append(after_id)

    def _on_outline_selected(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        selection = self.outline_tree.selection()
        if not selection:
            return "break"
        try:
            index = int(selection[0])
        except ValueError:
            return "break"
        if not (0 <= index < len(self.outline_entries)):
            return "break"
        entry = self.outline_entries[index]
        self._push_pdf_navigation_back()
        self.page_index = int(entry["page"])
        # Outline navigation is page-based. PDF destinations use several
        # coordinate conventions, so always place the requested page at the
        # top of the reading viewport.
        self.anchor_y = 0.0
        self._jump_to_page_anchor(self.page_index, 0.0)
        self._schedule_outline_selection_fade(selection[0])
        return "break"

    def show_problem(
        self,
        pdf_path: Path,
        problem_code: str,
        problem_title: str,
    ) -> None:
        pdf_path = pdf_path.resolve()

        if not pdf_path.is_file():
            raise FileNotFoundError(
                "尚未找到已生成的 PDF：\n"
                f"{pdf_path}\n\n"
                "请先点击“生成章节与 PDF”。"
            )

        page_index, anchor_y = self._resolve_problem_location(
            pdf_path,
            problem_code,
        )

        self.pdf_path = pdf_path
        self.problem_code = problem_code
        self.problem_title = problem_title
        self.page_index = page_index
        self.anchor_y = anchor_y
        self._clear_pdf_navigation_history()
        self.clear_search()
        self._load_pdf_outline(pdf_path)
        self.problem_var.set(
            f"{problem_code}  {problem_title}".strip()
        )
        self.window.title("标准题 PDF 定位")

        self.render_page()

        self.window.deiconify()
        self.maximize_window()
        self.window.after_idle(self.apply_default_outline_width)
        self.window.lift()
        self.window.focus_force()
        self.canvas.focus_set()
        self.window.after(80, lambda: self._jump_to_page_anchor(page_index, anchor_y))

    def show_pdf_location(
        self,
        pdf_path: Path,
        *,
        page_index: int = 0,
        anchor_y: float = 0.0,
        title: str = "",
    ) -> None:
        """Open a PDF in the standard preview and jump to a known page location."""

        pdf_path = pdf_path.resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(f"尚未找到已生成的 PDF：\n{pdf_path}")

        target_page = max(0, int(page_index))
        target_anchor = max(0.0, float(anchor_y))
        self.pdf_path = pdf_path
        self.problem_code = ""
        self.problem_title = title.strip()
        self.page_index = target_page
        self.anchor_y = target_anchor
        self._clear_pdf_navigation_history()
        self.clear_search()
        self._load_pdf_outline(pdf_path)
        self.problem_var.set(self.problem_title or pdf_path.name)
        self.window.title("讲义 PDF 定位")

        self.render_page()
        target_page = self.page_index
        self.window.deiconify()
        self.maximize_window()
        self.window.after_idle(self.apply_default_outline_width)
        self.window.lift()
        self.window.focus_force()
        self.canvas.focus_set()
        self.window.after(
            80,
            lambda: self._jump_to_page_anchor(target_page, target_anchor),
        )

    def show_search(
        self,
        pdf_path: Path,
        query: str,
        title: str = "",
    ) -> None:
        pdf_path = pdf_path.resolve()
        query = query.strip()
        if not query:
            raise ValueError("请先选择要在 PDF 中定位的单词或短语。")
        if not pdf_path.is_file():
            raise FileNotFoundError(
                "尚未找到已生成的 PDF：\n"
                f"{pdf_path}\n\n"
                "请先点击“生成章节与 PDF”。"
            )
        results = self._search_pdf_positions(pdf_path, query)
        if not results:
            raise LookupError(f"当前 PDF 中没有找到：{query}")
        self.pdf_path = pdf_path
        self.problem_code = ""
        self.problem_title = title or query
        self.search_query = query
        self.search_results = results
        self.current_search_index = 0
        self.problem_var.set(title or f"PDF 词汇定位：{query}")
        self.window.title("PDF 文本定位")
        self._clear_pdf_navigation_history()
        self._set_search_navigation_state()
        self._load_pdf_outline(pdf_path)
        self.page_index = int(results[0]["page"])
        self.anchor_y = max(0.0, float(results[0]["y0"]))
        self.render_page()
        self.window.deiconify()
        self.maximize_window()
        self.window.after_idle(self.apply_default_outline_width)
        self.window.lift()
        self.window.focus_force()
        self._jump_to_search_result(0)
        self.canvas.focus_set()

    def _search_pdf_positions(
        self,
        pdf_path: Path,
        query: str,
    ) -> list[dict[str, Any]]:
        return pdf_search_positions(pdf_path, query)

    def _jump_to_search_result(
        self,
        index: int,
    ) -> None:
        if not self.search_results:
            return
        self.current_search_index = index % len(self.search_results)
        result = self.search_results[self.current_search_index]
        self.page_index = int(result["page"])
        self.anchor_y = max(0.0, float(result["y0"]))
        self._set_search_navigation_state()
        self._redraw_search_highlights()
        self._jump_to_page_anchor(self.page_index, self.anchor_y)

    def change_search_result(
        self,
        offset: int,
    ) -> None:
        if not self.search_results:
            return
        self._jump_to_search_result(self.current_search_index + offset)

    def _resolve_problem_location(
        self,
        pdf_path: Path,
        problem_code: str,
    ) -> tuple[int, float]:
        anchor = problem_pdf_anchor(problem_code)
        document = fitz.open(str(pdf_path))

        try:
            names = document.resolve_names() or {}
            destination = names.get(anchor)

            if destination is not None:
                page_index = int(
                    destination.get("page", -1)
                )

                if 0 <= page_index < document.page_count:
                    page = document.load_page(page_index)
                    target = destination.get("to")
                    anchor_y = 0.0

                    if target is not None:
                        try:
                            target_y = float(target.y)
                        except AttributeError:
                            target_y = float(target[1])

                        # resolve_names() 返回的是 PDF 坐标：
                        # 原点位于左下角；Canvas / PyMuPDF 页面坐标
                        # 的原点位于左上角，因此需要转换。
                        anchor_y = max(
                            0.0,
                            float(page.rect.height) - target_y,
                        )

                    return page_index, anchor_y

            # 兼容尚未重新生成锚点的旧 PDF：
            # 尝试搜索 PDF 中打印出来的永久题号。
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                rectangles = page.search_for(problem_code)

                if rectangles:
                    return (
                        page_index,
                        max(0.0, float(rectangles[0].y0)),
                    )
        finally:
            document.close()

        raise LookupError(
            f"当前 PDF 中没有找到标准题 {problem_code}。\n\n"
            "请先重新点击“生成章节与 PDF”，"
            "使最新 PDF 写入题目锚点。"
        )

    def render_page(self) -> None:
        if self.pdf_path is None:
            return

        self._invalidate_pdf_text_state()
        self._clear_search_highlights()
        if self.link_highlight_item is not None:
            self.canvas.delete(self.link_highlight_item)
            self.link_highlight_item = None
        self.hover_link_index = None
        self.pressed_link_index = None
        self.page_photos = {}
        self.page_image_items = {}
        self.page_layouts = []
        self.pdf_links = []
        self.canvas.delete("all")
        document = fitz.open(str(self.pdf_path))

        try:
            self.page_count = int(document.page_count)

            if self.page_count <= 0:
                raise RuntimeError("PDF 中没有页面。")

            self.page_index = max(
                0,
                min(self.page_index, self.page_count - 1),
            )
            self._build_continuous_layout(document)
            self.pdf_links = self._load_pdf_links(document)
        finally:
            document.close()

        self._draw_page_placeholders()
        self._render_visible_pages()
        self._redraw_search_highlights()

        self.page_var.set(
            f"PDF 第 {self.page_index + 1} / {self.page_count} 页"
        )
        self.zoom_var.set(
            f"{round(self.zoom * 100)}%"
        )

        anchor_y = self.anchor_y

        def move_to_anchor() -> None:
            self.canvas.update_idletasks()
            self._center_horizontal_view()

            if anchor_y is None:
                self.canvas.yview_moveto(0.0)
                self._after_canvas_scroll()
                return

            self._jump_to_page_anchor(self.page_index, anchor_y)

        self.window.after_idle(move_to_anchor)

    def change_page(self, offset: int) -> None:
        target = self.page_index + offset

        if not (0 <= target < self.page_count):
            return

        self.page_index = target
        self.anchor_y = 0.0
        self._jump_to_page_anchor(target, 0.0)

    def change_zoom(self, delta: float) -> None:
        anchor_page = self.page_index
        anchor_y = 0.0
        if 0 <= anchor_page < len(self.page_layouts):
            _left, top, _right, _bottom = self._visible_canvas_bounds()
            layout = self.page_layouts[anchor_page]
            anchor_y = max(0.0, (top - float(layout["y"])) / max(self.zoom, 0.01))
        self.zoom = min(
            5.0,
            max(0.65, self.zoom + delta),
        )
        self.page_index = anchor_page
        self.anchor_y = anchor_y
        self.render_page()

    def reload_problem(self) -> None:
        if self.pdf_path is None:
            return
        if self.problem_code:
            self.show_problem(
                self.pdf_path,
                self.problem_code,
                self.problem_title,
            )
        elif self.search_query:
            self.show_search(
                self.pdf_path,
                self.search_query,
                self.problem_title,
            )
        else:
            self.show_pdf_location(
                self.pdf_path,
                page_index=self.page_index,
                anchor_y=float(self.anchor_y or 0.0),
                title=self.problem_title,
            )


def main() -> None:
    raise SystemExit(
        "problem_bank_center.py is now a compatibility module. "
        "Use shared/scripts/problem_bank_center_qt.py to start the application."
    )


if __name__ == "__main__":
    main()
