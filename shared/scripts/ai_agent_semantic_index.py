from __future__ import annotations

import hashlib
import gzip
import json
import os
import re
import sqlite3
import uuid
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from itertools import chain
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

import fitz
import numpy as np

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_ocr import extract_pdf_page_text

from shared.scripts.ai_agent_history import ConversationHistoryStore, HISTORY_PATH
from shared.scripts.ai_agent_reference_library import (
    SETTINGS_PATH as REFERENCE_LIBRARY_SETTINGS_PATH,
    ReferenceLibraryStore,
)
from shared.scripts.vocabulary_manager import workspace_vocabulary_paths


ROOT_DIR = APP_PATHS.application_root
INDEX_PATH = APP_PATHS.cache_dir / "ai_agent_semantic_index.db"
PDF_CACHE_DIR = APP_PATHS.cache_dir / "ai_agent_pdf_text"
TEXTBOOK_PAGE_IMAGE_CACHE_DIR = (
    APP_PATHS.cache_dir / "ai_agent_textbook_page_images"
)
MATH_WORKSPACE_DIR = APP_PATHS.workspace_root / "MathWorkspace"
INDEX_VERSION = 7
VECTOR_DIMS = 512
INDEXED_PROJECT_SUFFIXES = {
    ".tex",
    ".txt",
    ".md",
    ".bib",
    ".sty",
    ".cls",
    ".json",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
}
DOMAIN_ALIASES = {
    "one-dimensional": "一维",
    "one dimensional": "一维",
    "connected": "连通",
    "manifold": "流形",
    "classification": "分类",
    "homeomorphic": "同胚",
    "compactness": "紧致性 紧致",
    "compact": "紧致",
    "finite subcover": "有限子覆盖",
    "open cover": "开覆盖",
    "mobius": "莫比乌斯 Möbius",
    "möbius": "莫比乌斯 Mobius",
    "parameterization": "参数化",
    "parametrization": "参数化",
    "diffeomorphism": "微分同胚",
    "coordinate chart": "坐标图",
}


def _search_terms(text: str) -> list[str]:
    value = str(text or "").casefold()
    terms: list[str] = []
    terms.extend(re.findall(r"[a-z][a-z0-9_-]{1,}|\\[a-z]+|\d+(?:\.\d+)+", value))
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", value):
        if len(segment) <= 12:
            terms.append(segment)
        terms.extend(segment[index : index + 2] for index in range(len(segment) - 1))
        if len(segment) >= 3:
            terms.extend(segment[index : index + 3] for index in range(len(segment) - 2))
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        compact = term.strip()
        if compact and compact not in seen:
            seen.add(compact)
            unique.append(compact)
    return unique[:6000]


@lru_cache(maxsize=24)
def _bilingual_glossary(
    vocabulary_database: str = "",
    modified_ns: int = 0,
    file_size: int = 0,
) -> tuple[tuple[str, str], ...]:
    del modified_ns, file_size  # Values intentionally participate in the cache key.
    pairs = list(DOMAIN_ALIASES.items())
    path = Path(vocabulary_database) if vocabulary_database else None
    if path is not None and path.is_file():
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                rows = connection.execute(
                    "SELECT term,definition FROM vocabulary_entries "
                    "WHERE TRIM(term)<>'' AND TRIM(definition)<>''"
                ).fetchall()
        except sqlite3.Error:
            rows = []
        for term, definition in rows:
            english = str(term or "").casefold().strip()
            chinese = str(definition or "").strip()
            if english and re.search(r"[a-z]", english) and re.search(r"[\u3400-\u9fff]", chinese):
                pairs.append((english, chinese))
    unique: dict[str, str] = {}
    for english, chinese in pairs:
        unique.setdefault(english, chinese)
    return tuple(unique.items())


def _bilingual_alias_text(text: str, vocabulary_database: str = "") -> str:
    folded = str(text or "").casefold()
    aliases: list[str] = []
    path = Path(vocabulary_database) if vocabulary_database else None
    try:
        stat = path.stat() if path is not None else None
    except OSError:
        stat = None
    glossary = _bilingual_glossary(
        vocabulary_database,
        int(stat.st_mtime_ns) if stat is not None else 0,
        int(stat.st_size) if stat is not None else 0,
    )
    for english, chinese in glossary:
        if english in folded:
            aliases.append(chinese)
        elif any(term in folded for term in re.findall(r"[\u3400-\u9fff]{2,}", chinese)):
            aliases.append(english)
    return " ".join(aliases)


def _expanded_terms(text: str, vocabulary_database: str = "") -> list[str]:
    return _search_terms(
        str(text or "") + " " + _bilingual_alias_text(text, vocabulary_database)
    )


def _fts_text(text: str, vocabulary_database: str = "") -> str:
    return " ".join(_expanded_terms(text, vocabulary_database))


def _fts_query(text: str, vocabulary_database: str = "") -> str:
    terms = _expanded_terms(text, vocabulary_database)[:40]
    if not terms:
        raise ValueError("语义搜索关键词不能为空。")
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _semantic_vector(text: str = "", *, terms: list[str] | None = None) -> np.ndarray:
    """Build a deterministic, private local vector for bilingual reranking.

    The vector deliberately uses no remote embedding service.  Bilingual aliases,
    Chinese character n-grams, English terms and adjacent term pairs are hashed
    into a normalized feature vector.  It is less knowledgeable than a hosted
    embedding model, but it covers every local source without API cost or upload.
    """

    selected_terms = list(terms) if terms is not None else _expanded_terms(text)
    features = list(selected_terms)
    features.extend(
        f"{left}::{right}" for left, right in zip(selected_terms, selected_terms[1:])
    )
    vector = np.zeros(VECTOR_DIMS, dtype=np.float32)
    for feature in features[:12000]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "little")
        index = raw % VECTOR_DIMS
        sign = 1.0 if raw & (1 << 63) else -1.0
        vector[index] += sign
    norm = float(np.linalg.norm(vector))
    if norm:
        vector /= norm
    return vector


