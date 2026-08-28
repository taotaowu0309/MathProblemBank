from __future__ import annotations

import json
import difflib
import hashlib
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_compute import LocalComputeManager
from shared.scripts.ai_agent_lean import LeanCheckManager
from shared.scripts.ai_agent_operation_registry import (
    AI_OPERATION_REGISTRY,
    DERIVED_WRITE,
    DESTRUCTIVE,
    FORMAL_WRITE,
    OperationSpec,
)
from shared.scripts.ai_agent_reference_materials import AiReferenceMaterialRegistry
from shared.scripts.ai_agent_papers import AcademicPaperAccessor
from shared.scripts.ai_agent_resources import ReadOnlyResourceAccessor
from shared.scripts.ai_agent_semantic_index import SemanticIndex
from shared.scripts.ai_agent_reliability import OperationJournal
from shared.scripts.ai_agent_tex_editor import ProjectTexEditor
from shared.scripts.ai_agent_visual_validation import validate_math_figure, validate_pdf_near_text
from shared.scripts.ai_agent_workspace import DEFAULT_MATH_WORKSPACE, DEFAULT_PHYSICS_WORKSPACE, MathWorkspaceEditor
from shared.scripts.ai_agent_workspace_tools import WorkspaceToolManager
from shared.scripts.online_course_service import COURSE_STORAGE_ROOT, OnlineCourseService
from shared.scripts.study_project_service import DEFAULT_SUBJECTS, SUBJECTS_PATH, subject_to_runtime
from shared.scripts.vocabulary_manager import VocabularyManager, workspace_vocabulary_paths
from shared.scripts.english_learning_service import EnglishLearningService


ROOT_DIR = APP_PATHS.application_root
READABLE_PROJECT_SUFFIXES = {".tex", ".txt", ".md", ".json", ".bib", ".sty", ".cls"}

MATRIX_SCHEMA = {
    "type": "array",
    "items": {
        "type": "array",
        "items": {"type": ["string", "number"]},
        "minItems": 1,
        "maxItems": 200,
    },
    "minItems": 1,
    "maxItems": 200,
}
VARIABLE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "domain": {"type": "string", "enum": ["unspecified", "real", "complex", "integer"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "maxItems": 20,
}
ASSUMPTIONS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "variable": {"type": "string"},
            "property": {"type": "string", "enum": ["positive", "negative", "nonzero", "real", "integer"]},
            "left": {"type": ["string", "number"]},
            "relation": {"type": "string", "enum": ["=", "==", "!=", ">", ">=", "<", "<="]},
            "right": {"type": ["string", "number"]},
        },
        "additionalProperties": False,
    },
    "maxItems": 20,
}


def _qid(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _table_columns(connection: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(connection, name):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_qid(name)})")}


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n...[已截断 {len(text) - limit} 个字符]"


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


