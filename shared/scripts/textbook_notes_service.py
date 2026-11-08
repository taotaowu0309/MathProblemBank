"""LaTeX study notes linked to textbook PDF bookmarks.

A note is the body of a small LaTeX document (usually written by an AI on
request) stored under ``TextbookNotes/notes/<note_code>/note.tex`` and
compiled to ``note.pdf`` beside it.  The body never carries its own preamble:
every note is wrapped in the same document with ``shared/templates/
textbook-note.sty`` at compile time, so all notes share one fixed layout.  Notes are linked to one or more bookmarks (a chapter
or a section) of any textbook PDF.  Textbooks are identified by the SHA-256 of
the PDF file rather than its path, because the same book lives at different
paths on Windows and macOS; a book whose file changed (for example an OCR
reading copy) still matches by file name and page count.  A note can also be
linked to one or more question-bank questions, identified by a stable key such
as ``english:grammar:XYGE-01-002`` (the question bank is a separate database,
so the key is stored instead of a foreign key).

The service owns no Qt widgets: the PDF reader panel and the registered AI
operations call the same methods, so validation, backups and readback are
shared.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_NOTES_ROOT = APP_PATHS.workspace_root / "TextbookNotes"
TEMPLATE_DIR = ROOT_DIR / "shared" / "templates"
NOTE_STYLE_PATH = TEMPLATE_DIR / "textbook-note.sty"
NOTE_PROMPT_PATH = TEMPLATE_DIR / "textbook_note_chatgpt_prompt.txt"
NOTE_EXAMPLE_PATH = TEMPLATE_DIR / "textbook_note_example.tex"
QUESTION_NOTE_PROMPT_PATH = TEMPLATE_DIR / "question_note_chatgpt_prompt.txt"
QUESTION_NOTE_EXAMPLE_PATH = TEMPLATE_DIR / "question_note_example.tex"
# ElegantNote colour theme used for every note (green, cyan, blue, sakura, black, brown).
NOTE_THEME_COLOR = "blue"
# Where the shipped example note belongs in the Xuan Yu-You grammar book.
NOTE_EXAMPLE_META = {
    "title": "副词子句减化：分词构句",
    "book": "旋元佑文法",
    "bookmark": "第三部分 减化子句 › 副词子句减化",
    "pages": "400--415",
}

# Where the shipped question-note example belongs in 《旋元佑文法解题》.
QUESTION_NOTE_EXAMPLE_META = {
    "title": "deny 后面不加 of",
    "book": "旋元佑文法解题",
    "bookmark": "第 1 章 基本句型 › 第 2 题（台大 · 挑错题）",
    "pages": "13",
}
if APP_PATHS.public_release:
    NOTE_EXAMPLE_META = {
        "title": "合成示例：过去时", "book": "格式演示（非教材原文）",
        "bookmark": "合成示例", "pages": "",
    }
    QUESTION_NOTE_EXAMPLE_META = {
        "title": "合成示例：动词形式", "book": "合成练习",
        "bookmark": "合成示例", "pages": "",
    }
QUESTION_KEY_PATTERN = re.compile(r"[a-z]+:[a-z]+:[A-Za-z0-9._-]+")

TEXTBOOK_NOTES_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS textbook_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'ai' CHECK(source_kind IN ('ai','manual')),
    tex_path TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    tex_sha256 TEXT NOT NULL DEFAULT '',
    compiled_tex_sha256 TEXT NOT NULL DEFAULT '',
    compile_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(compile_status IN ('pending','compiled','failed')),
    compile_error TEXT NOT NULL DEFAULT '',
    compiled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS textbook_note_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    document_sha256 TEXT NOT NULL,
    document_name TEXT NOT NULL DEFAULT '',
    document_title TEXT NOT NULL DEFAULT '',
    document_page_count INTEGER NOT NULL DEFAULT 0,
    bookmark_title TEXT NOT NULL,
    bookmark_level INTEGER NOT NULL DEFAULT 1,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(note_id) REFERENCES textbook_notes(id) ON DELETE CASCADE,
    UNIQUE(note_id, document_sha256, bookmark_title, page_start)
);
CREATE INDEX IF NOT EXISTS idx_textbook_note_links_document
ON textbook_note_links(document_sha256, page_start, page_end);
CREATE INDEX IF NOT EXISTS idx_textbook_note_links_name
ON textbook_note_links(document_name, document_page_count);

CREATE TABLE IF NOT EXISTS textbook_note_question_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    question_key TEXT NOT NULL,
    question_title TEXT NOT NULL DEFAULT '',
    book_title TEXT NOT NULL DEFAULT '',
    source_page INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(note_id) REFERENCES textbook_notes(id) ON DELETE CASCADE,
    UNIQUE(note_id, question_key)
);
CREATE INDEX IF NOT EXISTS idx_textbook_note_question_links_key
ON textbook_note_question_links(question_key);
"""

BOOKMARK_SEPARATOR = " › "