def _text_chunks(text: str, *, target_chars: int = 10000, overlap: int = 800) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    if len(value) <= target_chars:
        return [value]
    chunks: list[str] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + target_chars)
        if end < len(value):
            boundary = max(value.rfind("\n\n", start + target_chars // 2, end), value.rfind("。", start + target_chars // 2, end))
            if boundary > start:
                end = boundary + 1
        chunks.append(value[start:end].strip())
        if end >= len(value):
            break
        start = max(start + 1, end - overlap)
    return [chunk for chunk in chunks if chunk]


def _pdf_cache_path(path: Path, *, ocr_page_limit: int | None = 12) -> Path:
    stat = path.stat()
    ocr_mode = "all" if ocr_page_limit is None else str(max(0, int(ocr_page_limit)))
    cache_version = "pdf-v2-ocr" if ocr_mode == "12" else f"pdf-v3-ocr-{ocr_mode}"
    fingerprint = hashlib.sha256(
        f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{cache_version}".encode("utf-8")
    ).hexdigest()
    return PDF_CACHE_DIR / f"{fingerprint}.json.gz"


def _incomplete_pdf_cache_path(path: Path) -> Path:
    complete = _pdf_cache_path(path, ocr_page_limit=None)
    return complete.with_name(complete.name.replace(".json.gz", ".incomplete.json.gz"))


def _load_pdf_page_records(cache_path: Path) -> list[dict[str, Any]] | None:
    try:
        with gzip.open(cache_path, "rt", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, list):
        return [
            {
                "page_number": index,
                "text": str(text),
                "method": "legacy_cache",
                "ocr_confidence": None,
                "ocr_error": "",
            }
            for index, text in enumerate(raw, 1)
        ]
    pages = raw.get("pages") if isinstance(raw, dict) else None
    if not isinstance(pages, list):
        return None
    records: list[dict[str, Any]] = []
    for index, item in enumerate(pages, 1):
        if not isinstance(item, dict):
            item = {"text": str(item)}
        records.append(
            {
                "page_number": int(item.get("page_number") or index),
                "text": str(item.get("text") or ""),
                "method": str(item.get("method") or "unreadable"),
                "ocr_confidence": item.get("ocr_confidence"),
                "ocr_error": str(item.get("ocr_error") or ""),
            }
        )
    return records


def _write_pdf_page_records(cache_path: Path, records: list[dict[str, Any]]) -> None:
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + f".{uuid.uuid4().hex}.tmp")
    payload = {"version": 4, "pages": records}
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.replace(temporary, cache_path)


def _extract_pdf_page_records(
    path: Path,
    *,
    ocr_page_limit: int | None = 12,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    ocr_count = 0
    with fitz.open(path) as document:
        for page_number, page in enumerate(document, 1):
            allow_ocr = ocr_page_limit is None or ocr_count < max(0, int(ocr_page_limit))
            extracted = dict(extract_pdf_page_text(page, allow_ocr=allow_ocr))
            method = str(extracted.get("method") or "unreadable")
            if allow_ocr and method in {"ocr", "unreadable"}:
                ocr_count += 1
            if not allow_ocr and not str(extracted.get("text") or "").strip():
                method = "ocr_deferred"
            records.append(
                {
                    "page_number": page_number,
                    "text": str(extracted.get("text") or ""),
                    "method": method,
                    "ocr_confidence": extracted.get("ocr_confidence"),
                    "ocr_error": str(extracted.get("ocr_error") or ""),
                }
            )
    return records


def _pdf_pages(path: Path, *, ocr_page_limit: int | None = 12) -> list[str]:
    cache_path = _pdf_cache_path(path, ocr_page_limit=ocr_page_limit)
    cached = _load_pdf_page_records(cache_path)
    if cached is not None:
        return [str(item.get("text") or "") for item in cached]
    records = _extract_pdf_page_records(path, ocr_page_limit=ocr_page_limit)
    ocr_errors = [
        str(item.get("ocr_error") or "")
        for item in records
        if str(item.get("method") or "") == "unreadable" and item.get("ocr_error")
    ]
    if ocr_page_limit is None and any(
        str(item.get("method") or "") == "unreadable" for item in records
    ):
        _write_pdf_page_records(_incomplete_pdf_cache_path(path), records)
        raise RuntimeError(
            "扫描教材完整 OCR 未完成："
            + ("; ".join(dict.fromkeys(ocr_errors)) or "仍有页面无法识别")
        )
    _write_pdf_page_records(cache_path, records)
    if ocr_page_limit is None:
        _incomplete_pdf_cache_path(path).unlink(missing_ok=True)
    return [str(item.get("text") or "") for item in records]


class _ReferenceHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "svg", "noscript"}:
            self.ignored_depth += 1
        elif tag.casefold() in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "svg", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag.casefold() in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def _reference_text(path: Path, max_chars: int = 1_500_000) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml")
            root = ElementTree.fromstring(xml)
        except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
            return ""
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs: list[str] = []
        for paragraph in root.iter(namespace + "p"):
            text = "".join(node.text or "" for node in paragraph.iter(namespace + "t"))
            if text:
                paragraphs.append(text)
        return "\n".join(paragraphs)[:max_chars]
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    if suffix in {".html", ".htm"}:
        parser = _ReferenceHtmlParser()
        parser.feed(text)
        text = parser.text()
    return text[:max_chars]


def _snippet(content: str, query: str, limit: int = 1800) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(text) <= limit:
        return text
    positions = [text.casefold().find(term.casefold()) for term in _search_terms(query)[:12]]
    positions = [position for position in positions if position >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 4)
    end = min(len(text), start + limit)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


@dataclass(slots=True)
class SemanticDocument:
    doc_id: str
    kind: str
    subject_name: str
    project_ref: str
    problem_ref: str
    path: str
    title: str
    content: str
    updated_at: str = ""
    page_start: int = 0
    page_end: int = 0


class SemanticIndex:
    def __init__(
        self,
        repository: Any,
        path: Path = INDEX_PATH,
        history_path: Path = HISTORY_PATH,
        reference_library_path: Path | None = None,
        workspace_dir: Path = MATH_WORKSPACE_DIR,
    ) -> None:
        self.repository = repository
        self.path = Path(path)
        self.history_path = Path(history_path)
        self.workspace_dir = Path(workspace_dir).resolve()
        self.workspace_name = self.workspace_dir.name
        repository_root = Path(getattr(repository, "root_dir", ROOT_DIR)).resolve()
        vocabulary_workspace = str(
            getattr(repository, "vocabulary_workspace", "math") or "math"
        )
        self.vocabulary_database = workspace_vocabulary_paths(
            vocabulary_workspace,
            root_dir=repository_root,
        )[0]
        self.reference_library_path = Path(
            reference_library_path
            if reference_library_path is not None
            else REFERENCE_LIBRARY_SETTINGS_PATH
            if repository_root == ROOT_DIR.resolve()
            else self.path.with_name("ai_math_reference_library.json")
        )

    @staticmethod
    def _ensure_textbook_health_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS textbook_index_health(
                subject_name TEXT NOT NULL,
                book_id INTEGER NOT NULL,
                book_code TEXT NOT NULL,
                title TEXT NOT NULL,
                pdf_path TEXT NOT NULL,
                pdf_exists INTEGER NOT NULL DEFAULT 0,
                pdf_openable INTEGER NOT NULL DEFAULT 0,
                file_size INTEGER NOT NULL DEFAULT 0,
                file_mtime_ns INTEGER NOT NULL DEFAULT 0,
                file_changed INTEGER NOT NULL DEFAULT 0,
                total_pages INTEGER NOT NULL DEFAULT 0,
                indexed_pages INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                text_layer_pages INTEGER NOT NULL DEFAULT 0,
                ocr_pages INTEGER NOT NULL DEFAULT 0,
                unreadable_pages INTEGER NOT NULL DEFAULT 0,
                deferred_ocr_pages INTEGER NOT NULL DEFAULT 0,
                complete_extraction INTEGER NOT NULL DEFAULT 0,
                last_successful_index_at TEXT NOT NULL DEFAULT '',
                stale INTEGER NOT NULL DEFAULT 1,
                stale_reason TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(subject_name, book_id)
            );
            CREATE TABLE IF NOT EXISTS textbook_page_health(
                subject_name TEXT NOT NULL,
                book_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                extraction_method TEXT NOT NULL,
                text_length INTEGER NOT NULL DEFAULT 0,
                indexed INTEGER NOT NULL DEFAULT 0,
                ocr_confidence REAL,
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(subject_name, book_id, page_number)
            );
            CREATE INDEX IF NOT EXISTS idx_textbook_page_health_state
            ON textbook_page_health(subject_name, book_id, extraction_method, page_number);
            """
        )

    def _reference_store(self) -> ReferenceLibraryStore:
        return ReferenceLibraryStore(
            self.reference_library_path,
            auto_add_default=self.reference_library_path.resolve()
            == REFERENCE_LIBRARY_SETTINGS_PATH.resolve(),
        )

    def _project_sources(self) -> list[tuple[dict[str, Any], Path]]:
        sources: list[tuple[dict[str, Any], Path]] = []
        for project in self.repository.list_projects().get("projects", []):
            try:
                directory, _row = self.repository._project_directory(
                    str(project.get("subject_name") or ""),
                    str(project.get("project_code") or project.get("project_id") or ""),
                )
            except (OSError, ValueError, sqlite3.Error):
                continue
            sources.append((dict(project), Path(directory)))
        return sources

    def _textbook_catalog(self) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for subject_name, config in self.repository.subject_configs().items():
            db_path = Path(config["db"])
            if not db_path.is_file():
                continue
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.row_factory = sqlite3.Row
                    if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books'"
                    ).fetchone() is None:
                        continue
                    columns = {
                        str(row[1]) for row in connection.execute("PRAGMA table_info(books)")
                    }
                    if "pdf_path" not in columns:
                        continue
                    rows = connection.execute(
                        "SELECT id, book_code, title, pdf_path FROM books ORDER BY book_code,id"
                    ).fetchall()
                    project_rows = []
                    if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='problem_collections'"
                    ).fetchone():
                        project_rows.extend(
                            connection.execute(
                                "SELECT id, collection_code, book_id FROM problem_collections"
                            ).fetchall()
                        )
                    collection_book_rows = []
                    if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_books'"
                    ).fetchone():
                        collection_book_rows = connection.execute(
                            "SELECT collection_id, book_id FROM collection_books"
                        ).fetchall()
            except sqlite3.Error:
                continue
            project_code_by_id = {
                int(row["id"]): str(row["collection_code"] or row["id"])
                for row in project_rows
            }
            project_refs_by_book: dict[int, set[str]] = {}
            for row in project_rows:
                if row["book_id"] is not None:
                    project_refs_by_book.setdefault(int(row["book_id"]), set()).add(
                        str(row["collection_code"] or row["id"])
                    )
            for row in collection_book_rows:
                project_ref = project_code_by_id.get(int(row["collection_id"]))
                if project_ref:
                    project_refs_by_book.setdefault(int(row["book_id"]), set()).add(project_ref)
            for row in rows:
                raw_path = str(row["pdf_path"] or "").strip()
                path = Path(raw_path).expanduser() if raw_path else None
                sources.append(
                    {
                        "subject_name": str(subject_name),
                        "book_id": int(row["id"]),
                        "book_code": str(row["book_code"] or row["id"]),
                        "title": str(
                            row["title"]
                            or row["book_code"]
                            or (path.stem if path is not None else row["id"])
                        ),
                        "path": path.resolve() if path is not None and path.is_file() else raw_path,
                        "pdf_bound": bool(raw_path),
                        "pdf_exists": bool(path is not None and path.is_file()),
                        "project_ref": ",".join(sorted(project_refs_by_book.get(int(row["id"]), set()))),
                    }
                )
        return sources

    def _textbook_sources(self) -> list[dict[str, Any]]:
        return [
            source
            for source in self._textbook_catalog()
            if source.get("pdf_exists")
            and Path(str(source.get("path") or "")).suffix.casefold() == ".pdf"
        ]

    def content_source_signature(
        self,
        *,
        ignore_complete_cache_paths: Iterable[str] | None = None,
    ) -> str:
        ignored_complete_caches = {
            str(Path(path).resolve()).casefold()
            for path in (ignore_complete_cache_paths or [])
            if str(path).strip()
        }
        rows: list[tuple[str, int, int]] = []
        for config in self.repository.subject_configs().values():
            db_path = Path(config["db"])
            if db_path.is_file():
                stat = db_path.stat()
                rows.append((str(db_path.resolve()), stat.st_mtime_ns, stat.st_size))
        if self.vocabulary_database.is_file():
            stat = self.vocabulary_database.stat()
            rows.append(
                (
                    str(self.vocabulary_database.resolve()),
                    stat.st_mtime_ns,
                    stat.st_size,
                )
            )
        for _project, directory in self._project_sources():
            for path in directory.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in INDEXED_PROJECT_SUFFIXES | {".pdf"}:
                    continue
                stat = path.stat()
                rows.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        for textbook in self._textbook_sources():
            path = Path(textbook["path"])
            stat = path.stat()
            rows.append((str(path), stat.st_mtime_ns, stat.st_size))
            complete_cache = _pdf_cache_path(path, ocr_page_limit=None)
            if (
                str(path.resolve()).casefold() not in ignored_complete_caches
                and complete_cache.is_file()
            ):
                cache_stat = complete_cache.stat()
                rows.append(
                    (str(complete_cache.resolve()), cache_stat.st_mtime_ns, cache_stat.st_size)
                )
        if self.workspace_dir.is_dir():
            for path in self.workspace_dir.rglob("*"):
                if path.is_file() and path.suffix.casefold() in INDEXED_PROJECT_SUFFIXES | {".pdf"}:
                    stat = path.stat()
                    rows.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        reference_store = self._reference_store()
        if reference_store.path.is_file():
            stat = reference_store.path.stat()
            rows.append((str(reference_store.path.resolve()), stat.st_mtime_ns, stat.st_size))
        for _root, path in reference_store.iter_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        payload = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def history_signature(self) -> str:
        if not self.history_path.is_file():
            return "missing"
        stat = self.history_path.stat()
        payload = f"{self.history_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def source_signature(self) -> str:
        return hashlib.sha256(
            (self.content_source_signature() + "|" + self.history_signature()).encode("utf-8")
        ).hexdigest()

    def _current_meta(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                return {str(key): str(value) for key, value in connection.execute("SELECT key,value FROM meta")}
        except sqlite3.Error:
            return {}

    def ensure_current(self, *, force: bool = False) -> dict[str, Any]:
        content_signature = self.content_source_signature()
        history_signature = self.history_signature()
        meta = self._current_meta()
        if (
            not force
            and meta.get("version") == str(INDEX_VERSION)
            and meta.get("content_source_signature") == content_signature
        ):
            if meta.get("history_signature") != history_signature:
                return self.refresh_history(
                    content_signature=content_signature,
                    history_signature=history_signature,
                )
            return {
                "rebuilt": False,
                "history_refreshed": False,
                "document_count": int(meta.get("document_count") or 0),
                "source_signature": self.source_signature(),
            }
        return self.rebuild(
            content_signature=content_signature,
            history_signature=history_signature,
        )

    def _problem_project_map(self) -> dict[tuple[str, int], list[str]]:
        mapping: dict[tuple[str, int], list[str]] = {}
        for project in self.repository.list_projects().get("projects", []):
            subject = str(project.get("subject_name") or "")
            project_ref = str(project.get("project_code") or project.get("project_id") or "")
            try:
                page = self.repository.get_project_problems(subject, project_ref, 100)
            except (OSError, ValueError, sqlite3.Error):
                continue
            for problem in page.get("problems", []):
                key = (subject, int(problem.get("problem_id") or 0))
                mapping.setdefault(key, []).append(project_ref)
        return mapping

    def _problem_documents(self) -> Iterable[SemanticDocument]:
        project_map = self._problem_project_map()
        for subject in self.repository.subject_configs():
            offset = 0
            while True:
                page = self.repository.list_subject_problems(subject, offset=offset, limit=100)
                problems = page.get("problems", [])
                for summary in problems:
                    problem_ref = str(summary.get("problem_code") or summary.get("problem_id") or "")
                    try:
                        problem = self.repository.get_problem(subject, problem_ref)
                    except (OSError, ValueError, sqlite3.Error):
                        continue
                    projects = project_map.get((subject, int(problem.get("problem_id") or 0)), [])
                    textbook_sources = problem.get("textbook_sources") or []
                    content = "\n".join(
                        str(problem.get(key) or "")
                        for key in (
                            "title",
                            "chapter_code",
                            "chapter_name",
                            "section_code",
                            "section_name",
                            "summary_tex",
                            "main_method",
                            "statement_tex",
                            "solution_tex",
                            "notes",
                        )
                    )
                    if textbook_sources:
                        content += "\n" + json.dumps(textbook_sources, ensure_ascii=False)
                    yield SemanticDocument(
                        doc_id=f"problem:{subject}:{problem_ref}",
                        kind="problem",
                        subject_name=subject,
                        project_ref=",".join(projects),
                        problem_ref=problem_ref,
                        path="",
                        title=str(problem.get("title") or problem_ref),
                        content=content[:140000],
                    )
                next_offset = page.get("next_offset")
                if next_offset is None or not problems:
                    break
                offset = int(next_offset)

    def _project_file_documents(self) -> Iterable[SemanticDocument]:
        for project, directory in self._project_sources():
            subject = str(project.get("subject_name") or "")
            project_ref = str(project.get("project_code") or project.get("project_id") or "")
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or path.suffix.casefold() not in INDEXED_PROJECT_SUFFIXES:
                    continue
                if path.stat().st_size > 2_000_000:
                    continue
                relative = path.relative_to(directory).as_posix()
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for chunk_index, chunk in enumerate(_text_chunks(content[:500000]), 1):
                    yield SemanticDocument(
                        doc_id=f"project_file:{subject}:{project_ref}:{relative}:{chunk_index}",
                        kind="project_file",
                        subject_name=subject,
                        project_ref=project_ref,
                        problem_ref="",
                        path=str(path.resolve()),
                        title=f"{project_ref} / {relative} / 片段 {chunk_index}",
                        content=chunk,
                        updated_at=str(path.stat().st_mtime_ns),
                    )

    def _pdf_documents(
        self,
        *,
        only_paths: Iterable[str] | None = None,
    ) -> Iterable[SemanticDocument]:
        sources: list[dict[str, Any]] = list(self._textbook_sources())
        selected_paths = {
            str(Path(path).resolve()).casefold()
            for path in (only_paths or [])
            if str(path).strip()
        }
        if selected_paths:
            sources = [
                source
                for source in sources
                if str(Path(source["path"]).resolve()).casefold() in selected_paths
            ]
        known_paths = {str(Path(item["path"]).resolve()).casefold() for item in sources}
        if not selected_paths:
            for project, directory in self._project_sources():
                subject = str(project.get("subject_name") or "")
                project_ref = str(project.get("project_code") or project.get("project_id") or "")
                for path in sorted(directory.rglob("*.pdf")):
                    resolved = str(path.resolve()).casefold()
                    if resolved in known_paths or path.stat().st_size > 200 * 1024 * 1024:
                        continue
                    sources.append(
                        {
                            "subject_name": subject,
                            "book_code": "",
                            "project_ref": project_ref,
                            "title": path.stem,
                            "path": path.resolve(),
                        }
                    )
                    known_paths.add(resolved)
            if self.workspace_dir.is_dir():
                for path in sorted(self.workspace_dir.rglob("*.pdf")):
                    resolved = str(path.resolve()).casefold()
                    if resolved in known_paths or path.stat().st_size > 200 * 1024 * 1024:
                        continue
                    sources.append(
                        {
                            "subject_name": "",
                            "book_code": "",
                            "project_ref": self.workspace_name,
                            "title": path.stem,
                            "path": path.resolve(),
                            "workspace_pdf": True,
                        }
                    )
                    known_paths.add(resolved)
        for source in sources:
            path = Path(source["path"])
            try:
                # Text-layer extraction covers every page immediately.  Full
                # OCR of a scanned textbook is deliberately deferred until the
                # model selects that book; once made, that complete cache is
                # reused by every later rebuild and query.
                full_textbook_cache = bool(
                    source.get("book_code")
                    and _pdf_cache_path(path, ocr_page_limit=None).is_file()
                )
                pages = _pdf_pages(
                    path,
                    ocr_page_limit=None if full_textbook_cache else 12,
                )
            except (OSError, RuntimeError, ValueError, fitz.FileDataError):
                continue
            kind = (
                "textbook_pdf"
                if source.get("book_code")
                else ("physics_workspace" if self.workspace_name == "PhysicsWorkspace" else "math_workspace")
                if source.get("workspace_pdf")
                else "project_pdf"
            )
            source_ref = str(source.get("book_code") or source.get("project_ref") or path.stem)
            source_key = hashlib.sha1(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:12]
            page_index = 0
            chunk_index = 0
            while page_index < len(pages):
                start = page_index
                collected: list[str] = []
                characters = 0
                while page_index < len(pages) and (characters < 12000 or page_index == start):
                    page_text = pages[page_index]
                    collected.append(page_text)
                    characters += len(page_text)
                    page_index += 1
                    if page_index - start >= 4:
                        break
                content = "\n\n".join(collected).strip()
                if not content:
                    continue
                chunk_index += 1
                page_start = start + 1
                page_end = page_index
                yield SemanticDocument(
                    doc_id=f"{kind}:{source['subject_name']}:{source_ref}:{source_key}:{chunk_index}",
                    kind=kind,
                    subject_name=str(source["subject_name"]),
                    project_ref=str(source.get("project_ref") or ""),
                    problem_ref="",
                    path=str(path),
                    title=f"{source_ref} / {source['title']} / 第 {page_start}-{page_end} 页",
                    content=content[:30000],
                    updated_at=str(path.stat().st_mtime_ns),
                    page_start=page_start,
                    page_end=page_end,
                )

    def _math_workspace_documents(self) -> Iterable[SemanticDocument]:
        if not self.workspace_dir.is_dir():
            return
        kind = "physics_workspace" if self.workspace_name == "PhysicsWorkspace" else "math_workspace"
        for path in sorted(self.workspace_dir.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in INDEXED_PROJECT_SUFFIXES:
                continue
            if path.stat().st_size > 5_000_000:
                continue
            relative = path.relative_to(self.workspace_dir).as_posix()
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for chunk_index, chunk in enumerate(_text_chunks(content[:500000]), 1):
                yield SemanticDocument(
                    doc_id=f"{kind}:{relative}:{chunk_index}",
                    kind=kind,
                    subject_name="",
                    project_ref=self.workspace_name,
                    problem_ref="",
                    path=str(path.resolve()),
                    title=f"{self.workspace_name} / {relative} / 片段 {chunk_index}",
                    content=chunk,
                    updated_at=str(path.stat().st_mtime_ns),
                )

    def _reference_library_documents(self) -> Iterable[SemanticDocument]:
        store = self._reference_store()
        for root, path in store.iter_files():
            try:
                stat = path.stat()
                relative = path.relative_to(Path(root.path)).as_posix()
            except (OSError, ValueError):
                continue
            source_key = hashlib.sha1(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:12]
            if path.suffix.casefold() == ".pdf":
                if stat.st_size > 300 * 1024 * 1024:
                    continue
                try:
                    pages = _pdf_pages(path)
                except (OSError, RuntimeError, ValueError, fitz.FileDataError):
                    continue
                page_index = 0
                chunk_index = 0
                while page_index < len(pages):
                    start = page_index
                    collected: list[str] = []
                    characters = 0
                    while page_index < len(pages) and (characters < 10000 or page_index == start):
                        page_text = pages[page_index]
                        collected.append(page_text)
                        characters += len(page_text)
                        page_index += 1
                        if page_index - start >= 4:
                            break
                    content = "\n\n".join(collected).strip()
                    if not content:
                        continue
                    chunk_index += 1
                    yield SemanticDocument(
                        doc_id=f"reference_library:{root.id}:{source_key}:pdf:{chunk_index}",
                        kind="reference_library",
                        subject_name="",
                        project_ref="",
                        problem_ref="",
                        path=str(path.resolve()),
                        title=f"{root.name} / {relative} / 第 {start + 1}-{page_index} 页",
                        content=content[:30000],
                        updated_at=str(stat.st_mtime_ns),
                        page_start=start + 1,
                        page_end=page_index,
                    )
                continue
            if stat.st_size > 12 * 1024 * 1024:
                continue
            content = _reference_text(path)
            if not content.strip():
                continue
            for chunk_index, chunk in enumerate(_text_chunks(content, target_chars=9000), 1):
                yield SemanticDocument(
                    doc_id=f"reference_library:{root.id}:{source_key}:text:{chunk_index}",
                    kind="reference_library",
                    subject_name="",
                    project_ref="",
                    problem_ref="",
                    path=str(path.resolve()),
                    title=f"{root.name} / {relative} / 片段 {chunk_index}",
                    content=chunk,
                    updated_at=str(stat.st_mtime_ns),
                )

    def _history_documents(self) -> Iterable[SemanticDocument]:
        store = ConversationHistoryStore(self.history_path)
        for record in store.all():
            content = "\n\n".join(
                ("用户：" if message["role"] == "user" else "AI：") + message["content"]
                for message in record.messages
            )
            for chunk_index, chunk in enumerate(_text_chunks(content[:500000]), 1):
                yield SemanticDocument(
                    doc_id=f"conversation:{record.id}:{chunk_index}",
                    kind="conversation",
                    subject_name="",
                    project_ref="",
                    problem_ref="",
                    path="",
                    title=f"{record.title} / 片段 {chunk_index}",
                    content=chunk,
                    updated_at=record.updated_at,
                )

    def _insert_document(self, connection: sqlite3.Connection, document: SemanticDocument) -> None:
        vocabulary_database = str(self.vocabulary_database)
        feature_terms = _expanded_terms(
            document.title + "\n" + document.content,
            vocabulary_database,
        )
        embedding = _semantic_vector(terms=feature_terms)
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                document.doc_id,
                document.kind,
                document.subject_name,
                document.project_ref,
                document.problem_ref,
                document.path,
                document.title,
                document.content,
                document.updated_at,
                document.page_start,
                document.page_end,
                json.dumps(feature_terms, ensure_ascii=False, separators=(",", ":")),
                embedding.tobytes(),
            ),
        )
        connection.execute(
            "INSERT INTO documents_fts(doc_id,search_text) VALUES(?,?)",
            (
                document.doc_id,
                _fts_text(
                    document.title + "\n" + document.content,
                    vocabulary_database,
                ),
            ),
        )

    def refresh_history(
        self,
        *,
        content_signature: str | None = None,
        history_signature: str | None = None,
    ) -> dict[str, Any]:
        with closing(sqlite3.connect(self.path)) as connection:
            conversation_ids = [
                str(row[0])
                for row in connection.execute("SELECT doc_id FROM documents WHERE kind='conversation'")
            ]
            if conversation_ids:
                placeholders = ",".join("?" for _ in conversation_ids)
                connection.execute(
                    f"DELETE FROM documents_fts WHERE doc_id IN ({placeholders})", conversation_ids
                )
            connection.execute("DELETE FROM documents WHERE kind='conversation'")
            for document in self._history_documents():
                self._insert_document(connection, document)
            count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            by_kind = {
                str(kind): int(value)
                for kind, value in connection.execute(
                    "SELECT kind,COUNT(*) FROM documents GROUP BY kind"
                )
            }
            meta = {
                "version": str(INDEX_VERSION),
                "content_source_signature": content_signature or self.content_source_signature(),
                "history_signature": history_signature or self.history_signature(),
                "document_count": str(count),
                "kind_counts": json.dumps(by_kind, ensure_ascii=False),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", meta.items()
            )
            connection.commit()
        return {
            "rebuilt": False,
            "history_refreshed": True,
            "document_count": count,
            "kind_counts": by_kind,
            "source_signature": self.source_signature(),
        }

    def refresh_textbook_documents(self, paths: Iterable[str]) -> dict[str, Any]:
        """Transactionally replace only selected textbook chunks."""

        selected_paths = sorted(
            {
                str(Path(path).resolve())
                for path in paths
                if str(path).strip()
            }
        )
        if not selected_paths:
            return {
                "rebuilt": False,
                "textbooks_refreshed": 0,
                "document_count": int(self._current_meta().get("document_count") or 0),
            }
        documents = list(self._pdf_documents(only_paths=selected_paths))
        placeholders = ",".join("?" for _ in selected_paths)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            old_ids = [
                str(row[0])
                for row in connection.execute(
                    f"SELECT doc_id FROM documents WHERE kind='textbook_pdf' "
                    f"AND path IN ({placeholders})",
                    selected_paths,
                )
            ]
            if old_ids:
                for offset in range(0, len(old_ids), 500):
                    batch = old_ids[offset : offset + 500]
                    id_placeholders = ",".join("?" for _ in batch)
                    connection.execute(
                        f"DELETE FROM documents_fts WHERE doc_id IN ({id_placeholders})",
                        batch,
                    )
            connection.execute(
                f"DELETE FROM documents WHERE kind='textbook_pdf' "
                f"AND path IN ({placeholders})",
                selected_paths,
            )
            for document in documents:
                self._insert_document(connection, document)
            count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            by_kind = {
                str(kind): int(value)
                for kind, value in connection.execute(
                    "SELECT kind,COUNT(*) FROM documents GROUP BY kind"
                )
            }
            current_meta = {
                str(key): str(value)
                for key, value in connection.execute("SELECT key,value FROM meta")
            }
            meta = {
                "version": str(INDEX_VERSION),
                "content_source_signature": self.content_source_signature(),
                "history_signature": current_meta.get(
                    "history_signature", self.history_signature()
                ),
                "document_count": str(count),
                "kind_counts": json.dumps(by_kind, ensure_ascii=False),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", meta.items()
            )
            connection.commit()
        return {
            "rebuilt": False,
            "textbooks_refreshed": len(selected_paths),
            "refreshed_document_count": len(documents),
            "document_count": count,
            "kind_counts": by_kind,
            "source_signature": self.source_signature(),
        }

    def rebuild(
        self,
        *,
        content_signature: str | None = None,
        history_signature: str | None = None,
    ) -> dict[str, Any]:
        selected_content_signature = content_signature or self.content_source_signature()
        selected_history_signature = history_signature or self.history_signature()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A fixed ``.tmp`` name makes a background index refresh in the running
        # Qt app collide with an offline test or a second window on Windows.
        # SQLite keeps the file handle open for the lifetime of that builder,
        # so the other process cannot even remove the stale-looking path.  Give
        # every rebuild its own sibling file and publish it atomically only
        # after the connection has been closed.
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        count = 0
        by_kind: dict[str, int] = {}
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE documents(
                    doc_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    project_ref TEXT NOT NULL,
                    problem_ref TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    page_start INTEGER NOT NULL DEFAULT 0,
                    page_end INTEGER NOT NULL DEFAULT 0,
                    feature_terms TEXT NOT NULL,
                    embedding BLOB NOT NULL
                );
                CREATE VIRTUAL TABLE documents_fts USING fts5(doc_id UNINDEXED, search_text);
                CREATE INDEX idx_semantic_kind ON documents(kind);
                CREATE INDEX idx_semantic_subject_project ON documents(subject_name, project_ref);
                """
            )
                self._ensure_textbook_health_schema(connection)
                for document in chain(
                    self._problem_documents(),
                    self._project_file_documents(),
                    self._math_workspace_documents(),
                    self._reference_library_documents(),
                    self._pdf_documents(),
                    self._history_documents(),
                ):
                    self._insert_document(connection, document)
                    count += 1
                    by_kind[document.kind] = by_kind.get(document.kind, 0) + 1
                connection.executemany(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    (
                        ("version", str(INDEX_VERSION)),
                        ("content_source_signature", selected_content_signature),
                        ("history_signature", selected_history_signature),
                        ("document_count", str(count)),
                        ("kind_counts", json.dumps(by_kind, ensure_ascii=False)),
                    ),
                )
                connection.commit()
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "rebuilt": True,
            "document_count": count,
            "kind_counts": by_kind,
            "source_signature": self.source_signature(),
        }

    def search(
        self,
        query: str,
        *,
        kinds: list[str] | None = None,
        subject_name: str = "",
        project_ref: str = "",
        paths: Iterable[str] | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        status = self.ensure_current()
        limit = max(1, min(int(limit), 30))
        # Search all in-scope vectors rather than only FTS hits.  FTS remains in
        # the database for exact diagnostics, while the local vector makes a
        # natural-language query able to retrieve a chunk with no literal term.
        vocabulary_database = str(self.vocabulary_database)
        _fts_query(query, vocabulary_database)  # validate that the query has searchable content
        clauses = ["1=1"]
        arguments: list[Any] = []
        selected_kinds = [str(kind) for kind in (kinds or []) if str(kind)]
        if not selected_kinds and not re.search(r"历史|之前|上次|过去|对话|聊过|回答过", str(query or "")):
            selected_kinds = [
                "problem",
                "project_file",
                "textbook_pdf",
                "project_pdf",
                "math_workspace",
                "reference_library",
            ]
        if selected_kinds:
            clauses.append("d.kind IN (" + ",".join("?" for _ in selected_kinds) + ")")
            arguments.extend(selected_kinds)
        if subject_name:
            clauses.append("(d.subject_name=? OR d.kind='reference_library')")
            arguments.append(str(subject_name))
        if project_ref:
            clauses.append("((',' || d.project_ref || ',') LIKE ? OR d.kind='reference_library')")
            arguments.append(f"%,{project_ref},%")
        selected_paths = [str(Path(path).resolve()) for path in (paths or []) if str(path).strip()]
        if selected_paths:
            clauses.append("d.path IN (" + ",".join("?" for _ in selected_paths) + ")")
            arguments.extend(selected_paths)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            vector_rows = connection.execute(
                """
                SELECT d.doc_id,d.title,d.embedding
                FROM documents d
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY d.kind,d.doc_id",
                arguments,
            ).fetchall()
        query_terms_list = _expanded_terms(query, vocabulary_database)
        query_terms = set(query_terms_list)
        query_vector = _semantic_vector(terms=query_terms_list)
        anchor_terms = [
            term.casefold()
            for term in re.findall(r"[a-zà-öø-ÿ][a-z0-9à-öø-ÿ_-]{3,}", str(query or "").casefold())
            if term.casefold() not in {"with", "from", "that", "this", "what", "problem", "proof"}
        ]
        vector_candidates: list[tuple[float, str]] = []
        folded_query = str(query or "").casefold()
        for row in vector_rows:
            embedding = np.frombuffer(row["embedding"], dtype=np.float32)
            semantic_similarity = (
                float(np.dot(query_vector, embedding))
                if embedding.size == VECTOR_DIMS
                else 0.0
            )
            title = str(row["title"]).casefold()
            exact_boost = 1.0 if folded_query and folded_query in title else 0.0
            vector_candidates.append((semantic_similarity + exact_boost, str(row["doc_id"])))
        vector_candidates.sort(key=lambda item: item[0], reverse=True)
        candidate_ids = [doc_id for _score, doc_id in vector_candidates[:160]]
        if not candidate_ids:
            rows: list[sqlite3.Row] = []
        else:
            placeholders = ",".join("?" for _ in candidate_ids)
            with closing(sqlite3.connect(self.path)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    f"SELECT d.*,0.0 AS rank FROM documents d WHERE d.doc_id IN ({placeholders})",
                    candidate_ids,
                ).fetchall()
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            title = str(row["title"])
            content = str(row["content"])
            embedding = np.frombuffer(row["embedding"], dtype=np.float32)
            semantic_similarity = (
                float(np.dot(query_vector, embedding))
                if embedding.size == VECTOR_DIMS
                else 0.0
            )
            feature_terms = [str(item) for item in json.loads(str(row["feature_terms"] or "[]"))]
            expanded_content = (title + "\n" + content).casefold()
            if anchor_terms and not any(anchor in expanded_content for anchor in anchor_terms):
                continue
            title_terms = set(_expanded_terms(title, vocabulary_database))
            content_terms = set(feature_terms)
            coverage = len(query_terms & content_terms) / max(1, len(query_terms))
            title_coverage = len(query_terms & title_terms) / max(1, len(query_terms))
            exact_bonus = 0
            folded_content = content.casefold()
            folded_title = title.casefold()
            for phrase in re.findall(r"[a-z][a-z0-9 _-]{2,}|[\u3400-\u9fff]{2,}", str(query or "").casefold()):
                if phrase in folded_title:
                    exact_bonus += 45
                elif phrase in folded_content:
                    exact_bonus += 18
            kind = str(row["kind"])
            kind_bonus = (
                14
                if kind == "problem"
                else 7
                if kind in {"project_file", "textbook_pdf"}
                else 4
                if kind == "reference_library"
                else 0
            )
            score = (
                coverage * 100
                + title_coverage * 80
                + semantic_similarity * 120
                + exact_bonus
                + kind_bonus
                - float(row["rank"])
            )
            payload = {
                "kind": str(row["kind"]),
                "subject_name": str(row["subject_name"]),
                "project_ref": str(row["project_ref"]),
                "problem_ref": str(row["problem_ref"]),
                "path": str(row["path"]),
                "title": str(row["title"]),
                "snippet": _snippet(str(row["content"]), query),
                "page_start": int(row["page_start"] or 0),
                "page_end": int(row["page_end"] or 0),
                "rank": float(row["rank"]),
                "semantic_similarity": round(semantic_similarity, 6),
                "term_coverage": round(coverage, 6),
                "title_term_coverage": round(title_coverage, 6),
                "relevance_score": round(score, 4),
            }
            ranked.append((score, payload))
        ranked.sort(key=lambda item: (-item[0], item[1]["kind"], item[1]["title"]))
        results = [payload for _score, payload in ranked[:limit]]
        return {
            "query": query,
            "results": results,
            "result_count": len(results),
            "index_rebuilt": bool(status.get("rebuilt")),
            "indexed_document_count": int(status.get("document_count") or 0),
        }

    def _textbook_catalog_item(
        self,
        subject_name: str,
        book_ref: int | str,
    ) -> dict[str, Any]:
        folded = str(book_ref).strip().casefold()
        matches = [
            source
            for source in self._textbook_catalog()
            if (not subject_name or str(source.get("subject_name") or "") == subject_name)
            and folded
            in {
                str(source.get("book_id") or "").casefold(),
                str(source.get("book_code") or "").casefold(),
            }
        ]
        if len(matches) != 1:
            raise ValueError(f"没有唯一找到教材：{subject_name} / {book_ref}")
        return matches[0]

    @staticmethod
    def _record_method_for_health(record: dict[str, Any], native_text: str) -> str:
        method = str(record.get("method") or "unreadable")
        if method != "legacy_cache":
            return method
        native_visible = len(re.sub(r"\s+", "", native_text))
        cached_visible = len(re.sub(r"\s+", "", str(record.get("text") or "")))
        if native_visible >= 48:
            return "text_layer"
        if cached_visible >= 12:
            return "ocr"
        return "ocr_deferred"

    def textbook_health_status(self, subject_name: str = "") -> dict[str, Any]:
        """Rebuild the derived health view without writing textbook source data."""

        prior_health: dict[tuple[str, int], dict[str, Any]] = {}
        if self.path.is_file():
            try:
                with closing(sqlite3.connect(self.path)) as prior_connection:
                    prior_connection.row_factory = sqlite3.Row
                    if prior_connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='textbook_index_health'"
                    ).fetchone():
                        prior_health = {
                            (str(row["subject_name"]), int(row["book_id"])): dict(row)
                            for row in prior_connection.execute(
                                "SELECT * FROM textbook_index_health"
                            )
                        }
            except sqlite3.Error:
                prior_health = {}
        self.ensure_current()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        catalogue = [
            source
            for source in self._textbook_catalog()
            if not subject_name or str(source.get("subject_name") or "") == subject_name
        ]
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            self._ensure_textbook_health_schema(connection)
            previous = {
                (str(row["subject_name"]), int(row["book_id"])): dict(row)
                for row in connection.execute("SELECT * FROM textbook_index_health")
            }
            previous.update(prior_health)
            document_rows = connection.execute(
                """
                SELECT path, page_start, page_end, updated_at
                FROM documents WHERE kind='textbook_pdf'
                ORDER BY path, page_start, page_end
                """
            ).fetchall()
            documents_by_path: dict[str, list[sqlite3.Row]] = {}
            for row in document_rows:
                key = str(Path(str(row["path"])).resolve()).casefold()
                documents_by_path.setdefault(key, []).append(row)

            active_keys: set[tuple[str, int]] = set()
            for source in catalogue:
                subject = str(source.get("subject_name") or "")
                book_id = int(source.get("book_id") or 0)
                active_keys.add((subject, book_id))
                old = previous.get((subject, book_id), {})
                raw_path = str(source.get("path") or "")
                path = Path(raw_path).expanduser() if raw_path else None
                pdf_exists = bool(path is not None and path.is_file())
                pdf_openable = False
                total_pages = 0
                file_size = 0
                file_mtime_ns = 0
                current_error = ""
                native_texts: list[str] = []
                if pdf_exists and path is not None:
                    try:
                        stat = path.stat()
                        file_size = int(stat.st_size)
                        file_mtime_ns = int(stat.st_mtime_ns)
                        with fitz.open(path) as document:
                            total_pages = int(document.page_count)
                            native_texts = [page.get_text("text", sort=True).strip() for page in document]
                        pdf_openable = True
                    except (OSError, ValueError, fitz.FileDataError) as error:
                        current_error = str(error)

                resolved_key = (
                    str(path.resolve()).casefold()
                    if pdf_exists and path is not None
                    else ""
                )
                docs = documents_by_path.get(resolved_key, [])
                chunk_count = len(docs)
                indexed_ranges = [
                    (int(row["page_start"] or 0), int(row["page_end"] or 0))
                    for row in docs
                ]
                indexed_mtimes = {str(row["updated_at"] or "") for row in docs}
                file_changed = bool(
                    pdf_exists
                    and (
                        (old and int(old.get("file_mtime_ns") or 0) not in {0, file_mtime_ns})
                        or (old and int(old.get("file_size") or 0) not in {0, file_size})
                        or (indexed_mtimes and str(file_mtime_ns) not in indexed_mtimes)
                    )
                )
                index_outdated = bool(
                    pdf_exists
                    and indexed_mtimes
                    and str(file_mtime_ns) not in indexed_mtimes
                )

                records: list[dict[str, Any]] = []
                if pdf_openable and path is not None:
                    for candidate in (
                        _pdf_cache_path(path, ocr_page_limit=None),
                        _incomplete_pdf_cache_path(path),
                        _pdf_cache_path(path),
                    ):
                        loaded = _load_pdf_page_records(candidate)
                        if loaded is not None:
                            records = loaded
                            break
                methods: dict[int, str] = {}
                page_rows: list[tuple[Any, ...]] = []
                indexed_page_count = 0
                for page_number in range(1, total_pages + 1):
                    record = records[page_number - 1] if page_number <= len(records) else {
                        "page_number": page_number,
                        "text": "",
                        "method": "ocr_deferred",
                        "ocr_confidence": None,
                        "ocr_error": "",
                    }
                    native = native_texts[page_number - 1] if page_number <= len(native_texts) else ""
                    method = self._record_method_for_health(record, native)
                    methods[page_number] = method
                    text_length = len(str(record.get("text") or "").strip())
                    indexed_page = bool(
                        text_length
                        and any(start <= page_number <= end for start, end in indexed_ranges)
                    )
                    indexed_page_count += int(indexed_page)
                    page_rows.append(
                        (
                            subject,
                            book_id,
                            page_number,
                            method,
                            text_length,
                            int(indexed_page),
                            record.get("ocr_confidence"),
                            str(record.get("ocr_error") or ""),
                        )
                    )
                text_layer_pages = sum(method == "text_layer" for method in methods.values())
                ocr_pages = sum(method == "ocr" for method in methods.values())
                unreadable_pages = sum(method == "unreadable" for method in methods.values())
                deferred_pages = sum(method == "ocr_deferred" for method in methods.values())
                complete_extraction = bool(
                    pdf_openable
                    and total_pages > 0
                    and len(records) == total_pages
                    and unreadable_pages == 0
                    and deferred_pages == 0
                )
                reasons: list[str] = []
                if not raw_path:
                    reasons.append("尚未绑定 PDF")
                elif not pdf_exists:
                    reasons.append("绑定的 PDF 已丢失")
                elif not pdf_openable:
                    reasons.append("PDF 无法打开")
                if index_outdated:
                    reasons.append("PDF 文件内容或修改时间已变化")
                if pdf_openable and chunk_count == 0:
                    reasons.append("尚未建立教材分段索引")
                if pdf_openable and indexed_page_count < total_pages:
                    reasons.append(f"仅索引 {indexed_page_count}/{total_pages} 页")
                if deferred_pages:
                    reasons.append(f"{deferred_pages} 页等待 OCR")
                if unreadable_pages:
                    reasons.append(f"{unreadable_pages} 页无法识别")
                stale = bool(reasons)
                successful_now = bool(
                    pdf_openable and chunk_count > 0 and not index_outdated and indexed_page_count > 0
                )
                last_success = str(old.get("last_successful_index_at") or "")
                if successful_now and (
                    not last_success
                    or int(old.get("file_mtime_ns") or 0) != file_mtime_ns
                    or int(old.get("chunk_count") or 0) != chunk_count
                ):
                    last_success = now
                last_error = current_error or str(old.get("last_error") or "")
                if not stale and not current_error:
                    last_error = ""
                connection.execute(
                    """
                    INSERT OR REPLACE INTO textbook_index_health VALUES(
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        subject,
                        book_id,
                        str(source.get("book_code") or ""),
                        str(source.get("title") or ""),
                        raw_path,
                        int(pdf_exists),
                        int(pdf_openable),
                        file_size,
                        file_mtime_ns,
                        int(file_changed),
                        total_pages,
                        indexed_page_count,
                        chunk_count,
                        text_layer_pages,
                        ocr_pages,
                        unreadable_pages,
                        deferred_pages,
                        int(complete_extraction),
                        last_success,
                        int(stale),
                        "；".join(reasons),
                        last_error,
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM textbook_page_health WHERE subject_name=? AND book_id=?",
                    (subject, book_id),
                )
                if page_rows:
                    connection.executemany(
                        "INSERT INTO textbook_page_health VALUES(?,?,?,?,?,?,?,?)",
                        page_rows,
                    )

            existing_keys = {
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT subject_name,book_id FROM textbook_index_health"
                )
            }
            for removed_subject, removed_book_id in existing_keys - active_keys:
                connection.execute(
                    "DELETE FROM textbook_index_health WHERE subject_name=? AND book_id=?",
                    (removed_subject, removed_book_id),
                )
                connection.execute(
                    "DELETE FROM textbook_page_health WHERE subject_name=? AND book_id=?",
                    (removed_subject, removed_book_id),
                )
            connection.commit()
            rows = connection.execute(
                "SELECT * FROM textbook_index_health "
                + ("WHERE subject_name=? " if subject_name else "")
                + "ORDER BY subject_name,book_code,book_id",
                (subject_name,) if subject_name else (),
            ).fetchall()
        textbooks = [dict(row) for row in rows]
        return {
            "dataset_path": str(self.path.resolve()),
            "derived_state_only": True,
            "textbooks": textbooks,
            "textbook_count": len(textbooks),
            "stale_count": sum(bool(item.get("stale")) for item in textbooks),
            "missing_pdf_count": sum(not bool(item.get("pdf_exists")) for item in textbooks),
            "unreadable_page_count": sum(int(item.get("unreadable_pages") or 0) for item in textbooks),
        }

    def _record_textbook_health_error(
        self,
        subject_name: str,
        book_id: int,
        error: Exception | str,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            self._ensure_textbook_health_schema(connection)
            connection.execute(
                "UPDATE textbook_index_health SET last_error=?, stale=1, checked_at=? "
                "WHERE subject_name=? AND book_id=?",
                (
                    str(error),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    subject_name,
                    int(book_id),
                ),
            )
            connection.commit()

    def complete_textbook_ocr(self, subject_name: str, book_ref: int | str) -> dict[str, Any]:
        source = self._textbook_catalog_item(subject_name, book_ref)
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"教材 PDF 不存在：{path}")
        self.ensure_current()
        try:
            _pdf_pages(path, ocr_page_limit=None)
            refreshed = self.refresh_textbook_documents([str(path.resolve())])
            health = self.textbook_health_status(subject_name)
        except Exception as error:
            self.textbook_health_status(subject_name)
            self._record_textbook_health_error(subject_name, int(source["book_id"]), error)
            raise
        item = next(
            row for row in health["textbooks"] if int(row["book_id"]) == int(source["book_id"])
        )
        return {"operation": "complete_ocr", "textbook": item, **refreshed}

    def repair_failed_textbook_pages(
        self,
        subject_name: str,
        book_ref: int | str,
    ) -> dict[str, Any]:
        source = self._textbook_catalog_item(subject_name, book_ref)
        book_id = int(source["book_id"])
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"教材 PDF 不存在：{path}")
        self.textbook_health_status(subject_name)
        with closing(sqlite3.connect(self.path)) as connection:
            failed_pages = [
                int(row[0])
                for row in connection.execute(
                    "SELECT page_number FROM textbook_page_health "
                    "WHERE subject_name=? AND book_id=? AND extraction_method='unreadable' "
                    "ORDER BY page_number",
                    (subject_name, book_id),
                )
            ]
        if not failed_pages:
            return {"operation": "repair_failed_pages", "repaired_pages": [], "remaining_failed_pages": []}
        complete_cache = _pdf_cache_path(path, ocr_page_limit=None)
        incomplete_cache = _incomplete_pdf_cache_path(path)
        partial_cache = _pdf_cache_path(path)
        records = (
            _load_pdf_page_records(complete_cache)
            or _load_pdf_page_records(incomplete_cache)
            or _load_pdf_page_records(partial_cache)
        )
        if records is None:
            records = _extract_pdf_page_records(path, ocr_page_limit=12)
        try:
            with fitz.open(path) as document:
                for page_number in failed_pages:
                    if not 1 <= page_number <= len(records) or page_number > document.page_count:
                        continue
                    extracted = dict(extract_pdf_page_text(document[page_number - 1], allow_ocr=True))
                    records[page_number - 1] = {
                        "page_number": page_number,
                        "text": str(extracted.get("text") or ""),
                        "method": str(extracted.get("method") or "unreadable"),
                        "ocr_confidence": extracted.get("ocr_confidence"),
                        "ocr_error": str(extracted.get("ocr_error") or ""),
                    }
            remaining = [
                int(record.get("page_number") or index)
                for index, record in enumerate(records, 1)
                if str(record.get("method") or "") == "unreadable"
            ]
            deferred = any(
                str(record.get("method") or "") == "ocr_deferred" for record in records
            )
            _write_pdf_page_records(partial_cache, records)
            if not remaining and not deferred:
                _write_pdf_page_records(complete_cache, records)
                incomplete_cache.unlink(missing_ok=True)
            else:
                _write_pdf_page_records(incomplete_cache, records)
            refreshed = self.refresh_textbook_documents([str(path.resolve())])
            self.textbook_health_status(subject_name)
        except Exception as error:
            self._record_textbook_health_error(subject_name, book_id, error)
            raise
        return {
            "operation": "repair_failed_pages",
            "repaired_pages": [page for page in failed_pages if page not in remaining],
            "remaining_failed_pages": remaining,
            **refreshed,
        }

    def rebuild_textbook_index(self, subject_name: str, book_ref: int | str) -> dict[str, Any]:
        source = self._textbook_catalog_item(subject_name, book_ref)
        book_id = int(source["book_id"])
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"教材 PDF 不存在：{path}")
        self.ensure_current()
        try:
            for cache_path in (
                _pdf_cache_path(path),
                _pdf_cache_path(path, ocr_page_limit=None),
                _incomplete_pdf_cache_path(path),
            ):
                cache_path.unlink(missing_ok=True)
            refreshed = self.refresh_textbook_documents([str(path.resolve())])
            health = self.textbook_health_status(subject_name)
        except Exception as error:
            self._record_textbook_health_error(subject_name, book_id, error)
            raise
        item = next(row for row in health["textbooks"] if int(row["book_id"]) == book_id)
        return {"operation": "rebuild_index", "textbook": item, **refreshed}

    def unrecognized_textbook_pages(
        self,
        subject_name: str,
        book_ref: int | str,
    ) -> dict[str, Any]:
        source = self._textbook_catalog_item(subject_name, book_ref)
        book_id = int(source["book_id"])
        self.textbook_health_status(subject_name)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT page_number,extraction_method,text_length,ocr_confidence,error "
                "FROM textbook_page_health WHERE subject_name=? AND book_id=? "
                "AND extraction_method='unreadable' ORDER BY page_number",
                (subject_name, book_id),
            ).fetchall()
        return {
            "book_id": book_id,
            "book_code": str(source.get("book_code") or ""),
            "title": str(source.get("title") or ""),
            "pdf_path": str(source.get("path") or ""),
            "pages": [dict(row) for row in rows],
            "page_count": len(rows),
        }

    def verify_textbook_index_hit(
        self,
        subject_name: str,
        book_ref: int | str,
        query: str,
        *,
        limit: int = 8,
    ) -> dict[str, Any]:
        if not str(query or "").strip():
            raise ValueError("请输入要验证命中的内容。")
        source = self._textbook_catalog_item(subject_name, book_ref)
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"教材 PDF 不存在：{path}")
        result = self.search(
            str(query),
            kinds=["textbook_pdf"],
            subject_name=subject_name,
            paths=[str(path.resolve())],
            limit=limit,
        )
        return {
            "book_id": int(source["book_id"]),
            "book_code": str(source.get("book_code") or ""),
            "title": str(source.get("title") or ""),
            "query": str(query),
            "hit": bool(result.get("results")),
            "results": result.get("results", []),
            "result_count": int(result.get("result_count") or 0),
        }

    def render_textbook_pages_for_ai(
        self,
        subject_name: str,
        book_ref: int | str,
        page_numbers: Iterable[int],
        *,
        dpi: int = 220,
        inspection_focus: str = "",
    ) -> dict[str, Any]:
        """Render a few exact textbook pages as derived multimodal evidence."""

        source = self._textbook_catalog_item(subject_name, book_ref)
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"教材 PDF 不存在：{path}")
        pages = sorted({int(page) for page in page_numbers})
        if not pages:
            raise ValueError("请指定至少一个教材页码。")
        if len(pages) > 4:
            raise ValueError("一次最多渲染 4 个精确教材页面，禁止整本视觉读取。")
        dpi = max(120, min(int(dpi), 260))
        stat = path.stat()
        source_fingerprint = hashlib.sha256(
            f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")
        ).hexdigest()
        cache_dir = TEXTBOOK_PAGE_IMAGE_CACHE_DIR / source_fingerprint
        cache_dir.mkdir(parents=True, exist_ok=True)

        records: list[dict[str, Any]] = []
        for candidate in (
            _pdf_cache_path(path, ocr_page_limit=None),
            _incomplete_pdf_cache_path(path),
            _pdf_cache_path(path),
        ):
            loaded = _load_pdf_page_records(candidate)
            if loaded is not None:
                records = loaded
                break

        visual_evidence: list[dict[str, Any]] = []
        page_evidence: list[dict[str, Any]] = []
        with fitz.open(path) as document:
            total_pages = int(document.page_count)
            invalid = [page for page in pages if page < 1 or page > total_pages]
            if invalid:
                raise ValueError(
                    f"教材共 {total_pages} 页，以下页码超出范围：{', '.join(map(str, invalid))}"
                )
            matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            for page_number in pages:
                image_path = cache_dir / f"page_{page_number:05d}_{dpi}dpi.png"
                if not image_path.is_file():
                    pixmap = document[page_number - 1].get_pixmap(
                        matrix=matrix,
                        colorspace=fitz.csRGB,
                        alpha=False,
                    )
                    temporary = image_path.with_suffix(
                        image_path.suffix + f".{uuid.uuid4().hex}.tmp"
                    )
                    temporary.write_bytes(pixmap.tobytes("png"))
                    os.replace(temporary, image_path)
                native_text = document[page_number - 1].get_text("text", sort=True).strip()
                record = records[page_number - 1] if page_number <= len(records) else {}
                method = self._record_method_for_health(record, native_text) if record else (
                    "text_layer" if len(re.sub(r"\s+", "", native_text)) >= 48 else "unindexed"
                )
                selected_text = str(record.get("text") or native_text).strip()
                image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
                visual_evidence.append(
                    {
                        "kind": "image",
                        "name": f"{source.get('book_code')} 第 {page_number} 页",
                        "path": str(image_path.resolve()),
                        "mime_type": "image/png",
                        "source_path": str(path.resolve()),
                        "subject_name": str(source.get("subject_name") or ""),
                        "book_code": str(source.get("book_code") or ""),
                        "page_number": page_number,
                        "sha256": image_sha256,
                    }
                )
                page_evidence.append(
                    {
                        "page_number": page_number,
                        "extraction_method": method,
                        "text_excerpt": selected_text[:5000],
                        "text_length": len(selected_text),
                        "ocr_confidence": record.get("ocr_confidence"),
                        "ocr_error": str(record.get("ocr_error") or ""),
                        "image_path": str(image_path.resolve()),
                        "image_sha256": image_sha256,
                    }
                )
        return {
            "operation": "render_textbook_pages_for_ai",
            "derived_state_only": True,
            "subject_name": str(source.get("subject_name") or ""),
            "book_id": int(source.get("book_id") or 0),
            "book_code": str(source.get("book_code") or ""),
            "title": str(source.get("title") or ""),
            "pdf_path": str(path.resolve()),
            "source_fingerprint": source_fingerprint,
            "page_numbers": pages,
            "page_count": len(pages),
            "dpi": dpi,
            "inspection_focus": str(inspection_focus or "").strip(),
            "page_evidence": page_evidence,
            "visual_evidence": visual_evidence,
            "visual_delivery": "attach_to_next_model_round",
            "scope_rule": "Only these exact pages were rendered; the rest of the textbook was not sent to the model.",
        }

    def inspect_textbook_pages_visual(
        self,
        subject_name: str,
        book_ref: int | str,
        page_numbers: Iterable[int],
        *,
        inspection_focus: str = "核对公式、上下标、特殊符号和图形",
        dpi: int = 220,
    ) -> dict[str, Any]:
        result = self.render_textbook_pages_for_ai(
            subject_name,
            book_ref,
            page_numbers,
            dpi=dpi,
            inspection_focus=inspection_focus,
        )
        result["operation"] = "inspect_textbook_pages_visual"
        result["model_instruction"] = (
            "请直接查看随工具结果附加的页面图像，并结合 page_evidence 核对指定内容；"
            "不得只根据 OCR 猜测公式。回答时保留教材编号和精确页码。"
        )
        return result

    def textbook_dataset_status(self, subject_name: str = "") -> dict[str, Any]:
        """Describe the local segmented textbook dataset without returning content."""

        status = self.ensure_current()
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                """
                SELECT path, COUNT(*) AS chunk_count,
                       MIN(page_start) AS page_start, MAX(page_end) AS page_end,
                       SUM(LENGTH(content)) AS indexed_chars
                FROM documents
                WHERE kind='textbook_pdf'
                GROUP BY path
                """
            ).fetchall()
        indexed = {
            str(Path(str(path)).resolve()).casefold(): {
                "chunk_count": int(chunk_count or 0),
                "page_start": int(page_start or 0),
                "page_end": int(page_end or 0),
                "indexed_chars": int(indexed_chars or 0),
            }
            for path, chunk_count, page_start, page_end, indexed_chars in rows
        }
        textbooks: list[dict[str, Any]] = []
        for source in self._textbook_sources():
            if subject_name and str(source.get("subject_name") or "") != subject_name:
                continue
            path = Path(source["path"]).resolve()
            metrics = indexed.get(str(path).casefold(), {})
            textbooks.append(
                {
                    "subject_name": str(source.get("subject_name") or ""),
                    "book_code": str(source.get("book_code") or ""),
                    "title": str(source.get("title") or path.stem),
                    "path": str(path),
                    "indexed": bool(metrics),
                    "full_extraction_cached": _pdf_cache_path(
                        path, ocr_page_limit=None
                    ).is_file(),
                    **metrics,
                }
            )
        textbooks.sort(
            key=lambda item: (
                str(item["subject_name"]).casefold(),
                str(item["book_code"]).casefold(),
            )
        )
        return {
            "dataset_kind": "local_segmented_textbook_retrieval",
            "dataset_path": str(self.path.resolve()),
            "index_version": INDEX_VERSION,
            "index_rebuilt": bool(status.get("rebuilt")),
            "textbooks": textbooks,
            "textbook_count": len(textbooks),
            "chunk_count": sum(int(item.get("chunk_count") or 0) for item in textbooks),
            "token_policy": "The model receives only selected snippets; complete textbooks remain local.",
            "full_extraction_cached_count": sum(
                1 for item in textbooks if item.get("full_extraction_cached")
            ),
            "ocr_policy": "Full-page OCR is deferred until a textbook is selected, then cached locally for later searches.",
        }

    def search_textbook_content(
        self,
        query: str,
        *,
        textbook_refs: Iterable[str] | None = None,
        subject_name: str = "",
        limit: int = 8,
    ) -> dict[str, Any]:
        """Search one or a few selected textbook datasets and return short passages."""

        sources = [
            source
            for source in self._textbook_sources()
            if not subject_name or str(source.get("subject_name") or "") == subject_name
        ]
        requested = [str(item).strip() for item in (textbook_refs or []) if str(item).strip()]
        selected: list[dict[str, Any]] = []
        if requested:
            for reference in requested:
                folded = reference.casefold()
                exact = [
                    source
                    for source in sources
                    if folded
                    in {
                        str(source.get("book_code") or "").casefold(),
                        str(source.get("title") or "").casefold(),
                        Path(source["path"]).name.casefold(),
                        Path(source["path"]).stem.casefold(),
                        str(Path(source["path"]).resolve()).casefold(),
                    }
                ]
                matches = exact or [
                    source
                    for source in sources
                    if folded in str(source.get("book_code") or "").casefold()
                    or folded in str(source.get("title") or "").casefold()
                    or folded in Path(source["path"]).name.casefold()
                ]
                for source in matches:
                    if not any(
                        str(Path(existing["path"]).resolve()).casefold()
                        == str(Path(source["path"]).resolve()).casefold()
                        for existing in selected
                    ):
                        selected.append(source)
            if not selected:
                available = ", ".join(
                    str(source.get("book_code") or source.get("title") or Path(source["path"]).name)
                    for source in sources[:30]
                )
                raise ValueError(f"未找到指定教材：{', '.join(requested)}。可用教材：{available or '无'}")
        else:
            if len(sources) > 5:
                raise ValueError(
                    f"当前范围有 {len(sources)} 本教材；请先调用 list_textbooks，"
                    "再用 textbook_refs 选择至多 5 本候选教材，避免无目的处理全部教材。"
                )
            selected = sources
        selected_paths = [str(Path(source["path"]).resolve()) for source in selected]
        self.ensure_current()
        baseline_without_selected_caches = self.content_source_signature(
            ignore_complete_cache_paths=selected_paths
        )
        expanded_extraction_paths: list[str] = []
        for source in selected:
            path = Path(source["path"]).resolve()
            complete_cache = _pdf_cache_path(path, ocr_page_limit=None)
            if complete_cache.is_file():
                continue
            _pdf_pages(path, ocr_page_limit=None)
            expanded_extraction_paths.append(str(path))
        if expanded_extraction_paths:
            if (
                self.content_source_signature(
                    ignore_complete_cache_paths=selected_paths
                )
                != baseline_without_selected_caches
            ):
                # Another source changed during extraction; a full consistency
                # refresh is safer than publishing a partial signature.
                self.ensure_current()
            else:
                self.refresh_textbook_documents(expanded_extraction_paths)
        result = self.search(
            query,
            kinds=["textbook_pdf"],
            subject_name=subject_name,
            paths=selected_paths,
            limit=limit,
        )
        metadata_by_path = {
            str(Path(source["path"]).resolve()).casefold(): {
                "subject_name": str(source.get("subject_name") or ""),
                "book_code": str(source.get("book_code") or ""),
                "title": str(source.get("title") or ""),
            }
            for source in selected
        }
        for item in result.get("results") or []:
            item["textbook"] = metadata_by_path.get(
                str(Path(str(item.get("path") or "")).resolve()).casefold(), {}
            )
        return {
            **result,
            "selected_textbooks": list(metadata_by_path.values()),
            "selected_textbook_count": len(metadata_by_path),
            "full_extraction_built_for": expanded_extraction_paths,
            "loading_policy": "Only the returned page snippets are intended for model context; use precise page reading only when needed.",
        }