class GlobalProblemRepository:
    """Read-only access to every enabled subject, project, and standard problem."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = Path(root_dir or ROOT_DIR).resolve()
        self.user_data_root = (
            APP_PATHS.vocabulary_root if root_dir is None else self.root_dir
        )
        self.vocabulary_workspace = "math"

    def set_vocabulary_workspace(self, workspace: str) -> None:
        self.vocabulary_workspace = str(workspace or "math").strip().lower()

    @property
    def vocabulary_database(self) -> Path:
        database, _backups, _exports = workspace_vocabulary_paths(
            self.vocabulary_workspace,
            root_dir=self.user_data_root,
        )
        return database

    def _registry(self) -> dict[str, dict[str, Any]]:
        if not SUBJECTS_PATH.is_file():
            return {name: dict(raw) for name, raw in DEFAULT_SUBJECTS.items()}
        try:
            parsed = json.loads(SUBJECTS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {name: dict(raw) for name, raw in DEFAULT_SUBJECTS.items()}
        return {
            str(name): raw
            for name, raw in parsed.items()
            if isinstance(raw, dict)
        } if isinstance(parsed, dict) else {}

    def subject_configs(self) -> dict[str, dict[str, Path]]:
        registry = self._registry()
        return {
            name: subject_to_runtime(raw)
            for name, raw in registry.items()
            if bool(raw.get("enabled", True))
        }

    def _connect_path(self, path: Path) -> sqlite3.Connection:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"数据库不存在：{resolved}")
        connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _connect_subject(self, subject_name: str) -> sqlite3.Connection:
        configs = self.subject_configs()
        if subject_name not in configs:
            raise ValueError(f"不存在或未启用的学科：{subject_name}")
        return self._connect_path(configs[subject_name]["db"])

    def _subject_problem_count(self, subject_name: str) -> int:
        try:
            with closing(self._connect_subject(subject_name)) as connection:
                if not _table_exists(connection, "canonical_problems"):
                    return 0
                return int(connection.execute("SELECT COUNT(*) FROM canonical_problems").fetchone()[0])
        except (OSError, sqlite3.Error):
            return 0

    def _subject_project_count(self, subject_name: str) -> int:
        try:
            with closing(self._connect_subject(subject_name)) as connection:
                if not _table_exists(connection, "problem_collections"):
                    return 0
                return int(connection.execute("SELECT COUNT(*) FROM problem_collections").fetchone()[0])
        except (OSError, sqlite3.Error):
            return 0

    def _subject_textbook_count(self, subject_name: str) -> int:
        try:
            with closing(self._connect_subject(subject_name)) as connection:
                if not _table_exists(connection, "books"):
                    return 0
                return int(connection.execute("SELECT COUNT(*) FROM books").fetchone()[0])
        except (OSError, sqlite3.Error):
            return 0

    def list_subjects(self) -> dict[str, Any]:
        subjects = []
        registry = self._registry()
        for name in self.subject_configs():
            raw = registry.get(name, {})
            domain = str(raw.get("domain") or "").strip().lower()
            if not domain:
                folder = str(raw.get("folder") or raw.get("folder_name") or "")
                domain = "physics" if folder.startswith(("Physics/", "Physics\\")) else "math"
            subjects.append(
                {
                    "subject_name": name,
                    "domain": domain,
                    "problem_count": self._subject_problem_count(name),
                    "project_count": self._subject_project_count(name),
                    "textbook_count": self._subject_textbook_count(name),
                }
            )
        return {"subjects": subjects, "subject_count": len(subjects)}

    def library_overview(self) -> dict[str, Any]:
        subject_data = self.list_subjects()
        return {
            **subject_data,
            "problem_count": sum(int(item["problem_count"]) for item in subject_data["subjects"]),
            "project_count": sum(int(item["project_count"]) for item in subject_data["subjects"]),
            "textbook_count": sum(int(item["textbook_count"]) for item in subject_data["subjects"]),
            "access_mode": "read_only_except_explicit_project_tex_edits",
        }

    def list_projects(self, subject_name: str = "") -> dict[str, Any]:
        configs = self.subject_configs()
        names = [subject_name] if subject_name else list(configs)
        projects: list[dict[str, Any]] = []
        for name in names:
            if name not in configs:
                raise ValueError(f"不存在或未启用的学科：{name}")
            with closing(self._connect_subject(name)) as connection:
                if not _table_exists(connection, "problem_collections"):
                    continue
                item_table = _table_exists(connection, "collection_items")
                count_select = (
                    "(SELECT COUNT(*) FROM collection_items ci "
                    "WHERE ci.collection_id=pc.id AND COALESCE(ci.included,1)=1)"
                    if item_table
                    else "0"
                )
                rows = connection.execute(
                    f"""
                    SELECT pc.*, {count_select} AS problem_count
                    FROM problem_collections pc
                    ORDER BY pc.updated_at DESC, pc.id DESC
                    """
                ).fetchall()
                for row in rows:
                    projects.append(
                        {
                            "subject_name": name,
                            "project_id": int(row["id"]),
                            "project_code": str(row["collection_code"] or ""),
                            "name": str(row["name"] or ""),
                            "project_type": str(row["collection_type"] or ""),
                            "description": _clip(row["description"] if "description" in row.keys() else "", 1200),
                            "problem_count": int(row["problem_count"] or 0),
                        }
                    )
        return {"projects": projects, "project_count": len(projects)}

    def list_textbooks(
        self,
        subject_name: str = "",
        query: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a metadata-only catalogue of registered textbooks.

        PDF contents deliberately stay out of this result.  The model can use
        the small catalogue to choose one or a few books before asking the
        segmented textbook dataset for relevant passages.
        """

        configs = self.subject_configs()
        names = [subject_name] if subject_name else list(configs)
        unknown = [name for name in names if name not in configs]
        if unknown:
            raise ValueError(f"不存在或未启用的学科：{', '.join(unknown)}")
        limit = max(1, min(int(limit), 200))
        folded_query = re.sub(r"\s+", " ", str(query or "").strip()).casefold()
        textbooks: list[dict[str, Any]] = []
        for name in names:
            with closing(self._connect_subject(name)) as connection:
                if not _table_exists(connection, "books"):
                    continue
                rows = connection.execute("SELECT * FROM books ORDER BY book_code, id").fetchall()
                for row in rows:
                    keys = set(row.keys())
                    pdf_path = str(row["pdf_path"] or "").strip() if "pdf_path" in keys else ""
                    path = Path(pdf_path).expanduser() if pdf_path else None
                    searchable = "\n".join(
                        str(row[key] or "")
                        for key in (
                            "book_code",
                            "title",
                            "author",
                            "edition",
                            "publisher",
                            "publication_year",
                            "notes",
                        )
                        if key in keys
                    ).casefold()
                    if folded_query and folded_query not in searchable:
                        query_terms = [term for term in folded_query.split(" ") if term]
                        if not query_terms or not all(term in searchable for term in query_terms):
                            continue
                    textbooks.append(
                        {
                            "subject_name": name,
                            "book_id": int(row["id"]),
                            "book_code": str(row["book_code"] or ""),
                            "title": str(row["title"] or ""),
                            "author": str(row["author"] or "") if "author" in keys else "",
                            "edition": str(row["edition"] or "") if "edition" in keys else "",
                            "publisher": str(row["publisher"] or "") if "publisher" in keys else "",
                            "publication_year": row["publication_year"] if "publication_year" in keys else None,
                            "notes": _clip(row["notes"] if "notes" in keys else "", 800),
                            "pdf_path": str(path.resolve()) if path and path.is_file() else pdf_path,
                            "pdf_available": bool(path and path.is_file() and path.suffix.casefold() == ".pdf"),
                        }
                    )
        textbooks.sort(
            key=lambda item: (
                str(item["subject_name"]).casefold(),
                str(item["book_code"]).casefold(),
                int(item["book_id"]),
            )
        )
        returned = textbooks[:limit]
        return {
            "query": str(query or ""),
            "searched_subjects": names,
            "textbooks": returned,
            "matched_count": len(textbooks),
            "returned_count": len(returned),
            "content_included": False,
            "next_step": "Only call search_textbook_content for one or a few relevant book_code values when textbook evidence is needed.",
        }

    def rebind_textbook_pdf(
        self,
        subject_name: str,
        book_ref: int | str,
        pdf_path: str,
    ) -> dict[str, Any]:
        configs = self.subject_configs()
        if subject_name not in configs:
            raise ValueError(f"不存在或未启用的学科：{subject_name}")
        path = Path(str(pdf_path or "")).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"PDF 文件不存在：{path}")
        if path.suffix.casefold() != ".pdf":
            raise ValueError("重新绑定教材时必须选择 PDF 文件。")
        path = path.resolve()
        config = configs[subject_name]
        database = Path(config["db"])
        folded = str(book_ref or "").strip().casefold()
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            if not _table_exists(connection, "books") or "pdf_path" not in _table_columns(connection, "books"):
                raise RuntimeError("当前教材表缺少可写的 pdf_path 字段。")
            rows = connection.execute(
                "SELECT id,book_code,title,pdf_path FROM books ORDER BY book_code,id"
            ).fetchall()
            matches = [
                row
                for row in rows
                if folded in {str(row["id"]).casefold(), str(row["book_code"] or "").casefold()}
            ]
        if len(matches) != 1:
            raise ValueError(f"没有唯一找到教材：{subject_name} / {book_ref}")
        selected = matches[0]
        old_path = str(selected["pdf_path"] or "").strip()
        if old_path and Path(old_path).expanduser().resolve() == path:
            return {
                "changed": False,
                "subject_name": subject_name,
                "book_id": int(selected["id"]),
                "book_code": str(selected["book_code"] or ""),
                "title": str(selected["title"] or ""),
                "pdf_path": str(path),
                "backup_path": "",
                "readback_verified": True,
            }
        backup_dir = Path(config.get("backups") or database.parent.parent / "backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        backup_path = backup_dir / f"{database.stem}_before_ai_rebind_textbook_pdf_{stamp}.db"
        shutil.copy2(database, backup_path)
        try:
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE books SET pdf_path=? WHERE id=?",
                    (str(path), int(selected["id"])),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("没有更新到目标教材。")
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
                if integrity != "ok" or foreign_keys:
                    raise RuntimeError(
                        f"教材数据库写后检查失败：integrity={integrity}, foreign_keys={foreign_keys[:5]}"
                    )
                connection.commit()
        except Exception:
            shutil.copy2(backup_path, database)
            raise
        with closing(sqlite3.connect(database)) as connection:
            readback = connection.execute(
                "SELECT pdf_path FROM books WHERE id=?", (int(selected["id"]),)
            ).fetchone()
        verified = bool(readback and Path(str(readback[0])).resolve() == path)
        if not verified:
            shutil.copy2(backup_path, database)
            raise RuntimeError("教材 PDF 绑定写后回读不一致，已经从备份恢复。")
        return {
            "changed": True,
            "subject_name": subject_name,
            "book_id": int(selected["id"]),
            "book_code": str(selected["book_code"] or ""),
            "title": str(selected["title"] or ""),
            "old_pdf_path": old_path,
            "pdf_path": str(path),
            "backup_path": str(backup_path.resolve()),
            "integrity_check": "ok",
            "foreign_key_check": "ok",
            "readback_verified": True,
        }

    def vocabulary_matches(self, text: str, limit: int = 40) -> list[dict[str, str]]:
        """Return local English-Chinese glossary entries that occur in text."""

        content = str(text or "")[:160000]
        if not content.strip():
            return []
        vocabulary_path = self.vocabulary_database
        if not vocabulary_path.is_file():
            return []
        try:
            with closing(self._connect_path(vocabulary_path)) as connection:
                if not _table_exists(connection, "vocabulary_entries"):
                    return []
                columns = _table_columns(connection, "vocabulary_entries")
                part_of_speech = (
                    "part_of_speech" if "part_of_speech" in columns else "'' AS part_of_speech"
                )
                rows = connection.execute(
                    f"""
                    SELECT term, definition, {part_of_speech}
                    FROM vocabulary_entries
                    WHERE TRIM(term) <> '' AND TRIM(definition) <> ''
                    ORDER BY LENGTH(term) DESC, term COLLATE NOCASE
                    """
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []

        folded = content.casefold()
        matched: list[tuple[int, int, dict[str, str]]] = []
        for row in rows:
            term = str(row["term"] or "").strip()
            if not term or not re.search(r"[A-Za-z]", term):
                continue
            escaped = re.escape(term.casefold()).replace(r"\ ", r"\s+")
            found = re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", folded)
            if found is None:
                continue
            matched.append(
                (
                    int(found.start()),
                    -len(term),
                    {
                        "term": term,
                        "part_of_speech": str(row["part_of_speech"] or "").strip(),
                        "definition": _clip(row["definition"], 500),
                    },
                )
            )
        matched.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in matched[: max(1, min(int(limit), 80))]]

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        normalized = re.sub(r"[\s,，。；;、/|]+", " ", str(query or "").strip())
        tokens = [token for token in normalized.split(" ") if token]
        if normalized and normalized not in tokens:
            tokens.insert(0, normalized)
        unique: list[str] = []
        for token in tokens:
            folded = token.casefold()
            if folded not in {item.casefold() for item in unique}:
                unique.append(token)
        return unique[:8]

    @staticmethod
    def _problem_payload(subject_name: str, row: sqlite3.Row, *, detail: bool) -> dict[str, Any]:
        keys = set(row.keys())
        payload: dict[str, Any] = {
            "subject_name": subject_name,
            "problem_id": int(row["id"]),
            "problem_code": str(row["problem_code"] or "") if "problem_code" in keys else "",
            "title": str(row["title"] or "") if "title" in keys else "",
            "chapter_code": str(row["chapter_code"] or "") if "chapter_code" in keys else "",
            "chapter_name": str(row["chapter_name"] or "") if "chapter_name" in keys else "",
            "section_code": str(row["section_code"] or "") if "section_code" in keys else "",
            "section_name": str(row["section_name"] or "") if "section_name" in keys else "",
            "main_method": _clip(row["main_method"] if "main_method" in keys else "", 1500),
            "summary_tex": _clip(row["summary_tex"] if "summary_tex" in keys else "", 3500),
        }
        if detail:
            payload.update(
                {
                    "statement_tex": _clip(row["statement_tex"] if "statement_tex" in keys else "", 18000),
                    "solution_tex": _clip(row["solution_tex"] if "solution_tex" in keys else "", 26000),
                    "notes": _clip(row["notes"] if "notes" in keys else "", 6000),
                    "difficulty": row["difficulty"] if "difficulty" in keys else None,
                    "solution_status": str(row["solution_status"] or "") if "solution_status" in keys else "",
                    "mastery_status": str(row["mastery_status"] or "") if "mastery_status" in keys else "",
                }
            )
        return payload

    def search_problems(
        self,
        query: str,
        subject_names: Iterable[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("搜索关键词不能为空。")
        limit = max(1, min(int(limit), 40))
        configs = self.subject_configs()
        requested = [str(name) for name in (subject_names or []) if str(name).strip()]
        names = requested or list(configs)
        unknown = [name for name in names if name not in configs]
        if unknown:
            raise ValueError(f"不存在或未启用的学科：{', '.join(unknown)}")
        tokens = self._query_tokens(query)
        scored: list[tuple[int, dict[str, Any]]] = []
        weights = {
            "problem_code": 20,
            "title": 12,
            "summary_tex": 10,
            "main_method": 9,
            "chapter_name": 5,
            "section_name": 5,
            "statement_tex": 4,
            "solution_tex": 3,
            "notes": 2,
        }
        for name in names:
            with closing(self._connect_subject(name)) as connection:
                columns = _table_columns(connection, "canonical_problems")
                search_columns = [column for column in weights if column in columns]
                if not search_columns:
                    continue
                clauses: list[str] = []
                args: list[str] = []
                for token in tokens:
                    pattern = f"%{token}%"
                    clauses.append("(" + " OR ".join(f"COALESCE({_qid(column)},'') LIKE ?" for column in search_columns) + ")")
                    args.extend([pattern] * len(search_columns))
                rows = connection.execute(
                    f"SELECT * FROM canonical_problems WHERE {' OR '.join(clauses)} LIMIT ?",
                    [*args, max(limit * 4, 80)],
                ).fetchall()
                for row in rows:
                    score = 0
                    for column in search_columns:
                        value = str(row[column] or "").casefold()
                        for token in tokens:
                            folded = token.casefold()
                            if folded and folded in value:
                                score += weights[column]
                                if value.strip() == folded:
                                    score += weights[column]
                    payload = self._problem_payload(name, row, detail=False)
                    payload["match_score"] = score
                    scored.append((score, payload))
        scored.sort(key=lambda item: (-item[0], item[1]["subject_name"], item[1]["problem_code"]))
        results = [payload for _score, payload in scored[:limit]]
        return {"query": query, "results": results, "result_count": len(results), "searched_subjects": names}

    def list_subject_problems(self, subject_name: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        with closing(self._connect_subject(subject_name)) as connection:
            if not _table_exists(connection, "canonical_problems"):
                return {
                    "subject_name": subject_name,
                    "problems": [],
                    "total_count": 0,
                    "offset": offset,
                    "returned_count": 0,
                }
            total = int(connection.execute("SELECT COUNT(*) FROM canonical_problems").fetchone()[0])
            rows = connection.execute(
                """
                SELECT * FROM canonical_problems
                ORDER BY chapter_code, section_code, problem_order, id
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return {
            "subject_name": subject_name,
            "problems": [self._problem_payload(subject_name, row, detail=False) for row in rows],
            "total_count": total,
            "offset": offset,
            "returned_count": len(rows),
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def resolve_problem_reference(
        self,
        hint: str,
        subject_name: str = "",
        project_ref: str | int = "",
        limit: int = 6,
    ) -> dict[str, Any]:
        """Resolve natural references such as 'section 1.1's very long problem'."""
        hint = re.sub(r"\s+", " ", str(hint or "")).strip()
        if not hint:
            raise ValueError("题目线索不能为空。")
        limit = max(1, min(int(limit), 12))
        configs = self.subject_configs()
        names = [str(subject_name)] if str(subject_name).strip() else list(configs)
        unknown = [name for name in names if name not in configs]
        if unknown:
            raise ValueError(f"不存在或未启用的学科：{', '.join(unknown)}")
        tokens = [
            token
            for token in self._query_tokens(hint)
            if token not in {"那道题", "这道题", "一个题", "问题", "题目", "很长", "最长", "详细", "复杂"}
        ]
        section_hints = re.findall(r"\d+(?:\.\d+)+", hint)
        section_number_hints = [
            tuple(int(part) for part in section.split(".")) for section in section_hints
        ]
        prefer_long = bool(re.search(r"最长|很长|长题|详细过程|解答很长|复杂", hint))
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for name in names:
            with closing(self._connect_subject(name)) as connection:
                if not _table_exists(connection, "canonical_problems"):
                    continue
                if str(project_ref).strip():
                    project = self._resolve_project(connection, project_ref)
                    if not _table_exists(connection, "collection_items"):
                        rows: list[sqlite3.Row] = []
                    else:
                        rows = connection.execute(
                            """
                            SELECT cp.*
                            FROM collection_items ci
                            JOIN canonical_problems cp ON cp.id=ci.canonical_problem_id
                            WHERE ci.collection_id=? AND COALESCE(ci.included,1)=1
                            ORDER BY ci.item_order, ci.id
                            """,
                            (int(project["id"]),),
                        ).fetchall()
                else:
                    rows = connection.execute("SELECT * FROM canonical_problems ORDER BY id").fetchall()
                for row in rows:
                    keys = set(row.keys())
                    location_fields = [
                        str(row[key] or "").casefold()
                        for key in ("chapter_code", "chapter_name", "section_code", "section_name")
                        if key in keys
                    ]
                    location_number_fields = [
                        tuple(int(part) for part in re.findall(r"\d+", field))
                        for field in location_fields
                    ]
                    section_matches = [
                        numbers
                        for numbers in section_number_hints
                        if numbers
                        and any(
                            field_numbers[index : index + len(numbers)] == numbers
                            for field_numbers in location_number_fields
                            for index in range(max(0, len(field_numbers) - len(numbers) + 1))
                        )
                    ]
                    if section_hints and not section_matches:
                        continue
                    searchable = " ".join(
                        str(row[key] or "")
                        for key in (
                            "problem_code",
                            "title",
                            "chapter_code",
                            "chapter_name",
                            "section_code",
                            "section_name",
                            "summary_tex",
                            "main_method",
                        )
                        if key in keys
                    ).casefold()
                    text_score = sum(12 if token.casefold() in searchable else 0 for token in tokens)
                    content_chars = sum(
                        len(str(row[key] or ""))
                        for key in ("statement_tex", "solution_tex")
                        if key in keys
                    )
                    location_score = 30 * len(section_matches)
                    long_score = min(content_chars // 400, 80) if prefer_long else 0
                    if tokens and text_score == 0 and not section_hints and not prefer_long:
                        continue
                    payload = self._problem_payload(name, row, detail=False)
                    payload["content_chars"] = content_chars
                    payload["reference_score"] = text_score + location_score + long_score
                    scored.append((payload["reference_score"], content_chars, payload))
        scored.sort(
            key=lambda item: (-item[0], -item[1], item[2]["subject_name"], item[2]["problem_code"])
        )
        results = [payload for _score, _length, payload in scored[:limit]]
        return {
            "hint": hint,
            "results": results,
            "result_count": len(results),
            "searched_subjects": names,
            "project_ref": str(project_ref or ""),
        }

    def get_problem(self, subject_name: str, problem_ref: str | int) -> dict[str, Any]:
        with closing(self._connect_subject(subject_name)) as connection:
            if not _table_exists(connection, "canonical_problems"):
                raise ValueError(f"学科“{subject_name}”没有标准题库。")
            text = str(problem_ref).strip()
            if text.isdigit():
                row = connection.execute(
                    "SELECT * FROM canonical_problems WHERE id=? OR problem_code=? COLLATE NOCASE LIMIT 1",
                    (int(text), text),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM canonical_problems WHERE problem_code=? COLLATE NOCASE LIMIT 1",
                    (text,),
                ).fetchone()
            if row is None:
                raise ValueError(f"未找到题目：{subject_name} / {problem_ref}")
            payload = self._problem_payload(subject_name, row, detail=True)
            payload["textbook_sources"] = self._problem_textbook_sources(connection, int(row["id"]))
            return payload

    @staticmethod
    def _problem_textbook_sources(connection: sqlite3.Connection, problem_id: int) -> list[dict[str, Any]]:
        required = {"source_mappings", "source_problems", "books"}
        if not all(_table_exists(connection, table) for table in required):
            return []
        rows = connection.execute(
            """
            SELECT
                b.book_code,
                b.title AS book_title,
                b.author,
                b.edition,
                b.pdf_path,
                sp.volume,
                sp.chapter,
                sp.section,
                sp.problem_number,
                sp.page,
                sp.original_statement_tex,
                sp.notes,
                sm.relation_type,
                sm.relation_note
            FROM source_mappings sm
            JOIN source_problems sp ON sp.id=sm.source_problem_id
            JOIN books b ON b.id=sp.book_id
            WHERE sm.canonical_problem_id=?
            ORDER BY b.book_code, sp.volume, sp.chapter, sp.section, sp.problem_number, sp.id
            """,
            (int(problem_id),),
        ).fetchall()
        return [
            {
                "book_code": str(row["book_code"] or ""),
                "book_title": str(row["book_title"] or ""),
                "author": str(row["author"] or ""),
                "edition": str(row["edition"] or ""),
                "pdf_path": str(row["pdf_path"] or ""),
                "volume": str(row["volume"] or ""),
                "chapter": str(row["chapter"] or ""),
                "section": str(row["section"] or ""),
                "problem_number": str(row["problem_number"] or ""),
                "page": str(row["page"] or ""),
                "original_statement_tex": _clip(row["original_statement_tex"], 12000),
                "notes": _clip(row["notes"], 2000),
                "relation_type": str(row["relation_type"] or ""),
                "relation_note": _clip(row["relation_note"], 2000),
            }
            for row in rows
        ]

    def _resolve_project(self, connection: sqlite3.Connection, project_ref: str | int) -> sqlite3.Row:
        if not _table_exists(connection, "problem_collections"):
            raise ValueError("该学科尚无学习项目。")
        text = str(project_ref).strip()
        if text.isdigit():
            row = connection.execute(
                "SELECT * FROM problem_collections WHERE id=? OR collection_code=? COLLATE NOCASE LIMIT 1",
                (int(text), text),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM problem_collections WHERE collection_code=? COLLATE NOCASE OR name=? COLLATE NOCASE LIMIT 1",
                (text, text),
            ).fetchone()
        if row is None:
            raise ValueError(f"未找到学习项目：{project_ref}")
        return row

    def get_project_problems(self, subject_name: str, project_ref: str | int, limit: int = 40) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        with closing(self._connect_subject(subject_name)) as connection:
            project = self._resolve_project(connection, project_ref)
            if not _table_exists(connection, "collection_items") or not _table_exists(connection, "canonical_problems"):
                rows: list[sqlite3.Row] = []
            else:
                rows = connection.execute(
                    """
                    SELECT cp.*
                    FROM collection_items ci
                    JOIN canonical_problems cp ON cp.id=ci.canonical_problem_id
                    WHERE ci.collection_id=? AND COALESCE(ci.included,1)=1
                    ORDER BY ci.item_order, ci.id
                    LIMIT ?
                    """,
                    (int(project["id"]), limit),
                ).fetchall()
        return {
            "subject_name": subject_name,
            "project_id": int(project["id"]),
            "project_code": str(project["collection_code"] or ""),
            "project_name": str(project["name"] or ""),
            "problems": [self._problem_payload(subject_name, row, detail=False) for row in rows],
            "returned_count": len(rows),
        }

    def project_reference_context(self, subject_name: str, project_ref: str | int) -> dict[str, Any]:
        """Return the local project/PDF/textbook boundary used by the tutor."""
        with closing(self._connect_subject(subject_name)) as connection:
            project = self._resolve_project(connection, project_ref)
            project_id = int(project["id"])
            book_ids: list[int] = []
            if "book_id" in project.keys() and project["book_id"] is not None:
                book_ids.append(int(project["book_id"]))
            if _table_exists(connection, "collection_books"):
                book_ids.extend(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT book_id FROM collection_books WHERE collection_id=? ORDER BY book_id",
                        (project_id,),
                    ).fetchall()
                )
            book_ids = list(dict.fromkeys(book_ids))
            books: list[dict[str, Any]] = []
            if book_ids and _table_exists(connection, "books"):
                placeholders = ",".join("?" for _ in book_ids)
                rows = connection.execute(
                    f"SELECT * FROM books WHERE id IN ({placeholders}) ORDER BY book_code, id",
                    book_ids,
                ).fetchall()
                books = [
                    {
                        "book_id": int(row["id"]),
                        "book_code": str(row["book_code"] or ""),
                        "title": str(row["title"] or ""),
                        "author": str(row["author"] or ""),
                        "edition": str(row["edition"] or ""),
                        "pdf_path": str(row["pdf_path"] or ""),
                    }
                    for row in rows
                ]
            project_dir = self.subject_configs()[subject_name]["folder"] / "collections" / str(project["collection_code"])
            pdf_filename = str(project["pdf_filename"] or "") if "pdf_filename" in project.keys() else ""
            return {
                "subject_name": subject_name,
                "project_id": project_id,
                "project_code": str(project["collection_code"] or ""),
                "project_name": str(project["name"] or ""),
                "project_description": _clip(project["description"] if "description" in project.keys() else "", 2000),
                "project_pdf_path": str((project_dir / pdf_filename).resolve()) if pdf_filename else "",
                "bound_textbooks": books,
                "scope_rule": "The current project and its bound textbooks are the highest-priority local sources, not a read boundary. Expand across subjects, projects, textbooks, workspace files, reference libraries, or history when the user's request requires it.",
            }

    def reference_scope_problem_ids(self, subject_name: str, project_ref: str | int) -> set[int]:
        """Problem ids in the current project or mapped to one of its bound textbooks."""
        with closing(self._connect_subject(subject_name)) as connection:
            project = self._resolve_project(connection, project_ref)
            project_id = int(project["id"])
            allowed: set[int] = set()
            if _table_exists(connection, "collection_items"):
                allowed.update(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT canonical_problem_id FROM collection_items WHERE collection_id=? AND COALESCE(included,1)=1",
                        (project_id,),
                    ).fetchall()
                )
            book_ids: set[int] = set()
            if "book_id" in project.keys() and project["book_id"] is not None:
                book_ids.add(int(project["book_id"]))
            if _table_exists(connection, "collection_books"):
                book_ids.update(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT book_id FROM collection_books WHERE collection_id=?",
                        (project_id,),
                    ).fetchall()
                )
            if book_ids and all(_table_exists(connection, table) for table in ("source_mappings", "source_problems")):
                placeholders = ",".join("?" for _ in book_ids)
                allowed.update(
                    int(row[0])
                    for row in connection.execute(
                        f"""
                        SELECT DISTINCT sm.canonical_problem_id
                        FROM source_mappings sm
                        JOIN source_problems sp ON sp.id=sm.source_problem_id
                        WHERE sp.book_id IN ({placeholders})
                        """,
                        sorted(book_ids),
                    ).fetchall()
                )
            return allowed

    def _project_directory(self, subject_name: str, project_ref: str | int) -> tuple[Path, sqlite3.Row]:
        configs = self.subject_configs()
        if subject_name not in configs:
            raise ValueError(f"不存在或未启用的学科：{subject_name}")
        with closing(self._connect_subject(subject_name)) as connection:
            project = self._resolve_project(connection, project_ref)
        project_dir = (configs[subject_name]["folder"] / "collections" / str(project["collection_code"])).resolve()
        subject_root = configs[subject_name]["folder"].resolve()
        if subject_root not in project_dir.parents or not project_dir.is_dir():
            raise ValueError(f"学习项目目录不存在：{project_dir}")
        return project_dir, project

    def list_project_files(self, subject_name: str, project_ref: str | int) -> dict[str, Any]:
        project_dir, project = self._project_directory(subject_name, project_ref)
        files = []
        for path in sorted(project_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in READABLE_PROJECT_SUFFIXES:
                continue
            resolved = path.resolve()
            if project_dir not in resolved.parents:
                continue
            files.append(
                {
                    "relative_path": resolved.relative_to(project_dir).as_posix(),
                    "size": resolved.stat().st_size,
                }
            )
            if len(files) >= 300:
                break
        return {
            "subject_name": subject_name,
            "project_code": str(project["collection_code"] or ""),
            "files": files,
            "returned_count": len(files),
        }

    def read_project_file(
        self,
        subject_name: str,
        project_ref: str | int,
        relative_path: str,
        max_chars: int = 50000,
    ) -> dict[str, Any]:
        project_dir, project = self._project_directory(subject_name, project_ref)
        relative = Path(str(relative_path or ""))
        if relative.is_absolute():
            raise ValueError("只能读取项目目录内的相对路径。")
        target = (project_dir / relative).resolve()
        if project_dir not in target.parents or not target.is_file():
            raise ValueError("文件不存在，或路径超出了当前项目目录。")
        if target.suffix.lower() not in READABLE_PROJECT_SUFFIXES:
            raise ValueError(f"不允许读取该文件类型：{target.suffix}")
        max_chars = max(1000, min(int(max_chars), 100000))
        text = target.read_text(encoding="utf-8", errors="replace")
        return {
            "subject_name": subject_name,
            "project_code": str(project["collection_code"] or ""),
            "relative_path": target.relative_to(project_dir).as_posix(),
            "content": _clip(text, max_chars),
            "total_chars": len(text),
        }


LEGACY_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "symbolic_math",
        "description": "使用隔离的本机 SymPy worker 进行受控精确计算；支持标量、矩阵、线性方程和公式核验。不执行任意 Python 代码，计算结果不能代替证明。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["simplify", "expand", "factor", "differentiate", "integrate", "limit", "solve", "equivalence", "determinant", "eigenvalues", "matrix_multiply", "matrix_inverse", "matrix_rank", "linear_solve"],
                },
                "expression": {"type": "string", "description": "SymPy 风格表达式；幂可写 ^ 或 **"},
                "variable": {"type": "string", "description": "变量名，默认 x"},
                "second_expression": {"type": "string", "description": "equivalence 操作使用"},
                "order": {"type": "integer", "minimum": 1, "maximum": 12},
                "lower": {"type": "string", "description": "定积分下限"},
                "upper": {"type": "string", "description": "定积分上限"},
                "point": {"type": "string", "description": "极限趋近点"},
                "direction": {"type": "string", "enum": ["+", "-", "+-"]},
                "matrix": MATRIX_SCHEMA,
                "second_matrix": MATRIX_SCHEMA,
                "rhs": {"type": "array", "items": {"type": ["string", "number"]}, "maxItems": 200},
                "variables": VARIABLE_SCHEMA,
                "domain": {"type": "string", "enum": ["unspecified", "real", "complex", "integer"]},
                "assumptions": ASSUMPTIONS_SCHEMA,
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "numerical_math",
        "description": "使用隔离的 SymPy/NumPy worker 进行受控高精度求值、数值积分、矩阵与误差计算；不执行任意代码。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["evaluate", "nsolve", "series", "numerical_integrate", "numerical_linear_solve", "numerical_eigenvalues", "numerical_matrix_multiply", "error_compare"]},
                "expression": {"type": "string"},
                "values": {"type": "object", "additionalProperties": {"type": ["string", "number"]}},
                "variable": {"type": "string"},
                "initial_guess": {"type": "number"},
                "order": {"type": "integer", "minimum": 2, "maximum": 30},
                "precision": {"type": "integer", "minimum": 15, "maximum": 80},
                "second_expression": {"type": "string"},
                "lower": {"type": "string"},
                "upper": {"type": "string"},
                "matrix": MATRIX_SCHEMA,
                "second_matrix": MATRIX_SCHEMA,
                "rhs": {"type": "array", "items": {"type": ["string", "number"]}, "maxItems": 200},
                "variables": VARIABLE_SCHEMA,
                "domain": {"type": "string", "enum": ["unspecified", "real", "complex", "integer"]},
                "assumptions": ASSUMPTIONS_SCHEMA,
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify_formula",
        "description": "先做精确符号核验，再用有界高精度采样寻找反例。找不到反例只返回不确定，绝不把有限采样声称为证明。",
        "parameters": {
            "type": "object",
            "properties": {
                "formula": {"type": "string", "description": "包含 =、!=、<、<=、> 或 >= 的公式"},
                "variables": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "ranges": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
            },
            "required": ["formula"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_counterexample",
        "description": "在用户指定的有限范围内确定性搜索等式或不等式反例；未找到反例明确标为不构成证明。",
        "parameters": {
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "variables": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "ranges": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                "samples_per_variable": {"type": "integer", "minimum": 3, "maximum": 41},
            },
            "required": ["claim"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mathematica_compute",
        "description": "通过本机 mma-mcp 安全过滤层调用 Mathematica 做结构化文本计算。仅用于计算、验证和寻找反例，不能代替数学证明；不得提交任意 Wolfram Language 程序。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["simplify", "factor", "solve", "reduce", "integrate", "limit", "sum", "series", "dsolve", "determinant", "eigenvalues", "numeric"]},
                "expression": {"type": "string"},
                "variable": {"type": "string"},
                "variables": VARIABLE_SCHEMA,
                "domain": {"type": "string", "enum": ["unspecified", "real", "complex", "integer"]},
                "assumptions": ASSUMPTIONS_SCHEMA,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "variable": {"type": "string"},
                        "dependent": {"type": "string"},
                        "lower": {"type": ["string", "number"]},
                        "upper": {"type": ["string", "number"]},
                        "point": {"type": ["string", "number"]},
                        "order": {"type": "integer", "minimum": 1, "maximum": 30},
                    },
                    "additionalProperties": False,
                },
                "precision": {"type": "integer", "minimum": 15, "maximum": 100},
            },
            "required": ["operation", "expression"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mathematica_plot",
        "description": "通过本机 mma-mcp 安全过滤层调用 Mathematica 原生绘图，支持二维显函数/参数曲线/隐式曲线/区域、三维显式或参数曲面、空间曲线、隐式曲面和二维向量场；生成 PNG、PDF、SVG、受控 WL 源码及元数据，不修改正式项目。",
        "parameters": {
            "type": "object",
            "properties": {
                "plot_type": {
                    "type": "string",
                    "enum": [
                        "explicit_2d", "parametric_2d", "implicit_2d", "region_2d",
                        "surface_3d", "parametric_curve_3d", "parametric_surface_3d",
                        "implicit_3d", "vector_field_2d"
                    ],
                },
                "expressions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
                "expression": {"type": "string"},
                "variable": {"type": "string"},
                "second_variable": {"type": "string"},
                "third_variable": {"type": "string"},
                "parameter": {"type": "string"},
                "second_parameter": {"type": "string"},
                "x_expression": {"type": "string"},
                "y_expression": {"type": "string"},
                "z_expression": {"type": "string"},
                "u_expression": {"type": "string"},
                "v_expression": {"type": "string"},
                "x_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "y_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "z_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "parameter_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "second_parameter_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "title": {"type": "string"},
                "x_label": {"type": "string"},
                "y_label": {"type": "string"},
                "z_label": {"type": "string"},
                "legend_labels": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "show_axes": {"type": "boolean"},
                "show_grid": {"type": "boolean"},
                "show_legend": {"type": "boolean"},
                "image_size": {"type": "integer", "minimum": 320, "maximum": 1600},
                "plot_points": {"type": "integer", "minimum": 20, "maximum": 400},
                "max_recursion": {"type": "integer", "minimum": 0, "maximum": 8},
                "mesh": {"type": "string", "enum": ["automatic", "none", "all"]},
                "theme": {"type": "string", "enum": ["default", "scientific", "classic", "monochrome"]},
            },
            "required": ["plot_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dual_verify_math",
        "description": "让 Python 与 Mathematica 独立计算同一标量或矩阵结果，并按数学对象进行等价、数值一致或无法判断的核验；绝不比较原始字符串，也不把有限采样当作证明。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["simplify", "factor", "integrate", "limit", "determinant", "eigenvalues"]},
                "expression": {"type": "string"},
                "variable": {"type": "string"},
                "variables": VARIABLE_SCHEMA,
                "domain": {"type": "string", "enum": ["unspecified", "real", "complex", "integer"]},
                "assumptions": ASSUMPTIONS_SCHEMA,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "variable": {"type": "string"},
                        "lower": {"type": ["string", "number"]},
                        "upper": {"type": ["string", "number"]},
                        "point": {"type": ["string", "number"]},
                    },
                    "additionalProperties": False,
                },
                "matrix": MATRIX_SCHEMA,
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "plot_math_function",
        "description": "使用隔离的 Python/Matplotlib Agg worker 绘制受控二维静态数学图形并生成 PNG、PDF、SVG。只接受结构化表达式、区间和有限样式；不能代替数学证明，也不修改正式项目。",
        "parameters": {
            "type": "object",
            "properties": {
                "plot_type": {"type": "string", "enum": ["explicit_2d", "parametric_2d", "points_2d"]},
                "expressions": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "variable": {"type": "string"},
                "parameter": {"type": "string"},
                "x_expression": {"type": "string"},
                "y_expression": {"type": "string"},
                "points": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                    "maxItems": 10000,
                },
                "x_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "y_range": {"type": ["array", "null"], "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "parameter_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "sample_count": {"type": "integer", "minimum": 100, "maximum": 5000},
                "title": {"type": "string"},
                "x_label": {"type": "string"},
                "y_label": {"type": "string"},
                "show_grid": {"type": "boolean"},
                "show_legend": {"type": "boolean"},
                "axis_mode": {"type": "string", "enum": ["standard", "equal", "origin"]},
                "styles": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "color": {"type": "string", "enum": ["blue", "orange", "green", "red", "purple", "brown", "pink", "gray", "olive", "cyan", "black"]},
                            "line_style": {"type": "string", "enum": ["solid", "dashed", "dotted", "dashdot"]},
                            "marker": {"type": "string", "enum": ["none", "circle", "square", "triangle", "cross", "plus"]},
                            "line_width": {"type": "number", "minimum": 0.6, "maximum": 4.0},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["plot_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "render_math_figure_preview",
        "description": "把完整 TikZ/PGFPlots 图形编译到应用临时缓存并自动检查空白、裁切、尺寸和标签重叠；不写入正式项目。适合用户要求试画或聊天内预览。",
        "parameters": {
            "type": "object",
            "properties": {
                "tikz_code": {"type": "string", "description": "完整 tikzpicture 环境，不得读取外部文件"},
                "caption": {"type": "string", "description": "可选的简短数学说明"}
            },
            "required": ["tikz_code"],
            "additionalProperties": False
        }
    },
    {
        "name": "validate_math_figure",
        "description": "渲染并检查本地 PDF 或图像的空白、裁切、分辨率、极端尺寸、文字块重叠及预期标签是否真正出现；返回局限说明。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "page_number": {"type": "integer", "minimum": 1},
                "expected_labels": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "semantic_search",
        "description": "在统一的本地语义索引中搜索标准题、教材映射、项目文件、用户配置的外部数学资料库和历史对话。适合自然语言概念、证明方法和模糊上下文检索；只返回少量带来源标识的片段。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["problem", "project_file", "textbook_pdf", "project_pdf", "math_workspace", "reference_library", "conversation"]},
                    "description": "可选文档类型；留空搜索全部",
                },
                "subject_name": {"type": "string"},
                "project_ref": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_available_project_tools",
        "description": "列出 AI 当前真正可以自主调用的题库、项目、文件、联网、TeX 写入和 PDF 生成功能，以及每项功能的读写权限。只读。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_provider_account_usage",
        "description": "读取本机可选 Provider 兼容层自动同步的账户余额、近 24 小时消耗、累计消耗、请求数和更新时间。只返回整理后的指标，不暴露 Cookie、网页登录令牌或 API Key。未安装兼容层时返回 unavailable。只读。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_library_overview",
        "description": "获取全部已启用学科、项目、教材和标准题的数量概览。只读。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_subjects",
        "description": "列出全部数学和物理学科以及每个学科的题目、项目数量。只读。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_projects",
        "description": "列出一个学科或全部学科中的学习项目。只读。",
        "parameters": {
            "type": "object",
            "properties": {"subject_name": {"type": "string", "description": "可选；留空表示全部学科"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_textbooks",
        "description": "只读取已登记教材的轻量目录信息（教材编号、书名、作者、版本和 PDF 是否可用），不读取教材正文。用于让 AI 先判断哪一本或哪几本教材可能相关；只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string", "description": "可选；留空表示全部学科"},
                "query": {"type": "string", "description": "可选的书名、作者、教材编号或版本线索"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_textbook_dataset_status",
        "description": "检查本机教材分段检索数据集是否已包含后来登记并绑定的教材 PDF；只返回教材、分段数和页码覆盖等状态，不返回正文。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string", "description": "可选；留空表示全部学科"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_textbook_content",
        "description": "只在确实需要教材证据时使用：从本机已自动分段的教材数据集中选择一本或少数几本教材，搜索少量相关页段。不会把整本书放入模型上下文；命中后如需核对原文，再读取精确页码。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要在教材中定位的概念、公式、证明方法或模糊记忆"},
                "textbook_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                    "description": "填写 list_textbooks 返回的一个或少数几个 book_code；候选超过 5 本时不可留空",
                },
                "subject_name": {"type": "string", "description": "可选学科范围"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_problems",
        "description": "跨全部题库搜索题号、标题、摘要、题干、解答、方法和备注，返回最相关候选题。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "数学概念、方法、题号、标题或 LaTeX 片段"},
                "subject_names": {"type": "array", "items": {"type": "string"}, "description": "可选的学科范围"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_problem_reference",
        "description": "根据自然语言线索定位题目，例如“微分流形 1.1 节那道很长的题”“刚才项目里解答最长的题”。可结合学科、项目、章节和题解长度返回少量候选；只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "hint": {"type": "string", "description": "用户对题目的自然语言描述"},
                "subject_name": {"type": "string", "description": "可选学科；当前上下文明确时应填写"},
                "project_ref": {"type": "string", "description": "可选项目编号、名称或数据库 ID"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["hint"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_problem",
        "description": "读取一道标准题的完整题干、解答、摘要、方法、备注及 LaTeX。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "problem_ref": {"type": "string", "description": "标准题编号或数据库 ID"},
            },
            "required": ["subject_name", "problem_ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_problem_evidence_batch",
        "description": "一次读取最多 8 道候选题的紧凑证据，返回题干、摘要、方法和可选题解摘录，避免逐题重复调用。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "problem_refs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {"type": "string"}
                },
                "include_solution": {"type": "boolean"},
                "max_chars_per_problem": {"type": "integer", "minimum": 800, "maximum": 6000}
            },
            "required": ["subject_name", "problem_refs"],
            "additionalProperties": False
        }
    },
    {
        "name": "get_project_problems",
        "description": "读取某个学习项目当前包含的标准题列表和摘要。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "project_ref": {"type": "string", "description": "项目编号、名称或数据库 ID"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["subject_name", "project_ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_project_files",
        "description": "列出学习项目目录中可读取的 LaTeX、Markdown、JSON、BibTeX 等文本文件。只读。",
        "parameters": {
            "type": "object",
            "properties": {"subject_name": {"type": "string"}, "project_ref": {"type": "string"}},
            "required": ["subject_name", "project_ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_project_file",
        "description": "读取学习项目目录内一个指定文本或 LaTeX 文件。路径被限制在项目目录内。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "project_ref": {"type": "string"},
                "relative_path": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
            },
            "required": ["subject_name", "project_ref", "relative_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_project_tex",
        "description": "按唯一定位文本修改学习项目内已有的 .tex 文件。仅在用户明确要求修改项目 TeX 时使用；写入前自动备份，写入后禁用 shell escape 增量生成并替换正式项目 PDF，任一步失败都会回滚。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "project_ref": {"type": "string", "description": "项目编号、名称或数据库 ID"},
                "relative_path": {"type": "string", "description": "项目目录内已有 .tex 文件的相对路径"},
                "operation": {"type": "string", "enum": ["insert_before", "insert_after", "replace"]},
                "anchor_text": {"type": "string", "description": "从 read_project_file 得到且在文件中只出现一次的精确原文"},
                "new_tex": {"type": "string", "description": "要插入或替换的 TeX 源码"},
            },
            "required": ["subject_name", "project_ref", "relative_path", "operation", "anchor_text", "new_tex"],
            "additionalProperties": False,
        },
    },
    {
        "name": "insert_tikz_figure",
        "description": "仅在用户明确要求写入项目时，在现有 .tex 文件的精确位置插入 TikZ 数学图形；单纯测试或预览不得写入。自动备份、增量生成并替换正式项目 PDF；任一步失败都会回滚。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "project_ref": {"type": "string"},
                "relative_path": {"type": "string"},
                "anchor_text": {"type": "string", "description": "目标文件中只出现一次的精确原文"},
                "position": {"type": "string", "enum": ["before", "after"]},
                "tikz_code": {"type": "string", "description": "完整的 tikzpicture 环境；不要包含完整 LaTeX 文档"},
                "caption": {"type": "string", "description": "可选图题；提供后自动包裹 figure 环境"},
                "label": {"type": "string", "description": "可选 LaTeX label"},
            },
            "required": ["subject_name", "project_ref", "relative_path", "anchor_text", "position", "tikz_code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "build_project_pdf",
        "description": "使用现有缓存快速增量生成指定学习项目的正式 PDF，并确认正式 PDF 文件已经被实际替换。适用于用户要求刷新、重新生成或核验项目 PDF；可能需要十几秒到数十秒。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "project_ref": {"type": "string", "description": "项目编号、名称或数据库 ID"},
            },
            "required": ["subject_name", "project_ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "discover_public_math_resources",
        "description": "为用户寻找可公开访问的数学资料。一次调用会在全球互联网中检索、按数学来源质量排序、实际打开候选网页或 PDF，并只返回已经核验可访问的资料。适合找讲义、教材、课程、论文、笔记或视频；不要用于简单定义和普通计算。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户想寻找的数学主题和资料要求"},
                "alternate_query": {
                    "type": "string",
                    "description": "准确的英文数学主题和资料类型关键词，中文主题通常应提供"
                },
                "resource_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["pdf", "course_or_notes", "paper", "book", "video", "web_page"]
                    },
                    "description": "可选资料类型；留空表示全部类型"
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 6}
            },
            "required": ["query"],
            "additionalProperties": False
        }
    },
    {
        "name": "search_math_papers",
        "description": "使用 arXiv 官方 API 与 Crossref REST API 检索数学、统计、理论计算机和物理论文；返回作者、摘要、年份、arXiv ID、DOI、期刊与开放全文状态。优先用于论文/期刊/arXiv 请求，不使用普通网页搜索代替。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "准确的英文论文主题、标题、作者或关键词"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["arxiv", "crossref"]},
                    "maxItems": 2,
                },
                "category": {
                    "type": "string",
                    "enum": ["all", "math", "statistics", "computer_science", "physics"],
                },
                "publication_type": {
                    "type": "string",
                    "enum": ["journal_article", "all"],
                    "description": "Crossref 默认只检索期刊论文；all 还包含专著章节等",
                },
                "year_from": {"type": "integer", "minimum": 1900, "maximum": 2100},
                "year_to": {"type": "integer", "minimum": 1900, "maximum": 2100},
                "sort": {"type": "string", "enum": ["relevance", "newest"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_math_paper",
        "description": "读取 search_math_papers 返回的论文，或直接读取 arXiv ID/DOI。arXiv 公开 PDF 会下载到本机缓存并按页提取正文；普通期刊 DOI 只读取 Crossref 元数据和摘要，不绕过付费墙。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "paper_id、arXiv ID、DOI 或本轮结果中的论文 URL"},
                "page_start": {"type": "integer", "minimum": 1},
                "page_end": {"type": "integer", "minimum": 1},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 120000},
            },
            "required": ["identifier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_search",
        "description": "搜索公开互联网，返回真实搜索结果的标题、摘要和原始 URL。数学问题需要社区解释时可优先指定 zhihu.com、math.stackexchange.com、mathoverflow.net 等域名；搜索摘要不能代替打开网页核验。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "与用户问题直接相关的搜索关键词"},
                "alternate_query": {
                    "type": "string",
                    "description": "可选的第二组搜索词。中文问题可填写准确的英文数学术语；简单问题不要为了凑数填写",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "preferred_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                    "description": "可选优先域名，例如 zhihu.com、math.stackexchange.com；不要包含路径"
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fetch_url",
        "description": "读取用户明确给出的公开网址，或本轮网页搜索返回的网页/在线 PDF；提取正文供分析。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整的 http/https URL"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 150000},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_workspace_tree",
        "description": "递归列出当前 MathProblemBank 正式仓库中的目录和文件。用于先确认真实入口与结构；只读，不扫描仓库外路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "仓库相对路径；留空表示仓库根目录"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 8},
                "limit": {"type": "integer", "minimum": 1, "maximum": 3000}
            },
            "additionalProperties": False
        }
    },
    {
        "name": "search_workspace_text",
        "description": "在当前正式仓库的文本和源代码正文中搜索，返回精确文件、行号、列号和片段。用于定位定义、调用方和测试；只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "extensions": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "case_sensitive": {"type": "boolean"}
            },
            "required": ["query"],
            "additionalProperties": False
        }
    },
    {
        "name": "read_workspace_files",
        "description": "一次按行读取最多 20 个仓库文本或代码文件，返回绝对路径、行号、内容和 SHA-256。修改前应先用它读取目标文件；只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line_start": {"type": "integer", "minimum": 1},
                            "line_end": {"type": "integer", "minimum": 1}
                        },
                        "required": ["path"], "additionalProperties": False
                    }
                },
                "max_total_chars": {"type": "integer", "minimum": 2000, "maximum": 500000}
            },
            "required": ["requests"],
            "additionalProperties": False
        }
    },
    {
        "name": "inspect_git_changes",
        "description": "读取当前正式仓库的 git status 和未暂存 diff，用于区分用户既有修改与本轮修改；不会暂存、提交或回退。",
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                "include_diff": {"type": "boolean"}
            },
            "additionalProperties": False
        }
    },
    {
        "name": "inspect_workspace_sqlite",
        "description": "以 SQLite 只读模式检查仓库内数据库的结构、哈希、完整性，并可执行一条 SELECT/WITH/安全 PRAGMA。不会写数据库。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500}
            },
            "required": ["path"],
            "additionalProperties": False
        }
    },
    {
        "name": "apply_workspace_patch",
        "description": "在用户明确要求实际修改后，对当前正式仓库最多 20 个已读取文本/代码文件执行一次带备份、原子替换、失败回滚和 SHA-256 回读核验的补丁事务。",
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "operation": {"type": "string", "enum": ["create_or_replace", "replace", "insert_before", "insert_after"]},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "expected_sha256": {"type": "string"}
                        },
                        "required": ["path", "operation", "new_text"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["edits"],
            "additionalProperties": False
        }
    },
    {
        "name": "manage_workspace_files",
        "description": "在用户明确授权后，在正式仓库内创建目录、移动、复制，或把文件移入可恢复的 AI 回收目录。禁止直接永久删除和仓库外路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string", "enum": ["create_directory", "move", "copy", "delete_to_trash"]},
                            "path": {"type": "string"},
                            "destination": {"type": "string"}
                        },
                        "required": ["operation", "path"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["operations"],
            "additionalProperties": False
        }
    },
    {
        "name": "run_workspace_command",
        "description": "在用户授权执行后，以参数数组而非 shell 运行仓库内受控 python、只读 git 或 rg 命令；返回退出码、输出、耗时和完整日志路径。适合语法检查、单元测试与核心回归。",
        "parameters": {
            "type": "object",
            "properties": {
                "executable": {"type": "string", "enum": ["python", "git", "rg"]},
                "arguments": {"type": "array", "items": {"type": "string"}, "maxItems": 80},
                "working_directory": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 900}
            },
            "required": ["executable", "arguments"],
            "additionalProperties": False
        }
    },
    {
        "name": "read_workspace_command_log",
        "description": "读取 run_workspace_command 生成的完整命令日志，用于输出被截断时继续核对；只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 500000}
            },
            "required": ["path"],
            "additionalProperties": False
        }
    },
    {
        "name": "run_workspace_sqlite_migration",
        "description": "在用户明确授权后，对仓库内 SQLite 数据库执行迁移。先用 SQLite backup API 建立恢复副本，事务执行后做 integrity_check 和 foreign_key_check，失败自动恢复。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "sql": {"type": "string"}
            },
            "required": ["path", "sql"],
            "additionalProperties": False
        }
    },
    {
        "name": "search_local_files",
        "description": "根据用户的自然语言线索搜索相关本机文件。只比较文件名和目录路径，不读取正文；返回的候选才可继续读取。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "简短的文件名、目录、学科、项目或扩展名关键词"},
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选扩展名，例如 .tex、.pdf",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_local_directory",
        "description": "仅当用户在当前对话中明确写出一个本机绝对目录时，列出该目录的直接子项；不递归扫描。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "用户明确给出的本机绝对目录"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_local_file",
        "description": "读取用户明确给出的本机文件，或本轮文件名搜索返回的相关候选文件；支持文本、代码、PDF 和 DOCX。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "用户明确给出的本机绝对文件路径"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_local_pdf_pages",
        "description": "精确读取用户明确给出或本轮语义/文件搜索返回的本地 PDF 连续页，保留页码标记；一次最多 30 页。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "page_start": {"type": "integer", "minimum": 1},
                "page_end": {"type": "integer", "minimum": 1},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
            "required": ["path", "page_start", "page_end"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_local_pdf_evidence_batch",
        "description": "一次读取最多 5 个本地 PDF 页段，统一返回带路径和页码的紧凑证据，适合跨教材或跨章节核对。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "page_start": {"type": "integer", "minimum": 1},
                            "page_end": {"type": "integer", "minimum": 1},
                            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 12000}
                        },
                        "required": ["path", "page_start", "page_end"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["requests"],
            "additionalProperties": False
        }
    },
    {
        "name": "edit_math_workspace_files",
        "description": "在用户明确要求修改后，以一次事务创建或修改最多 12 个本机文本、配置、LaTeX 或常见源代码文件。执行前应用会展示每个目标与差异并要求用户确认；自动备份，任一文件失败则全部回滚。二进制文件和数据库需要专用工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "绝对路径，或 MathWorkspace 下的相对路径"},
                            "operation": {
                                "type": "string",
                                "enum": ["create_or_replace", "insert_before", "insert_after", "replace"],
                            },
                            "anchor_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["path", "operation", "new_text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["edits"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compile_standalone_tex",
        "description": "编译受控数学工作区中的完整独立 TeX 文档，禁用 shell escape，并原子更新同名 PDF。需要用户明确授权。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lean_check",
        "description": "在固定的本机 Lean 4 + mathlib 工程中调用 Lean 内核核验 Generated 目录下的 .lean 文件。只运行固定命令，不开放 shell；拒绝 sorry、admit、新公理和编译期 IO。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "MathWorkspace/LeanProofs/Generated 下的 .lean 文件路径",
                },
                "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 180},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]

AI_OPERATION_REGISTRY.register_legacy_definitions(
    LEGACY_TOOL_DEFINITIONS,
    expected_manifest_hash="e6042bf3a74383154a7044507f10410f2934f403fafe7213d69df27dd5e00054",
)

_TEXTBOOK_REF_PROPERTIES = {
    "subject_name": {"type": "string", "description": "教材所属学科"},
    "book_ref": {
        "type": ["string", "integer"],
        "description": "教材 ID 或 book_code",
    },
}

for _operation_spec in (
    OperationSpec(
        operation_id="textbook.index.health",
        tool_name="get_textbook_index_health",
        description=(
            "读取并刷新教材索引健康状态：PDF 是否存在/可打开、文件变化、总页数、索引页数、"
            "分段数、文本层/OCR/无法识别页数、完整提取、最后成功时间、过期原因和最近错误。"
            "只写可重建的语义索引派生状态，不修改教材或题库正式数据。"
        ),
        parameters={
            "type": "object",
            "properties": {"subject_name": {"type": "string"}},
            "additionalProperties": False,
        },
        handler_name="_tool_get_textbook_index_health",
        access_level=DERIVED_WRITE,
        category="textbook_index",
    ),
    OperationSpec(
        operation_id="textbook.index.repair_failed_pages",
        tool_name="repair_failed_textbook_pages",
        description="只重新 OCR 已确认失败的教材页面，并刷新这些结果对应的本地分段索引；不处理其他页面。",
        parameters={
            "type": "object",
            "properties": dict(_TEXTBOOK_REF_PROPERTIES),
            "required": ["subject_name", "book_ref"],
            "additionalProperties": False,
        },
        handler_name="_tool_repair_failed_textbook_pages",
        access_level=DERIVED_WRITE,
        category="textbook_index",
        evidence_policy="before_after_page_readback",
    ),
    OperationSpec(
        operation_id="textbook.index.complete_ocr",
        tool_name="complete_textbook_ocr",
        description="对已经选定的一本教材补全尚缺少的 OCR，缓存到本机并刷新分段索引；不得无目的处理全部教材。",
        parameters={
            "type": "object",
            "properties": dict(_TEXTBOOK_REF_PROPERTIES),
            "required": ["subject_name", "book_ref"],
            "additionalProperties": False,
        },
        handler_name="_tool_complete_textbook_ocr",
        access_level=DERIVED_WRITE,
        category="textbook_index",
        evidence_policy="before_after_page_readback",
    ),
    OperationSpec(
        operation_id="textbook.index.rebuild",
        tool_name="rebuild_textbook_index",
        description="删除并重建所选教材的可重建文本/OCR缓存和语义分段，不修改教材登记与题库正式记录。",
        parameters={
            "type": "object",
            "properties": dict(_TEXTBOOK_REF_PROPERTIES),
            "required": ["subject_name", "book_ref"],
            "additionalProperties": False,
        },
        handler_name="_tool_rebuild_textbook_index",
        access_level=DERIVED_WRITE,
        category="textbook_index",
        evidence_policy="before_after_index_readback",
    ),
    OperationSpec(
        operation_id="textbook.index.unrecognized_pages",
        tool_name="list_unrecognized_textbook_pages",
        description="列出所选教材中已经尝试 OCR 但仍无法识别的精确页码、错误和置信度。",
        parameters={
            "type": "object",
            "properties": dict(_TEXTBOOK_REF_PROPERTIES),
            "required": ["subject_name", "book_ref"],
            "additionalProperties": False,
        },
        handler_name="_tool_list_unrecognized_textbook_pages",
        access_level=DERIVED_WRITE,
        category="textbook_index",
    ),
    OperationSpec(
        operation_id="textbook.index.verify_hit",
        tool_name="verify_textbook_index_hit",
        description="验证所选教材的本地分段索引能否命中指定术语、定理名或短句，并返回精确页段。",
        parameters={
            "type": "object",
            "properties": {
                **_TEXTBOOK_REF_PROPERTIES,
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["subject_name", "book_ref", "query"],
            "additionalProperties": False,
        },
        handler_name="_tool_verify_textbook_index_hit",
        category="textbook_index",
    ),
    OperationSpec(
        operation_id="textbook.binding.rebind_pdf",
        tool_name="rebind_textbook_pdf",
        description=(
            "找回或重新绑定所选教材的正式 PDF 路径。工具始终可发现，但只有用户明确授权写入后才执行；"
            "写入前备份教材数据库，事务提交后重新读取并检查 SQLite 完整性。"
        ),
        parameters={
            "type": "object",
            "properties": {
                **_TEXTBOOK_REF_PROPERTIES,
                "pdf_path": {"type": "string"},
            },
            "required": ["subject_name", "book_ref", "pdf_path"],
            "additionalProperties": False,
        },
        handler_name="_tool_rebind_textbook_pdf",
        access_level=FORMAL_WRITE,
        category="textbook_binding",
        evidence_policy="backup_transaction_integrity_readback",
    ),
    OperationSpec(
        operation_id="textbook.visual.render_pages",
        tool_name="render_textbook_pages_for_ai",
        description=(
            "把所选教材的 1 至 4 个精确页面渲染为本地高清 PNG，并在下一轮自动作为视觉证据交给模型。"
            "只能在文本检索已缩小教材和页码后调用，禁止整本视觉读取。"
        ),
        parameters={
            "type": "object",
            "properties": {
                **_TEXTBOOK_REF_PROPERTIES,
                "page_numbers": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 1,
                    "maxItems": 4,
                },
                "dpi": {"type": "integer", "minimum": 120, "maximum": 260},
                "inspection_focus": {"type": "string"},
            },
            "required": ["subject_name", "book_ref", "page_numbers"],
            "additionalProperties": False,
        },
        handler_name="_tool_render_textbook_pages_for_ai",
        access_level=DERIVED_WRITE,
        category="textbook_visual",
        evidence_policy="page_image_hash_and_source_fingerprint",
    ),
    OperationSpec(
        operation_id="textbook.visual.inspect_pages",
        tool_name="inspect_textbook_pages_visual",
        description=(
            "对所选教材的 1 至 4 个精确页面进行数学视觉核对。工具把页面图像、文字层和 OCR 摘要"
            "一起交给模型，适合公式、上下标、张量指标、交换图或 OCR 残缺页面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                **_TEXTBOOK_REF_PROPERTIES,
                "page_numbers": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 1,
                    "maxItems": 4,
                },
                "inspection_focus": {"type": "string"},
                "dpi": {"type": "integer", "minimum": 120, "maximum": 260},
            },
            "required": ["subject_name", "book_ref", "page_numbers"],
            "additionalProperties": False,
        },
        handler_name="_tool_inspect_textbook_pages_visual",
        access_level=DERIVED_WRITE,
        category="textbook_visual",
        evidence_policy="page_image_hash_and_source_fingerprint",
    ),
):
    AI_OPERATION_REGISTRY.register(_operation_spec)

