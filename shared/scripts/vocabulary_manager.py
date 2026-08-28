from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parents[2]
LEGACY_GLOBAL_DATABASE = ROOT_DIR / "shared" / "vocabulary.db"


def normalize_vocabulary_workspace(value: str | None = None) -> str:
    workspace = str(value or os.environ.get("STUDY_BANK_WORKSPACE", "math")).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", workspace):
        return "math"
    return workspace


def workspace_vocabulary_paths(
    workspace: str | None = None,
    *,
    root_dir: Path = ROOT_DIR,
) -> tuple[Path, Path, Path]:
    """Return isolated database, backup, and export paths for one workspace."""

    scope = normalize_vocabulary_workspace(workspace)
    base = Path(root_dir).resolve() / "shared" / "vocabularies" / scope
    return base / "vocabulary.db", base / "backups", base / "exports"


DEFAULT_DATABASE, DEFAULT_BACKUP_DIR, DEFAULT_EXPORT_DIR = workspace_vocabulary_paths("math")

VOCABULARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS vocabulary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL COLLATE NOCASE,
    part_of_speech TEXT NOT NULL COLLATE NOCASE DEFAULT '',
    definition TEXT NOT NULL DEFAULT '',
    familiarity TEXT NOT NULL DEFAULT 'unfamiliar',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    entry_kind TEXT NOT NULL DEFAULT 'word',
    pronunciation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(term, part_of_speech)
);
CREATE INDEX IF NOT EXISTS idx_vocabulary_entries_term
ON vocabulary_entries(term COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_vocabulary_entries_search
ON vocabulary_entries(term COLLATE NOCASE, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_vocabulary_entries_updated
ON vocabulary_entries(updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_vocabulary_entries_familiarity
ON vocabulary_entries(familiarity, term COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS vocabulary_senses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vocabulary_entry_id INTEGER NOT NULL,
    definition_zh TEXT NOT NULL DEFAULT '',
    definition_en TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT 'general',
    register_note TEXT NOT NULL DEFAULT '',
    collocations TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'user',
    source_material_id INTEGER,
    source_page INTEGER,
    confidence TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(vocabulary_entry_id) REFERENCES vocabulary_entries(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vocabulary_senses_entry
ON vocabulary_senses(vocabulary_entry_id, domain, id);

CREATE TABLE IF NOT EXISTS vocabulary_encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vocabulary_entry_id INTEGER,
    sense_id INTEGER,
    surface_form TEXT NOT NULL,
    selected_text TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',
    source_domain TEXT NOT NULL DEFAULT 'general',
    material_id INTEGER,
    material_code TEXT NOT NULL DEFAULT '',
    material_title TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    page_number INTEGER,
    anchor_y REAL NOT NULL DEFAULT 0,
    event_type TEXT NOT NULL DEFAULT 'lookup',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(vocabulary_entry_id) REFERENCES vocabulary_entries(id) ON DELETE SET NULL,
    FOREIGN KEY(sense_id) REFERENCES vocabulary_senses(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_vocabulary_encounters_entry_time
ON vocabulary_encounters(vocabulary_entry_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_vocabulary_encounters_material_page
ON vocabulary_encounters(material_id, page_number, created_at DESC);
"""


_PDF_DASH_TRANSLATION = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "’": "'",
        "‘": "'",
        "`": "'",
    }
)
_PDF_SELECTION_EDGE_RE = re.compile(
    r"^[\s\"'“”‘’`*_.,;:!?¡¿…·•()\[\]{}<>《》【】]+"
    r"|[\s\"'“”‘’`*_.,;:!?¡¿…·•()\[\]{}<>《》【】]+$"
)


def _merge_semicolon_segments(existing: str, incoming: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in (existing, incoming):
        for part in re.split(r"[；;\n]+", str(value or "")):
            clean = re.sub(r"\s+", " ", part).strip(" ，,。.;；")
            if not clean:
                continue
            key = re.sub(r"[\s，,。.;；:：]+", "", clean).casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(clean)
    return "；".join(merged)


def _pdf_vocabulary_key(value: str, *, strip_diacritics: bool = False) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_PDF_DASH_TRANSLATION)
    text = text.replace("\u00ad", "").casefold()
    if strip_diacritics:
        text = "".join(
            char
            for char in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(char)
        )
    text = re.sub(r"\s+", " ", text)
    return _PDF_SELECTION_EDGE_RE.sub("", text).strip()


def _pdf_vocabulary_compact_key(value: str, *, strip_diacritics: bool = False) -> str:
    return "".join(
        char
        for char in _pdf_vocabulary_key(value, strip_diacritics=strip_diacritics)
        if char.isalnum()
    )


def pdf_vocabulary_selection_candidates(value: str) -> list[str]:
    """Normalize common PDF text-layer artifacts without guessing a translation."""

    text = unicodedata.normalize("NFKC", str(value or "")).replace("\u00ad", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    variants = [
        re.sub(r"\s*\n\s*", " ", text),
        re.sub(r"(?<=\w)[‐‑‒–—-]\s*\n\s*(?=\w)", "-", text),
        re.sub(r"(?<=\w)[‐‑‒–—-]\s*\n\s*(?=\w)", "", text),
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        clean = re.sub(r"\s+", " ", variant.translate(_PDF_DASH_TRANSLATION))
        clean = _PDF_SELECTION_EDGE_RE.sub("", clean).strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            candidates.append(clean)
    return candidates


_PDF_AGENT_TERM_RE = re.compile(
    r"^[A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f0-9'\u2019.\-]*"
    r"(?:[ \t]+[A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f0-9'\u2019.\-]*)*$"
)


def normalize_pdf_vocabulary_agent_term(
    proposed_term: str,
    selected_term: str = "",
) -> str:
    """Validate an Agent's English dictionary headword before PDF import.

    The selected PDF surface form may be inflected (for example ``running``),
    while the Agent is expected to return its canonical headword (``run``).
    This helper deliberately validates script and shape only; grammatical
    lemmatization belongs to the language model and the surrounding context.
    """

    candidates = pdf_vocabulary_selection_candidates(proposed_term)
    if not candidates:
        raise ValueError("Agent 没有返回可保存的英文规范词条。")
    normalized = candidates[0].strip()
    if len(normalized) > 160 or not _PDF_AGENT_TERM_RE.fullmatch(normalized):
        raise ValueError("Agent 返回的词条不是可保存的英文词或短语。")
    if "|" in normalized or "\n" in normalized:
        raise ValueError("Agent 返回的词条包含非法分隔符。")
    return normalized


def rank_pdf_vocabulary_matches(
    rows: Iterable[dict[str, Any]],
    selected_text: str,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank exact and conservative phrase matches for selected PDF text."""

    candidates = pdf_vocabulary_selection_candidates(selected_text)
    if not candidates:
        return []
    candidate_keys = {_pdf_vocabulary_key(candidate) for candidate in candidates}
    candidate_relaxed = {
        _pdf_vocabulary_key(candidate, strip_diacritics=True) for candidate in candidates
    }
    candidate_compact = {_pdf_vocabulary_compact_key(candidate) for candidate in candidates}
    candidate_compact_relaxed = {
        _pdf_vocabulary_compact_key(candidate, strip_diacritics=True)
        for candidate in candidates
    }
    ranked: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for raw in rows:
        row = dict(raw)
        term = str(row.get("term") or "").strip()
        if not term:
            continue
        term_key = _pdf_vocabulary_key(term)
        term_relaxed = _pdf_vocabulary_key(term, strip_diacritics=True)
        term_compact = _pdf_vocabulary_compact_key(term)
        term_compact_relaxed = _pdf_vocabulary_compact_key(term, strip_diacritics=True)
        match_type = ""
        match_rank = 99
        if term_key in candidate_keys:
            match_type = "精确匹配"
            match_rank = 0
        elif (
            term_relaxed in candidate_relaxed
            or term_compact in candidate_compact
            or term_compact_relaxed in candidate_compact_relaxed
        ):
            match_type = "规范化精确匹配"
            match_rank = 1
        else:
            padded_term = f" {term_relaxed} "
            related = any(
                candidate
                and (
                    f" {candidate} " in padded_term
                    or padded_term in f" {candidate} "
                )
                for candidate in candidate_relaxed
            )
            if not related:
                continue
            match_type = "相关短语"
            match_rank = 2
        row["pdf_match_type"] = match_type
        ranked.append(
            (
                (
                    match_rank,
                    abs(len(term_compact) - min(len(item) for item in candidate_compact)),
                    term.casefold(),
                ),
                row,
            )
        )
    ranked.sort(key=lambda item: item[0])
    return [row for _score, row in ranked[: max(1, min(int(limit), 20))]]


def _latex_text(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "#": r"\#",
        "$": r"\$",
        "%": r"\%",
        "&": r"\&",
        "_": r"\_",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
    }
    return "".join(replacements.get(char, char) for char in str(value or ""))


class VocabularyManager:
    """Workspace-isolated vocabulary implementation used by Qt and the AI executor."""

    def __init__(
        self,
        database: Path | None = None,
        backup_dir: Path | None = None,
        export_dir: Path | None = None,
        *,
        workspace: str | None = None,
        root_dir: Path = ROOT_DIR,
        migrate_legacy: bool = True,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.workspace = normalize_vocabulary_workspace(workspace)
        if database is None:
            scoped_database, scoped_backups, scoped_exports = workspace_vocabulary_paths(
                self.workspace,
                root_dir=self.root_dir,
            )
            database = scoped_database
            backup_dir = backup_dir or scoped_backups
            export_dir = export_dir or scoped_exports
            self.legacy_database: Path | None = (
                self.root_dir / "shared" / "vocabulary.db"
                if self.workspace == "math"
                else None
            )
        else:
            self.legacy_database = None
        self.database = Path(database)
        self.backup_dir = Path(backup_dir or self.database.parent / "backups")
        self.export_dir = Path(export_dir or self.database.parent / "exports")
        self.migrate_legacy = bool(migrate_legacy)

    @classmethod
    def for_workspace(
        cls,
        workspace: str,
        *,
        root_dir: Path = ROOT_DIR,
    ) -> "VocabularyManager":
        return cls(workspace=workspace, root_dir=root_dir)

    def _migrate_legacy_math_database_if_needed(self) -> None:
        legacy = self.legacy_database
        if (
            not self.migrate_legacy
            or self.workspace != "math"
            or legacy is None
            or self.database.exists()
            or not legacy.is_file()
            or legacy.resolve() == self.database.resolve()
        ):
            return
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(legacy, timeout=30)) as source:
            source.execute("PRAGMA busy_timeout=30000")
            integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"旧全局词汇库完整性检查失败，未执行工作空间拆分：{integrity}")
            with tempfile.NamedTemporaryFile(
                suffix=".db",
                prefix="vocabulary_workspace_split_",
                dir=str(self.database.parent),
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
            try:
                with closing(sqlite3.connect(temporary, timeout=30)) as target:
                    source.backup(target)
                    target.commit()
                    copied_integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
                    if copied_integrity != "ok":
                        raise RuntimeError(
                            f"数学词汇库迁移副本完整性检查失败：{copied_integrity}"
                        )
                os.replace(temporary, self.database)
            finally:
                if temporary.exists():
                    temporary.unlink()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        recovery = self.backup_dir / f"legacy_global_before_workspace_split_{stamp}.db"
        shutil.copy2(legacy, recovery)
        try:
            shutil.copy2(recovery, self.backup_dir / "vocabulary_latest.db")
        except OSError:
            pass

    def connect(self, *, rows: bool = False) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        if rows:
            connection.row_factory = sqlite3.Row
        return connection

    def ensure_schema(self) -> None:
        self._migrate_legacy_math_database_if_needed()
        with closing(self.connect()) as connection:
            # Create the parent table before the full schema.  Old vocabulary
            # databases predate familiarity/source and therefore cannot create
            # the newer indexes until their additive columns have been added.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vocabulary_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term TEXT NOT NULL COLLATE NOCASE,
                    part_of_speech TEXT NOT NULL COLLATE NOCASE DEFAULT '',
                    definition TEXT NOT NULL DEFAULT '',
                    familiarity TEXT NOT NULL DEFAULT 'unfamiliar',
                    note TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    entry_kind TEXT NOT NULL DEFAULT 'word',
                    pronunciation TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(vocabulary_entries)")
            }
            for column, definition in {
                "part_of_speech": "TEXT NOT NULL DEFAULT ''",
                "familiarity": "TEXT NOT NULL DEFAULT 'unfamiliar'",
                "note": "TEXT NOT NULL DEFAULT ''",
                "source": "TEXT NOT NULL DEFAULT ''",
                "entry_kind": "TEXT NOT NULL DEFAULT 'word'",
                "pronunciation": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in columns:
                    connection.execute(
                        f'ALTER TABLE vocabulary_entries ADD COLUMN "{column}" {definition}'
                    )
            connection.commit()
            if not self._has_term_part_of_speech_unique_key(connection):
                self._backup_before_term_part_of_speech_migration(connection)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    duplicate = connection.execute(
                    """
                    SELECT term,part_of_speech,COUNT(*)
                    FROM vocabulary_entries
                    GROUP BY term COLLATE NOCASE,part_of_speech COLLATE NOCASE
                    HAVING COUNT(*)>1
                    LIMIT 1
                    """
                ).fetchone()
                    if duplicate is not None:
                        raise RuntimeError(
                            "词汇库存在同词同词性的重复行，无法安全迁移联合唯一键。"
                        )
                    connection.execute("DROP TABLE IF EXISTS vocabulary_entries_migrating")
                    connection.execute(
                    """
                    CREATE TABLE vocabulary_entries_migrating (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        term TEXT NOT NULL COLLATE NOCASE,
                        part_of_speech TEXT NOT NULL COLLATE NOCASE DEFAULT '',
                        definition TEXT NOT NULL DEFAULT '',
                        familiarity TEXT NOT NULL DEFAULT 'unfamiliar',
                        note TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        entry_kind TEXT NOT NULL DEFAULT 'word',
                        pronunciation TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(term, part_of_speech)
                    )
                    """
                )
                    connection.execute(
                    """
                    INSERT INTO vocabulary_entries_migrating(
                        id,term,part_of_speech,definition,familiarity,note,source,
                        entry_kind,pronunciation,
                        created_at,updated_at
                    )
                    SELECT id,term,COALESCE(part_of_speech,''),definition,
                           familiarity,note,source,entry_kind,pronunciation,created_at,updated_at
                    FROM vocabulary_entries
                    ORDER BY id
                    """
                )
                    connection.execute("DROP TABLE vocabulary_entries")
                    connection.execute(
                        "ALTER TABLE vocabulary_entries_migrating RENAME TO vocabulary_entries"
                    )
                    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                    if integrity != "ok":
                        raise RuntimeError(f"词汇库唯一键迁移完整性检查失败：{integrity}")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            # Child tables and every index are created only after the parent
            # migration, so foreign keys never point at a table being replaced.
            connection.executescript(VOCABULARY_SCHEMA)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vocabulary_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO vocabulary_metadata(key,value) VALUES('workspace',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE vocabulary_metadata.value<>excluded.value
                """,
                (self.workspace,),
            )
            connection.commit()

    @staticmethod
    def _has_term_part_of_speech_unique_key(connection: sqlite3.Connection) -> bool:
        for index in connection.execute("PRAGMA index_list(vocabulary_entries)"):
            if not bool(index[2]):
                continue
            index_name = str(index[1]).replace('"', '""')
            columns = tuple(
                str(row[2])
                for row in connection.execute(f'PRAGMA index_info("{index_name}")')
            )
            if columns == ("term", "part_of_speech"):
                return True
        return False

    def _backup_before_term_part_of_speech_migration(
        self,
        connection: sqlite3.Connection,
    ) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        target = self.backup_dir / (
            f"vocabulary_before_term_part_of_speech_migration_{stamp}.db"
        )
        with closing(sqlite3.connect(target)) as destination:
            connection.backup(destination)
            destination.commit()
        try:
            shutil.copy2(target, self.backup_dir / "vocabulary_latest.db")
        except OSError:
            pass
        return target.resolve()

    def backup(self, reason: str) -> Path:
        self.ensure_schema()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(reason or "manual")).strip("_") or "manual"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        target = self.backup_dir / f"vocabulary_before_{safe}_{stamp}.db"
        with closing(self.connect()) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            destination.commit()
        latest = self.backup_dir / "vocabulary_latest.db"
        try:
            shutil.copy2(target, latest)
        except OSError:
            pass
        return target.resolve()

    @staticmethod
    def _normalize_familiarity(value: str, *, allow_all: bool = False) -> str:
        folded = str(value or "").strip().casefold()
        aliases = {
            "familiar": "familiar",
            "熟悉": "familiar",
            "unfamiliar": "unfamiliar",
            "不熟悉": "unfamiliar",
        }
        if allow_all and folded in {"", "all", "全部"}:
            return "all"
        if folded not in aliases:
            raise ValueError("熟悉度只能是 familiar/熟悉 或 unfamiliar/不熟悉。")
        return aliases[folded]

    def list_entries(
        self,
        query: str = "",
        familiarity: str = "all",
        *,
        limit: int | None = 200,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        familiarity = self._normalize_familiarity(familiarity, allow_all=True)
        clauses: list[str] = []
        arguments: list[Any] = []
        if str(query or "").strip():
            pattern = f"%{str(query).strip()}%"
            clauses.append("(term LIKE ? OR part_of_speech LIKE ? OR definition LIKE ? OR note LIKE ?)")
            arguments.extend([pattern, pattern, pattern, pattern])
        if familiarity != "all":
            clauses.append("familiarity=?")
            arguments.append(familiarity)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        limit_sql = "" if limit is None else "LIMIT ?"
        if limit is not None:
            arguments.append(max(1, min(int(limit), 5000)))
        with closing(self.connect(rows=True)) as connection:
            rows = connection.execute(
                f"""
                SELECT id,term,part_of_speech,definition,familiarity,note,source,
                       entry_kind,pronunciation,created_at,updated_at
                FROM vocabulary_entries
                {where}
                ORDER BY term COLLATE NOCASE, id
                {limit_sql}
                """,
                arguments,
            ).fetchall()
        return [dict(row) for row in rows]

    def lookup_pdf_selection(self, selected_text: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Read-only lookup for text selected in either in-app PDF viewer."""

        if not self.database.is_file():
            return []
        uri = f"file:{self.database.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            available = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(vocabulary_entries)")
            }
            optional = [name for name in ("entry_kind", "pronunciation") if name in available]
            select_columns = (
                "id,term,part_of_speech,definition,familiarity,note,source,created_at,updated_at"
                + ("," + ",".join(optional) if optional else "")
            )
            rows = [dict(row) for row in connection.execute(
                f"SELECT {select_columns} FROM vocabulary_entries ORDER BY term COLLATE NOCASE,id"
            ).fetchall()]
        return rank_pdf_vocabulary_matches(rows, selected_text, limit=limit)

    def status(self) -> dict[str, Any]:
        self.ensure_schema()
        with closing(self.connect()) as connection:
            total, familiar, unfamiliar = connection.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN familiarity='familiar' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN familiarity='unfamiliar' THEN 1 ELSE 0 END)
                FROM vocabulary_entries
                """
            ).fetchone()
            sense_count = int(connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0])
            encounter_count = int(connection.execute("SELECT COUNT(*) FROM vocabulary_encounters").fetchone()[0])
            repeated_terms = int(connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT LOWER(COALESCE(v.term,e.surface_form)) AS term_key
                    FROM vocabulary_encounters e
                    LEFT JOIN vocabulary_entries v ON v.id=e.vocabulary_entry_id
                    GROUP BY term_key HAVING COUNT(*) >= 2
                )
                """
            ).fetchone()[0])
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "workspace": self.workspace,
            "database_path": str(self.database.resolve()),
            "legacy_global_database": (
                str(self.legacy_database.resolve())
                if self.legacy_database is not None and self.legacy_database.is_file()
                else ""
            ),
            "total": int(total or 0),
            "familiar": int(familiar or 0),
            "unfamiliar": int(unfamiliar or 0),
            "sense_count": sense_count,
            "encounter_count": encounter_count,
            "repeated_terms": repeated_terms,
            "integrity_check": integrity,
        }

    def resolve_entries(
        self,
        entry_ids: Iterable[int] | None = None,
        terms: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        ids = sorted({int(value) for value in (entry_ids or []) if int(value) > 0})
        normalized_terms = sorted(
            {str(value).strip().casefold() for value in (terms or []) if str(value).strip()}
        )
        if not ids and not normalized_terms:
            raise ValueError("必须提供 entry_ids 或 terms。")
        clauses: list[str] = []
        arguments: list[Any] = []
        if ids:
            clauses.append("id IN (" + ",".join("?" for _ in ids) + ")")
            arguments.extend(ids)
        if normalized_terms:
            clauses.append("LOWER(term) IN (" + ",".join("?" for _ in normalized_terms) + ")")
            arguments.extend(normalized_terms)
        self.ensure_schema()
        with closing(self.connect(rows=True)) as connection:
            rows = connection.execute(
                """
                SELECT id,term,part_of_speech,definition,familiarity,note,source,
                       entry_kind,pronunciation,created_at,updated_at
                FROM vocabulary_entries WHERE """
                + " OR ".join(clauses)
                + " ORDER BY term COLLATE NOCASE,id",
                arguments,
            ).fetchall()
        return [dict(row) for row in rows]

    def import_entries(
        self,
        entries: Iterable[dict[str, Any]],
        *,
        merge_definitions: bool = False,
    ) -> dict[str, Any]:
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        positions_by_term: dict[str, set[str]] = {}
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError("每个词条必须是对象。")
            term = re.sub(r"\s+", " ", str(raw.get("term") or "")).strip()
            definition = str(raw.get("definition") or "").strip()
            if not term or not definition:
                raise ValueError("每个词条都必须包含非空 term 和 definition。")
            part_of_speech = str(raw.get("part_of_speech") or "").strip()
            term_key = term.casefold()
            key = (term_key, part_of_speech.casefold())
            if key in seen:
                raise ValueError(
                    f"本次批量导入包含重复词条：{term} [{part_of_speech or '未标词性'}]"
                )
            seen.add(key)
            positions_by_term.setdefault(term_key, set()).add(part_of_speech.casefold())
            familiarity_provided = bool(str(raw.get("familiarity") or "").strip())
            familiarity = self._normalize_familiarity(
                str(raw.get("familiarity") or "unfamiliar")
            )
            normalized.append(
                {
                    "term": term,
                    "part_of_speech": part_of_speech,
                    "definition": definition,
                    "familiarity": familiarity,
                    "note": str(raw.get("note") or "").strip(),
                    "source": str(raw.get("source") or "").strip(),
                    "entry_kind": str(raw.get("entry_kind") or (
                        "phrase" if " " in term.strip() else "word"
                    )).strip(),
                    "pronunciation": str(raw.get("pronunciation") or "").strip(),
                    "familiarity_provided": familiarity_provided,
                }
            )
        ambiguous = [
            item["term"]
            for item in normalized
            if "" in positions_by_term[item["term"].casefold()]
            and len(positions_by_term[item["term"].casefold()]) > 1
        ]
        if ambiguous:
            raise ValueError(
                "同一批次中的同名词条必须逐项写明词性："
                + "、".join(dict.fromkeys(ambiguous))
            )
        if not normalized:
            raise ValueError("没有可导入的词条。")
        if len(normalized) > 500:
            raise ValueError("单次最多批量导入 500 个词条。")
        backup = self.backup("vocabulary_import")
        inserted = 0
        updated = 0
        definitions_appended = 0
        new_part_of_speech_entries = 0
        affected_ids: list[int] = []
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for item in normalized:
                    same_term = connection.execute(
                        """
                        SELECT id,term,part_of_speech,definition,familiarity,note,source,
                               entry_kind,pronunciation
                        FROM vocabulary_entries
                        WHERE term=? COLLATE NOCASE
                        ORDER BY id
                        """,
                        (item["term"],),
                    ).fetchall()
                    exact = [
                        row
                        for row in same_term
                        if str(row[2] or "").casefold()
                        == item["part_of_speech"].casefold()
                    ]
                    existing = exact[0] if exact else None
                    if existing is None and item["part_of_speech"]:
                        if len(same_term) == 1 and not str(same_term[0][2] or "").strip():
                            existing = same_term[0]
                    elif existing is None and not item["part_of_speech"]:
                        if len(same_term) == 1:
                            existing = same_term[0]
                            item["part_of_speech"] = str(existing[2] or "").strip()
                        elif len(same_term) > 1:
                            raise ValueError(
                                f"词条 {item['term']} 已有多个词性，导入时必须明确 part_of_speech。"
                            )
                    if existing is None:
                        cursor = connection.execute(
                            """
                            INSERT INTO vocabulary_entries(
                                term,part_of_speech,definition,familiarity,note,source,
                                entry_kind,pronunciation
                            ) VALUES(?,?,?,?,?,?,?,?)
                            """,
                            tuple(item[key] for key in (
                                "term", "part_of_speech", "definition", "familiarity", "note", "source",
                                "entry_kind", "pronunciation"
                            )),
                        )
                        affected_ids.append(int(cursor.lastrowid))
                        inserted += 1
                        if same_term:
                            new_part_of_speech_entries += 1
                    else:
                        effective_familiarity = (
                            item["familiarity"]
                            if item["familiarity_provided"]
                            else str(existing[4] or "unfamiliar")
                        )
                        definition = item["definition"]
                        note = item["note"]
                        source = item["source"]
                        if merge_definitions:
                            definition = _merge_semicolon_segments(existing[3], definition)
                            note = _merge_semicolon_segments(existing[5], note)
                            source = _merge_semicolon_segments(existing[6], source)
                            if definition != str(existing[3] or ""):
                                definitions_appended += 1
                        connection.execute(
                            """
                            UPDATE vocabulary_entries
                            SET term=?,part_of_speech=?,definition=?,familiarity=?,note=?,source=?,
                                entry_kind=?,pronunciation=?,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE id=?
                            """,
                            (
                                str(existing[1] or item["term"]),
                                item["part_of_speech"],
                                definition,
                                effective_familiarity,
                                note,
                                source,
                                item["entry_kind"] or str(existing[7] or "word"),
                                item["pronunciation"] or str(existing[8] or ""),
                                int(existing[0]),
                            ),
                        )
                        affected_ids.append(int(existing[0]))
                        updated += 1
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"词汇库完整性检查失败：{integrity}")
                connection.commit()
        except Exception:
            shutil.copy2(backup, self.database)
            raise
        readback = self.resolve_entries(entry_ids=affected_ids)
        if len(readback) != len(normalized):
            shutil.copy2(backup, self.database)
            raise RuntimeError("词汇批量导入写后回读数量不一致，已从备份恢复。")
        return {
            "inserted": inserted,
            "updated": updated,
            "affected": inserted + updated,
            "definitions_appended": definitions_appended,
            "new_part_of_speech_entries": new_part_of_speech_entries,
            "merge_definitions": bool(merge_definitions),
            "entries": readback,
            "backup_path": str(backup),
            "integrity_check": "ok",
            "readback_verified": True,
        }

    def add_sense(
        self,
        vocabulary_entry_id: int,
        *,
        definition_zh: str,
        definition_en: str = "",
        domain: str = "general",
        register_note: str = "",
        collocations: str = "",
        source_kind: str = "user",
        source_material_id: int | None = None,
        source_page: int | None = None,
        confidence: str = "confirmed",
    ) -> dict[str, Any]:
        if not str(definition_zh or "").strip() and not str(definition_en or "").strip():
            raise ValueError("词义至少需要中文或英文定义。")
        entries = self.resolve_entries(entry_ids=[int(vocabulary_entry_id)])
        if not entries:
            raise ValueError("词汇条目不存在。")
        backup = self.backup("vocabulary_sense")
        with closing(self.connect(rows=True)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO vocabulary_senses(
                        vocabulary_entry_id,definition_zh,definition_en,domain,
                        register_note,collocations,source_kind,source_material_id,
                        source_page,confidence
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(vocabulary_entry_id), str(definition_zh).strip(),
                        str(definition_en).strip(), str(domain or "general").strip(),
                        str(register_note).strip(), str(collocations).strip(),
                        str(source_kind or "user").strip(), source_material_id,
                        source_page, str(confidence or "confirmed").strip(),
                    ),
                )
                sense_id = int(cursor.lastrowid)
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"词汇库完整性检查失败：{integrity}")
                connection.commit()
            except Exception:
                connection.rollback()
                shutil.copy2(backup, self.database)
                raise
            row = connection.execute(
                "SELECT * FROM vocabulary_senses WHERE id=?", (sense_id,)
            ).fetchone()
        return {
            "sense": dict(row),
            "backup_path": str(backup),
            "readback_verified": row is not None,
            "integrity_check": "ok",
        }

    def senses_for_entry(self, vocabulary_entry_id: int) -> list[dict[str, Any]]:
        self.ensure_schema()
        with closing(self.connect(rows=True)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM vocabulary_senses
                WHERE vocabulary_entry_id=? ORDER BY domain,id
                """,
                (int(vocabulary_entry_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_encounter(
        self,
        surface_form: str,
        *,
        vocabulary_entry_id: int | None = None,
        sense_id: int | None = None,
        selected_text: str = "",
        context: str = "",
        source_domain: str = "general",
        material_id: int | None = None,
        material_code: str = "",
        material_title: str = "",
        source_path: str = "",
        page_number: int | None = None,
        anchor_y: float = 0.0,
        event_type: str = "lookup",
    ) -> dict[str, Any]:
        surface = re.sub(r"\s+", " ", str(surface_form or "")).strip()
        if not surface:
            raise ValueError("encounter 必须保留实际词形或短语。")
        self.ensure_schema()
        with closing(self.connect(rows=True)) as connection:
            if vocabulary_entry_id is None:
                candidates = rank_pdf_vocabulary_matches(
                    [
                        dict(row)
                        for row in connection.execute(
                            """
                            SELECT id,term,part_of_speech,definition,familiarity,note,source,
                                   entry_kind,pronunciation,created_at,updated_at
                            FROM vocabulary_entries ORDER BY term COLLATE NOCASE,id
                            """
                        ).fetchall()
                    ],
                    surface,
                    limit=1,
                )
                vocabulary_entry_id = int(candidates[0]["id"]) if candidates else None
            cursor = connection.execute(
                """
                INSERT INTO vocabulary_encounters(
                    vocabulary_entry_id,sense_id,surface_form,selected_text,context,
                    source_domain,material_id,material_code,material_title,source_path,
                    page_number,anchor_y,event_type
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    vocabulary_entry_id, sense_id, surface,
                    re.sub(r"\s+", " ", str(selected_text or surface)).strip(),
                    re.sub(r"\s+", " ", str(context or "")).strip()[:2000],
                    str(source_domain or "general").strip(), material_id,
                    str(material_code or "").strip(), str(material_title or "").strip(),
                    str(source_path or "").strip(), page_number,
                    max(0.0, float(anchor_y)), str(event_type or "lookup").strip(),
                ),
            )
            encounter_id = int(cursor.lastrowid)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM vocabulary_encounters WHERE id=?", (encounter_id,)
            ).fetchone()
        return {
            "encounter": dict(row),
            "readback_verified": row is not None,
        }

    def list_encounters(
        self,
        *,
        vocabulary_entry_id: int | None = None,
        material_id: int | None = None,
        query: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        clauses: list[str] = []
        args: list[Any] = []
        if vocabulary_entry_id is not None:
            clauses.append("e.vocabulary_entry_id=?")
            args.append(int(vocabulary_entry_id))
        if material_id is not None:
            clauses.append("e.material_id=?")
            args.append(int(material_id))
        if query.strip():
            pattern = f"%{query.strip()}%"
            clauses.append("(e.surface_form LIKE ? OR e.context LIKE ? OR v.term LIKE ?)")
            args.extend([pattern, pattern, pattern])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(max(1, min(int(limit), 5000)))
        with closing(self.connect(rows=True)) as connection:
            rows = connection.execute(
                f"""
                SELECT e.*,v.term,v.part_of_speech,s.definition_zh,s.definition_en
                FROM vocabulary_encounters e
                LEFT JOIN vocabulary_entries v ON v.id=e.vocabulary_entry_id
                LEFT JOIN vocabulary_senses s ON s.id=e.sense_id
                {where}
                ORDER BY e.created_at DESC,e.id DESC LIMIT ?
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_familiarity(
        self,
        familiarity: str,
        *,
        entry_ids: Iterable[int] | None = None,
        terms: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        familiarity = self._normalize_familiarity(familiarity)
        selected = self.resolve_entries(entry_ids, terms)
        if not selected:
            raise ValueError("没有找到要设置熟悉度的词条。")
        ids = [int(item["id"]) for item in selected]
        backup = self.backup("vocabulary_familiarity")
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE vocabulary_entries SET familiarity=?,updated_at=CURRENT_TIMESTAMP WHERE id IN ("
                    + ",".join("?" for _ in ids)
                    + ")",
                    [familiarity, *ids],
                )
                connection.commit()
                affected = int(cursor.rowcount)
        except Exception:
            shutil.copy2(backup, self.database)
            raise
        readback = self.resolve_entries(ids, None)
        verified = len(readback) == len(ids) and all(
            str(item["familiarity"]) == familiarity for item in readback
        )
        if not verified:
            shutil.copy2(backup, self.database)
            raise RuntimeError("熟悉度写后回读不一致，已从备份恢复。")
        return {
            "affected": affected,
            "familiarity": familiarity,
            "entries": readback,
            "backup_path": str(backup),
            "integrity_check": "ok",
            "readback_verified": True,
        }

    def delete_entries(
        self,
        *,
        entry_ids: Iterable[int] | None = None,
        terms: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = self.resolve_entries(entry_ids, terms)
        if not selected:
            raise ValueError("没有找到要删除的词条。")
        ids = [int(item["id"]) for item in selected]
        backup = self.backup("vocabulary_delete")
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "DELETE FROM vocabulary_entries WHERE id IN ("
                    + ",".join("?" for _ in ids)
                    + ")",
                    ids,
                )
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"词汇库完整性检查失败：{integrity}")
                connection.commit()
                affected = int(cursor.rowcount)
        except Exception:
            shutil.copy2(backup, self.database)
            raise
        remaining = self.resolve_entries(ids, None)
        if remaining or affected != len(ids):
            shutil.copy2(backup, self.database)
            raise RuntimeError("删除写后回读不一致，已从备份恢复。")
        return {
            "affected": affected,
            "deleted_entries": selected,
            "backup_path": str(backup),
            "integrity_check": "ok",
            "readback_verified": True,
        }

    @staticmethod
    def txt_text(rows: list[dict[str, Any]], title: str) -> str:
        lines = [
            title,
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"词条数: {len(rows)}",
            "",
            "格式: English term | part of speech | 中文释义 | familiarity | note",
            "",
        ]
        for row in rows:
            familiarity = "熟悉" if row.get("familiarity") == "familiar" else "不熟悉"
            values = [
                row.get("term", ""), row.get("part_of_speech", ""),
                row.get("definition", ""), familiarity, row.get("note", ""),
            ]
            lines.append(" | ".join(str(value).replace("\n", " ").strip() for value in values))
        return "\n".join(lines).rstrip() + "\n"

    def export_txt(self, familiarity: str = "all") -> dict[str, Any]:
        familiarity = self._normalize_familiarity(familiarity, allow_all=True)
        rows = self.list_entries("", familiarity, limit=None)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        target = self.export_dir / f"vocabulary_{familiarity}.txt"
        temporary = target.with_suffix(".txt.tmp")
        temporary.write_text(self.txt_text(rows, "词汇库导出"), encoding="utf-8-sig")
        os.replace(temporary, target)
        if not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError("词汇库 TXT 没有成功生成。")
        return {
            "path": str(target.resolve()),
            "familiarity": familiarity,
            "entry_count": len(rows),
            "readback_verified": True,
        }

    @staticmethod
    def pdf_tex(rows: list[dict[str, Any]], title: str) -> str:
        body = []
        for index, row in enumerate(rows, 1):
            body.append(
                " & ".join(
                    [
                        str(index), _latex_text(row.get("term", "")),
                        _latex_text(row.get("part_of_speech", "")),
                        _latex_text(row.get("definition", "")),
                        "熟悉" if row.get("familiarity") == "familiar" else "不熟悉",
                    ]
                )
                + r" \\"
            )
        if not body:
            body.append("\\multicolumn{5}{c}{没有符合条件的词条。} \\\\")
        return rf"""\documentclass[UTF8,12pt]{{ctexart}}
\usepackage[a4paper,margin=2cm]{{geometry}}
\usepackage{{array,longtable,booktabs}}
\setlength{{\parindent}}{{0pt}}
\renewcommand{{\arraystretch}}{{1.35}}
\begin{{document}}
\begin{{center}}{{\LARGE\bfseries {_latex_text(title)}\par}}\end{{center}}
\begin{{longtable}}{{r p{{4.2cm}} p{{2.1cm}} p{{7cm}} p{{1.6cm}}}}
\toprule 序号 & 英文 & 词性 & 中文释义 & 熟悉度 \\ \midrule
\endfirsthead
\toprule 序号 & 英文 & 词性 & 中文释义 & 熟悉度 \\ \midrule
\endhead
{chr(10).join(body)}
\bottomrule
\end{{longtable}}
\end{{document}}
"""

    def export_pdf(self, familiarity: str = "all") -> dict[str, Any]:
        familiarity = self._normalize_familiarity(familiarity, allow_all=True)
        rows = self.list_entries("", familiarity, limit=None)
        latexmk = shutil.which("latexmk")
        if not latexmk:
            raise RuntimeError("未找到 latexmk，无法导出词汇库 PDF。")
        build_dir = self.export_dir / f"vocabulary_{familiarity}_build"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        tex_path = build_dir / "vocabulary.tex"
        tex_path.write_text(self.pdf_tex(rows, "词汇库导出"), encoding="utf-8")
        completed = subprocess.run(
            [latexmk, "-xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            cwd=build_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0:
            log = build_dir / "compile_error.log"
            log.write_text(completed.stdout + "\n" + completed.stderr, encoding="utf-8")
            raise RuntimeError(f"词汇库 PDF 编译失败，日志：{log}")
        generated = build_dir / "vocabulary.pdf"
        if not generated.is_file():
            raise RuntimeError("词汇库 PDF 编译结束但没有正式输出。")
        target = self.export_dir / f"vocabulary_{familiarity}.pdf"
        shutil.copy2(generated, target)
        return {
            "path": str(target.resolve()),
            "familiarity": familiarity,
            "entry_count": len(rows),
            "readback_verified": target.is_file() and target.stat().st_size > 0,
        }
