"""Reading help: "I don't understand" marks in the PDF reader and their stored explanations.

The user marks a sentence or paragraph in the project's PDF reader.  ChatGPT
Work (or Claude) later explains the marked text through ``chatgpt_work reading``
commands, following ``shared/templates/reading_help_explanation_rules.txt``;
the program never calls a model.  Explanations are kept locally so they are
written once and shown in the reader from then on.

Data lives in ``ReadingHelp/reading_help.db`` (subject data, synced by
Syncthing).  A document is identified by its path relative to the project root,
so a recompiled lecture PDF keeps its marks; marks are re-anchored by their
text when the PDF content changes.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from shared.scripts import reading_help_text as rtext
from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = Path(__file__).resolve().parents[2]

DEFAULT_READING_HELP_ROOT = APP_PATHS.workspace_root / "ReadingHelp"
RULES_PATH = ROOT_DIR / "shared" / "templates" / "reading_help_explanation_rules.txt"

SCOPES = ("selection", "sentence", "paragraph")
STATUSES = ("pending", "explained", "reexplain")
LOCATIONS = ("exact", "relocated", "lost")
MAX_MARK_CHARS = 6000
_CJK_RE = re.compile(r"[一-鿿]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reading_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    rel_path TEXT NOT NULL DEFAULT '',
    last_pdf_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reading_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('selection','sentence','paragraph')),
    text TEXT NOT NULL,
    text_key TEXT NOT NULL,
    context_before TEXT NOT NULL DEFAULT '',
    context_after TEXT NOT NULL DEFAULT '',
    page_number INTEGER NOT NULL DEFAULT 1,
    boxes_json TEXT NOT NULL DEFAULT '[]',
    pdf_hash TEXT NOT NULL DEFAULT '',
    located TEXT NOT NULL DEFAULT 'exact' CHECK(located IN ('exact','relocated','lost')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','explained','reexplain')),
    question TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(document_id) REFERENCES reading_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reading_marks_document ON reading_marks(document_id, status, page_number);
CREATE TABLE IF NOT EXISTS reading_explanations (
    mark_id INTEGER PRIMARY KEY,
    gist TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    sentences_json TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(mark_id) REFERENCES reading_marks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reading_explanation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mark_id INTEGER NOT NULL,
    gist TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    sentences_json TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL DEFAULT '',
    replaced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reading_positions (
    doc_key TEXT PRIMARY KEY,
    page_index INTEGER NOT NULL DEFAULT 0,
    anchor_y REAL NOT NULL DEFAULT 0,
    zoom REAL NOT NULL DEFAULT 0,
    anchor_text TEXT NOT NULL DEFAULT '',
    pdf_hash TEXT NOT NULL DEFAULT '',
    page_offset INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fitz() -> Any:
    try:
        import pymupdf as module  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - older PyMuPDF
        import fitz as module  # type: ignore[import-not-found,no-redef]
    return module


class PdfGeometrySource:
    """Lazily built glyph geometry for every page of one PDF file."""

    def __init__(self, pdf_path: Path, *, ocr_cache: Any = None, pdf_sha256: str = "") -> None:
        from shared.scripts.pdf_word_geometry import build_page_geometry

        self._build = build_page_geometry
        self._document = _fitz().open(str(pdf_path))
        self._cache: dict[int, Any] = {}
        # Scanned pages use the reader's cached OCR lines (never a new OCR run here).
        self._ocr_cache = ocr_cache
        self._pdf_sha256 = pdf_sha256

    @property
    def page_count(self) -> int:
        return int(self._document.page_count)

    def geometry(self, page_index: int) -> Any:
        if not (0 <= page_index < self.page_count):
            return None
        if page_index not in self._cache:
            page = self._document.load_page(page_index)
            raw = page.get_text("rawdict") or {}
            if self._ocr_cache is not None and self._pdf_sha256:
                from shared.scripts.pdf_ocr_layer import ocr_rawdict, page_is_scanned

                if page_is_scanned(page):
                    lines = self._ocr_cache.load(self._pdf_sha256, page_index)
                    if lines:
                        raw = ocr_rawdict(lines)
            self._cache[page_index] = self._build(page_index, raw)
        return self._cache[page_index]

    def close(self) -> None:
        self._document.close()

    def __enter__(self) -> "PdfGeometrySource":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ReadingHelpService:
    def __init__(self, root: Path | str = DEFAULT_READING_HELP_ROOT, *, project_root: Path | str = ROOT_DIR) -> None:
        self.root = Path(root)
        self.project_root = Path(project_root).resolve()
        self.database_path = self.root / "reading_help.db"
        self.backup_dir = self.root / "backups"
        self.task_root = self.root / "tasks"
        self._hash_cache: dict[str, tuple[float, int, str]] = {}
        self._schema_ready = False
        from shared.scripts.pdf_ocr_layer import OcrCache

        self.ocr_cache = OcrCache(self.root / "ocr_cache")

    def geometry_source(self, pdf_path: Path) -> PdfGeometrySource:
        """Glyph geometry of a PDF, with cached OCR lines standing in on scanned pages."""

        return PdfGeometrySource(Path(pdf_path), ocr_cache=self.ocr_cache, pdf_sha256=self.pdf_hash(Path(pdf_path)))

    # -- storage ---------------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.executescript(SCHEMA)
            # Added columns (older databases lack them; older code ignores them).
            columns = {row[1] for row in connection.execute("PRAGMA table_info(reading_positions)")}
            for name in ("pdf_path", "title"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE reading_positions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            connection.commit()
        self._schema_ready = True

    def backup(self, reason: str) -> Path | None:
        if not self.database_path.is_file():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_") or "change"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        target = self.backup_dir / f"reading_help_before_{safe}_{stamp}.db"
        with closing(sqlite3.connect(self.database_path, timeout=30)) as source:
            if str(source.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("讲解数据库完整性检查失败，已停止写入。")
            with closing(sqlite3.connect(target, timeout=30)) as destination:
                source.backup(destination)
        shutil.copy2(target, self.backup_dir / "reading_help_latest.db")
        return target

    # -- documents -------------------------------------------------------------

    def pdf_hash(self, pdf_path: Path) -> str:
        path = Path(pdf_path).resolve()
        stat = path.stat()
        cached = self._hash_cache.get(str(path))
        if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        value = file_sha256(path)
        self._hash_cache[str(path)] = (stat.st_mtime, stat.st_size, value)
        return value

    def document_key(self, pdf_path: Path) -> tuple[str, str]:
        """Stable key: the project-relative path, or file name plus content hash outside it."""

        path = Path(pdf_path).resolve()
        try:
            relative = path.relative_to(self.project_root).as_posix()
        except ValueError:
            return f"file:{path.name}:{self.pdf_hash(path)}", ""
        return f"path:{relative}", relative

    def resolve_document_path(self, document: sqlite3.Row | dict[str, Any]) -> Path | None:
        relative = str(document["rel_path"] or "")
        if relative:
            path = self.project_root / relative
            return path if path.is_file() else None
        return None

    def _document_row(self, connection: sqlite3.Connection, pdf_path: Path, title: str = "", *, create: bool) -> sqlite3.Row | None:
        key, relative = self.document_key(pdf_path)
        row = connection.execute("SELECT * FROM reading_documents WHERE doc_key = ?", (key,)).fetchone()
        if row is None and create:
            connection.execute(
                "INSERT INTO reading_documents(doc_key, title, rel_path) VALUES (?, ?, ?)",
                (key, title.strip() or Path(pdf_path).name, relative),
            )
            row = connection.execute("SELECT * FROM reading_documents WHERE doc_key = ?", (key,)).fetchone()
        elif row is not None and title.strip() and title.strip() != row["title"]:
            connection.execute(
                "UPDATE reading_documents SET title = ?, updated_at = ? WHERE id = ?",
                (title.strip(), _now(), row["id"]),
            )
        return row

    def list_documents(self, *, pending_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*,
                       SUM(CASE WHEN m.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN m.status = 'reexplain' THEN 1 ELSE 0 END) AS reexplain,
                       SUM(CASE WHEN m.status = 'explained' THEN 1 ELSE 0 END) AS explained,
                       SUM(CASE WHEN m.located = 'lost' THEN 1 ELSE 0 END) AS lost,
                       MAX(m.updated_at) AS last_mark_at
                FROM reading_documents d LEFT JOIN reading_marks m ON m.document_id = d.id
                GROUP BY d.id ORDER BY last_mark_at DESC, d.id DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            for key in ("pending", "reexplain", "explained", "lost"):
                item[key] = int(item[key] or 0)
            if pending_only and not (item["pending"] or item["reexplain"]):
                continue
            path = self.resolve_document_path(row)
            item["pdf_path"] = str(path) if path else ""
            result.append(item)
        return result

    # -- reading position (where the reader was left; the printed-page offset) ----

    def reading_position(self, pdf_path: Path) -> dict[str, Any] | None:
        """Where this PDF was last read; re-found by its text when the PDF changed since.

        Returns ``{"page_index", "anchor_y", "zoom", "page_offset", "relocated"}``
        or None.  A position is a reading convenience, not formal data, so it is
        written without a backup.
        """

        key, _relative = self.document_key(pdf_path)
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM reading_positions WHERE doc_key = ?", (key,)).fetchone()
        if row is None:
            return None
        result = {
            "page_index": int(row["page_index"]),
            "anchor_y": float(row["anchor_y"]),
            "zoom": float(row["zoom"]),
            "page_offset": None if row["page_offset"] is None else int(row["page_offset"]),
            "relocated": False,
        }
        anchor_text = str(row["anchor_text"] or "")
        if anchor_text and row["pdf_hash"] and row["pdf_hash"] != self.pdf_hash(pdf_path):
            try:
                with self.geometry_source(Path(pdf_path)) as source:
                    spans = rtext.locate_text(source.page_count, source.geometry, anchor_text, result["page_index"])
                    if spans:
                        geometry = source.geometry(spans[0].page_index)
                        boxes = rtext.line_boxes(geometry, spans[0].char_ids)
                        result["page_index"] = spans[0].page_index
                        if boxes:
                            result["anchor_y"] = max(0.0, float(boxes[0][1]) - 12.0)
                        result["relocated"] = True
            except Exception:
                pass
        return result

    def save_reading_position(
        self,
        pdf_path: Path,
        page_index: int,
        anchor_y: float,
        *,
        zoom: float = 0.0,
        anchor_text: str = "",
        title: str = "",
    ) -> None:
        key, relative = self.document_key(pdf_path)
        # Inside the project the relative path works on both computers; outside it, this machine's path.
        stored_path = relative or str(Path(pdf_path).resolve())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO reading_positions(doc_key, page_index, anchor_y, zoom, anchor_text, pdf_hash, updated_at, pdf_path, title)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_key) DO UPDATE SET page_index = excluded.page_index, anchor_y = excluded.anchor_y,
                    zoom = excluded.zoom, anchor_text = excluded.anchor_text, pdf_hash = excluded.pdf_hash,
                    updated_at = excluded.updated_at, pdf_path = excluded.pdf_path,
                    title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE reading_positions.title END
                """,
                (key, max(0, int(page_index)), max(0.0, float(anchor_y)), float(zoom), rtext.clean_text(anchor_text)[:160],
                 self.pdf_hash(pdf_path), _now(), stored_path, title.strip()[:200]),
            )
            connection.commit()

    def recent_documents(self, limit: int = 12) -> list[dict[str, Any]]:
        """PDFs read most recently that exist on this computer, newest first."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reading_positions WHERE pdf_path <> '' ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)) * 3,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            stored = Path(str(row["pdf_path"]))
            path = stored if stored.is_absolute() else self.project_root / stored
            if not path.is_file():
                continue
            result.append({
                "pdf_path": str(path), "title": str(row["title"] or "") or path.stem,
                "page_index": int(row["page_index"]), "anchor_y": float(row["anchor_y"]), "updated_at": str(row["updated_at"]),
            })
            if len(result) >= limit:
                break
        return result

    def set_page_offset(self, pdf_path: Path, offset: int | None) -> None:
        """Printed page 1 is PDF page ``offset + 1`` (for books without PDF page labels)."""

        key, _relative = self.document_key(pdf_path)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO reading_positions(doc_key, page_offset, pdf_hash, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(doc_key) DO UPDATE SET page_offset = excluded.page_offset, updated_at = excluded.updated_at
                """,
                (key, None if offset is None else int(offset), self.pdf_hash(pdf_path), _now()),
            )
            connection.commit()

    # -- marks -------------------------------------------------------------------

    @staticmethod
    def _mark_dict(row: sqlite3.Row, explanation: sqlite3.Row | None = None) -> dict[str, Any]:
        item = {key: row[key] for key in row.keys()}
        item["boxes"] = json.loads(item.pop("boxes_json") or "[]")
        if explanation is not None:
            item["explanation"] = {
                "gist": explanation["gist"],
                "answer": explanation["answer"],
                "sentences": json.loads(explanation["sentences_json"] or "[]"),
                "task_id": explanation["task_id"],
                "updated_at": explanation["updated_at"],
            }
        else:
            item["explanation"] = None
        return item

    def add_mark(
        self,
        pdf_path: Path,
        *,
        text: str,
        page_number: int,
        boxes: Sequence[Sequence[float]],
        scope: str = "selection",
        context_before: str = "",
        context_after: str = "",
        question: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """Store one "I don't understand" mark; boxes are ``[page_index, x0, y0, x1, y1]``."""

        cleaned = rtext.clean_text(text)
        key = rtext.match_key(cleaned)
        if scope not in SCOPES:
            raise ValueError(f"未知的标记范围：{scope}")
        if not key:
            raise ValueError("选中的内容里没有可标记的文字。")
        if len(cleaned) > MAX_MARK_CHARS:
            raise ValueError(f"一次最多标记 {MAX_MARK_CHARS} 个字符；请分段标记。")
        normalized_boxes = [[int(box[0]), *(round(float(value), 2) for value in box[1:5])] for box in boxes]
        pdf_hash = self.pdf_hash(pdf_path)
        with self.connect() as connection:
            with connection:
                mark_id, duplicate = self._store_mark(connection, pdf_path, {
                    "text": cleaned, "scope": scope, "context_before": context_before,
                    "context_after": context_after, "page_number": page_number,
                    "boxes": normalized_boxes, "question": question,
                }, title, pdf_hash)
        mark = self.mark(mark_id)
        if mark is None or mark["text_key"] != key:
            raise RuntimeError("标记写入后回读不一致。")
        mark["duplicate"] = duplicate
        return mark

    def _store_mark(
        self, connection: sqlite3.Connection, pdf_path: Path, item: dict[str, Any], title: str, pdf_hash: str,
        *, refresh_text: bool = False,
    ) -> tuple[int, bool]:
        """Shared transactional insertion for the reader and the batch operation."""
        document = self._document_row(connection, pdf_path, title, create=True)
        assert document is not None
        key = rtext.match_key(item["text"])
        duplicate = connection.execute(
            "SELECT id FROM reading_marks WHERE document_id = ? AND text_key = ?", (document["id"], key),
        ).fetchone()
        if duplicate is not None:
            mark_id = int(duplicate["id"])
            if refresh_text:
                connection.execute("UPDATE reading_marks SET text = ?, updated_at = ? WHERE id = ?",
                                   (rtext.clean_text(item["text"]), _now(), mark_id))
        else:
            cursor = connection.execute(
                """INSERT INTO reading_marks(document_id, scope, text, text_key, context_before, context_after,
                                             page_number, boxes_json, pdf_hash, question)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document["id"], item["scope"], rtext.clean_text(item["text"]), key,
                 rtext.clean_text(item.get("context_before", ""))[-800:],
                 rtext.clean_text(item.get("context_after", ""))[:800], max(1, int(item["page_number"])),
                 json.dumps(item["boxes"]), pdf_hash, str(item.get("question", "")).strip()),
            )
            mark_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE reading_documents SET last_pdf_hash = ?, updated_at = ? WHERE id = ?",
            (pdf_hash, _now(), document["id"]),
        )
        return mark_id, duplicate is not None

    def preview_marks_import(
        self, pdf_path: Path | str, marks: Sequence[dict[str, Any]], *, expected_pdf_sha256: str, title: str = "",
    ) -> dict[str, Any]:
        """Locate every original passage before writing anything; callers supply no rectangles."""
        pdf = Path(pdf_path).resolve()
        if not 1 <= len(marks) <= 300:
            raise ValueError("一次需要提供1至300条原句标记。")
        digest = file_sha256(pdf)
        if digest != expected_pdf_sha256:
            raise ValueError("PDF已变化；请重新读取原文并预览。")
        prepared = []
        with self.geometry_source(pdf) as source:
            for index, item in enumerate(marks, start=1):
                text = rtext.clean_text(item.get("text", ""))
                scope = str(item.get("scope", "sentence"))
                if not 16 <= len(rtext.match_key(text)) or len(text) > MAX_MARK_CHARS or scope not in SCOPES:
                    raise ValueError(f"第{index}条原句为空、太短、过长或范围无效。")
                page = int(item.get("page_number", 1))
                if not 1 <= page <= source.page_count:
                    raise ValueError(f"第{index}条页码超出PDF范围。")
                segments = item.get("segments") or [{"text": text, "page_number": page}]
                if rtext.match_key(" ".join(str(segment["text"]) for segment in segments)) != rtext.match_key(text):
                    raise ValueError(f"第{index}条跨页片段与完整原句不一致。")
                spans = []
                for segment in segments:
                    found = rtext.locate_text(source.page_count, source.geometry, segment["text"], int(segment.get("page_number", page)) - 1)
                    if not found:
                        spans = []
                        break
                    if spans and (found[0].page_index, found[0].char_ids[0]) <= (spans[-1].page_index, spans[-1].char_ids[-1]):
                        raise ValueError(f"第{index}条跨页片段顺序不正确。")
                    spans.extend(found)
                if not spans:
                    raise ValueError(f"第{index}条原句无法在PDF中定位：{text[:100]}")
                before, _ = rtext.context_around(source.geometry(spans[0].page_index), spans[0].char_ids)
                _, after = rtext.context_around(source.geometry(spans[-1].page_index), spans[-1].char_ids)
                boxes = [[span.page_index, *(round(float(v), 2) for v in box)]
                         for span in spans for box in rtext.line_boxes(source.geometry(span.page_index), span.char_ids)]
                prepared.append({
                    "key": str(item.get("key", index)), "text": text, "scope": scope,
                    "page_number": spans[0].page_index + 1, "boxes": boxes,
                    "context_before": before, "context_after": after,
                    "question": str(item.get("question", "")),
                })
        if file_sha256(pdf) != digest:
            raise ValueError("定位期间PDF发生变化；未保存标记。")
        return {"pdf_path": str(pdf), "pdf_sha256": digest, "title": title, "marks": prepared, "count": len(prepared)}

    def import_marks(
        self, pdf_path: Path | str, marks: Sequence[dict[str, Any]], *, expected_pdf_sha256: str, title: str = "",
        refresh_existing_text: bool = False,
    ) -> dict[str, Any]:
        preview = self.preview_marks_import(pdf_path, marks, expected_pdf_sha256=expected_pdf_sha256, title=title)
        existing: dict[str, int] = {}
        if self.database_path.is_file():
            with self.connect() as connection:
                document = self._document_row(connection, Path(preview["pdf_path"]), create=False)
                if document is not None:
                    existing = {row["text_key"]: int(row["id"]) for row in connection.execute(
                        "SELECT id, text_key FROM reading_marks WHERE document_id = ?", (document["id"],),
                    )}
        if not refresh_existing_text and all(rtext.match_key(item["text"]) in existing for item in preview["marks"]):
            return {"pdf_sha256": preview["pdf_sha256"], "marks": [
                {"key": item["key"], "mark_id": existing[rtext.match_key(item["text"])], "duplicate": True}
                for item in preview["marks"]
            ], "added": 0, "reused": len(preview["marks"]), "backup": "", "readback_verified": True}
        backup = self.backup("marks_import")
        stored = []
        with self.connect() as connection:
            with connection:
                for item in preview["marks"]:
                    mark_id, duplicate = self._store_mark(connection, Path(preview["pdf_path"]), item, title, preview["pdf_sha256"],
                                                         refresh_text=refresh_existing_text)
                    stored.append({"key": item["key"], "mark_id": mark_id, "duplicate": duplicate})
                if file_sha256(Path(preview["pdf_path"])) != preview["pdf_sha256"]:
                    raise ValueError("保存期间PDF发生变化；本批标记已回滚。")
        for item, result in zip(preview["marks"], stored):
            mark = self.mark(result["mark_id"])
            if mark is None or mark["text_key"] != rtext.match_key(item["text"]):
                raise RuntimeError(f"批量标记回读失败；备份：{backup}")
            if refresh_existing_text and mark["text"] != rtext.clean_text(item["text"]):
                raise RuntimeError(f"原句文字刷新回读失败；备份：{backup}")
        return {"pdf_sha256": preview["pdf_sha256"], "marks": stored,
                "added": sum(not item["duplicate"] for item in stored),
                "reused": sum(item["duplicate"] for item in stored),
                "backup": str(backup) if backup else "", "readback_verified": True}

    def mark(self, mark_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM reading_marks WHERE id = ?", (int(mark_id),)).fetchone()
            if row is None:
                return None
            explanation = connection.execute(
                "SELECT * FROM reading_explanations WHERE mark_id = ?", (int(mark_id),)
            ).fetchone()
        return self._mark_dict(row, explanation)

    def marks_for_document(self, document_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reading_marks WHERE document_id = ? ORDER BY page_number, id", (int(document_id),)
            ).fetchall()
            explanations = {
                int(row["mark_id"]): row
                for row in connection.execute(
                    "SELECT e.* FROM reading_explanations e JOIN reading_marks m ON m.id = e.mark_id WHERE m.document_id = ?",
                    (int(document_id),),
                ).fetchall()
            }
        return [self._mark_dict(row, explanations.get(int(row["id"]))) for row in rows]

    def document_for_pdf(self, pdf_path: Path) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = self._document_row(connection, pdf_path, create=False)
        return {key: row[key] for key in row.keys()} if row is not None else None

    def marks_for_pdf(self, pdf_path: Path, *, relocate: bool = True) -> list[dict[str, Any]]:
        """Marks of one PDF; re-anchors them by text first when the PDF content changed."""

        document = self.document_for_pdf(pdf_path)
        if document is None:
            return []
        if relocate:
            self.relocate_marks(pdf_path)
        return self.marks_for_document(int(document["id"]))

    def relocate_marks(self, pdf_path: Path, geometry_source: Any = None) -> dict[str, int]:
        """Find marks again by text in a changed PDF and update their boxes."""

        document = self.document_for_pdf(pdf_path)
        counts = {"checked": 0, "relocated": 0, "lost": 0}
        if document is None:
            return counts
        current_hash = self.pdf_hash(pdf_path)
        with self.connect() as connection:
            stale = connection.execute(
                "SELECT * FROM reading_marks WHERE document_id = ? AND pdf_hash != ?",
                (document["id"], current_hash),
            ).fetchall()
        if not stale:
            return counts
        owned = geometry_source is None
        source = geometry_source or self.geometry_source(pdf_path)
        updates: list[tuple[Any, ...]] = []
        try:
            for row in stale:
                counts["checked"] += 1
                spans = rtext.locate_text(source.page_count, source.geometry, row["text"], int(row["page_number"]) - 1)
                if spans:
                    boxes = [
                        [span.page_index, *(round(value, 2) for value in box)]
                        for span in spans
                        for box in rtext.line_boxes(source.geometry(span.page_index), span.char_ids)
                    ]
                    updates.append((spans[0].page_index + 1, json.dumps(boxes), current_hash, "relocated", _now(), row["id"]))
                    counts["relocated"] += 1
                else:
                    updates.append((row["page_number"], row["boxes_json"], current_hash, "lost", _now(), row["id"]))
                    counts["lost"] += 1
        finally:
            if owned:
                source.close()
        with self.connect() as connection:
            with connection:
                connection.executemany(
                    "UPDATE reading_marks SET page_number = ?, boxes_json = ?, pdf_hash = ?, located = ?, updated_at = ? WHERE id = ?",
                    updates,
                )
                connection.execute(
                    "UPDATE reading_documents SET last_pdf_hash = ?, updated_at = ? WHERE id = ?",
                    (current_hash, _now(), document["id"]),
                )
        return counts

    def request_reexplain(self, mark_id: int, question: str = "") -> dict[str, Any]:
        mark = self.mark(mark_id)
        if mark is None:
            raise ValueError(f"标记 {mark_id} 不存在。")
        if mark["status"] == "pending":
            status = "pending"
        else:
            status = "reexplain"
        self.backup("reexplain")
        with self.connect() as connection:
            with connection:
                connection.execute(
                    "UPDATE reading_marks SET status = ?, question = ?, updated_at = ? WHERE id = ?",
                    (status, question.strip(), _now(), int(mark_id)),
                )
        result = self.mark(mark_id)
        if result is None or result["status"] != status:
            raise RuntimeError("标记状态写入后回读不一致。")
        return result

    def delete_mark(self, mark_id: int) -> dict[str, Any]:
        mark = self.mark(mark_id)
        if mark is None:
            raise ValueError(f"标记 {mark_id} 不存在。")
        backup = self.backup("delete_mark")
        with self.connect() as connection:
            with connection:
                if mark["explanation"] is not None:
                    connection.execute(
                        """
                        INSERT INTO reading_explanation_history(mark_id, gist, answer, sentences_json, task_id, question)
                        SELECT mark_id, gist, answer, sentences_json, task_id, ? FROM reading_explanations WHERE mark_id = ?
                        """,
                        (mark["question"], int(mark_id)),
                    )
                connection.execute("DELETE FROM reading_explanations WHERE mark_id = ?", (int(mark_id),))
                connection.execute("DELETE FROM reading_marks WHERE id = ?", (int(mark_id),))
        if self.mark(mark_id) is not None:
            raise RuntimeError("删除标记后回读仍然存在。")
        return {"deleted": int(mark_id), "backup": str(backup) if backup else ""}

    # -- Work tasks --------------------------------------------------------------

    def _resolve_document(self, document: str | int) -> dict[str, Any]:
        rows = self.list_documents()
        text = str(document).strip()
        for row in rows:
            if text.isdigit() and int(text) == int(row["id"]):
                return row
        lowered = text.casefold()
        matches = [row for row in rows if lowered and (lowered in str(row["title"]).casefold() or lowered in str(row["rel_path"]).casefold())]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"没有找到文档：{document}。先运行 reading list 查看文档编号。")
        names = "；".join(f"{row['id']} {row['title']}" for row in matches[:8])
        raise ValueError(f"“{document}”对应多个文档，请改用编号：{names}")

    @staticmethod
    def _tex_excerpt(pdf_path: Path, text: str) -> dict[str, Any] | None:
        """Best-effort: the TeX lines of a project PDF that contain the marked prose."""

        words = re.findall(r"[A-Za-z]{2,}", text)
        runs: list[list[str]] = []
        current: list[str] = []
        for token in re.split(r"\s+", rtext.clean_text(text)):
            if re.fullmatch(r"[A-Za-z][A-Za-z,;:'-]*[.,;:]?", token):
                current.append(token)
            else:
                if len(current) >= 4:
                    runs.append(current)
                current = []
        if len(current) >= 4:
            runs.append(current)
        if not runs and len(words) < 4:
            return None
        probe = rtext.match_key(" ".join((max(runs, key=len) if runs else words)[:8]))
        if len(probe) < 16:
            return None
        candidates = sorted(
            (
                path for path in Path(pdf_path).parent.rglob("*.tex")
                if not any(part.casefold().startswith(("backup", "layout_backup")) or part.casefold().endswith("backups")
                           for part in path.relative_to(Path(pdf_path).parent).parts[:-1])
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:400]
        for tex_path in candidates:
            try:
                lines = tex_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            keys: list[str] = []
            owners: list[int] = []
            for number, line in enumerate(lines):
                for character in line:
                    if character.isalnum():
                        keys.append(character.casefold())
                        owners.append(number)
            position = "".join(keys).find(probe)
            if position < 0:
                continue
            line_number = owners[position]
            start, end = max(0, line_number - 4), min(len(lines), line_number + 12)
            return {
                "file": str(tex_path),
                "first_line": start + 1,
                "text": "\n".join(lines[start:end]),
            }
        return None

    @staticmethod
    def _render_crop(document: Any, boxes: Sequence[Sequence[float]], target: Path) -> list[str]:
        fitz = _fitz()
        written: list[str] = []
        pages: dict[int, list[Sequence[float]]] = {}
        for box in boxes:
            pages.setdefault(int(box[0]), []).append(box)
        for page_index, page_boxes in sorted(pages.items()):
            if not (0 <= page_index < document.page_count):
                continue
            page = document.load_page(page_index)
            x0 = min(float(box[1]) for box in page_boxes)
            y0 = min(float(box[2]) for box in page_boxes)
            x1 = max(float(box[3]) for box in page_boxes)
            y1 = max(float(box[4]) for box in page_boxes)
            clip = fitz.Rect(max(0, x0 - 24), max(0, y0 - 28), min(page.rect.width, x1 + 24), min(page.rect.height, y1 + 28))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), clip=clip, alpha=False)
            path = target.with_name(f"{target.stem}_p{page_index + 1}.png")
            pixmap.save(str(path))
            written.append(path.name)
        return written

    def prepare_task(self, document: str | int, *, mark_ids: Sequence[int] | None = None) -> dict[str, Any]:
        """Write a task folder with every pending/reexplain mark of one document."""

        row = self._resolve_document(document)
        marks = self.marks_for_document(int(row["id"]))
        pdf_path = self.resolve_document_path(row)
        if pdf_path is not None:
            self.relocate_marks(pdf_path)
            marks = self.marks_for_document(int(row["id"]))
        if mark_ids:
            wanted = {int(value) for value in mark_ids}
            selected = [mark for mark in marks if int(mark["id"]) in wanted]
            missing = wanted - {int(mark["id"]) for mark in selected}
            if missing:
                raise ValueError(f"这些标记不属于该文档：{sorted(missing)}")
        else:
            selected = [mark for mark in marks if mark["status"] in ("pending", "reexplain")]
        if not selected:
            raise ValueError(f"文档 {row['id']}《{row['title']}》没有待讲解的标记。")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        task_id = f"rh{int(row['id'])}_{stamp}"
        suffix = 1
        while (self.task_root / task_id).exists():
            suffix += 1
            task_id = f"rh{int(row['id'])}_{stamp}_{suffix}"
        task_dir = self.task_root / task_id
        crops_dir = task_dir / "crops"
        crops_dir.mkdir(parents=True, exist_ok=False)
        items = []
        document_handle = _fitz().open(str(pdf_path)) if pdf_path is not None else None
        try:
            for mark in selected:
                crops: list[str] = []
                if document_handle is not None and mark["boxes"] and mark["located"] != "lost":
                    crops = [f"crops/{name}" for name in self._render_crop(document_handle, mark["boxes"], crops_dir / f"mark_{mark['id']}.png")]
                items.append(
                    {
                        "mark_id": int(mark["id"]),
                        "status": mark["status"],
                        "scope": mark["scope"],
                        "page_number": int(mark["page_number"]),
                        "text": mark["text"],
                        "sentences": rtext.split_sentences(mark["text"]),
                        "context_before": mark["context_before"],
                        "context_after": mark["context_after"],
                        "question": mark["question"],
                        "crops": crops,
                        "tex_excerpt": self._tex_excerpt(pdf_path, mark["text"]) if pdf_path is not None else None,
                        "previous_explanation": mark["explanation"] if mark["status"] == "reexplain" else None,
                    }
                )
        finally:
            if document_handle is not None:
                document_handle.close()
        manifest = {
            "task_id": task_id,
            "document_id": int(row["id"]),
            "document_title": row["title"],
            "pdf_path": str(pdf_path) if pdf_path else "",
            "created_at": _now(),
            "marks": items,
        }
        (task_dir / "MARKS.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (task_dir / "TASK.md").write_text(self._task_markdown(manifest), encoding="utf-8")
        return {
            "task_id": task_id,
            "task_dir": str(task_dir),
            "task_file": str(task_dir / "TASK.md"),
            "marks_file": str(task_dir / "MARKS.json"),
            "output_file": str(task_dir / "OUTPUT.json"),
            "document": {"id": int(row["id"]), "title": row["title"]},
            "marks": len(items),
            "sentences": sum(len(item["sentences"]) for item in items),
        }

    @staticmethod
    def _task_markdown(manifest: dict[str, Any]) -> str:
        rules = RULES_PATH.read_text(encoding="utf-8")
        example = {
            "task_id": manifest["task_id"],
            "items": [
                {
                    "mark_id": manifest["marks"][0]["mark_id"],
                    "gist": "（多句时）这一段在说什么、句子之间的逻辑关系。",
                    "answer": "（用户写了 question 时）直接回答用户的提问。",
                    "split_note": "",
                    "sentences": [
                        {
                            "text": "英文原句，照抄 MARKS.json 中拆好的句子。",
                            "translation": "中文意思。",
                            "skeleton": "主干（英文），必要时加中文说明。",
                            "structures": [{"part": "原句片段", "role": "后置定语", "explanation": "修饰谁、怎么理解。"}],
                            "phrases": [{"term": "it suffices to", "meaning": "只需", "note": "数学证明中的惯用说法。"}],
                            "math_note": "",
                        }
                    ],
                }
            ],
        }
        lines = [
            f"# 难句讲解任务 {manifest['task_id']}",
            "",
            f"文档：{manifest['document_title']}（编号 {manifest['document_id']}）",
            f"PDF：{manifest['pdf_path'] or '（不在项目目录内，只有截图和文字）'}",
            f"标记数：{len(manifest['marks'])}",
            "",
            "读 `MARKS.json` 和 `crops/` 里的截图，按下面的规则写 `OUTPUT.json`（与本文件同目录），然后：",
            "",
            f"1. `.venv/bin/python -m shared.scripts.chatgpt_work reading check {manifest['task_id']}`",
            f"2. 检查通过后 `.venv/bin/python -m shared.scripts.chatgpt_work reading collect {manifest['task_id']}`",
            "",
            "Windows 上把 `.venv/bin/python` 换成项目使用的 `python`。",
            "",
            "## 讲解规则",
            "",
            rules.strip(),
            "",
            "## OUTPUT.json 示例",
            "",
            "```json",
            json.dumps(example, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
        return "\n".join(lines)

    def _task_dir(self, task: str | Path) -> Path:
        candidate = Path(str(task))
        if candidate.is_dir():
            return candidate.resolve()
        named = self.task_root / str(task)
        if named.is_dir():
            return named
        raise ValueError(f"找不到讲解任务：{task}")

    def check_task(self, task: str | Path) -> dict[str, Any]:
        task_dir = self._task_dir(task)
        manifest = json.loads((task_dir / "MARKS.json").read_text(encoding="utf-8"))
        output_path = task_dir / "OUTPUT.json"
        errors: list[str] = []
        warnings: list[str] = []
        if not output_path.is_file():
            return {"ok": False, "errors": [f"还没有 {output_path}"], "warnings": [], "items": []}
        try:
            output = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return {"ok": False, "errors": [f"OUTPUT.json 不是合法 JSON：{error}"], "warnings": [], "items": []}
        if str(output.get("task_id") or "") != manifest["task_id"]:
            errors.append(f"task_id 应为 {manifest['task_id']}")
        marks = {int(item["mark_id"]): item for item in manifest["marks"]}
        items = output.get("items")
        if not isinstance(items, list):
            return {"ok": False, "errors": errors + ["items 必须是列表"], "warnings": [], "items": []}
        seen: set[int] = set()
        clean_items: list[dict[str, Any]] = []
        for position, item in enumerate(items, start=1):
            label = f"第 {position} 项"
            if not isinstance(item, dict):
                errors.append(f"{label} 不是对象")
                continue
            try:
                mark_id = int(item.get("mark_id"))
            except (TypeError, ValueError):
                errors.append(f"{label} 缺少 mark_id")
                continue
            label = f"标记 {mark_id}"
            if mark_id not in marks:
                errors.append(f"{label} 不在本任务中")
                continue
            if mark_id in seen:
                errors.append(f"{label} 重复出现")
                continue
            seen.add(mark_id)
            source = marks[mark_id]
            sentences = item.get("sentences")
            if not isinstance(sentences, list) or not sentences:
                errors.append(f"{label} 没有 sentences")
                continue
            clean_sentences = []
            for index, sentence in enumerate(sentences, start=1):
                where = f"{label} 第 {index} 句"
                if not isinstance(sentence, dict):
                    errors.append(f"{where} 不是对象")
                    continue
                text = rtext.clean_text(str(sentence.get("text") or ""))
                translation = str(sentence.get("translation") or "").strip()
                skeleton = str(sentence.get("skeleton") or "").strip()
                if not text:
                    errors.append(f"{where} 缺少 text")
                if not _CJK_RE.search(translation):
                    errors.append(f"{where} 的 translation 必须是中文")
                if not skeleton:
                    errors.append(f"{where} 缺少 skeleton")
                structures = sentence.get("structures") or []
                phrases = sentence.get("phrases") or []
                if not isinstance(structures, list) or not isinstance(phrases, list):
                    errors.append(f"{where} 的 structures/phrases 必须是列表")
                    structures, phrases = [], []
                clean_structures = []
                for part in structures:
                    if not isinstance(part, dict) or not all(str(part.get(key) or "").strip() for key in ("part", "role", "explanation")):
                        errors.append(f"{where} 的 structures 每项都要有 part、role、explanation")
                        continue
                    if rtext.match_key(str(part["part"])) and rtext.match_key(str(part["part"])) not in rtext.match_key(text):
                        warnings.append(f"{where} 的成分“{str(part['part'])[:40]}”不在原句中")
                    clean_structures.append({key: str(part[key]).strip() for key in ("part", "role", "explanation")})
                clean_phrases = []
                for phrase in phrases:
                    if not isinstance(phrase, dict) or not str(phrase.get("term") or "").strip() or not str(phrase.get("meaning") or "").strip():
                        errors.append(f"{where} 的 phrases 每项都要有 term 和 meaning")
                        continue
                    clean_phrases.append({
                        "term": str(phrase["term"]).strip(),
                        "meaning": str(phrase["meaning"]).strip(),
                        "note": str(phrase.get("note") or "").strip(),
                    })
                if len(text.split()) >= 12 and not clean_structures:
                    errors.append(f"{where} 较长，structures 不能为空")
                clean_sentences.append({
                    "text": text,
                    "translation": translation,
                    "skeleton": skeleton,
                    "structures": clean_structures,
                    "phrases": clean_phrases,
                    "math_note": str(sentence.get("math_note") or "").strip(),
                })
            joined = rtext.match_key(" ".join(sentence["text"] for sentence in clean_sentences))
            if joined != rtext.match_key(source["text"]):
                errors.append(f"{label} 的全部句子连起来与原标记文字不一致")
            elif len(clean_sentences) != len(source["sentences"]) and not str(item.get("split_note") or "").strip():
                errors.append(f"{label} 改变了程序的拆句（{len(source['sentences'])} → {len(clean_sentences)}），请在 split_note 说明")
            gist = str(item.get("gist") or "").strip()
            if len(clean_sentences) > 1 and not _CJK_RE.search(gist):
                errors.append(f"{label} 有多句，gist 要用中文概括")
            answer = str(item.get("answer") or "").strip()
            if source.get("question") and not _CJK_RE.search(answer):
                errors.append(f"{label} 用户有提问，answer 要用中文回答")
            clean_items.append({
                "mark_id": mark_id,
                "gist": gist,
                "answer": answer,
                "split_note": str(item.get("split_note") or "").strip(),
                "sentences": clean_sentences,
            })
        missing = sorted(set(marks) - seen)
        if missing:
            errors.append(f"缺少这些标记的讲解：{missing}")
        return {
            "ok": not errors,
            "task_id": manifest["task_id"],
            "errors": errors,
            "warnings": warnings,
            "items": clean_items,
            "sentences": sum(len(item["sentences"]) for item in clean_items),
        }

    def collect_task(self, task: str | Path) -> dict[str, Any]:
        check = self.check_task(task)
        if not check["ok"]:
            raise ValueError("OUTPUT.json 未通过检查：\n" + "\n".join(check["errors"]))
        task_id = str(check["task_id"])
        existing = {int(item["mark_id"]): self.mark(int(item["mark_id"])) for item in check["items"]}
        gone = [mark_id for mark_id, mark in existing.items() if mark is None]
        if gone:
            raise ValueError(f"这些标记在任务生成后已被删除：{gone}")
        backup = self.backup(f"collect_{task_id}")
        with self.connect() as connection:
            with connection:
                for item in check["items"]:
                    mark_id = int(item["mark_id"])
                    mark = existing[mark_id]
                    assert mark is not None
                    if mark["explanation"] is not None:
                        connection.execute(
                            """
                            INSERT INTO reading_explanation_history(mark_id, gist, answer, sentences_json, task_id, question)
                            SELECT mark_id, gist, answer, sentences_json, task_id, ? FROM reading_explanations WHERE mark_id = ?
                            """,
                            (mark["question"], mark_id),
                        )
                    payload = json.dumps(item["sentences"], ensure_ascii=False)
                    connection.execute(
                        """
                        INSERT INTO reading_explanations(mark_id, gist, answer, sentences_json, task_id)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(mark_id) DO UPDATE SET gist = excluded.gist, answer = excluded.answer,
                            sentences_json = excluded.sentences_json, task_id = excluded.task_id,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (mark_id, item["gist"], item["answer"], payload, task_id),
                    )
                    connection.execute(
                        "UPDATE reading_marks SET status = 'explained', updated_at = ? WHERE id = ?",
                        (_now(), mark_id),
                    )
        for item in check["items"]:
            stored = self.mark(int(item["mark_id"]))
            if (
                stored is None
                or stored["status"] != "explained"
                or stored["explanation"] is None
                or stored["explanation"]["sentences"] != item["sentences"]
            ):
                raise RuntimeError(f"标记 {item['mark_id']} 的讲解写入后回读不一致；备份：{backup}")
        task_dir = self._task_dir(task)
        (task_dir / "COLLECTED.json").write_text(
            json.dumps({"collected_at": _now(), "marks": [item["mark_id"] for item in check["items"]]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "task_id": task_id,
            "imported_marks": len(check["items"]),
            "sentences": check["sentences"],
            "backup": str(backup) if backup else "",
            "warnings": check["warnings"],
        }


# -- registered operations -------------------------------------------------------

def _reading_operation_specs() -> tuple[Any, ...]:
    from shared.scripts.ai_agent_operation_registry import FORMAL_WRITE, READ_ONLY, DERIVED_WRITE, OperationSpec

    document = {"type": ["string", "integer"], "description": "文档编号，或标题/路径中能唯一确定文档的片段"}
    task = {"type": "string", "minLength": 1, "description": "讲解任务编号（rh…）或任务文件夹路径"}
    marks_parameters = {"type": "object", "properties": {
        "pdf_path": {"type": "string", "minLength": 1},
        "expected_pdf_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "title": {"type": "string"},
        "marks": {"type": "array", "minItems": 1, "maxItems": 300, "items": {
            "type": "object", "properties": {
                "key": {"type": "string"}, "text": {"type": "string", "minLength": 16, "maxLength": MAX_MARK_CHARS},
                "page_number": {"type": "integer", "minimum": 1},
                "scope": {"type": "string", "enum": list(SCOPES)}, "question": {"type": "string"},
                "segments": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                    "type": "object", "properties": {"text": {"type": "string", "minLength": 1},
                    "page_number": {"type": "integer", "minimum": 1}}, "required": ["text", "page_number"], "additionalProperties": False,
                }},
            }, "required": ["text"], "additionalProperties": False,
        }},
    }, "required": ["pdf_path", "expected_pdf_sha256", "marks"], "additionalProperties": False}
    return (
        OperationSpec(
            operation_id="reading_help.marks.preview", tool_name="preview_reading_help_marks",
            description="批量预览Work主动筛选的PDF原句：按PDF哈希核对并定位全部文字，自动取得页码、矩形与前后文；不写入标记。",
            parameters=marks_parameters, handler_name="_tool_preview_reading_help_marks", access_level=READ_ONLY,
            category="reading_help", evidence_policy="unchanged_pdf_hash_all_original_passages_located",
        ),
        OperationSpec(
            operation_id="reading_help.marks.import", tool_name="import_reading_help_marks",
            description="预览核对全部原句后批量保存阅读器标记；先备份、单事务提交、写后回读，重复原句复用原标记及讲解；可显式刷新同文原句的空格和标点，不改PDF。",
            parameters={**marks_parameters, "properties": {**marks_parameters["properties"],
                "refresh_existing_text": {"type": "boolean", "description": "显式刷新重复标记的已验证原句文字（相同文字键）；保留状态和已有讲解。"},
            }}, handler_name="_tool_import_reading_help_marks", access_level=FORMAL_WRITE,
            category="reading_help", evidence_policy="pdf_hash_preview_backup_single_transaction_mark_readback",
        ),
        OperationSpec(
            operation_id="reading_help.documents.list",
            tool_name="list_reading_help_documents",
            description="列出在项目 PDF 阅读器里有“不懂”标记的文档及待讲解、已讲解、位置失效的数量；不调用模型。",
            parameters={"type": "object", "properties": {"pending_only": {"type": "boolean"}}, "additionalProperties": False},
            handler_name="_tool_list_reading_help_documents", access_level=READ_ONLY,
            category="reading_help", evidence_policy="live_database_readback",
        ),
        OperationSpec(
            operation_id="reading_help.task.prepare",
            tool_name="prepare_reading_help_task",
            description="为一个文档的待讲解标记生成讲解任务文件夹：标记原文、程序拆好的句子、前后文、原页截图、讲义 TeX 原文和讲解规则；不调用模型。",
            parameters={"type": "object", "properties": {
                "document": document,
                "mark_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 200},
            }, "required": ["document"], "additionalProperties": False},
            handler_name="_tool_prepare_reading_help_task", access_level=DERIVED_WRITE,
            category="reading_help", evidence_policy="task_folder_from_live_marks_and_pdf_crops",
        ),
        OperationSpec(
            operation_id="reading_help.output.check",
            tool_name="check_reading_help_output",
            description="只检查讲解任务的 OUTPUT.json：覆盖全部标记、句子与原文一致、中文意思、主干和成分齐全；不写入。",
            parameters={"type": "object", "properties": {"task": task}, "required": ["task"], "additionalProperties": False},
            handler_name="_tool_check_reading_help_output", access_level=READ_ONLY,
            category="reading_help", evidence_policy="deterministic_output_validation",
        ),
        OperationSpec(
            operation_id="reading_help.output.import",
            tool_name="import_reading_help_output",
            description="检查通过后把讲解正式存入本地讲解库（先备份，旧讲解转入历史，写后回读），之后在 PDF 阅读器中可以查看。",
            parameters={"type": "object", "properties": {"task": task}, "required": ["task"], "additionalProperties": False},
            handler_name="_tool_import_reading_help_output", access_level=FORMAL_WRITE,
            category="reading_help", evidence_policy="backup_transaction_and_explanation_readback",
        ),
    )


READING_HELP_OPERATION_SPECS = _reading_operation_specs()