_VOCABULARY_SELECTOR_PROPERTIES = {
    "entry_ids": {
        "type": "array",
        "items": {"type": "integer", "minimum": 1},
        "maxItems": 500,
        "description": "先用 search_vocabulary_entries 获得的词条 ID；可与 terms 二选一",
    },
    "terms": {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 500,
        "description": "要操作的完整英文单词或短语；可与 entry_ids 二选一",
    },
}
_VOCABULARY_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "term": {"type": "string", "description": "英文单词或完整短语，例如 omit"},
        "part_of_speech": {"type": "string", "description": "词性，例如 v.、n.、adj."},
        "definition": {"type": "string", "description": "中文释义，不能为空"},
        "familiarity": {
            "type": "string",
            "enum": ["familiar", "unfamiliar"],
            "description": "熟悉或不熟悉；省略时默认 unfamiliar",
        },
        "note": {"type": "string", "description": "可选语境、搭配或备注"},
        "source": {"type": "string", "description": "可选来源说明"},
        "entry_kind": {"type": "string", "enum": ["word", "phrase", "proper_noun", "abbreviation"]},
        "pronunciation": {"type": "string", "description": "可选 IPA 或学习者发音备注"},
    },
    "required": ["term", "definition"],
    "additionalProperties": False,
}

for _operation_spec in (
    OperationSpec(
        operation_id="vocabulary.format.read",
        tool_name="get_vocabulary_import_format",
        description=(
            "读取 AI 批量导入词汇的正式格式、字段含义、熟悉度取值、更新规则和 JSON 示例。"
            "在不确定导入格式时必须先调用本工具，不得自行猜测。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler_name="_tool_get_vocabulary_import_format",
        category="vocabulary",
    ),
    OperationSpec(
        operation_id="vocabulary.status.read",
        tool_name="get_vocabulary_status",
        description="读取当前工作空间专用词汇库路径、总词条数、熟悉/不熟悉数量和 SQLite 完整性。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler_name="_tool_get_vocabulary_status",
        category="vocabulary",
    ),
    OperationSpec(
        operation_id="vocabulary.entries.search",
        tool_name="search_vocabulary_entries",
        description=(
            "按英文词、短语、中文释义或备注查询当前工作空间专用词汇库，返回可供批量操作使用的 entry id、"
            "词性、释义、熟悉度和更新时间。修改或删除前应先查询并核对目标。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "familiarity": {"type": "string", "enum": ["all", "familiar", "unfamiliar"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler_name="_tool_search_vocabulary_entries",
        category="vocabulary",
    ),
    OperationSpec(
        operation_id="vocabulary.entries.import",
        tool_name="import_vocabulary_entries",
        description=(
            "批量新增或更新当前工作空间专用词汇库。参数必须是结构化 entries 数组；每项至少含英文 term 和中文 definition，"
            "可含 part_of_speech、familiarity、note、source。term 与 part_of_speech 的组合不区分大小写；"
            "同词同词性更新，同词不同词性作为独立词条。merge_definitions=true 时保留旧释义并追加新义项。"
            "单次最多 500 条；执行前备份，事务提交后逐条回读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "items": _VOCABULARY_ENTRY_SCHEMA,
                    "minItems": 1,
                    "maxItems": 500,
                    "description": (
                        '示例：[{"term":"omit","part_of_speech":"v.",'
                        '"definition":"省略；删除；遗漏","familiarity":"unfamiliar",'
                        '"note":"cannot be omitted"}]'
                    ),
                },
                "merge_definitions": {
                    "type": "boolean",
                    "description": (
                        "为 true 时，同词同词性保留原释义并只追加尚不存在的新义项；"
                        "省略或为 false 时按普通更新覆盖释义"
                    ),
                },
            },
            "required": ["entries"],
            "additionalProperties": False,
        },
        handler_name="_tool_import_vocabulary_entries",
        access_level=FORMAL_WRITE,
        category="vocabulary",
        evidence_policy="backup_transaction_integrity_entry_readback",
    ),
    OperationSpec(
        operation_id="vocabulary.entries.set_familiarity",
        tool_name="set_vocabulary_familiarity",
        description=(
            "批量把指定词条设为 familiar（熟悉）或 unfamiliar（不熟悉）。"
            "先查询得到精确 entry_ids 或提供完整 terms；执行前展示目标并备份，写后逐条回读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                **_VOCABULARY_SELECTOR_PROPERTIES,
                "familiarity": {"type": "string", "enum": ["familiar", "unfamiliar"]},
            },
            "required": ["familiarity"],
            "anyOf": [{"required": ["entry_ids"]}, {"required": ["terms"]}],
            "additionalProperties": False,
        },
        handler_name="_tool_set_vocabulary_familiarity",
        access_level=FORMAL_WRITE,
        category="vocabulary",
        evidence_policy="backup_transaction_entry_readback",
    ),
    OperationSpec(
        operation_id="vocabulary.entries.delete",
        tool_name="delete_vocabulary_entries",
        description=(
            "批量删除精确指定的词汇库词条。必须先查询并核对 entry_ids 或完整 terms；"
            "执行前展示将删除的词条并取得确认，先备份数据库，提交后验证目标已不存在。"
        ),
        parameters={
            "type": "object",
            "properties": dict(_VOCABULARY_SELECTOR_PROPERTIES),
            "anyOf": [{"required": ["entry_ids"]}, {"required": ["terms"]}],
            "additionalProperties": False,
        },
        handler_name="_tool_delete_vocabulary_entries",
        access_level=DESTRUCTIVE,
        category="vocabulary",
        evidence_policy="backup_transaction_absence_readback",
    ),
    OperationSpec(
        operation_id="vocabulary.export.txt",
        tool_name="export_vocabulary_txt",
        description="把全部、熟悉或不熟悉词条导出为 UTF-8 BOM TXT，并返回真实文件路径和词条数。",
        parameters={
            "type": "object",
            "properties": {
                "familiarity": {"type": "string", "enum": ["all", "familiar", "unfamiliar"]}
            },
            "additionalProperties": False,
        },
        handler_name="_tool_export_vocabulary_txt",
        access_level=DERIVED_WRITE,
        category="vocabulary",
        evidence_policy="export_file_readback",
    ),
    OperationSpec(
        operation_id="vocabulary.export.pdf",
        tool_name="export_vocabulary_pdf",
        description="把全部、熟悉或不熟悉词条导出为正式 PDF，并返回真实文件路径、词条数和输出回读。",
        parameters={
            "type": "object",
            "properties": {
                "familiarity": {"type": "string", "enum": ["all", "familiar", "unfamiliar"]}
            },
            "additionalProperties": False,
        },
        handler_name="_tool_export_vocabulary_pdf",
        access_level=DERIVED_WRITE,
        category="vocabulary",
        evidence_policy="compiled_export_file_readback",
    ),
    OperationSpec(
        operation_id="reference_materials.list",
        tool_name="list_ai_reference_materials",
        description=(
            "列出 AI 可稳定读取的项目文字规范及其 material_id，包括 LaTeX 写作规范、"
            "直接导入题目的中英文/批量模板和 AI 工具工作流。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler_name="_tool_list_ai_reference_materials",
        category="project_reference",
    ),
    OperationSpec(
        operation_id="reference_materials.read",
        tool_name="read_ai_reference_material",
        description=(
            "按稳定 material_id 读取项目正式文字规范的完整内容、来源定位和哈希。"
            "导入题目、生成 LaTeX 或不确定格式时，先列出并读取相应资料。"
        ),
        parameters={
            "type": "object",
            "properties": {"material_id": {"type": "string"}},
            "required": ["material_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_read_ai_reference_material",
        category="project_reference",
    ),
):
    AI_OPERATION_REGISTRY.register(_operation_spec)

AI_OPERATION_REGISTRY.register(
    OperationSpec(
        operation_id="markdown.preview.compile",
        tool_name="compile_markdown_preview",
        description=(
            "按题库管理中心 Markdown 数学阅读器使用的同一 CommonMark + GFM + 数学扩展规则"
            "编译 Markdown，返回定位块、公式数量、安全提示和渲染哈希；可选返回 HTML 片段。"
            "此操作只进行内存转换，不写文件、数据库、LaTeX 或 PDF。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "markdown": {"type": "string", "maxLength": 500000},
                "include_html": {"type": "boolean"},
            },
            "required": ["markdown"],
            "additionalProperties": False,
        },
        handler_name="_tool_compile_markdown_preview",
        category="markdown",
        evidence_policy="rendered_html_hash_and_source_map",
    )
)

AI_OPERATION_REGISTRY.register(
    OperationSpec(
        operation_id="math.exposition.audit",
        tool_name="audit_math_exposition",
        description=(
            "在提交数学回答前审校一份完整草稿：检查长回答是否按逻辑分节、自然段是否过长、"
            "并列内容是否适合编号、完整证明是否过短、是否含显然/同理式跳步，以及 Haus多夫这类"
            "数学人名中英文拆分混写；同时用正式 Markdown 数学渲染器检查标题、公式片段和安全警告。"
            "复杂证明、多阶段推导、三项以上对比或长回答应在最终输出前调用一次，并按问题清单修订。"
            "本工具只审校讲解质量，不能替代公式计算、数学证明或 Lean 内核验证。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "draft": {
                    "type": "string",
                    "maxLength": 100000,
                    "description": "准备提交给用户的完整 Markdown 数学回答草稿",
                },
                "user_request": {
                    "type": "string",
                    "maxLength": 12000,
                    "description": "用户本轮原始问题，用于判断是否要求完整证明或特殊长度",
                },
                "require_complete_proof": {
                    "type": "boolean",
                    "description": "本轮是否必须给出可逐步审查的完整证明",
                },
            },
            "required": ["draft"],
            "additionalProperties": False,
        },
        handler_name="_tool_audit_math_exposition",
        category="math_quality",
        evidence_policy="deterministic_structure_terminology_and_markdown_audit",
    )
)