# Commands that would change the shared layout or pull in other files.  A note
# body may use any environment and macro of textbook-note.sty, nothing more.
_FORBIDDEN_BODY_COMMANDS = (
    "documentclass", "usepackage", "RequirePackage", "geometry", "newgeometry",
    "pagestyle", "thispagestyle", "input", "include", "maketitle", "title",
    "author", "date", "definecolor", "setmainfont", "setCJKmainfont",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _escape_latex_text(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


def clean_note_body(latex: str) -> str:
    """Return the note body, refusing anything that would alter the fixed layout."""

    text = str(latex or "").replace("\r\n", "\n").strip()
    # Chat answers often arrive wrapped in a Markdown code fence.
    fenced = re.fullmatch(r"```[A-Za-z]*\n(.*?)\n?```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise ValueError("笔记 LaTeX 内容为空。")
    if re.search(r"\\(begin|end)\{document\}", text):
        raise ValueError("笔记只写正文：不要包含 \\documentclass、导言区或 document 环境。")
    found = sorted({
        match.group(1) for match in re.finditer(r"\\([A-Za-z]+)\b", text)
        if match.group(1) in _FORBIDDEN_BODY_COMMANDS
    })
    if found:
        raise ValueError(
            "笔记采用固定排版，正文不能使用这些命令："
            + "、".join("\\" + name for name in found)
        )
    return text + "\n"


def note_document_tex(meta: Mapping[str, Any]) -> str:
    """The fixed wrapper that turns body.tex into a complete note document.

    The page, fonts, colour theme and title block come from the open-source
    ElegantNote class; textbook-note.sty adds the example-sentence commands.
    """

    def field(name: str) -> str:
        # xeCJK drops plain spaces between Chinese characters; keep them explicit.
        return _escape_latex_text(str(meta.get(name) or "").strip()).replace(" ", "\\ ")

    source = "\\ ·\\ ".join(
        part for part in (
            "示例" if meta.get("example") else "",
            field("book"),
            f"p.\\,{field('pages')}" if field("pages") else "",
        ) if part
    )
    return (
        f"\\documentclass[lang=cn,device=pad,color={NOTE_THEME_COLOR},11pt]{{elegantnote}}\n"
        "\\usepackage{textbook-note}\n"
        f"\\title{{{field('title')}}}\n"
        f"\\author{{{source}}}\n"
        f"\\institute{{{field('bookmark')}}}\n"
        "\\date{}\n"
        "\\begin{document}\n\\maketitle\n\\input{body}\n\\end{document}\n"
    )


def writing_prompt(*, book: str = "", bookmarks: Iterable[str] = (), pages: str = "", ideas: str = "") -> str:
    """The ChatGPT writing prompt (built around the user's own ideas) plus the example note."""

    prompt = NOTE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    target = "、".join(item for item in bookmarks if item) or "（填写书签，例如：第三部分 减化子句 › 副词子句减化）"
    prompt = (
        prompt.replace("{{BOOK}}", book or "（填写书名）")
        .replace("{{BOOKMARK}}", target)
        .replace("{{PAGES}}", pages or "（填写页码）")
        .replace("{{IDEAS}}", str(ideas or "").strip() or "（在这里写你想记的内容：想弄清的问题、自己的理解、容易混的地方，可以很零碎）")
    )
    example = NOTE_EXAMPLE_PATH.read_text(encoding="utf-8").strip()
    return f"{prompt}\n\n===== 示例 =====\n\n{example}\n"


def _shared_format_rules() -> str:
    """The format part of the textbook prompt, reused verbatim by question notes."""

    prompt = NOTE_PROMPT_PATH.read_text(encoding="utf-8")
    start = prompt.index("【格式")
    end = prompt.index("下面是一个完整的示例")
    return prompt[start:end].strip()


def question_writing_prompt(*, book: str = "", question: str = "", ideas: str = "") -> str:
    """The ChatGPT prompt for a note on one question: the question, the user's ideas,
    the shared format rules and the complete question-note example."""

    prompt = QUESTION_NOTE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    prompt = (
        prompt.replace("{{BOOK}}", book or "（填写书名）")
        .replace("{{QUESTION}}", str(question or "").strip() or "（在这里贴上题目、答案和原书解析）")
        .replace("{{IDEAS}}", str(ideas or "").strip() or "（在这里写你的想法：为什么选错了、哪里没看懂、想弄清什么，可以很零碎）")
        .replace("{{FORMAT_RULES}}", _shared_format_rules())
    )
    example = QUESTION_NOTE_EXAMPLE_PATH.read_text(encoding="utf-8").strip()
    return f"{prompt}\n\n===== 示例 =====\n\n{example}\n"


def outline_with_ranges(entries: Iterable[tuple[int, str, int]], page_count: int) -> list[dict[str, Any]]:
    """Give each ``(level, title, page_number)`` bookmark its page range and full path.

    A bookmark covers its own start page up to the page before the next
    bookmark of the same or a higher level (or the end of the document).
    """

    items = [(max(1, int(level)), str(title).strip() or "(未命名)", max(1, int(page))) for level, title, page in entries]
    result: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    for index, (level, title, page) in enumerate(items):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = page_count
        for next_level, _next_title, next_page in items[index + 1:]:
            if next_level <= level:
                end = next_page - 1
                break
        result.append({
            "index": index,
            "level": level,
            "title": title,
            "path_title": BOOKMARK_SEPARATOR.join(part for _level, part in stack),
            "page_start": page,
            "page_end": max(page, min(end, page_count) if page_count else end),
        })
    return result


class TextbookNotesService:
    def __init__(self, root: Path | str = DEFAULT_NOTES_ROOT) -> None:
        self.root = Path(root).resolve()
        self.database_path = self.root / "data" / "textbook_notes.db"
        self.backup_dir = self.root / "backups"
        self.notes_dir = self.root / "notes"
        self._identity_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._schema_ready = False

    # -- database -------------------------------------------------------------

    def connect(self, *, rows: bool = False) -> sqlite3.Connection:
        self._ensure_schema()
        return self._open(rows=rows)

    def _open(self, *, rows: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if rows:
            connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        # The store is created lazily so that merely opening a PDF never
        # writes into the synchronised data folder.
        if self._schema_ready:
            return
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as connection:
            connection.executescript(TEXTBOOK_NOTES_SCHEMA)
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version','1')")
            connection.commit()
        self._schema_ready = True

    def has_store(self) -> bool:
        return self.database_path.is_file()

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> None:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"教材笔记数据库完整性检查失败：{result}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("教材笔记数据库存在外键错误。")

    def backup(self, reason: str) -> Path | None:
        if not self.database_path.is_file():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_") or "change"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        target = self.backup_dir / f"textbook_notes_before_{safe}_{stamp}.db"
        with closing(sqlite3.connect(self.database_path, timeout=30)) as source:
            with closing(sqlite3.connect(target, timeout=30)) as destination:
                self._integrity(source)
                source.backup(destination)
                destination.commit()
        shutil.copy2(target, self.backup_dir / "textbook_notes_latest.db")
        return target

    # -- textbooks ------------------------------------------------------------

    def document_identity(self, pdf_path: Path | str) -> dict[str, Any]:
        path = Path(pdf_path).resolve()
        if not path.is_file():
            raise ValueError(f"PDF 不存在：{path}")
        stat = path.stat()
        key = (str(path), int(stat.st_size), int(stat.st_mtime_ns))
        cached = self._identity_cache.get(key)
        if cached is not None:
            return dict(cached)
        import fitz

        with fitz.open(str(path)) as document:
            page_count = int(document.page_count)
            title = str((document.metadata or {}).get("title") or "").strip()
        identity = {
            "sha256": _sha256(path), "name": path.name, "page_count": page_count,
            "path": str(path), "title": title or path.stem,
        }
        self._identity_cache[key] = identity
        return dict(identity)

    def pdf_outline(self, pdf_path: Path | str) -> list[dict[str, Any]]:
        import fitz

        with fitz.open(str(Path(pdf_path).resolve())) as document:
            toc = document.get_toc(simple=True) or []
            page_count = int(document.page_count)
        entries = [(int(item[0]), str(item[1]), int(item[2])) for item in toc if len(item) >= 3 and int(item[2]) > 0]
        return outline_with_ranges(entries, page_count)

    @staticmethod
    def _document_clause(identity: Mapping[str, Any]) -> tuple[str, list[Any]]:
        return (
            "(l.document_sha256=? OR (l.document_name=? AND l.document_page_count=?))",
            [identity["sha256"], identity["name"], int(identity["page_count"])],
        )

    # -- reading notes --------------------------------------------------------

    def _note_dict(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        note = dict(row)
        tex_file = self.root / str(note["tex_path"])
        pdf_file = self.root / str(note["pdf_path"])
        note["tex_file"] = str(tex_file)
        note["pdf_file"] = str(pdf_file)
        note["tex_consistent"] = tex_file.is_file() and _sha256(tex_file) == note.get("tex_sha256")
        # The panel shows a PDF only when it was built from the current TeX.
        note["pdf_current"] = bool(
            pdf_file.is_file()
            and note["tex_consistent"]
            and note.get("compile_status") == "compiled"
            and note.get("compiled_tex_sha256") == note.get("tex_sha256")
        )
        note["pdf_available"] = pdf_file.is_file()
        return note

    def _links(self, connection: sqlite3.Connection, note_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not note_ids:
            return {}
        placeholders = ",".join("?" for _ in note_ids)
        rows = connection.execute(
            f"SELECT * FROM textbook_note_links WHERE note_id IN ({placeholders}) ORDER BY document_name,page_start,id",
            note_ids,
        ).fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(int(row["note_id"]), []).append(dict(row))
        return result

    @staticmethod
    def _question_links(connection: sqlite3.Connection, note_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not note_ids:
            return {}
        placeholders = ",".join("?" for _ in note_ids)
        rows = connection.execute(
            f"SELECT * FROM textbook_note_question_links WHERE note_id IN ({placeholders}) "
            "ORDER BY question_key,id",
            note_ids,
        ).fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(int(row["note_id"]), []).append(dict(row))
        return result

    def _with_links(self, connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        ids = [int(row["id"]) for row in rows]
        links = self._links(connection, ids)
        question_links = self._question_links(connection, ids)
        return [
            {
                **self._note_dict(row),
                "links": links.get(int(row["id"]), []),
                "question_links": question_links.get(int(row["id"]), []),
            }
            for row in rows
        ]

    def note(self, note_id: int) -> dict[str, Any] | None:
        if not self.has_store():
            return None
        with closing(self.connect(rows=True)) as connection:
            row = connection.execute("SELECT * FROM textbook_notes WHERE id=?", (int(note_id),)).fetchone()
            if row is None:
                return None
            return self._with_links(connection, [row])[0]

    def list_notes(self, *, keyword: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if not self.has_store():
            return []
        clauses, args = "", []
        if keyword.strip():
            clauses = (
                "WHERE n.title LIKE ? OR n.note_code LIKE ? OR EXISTS (SELECT 1 FROM textbook_note_links l "
                "WHERE l.note_id=n.id AND (l.bookmark_title LIKE ? OR l.document_name LIKE ?)) "
                "OR EXISTS (SELECT 1 FROM textbook_note_question_links q "
                "WHERE q.note_id=n.id AND (q.question_key LIKE ? OR q.question_title LIKE ?))"
            )
            args = [f"%{keyword.strip()}%"] * 6
        with closing(self.connect(rows=True)) as connection:
            rows = connection.execute(
                f"SELECT n.* FROM textbook_notes n {clauses} ORDER BY n.updated_at DESC,n.id DESC LIMIT ?",
                [*args, max(1, min(int(limit), 2000))],
            ).fetchall()
            return self._with_links(connection, list(rows))

    def notes_for_question(self, question_key: str) -> list[dict[str, Any]]:
        """Notes linked to one question-bank question, newest first."""

        if not self.has_store():
            return []
        with closing(self.connect(rows=True)) as connection:
            rows = connection.execute(
                "SELECT n.* FROM textbook_notes n WHERE EXISTS (SELECT 1 FROM textbook_note_question_links q "
                "WHERE q.note_id=n.id AND q.question_key=?) ORDER BY n.updated_at DESC,n.id DESC",
                (str(question_key or "").strip(),),
            ).fetchall()
            return self._with_links(connection, list(rows))

    def question_note_counts(self, question_keys: Iterable[str]) -> dict[str, int]:
        keys = [str(key) for key in question_keys if str(key or "").strip()]
        if not keys or not self.has_store():
            return {}
        placeholders = ",".join("?" for _ in keys)
        with closing(self.connect(rows=True)) as connection:
            rows = connection.execute(
                f"SELECT question_key,COUNT(*) AS count FROM textbook_note_question_links "
                f"WHERE question_key IN ({placeholders}) GROUP BY question_key",
                keys,
            ).fetchall()
        return {str(row["question_key"]): int(row["count"]) for row in rows}

    def notes_for_document(self, pdf_path: Path | str, *, page_number: int | None = None) -> list[dict[str, Any]]:
        """Notes linked to this textbook; with a page, only bookmarks covering it.

        Page results put the most specific (narrowest) bookmark first, so a
        section note precedes the note of its chapter.
        """

        if not self.has_store():
            return []
        identity = self.document_identity(pdf_path)
        clause, args = self._document_clause(identity)
        if page_number is not None:
            clause += " AND l.page_start<=? AND l.page_end>=?"
            args += [int(page_number), int(page_number)]
        with closing(self.connect(rows=True)) as connection:
            link_rows = connection.execute(
                f"SELECT l.* FROM textbook_note_links l WHERE {clause} "
                "ORDER BY (l.page_end-l.page_start),l.bookmark_level DESC,l.page_start,l.id",
                args,
            ).fetchall()
            ordered_ids: list[int] = []
            matched: dict[int, list[dict[str, Any]]] = {}
            for row in link_rows:
                note_id = int(row["note_id"])
                if note_id not in matched:
                    ordered_ids.append(note_id)
                matched.setdefault(note_id, []).append(dict(row))
            if not ordered_ids:
                return []
            placeholders = ",".join("?" for _ in ordered_ids)
            rows = {
                int(row["id"]): row
                for row in connection.execute(
                    f"SELECT * FROM textbook_notes WHERE id IN ({placeholders})", ordered_ids
                ).fetchall()
            }
            question_links = self._question_links(connection, ordered_ids)
        return [
            {
                **self._note_dict(rows[note_id]),
                "links": matched[note_id],
                "question_links": question_links.get(note_id, []),
            }
            for note_id in ordered_ids if note_id in rows
        ]

    def read_note_tex(self, note_id: int) -> dict[str, Any]:
        note = self.note(note_id)
        if note is None:
            raise ValueError("教材笔记不存在。")
        return {**note, "content": Path(note["tex_file"]).read_text(encoding="utf-8")}

    # -- writing notes --------------------------------------------------------

    def _resolve_links(self, links: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Turn ``{pdf_path, bookmark_title | outline_index, book_title?}`` requests into link rows.

        ``book_title`` is the name shown in the note header (the reader passes
        the title it displays); without it the PDF title or file name is used.
        """

        resolved: list[dict[str, Any]] = []
        outlines: dict[str, list[dict[str, Any]]] = {}
        for request in links:
            pdf_path = str(request.get("pdf_path") or "").strip()
            if not pdf_path:
                raise ValueError("书签链接缺少 pdf_path。")
            identity = self.document_identity(pdf_path)
            outline = outlines.setdefault(identity["path"], self.pdf_outline(identity["path"]))
            entry: dict[str, Any] | None = None
            if request.get("outline_index") is not None:
                index = int(request["outline_index"])
                entry = outline[index] if 0 <= index < len(outline) else None
            else:
                wanted = re.sub(r"\s+", " ", str(request.get("bookmark_title") or "")).strip()
                if not wanted:
                    raise ValueError("书签链接需要 bookmark_title 或 outline_index。")
                normalized = lambda value: re.sub(r"\s+", " ", value).strip()
                candidates = [item for item in outline if normalized(item["path_title"]) == wanted]
                candidates = candidates or [item for item in outline if normalized(item["title"]) == wanted]
                if len(candidates) > 1:
                    raise ValueError(f"书签“{wanted}”不唯一，请用完整路径（以 › 分隔）指定。")
                entry = candidates[0] if candidates else None
            if entry is None:
                raise ValueError(f"《{identity['name']}》中找不到指定书签：{request.get('bookmark_title') or request.get('outline_index')}")
            resolved.append({
                "document_sha256": identity["sha256"],
                "document_name": identity["name"],
                "document_title": str(request.get("book_title") or "").strip() or identity["title"],
                "document_page_count": identity["page_count"],
                "bookmark_title": entry["path_title"],
                "bookmark_level": entry["level"],
                "page_start": entry["page_start"],
                "page_end": entry["page_end"],
            })
        return resolved

    @staticmethod
    def _resolve_question_links(links: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Validate ``{question_key, question_title?, book_title?, source_page?}`` requests."""

        resolved: list[dict[str, Any]] = []
        for request in links:
            key = str(request.get("question_key") or "").strip()
            if not QUESTION_KEY_PATTERN.fullmatch(key):
                raise ValueError(f"题目链接的 question_key 格式不对：{key or '（空）'}，应形如 english:grammar:XYGE-01-002。")
            page = request.get("source_page")
            resolved.append({
                "question_key": key,
                "question_title": re.sub(r"\s+", " ", str(request.get("question_title") or "")).strip(),
                "book_title": str(request.get("book_title") or "").strip(),
                "source_page": int(page) if page not in (None, "") else None,
            })
        return resolved

    @staticmethod
    def _insert_question_links(connection: sqlite3.Connection, note_id: int, links: list[dict[str, Any]]) -> None:
        for link in links:
            connection.execute(
                """
                INSERT OR IGNORE INTO textbook_note_question_links(
                    note_id,question_key,question_title,book_title,source_page
                ) VALUES(?,?,?,?,?)
                """,
                (note_id, link["question_key"], link["question_title"], link["book_title"], link["source_page"]),
            )

    @staticmethod
    def _insert_links(connection: sqlite3.Connection, note_id: int, links: list[dict[str, Any]]) -> None:
        for link in links:
            connection.execute(
                """
                INSERT OR IGNORE INTO textbook_note_links(
                    note_id,document_sha256,document_name,document_title,document_page_count,
                    bookmark_title,bookmark_level,page_start,page_end
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    note_id, link["document_sha256"], link["document_name"], link["document_title"],
                    link["document_page_count"],
                    link["bookmark_title"], link["bookmark_level"], link["page_start"], link["page_end"],
                ),
            )

    def create_note(
        self,
        title: str,
        latex: str,
        *,
        links: Iterable[Mapping[str, Any]] = (),
        question_links: Iterable[Mapping[str, Any]] = (),
        source_kind: str = "ai",
        compile_pdf: bool = True,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not clean_title:
            raise ValueError("笔记标题不能为空。")
        if source_kind not in {"ai", "manual"}:
            raise ValueError("笔记来源只能是 ai 或 manual。")
        document = clean_note_body(latex)
        link_rows = self._resolve_links(links)
        question_rows = self._resolve_question_links(question_links)
        backup = self.backup("note_create")
        with closing(self.connect(rows=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            number = int(connection.execute("SELECT COALESCE(MAX(id),0)+1 FROM textbook_notes").fetchone()[0])
            note_code = f"N{number:04d}"
            while connection.execute(
                "SELECT 1 FROM textbook_notes WHERE note_code=?", (note_code,)
            ).fetchone() or (self.notes_dir / note_code).exists():
                number += 1
                note_code = f"N{number:04d}"
            note_dir = self.notes_dir / note_code
            try:
                note_dir.mkdir(parents=True)
                tex_path = note_dir / "note.tex"
                temporary = note_dir / "note.tex.tmp"
                temporary.write_text(document, encoding="utf-8")
                os.replace(temporary, tex_path)
                cursor = connection.execute(
                    """
                    INSERT INTO textbook_notes(note_code,title,source_kind,tex_path,pdf_path,tex_sha256)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        note_code, clean_title, source_kind, f"notes/{note_code}/note.tex",
                        f"notes/{note_code}/note.pdf", _sha256(tex_path),
                    ),
                )
                note_id = int(cursor.lastrowid)
                self._insert_links(connection, note_id, link_rows)
                self._insert_question_links(connection, note_id, question_rows)
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                shutil.rmtree(note_dir, ignore_errors=True)
                raise
        note = self.note(note_id)
        if note is None or Path(note["tex_file"]).read_text(encoding="utf-8") != document or len(note["links"]) != len(
            {(row["document_sha256"], row["bookmark_title"], row["page_start"]) for row in link_rows}
        ) or {row["question_key"] for row in note["question_links"]} != {row["question_key"] for row in question_rows}:
            raise RuntimeError("笔记写入后回读失败。")
        result: dict[str, Any] = {**note, "backup_path": str(backup or ""), "readback_verified": True}
        if compile_pdf:
            result["compile"] = self._compile_reporting(note_id, emit)
        return result

    def link_note(
        self,
        note_id: int,
        links: Iterable[Mapping[str, Any]] = (),
        *,
        question_links: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        if self.note(note_id) is None:
            raise ValueError("教材笔记不存在。")
        link_rows = self._resolve_links(links)
        question_rows = self._resolve_question_links(question_links)
        if not link_rows and not question_rows:
            raise ValueError("请至少选择一个书签或一道题目。")
        backup = self.backup("note_link")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_links(connection, int(note_id), link_rows)
            self._insert_question_links(connection, int(note_id), question_rows)
            connection.execute("UPDATE textbook_notes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(note_id),))
            self._integrity(connection)
            connection.commit()
        note = self.note(note_id)
        assert note is not None
        stored = {(row["document_sha256"], row["bookmark_title"], row["page_start"]) for row in note["links"]}
        if not all((row["document_sha256"], row["bookmark_title"], row["page_start"]) in stored for row in link_rows):
            raise RuntimeError("书签链接写入后回读失败。")
        stored_questions = {row["question_key"] for row in note["question_links"]}
        if not all(row["question_key"] in stored_questions for row in question_rows):
            raise RuntimeError("题目链接写入后回读失败。")
        return {**note, "backup_path": str(backup or ""), "readback_verified": True}

    def unlink_question(self, link_id: int) -> dict[str, Any]:
        """Remove one question link; the note itself and its files are kept."""

        with closing(self.connect(rows=True)) as connection:
            row = connection.execute(
                "SELECT * FROM textbook_note_question_links WHERE id=?", (int(link_id),)
            ).fetchone()
        if row is None:
            raise ValueError("题目链接不存在。")
        backup = self.backup("note_unlink_question")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM textbook_note_question_links WHERE id=?", (int(link_id),))
            self._integrity(connection)
            connection.commit()
            remaining = connection.execute(
                "SELECT 1 FROM textbook_note_question_links WHERE id=?", (int(link_id),)
            ).fetchone()
        if remaining is not None:
            raise RuntimeError("题目链接删除后回读失败。")
        return {
            "link_id": int(link_id), "note_id": int(row["note_id"]), "question_key": str(row["question_key"]),
            "backup_path": str(backup or ""), "readback_verified": True,
        }

    def unlink_note(self, link_id: int) -> dict[str, Any]:
        """Remove one bookmark link; the note itself and its files are kept."""

        with closing(self.connect(rows=True)) as connection:
            row = connection.execute("SELECT * FROM textbook_note_links WHERE id=?", (int(link_id),)).fetchone()
        if row is None:
            raise ValueError("书签链接不存在。")
        backup = self.backup("note_unlink")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM textbook_note_links WHERE id=?", (int(link_id),))
            self._integrity(connection)
            connection.commit()
            remaining = connection.execute("SELECT 1 FROM textbook_note_links WHERE id=?", (int(link_id),)).fetchone()
        if remaining is not None:
            raise RuntimeError("书签链接删除后回读失败。")
        return {"link_id": int(link_id), "note_id": int(row["note_id"]), "backup_path": str(backup or ""), "readback_verified": True}

    def update_note_tex(
        self,
        note_id: int,
        latex: str,
        *,
        title: str = "",
        compile_pdf: bool = True,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Replace a note's TeX, keeping the previous text under versions/."""

        note = self.note(note_id)
        if note is None:
            raise ValueError("教材笔记不存在。")
        clean_title = re.sub(r"\s+", " ", str(title or "")).strip() or str(note["title"])
        document = clean_note_body(latex)
        tex_path = Path(note["tex_file"])
        def staged_text(content: str | bytes) -> Path:
            handle, name = tempfile.mkstemp(prefix=".note_", suffix=".tmp", dir=tex_path.parent)
            if isinstance(content, bytes):
                with os.fdopen(handle, "wb") as stream:
                    stream.write(content)
            else:
                with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
            return Path(name)

        with closing(self.connect(rows=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            temporary: Path | None = None
            replaced = False
            existed = tex_path.is_file()
            previous = tex_path.read_text(encoding="utf-8") if existed else ""
            # Restore the exact bytes: a Windows (CRLF) body rewritten as text would
            # no longer match the stored tex_sha256.
            previous_bytes = tex_path.read_bytes() if existed else b""
            try:
                current = connection.execute("SELECT * FROM textbook_notes WHERE id=?", (int(note_id),)).fetchone()
                if current is None:
                    raise ValueError("教材笔记不存在。")
                if previous == document and clean_title == current["title"] and _sha256(tex_path) == current["tex_sha256"]:
                    return {**note, "changed": False, "readback_verified": True}
                if previous:
                    versions = tex_path.parent / "versions"
                    versions.mkdir(exist_ok=True)
                    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
                    (versions / f"note_{stamp}.tex").write_text(previous, encoding="utf-8")
                temporary = staged_text(document)
                os.replace(temporary, tex_path)
                replaced = True
                connection.execute(
                    "UPDATE textbook_notes SET title=?,tex_sha256=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (clean_title, _sha256(tex_path), int(note_id)),
                )
                connection.commit()
            except Exception:
                # Keep the write lock until the old body is restored, so another
                # editor cannot commit between our failure and recovery.
                try:
                    if replaced:
                        if existed:
                            temporary = staged_text(previous_bytes)
                            os.replace(temporary, tex_path)
                        else:
                            tex_path.unlink(missing_ok=True)
                finally:
                    connection.rollback()
                raise
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        updated = self.note(note_id)
        if updated is None or tex_path.read_text(encoding="utf-8") != document:
            raise RuntimeError("笔记更新后回读失败。")
        result: dict[str, Any] = {**updated, "changed": True, "readback_verified": True}
        if compile_pdf:
            result["compile"] = self._compile_reporting(note_id, emit)
        return result

    def _compile_reporting(self, note_id: int, emit: Callable[[str], None] | None) -> dict[str, Any]:
        try:
            return self.compile_note(note_id, emit=emit)
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def _header_meta(self, note: Mapping[str, Any]) -> dict[str, Any]:
        links = list(note.get("links") or [])
        question_links = list(note.get("question_links") or [])
        if not links and question_links:
            book = question_links[0]["book_title"]
            same_book = [link for link in question_links if link["book_title"] == book]
            pages = sorted({int(link["source_page"]) for link in same_book if link.get("source_page")})
            page_text = ""
            if pages:
                page_text = f"{pages[0]}--{pages[-1]}" if pages[-1] > pages[0] else str(pages[0])
            return {
                "title": note["title"],
                "book": book,
                "bookmark": "；".join(dict.fromkeys(
                    link["question_title"] or link["question_key"] for link in same_book
                )),
                "pages": page_text,
                "code": note["note_code"],
                "date": str(note.get("created_at") or "")[:10],
            }
        first = links[0] if links else {}
        same_book = [link for link in links if link["document_sha256"] == first.get("document_sha256")]
        pages = ""
        if same_book:
            start = min(int(link["page_start"]) for link in same_book)
            end = max(int(link["page_end"]) for link in same_book)
            pages = f"{start}--{end}" if end > start else str(start)
        return {
            "title": note["title"],
            "book": first.get("document_title") or first.get("document_name") or "",
            "bookmark": "；".join(dict.fromkeys(link["bookmark_title"] for link in same_book)),
            "pages": pages,
            "code": note["note_code"],
            "date": str(note.get("created_at") or "")[:10],
        }

    def _build_note_pdf(
        self,
        body_path: Path,
        meta: Mapping[str, Any],
        target_pdf: Path,
        *,
        emit: Callable[[str], None] | None = None,
    ) -> int:
        """Compile body + fixed wrapper in ``<target>/build`` and atomically replace the PDF."""

        from shared.scripts.latex_build_pipeline import run_latexmk, run_unified_latex_build

        work_dir = target_pdf.parent
        build_dir = work_dir / "build"
        shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir(parents=True)
        shutil.copy2(body_path, build_dir / "body.tex")
        shutil.copy2(NOTE_STYLE_PATH, build_dir / NOTE_STYLE_PATH.name)
        (build_dir / "main.tex").write_text(note_document_tex(meta), encoding="utf-8")
        log_path = work_dir / "compile.log"
        try:
            built = run_unified_latex_build(
                workdir=build_dir,
                main_tex="main.tex",
                emit=emit,
                log_path=log_path,
                source_files=[build_dir / "body.tex"],
                compile_once=lambda: run_latexmk(build_dir, main_tex="main.tex", emit=emit, timeout=600),
            )
            import fitz

            with fitz.open(str(built.pdf_path)) as document:
                page_count = int(document.page_count)
            if page_count < 1:
                raise RuntimeError("编译生成的 PDF 没有页面。")
            temporary = target_pdf.with_name(target_pdf.name + ".tmp")
            shutil.copy2(built.pdf_path, temporary)
            os.replace(temporary, target_pdf)
        except Exception as error:
            raw = str(error).strip() or error.__class__.__name__
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else raw
            # Keep the LaTeX error lines themselves instead of the whole log tail.
            error_lines = [
                line.strip() for line in log_text.splitlines()
                if line.startswith("!") or re.search(r"\.tex:\d+: ", line)
            ]
            message = raw.splitlines()[0]
            if error_lines:
                message += "\n" + "\n".join(dict.fromkeys(error_lines[:6]))
            raise RuntimeError(message) from error
        shutil.rmtree(build_dir, ignore_errors=True)
        return page_count

    def compile_note(self, note_id: int, *, emit: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Build note.pdf with the fixed layout; the old PDF is replaced only after success."""

        note = self.note(note_id)
        if note is None:
            raise ValueError("教材笔记不存在。")
        tex_path = Path(note["tex_file"])
        if not tex_path.is_file():
            raise ValueError(f"笔记 TeX 文件不存在：{tex_path}")
        tex_hash = _sha256(tex_path)
        try:
            page_count = self._build_note_pdf(tex_path, self._header_meta(note), Path(note["pdf_file"]), emit=emit)
        except Exception as error:
            message = str(error)
            with closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE textbook_notes SET compile_status='failed',compile_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (message[:4000], int(note_id)),
                )
                connection.commit()
            raise RuntimeError(f"笔记编译失败，已保留原有 PDF：{message}") from error
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE textbook_notes
                SET compile_status='compiled',compile_error='',compiled_tex_sha256=?,
                    compiled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (tex_hash, int(note_id)),
            )
            connection.commit()
        updated = self.note(note_id)
        if updated is None or not updated["pdf_current"]:
            raise RuntimeError("笔记 PDF 编译后回读失败。")
        return {
            "ok": True,
            "note_id": int(note_id),
            "pdf_file": updated["pdf_file"],
            "page_count": page_count,
            "log_path": str(Path(note["pdf_file"]).parent / "compile.log"),
            "readback_verified": True,
        }

    # -- the shipped example -------------------------------------------------

    def writing_guide(
        self, *, book: str = "", bookmarks: Iterable[str] = (), pages: str = "", ideas: str = "",
    ) -> dict[str, Any]:
        return {
            "prompt": writing_prompt(book=book, bookmarks=bookmarks, pages=pages, ideas=ideas),
            "example_body": NOTE_EXAMPLE_PATH.read_text(encoding="utf-8"),
            "example_meta": dict(NOTE_EXAMPLE_META),
            "style_path": str(NOTE_STYLE_PATH),
        }

    # -- ChatGPT Work tasks -----------------------------------------------------

    def _note_task_dir(self, kind: str, key: str) -> Path:
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]
        return self.root / "chatgpt_work" / kind / digest

    def note_task(self, kind: str, key: str) -> dict[str, Any]:
        """Describe the task folder for one book (kind="textbook") or question
        (kind="question") and the draft ChatGPT Work wrote into it."""

        task_dir = self._note_task_dir(kind, key)
        task_file = task_dir / "TASK.md"
        output_file = task_dir / "OUTPUT.tex"
        draft = output_file.read_text(encoding="utf-8") if output_file.is_file() else ""
        return {
            "task_dir": str(task_dir),
            "task_file": str(task_file),
            "output_file": str(output_file),
            "task_ready": task_file.is_file(),
            "draft_exists": bool(draft.strip()),
            "draft_source": draft,
            "task_line": f"请阅读 {task_file} 并按其中要求完成，结果只写入同一文件夹的 OUTPUT.tex。",
        }

    def _write_note_task(self, kind: str, key: str, prompt: str, attachments: list[str]) -> dict[str, Any]:
        task_dir = self._note_task_dir(kind, key)
        listed = "\n".join(f"- `{name}`" for name in ("TASK.md", *attachments))
        text = (
            "# ChatGPT Work 笔记任务\n\n"
            "本文件中的路径都相对于本文件所在的文件夹。\n\n"
            + (
                "下面提示里说“附上这几页原文”，原文就是本文件夹的 `SOURCE_PAGES.pdf`，先完整阅读。"
                if "SOURCE_PAGES.pdf" in attachments
                else ""
            )
            + "按下面的提示写笔记，把笔记正文的 LaTeX 写入本文件夹的 `OUTPUT.tex`；"
            "提示里说的“输出”“代码块”都指 `OUTPUT.tex` 的内容。"
            "不要创建、修改或删除其他文件；程序会自己检查、保存并编译。\n\n"
            "## 文件清单\n\n" + listed + "\n\n## 写作提示\n\n" + prompt.strip() + "\n"
        )
        task_dir.mkdir(parents=True, exist_ok=True)
        task_file = task_dir / "TASK.md"
        temporary = task_file.with_name("TASK.md.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, task_file)
        if task_file.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"任务文件写后回读失败：{task_file}")
        return self.note_task(kind, key)

    def prepare_textbook_note_task(
        self,
        pdf_path: Path | str,
        *,
        book: str = "",
        bookmarks: Iterable[str] = (),
        page_start: int = 0,
        page_end: int = 0,
        ideas: str = "",
    ) -> dict[str, Any]:
        """Write the book's note task: the writing prompt plus the chosen pages as a PDF.

        One book has one current task; a new task replaces the previous one."""

        import fitz

        identity = self.document_identity(pdf_path)
        key = identity["sha256"]
        pages = ""
        attachments: list[str] = []
        task_dir = self._note_task_dir("textbook", key)
        task_dir.mkdir(parents=True, exist_ok=True)
        if page_start > 0:
            last = max(int(page_start), int(page_end or page_start))
            pages = f"{int(page_start)}–{last}" if last > int(page_start) else str(int(page_start))
            target = task_dir / "SOURCE_PAGES.pdf"
            temporary = task_dir / "SOURCE_PAGES.tmp.pdf"
            with fitz.open(str(identity["path"])) as source, fitz.open() as excerpt:
                excerpt.insert_pdf(
                    source,
                    from_page=int(page_start) - 1,
                    to_page=min(last, int(source.page_count)) - 1,
                )
                excerpt.save(str(temporary))
            os.replace(temporary, target)
            attachments.append("SOURCE_PAGES.pdf")
        prompt = writing_prompt(book=book, bookmarks=bookmarks, pages=pages, ideas=ideas)
        return {**self._write_note_task("textbook", key, prompt, attachments), "task_key": key}

    def textbook_note_task(self, pdf_path: Path | str) -> dict[str, Any]:
        key = self.document_identity(pdf_path)["sha256"]
        return {**self.note_task("textbook", key), "task_key": key}

    def prepare_question_note_task(
        self, question_key: str, *, book: str = "", question: str = "", ideas: str = "",
    ) -> dict[str, Any]:
        prompt = question_writing_prompt(book=book, question=question, ideas=ideas)
        return self._write_note_task("question", question_key, prompt, [])

    def archive_note_draft(self, kind: str, key: str) -> Path | None:
        """Move the draft aside once a note was created from it."""

        output_file = Path(self.note_task(kind, key)["output_file"])
        if not output_file.is_file():
            return None
        archived = output_file.parent / "imported_drafts" / f"OUTPUT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.tex"
        archived.parent.mkdir(parents=True, exist_ok=True)
        os.replace(output_file, archived)
        return archived

    def example_pdf(self, *, emit: Callable[[str], None] | None = None) -> Path:
        """The example note compiled with the current layout (a rebuildable cache)."""

        return self._cached_example(NOTE_EXAMPLE_PATH, NOTE_EXAMPLE_META, "example", emit=emit)

    def question_writing_guide(self, *, book: str = "", question: str = "", ideas: str = "") -> dict[str, Any]:
        return {
            "prompt": question_writing_prompt(book=book, question=question, ideas=ideas),
            "example_body": QUESTION_NOTE_EXAMPLE_PATH.read_text(encoding="utf-8"),
            "example_meta": dict(QUESTION_NOTE_EXAMPLE_META),
            "style_path": str(NOTE_STYLE_PATH),
        }

    def question_example_pdf(self, *, emit: Callable[[str], None] | None = None) -> Path:
        """The question-note example compiled with the current layout (a rebuildable cache)."""

        return self._cached_example(
            QUESTION_NOTE_EXAMPLE_PATH, QUESTION_NOTE_EXAMPLE_META, "question_example", emit=emit,
        )

    def _cached_example(
        self, body: Path, meta: Mapping[str, Any], name: str, *, emit: Callable[[str], None] | None = None,
    ) -> Path:
        target = self.root / "example" / f"{name}.pdf"
        stamp_path = target.with_name(f"{name}.sha256")
        digest = hashlib.sha256(
            body.read_bytes() + NOTE_STYLE_PATH.read_bytes()
            + note_document_tex({**meta, "example": True}).encode("utf-8")
        ).hexdigest()
        if target.is_file() and stamp_path.is_file() and stamp_path.read_text(encoding="utf-8").strip() == digest:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        self._build_note_pdf(body, {**meta, "example": True}, target, emit=emit)
        stamp_path.write_text(digest, encoding="utf-8")
        return target

__all__ = [
    "BOOKMARK_SEPARATOR",
    "QUESTION_NOTE_EXAMPLE_META",
    "question_writing_prompt",
    "DEFAULT_NOTES_ROOT",
    "TextbookNotesService",
    "outline_with_ranges",
    "clean_note_body",
    "note_document_tex",
    "writing_prompt",
]