for _operation_spec in (
    OperationSpec(
        operation_id="online_course.courses.list",
        tool_name="list_online_courses",
        description="列出指定学习项目中的网课、分期数量、录制状态和统一 PDF 状态。",
        parameters={
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "project_ref": {"type": ["string", "integer"]},
            },
            "required": ["subject_name", "project_ref"],
            "additionalProperties": False,
        },
        handler_name="_tool_list_online_courses",
        category="online_courses",
    ),
    OperationSpec(
        operation_id="online_course.lecture_outline.read",
        tool_name="get_online_course_lecture_outline",
        description=(
            "Read the complete locked outline of one online course and map every stable "
            "Section/Subsection to physical and printed page numbers in the latest formal PDF. "
            "Use this before claiming that an online-course subsection or page cannot be located."
        ),
        parameters={
            "type": "object",
            "properties": {"course_id": {"type": "integer", "minimum": 1}},
            "required": ["course_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_get_online_course_lecture_outline",
        category="online_courses",
        evidence_policy="formal_pdf_hash_complete_outline_and_bookmark_page_mapping",
    ),
    OperationSpec(
        operation_id="online_course.formal_pdf.search",
        tool_name="search_online_course_lecture_pdf",
        description=(
            "Search every page of the latest formal PDF for one online course. Return ranked "
            "snippets with the stable outline unit, physical PDF page, printed page label, PDF "
            "hash, and proof that the complete document was scanned. For a Chinese question, "
            "include precise English mathematical terms in search_terms."
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "search_terms": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 300},
                    "maxItems": 20,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["course_id", "query"],
            "additionalProperties": False,
        },
        handler_name="_tool_search_online_course_lecture_pdf",
        category="online_courses",
        evidence_policy="formal_pdf_sha256_every_page_text_scan_ranked_page_hits",
    ),
    OperationSpec(
        operation_id="online_course.formal_pdf.pages.read",
        tool_name="read_online_course_lecture_pdf_pages",
        description=(
            "Read up to 12 consecutive physical pages from the latest formal online-course PDF "
            "after a directory lookup or full-document search. Return printed page labels and "
            "the stable outline unit for every page."
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "page_start": {"type": "integer", "minimum": 1},
                "page_end": {"type": "integer", "minimum": 1},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 160000,
                },
            },
            "required": ["course_id", "page_start", "page_end"],
            "additionalProperties": False,
        },
        handler_name="_tool_read_online_course_lecture_pdf_pages",
        category="online_courses",
        evidence_policy="formal_pdf_hash_bounded_page_text_and_outline_readback",
    ),
    OperationSpec(
        operation_id="online_course.course.create",
        tool_name="create_online_course",
        description="在指定学习项目中创建一门网课；该网课以后录制的全部分期默认合并为一份英文讲义 PDF。",
        parameters={
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "project_ref": {"type": ["string", "integer"]},
                "title": {"type": "string"},
                "lecturer": {"type": "string"},
                "course_track": {
                    "type": "string",
                    "enum": ["general", "grammar", "vocabulary", "reading", "writing", "pronunciation", "supplement"],
                },
            },
            "required": ["subject_name", "project_ref", "title"],
            "additionalProperties": False,
        },
        handler_name="_tool_create_online_course",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="course_database_and_directory_readback",
    ),
    OperationSpec(
        operation_id="online_course.outline_structure.configure",
        tool_name="configure_online_course_outline_structure",
        description=(
            "经用户逐次确认后，为一门网课持久选择最细目录层级：Chapter / Section / Subsection，"
            "或只到 Chapter / Section。该选择按课程分别保存且以后可更改；两级结构要求已导入并"
            "按 Section 分拆的参考资料，保存后材料包失效并需要重新生成。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "include_subsections": {"type": "boolean"},
            },
            "required": ["course_id", "include_subsections"],
            "additionalProperties": False,
        },
        handler_name="_tool_configure_online_course_outline_structure",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="database_backup_course_mode_and_outline_readback",
    ),
    OperationSpec(
        operation_id="online_course.reference_materials.list",
        tool_name="list_online_course_reference_materials",
        description="列出一门网课已导入的参考资料、分拆状态、部分数量和正式保存位置。",
        parameters={
            "type": "object",
            "properties": {"course_id": {"type": "integer", "minimum": 1}},
            "required": ["course_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_list_online_course_reference_materials",
        category="online_courses",
        evidence_policy="reference_material_database_readback",
    ),
    OperationSpec(
        operation_id="online_course.reference_materials.import",
        tool_name="import_online_course_reference_materials",
        description=(
            "把用户明确指定的 PDF、DOCX、PPTX、TXT、Markdown 或 TeX 参考资料导入一门网课；"
            "PDF 由题库在后台自动调用开源 MinerU 提取正文、LaTeX 公式、表格、数学图、页码坐标、"
            "图注和邻近上下文；固定每 32 页一块并逐块校验落盘，失败后从失败块续跑，"
            "无需打开 MinerU 桌面程序。随后备份数据库、保存并校验原件，"
            "调用 Agent 只按参考资料中的节完整分拆成稳定目录，不依据当前尚不完整的课程目录预判"
            "未来映射，再逐节写入正式文件并回读。以后生成某个实际小节材料包时，才从完整紧凑目录"
            "做一次目标专属语义映射并持久化复用；后续网课材料 ZIP 只携带该小节选中的完整部分；"
            "其中每个 PDF 上传副本强制不超过 5 MB，并携带原始数学图、图上下文、重绘义务和固定全覆盖提示词。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
            },
            "required": ["course_id", "paths"],
            "additionalProperties": False,
        },
        handler_name="_tool_import_online_course_reference_materials",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy=(
            "database_backup_source_sha256_mineru_manifest_verified_32_page_checkpoints_"
            "complete_block_and_figure_coverage_"
            "agent_section_partition_without_future_mapping_atomic_part_write_database_readback_"
            "target_specific_incremental_mapping_and_five_mb_pdf_guard"
        ),
    ),
    OperationSpec(
        operation_id="online_course.reference_materials.reanalyze",
        tool_name="reanalyze_online_course_reference_materials",
        description=(
            "重新读取一门网课已经保存并校验的参考资料原件：PDF 自动复用源哈希、MinerU 版本和"
            "解析配置均一致的多模态缓存，否则在后台重新提取正文、公式、表格和数学图；"
            "提取固定每 32 页保存一个带哈希的检查点，异常后从失败块继续；随后只重建稳定教材节"
            "目录，不预判未来课程小节。每个实际小节在生成材料包时单独增量映射，无需打开 MinerU 桌面程序。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "material_ids": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            "required": ["course_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_reanalyze_online_course_reference_materials",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy=(
            "database_backup_verified_source_sha256_parser_config_fingerprint_"
            "verified_32_page_checkpoints_resume_from_failed_block_"
            "complete_block_and_figure_coverage_bounded_visual_boundary_review_"
            "partition_without_future_mapping_atomic_part_write_and_database_readback"
        ),
    ),
    OperationSpec(
        operation_id="online_course.recording.arm",
        tool_name="prepare_online_course_recording",
        description="把一门已创建的网课设为 Chrome 网页录制扩展的当前目标；只写可重建的临时选择状态。",
        parameters={
            "type": "object",
            "properties": {"course_id": {"type": "integer", "minimum": 1}},
            "required": ["course_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_prepare_online_course_recording",
        access_level=DERIVED_WRITE,
        category="online_courses",
        evidence_policy="armed_course_readback",
    ),
    OperationSpec(
        operation_id="online_course.media.status",
        tool_name="get_online_course_media_engine_status",
        description="检查网课模块的 Summarize、yt-dlp、FFmpeg/ffprobe、PySceneDetect、媒体运行时目录和转写服务配置状态；不返回 API Key。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler_name="_tool_get_online_course_media_engine_status",
        category="online_courses",
        evidence_policy="runtime_version_and_credential_presence_readback",
    ),
    OperationSpec(
        operation_id="video.episodes.list",
        tool_name="list_video_episodes",
        description=(
            "读取一个 Bilibili、YouTube 或其他 yt-dlp 支持网址的标题、平台和分集列表；"
            "只返回动态识别的集数、标题与时长，不下载媒体。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 8, "maxLength": 4000},
                "use_chrome_cookies": {"type": "boolean"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler_name="_tool_list_video_episodes",
        category="media_transcription",
        evidence_policy="remote_metadata_readback",
    ),
    OperationSpec(
        operation_id="video.quick_transcript.create",
        tool_name="create_quick_video_transcript",
        description=(
            "从 Bilibili、YouTube 或其他 yt-dlp 支持网址中只下载所选一集的最佳原始音频流，"
            "并用 Groq Large V3 或本地 Whisper 生成该集无时间戳纯文字稿；Groq 路径保存"
            "置信度证据，并只对自动识别出的疑难短区间二次转写。"
            "不播放视频，不进入网课讲义、目录或材料包流水线；该确定性操作不调用 Agent 改写内容。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 8, "maxLength": 4000},
                "use_chrome_cookies": {"type": "boolean"},
                "whisper_model": {
                    "type": "string",
                    "enum": ["tiny", "base", "small", "medium"],
                },
                "language": {"type": "string", "enum": ["", "zh", "en"]},
                "episode_number": {"type": "integer", "minimum": 1},
                "transcription_backend": {
                    "type": "string",
                    "enum": ["groq", "local"],
                },
                "force_retranscribe": {"type": "boolean"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler_name="_tool_create_quick_video_transcript",
        access_level=DERIVED_WRITE,
        category="media_transcription",
        evidence_policy="downloaded_audio_segment_confidence_and_raw_final_text_readback",
    ),
    OperationSpec(
        operation_id="online_course.diagrams.status",
        tool_name="get_online_course_diagram_backend_status",
        description=(
            "读取网课讲义唯一 TikZ 矢量绘图后端的可用状态，以及教材原图哈希原样复制策略。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler_name="_tool_get_online_course_diagram_backend_status",
        category="online_courses",
        evidence_policy="renderer_runtime_and_exact_copy_policy_readback",
    ),
    OperationSpec(
        operation_id="online_course.diagrams.render",
        tool_name="render_online_course_diagrams",
        description=(
            "Render one to twenty mathematical diagram sources inside the selected online course "
            "using the required TikZ backend and one complete body-local tikzpicture. "
            "Call this when the Agent creates a diagram: it returns a content-addressed vector PDF "
            "artifacts, hashes, timings, and readback evidence without changing formal lecture TeX."
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "purpose": {"type": "string", "maxLength": 200},
                "diagrams": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "diagram_id": {"type": "string", "maxLength": 100},
                            "title": {"type": "string", "maxLength": 500},
                            "description": {"type": "string", "maxLength": 12000},
                            "backend": {
                                "type": "string",
                                "enum": ["tikz"],
                            },
                            "source": {"type": "string", "minLength": 1, "maxLength": 200000},
                        },
                        "required": ["backend", "source"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["course_id", "diagrams"],
            "additionalProperties": False,
        },
        handler_name="_tool_render_online_course_diagrams",
        access_level=DERIVED_WRITE,
        category="online_course_diagrams",
        evidence_policy=(
            "tikz_source_hash_vector_pdf_single_page_and_manifest_readback"
        ),
    ),
    OperationSpec(
        operation_id="online_course.diagrams.recompile_previews",
        tool_name="recompile_online_course_diagram_previews",
        description=(
            "只重新编译一个已选网课正式小节时间范围内已经锁定的 TikZ 数学图源码，并把矢量 PDF "
            "预览重新发布到处理日志；同集其他小节不在作用域内。该操作不生成材料压缩包、"
            "不重建目录、也不调用识别 Agent。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subsection_id": {"type": "integer", "minimum": 1}
            },
            "required": ["subsection_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_recompile_online_course_diagram_previews",
        access_level=DERIVED_WRITE,
        category="online_course_diagrams",
        evidence_policy="locked_source_pdf_png_hash_and_preview_event_readback",
    ),
    OperationSpec(
        operation_id="online_course.processing.status",
        tool_name="get_online_course_processing_status",
        description=(
            "读取当前或最近网课分集的录制、增量转写、场景截图和材料压缩包状态；"
            "未运行任务时也返回最近一次已持久化结果。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "episode_id": {"type": "integer", "minimum": 1},
                "course_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        handler_name="_tool_get_online_course_processing_status",
        category="online_courses",
        evidence_policy="course_database_and_in_memory_queue_readback",
    ),
    OperationSpec(
        operation_id="online_course.retention.status",
        tool_name="get_online_course_retention_status",
        description=(
            "读取网课分集原始证据的自动保留策略、下一次到期时间、最近清理时间和待清理数量。"
            "永久材料 ZIP、讲义目录、LaTeX 与 PDF 不属于清理对象。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler_name="_tool_get_online_course_retention_status",
        category="online_courses",
        evidence_policy="settings_database_and_package_readback",
    ),
    OperationSpec(
        operation_id="online_course.retention.cleanup",
        tool_name="cleanup_online_course_expired_evidence",
        description=(
            "清理材料 ZIP 已成功保留满 24 小时的网课分集原始录制、派生截图和临时材料；"
            "只保留每集永久 ZIP，并保留课程数据库、目录、已导入 LaTeX 与最终 PDF。"
            "可先用 dry_run 预览精确目标。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "episode_id": {"type": "integer", "minimum": 1},
                "dry_run": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler_name="_tool_cleanup_online_course_expired_evidence",
        access_level=DESTRUCTIVE,
        category="online_courses",
        evidence_policy="eligible_zip_validation_exact_path_preview_and_post_delete_readback",
    ),
    OperationSpec(
        operation_id="online_course.media.install",
        tool_name="install_online_course_media_engine",
        description="一键安装或修复网课媒体运行时目录中的 Summarize、yt-dlp、FFmpeg/ffprobe 和 PySceneDetect，并回读版本。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler_name="_tool_install_online_course_media_engine",
        access_level=DERIVED_WRITE,
        category="online_courses",
        evidence_policy="runtime_version_readback",
    ),
    OperationSpec(
        operation_id="online_course.transcription.configure",
        tool_name="configure_online_course_transcription",
        description="选择无字幕音频使用的云端转写服务；API Key 用 Windows DPAPI 在本机加密保存。",
        parameters={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["groq", "assemblyai", "gemini", "openai", "fal", "deepgram"],
                },
                "api_key": {"type": "string", "maxLength": 4096},
            },
            "required": ["provider"],
            "additionalProperties": False,
        },
        handler_name="_tool_configure_online_course_transcription",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="dpapi_credential_presence_readback_without_secret",
    ),
    OperationSpec(
        operation_id="online_course.continuation_overlap.configure",
        tool_name="configure_online_course_continuation_overlap",
        description=(
            "设置同一分集分多次续录时预计提前重复录制的秒数。生成材料时仍按网页视频真实时间轴"
            "自动排除完整重叠音视频，只把跨越旧截止点的边界分块交给证据 Agent 做语句与公式衔接。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "minimum": 0, "maximum": 300},
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
        handler_name="_tool_configure_online_course_continuation_overlap",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="settings_json_atomic_write_and_readback",
    ),
    OperationSpec(
        operation_id="online_course.media.process",
        tool_name="process_online_course_media",
        description="为一门网课的全部分期核对原站信息、提取字幕、校验并标准化录音、执行场景检测与画面去重，并转写无字幕音频。",
        parameters={
            "type": "object",
            "properties": {"course_id": {"type": "integer", "minimum": 1}},
            "required": ["course_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_process_online_course_media",
        access_level=DERIVED_WRITE,
        category="online_courses",
        evidence_policy="transcript_file_and_database_readback",
    ),
    OperationSpec(
        operation_id="online_course.episode.prepare_chatgpt_package",
        tool_name="prepare_online_course_episode_package",
        description=(
            "根据某一分期当前永久保存的全部录制会话，转写音频、整理 Markdown 公式、"
            "按视频时间轴消除续录重叠，并用不低于 medium 的模型筛除重复或无价值截图，"
            "最后原子覆盖该集固定的 ChatGPT ZIP 材料包。"
            "本操作不会编写讲义或回答问题。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "episode_id": {"type": "integer", "minimum": 1},
                "normalize_formulas": {"type": "boolean"},
            },
            "required": ["episode_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_prepare_online_course_episode_package",
        access_level=DERIVED_WRITE,
        category="online_courses",
        evidence_policy="episode_zip_atomic_replace_and_readback",
    ),
    OperationSpec(
        operation_id="online_course.subsection.prepare_chatgpt_package",
        tool_name="prepare_online_course_subsection_package",
        description=(
            "重新生成一个稳定 Agent 小节 ID 的永久 ChatGPT 材料包。先更新该小节涉及的"
            "内部证据分集并锁定复用已完成录制批次，再严格按片段标注时间范围汇总完整"
            "时间戳转写、数学还原和 Agent 最终关键帧；随后只针对这个已经存在的小节从完整教材"
            "节目录建立或复用语义映射，ZIP 只包含选中的完整教材节。其他小节的证据不得进入该 ZIP。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subsection_id": {"type": "integer", "minimum": 1},
                "normalize_formulas": {"type": "boolean"},
            },
            "required": ["subsection_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_prepare_online_course_subsection_package",
        access_level=DERIVED_WRITE,
        category="online_courses",
        evidence_policy=(
            "stable_subsection_target_and_reference_catalog_fingerprints_incremental_mapping_"
            "scoped_zip_atomic_replace_and_readback"
        ),
    ),
    OperationSpec(
        operation_id="online_course.course.prepare_chatgpt_packages",
        tool_name="prepare_online_course_packages",
        description=(
            "重新生成一门网课中明确指定的单个分集或稳定写作节的永久 ChatGPT 材料包。"
            "必须提供 target_type 与 target_id；不得扫描或重建课程下其他分集。"
            "尚未形成稳定目录的失败录制使用 episode 目标按整节处理，成功后由 Agent 按数学内容形成目录。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "target_type": {
                    "type": "string",
                    "enum": ["episode", "subsection"],
                },
                "target_id": {"type": "integer", "minimum": 1},
                "normalize_formulas": {"type": "boolean"},
            },
            "required": ["course_id", "target_type", "target_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_prepare_online_course_packages",
        access_level=DERIVED_WRITE,
        category="online_courses",
        evidence_policy="explicit_selected_episode_or_subsection_zip_readback",
    ),
    OperationSpec(
        operation_id="online_course.recording_segment.delete",
        tool_name="delete_online_course_recording_segment",
        description=(
            "经用户逐次确认后精确删除一个物理录制段。操作不调用模型或外部 API；"
            "先预览批次 ID、时间范围、媒体分块和文件体积，备份 SQLite，把录制原件及"
            "含该段的失效派生材料移动到可恢复目录，再事务删除会话及其分块并写后回读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "subsection_id": {"type": "integer", "minimum": 1},
            },
            "required": ["session_id", "subsection_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_delete_online_course_recording_segment",
        access_level="destructive",
        category="online_courses",
        evidence_policy=(
            "explicit_preview_sqlite_backup_recoverable_file_move_transaction_"
            "integrity_check_and_write_readback_without_api"
        ),
    ),
    OperationSpec(
        operation_id="online_course.outline.configure",
        tool_name="configure_online_course_lecture_outline",
        description=(
            "经用户逐次确认后保存并锁定一门网课的正式英文讲义目录。Chapter 下包含 Section；每个录制分集可以"
            "按视频时间节点映射到一个或多个 Subsection 片段，同一分集编号可以重复出现。"
            "保存后 ChatGPT 提示词、LaTeX 导入和 PDF 编译统一使用稳定小节 ID；"
            "后续材料 Agent 对继续内容必须复用已有 ID 和规范标题，对有明确数学转场的新内容才可新建小节。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "chapters": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 160},
                            "number": {"type": "integer", "minimum": 1, "maximum": 999},
                            "sections": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {
                                            "type": "string", "minLength": 1, "maxLength": 160
                                        },
                                        "number": {
                                            "type": "integer", "minimum": 1, "maximum": 999
                                        },
                                        "segments": {
                                            "type": "array",
                                            "minItems": 1,
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "segment_id": {
                                                        "type": "integer", "minimum": 0
                                                    },
                                                    "episode_id": {
                                                        "type": "integer", "minimum": 1
                                                    },
                                                    "subsection_title": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                        "maxLength": 160,
                                                    },
                                                    "number": {
                                                        "type": "integer",
                                                        "minimum": 1,
                                                        "maximum": 999,
                                                    },
                                                    "end_video_time": {
                                                        "type": "number",
                                                        "exclusiveMinimum": 0,
                                                    },
                                                },
                                                "required": [
                                                    "episode_id",
                                                    "subsection_title",
                                                    "number",
                                                    "end_video_time",
                                                ],
                                                "additionalProperties": False,
                                            },
                                        },
                                    },
                                    "required": ["title", "number", "segments"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["title", "number", "sections"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["course_id", "chapters"],
            "additionalProperties": False,
        },
        handler_name="_tool_configure_online_course_lecture_outline",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="database_backup_outline_json_atomic_write_and_readback",
    ),
    OperationSpec(
        operation_id="online_course.episode.import_chatgpt_latex",
        tool_name="import_online_course_episode_latex",
        description=(
            "导入 ChatGPT 网页版为指定分集中的一个时间有界目录片段编写的英文 LaTeX；"
            "严格按 Agent 自动生成后可由用户或内置 AI 修订的正式讲义目录，"
            "检查 Chapter / Section / Subsection 的逐片段硬约束、禁用命令、中文正文和图片路径，"
            "备份旧源码后保存；禁止静默修正或降级 ChatGPT 擅自添加的目录标题。"
            "该操作只导入原始分段，不合并、不编译；合并只能由显式同小节合并操作触发。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "minimum": 1},
                "episode_id": {"type": "integer", "minimum": 1},
                "segment_id": {"type": "integer", "minimum": 1},
                "latex_source": {"type": "string", "maxLength": 1000000},
            },
            "required": ["course_id", "episode_id", "segment_id", "latex_source"],
            "additionalProperties": False,
        },
        handler_name="_tool_import_online_course_episode_latex",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="backup_latex_validation_and_source_readback_without_implicit_merge",
    ),
    OperationSpec(
        operation_id="online_course.recording_segment.import_chatgpt_latex",
        tool_name="import_online_course_recording_segment_latex",
        description=(
            "把 ChatGPT 返回的英文 LaTeX 直接导入一个或多个连续物理录制段；"
            "按稳定小节 ID、录制会话 ID 和精确时间范围验证，备份并回读正式源，"
            "随后执行普通 LaTeX/PDF 编译。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subsection_id": {"type": "integer", "minimum": 1},
                "session_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "session_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "latex_source": {"type": "string", "maxLength": 1000000},
            },
            "required": ["subsection_id", "latex_source"],
            "anyOf": [
                {"required": ["session_id"]},
                {"required": ["session_ids"]},
            ],
            "additionalProperties": False,
        },
        handler_name="_tool_import_online_course_recording_segment_latex",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="backup_scope_validation_formal_source_readback_and_pdf_hash_without_agent",
    ),
    OperationSpec(
        operation_id="online_course.subsection_latex.read",
        tool_name="get_online_course_subsection_latex",
        description=(
            "读取一个稳定网课小节当前可编辑的正文 LaTeX、正式目录编号、人工覆盖源路径和正式 PDF 状态；"
            "只返回正文，不把应用生成的 Chapter / Section / Subsection 标题混入编辑内容。"
        ),
        parameters={
            "type": "object",
            "properties": {"subsection_id": {"type": "integer", "minimum": 1}},
            "required": ["subsection_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_get_online_course_subsection_latex",
        category="online_courses",
        evidence_policy="stable_subsection_lookup_and_source_readback",
    ),
    OperationSpec(
        operation_id="online_course.subsection_latex.preview",
        tool_name="compile_online_course_subsection_latex_preview",
        description=(
            "在隔离预览目录中只渲染并编译候选小节，生成该小节的 PDF、SyncTeX 和派生源码；"
            "不改动正式人工覆盖稿、合并稿或正式课程 PDF。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subsection_id": {"type": "integer", "minimum": 1},
                "latex_source": {"type": "string", "minLength": 1, "maxLength": 1000000},
            },
            "required": ["subsection_id", "latex_source"],
            "additionalProperties": False,
        },
        handler_name="_tool_compile_online_course_subsection_latex_preview",
        access_level=DERIVED_WRITE,
        category="online_courses",
        evidence_policy="isolated_single_subsection_latexmk_pdf_synctex_and_source_readback",
    ),
    OperationSpec(
        operation_id="online_course.subsection_latex.edit",
        tool_name="edit_online_course_subsection_latex",
        description=(
            "正式保存一个网课小节的人工精修正文。先在隔离目录只编译当前小节预览，再备份旧覆盖稿、原子写入并回读，"
            "随后从头重建全部派生小节并完整编译、原子替换正式课程 PDF；普通失败恢复旧覆盖稿。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subsection_id": {"type": "integer", "minimum": 1},
                "latex_source": {"type": "string", "minLength": 1, "maxLength": 1000000},
            },
            "required": ["subsection_id", "latex_source"],
            "additionalProperties": False,
        },
        handler_name="_tool_edit_online_course_subsection_latex",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy=(
            "isolated_preview_backup_atomic_override_readback_full_rebuild_"
            "formal_pdf_hash_and_failure_rollback"
        ),
    ),
    OperationSpec(
        operation_id="online_course.subsections.merge",
        tool_name="merge_online_course_same_subsections",
        description=(
            "仅在用户明确要求时，按录制结束时持久化的稳定小节 ID，把跨证据分集或跨片段的同一小节合并为"
            "连续派生 LaTeX；保持原始分段 TeX 哈希不变，备份数据库和 LaTeX，随后编译并"
            "原子覆盖正式课程 PDF；已应用且源哈希未变的归组是幂等的，不再重复确认；底层证据分集与录制批次保留"
            "用于锁定复用已完成材料；完成合并清单、SQLite 完整性与 PDF 哈希回读。"
        ),
        parameters={
            "type": "object",
            "properties": {"course_id": {"type": "integer", "minimum": 1}},
            "required": ["course_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_merge_online_course_same_subsections",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy=(
            "explicit_preview_database_and_latex_backup_source_hash_preservation_"
            "sqlite_integrity_atomic_pdf_replace_and_manifest_readback"
        ),
    ),
    OperationSpec(
        operation_id="online_course.pdf.build",
        tool_name="compile_online_course_pdf",
        description=(
            "重新编译一门网课已经导入的正式 LaTeX，实时报告 latexmk 输出，"
            "原子替换课程 PDF，并进行文件哈希和数据库状态写后回读。"
        ),
        parameters={
            "type": "object",
            "properties": {"course_id": {"type": "integer", "minimum": 1}},
            "required": ["course_id"],
            "additionalProperties": False,
        },
        handler_name="_tool_compile_online_course_pdf",
        access_level=FORMAL_WRITE,
        category="online_courses",
        evidence_policy="latexmk_stream_atomic_pdf_replace_hash_and_database_readback",
    ),
    OperationSpec(
        operation_id="english.materials.list",
        tool_name="list_english_materials",
        description="列出英语课程与广读材料、来源绑定、文字层健康、阅读状态和最后阅读位置。",
        parameters={"type": "object", "properties": {
            "role": {"type": "string"}, "status": {"type": "string"},
            "keyword": {"type": "string"},
        }, "additionalProperties": False},
        handler_name="_tool_list_english_materials",
        category="english_learning",
    ),
    OperationSpec(
        operation_id="english.search",
        tool_name="search_english_learning",
        description="统一检索英语材料、Usage、句型 encounter 与稍后查标记，并返回可回跳的材料页码。",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        }, "required": ["query"], "additionalProperties": False},
        handler_name="_tool_search_english_learning",
        category="english_learning",
    ),
    OperationSpec(
        operation_id="english.material.import",
        tool_name="import_english_material",
        description="把 PDF/TXT/Markdown/HTML/DOCX/TeX 导入英语资料库；非 PDF 生成真实文字层阅读 PDF，写前备份并写后回读。",
        parameters={"type": "object", "properties": {
            "source_path": {"type": "string", "minLength": 1},
            "title": {"type": "string"}, "role": {"type": "string"},
            "material_type": {"type": "string"}, "program_code": {"type": "string"},
            "author": {"type": "string"}, "source_url": {"type": "string"},
        }, "required": ["source_path"], "additionalProperties": False},
        handler_name="_tool_import_english_material", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_copy_hash_text_layer_and_database_readback",
    ),
    OperationSpec(
        operation_id="english.material.bind",
        tool_name="bind_english_material_file",
        description="为预建课程材料重新绑定用户合法持有的本地原件；复制到材料库、检测文字层、备份并回读，不覆盖原文件。",
        parameters={"type": "object", "properties": {
            "material_id": {"type": "integer", "minimum": 1},
            "source_path": {"type": "string", "minLength": 1},
        }, "required": ["material_id", "source_path"], "additionalProperties": False},
        handler_name="_tool_bind_english_material_file", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_copy_hash_text_layer_and_database_readback",
    ),
    OperationSpec(
        operation_id="english.material.ocr_copy",
        tool_name="create_english_searchable_reading_copy",
        description="为扫描 PDF 生成不覆盖原件的 OCR 可搜索阅读副本，并用真实文字层检测验证选词能力。",
        parameters={"type": "object", "properties": {
            "material_id": {"type": "integer", "minimum": 1},
        }, "required": ["material_id"], "additionalProperties": False},
        handler_name="_tool_create_english_searchable_reading_copy", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_ocr_separate_copy_text_layer_and_database_readback",
    ),
    OperationSpec(
        operation_id="english.reading.defer",
        tool_name="mark_english_selection_for_later",
        description="在低打扰广读中把选中文本标记为稍后查，并保留上下文、材料和页码。",
        parameters={"type": "object", "properties": {
            "material_id": {"type": "integer", "minimum": 1}, "selected_text": {"type": "string", "minLength": 1},
            "context": {"type": "string"}, "page_number": {"type": "integer", "minimum": 1},
        }, "required": ["material_id", "selected_text", "page_number"], "additionalProperties": False},
        handler_name="_tool_mark_english_selection_for_later", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_source_context_readback",
    ),
    OperationSpec(
        operation_id="english.usage.save",
        tool_name="save_english_usage",
        description="保存英文句子、搭配或写作技巧，并保留材料、页码、上下文和分析。",
        parameters={"type": "object", "properties": {
            "text": {"type": "string", "minLength": 1}, "usage_kind": {"type": "string"},
            "context": {"type": "string"}, "material_id": {"type": ["integer", "null"]},
            "page_number": {"type": ["integer", "null"]}, "user_note": {"type": "string"},
            "agent_analysis": {"type": "string"}, "writing_technique": {"type": "string"},
        }, "required": ["text"], "additionalProperties": False},
        handler_name="_tool_save_english_usage", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_source_context_readback",
    ),
    OperationSpec(
        operation_id="english.grammar.encounter.save",
        tool_name="save_english_grammar_encounter",
        description="保存阅读中遇到的英文句子结构、分析和来源页码。",
        parameters={"type": "object", "properties": {
            "material_id": {"type": "integer", "minimum": 1},
            "selected_sentence": {"type": "string", "minLength": 1}, "analysis": {"type": "string"},
            "context": {"type": "string"}, "page_number": {"type": "integer", "minimum": 1},
        }, "required": ["material_id", "selected_sentence", "page_number"], "additionalProperties": False},
        handler_name="_tool_save_english_grammar_encounter", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_source_context_readback",
    ),
    OperationSpec(
        operation_id="english.writing.practice.create",
        tool_name="create_english_writing_practice",
        description="建立英语写作练习并可保存原稿，后续按诊断—学习者修订的版本链继续。",
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1}, "prompt": {"type": "string"},
            "original_draft": {"type": "string"}, "material_id": {"type": ["integer", "null"]},
        }, "required": ["title"], "additionalProperties": False},
        handler_name="_tool_create_english_writing_practice", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_revision_readback",
    ),
    OperationSpec(
        operation_id="english.writing.revision.add",
        tool_name="add_english_writing_revision",
        description="向既有写作练习添加诊断或学习者修订版本，永久保留原稿而不静默覆盖。",
        parameters={"type": "object", "properties": {
            "practice_id": {"type": "integer", "minimum": 1}, "content": {"type": "string", "minLength": 1},
            "revision_kind": {"type": "string"}, "diagnostic_feedback": {"type": "string"},
        }, "required": ["practice_id", "content"], "additionalProperties": False},
        handler_name="_tool_add_english_writing_revision", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_revision_readback",
    ),
    OperationSpec(
        operation_id="english.audio.resource.add",
        tool_name="add_english_audio_resource",
        description="登记本地音频或合法 URL 到英语材料；不可导出的专有 App 音频只记录存在，不尝试破解获取。",
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1}, "path_or_url": {"type": "string", "minLength": 1},
            "material_id": {"type": ["integer", "null"]}, "resource_kind": {"type": "string", "enum": ["local_file", "url", "external_app_reference"]},
            "notes": {"type": "string"},
        }, "required": ["title", "path_or_url"], "additionalProperties": False},
        handler_name="_tool_add_english_audio_resource", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_resource_readback",
    ),
    OperationSpec(
        operation_id="english.shadowing.attempt.record",
        tool_name="record_english_shadowing_attempt",
        description="记录一次输入驱动的 shadowing，保存源句、可选材料和用户录音路径，不伪造发音评分。",
        parameters={"type": "object", "properties": {
            "source_text": {"type": "string", "minLength": 1}, "material_id": {"type": ["integer", "null"]},
            "user_recording_path": {"type": "string"}, "note": {"type": "string"},
        }, "required": ["source_text"], "additionalProperties": False},
        handler_name="_tool_record_english_shadowing_attempt", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_attempt_readback",
    ),
    OperationSpec(
        operation_id="english.chapter.progress.update",
        tool_name="update_english_chapter_progress",
        description="更新旋元佑五书某章的学习状态和难点笔记，保留备份并写后回读。",
        parameters={"type": "object", "properties": {
            "chapter_id": {"type": "integer", "minimum": 1},
            "progress_status": {"type": "string", "enum": ["not_started", "reading", "practising", "reviewing", "completed"]},
            "progress_note": {"type": "string"},
            "page_start": {"type": ["integer", "null"], "minimum": 1},
            "page_end": {"type": ["integer", "null"], "minimum": 1},
        }, "required": ["chapter_id", "progress_status"], "additionalProperties": False},
        handler_name="_tool_update_english_chapter_progress", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_chapter_readback",
    ),
    OperationSpec(
        operation_id="english.grammar.exercise.attempt.record",
        tool_name="record_english_grammar_exercise_attempt",
        description="记录《文法解题》的题号、答案与错误原因，并把错误沉淀为可追踪误区。",
        parameters={"type": "object", "properties": {
            "material_id": {"type": "integer", "minimum": 1},
            "question_reference": {"type": "string", "minLength": 1},
            "selected_answer": {"type": "string"}, "correct_answer": {"type": "string"},
            "chapter_id": {"type": ["integer", "null"]}, "source_page": {"type": ["integer", "null"]},
            "explanation_reference": {"type": "string"}, "mistake_reason": {"type": "string"},
            "misconception_category": {"type": "string"},
        }, "required": ["material_id", "question_reference", "selected_answer", "correct_answer"], "additionalProperties": False},
        handler_name="_tool_record_english_grammar_exercise_attempt", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_attempt_and_misconception_readback",
    ),
    OperationSpec(
        operation_id="english.reading.training.attempt.record",
        tool_name="record_english_reading_training_attempt",
        description="记录《阅读》的篇章用时和正确率；与不计时的广读行为分开保存。",
        parameters={"type": "object", "properties": {
            "material_id": {"type": "integer", "minimum": 1},
            "passage_reference": {"type": "string", "minLength": 1},
            "duration_seconds": {"type": ["integer", "null"], "minimum": 0},
            "correct_count": {"type": ["integer", "null"], "minimum": 0},
            "question_count": {"type": ["integer", "null"], "minimum": 0},
            "question_type": {"type": "string"}, "error_reason": {"type": "string"},
            "note": {"type": "string"}, "chapter_id": {"type": ["integer", "null"]},
        }, "required": ["material_id", "passage_reference"], "additionalProperties": False},
        handler_name="_tool_record_english_reading_training_attempt", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_training_readback",
    ),
    OperationSpec(
        operation_id="english.reading.deferred.list",
        tool_name="list_english_deferred_lookups",
        description="列出广读中尚未处理或已处理的稍后查文本及其材料、上下文和页码。",
        parameters={"type": "object", "properties": {
            "resolved": {"type": ["boolean", "null"]}, "keyword": {"type": "string"},
        }, "additionalProperties": False},
        handler_name="_tool_list_english_deferred_lookups",
        category="english_learning",
    ),
    OperationSpec(
        operation_id="english.reading.deferred.resolve",
        tool_name="resolve_english_deferred_lookup",
        description="把一条稍后查记录标记为已处理，保留原上下文并执行备份与写后回读。",
        parameters={"type": "object", "properties": {
            "lookup_id": {"type": "integer", "minimum": 1},
        }, "required": ["lookup_id"], "additionalProperties": False},
        handler_name="_tool_resolve_english_deferred_lookup", access_level=FORMAL_WRITE,
        category="english_learning", evidence_policy="backup_transaction_deferred_readback",
    ),
):
    AI_OPERATION_REGISTRY.register(_operation_spec)

TOOL_DEFINITIONS: list[dict[str, Any]] = AI_OPERATION_REGISTRY.definitions()


class AiAgentToolExecutor:
    def __init__(self, repository: GlobalProblemRepository, discipline: str = "math") -> None:
        self.repository = repository
        requested_discipline = str(discipline).casefold()
        self.discipline = requested_discipline if requested_discipline in {"math", "physics", "english"} else "math"
        self.repository.set_vocabulary_workspace(self.discipline)
        # Default resource discovery includes the repository, the user profile
        # and every existing Windows drive.  Exact absolute paths remain
        # directly readable even when an indexed filename search misses them.
        self.resources = ReadOnlyResourceAccessor()
        if self.discipline == "physics":
            cache_dir = self.repository.root_dir / "shared" / "ui" / "cache"
            self.semantic_index = SemanticIndex(
                repository,
                path=cache_dir / "ai_physics_agent_semantic_index.db",
                history_path=cache_dir / "ai_physics_agent_history.json",
                reference_library_path=cache_dir / "ai_physics_reference_library.json",
                workspace_dir=DEFAULT_PHYSICS_WORKSPACE,
            )
        else:
            self.semantic_index = SemanticIndex(repository)
        self.tex_editor = ProjectTexEditor(repository)
        self.workspace_editor = MathWorkspaceEditor(
            default_workspace=DEFAULT_PHYSICS_WORKSPACE if self.discipline == "physics" else DEFAULT_MATH_WORKSPACE,
            allow_lean=self.discipline == "math",
        )
        self.workspace_tools = WorkspaceToolManager(self.repository.root_dir)
        self.compute_manager = LocalComputeManager()
        self.lean_manager = LeanCheckManager() if self.discipline == "math" else None
        self.paper_accessor = AcademicPaperAccessor()
        self.vocabulary_manager = VocabularyManager(
            workspace=self.discipline,
            root_dir=self.repository.user_data_root,
        )
        self.reference_materials = AiReferenceMaterialRegistry()
        self.online_courses = OnlineCourseService()
        self._english_learning_service: EnglishLearningService | None = None
        self._scope_subject = ""
        self._scope_project = ""
        self._scope_problem_ids: set[int] | None = None
        self._write_authorized = False
        self._successful_project_reads: set[tuple[str, str, str]] = set()
        self._successful_local_reads: set[str] = set()
        self._successful_workspace_database_reads: set[str] = set()
        self._project_mutation_succeeded = False
        self._mutation_rejected = False
        self._progress_callback: Callable[[str], None] | None = None
        self._mutation_approval_callback: Callable[[dict[str, Any]], bool] | None = None
        self._operation_task_id = ""
        self.operation_journal = OperationJournal()

    def set_progress_callback(self, callback: Callable[[str], None] | None) -> None:
        self._progress_callback = callback

    def cancel_current_compute(self) -> None:
        self.compute_manager.cancel_current()
        if self.lean_manager is not None:
            self.lean_manager.cancel_current()

    def set_mutation_approval_callback(
        self,
        callback: Callable[[dict[str, Any]], bool] | None,
        task_id: str = "",
    ) -> None:
        self._mutation_approval_callback = callback
        self._operation_task_id = str(task_id or "")

    @staticmethod
    def _unified_diff(path: str, original: str, updated: str) -> str:
        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=path + "（修改前）",
                tofile=path + "（修改后）",
                n=4,
            )
        )[:60000]

    def mutation_preview(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        preview: dict[str, Any] = {
            "tool_name": name,
            "title": "确认 AI 项目操作",
            "targets": [],
            "diff": "",
            "arguments": dict(arguments),
        }
        if name in {"edit_project_tex", "insert_tikz_figure"}:
            project_dir, _project, target = self.tex_editor._target(
                str(arguments.get("subject_name") or ""),
                str(arguments.get("project_ref") or ""),
                str(arguments.get("relative_path") or ""),
            )
            original = target.read_text(encoding="utf-8")
            operation = str(arguments.get("operation") or "")
            fragment = str(arguments.get("new_tex") or "")
            if name == "insert_tikz_figure":
                operation = "insert_before" if str(arguments.get("position") or "") == "before" else "insert_after"
                fragment = str(arguments.get("tikz_code") or "")
                caption = str(arguments.get("caption") or "").strip()
                label = str(arguments.get("label") or "").strip()
                if caption or label:
                    lines = [r"\begin{figure}[htbp]", r"\centering", fragment]
                    if caption:
                        lines.append(r"\caption{" + caption + "}")
                    if label:
                        lines.append(r"\label{" + label + "}")
                    lines.append(r"\end{figure}")
                    fragment = "\n".join(lines)
            updated = self.tex_editor._apply_operation(
                original,
                operation,
                str(arguments.get("anchor_text") or ""),
                fragment,
            )
            relative = target.relative_to(project_dir).as_posix()
            preview["targets"] = [str(target)]
            preview["diff"] = self._unified_diff(relative, original, updated)
            patch_state = project_dir / ".ai_agent_tex_patches.json"
            preview["recovery"] = [
                {"path": str(target), "existed_before": True, "original_text": original},
                {
                    "path": str(patch_state),
                    "existed_before": patch_state.is_file(),
                    "original_text": patch_state.read_text(encoding="utf-8") if patch_state.is_file() else "",
                },
            ]
        elif name == "edit_math_workspace_files":
            diffs: list[str] = []
            targets: list[str] = []
            for edit in arguments.get("edits") or []:
                if not isinstance(edit, dict):
                    continue
                target = self.workspace_editor._target(
                    str(edit.get("path") or ""),
                    allow_create=str(edit.get("operation") or "") == "create_or_replace",
                )
                original = target.read_text(encoding="utf-8") if target.is_file() else ""
                updated = self.workspace_editor._apply(original, edit)
                targets.append(str(target))
                diffs.append(self._unified_diff(str(target), original, updated))
            preview["targets"] = targets
            preview["diff"] = "\n".join(diffs)[:60000]
            preview["recovery"] = [
                {
                    "path": str(target),
                    "existed_before": target.is_file(),
                    "original_text": target.read_text(encoding="utf-8") if target.is_file() else "",
                }
                for target in [
                    self.workspace_editor._target(
                        str(edit.get("path") or ""),
                        allow_create=str(edit.get("operation") or "") == "create_or_replace",
                    )
                    for edit in arguments.get("edits") or []
                    if isinstance(edit, dict)
                ]
            ]
        elif name == "apply_workspace_patch":
            patch_preview = self.workspace_tools.preview_patch(arguments.get("edits") or [])
            preview["title"] = "确认 AI 修改正式仓库代码"
            preview["targets"] = list(patch_preview.get("targets") or [])
            preview["diff"] = str(patch_preview.get("diff") or "")
            preview["recovery"] = list(patch_preview.get("recovery") or [])
        elif name == "manage_workspace_files":
            file_preview = self.workspace_tools.preview_file_operations(arguments.get("operations") or [])
            preview["title"] = "确认 AI 管理正式仓库文件"
            preview["targets"] = list(file_preview.get("targets") or [])
            preview["diff"] = str(file_preview.get("diff") or "")
        elif name == "run_workspace_command":
            preview["title"] = "确认 AI 运行本地验证命令"
            preview["targets"] = [str(self.workspace_tools._resolve(str(arguments.get("working_directory") or "")))]
            preview["diff"] = "将运行：" + subprocess.list2cmdline(
                [str(arguments.get("executable") or ""), *[str(item) for item in arguments.get("arguments") or []]]
            )
        elif name == "run_workspace_sqlite_migration":
            target = self.workspace_tools._resolve(str(arguments.get("path") or ""))
            preview["title"] = "确认 AI 执行 SQLite 迁移"
            preview["targets"] = [str(target)]
            preview["diff"] = str(arguments.get("sql") or "")[:60000]
        elif name == "compile_standalone_tex":
            preview["title"] = "确认生成独立 PDF"
            preview["targets"] = [str(arguments.get("path") or "")]
            preview["diff"] = "此操作不会修改 TeX 正文，但会生成或替换同名 PDF。"
        elif name == "build_project_pdf":
            preview["title"] = "确认重新生成项目 PDF"
            preview["targets"] = [
                f"{arguments.get('subject_name') or ''} / {arguments.get('project_ref') or ''}"
            ]
            preview["diff"] = "此操作不会修改 TeX 正文，但会重新生成并替换项目正式 PDF。"
        elif name == "rebind_textbook_pdf":
            preview["title"] = "确认 AI 重新绑定教材 PDF"
            preview["targets"] = [
                f"{arguments.get('subject_name') or ''} / {arguments.get('book_ref') or ''}",
                str(arguments.get("pdf_path") or ""),
            ]
            preview["diff"] = (
                "将修改正式教材表中的 pdf_path。执行前自动备份数据库，"
                "提交后运行 SQLite 完整性检查并重新读取绑定结果。"
            )
        elif name == "import_vocabulary_entries":
            entries = [item for item in arguments.get("entries") or [] if isinstance(item, dict)]
            preview["title"] = "确认 AI 批量导入 / 更新词汇"
            preview["targets"] = [
                f"{item.get('term') or ''} = {item.get('definition') or ''}"
                for item in entries[:100]
            ]
            preview["diff"] = (
                f"将新增或更新 {len(entries)} 个词条；同名 term 不区分大小写并更新原词条。"
                f"执行前备份 {self.vocabulary_manager.database}，事务提交后逐条回读。"
            )
        elif name in {"set_vocabulary_familiarity", "delete_vocabulary_entries"}:
            selected = self.vocabulary_manager.resolve_entries(
                arguments.get("entry_ids") or [], arguments.get("terms") or []
            )
            preview["title"] = (
                "确认 AI 批量设置词汇熟悉度"
                if name == "set_vocabulary_familiarity"
                else "确认 AI 批量删除词汇"
            )
            preview["targets"] = [
                f"#{item['id']} {item['term']} | {item['definition']}"
                for item in selected[:500]
            ]
            preview["diff"] = (
                f"将把 {len(selected)} 个词条设为 {arguments.get('familiarity')}。"
                if name == "set_vocabulary_familiarity"
                else f"将从正式词汇库删除 {len(selected)} 个精确匹配词条。"
            ) + "执行前自动备份，事务提交后回读验证。"
        elif name == "import_online_course_reference_materials":
            course_id = int(arguments.get("course_id") or 0)
            paths = [Path(str(value)).expanduser().resolve() for value in arguments.get("paths") or []]
            targets: list[str] = [f"网课 course_id={course_id}"]
            for path in paths:
                digest = ""
                if path.is_file():
                    hasher = hashlib.sha256()
                    with path.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            hasher.update(block)
                    digest = hasher.hexdigest()
                targets.append(
                    f"{path} | sha256={digest or '文件不存在'}"
                )
            preview["title"] = "确认由 MinerU 提取并导入网课参考教材"
            preview["targets"] = targets
            preview["diff"] = (
                "将备份网课数据库并校验所列原件；PDF 由题库后台调用隔离的开源 MinerU，"
                "持久化正文、LaTeX 公式、表格、数学图裁图、页码坐标、图注和邻近上下文，"
                "再原子写入完整稳定章节分拆，但不预判未来课程小节。原件不改写；失败时保留恢复点和错误证据。"
            )
        elif name == "reanalyze_online_course_reference_materials":
            course_id = int(arguments.get("course_id") or 0)
            requested = {
                int(value) for value in arguments.get("material_ids") or []
                if int(value) > 0
            }
            materials = [
                row
                for row in self.online_courses.reference_materials(course_id)
                if not requested or int(row.get("id") or 0) in requested
            ]
            preview["title"] = "确认由 MinerU 重新提取 / 分拆参考教材"
            preview["targets"] = [
                (
                    f"material_id={int(row.get('id') or 0)} "
                    f"{row.get('original_filename') or ''} | "
                    f"现有 parts={int(row.get('part_count') or 0)} | "
                    f"sha256={row.get('source_sha256') or ''}"
                )
                for row in materials
            ]
            preview["diff"] = (
                "将先备份网课数据库；源哈希、MinerU 版本和解析配置完全一致时复用多模态缓存，"
                "否则重新提取。成功后原子替换所列材料的 chunks、稳定 parts 和数学图上下文；"
                "具体课程小节在生成材料包时按目标与目录指纹增量映射；"
                "任一材料重建失败时不得用不完整结果覆盖它此前的正式分拆。"
            )
        elif name in {
            "create_online_course",
            "import_online_course_episode_latex",
            "import_online_course_recording_segment_latex",
            "merge_online_course_same_subsections",
            "edit_online_course_subsection_latex",
        }:
            preview["title"] = {
                "create_online_course": "确认创建网课",
                "import_online_course_episode_latex": "确认导入 ChatGPT 分集 LaTeX",
                "import_online_course_recording_segment_latex": "确认导入 ChatGPT 录制段 LaTeX",
                "merge_online_course_same_subsections": "确认合并网课同一小节",
                "edit_online_course_subsection_latex": "确认精修网课小节 TeX",
            }[name]
            preview["targets"] = [str(COURSE_STORAGE_ROOT)]
            preview["diff"] = {
                "create_online_course": "将在课程数据目录中新增课程记录和独立课程文件夹。",
                "import_online_course_episode_latex": (
                    "将检查并备份后保存 ChatGPT 网页版返回的英文分段 LaTeX；"
                    "不会自动合并或编译正式 PDF。"
                ),
                "import_online_course_recording_segment_latex": (
                    f"将小节 #{int(arguments.get('subsection_id') or 0)} 的物理录制批次 "
                    f"{', '.join(str(value) for value in (arguments.get('session_ids') or [arguments.get('session_id')]) if value)} "
                    "直接写入正式讲义并执行普通 PDF 编译。"
                ),
                "merge_online_course_same_subsections": (
                    "将按人工确认目录预览同小节分组，备份数据库与 LaTeX，保持原始分段"
                    "哈希不变，生成连续小节文件并原子覆盖正式 PDF。"
                ),
                "edit_online_course_subsection_latex": (
                    f"将精修小节 #{int(arguments.get('subsection_id') or 0)} 的正文 TeX；"
                    "先单独编译当前小节预览，再备份并原子保存人工覆盖稿，随后从头重建整门课程并替换正式 PDF。"
                ),
            }[name]
        elif name == "delete_online_course_recording_segment":
            deletion = self.online_courses.recording_segment_delete_preview(
                str(arguments.get("session_id") or ""),
                subsection_id=int(arguments.get("subsection_id") or 0),
            )
            preview["title"] = "确认精确删除网课录制段"
            preview["targets"] = [
                str(deletion.get("session_dir") or ""),
                *[
                    f"{item.get('stable_key') or ''} {item.get('title') or ''}"
                    for item in deletion.get("affected_subsections") or []
                ],
            ]
            preview["diff"] = (
                f"将删除批次 {deletion.get('session_id')} 的 "
                f"{int(deletion.get('chunk_count') or 0)} 个媒体分块、"
                f"{int(deletion.get('caption_count') or 0)} 条批次字幕、"
                f"{int(deletion.get('frame_count') or 0)} 张截图和 "
                f"{int(deletion.get('file_count') or 0)} 个原始文件；"
                "执行前备份 SQLite，原件与失效材料移动到恢复目录，API 调用为 0。"
            )
        return preview

    def begin_turn(
        self,
        user_text: str,
        current_context: dict[str, Any] | None = None,
        *,
        write_authorized: bool = False,
    ) -> None:
        self.resources.begin_turn(user_text)
        self.paper_accessor.begin_turn()
        self._scope_subject = ""
        self._scope_project = ""
        self._scope_problem_ids = None
        self._write_authorized = bool(write_authorized)
        self._successful_project_reads.clear()
        self._successful_local_reads.clear()
        self._successful_workspace_database_reads.clear()
        self._project_mutation_succeeded = False
        self._mutation_rejected = False
        # Current UI context is a ranking hint, not a read-permission boundary.
        # All subjects, projects and standard problems remain readable.  Every
        # mutating tool is still guarded by the per-operation confirmation path.

    def _ensure_problem_in_scope(self, problem: dict[str, Any]) -> None:
        return

    def _ensure_project_in_scope(self, subject_name: str, project_ref: str) -> None:
        return

    def _filter_problem_page(self, result: dict[str, Any], key: str) -> dict[str, Any]:
        return result

    @staticmethod
    def _textbook_tool_scope(arguments: dict[str, Any]) -> tuple[str, int | str]:
        subject_name = str(arguments.get("subject_name") or "").strip()
        book_ref = arguments.get("book_ref")
        if not subject_name:
            raise ValueError("教材工具必须指定 subject_name。")
        if book_ref in (None, ""):
            raise ValueError("教材工具必须指定 book_ref。")
        return subject_name, book_ref

    def _tool_get_textbook_index_health(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.semantic_index.textbook_health_status(
            str(arguments.get("subject_name") or "")
        )

    def _tool_repair_failed_textbook_pages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        return self.semantic_index.repair_failed_textbook_pages(subject_name, book_ref)

    def _tool_complete_textbook_ocr(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        return self.semantic_index.complete_textbook_ocr(subject_name, book_ref)

    def _tool_rebuild_textbook_index(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        return self.semantic_index.rebuild_textbook_index(subject_name, book_ref)

    def _tool_list_unrecognized_textbook_pages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        return self.semantic_index.unrecognized_textbook_pages(subject_name, book_ref)

    def _tool_verify_textbook_index_hit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        return self.semantic_index.verify_textbook_index_hit(
            subject_name,
            book_ref,
            str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 8),
        )

    def _tool_rebind_textbook_pdf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        result = self.repository.rebind_textbook_pdf(
            subject_name,
            book_ref,
            str(arguments.get("pdf_path") or ""),
        )
        health = self.semantic_index.textbook_health_status(subject_name)
        result["index_health_readback"] = next(
            (
                row
                for row in health.get("textbooks", [])
                if int(row.get("book_id") or 0) == int(result.get("book_id") or 0)
            ),
            {},
        )
        return result

    def _tool_render_textbook_pages_for_ai(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        result = self.semantic_index.render_textbook_pages_for_ai(
            subject_name,
            book_ref,
            [int(page) for page in arguments.get("page_numbers") or []],
            dpi=int(arguments.get("dpi") or 220),
            inspection_focus=str(arguments.get("inspection_focus") or ""),
        )
        for item in result.get("visual_evidence") or []:
            if isinstance(item, dict) and item.get("path"):
                self.resources.authorize_generated_path(str(item["path"]))
        return result

    def _tool_inspect_textbook_pages_visual(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name, book_ref = self._textbook_tool_scope(arguments)
        result = self.semantic_index.inspect_textbook_pages_visual(
            subject_name,
            book_ref,
            [int(page) for page in arguments.get("page_numbers") or []],
            inspection_focus=str(arguments.get("inspection_focus") or "").strip()
            or "核对公式、上下标、特殊符号和图形",
            dpi=int(arguments.get("dpi") or 220),
        )
        for item in result.get("visual_evidence") or []:
            if isinstance(item, dict) and item.get("path"):
                self.resources.authorize_generated_path(str(item["path"]))
        return result

    def _tool_get_vocabulary_import_format(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_name": "import_vocabulary_entries",
            "format": "structured_json_entries",
            "max_entries_per_call": 500,
            "required_fields": {
                "term": "英文单词或完整短语，不能为空",
                "definition": "中文释义，不能为空",
            },
            "optional_fields": {
                "part_of_speech": "词性，例如 v.、n.、adj.",
                "familiarity": "familiar 或 unfamiliar；新词条默认 unfamiliar，更新旧词条时省略则保留原值",
                "note": "语境、固定搭配或备注",
                "source": "来源说明",
            },
            "optional_parameters": {
                "merge_definitions": (
                    "true 表示同词同词性保留原释义并追加未出现的新义项；"
                    "false 表示普通更新覆盖释义"
                ),
            },
            "matching_rule": (
                "term + part_of_speech 使用 COLLATE NOCASE 联合判重；"
                "同词同词性更新，同词不同词性分别保存；merge_definitions=true 时追加新义项；"
                "更新时未提供 familiarity 会保留原熟悉度"
            ),
            "example_arguments": {
                "entries": [
                    {
                        "term": "omit",
                        "part_of_speech": "v.",
                        "definition": "省略；删除；遗漏",
                        "familiarity": "unfamiliar",
                        "note": "cannot be omitted：不能省略",
                        "source": "当前教材",
                    },
                    {
                        "term": "finite subcover",
                        "part_of_speech": "n.",
                        "definition": "有限子覆盖",
                        "familiarity": "familiar",
                    },
                ]
            },
            "ui_text_formats": [
                "term | part of speech | 中文释义",
                "term = 中文释义",
                "term [n.]：中文释义",
            ],
            "safety": "正式写入前逐次确认并备份；事务提交后逐条回读",
        }

    def _tool_get_vocabulary_status(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return self.vocabulary_manager.status()

    def _tool_search_vocabulary_entries(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entries = self.vocabulary_manager.list_entries(
            str(arguments.get("query") or ""),
            str(arguments.get("familiarity") or "all"),
            limit=int(arguments.get("limit") or 200),
        )
        return {"entries": entries, "result_count": len(entries)}

    def _tool_import_vocabulary_entries(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entries = [
            {**item, "source": str(item.get("source") or "AI assistant")}
            for item in arguments.get("entries") or []
            if isinstance(item, dict)
        ]
        return self.vocabulary_manager.import_entries(
            entries,
            merge_definitions=bool(arguments.get("merge_definitions")),
        )

    def _tool_set_vocabulary_familiarity(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.vocabulary_manager.set_familiarity(
            str(arguments.get("familiarity") or ""),
            entry_ids=arguments.get("entry_ids") or [],
            terms=arguments.get("terms") or [],
        )

    def _tool_delete_vocabulary_entries(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.vocabulary_manager.delete_entries(
            entry_ids=arguments.get("entry_ids") or [],
            terms=arguments.get("terms") or [],
        )

    def _tool_export_vocabulary_txt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.vocabulary_manager.export_txt(
            str(arguments.get("familiarity") or "all")
        )
        self.resources.authorize_generated_path(str(result.get("path") or ""))
        return result

    def _english_learning(self) -> EnglishLearningService:
        if self._english_learning_service is None:
            self._english_learning_service = EnglishLearningService(self.repository.root_dir / "English")
        return self._english_learning_service

    def _tool_list_english_materials(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rows = self._english_learning().list_materials(
            role=str(arguments.get("role") or ""), status=str(arguments.get("status") or ""),
            keyword=str(arguments.get("keyword") or ""),
        )
        return {"materials": rows, "result_count": len(rows), "summary": self._english_learning().summary()}

    def _tool_search_english_learning(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rows = self._english_learning().unified_search(
            str(arguments.get("query") or ""), limit=int(arguments.get("limit") or 100)
        )
        return {"results": rows, "result_count": len(rows)}

    def _tool_import_english_material(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(arguments.get("source_path") or "")).expanduser().resolve()
        if not self.resources.path_is_authorized(str(source)):
            raise ValueError("导入本地英语材料前，用户必须在当前消息中明确提供该绝对路径。")
        result = self._english_learning().import_material(
            source, title=str(arguments.get("title") or ""), role=str(arguments.get("role") or "extensive_reading"),
            material_type=str(arguments.get("material_type") or "document"),
            program_code=str(arguments.get("program_code") or ""), author=str(arguments.get("author") or ""),
            source_url=str(arguments.get("source_url") or ""),
        )
        self.resources.authorize_generated_path(str(result.get("reading_path") or ""))
        return result

    def _tool_bind_english_material_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(arguments.get("source_path") or "")).expanduser().resolve()
        if not self.resources.path_is_authorized(str(source)):
            raise ValueError("绑定英语材料前，用户必须在当前消息中明确提供该绝对路径。")
        result = self._english_learning().bind_material_file(int(arguments["material_id"]), source)
        self.resources.authorize_generated_path(str(result.get("reading_path") or ""))
        return result

    def _tool_create_english_searchable_reading_copy(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._english_learning().create_searchable_reading_copy(int(arguments["material_id"]))
        self.resources.authorize_generated_path(str(result.get("reading_path") or ""))
        return result

    def _tool_mark_english_selection_for_later(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().mark_for_later(
            int(arguments["material_id"]), str(arguments["selected_text"]),
            context=str(arguments.get("context") or ""), page_number=int(arguments["page_number"]),
        )

    def _tool_save_english_usage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().save_usage(
            str(arguments["text"]), usage_kind=str(arguments.get("usage_kind") or "sentence"),
            context=str(arguments.get("context") or ""), material_id=arguments.get("material_id"),
            page_number=arguments.get("page_number"), user_note=str(arguments.get("user_note") or ""),
            agent_analysis=str(arguments.get("agent_analysis") or ""),
            writing_technique=str(arguments.get("writing_technique") or ""),
        )

    def _tool_save_english_grammar_encounter(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().save_grammar_encounter(
            int(arguments["material_id"]), str(arguments["selected_sentence"]),
            analysis=str(arguments.get("analysis") or ""), context=str(arguments.get("context") or ""),
            page_number=int(arguments["page_number"]),
        )

    def _tool_create_english_writing_practice(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().create_writing_practice(
            str(arguments["title"]), prompt=str(arguments.get("prompt") or ""),
            original_draft=str(arguments.get("original_draft") or ""), material_id=arguments.get("material_id"),
        )

    def _tool_add_english_writing_revision(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().add_writing_revision(
            int(arguments["practice_id"]), str(arguments["content"]),
            revision_kind=str(arguments.get("revision_kind") or "user"),
            diagnostic_feedback=str(arguments.get("diagnostic_feedback") or ""),
        )

    def _tool_add_english_audio_resource(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().add_audio_resource(
            str(arguments["title"]), str(arguments["path_or_url"]),
            material_id=arguments.get("material_id"), resource_kind=str(arguments.get("resource_kind") or "local_file"),
            notes=str(arguments.get("notes") or ""),
        )

    def _tool_record_english_shadowing_attempt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().record_shadowing_attempt(
            str(arguments["source_text"]), material_id=arguments.get("material_id"),
            user_recording_path=str(arguments.get("user_recording_path") or ""),
            note=str(arguments.get("note") or ""),
        )

    def _tool_update_english_chapter_progress(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().update_chapter_progress(
            int(arguments["chapter_id"]), str(arguments["progress_status"]),
            progress_note=str(arguments.get("progress_note") or ""),
            page_start=arguments.get("page_start"), page_end=arguments.get("page_end"),
        )

    def _tool_record_english_grammar_exercise_attempt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().record_grammar_exercise_attempt(
            int(arguments["material_id"]), str(arguments["question_reference"]),
            selected_answer=str(arguments.get("selected_answer") or ""),
            correct_answer=str(arguments.get("correct_answer") or ""),
            chapter_id=arguments.get("chapter_id"), source_page=arguments.get("source_page"),
            explanation_reference=str(arguments.get("explanation_reference") or ""),
            mistake_reason=str(arguments.get("mistake_reason") or ""),
            misconception_category=str(arguments.get("misconception_category") or ""),
        )

    def _tool_record_english_reading_training_attempt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().record_reading_training_attempt(
            int(arguments["material_id"]), str(arguments["passage_reference"]),
            duration_seconds=arguments.get("duration_seconds"),
            correct_count=arguments.get("correct_count"), question_count=arguments.get("question_count"),
            question_type=str(arguments.get("question_type") or ""),
            error_reason=str(arguments.get("error_reason") or ""), note=str(arguments.get("note") or ""),
            chapter_id=arguments.get("chapter_id"),
        )

    def _tool_list_english_deferred_lookups(self, arguments: dict[str, Any]) -> dict[str, Any]:
        resolved = arguments.get("resolved", False)
        rows = self._english_learning().deferred_lookups(
            resolved=resolved, keyword=str(arguments.get("keyword") or "")
        )
        return {"lookups": rows, "result_count": len(rows)}

    def _tool_resolve_english_deferred_lookup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._english_learning().resolve_deferred_lookup(int(arguments["lookup_id"]))

    def _tool_export_vocabulary_pdf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.vocabulary_manager.export_pdf(
            str(arguments.get("familiarity") or "all")
        )
        self.resources.authorize_generated_path(str(result.get("path") or ""))
        return result

    def _tool_list_ai_reference_materials(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return self.reference_materials.list_materials()

    def _tool_read_ai_reference_material(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.reference_materials.read_material(
            str(arguments.get("material_id") or "")
        )

    def _tool_compile_markdown_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from shared.scripts.markdown_renderer import compile_markdown

        result = compile_markdown(str(arguments.get("markdown") or ""))
        return result.summary(include_html=bool(arguments.get("include_html", False)))

    def _tool_audit_math_exposition(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from shared.scripts.ai_agent_quality import audit_math_exposition
        from shared.scripts.markdown_renderer import compile_markdown

        draft = str(arguments.get("draft") or "")
        audit = audit_math_exposition(
            draft,
            user_request=str(arguments.get("user_request") or ""),
            require_complete_proof=bool(arguments.get("require_complete_proof", False)),
        )
        audit["markdown_validation"] = compile_markdown(draft).summary()
        return audit

    @staticmethod
    def _online_course_row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def _tool_list_online_courses(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name = str(arguments.get("subject_name") or "").strip()
        project_dir, project = self.repository._project_directory(
            subject_name, arguments.get("project_ref") or ""
        )
        del project_dir
        rows = self.online_courses.courses_for_project(subject_name, str(project["collection_code"]))
        courses: list[dict[str, Any]] = []
        for row in rows:
            course = self._online_course_row(row)
            formal_pdf = (
                Path(str(course.get("storage_dir") or ""))
                / f"{course.get('course_code')}.pdf"
            )
            course["formal_pdf_path"] = (
                str(formal_pdf.resolve()) if formal_pdf.is_file() else str(formal_pdf)
            )
            course["formal_pdf_available"] = bool(
                formal_pdf.is_file() and formal_pdf.stat().st_size > 0
            )
            course["outline_lookup_tool"] = "get_online_course_lecture_outline"
            course["formal_pdf_search_tool"] = "search_online_course_lecture_pdf"
            if course["formal_pdf_available"]:
                self.resources.authorize_generated_path(str(formal_pdf))
            courses.append(course)
        return {
            "subject_name": subject_name,
            "project_code": str(project["collection_code"]),
            "courses": courses,
            "course_count": len(rows),
            "storage_root": str(self.online_courses.root),
        }

    def _tool_get_online_course_lecture_outline(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = self.online_courses.formal_lecture_outline_catalog(
            int(arguments.get("course_id") or 0)
        )
        if not bool(result.get("readback_verified")) or not int(
            result.get("outline_unit_count") or 0
        ):
            raise RuntimeError("Formal online-course outline readback failed.")
        self.resources.authorize_generated_path(str(result.get("formal_pdf_path") or ""))
        return dict(result)

    def _tool_search_online_course_lecture_pdf(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = self.online_courses.search_formal_lecture_pdf(
            int(arguments.get("course_id") or 0),
            str(arguments.get("query") or ""),
            search_terms=[str(item) for item in arguments.get("search_terms") or []],
            limit=int(arguments.get("limit") or 8),
        )
        if (
            not bool(result.get("readback_verified"))
            or not bool(result.get("full_document_scanned"))
            or int(result.get("searched_page_count") or 0)
            != int(result.get("pdf_page_count") or 0)
        ):
            raise RuntimeError("Formal online-course PDF search did not scan every page.")
        self.resources.authorize_generated_path(str(result.get("formal_pdf_path") or ""))
        return dict(result)

    def _tool_read_online_course_lecture_pdf_pages(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = self.online_courses.read_formal_lecture_pdf_pages(
            int(arguments.get("course_id") or 0),
            int(arguments.get("page_start") or 1),
            int(arguments.get("page_end") or arguments.get("page_start") or 1),
            max_chars=int(arguments.get("max_chars") or 120000),
        )
        if not bool(result.get("readback_verified")) or not int(
            result.get("returned_page_count") or 0
        ):
            raise RuntimeError("Formal online-course PDF page readback failed.")
        self.resources.authorize_generated_path(str(result.get("formal_pdf_path") or ""))
        return dict(result)

    def _tool_create_online_course(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject_name = str(arguments.get("subject_name") or "").strip()
        project_dir, project = self.repository._project_directory(
            subject_name, arguments.get("project_ref") or ""
        )
        project_keys = set(project.keys())
        pdf_name = str(project["pdf_filename"] or "") if "pdf_filename" in project_keys else ""
        project_pdf = project_dir / (pdf_name or f"{project['collection_code']}.pdf")
        row = self.online_courses.create_course(
            subject_name=subject_name,
            collection_id=int(project["id"]),
            collection_code=str(project["collection_code"]),
            project_name=str(project["name"]),
            project_dir=project_dir,
            project_pdf_path=project_pdf,
            title=str(arguments.get("title") or ""),
            lecturer=str(arguments.get("lecturer") or ""),
            course_domain="english" if self.discipline == "english" else self.discipline,
            course_track=str(arguments.get("course_track") or "general"),
        )
        return self._online_course_row(self.online_courses.course(int(row["id"])))

    def _tool_configure_online_course_outline_structure(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = self.online_courses.configure_course_outline_structure(
            int(arguments.get("course_id") or 0),
            include_subsections=bool(arguments.get("include_subsections")),
        )
        for key in ("database_backup", "course_json_path"):
            path = str(result.get(key) or "")
            if path:
                self.resources.authorize_generated_path(path)
        return dict(result)

    def _tool_list_online_course_reference_materials(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        course_id = int(arguments.get("course_id") or 0)
        rows = self.online_courses.reference_materials(course_id)
        return {
            "course_id": course_id,
            "materials": rows,
            "material_count": len(rows),
        }

    def _tool_import_online_course_reference_materials(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.import_reference_materials(
            int(arguments.get("course_id") or 0),
            [Path(str(path)) for path in arguments.get("paths") or []],
            emit,
        )
        if not bool(result.get("readback_verified")):
            raise RuntimeError("网课参考资料导入写后回读失败。")
        for item in result.get("imported") or []:
            stored_path = Path(str(item.get("stored_path") or ""))
            if stored_path.is_file():
                self.resources.authorize_generated_path(str(stored_path))
        return dict(result)

    def _tool_reanalyze_online_course_reference_materials(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.reanalyze_reference_materials(
            int(arguments.get("course_id") or 0),
            [int(value) for value in arguments.get("material_ids") or []] or None,
            emit,
        )
        if not bool(result.get("readback_verified")):
            raise RuntimeError("网课参考资料重新分析写后回读失败。")
        return dict(result)

    def _tool_prepare_online_course_recording(self, arguments: dict[str, Any]) -> dict[str, Any]:
        state = self.online_courses.arm_course(int(arguments.get("course_id") or 0))
        return {**state, "armed": self.online_courses.armed_course() == state}

    def _tool_get_online_course_media_engine_status(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return self.online_courses.media_engine.status()

    def _tool_list_video_episodes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from shared.scripts.quick_video_transcript import QuickVideoTranscriptService

        media_engine = self.online_courses.media_engine
        service = QuickVideoTranscriptService(
            yt_dlp_path=media_engine.yt_dlp_path,
            ffmpeg_path=media_engine.ffmpeg_path,
        )
        return service.episode_catalog(
            str(arguments.get("url") or ""),
            use_chrome_cookies=bool(arguments.get("use_chrome_cookies", False)),
        )

    def _tool_create_quick_video_transcript(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from shared.scripts.quick_video_transcript import QuickVideoTranscriptService

        emit = self._progress_callback or (lambda _message: None)
        media_engine = self.online_courses.media_engine
        service = QuickVideoTranscriptService(
            yt_dlp_path=media_engine.yt_dlp_path,
            ffmpeg_path=media_engine.ffmpeg_path,
        )
        backend = str(arguments.get("transcription_backend") or "groq")
        quality_cloud_transcriber = None
        bypass_proxy = False
        if backend == "groq":
            route_probe = getattr(media_engine, "quick_transcription_bypass_proxy", None)
            if callable(route_probe):
                bypass_proxy = bool(route_probe(emit))
            def quality_cloud_transcriber(
                audio_path: Path,
                language: str,
                prompt: str,
                progress: Callable[[str], None],
            ) -> Any:
                return media_engine.transcribe_file(
                    audio_path,
                    progress,
                    model="whisper-large-v3",
                    language=language,
                    prompt=prompt,
                    word_timestamps=True,
                    bypass_proxy=bypass_proxy,
                    max_retries=4,
                    retry_jitter_seconds=0.75,
                )
        result = service.run(
            str(arguments.get("url") or ""),
            use_chrome_cookies=bool(arguments.get("use_chrome_cookies", False)),
            model_name=str(arguments.get("whisper_model") or "small"),
            language=str(arguments.get("language") or ""),
            episode_number=int(arguments.get("episode_number") or 1),
            quality_cloud_transcriber=quality_cloud_transcriber,
            force_retranscribe=bool(arguments.get("force_retranscribe", False)),
            emit=emit,
        )
        payload = result.as_dict()
        if not bool(payload.get("readback_verified")):
            raise RuntimeError("快速视频文字稿写后回读失败。")
        for key in (
            "job_dir",
            "audio_path",
            "raw_transcript_path",
            "final_transcript_path",
            "evidence_path",
        ):
            value = str(payload.get(key) or "")
            if value:
                self.resources.authorize_generated_path(value)
        for key in ("audio_paths", "episode_transcript_paths"):
            for path in payload.get(key) or []:
                self.resources.authorize_generated_path(str(path))
        return payload

    def _tool_get_online_course_diagram_backend_status(
        self, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self.online_courses.diagram_backend_status()

    def _tool_render_online_course_diagrams(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.render_online_course_diagrams(
            int(arguments.get("course_id") or 0),
            [
                dict(item)
                for item in arguments.get("diagrams") or []
                if isinstance(item, dict)
            ],
            emit,
            purpose=str(arguments.get("purpose") or "agent_tool"),
            strict=True,
        )
        artifacts = [
            dict(item)
            for item in result.get("results") or []
            if isinstance(item, dict)
        ]
        if not artifacts or not bool(result.get("readback_verified")):
            raise RuntimeError("Online-course Agent diagram rendering failed readback verification.")
        for artifact in artifacts:
            if not bool(artifact.get("readback_verified")):
                raise RuntimeError("One Agent diagram artifact failed readback verification.")
            for key in ("source_path", "pdf_path", "artifact_manifest_path"):
                path = Path(str(artifact.get(key) or ""))
                if not path.is_file():
                    raise RuntimeError(f"Agent diagram artifact is missing: {key}")
                self.resources.authorize_generated_path(str(path))
        result["vector_artifacts"] = [
            {
                "kind": "vector_pdf",
                "path": str(artifact["pdf_path"]),
                "mime_type": "application/pdf",
                "diagram_id": str(artifact.get("diagram_id") or ""),
                "backend": str(artifact.get("backend") or ""),
                "source_sha256": str(artifact.get("source_sha256") or ""),
            }
            for artifact in artifacts[:4]
        ]
        result["visual_evidence"] = []
        return dict(result)

    def _tool_recompile_online_course_diagram_previews(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        return self.online_courses.recompile_subsection_diagram_previews(
            int(arguments.get("subsection_id") or 0),
            emit,
        )

    def _tool_get_online_course_processing_status(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        episode_id = int(arguments.get("episode_id") or 0) or None
        course_id = int(arguments.get("course_id") or 0) or None
        return self.online_courses.processing_status(
            episode_id=episode_id,
            course_id=course_id,
        )

    def _tool_get_online_course_retention_status(
        self, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self.online_courses.automatic_cleanup_status()

    def _tool_cleanup_online_course_expired_evidence(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        episode_id = int(arguments.get("episode_id") or 0) or None
        return self.online_courses.cleanup_expired_episode_evidence(
            episode_id=episode_id,
            dry_run=bool(arguments.get("dry_run", False)),
        )

    def _tool_install_online_course_media_engine(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        return self.online_courses.media_engine.install(emit)

    def _tool_configure_online_course_transcription(self, arguments: dict[str, Any]) -> dict[str, Any]:
        status = self.online_courses.media_engine.configure(
            str(arguments.get("provider") or ""),
            str(arguments.get("api_key") or "") or None,
        )
        return {**status, "secret_returned": False}

    def _tool_configure_online_course_continuation_overlap(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self.online_courses.update_settings(
            continuation_overlap_seconds=int(arguments.get("seconds") or 0)
        )

    def _tool_process_online_course_media(self, arguments: dict[str, Any]) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        return self.online_courses.prepare_course_transcripts(
            int(arguments.get("course_id") or 0), emit
        )

    def _tool_prepare_online_course_episode_package(self, arguments: dict[str, Any]) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.prepare_episode_chatgpt_package(
            int(arguments.get("episode_id") or 0),
            emit,
            normalize_formulas=bool(arguments.get("normalize_formulas", True)),
        )
        package_path = Path(str(result.get("package_path") or ""))
        if not package_path.is_file():
            raise RuntimeError("分集 ChatGPT 压缩包写后回读失败。")
        self.resources.authorize_generated_path(str(package_path))
        return dict(result)

    def _tool_prepare_online_course_subsection_package(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.rebuild_subsection_chatgpt_package(
            int(arguments.get("subsection_id") or 0),
            emit,
            normalize_formulas=bool(arguments.get("normalize_formulas", True)),
        )
        package_path = Path(str(result.get("package_path") or ""))
        if not package_path.is_file() or not bool(result.get("readback_verified")):
            raise RuntimeError("小节 ChatGPT 压缩包写后回读失败。")
        self.resources.authorize_generated_path(str(package_path))
        return dict(result)

    def _tool_prepare_online_course_packages(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        course_id = int(arguments.get("course_id") or 0)
        target_type = str(arguments.get("target_type") or "").strip()
        target_id = int(arguments.get("target_id") or 0)
        normalize_formulas = bool(arguments.get("normalize_formulas", True))
        self.online_courses.course(course_id)
        if target_type == "episode":
            matching_episode = next(
                (
                    row
                    for row in self.online_courses.episodes(course_id)
                    if int(row["id"]) == target_id
                ),
                None,
            )
            if matching_episode is None:
                raise ValueError("所选录制分集不属于指定网课。")
            episode_result = self.online_courses.prepare_episode_chatgpt_package(
                target_id,
                emit,
                normalize_formulas=normalize_formulas,
                refresh_subsection_packages=False,
            )
            result = {
                "course_id": course_id,
                "target_type": target_type,
                "target_id": target_id,
                "episode_results": [episode_result],
                "writing_unit_results": [],
                "readback_verified": bool(episode_result.get("readback_verified")),
            }
        elif target_type == "subsection":
            subsection = self.online_courses.subsection(target_id)
            if int(subsection["course_id"]) != course_id:
                raise ValueError("所选写作节不属于指定网课。")
            writing_unit_result = self.online_courses.rebuild_subsection_chatgpt_package(
                target_id,
                emit,
                normalize_formulas=normalize_formulas,
            )
            result = {
                "course_id": course_id,
                "target_type": target_type,
                "target_id": target_id,
                "episode_results": [],
                "writing_unit_results": [writing_unit_result],
                "readback_verified": bool(
                    writing_unit_result.get("readback_verified")
                ),
            }
        else:
            raise ValueError("target_type 必须是 episode 或 subsection。")
        if not bool(result.get("readback_verified")):
            raise RuntimeError("所选网课材料压缩包写后回读失败。")
        for item in [
            *list(result.get("episode_results") or []),
            *list(result.get("writing_unit_results") or []),
        ]:
            path = Path(str(item.get("package_path") or ""))
            if path.is_file():
                self.resources.authorize_generated_path(str(path))
        return dict(result)

    def _tool_delete_online_course_recording_segment(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = self.online_courses.delete_recording_segment(
            str(arguments.get("session_id") or ""),
            subsection_id=int(arguments.get("subsection_id") or 0),
        )
        recovery_dir = Path(str(result.get("recovery_dir") or ""))
        backup = Path(str(result.get("database_backup") or ""))
        if not recovery_dir.is_dir() or not backup.is_file():
            raise RuntimeError("录制段删除的恢复材料写后回读失败。")
        self.resources.authorize_generated_path(str(recovery_dir))
        self.resources.authorize_generated_path(str(backup))
        return dict(result)

    def _tool_configure_online_course_lecture_outline(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = self.online_courses.update_lecture_outline(
            int(arguments.get("course_id") or 0),
            list(arguments.get("chapters") or []),
            confirmation_source="authorized_ai_outline_revision",
        )
        outline_path = Path(str(result.get("outline_path") or ""))
        if not outline_path.is_file():
            raise RuntimeError("网课讲义目录写后回读失败。")
        self.resources.authorize_generated_path(str(outline_path))
        return dict(result)

    def _tool_import_online_course_episode_latex(self, arguments: dict[str, Any]) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.import_chatgpt_latex(
            int(arguments.get("course_id") or 0),
            str(arguments.get("latex_source") or ""),
            emit,
            episode_id=int(arguments.get("episode_id") or 0),
            segment_id=int(arguments.get("segment_id") or 0),
            compile_when_complete=False,
        )
        imported = Path(str(result.get("imported_tex") or ""))
        if not imported.is_file():
            raise RuntimeError("分集 ChatGPT LaTeX 写后回读失败。")
        self.resources.authorize_generated_path(str(imported))
        pdf_path = Path(str(result.get("pdf_path") or ""))
        if pdf_path.is_file():
            self.resources.authorize_generated_path(str(pdf_path))
        return dict(result)

    def _tool_import_online_course_recording_segment_latex(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        session_ids = list(arguments.get("session_ids") or [])
        if not session_ids:
            session_ids = [str(arguments.get("session_id") or "")]
        result = self.online_courses.import_recording_segments_latex(
            int(arguments.get("subsection_id") or 0),
            [str(value) for value in session_ids],
            str(arguments.get("latex_source") or ""),
            emit,
            compile_full_course=True,
        )
        imported_rows = list(result.get("imported_recording_segments") or [])
        if not bool(result.get("readback_verified")) or not imported_rows:
            raise RuntimeError("录制段 ChatGPT LaTeX 正式写入回读失败。")
        for row in imported_rows:
            imported_path = Path(str(row.get("imported_tex") or ""))
            if not imported_path.is_file():
                raise RuntimeError("录制段 ChatGPT LaTeX 正式文件不存在。")
            self.resources.authorize_generated_path(str(imported_path))
        pdf_path = Path(str(result.get("pdf_path") or ""))
        if pdf_path.is_file():
            self.resources.authorize_generated_path(str(pdf_path))
        return dict(result)

    def _tool_get_online_course_subsection_latex(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = self.online_courses.subsection_latex_editor_payload(
            int(arguments.get("subsection_id") or 0)
        )
        if not bool(result.get("readback_verified")) or not str(
            result.get("latex_source") or ""
        ).strip():
            raise RuntimeError("网课小节 LaTeX 读取或回读失败。")
        return dict(result)

    def _tool_compile_online_course_subsection_latex_preview(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.compile_subsection_latex_preview(
            int(arguments.get("subsection_id") or 0),
            str(arguments.get("latex_source") or ""),
            emit,
        )
        pdf_path = Path(str(result.get("pdf_path") or ""))
        source_path = Path(str(result.get("source_path") or ""))
        if (
            not pdf_path.is_file()
            or pdf_path.stat().st_size <= 0
            or not source_path.is_file()
            or not bool(result.get("readback_verified"))
        ):
            raise RuntimeError("网课小节隔离预览编译后的文件回读失败。")
        self.resources.authorize_generated_path(str(pdf_path))
        self.resources.authorize_generated_path(str(source_path))
        log_path = Path(str(result.get("log_path") or ""))
        if log_path.is_file():
            self.resources.authorize_generated_path(str(log_path))
        return dict(result)

    def _tool_edit_online_course_subsection_latex(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.save_subsection_latex_override(
            int(arguments.get("subsection_id") or 0),
            str(arguments.get("latex_source") or ""),
            emit,
        )
        override_path = Path(str(result.get("override_path") or ""))
        pdf_path = Path(str(result.get("pdf_path") or ""))
        manifest_path = Path(str(result.get("merge_manifest") or ""))
        if (
            not override_path.is_file()
            or not pdf_path.is_file()
            or pdf_path.stat().st_size <= 0
            or not manifest_path.is_file()
            or not bool(result.get("readback_verified"))
        ):
            raise RuntimeError("网课小节正式精修后的覆盖稿、合并清单或 PDF 回读失败。")
        for path in (override_path, pdf_path, manifest_path):
            self.resources.authorize_generated_path(str(path))
        return dict(result)

    def _tool_merge_online_course_same_subsections(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.merge_same_subsection_latex(
            int(arguments.get("course_id") or 0), emit
        )
        pdf_path = Path(str(result.get("pdf_path") or ""))
        manifest = Path(str(result.get("merge_manifest") or ""))
        if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
            raise RuntimeError("同一小节合并后的正式 PDF 写后回读失败。")
        if not manifest.is_file() or not bool(result.get("readback_verified")):
            raise RuntimeError("同一小节合并清单写后回读失败。")
        self.resources.authorize_generated_path(str(pdf_path))
        self.resources.authorize_generated_path(str(manifest))
        return dict(result)

    def _tool_compile_online_course_pdf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        emit = self._progress_callback or (lambda _message: None)
        result = self.online_courses.build_course_pdf(
            int(arguments.get("course_id") or 0),
            emit,
            force_full_rebuild=True,
        )
        pdf_path = Path(str(result.pdf_path or ""))
        if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
            raise RuntimeError("网课讲义 PDF 编译后写后回读失败。")
        self.resources.authorize_generated_path(str(pdf_path))
        return {
            "course_id": result.course_id,
            "course_code": result.course_code,
            "pdf_path": str(pdf_path),
            "size_bytes": pdf_path.stat().st_size,
            "readback_verified": True,
        }

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        operation_id = ""
        try:
            registered_spec = AI_OPERATION_REGISTRY.spec(name)
            mutating_tools = {
                item["name"]
                for item in AI_OPERATION_REGISTRY.catalog()
                if item["access"] in {FORMAL_WRITE, "destructive"}
            }
            if name in mutating_tools and not self._write_authorized:
                return {
                    "ok": False,
                    "error": "本轮用户没有明确授权写入、修改或重建项目；程序已阻止该工具。",
                }
            if name in mutating_tools and self._mutation_rejected:
                return {
                    "ok": False,
                    "error": "用户已经拒绝本轮写入操作；本轮不会再次弹出确认或执行其他写入。",
                }
            if name in {"edit_project_tex", "insert_tikz_figure"}:
                read_key = (
                    str(arguments.get("subject_name") or ""),
                    str(arguments.get("project_ref") or ""),
                    Path(str(arguments.get("relative_path") or "")).as_posix(),
                )
                if read_key not in self._successful_project_reads:
                    return {
                        "ok": False,
                        "error": "写入前必须先用 read_project_file 成功读取同一项目中的同一目标文件；程序已停止盲写。",
                    }
            if name == "build_project_pdf" and self._project_mutation_succeeded:
                return {
                    "ok": False,
                    "error": "本轮成功的 TeX 写入已经生成并替换正式项目 PDF；已阻止重复编译。",
                }
            if name == "edit_math_workspace_files":
                for edit in arguments.get("edits") or []:
                    if not isinstance(edit, dict):
                        continue
                    target = self.workspace_editor._target(
                        str(edit.get("path") or ""),
                        allow_create=str(edit.get("operation") or "") == "create_or_replace",
                    )
                    default_workspace = self.workspace_editor.default_workspace
                    if default_workspace not in target.parents and target != default_workspace:
                        if not self.resources.path_is_authorized(str(target)):
                            return {
                                "ok": False,
                                "error": "在默认 MathWorkspace 之外创建新文件时，用户必须在当前消息中明确写出绝对路径。",
                            }
                    if target.is_file() and str(target).casefold() not in self._successful_local_reads:
                        return {
                            "ok": False,
                            "error": f"跨文件事务修改已有文件前必须先用 read_local_file 读取同一路径：{target}",
                        }
            if name == "apply_workspace_patch":
                for edit in arguments.get("edits") or []:
                    if not isinstance(edit, dict):
                        continue
                    target = self.workspace_tools._resolve(
                        str(edit.get("path") or ""),
                        allow_missing=str(edit.get("operation") or "") == "create_or_replace",
                    )
                    if target.is_file() and str(target).casefold() not in self._successful_local_reads:
                        return {
                            "ok": False,
                            "error": f"仓库补丁修改已有文件前必须先用 read_workspace_files 读取同一路径：{target}",
                        }
            if name == "run_workspace_sqlite_migration":
                target = self.workspace_tools._resolve(str(arguments.get("path") or ""))
                if str(target).casefold() not in self._successful_workspace_database_reads:
                    return {
                        "ok": False,
                        "error": "数据库迁移前必须先用 inspect_workspace_sqlite 检查同一数据库。",
                    }
            if name == "compile_standalone_tex":
                target = self.workspace_editor._target(str(arguments.get("path") or ""), allow_create=False)
                if str(target).casefold() not in self._successful_local_reads:
                    return {"ok": False, "error": "独立 TeX 编译前必须先读取同一文件。"}
            if name in mutating_tools:
                if self._mutation_approval_callback is None:
                    return {
                        "ok": False,
                        "error": "当前没有可用的修改确认通道；程序已阻止写入，未修改任何本地文件。",
                    }
                preview = self.mutation_preview(name, arguments)
                operation = self.operation_journal.begin(self._operation_task_id, name, preview)
                operation_id = operation.id
                approved = bool(self._mutation_approval_callback(preview))
                if not approved:
                    self._mutation_rejected = True
                    self.operation_journal.finish(operation_id, "rejected", {"reason": "user_rejected"})
                    return {
                        "ok": False,
                        "error": "用户在操作预览窗口中拒绝了这次写入或生成操作；没有修改任何文件。",
                    }
                self.operation_journal.finish(operation_id, "executing")
            if registered_spec is not None:
                result = getattr(self, registered_spec.handler_name)(dict(arguments))
            elif name in {"symbolic_math", "numerical_math", "verify_formula", "find_counterexample", "plot_math_function"}:
                result = self.compute_manager.run_python(
                    name, dict(arguments), self._progress_callback, timeout=20
                )
                if name == "plot_math_function":
                    for artifact in result.get("artifacts") or []:
                        if isinstance(artifact, dict) and artifact.get("absolute_path"):
                            self.resources.authorize_generated_path(str(artifact["absolute_path"]))
                    pdf_path = str(result.get("pdf_path") or "")
                    if pdf_path:
                        result["visual_validation"] = validate_math_figure(pdf_path)
            elif name == "mathematica_compute":
                result = self.compute_manager.run_mathematica(
                    dict(arguments), self._progress_callback, timeout=70
                )
            elif name == "mathematica_plot":
                result = self.compute_manager.run_mathematica_plot(
                    dict(arguments), self._progress_callback, timeout=90
                )
                for artifact in result.get("artifacts") or []:
                    if isinstance(artifact, dict) and artifact.get("absolute_path"):
                        self.resources.authorize_generated_path(str(artifact["absolute_path"]))
                preview_path = str(result.get("pdf_path") or result.get("png_path") or "")
                if preview_path:
                    result["visual_validation"] = validate_math_figure(preview_path)
            elif name == "dual_verify_math":
                result = self.compute_manager.dual_verify(dict(arguments), self._progress_callback)
            elif name == "lean_check":
                if self.lean_manager is None:
                    return {"ok": False, "error": "物理助手不提供 Lean 工具。"}
                result = self.lean_manager.check(
                    str(arguments.get("path") or ""),
                    self._progress_callback,
                    int(arguments.get("timeout_seconds") or 90),
                )
                self.resources.authorize_generated_path(str(result.get("path") or ""))
                if not result.get("verified"):
                    return {
                        "ok": False,
                        "error": "Lean 内核未接受该证明。请根据 diagnostics 修改后重新核验。",
                        "data": result,
                    }
            elif name == "validate_math_figure":
                if not self.resources.path_is_authorized(str(arguments.get("path") or "")):
                    raise ValueError(
                        "只能视觉检查用户明确给出的文件、本轮搜索结果或本轮工具生成的文件。"
                    )
                result = validate_math_figure(
                    str(arguments.get("path") or ""),
                    page_number=int(arguments.get("page_number") or 1),
                    expected_labels=[str(item) for item in arguments.get("expected_labels") or []],
                )
            elif name == "render_math_figure_preview":
                result = self.workspace_editor.render_math_figure_preview(
                    str(arguments.get("tikz_code") or ""),
                    str(arguments.get("caption") or ""),
                    self._progress_callback,
                )
                self.resources.authorize_generated_path(str(result.get("pdf_path") or ""))
            elif name == "semantic_search":
                requested_subject = str(arguments.get("subject_name") or self._scope_subject or "")
                requested_project = str(arguments.get("project_ref") or self._scope_project or "")
                if requested_project:
                    self._ensure_project_in_scope(requested_subject, requested_project)
                result = self.semantic_index.search(
                    str(arguments.get("query") or ""),
                    kinds=[str(item) for item in arguments.get("kinds") or []],
                    subject_name=requested_subject,
                    project_ref=requested_project,
                    limit=int(arguments.get("limit") or 12),
                )
                for item in result.get("results", []):
                    if item.get("path"):
                        self.resources.authorize_generated_path(str(item["path"]))
            elif name == "get_available_project_tools":
                catalog = [
                    item
                    for item in AI_OPERATION_REGISTRY.catalog()
                    if self.discipline == "math" or item["name"] != "lean_check"
                ]
                result = {
                    "tools": [
                        {
                            **item,
                            "callable_this_turn": (
                                item["access"] not in {FORMAL_WRITE, "destructive"}
                                or self._write_authorized
                            ),
                            "authorization_required": (
                                item["access"] in {FORMAL_WRITE, "destructive"}
                                and not self._write_authorized
                            ),
                        }
                        for item in catalog
                    ],
                    "registered_tool_count": len(catalog),
                    "registry_validation": "ok",
                    "rule": (
                        "全部注册能力始终可发现。read_only 和 derived_write 可直接调用；"
                        "formal_write 与 destructive 只有在用户明确授权后执行，但不会从能力目录隐藏。"
                    ),
                }
            elif name == "get_provider_account_usage":
                if APP_PATHS.public_release:
                    result = {
                        "available": False,
                        "message": "公开版本未安装 Provider 账户用量兼容层。",
                    }
                else:
                    try:
                        from shared.scripts.ai_agent_account_usage import load_usage_snapshot
                    except ImportError:
                        snapshot = {}
                    else:
                        snapshot = load_usage_snapshot()
                    result = (
                        {"available": True, **snapshot}
                        if snapshot
                        else {
                            "available": False,
                            "message": "尚无已同步的 Provider 账户数据；请先完成兼容层配置。",
                        }
                    )
            elif name == "get_library_overview":
                result = self.repository.library_overview()
            elif name == "list_subjects":
                result = self.repository.list_subjects()
            elif name == "list_projects":
                result = self.repository.list_projects(str(arguments.get("subject_name") or ""))
            elif name == "list_textbooks":
                result = self.repository.list_textbooks(
                    str(arguments.get("subject_name") or ""),
                    str(arguments.get("query") or ""),
                    int(arguments.get("limit") or 50),
                )
            elif name == "get_textbook_dataset_status":
                result = self.semantic_index.textbook_dataset_status(
                    str(arguments.get("subject_name") or "")
                )
            elif name == "search_textbook_content":
                result = self.semantic_index.search_textbook_content(
                    str(arguments.get("query") or ""),
                    textbook_refs=[str(item) for item in arguments.get("textbook_refs") or []][:5],
                    subject_name=str(arguments.get("subject_name") or ""),
                    limit=int(arguments.get("limit") or 8),
                )
                for item in result.get("results", []):
                    if item.get("path"):
                        self.resources.authorize_generated_path(str(item["path"]))
            elif name == "search_problems":
                result = self.repository.search_problems(
                    str(arguments.get("query") or ""),
                    [self._scope_subject] if self._scope_problem_ids is not None else arguments.get("subject_names") or [],
                    int(arguments.get("limit") or 20),
                )
                result = self._filter_problem_page(result, "results")
            elif name == "resolve_problem_reference":
                requested_subject = str(arguments.get("subject_name") or self._scope_subject or "")
                requested_project = str(arguments.get("project_ref") or self._scope_project or "")
                if requested_project:
                    self._ensure_project_in_scope(requested_subject, requested_project)
                result = self.repository.resolve_problem_reference(
                    str(arguments.get("hint") or ""),
                    requested_subject,
                    requested_project,
                    int(arguments.get("limit") or 6),
                )
                result = self._filter_problem_page(result, "results")
            elif name == "get_problem":
                result = self.repository.get_problem(
                    str(arguments.get("subject_name") or ""),
                    str(arguments.get("problem_ref") or ""),
                )
                self._ensure_problem_in_scope(result)
            elif name == "get_problem_evidence_batch":
                subject_name = str(arguments.get("subject_name") or "")
                refs = [str(item) for item in arguments.get("problem_refs") or []][:8]
                if not refs:
                    raise ValueError("请至少提供一道题目编号。")
                include_solution = bool(arguments.get("include_solution"))
                max_chars = max(800, min(int(arguments.get("max_chars_per_problem") or 2800), 6000))
                problems: list[dict[str, Any]] = []
                for problem_ref in refs:
                    problem = self.repository.get_problem(subject_name, problem_ref)
                    self._ensure_problem_in_scope(problem)
                    compact = {
                        key: problem.get(key)
                        for key in (
                            "subject_name", "problem_id", "problem_code", "title",
                            "chapter_code", "chapter_name", "section_code", "section_name",
                            "problem_statement", "summary", "main_method", "notes",
                        )
                        if problem.get(key) not in (None, "", [], {})
                    }
                    for key in ("problem_statement", "summary", "main_method", "notes"):
                        if key in compact:
                            compact[key] = str(compact[key])[:max_chars]
                    if include_solution and problem.get("solution"):
                        compact["solution_excerpt"] = str(problem["solution"])[:max_chars]
                    problems.append(compact)
                result = {"subject_name": subject_name, "problem_count": len(problems), "problems": problems}
            elif name == "list_subject_problems":
                result = self.repository.list_subject_problems(
                    str(arguments.get("subject_name") or ""),
                    int(arguments.get("offset") or 0),
                    int(arguments.get("limit") or 50),
                )
                result = self._filter_problem_page(result, "problems")
            elif name == "get_project_problems":
                requested_subject = str(arguments.get("subject_name") or "")
                requested_project = str(arguments.get("project_ref") or "")
                self._ensure_project_in_scope(requested_subject, requested_project)
                result = self.repository.get_project_problems(
                    requested_subject,
                    requested_project,
                    int(arguments.get("limit") or 40),
                )
            elif name == "list_project_files":
                self._ensure_project_in_scope(
                    str(arguments.get("subject_name") or ""), str(arguments.get("project_ref") or "")
                )
                result = self.repository.list_project_files(
                    str(arguments.get("subject_name") or ""), str(arguments.get("project_ref") or "")
                )
            elif name == "read_project_file":
                self._ensure_project_in_scope(
                    str(arguments.get("subject_name") or ""), str(arguments.get("project_ref") or "")
                )
                result = self.repository.read_project_file(
                    str(arguments.get("subject_name") or ""),
                    str(arguments.get("project_ref") or ""),
                    str(arguments.get("relative_path") or ""),
                    int(arguments.get("max_chars") or 50000),
                )
                self._successful_project_reads.add(
                    (
                        str(arguments.get("subject_name") or ""),
                        str(arguments.get("project_ref") or ""),
                        Path(str(arguments.get("relative_path") or "")).as_posix(),
                    )
                )
            elif name == "edit_project_tex":
                self._ensure_project_in_scope(
                    str(arguments.get("subject_name") or ""), str(arguments.get("project_ref") or "")
                )
                result = self.tex_editor.edit_tex(
                    str(arguments.get("subject_name") or ""),
                    str(arguments.get("project_ref") or ""),
                    str(arguments.get("relative_path") or ""),
                    str(arguments.get("operation") or ""),
                    str(arguments.get("anchor_text") or ""),
                    str(arguments.get("new_tex") or ""),
                )
                self.resources.authorize_generated_path(str(result.get("project_pdf_path") or ""))
                self._project_mutation_succeeded = True
            elif name == "insert_tikz_figure":
                self._ensure_project_in_scope(
                    str(arguments.get("subject_name") or ""), str(arguments.get("project_ref") or "")
                )
                result = self.tex_editor.insert_tikz_figure(
                    str(arguments.get("subject_name") or ""),
                    str(arguments.get("project_ref") or ""),
                    str(arguments.get("relative_path") or ""),
                    str(arguments.get("anchor_text") or ""),
                    str(arguments.get("position") or ""),
                    str(arguments.get("tikz_code") or ""),
                    str(arguments.get("caption") or ""),
                    str(arguments.get("label") or ""),
                )
                if result.get("project_pdf_path"):
                    self.resources.authorize_generated_path(str(result["project_pdf_path"]))
                    result["visual_validation"] = validate_pdf_near_text(
                        str(result["project_pdf_path"]),
                        str(arguments.get("caption") or ""),
                    )
                self._project_mutation_succeeded = True
            elif name == "build_project_pdf":
                self._ensure_project_in_scope(
                    str(arguments.get("subject_name") or ""), str(arguments.get("project_ref") or "")
                )
                result = self.tex_editor.build_project_pdf(
                    str(arguments.get("subject_name") or ""),
                    str(arguments.get("project_ref") or ""),
                )
                self.resources.authorize_generated_path(str(result.get("project_pdf_path") or ""))
            elif name == "web_search":
                result = self.resources.web_search(
                    str(arguments.get("query") or ""),
                    int(arguments.get("limit") or 8),
                    [str(item) for item in arguments.get("preferred_domains") or []],
                    str(arguments.get("alternate_query") or ""),
                )
            elif name == "search_math_papers":
                result = self.paper_accessor.search(
                    str(arguments.get("query") or ""),
                    [str(item) for item in arguments.get("sources") or []] or None,
                    str(arguments.get("category") or "all"),
                    int(arguments["year_from"]) if arguments.get("year_from") is not None else None,
                    int(arguments["year_to"]) if arguments.get("year_to") is not None else None,
                    str(arguments.get("sort") or "relevance"),
                    str(arguments.get("publication_type") or "journal_article"),
                    int(arguments.get("limit") or 8),
                )
            elif name == "read_math_paper":
                result = self.paper_accessor.read(
                    str(arguments.get("identifier") or ""),
                    int(arguments.get("page_start") or 1),
                    int(arguments.get("page_end") or 8),
                    int(arguments.get("max_chars") or 60000),
                )
                if result.get("pdf_path"):
                    self.resources.authorize_generated_path(str(result["pdf_path"]))
            elif name == "discover_public_math_resources":
                result = self.resources.discover_public_math_resources(
                    str(arguments.get("query") or ""),
                    str(arguments.get("alternate_query") or ""),
                    [str(item) for item in arguments.get("resource_types") or []],
                    int(arguments.get("limit") or 6),
                )
            elif name == "fetch_url":
                result = self.resources.fetch_url(
                    str(arguments.get("url") or ""), int(arguments.get("max_chars") or 80000)
                )
            elif name == "search_local_files":
                result = self.resources.search_local_files(
                    str(arguments.get("query") or ""),
                    arguments.get("extensions") or [],
                    int(arguments.get("limit") or 30),
                )
            elif name == "list_workspace_tree":
                result = self.workspace_tools.list_tree(
                    str(arguments.get("path") or ""),
                    int(arguments.get("depth") or 3),
                    int(arguments.get("limit") or 800),
                )
            elif name == "search_workspace_text":
                result = self.workspace_tools.search_text(
                    str(arguments.get("query") or ""),
                    [str(item) for item in arguments.get("paths") or []],
                    [str(item) for item in arguments.get("extensions") or []],
                    int(arguments.get("limit") or 120),
                    bool(arguments.get("case_sensitive")),
                )
            elif name == "read_workspace_files":
                result = self.workspace_tools.read_files(
                    [dict(item) for item in arguments.get("requests") or [] if isinstance(item, dict)],
                    int(arguments.get("max_total_chars") or 240000),
                )
                self._successful_local_reads.update(
                    str(Path(str(item.get("path") or "")).resolve()).casefold()
                    for item in result.get("files") or [] if isinstance(item, dict) and item.get("path")
                )
            elif name == "inspect_git_changes":
                result = self.workspace_tools.inspect_git(
                    [str(item) for item in arguments.get("paths") or []],
                    bool(arguments.get("include_diff", True)),
                )
            elif name == "inspect_workspace_sqlite":
                result = self.workspace_tools.inspect_sqlite(
                    str(arguments.get("path") or ""),
                    str(arguments.get("query") or ""),
                    int(arguments.get("limit") or 100),
                )
                self._successful_workspace_database_reads.add(
                    str(Path(str(result.get("path") or "")).resolve()).casefold()
                )
            elif name == "apply_workspace_patch":
                result = self.workspace_tools.apply_patch(
                    [dict(item) for item in arguments.get("edits") or [] if isinstance(item, dict)]
                )
                self._successful_local_reads.update(
                    str(Path(path).resolve()).casefold() for path in result.get("changed_files") or []
                )
            elif name == "manage_workspace_files":
                result = self.workspace_tools.manage_files(
                    [dict(item) for item in arguments.get("operations") or [] if isinstance(item, dict)]
                )
            elif name == "run_workspace_command":
                result = self.workspace_tools.run_command(
                    str(arguments.get("executable") or ""),
                    [str(item) for item in arguments.get("arguments") or []],
                    str(arguments.get("working_directory") or ""),
                    int(arguments.get("timeout_seconds") or 180),
                )
                if int(result.get("exit_code", -1)) != 0:
                    response = {
                        "ok": False,
                        "error": f"本地命令退出码为 {result.get('exit_code')}，验证未通过。",
                        "data": result,
                    }
                    if operation_id:
                        self.operation_journal.finish(operation_id, "failed", response)
                    return response
            elif name == "read_workspace_command_log":
                result = self.workspace_tools.read_command_log(
                    str(arguments.get("path") or ""), int(arguments.get("max_chars") or 100000)
                )
            elif name == "run_workspace_sqlite_migration":
                result = self.workspace_tools.migrate_sqlite(
                    str(arguments.get("path") or ""), str(arguments.get("sql") or "")
                )
            elif name == "list_local_directory":
                result = self.resources.list_local_directory(
                    str(arguments.get("path") or ""), int(arguments.get("limit") or 200)
                )
            elif name == "read_local_file":
                result = self.resources.read_local_file(
                    str(arguments.get("path") or ""), int(arguments.get("max_chars") or 100000)
                )
                self._successful_local_reads.add(str(Path(str(result.get("path") or "")).resolve()).casefold())
            elif name == "read_local_pdf_pages":
                result = self.resources.read_local_pdf_pages(
                    str(arguments.get("path") or ""),
                    int(arguments.get("page_start") or 1),
                    int(arguments.get("page_end") or arguments.get("page_start") or 1),
                    int(arguments.get("max_chars") or 120000),
                )
            elif name == "read_local_pdf_evidence_batch":
                requests = [dict(item) for item in arguments.get("requests") or [] if isinstance(item, dict)][:5]
                if not requests:
                    raise ValueError("请至少提供一个 PDF 页段。")
                evidence: list[dict[str, Any]] = []
                for request in requests:
                    page_start = int(request.get("page_start") or 1)
                    page_end = int(request.get("page_end") or page_start)
                    if page_end - page_start + 1 > 8:
                        raise ValueError("批量读取中每个 PDF 页段最多 8 页。")
                    item = self.resources.read_local_pdf_pages(
                        str(request.get("path") or ""),
                        page_start,
                        page_end,
                        max(1000, min(int(request.get("max_chars") or 6000), 12000)),
                    )
                    evidence.append(item)
                result = {"evidence_count": len(evidence), "evidence": evidence}
            elif name == "edit_math_workspace_files":
                result = self.workspace_editor.apply_transaction(
                    [dict(item) for item in arguments.get("edits") or [] if isinstance(item, dict)]
                )
                self._successful_local_reads.update(
                    str(Path(path).resolve()).casefold() for path in result.get("changed_files", [])
                )
                for path in result.get("changed_files", []):
                    self.resources.authorize_generated_path(str(path))
            elif name == "compile_standalone_tex":
                result = self.workspace_editor.compile_standalone_tex(
                    str(arguments.get("path") or ""), self._progress_callback
                )
                self.resources.authorize_generated_path(str(result.get("pdf_path") or ""))
            else:
                return {"ok": False, "error": f"未知或未授权的工具：{name}"}
            response = {"ok": True, "data": result}
            if operation_id:
                self.operation_journal.finish(operation_id, "completed", response)
            return response
        except (ValueError, RuntimeError, OSError, sqlite3.Error, json.JSONDecodeError, subprocess.SubprocessError) as error:
            if operation_id:
                self.operation_journal.finish(operation_id, "failed", {"error": str(error)})
            return {"ok": False, "error": str(error)}


# Kept as an import-compatible alias for older callers and saved sessions.
ReadOnlyToolExecutor = AiAgentToolExecutor

_REGISTRY_ERRORS = AI_OPERATION_REGISTRY.validate(AiAgentToolExecutor)
if _REGISTRY_ERRORS:
    raise RuntimeError("AI 操作注册表不完整：" + "；".join(_REGISTRY_ERRORS))
