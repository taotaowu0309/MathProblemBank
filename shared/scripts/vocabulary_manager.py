from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from shared.scripts.platform_runtime import portable_latex_source


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

# Canonical labels used by the existing vocabulary.  Import and display accept
# common long-form aliases, but persist/return the majority spelling.
VOCABULARY_POS_ALIASES = {
    "noun": "n.",
    "adjective": "adj.",
    "verb": "v.",
    "adverb": "adv.",
    "preposition": "prep.",
    "conjunction": "conj.",
    "noun phrase": "n. phr.", "n phrase": "n. phr.", "n.phr.": "n. phr.",
    "adjective phrase": "adj. phr.", "adj phrase": "adj. phr.", "adj.phr.": "adj. phr.",
    "verb phrase": "v. phr.", "v phrase": "v. phr.", "v.phr.": "v. phr.",
    "adverb phrase": "adv. phr.", "adv phrase": "adv. phr.", "adv.phr.": "adv. phr.",
    "prepositional phrase": "prep. phr.", "preposition phrase": "prep. phr.", "prep phrase": "prep. phr.", "prep.phr.": "prep. phr.",
    "conjunction phrase": "conj. phr.", "conj phrase": "conj. phr.", "conj.phr.": "conj. phr.",
    "名词": "n.", "形容词": "adj.", "动词": "v.", "副词": "adv.", "介词": "prep.", "连词": "conj.",
}

def normalize_vocabulary_part_of_speech(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return VOCABULARY_POS_ALIASES.get(text.casefold(), VOCABULARY_POS_ALIASES.get(text, text))

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


def vocabulary_popup_meanings(row: dict[str, Any]) -> list[str]:
    """Saved meanings of one merged PDF lookup row, one display line per sense."""

    lines: list[str] = []
    seen: set[str] = set()
    senses = [sense for sense in row.get("senses") or [] if isinstance(sense, dict)]
    if not senses and str(row.get("definition") or "").strip():
        senses = [{"part_of_speech": row.get("part_of_speech"), "definition": row.get("definition")}]
    head_part_of_speech = str(row.get("part_of_speech") or "").strip()
    for sense in senses:
        definition = str(
            sense.get("definition") or sense.get("definition_zh") or sense.get("definition_en") or ""
        ).strip()
        if not definition or definition.casefold() in seen:
            continue
        seen.add(definition.casefold())
        part_of_speech = str(sense.get("part_of_speech") or "").strip()
        # The headline already carries the entry's part of speech.
        if part_of_speech and part_of_speech != head_part_of_speech:
            definition = f"{part_of_speech}  {definition}"
        lines.append(definition)
    return lines


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
        reading_only: bool = False,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        familiarity = self._normalize_familiarity(familiarity, allow_all=True)
        clauses: list[str] = []
        arguments: list[Any] = []
        if reading_only:
            clauses.append(
                "NOT (LOWER(COALESCE(source,'')) LIKE '%raydu_vocabulary%' "
                "OR LOWER(COALESCE(source,'')) LIKE '%英文字彙%' "
                "OR LOWER(COALESCE(source,'')) LIKE '%英文字汇%')"
            )
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
        result = [dict(row) for row in rows]
        for row in result:
            row["part_of_speech"] = normalize_vocabulary_part_of_speech(row.get("part_of_speech"))
        return result

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

    @staticmethod
    def _read_entries_by_ids(
        connection: sqlite3.Connection,
        entry_ids: Iterable[int],
    ) -> list[dict[str, Any]]:
        ids = sorted(set(entry_ids))
        if not ids:
            return []
        rows = connection.execute(
            """
            SELECT id,term,part_of_speech,definition,familiarity,note,source,
                   entry_kind,pronunciation,created_at,updated_at
            FROM vocabulary_entries WHERE id IN ("""
            + ",".join("?" for _ in ids)
            + ") ORDER BY term COLLATE NOCASE,id",
            ids,
        ).fetchall()
        return [dict(row) for row in rows]

    def _read_committed_entries(
        self, entry_ids: Iterable[int], backup: Path, operation: str,
    ) -> list[dict[str, Any]]:
        try:
            return self.resolve_entries(entry_ids=entry_ids)
        except Exception as error:
            raise RuntimeError(
                f"{operation}已提交，但写后回读失败；未覆盖数据库。恢复备份：{backup}"
            ) from error

    @staticmethod
    def _entries_match_expected(
        rows: list[dict[str, Any]], expected: dict[int, dict[str, Any]],
    ) -> bool:
        return len(rows) == len(expected) and all(
            int(row["id"]) in expected and all(
                row[field] == value for field, value in expected[int(row["id"])].items()
            )
            for row in rows
        )

    def import_entries(
        self,
        entries: Iterable[dict[str, Any]],
        *,
        merge_definitions: bool = False,
        max_entries: int = 500,
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
            part_of_speech = normalize_vocabulary_part_of_speech(raw.get("part_of_speech"))
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
        entry_limit = max(1, int(max_entries))
        if len(normalized) > entry_limit:
            raise ValueError(f"单次最多批量导入 {entry_limit} 个词条。")
        backup = self.backup("vocabulary_import")
        inserted = 0
        updated = 0
        definitions_appended = 0
        new_part_of_speech_entries = 0
        affected_ids: list[int] = []
        expected_rows: dict[int, dict[str, Any]] = {}
        entry_fields = (
            "term", "part_of_speech", "definition", "familiarity", "note", "source",
            "entry_kind", "pronunciation",
        )
        # Roll back this transaction on failure; restoring the backup file
        # would also erase unrelated commits made after that snapshot.
        with closing(self.connect(rows=True)) as connection, connection:
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
                    expected = {field: item[field] for field in entry_fields}
                    cursor = connection.execute(
                        """
                        INSERT INTO vocabulary_entries(
                            term,part_of_speech,definition,familiarity,note,source,
                            entry_kind,pronunciation
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        tuple(expected[field] for field in entry_fields),
                    )
                    affected_ids.append(int(cursor.lastrowid))
                    expected_rows[int(cursor.lastrowid)] = expected
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
                    expected = dict(zip(entry_fields, (
                        str(existing[1] or item["term"]), item["part_of_speech"], definition,
                        effective_familiarity, note, source,
                        item["entry_kind"] or str(existing[7] or "word"),
                        item["pronunciation"] or str(existing[8] or ""),
                    )))
                    connection.execute(
                        """
                        UPDATE vocabulary_entries
                        SET term=?,part_of_speech=?,definition=?,familiarity=?,note=?,source=?,
                            entry_kind=?,pronunciation=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (*[expected[field] for field in entry_fields], int(existing[0])),
                    )
                    affected_ids.append(int(existing[0]))
                    expected_rows[int(existing[0])] = expected
                    updated += 1
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"词汇库完整性检查失败：{integrity}")
            pending = self._read_entries_by_ids(connection, affected_ids)
            if len(pending) != len(normalized) or not self._entries_match_expected(pending, expected_rows):
                raise RuntimeError("词汇批量导入回读内容不一致，事务已取消。")
            connection.commit()
        readback = self._read_committed_entries(affected_ids, backup, "词汇批量导入")
        if len(readback) != len(normalized) or not self._entries_match_expected(readback, expected_rows):
            raise RuntimeError(
                f"词汇批量导入已提交，但写后回读内容不一致；未覆盖数据库。恢复备份：{backup}"
            )
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
        expected = {
            "vocabulary_entry_id": int(vocabulary_entry_id),
            "definition_zh": str(definition_zh).strip(),
            "definition_en": str(definition_en).strip(),
            "domain": str(domain or "general").strip(),
            "register_note": str(register_note).strip(),
            "collocations": str(collocations).strip(),
            "source_kind": str(source_kind or "user").strip(),
            "source_material_id": source_material_id,
            "source_page": source_page,
            "confidence": str(confidence or "confirmed").strip(),
        }
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
                    tuple(expected.values()),
                )
                sense_id = int(cursor.lastrowid)
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"词汇库完整性检查失败：{integrity}")
                pending = connection.execute(
                    "SELECT * FROM vocabulary_senses WHERE id=?", (sense_id,)
                ).fetchone()
                if pending is None or any(pending[field] != value for field, value in expected.items()):
                    raise RuntimeError("词义回读不一致，事务已取消。")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            try:
                row = connection.execute(
                    "SELECT * FROM vocabulary_senses WHERE id=?", (sense_id,)
                ).fetchone()
            except Exception as error:
                raise RuntimeError(
                    f"词义已提交，但写后回读失败；未覆盖数据库。恢复备份：{backup}"
                ) from error
        if row is None or any(row[field] != value for field, value in expected.items()):
            raise RuntimeError(
                f"词义已提交，但写后回读不一致；未覆盖数据库。恢复备份：{backup}"
            )
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
        automatic: bool = False,
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
        with closing(self.connect(rows=True)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE vocabulary_entries SET familiarity=?,updated_at=CURRENT_TIMESTAMP WHERE id IN ("
                + ",".join("?" for _ in ids)
                + ")",
                [familiarity, *ids],
            )
            affected = int(cursor.rowcount)
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"词汇库完整性检查失败：{integrity}")
            pending = self._read_entries_by_ids(connection, ids)
            if affected != len(ids) or len(pending) != len(ids) or any(
                str(item["familiarity"]) != familiarity for item in pending
            ):
                raise RuntimeError("熟悉度回读不一致，事务已取消。")
            connection.commit()
        readback = self._read_committed_entries(ids, backup, "熟悉度修改")
        verified = len(readback) == len(ids) and all(
            str(item["familiarity"]) == familiarity for item in readback
        )
        if not verified:
            raise RuntimeError(
                f"熟悉度修改已提交，但写后回读不一致；未覆盖数据库。恢复备份：{backup}"
            )
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
        with closing(self.connect(rows=True)) as connection, connection:
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
            affected = int(cursor.rowcount)
            if affected != len(ids) or self._read_entries_by_ids(connection, ids):
                raise RuntimeError("删除回读不一致，事务已取消。")
            connection.commit()
        remaining = self._read_committed_entries(ids, backup, "词汇删除")
        if remaining or affected != len(ids):
            raise RuntimeError(
                f"词汇删除已提交，但写后回读不一致；未覆盖数据库。恢复备份：{backup}"
            )
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

    def export_txt(
        self,
        familiarity: str = "all",
        *,
        reading_only: bool = False,
        entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        familiarity = self._normalize_familiarity(familiarity, allow_all=True)
        rows = self.list_entries("", familiarity, limit=None, reading_only=reading_only) if entries is None else entries
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
        source = rf"""\documentclass[UTF8,12pt]{{ctexart}}
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

        return portable_latex_source(source)

    def export_pdf(
        self,
        familiarity: str = "all",
        *,
        reading_only: bool = False,
        entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        familiarity = self._normalize_familiarity(familiarity, allow_all=True)
        rows = self.list_entries("", familiarity, limit=None, reading_only=reading_only) if entries is None else entries
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

    # -- spaced review -------------------------------------------------------
    # Review progress belongs to the vocabulary database, but its tables are
    # created only on the first review write.  Merely browsing cards therefore
    # never changes a database.

    @staticmethod
    def study_day_start(now: datetime | None = None) -> datetime:
        moment = now or datetime.now()
        start = moment.replace(hour=REVIEW_DAY_START_HOUR, minute=0, second=0, microsecond=0)
        return start if moment >= start else start - timedelta(days=1)

    @classmethod
    def study_day_end(cls, now: datetime | None = None) -> datetime:
        return cls.study_day_start(now) + timedelta(days=1)

    @staticmethod
    def _has_review_tables(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vocabulary_review_cards'"
        ).fetchone() is not None

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _ensure_review_schema(self) -> None:
        self.ensure_schema()
        with closing(self.connect()) as connection:
            if self._has_review_tables(connection):
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(vocabulary_review_cards)")}
                if "collected_at" not in columns:
                    # Added after the first review tables; additive, older code ignores it.
                    connection.execute("ALTER TABLE vocabulary_review_cards ADD COLUMN collected_at TEXT")
                    connection.commit()
            if self._has_review_tables(connection) and all(
                self._has_table(connection, name) for name in ("vocabulary_card_examples", "vocabulary_card_etymology")
            ):
                return
            connection.executescript(VOCABULARY_REVIEW_SCHEMA)
            connection.commit()

    def _backup_for_review(self) -> str:
        """Back up once per study day before the first review write of that day."""

        stamp = self.study_day_start().strftime("%Y-%m-%d")
        marker = f"vocabulary_before_vocabulary_review_{stamp}"
        if self.backup_dir.is_dir():
            existing = sorted(self.backup_dir.glob(f"{marker}*.db"))
            if existing:
                return str(existing[-1].resolve())
        if not self.database.is_file():
            return ""
        target = self.backup(f"vocabulary_review_{stamp}")
        return str(target)

    def review_card(self, card_key: str) -> dict[str, Any] | None:
        if not self.database.is_file():
            return None
        with closing(self.connect(rows=True)) as connection:
            if not self._has_review_tables(connection):
                return None
            row = connection.execute(
                "SELECT * FROM vocabulary_review_cards WHERE card_key=?", (str(card_key),)
            ).fetchone()
        return dict(row) if row is not None else None

    def review_streak(self, now: datetime | None = None) -> int:
        """Consecutive study days with at least one review, ending today (or yesterday if today has none yet)."""

        if not self.database.is_file():
            return 0
        moment = now or datetime.now()
        today = self.study_day_start(moment)
        with closing(self.connect()) as connection:
            if not self._has_review_tables(connection):
                return 0
            rows = connection.execute(
                "SELECT reviewed_at FROM vocabulary_review_log WHERE reviewed_at>=?",
                ((today - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchall()
        days = set()
        for (stamp,) in rows:
            try:
                days.add(self.study_day_start(datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")))
            except ValueError:
                continue
        day = today if today in days else today - timedelta(days=1)
        streak = 0
        while day in days:
            streak += 1
            day -= timedelta(days=1)
        return streak

    def review_progress(self, day_end: datetime | None = None) -> dict[str, dict[str, Any]]:
        """``card_key -> {state, due, collected, collected_at}`` for every stored review card."""

        if not self.database.is_file():
            return {}
        end = (day_end or self.study_day_end()).strftime("%Y-%m-%d %H:%M:%S")
        with closing(self.connect(rows=True)) as connection:
            if not self._has_review_tables(connection):
                return {}
            rows = [dict(row) for row in connection.execute("SELECT * FROM vocabulary_review_cards")]
        return {
            str(row["card_key"]): {
                "state": str(row["state"]),
                "term": str(row["term"]),
                "due": str(row["state"]) not in {"new", "suspended"}
                and bool(row["due_at"]) and str(row["due_at"]) < end,
                "collected": bool(row.get("collected_at")),
                "collected_at": str(row.get("collected_at") or ""),
            }
            for row in rows
        }

    def ensure_review_cards(self, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Insert missing review cards and mark lookups; writes nothing when all are present."""

        wanted: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("card_key") or "").strip()
            term = re.sub(r"\s+", " ", str(row.get("term") or "")).strip()
            if not key or not term:
                raise ValueError("复习卡片必须有 card_key 和 term。")
            wanted.setdefault(key, {**row, "card_key": key, "term": term})
        if not wanted:
            return {"inserted": 0, "marked": 0, "backup_path": "", "readback_verified": True}
        existing: dict[str, dict[str, Any]] = {}
        if self.database.is_file():
            with closing(self.connect(rows=True)) as connection:
                if self._has_review_tables(connection):
                    for row in connection.execute("SELECT * FROM vocabulary_review_cards"):
                        existing[str(row["card_key"])] = dict(row)
        inserts = [row for key, row in wanted.items() if key not in existing]
        marks = [
            row for key, row in wanted.items()
            if key in existing and row.get("collected_at") and not existing[key].get("collected_at")
        ]
        if not inserts and not marks:
            return {"inserted": 0, "marked": 0, "backup_path": "", "readback_verified": True}
        self._ensure_review_schema()
        backup = self._backup_for_review()
        with closing(self.connect(rows=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                entry_ids = {
                    str(row["term"]).casefold(): int(row["id"])
                    for row in connection.execute(
                        "SELECT id,term FROM vocabulary_entries ORDER BY id DESC"
                    )
                }
                for row in inserts:
                    entry_id = row.get("vocabulary_entry_id") or entry_ids.get(row["term"].casefold())
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO vocabulary_review_cards(
                            card_key,vocabulary_entry_id,term,deck,unit_key,sort_order,collected_at
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            row["card_key"], int(entry_id) if entry_id else None, row["term"],
                            str(row.get("deck") or "raydu"), str(row.get("unit_key") or ""),
                            int(row.get("sort_order") or 0), row.get("collected_at") or None,
                        ),
                    )
                for row in marks:
                    connection.execute(
                        "UPDATE vocabulary_review_cards SET collected_at=?,updated_at=CURRENT_TIMESTAMP "
                        "WHERE card_key=? AND collected_at IS NULL",
                        (row["collected_at"], row["card_key"]),
                    )
                keys = list(wanted)
                stored = 0
                for start in range(0, len(keys), 500):
                    chunk = keys[start:start + 500]
                    stored += int(connection.execute(
                        "SELECT COUNT(*) FROM vocabulary_review_cards WHERE card_key IN ("
                        + ",".join("?" for _ in chunk) + ")",
                        chunk,
                    ).fetchone()[0])
                if stored != len(keys):
                    raise RuntimeError("复习卡片写入后数量不一致，事务已取消。")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        verified = all(self.review_card(row["card_key"]) is not None for row in inserts[:50] + marks[:50])
        if not verified:
            raise RuntimeError(f"复习卡片已提交，但写后回读不一致。恢复备份：{backup}")
        return {
            "inserted": len(inserts),
            "marked": len(marks),
            "backup_path": backup,
            "readback_verified": True,
        }

    def review_new_per_day(self) -> int:
        if not self.database.is_file():
            return REVIEW_DEFAULT_NEW_PER_DAY
        with closing(self.connect()) as connection:
            try:
                row = connection.execute(
                    "SELECT value FROM vocabulary_metadata WHERE key='review_new_per_day'"
                ).fetchone()
            except sqlite3.Error:
                row = None
        try:
            return max(0, min(int(row[0]), 500)) if row else REVIEW_DEFAULT_NEW_PER_DAY
        except (TypeError, ValueError):
            return REVIEW_DEFAULT_NEW_PER_DAY

    def set_review_new_per_day(self, value: int) -> dict[str, Any]:
        number = int(value)
        if not 0 <= number <= 500:
            raise ValueError("每天新词数必须在 0 到 500 之间。")
        self.ensure_schema()
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO vocabulary_metadata(key,value) VALUES('review_new_per_day',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(number),),
            )
        stored = self.review_new_per_day()
        if stored != number:
            raise RuntimeError("每天新词数写后回读不一致。")
        return {"review_new_per_day": stored, "readback_verified": True}

    def clear_collected(self, card_keys: Iterable[str], *, entry_ids: Iterable[int] = ()) -> dict[str, Any]:
        """Remove library saves together, keeping the book cards' review progress."""

        keys = [str(key) for key in card_keys if str(key).strip()]
        ids = list(dict.fromkeys(int(entry_id) for entry_id in entry_ids))
        if not keys and not ids:
            raise ValueError("没有要移出词汇库的单词。")
        backup = str(self.backup("vocabulary_library_remove"))
        self._ensure_review_schema()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if keys:
                    connection.execute(
                        "UPDATE vocabulary_review_cards SET collected_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE card_key IN ("
                        + ",".join("?" for _ in keys) + ")",
                        keys,
                    )
                if ids:
                    deleted = connection.execute(
                        "DELETE FROM vocabulary_entries WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids,
                    )
                    if deleted.rowcount != len(ids):
                        raise RuntimeError("词条移出数量不一致，事务已取消。")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if any((self.review_card(key) or {}).get("collected_at") for key in keys):
            raise RuntimeError(f"取消收藏写后回读不一致。恢复备份：{backup}")
        if ids and self._read_committed_entries(ids, Path(backup), "移出词汇库"):
            raise RuntimeError(f"词条移出后回读不一致。恢复备份：{backup}")
        return {"cleared": keys, "deleted_entry_ids": ids, "backup_path": backup, "readback_verified": True}

    def card_examples(self, term: str) -> list[dict[str, Any]]:
        """Stored examples (AI-written) of one 词汇库 word, in order."""

        if not self.database.is_file():
            return []
        with closing(self.connect(rows=True)) as connection:
            if not self._has_table(connection, "vocabulary_card_examples"):
                return []
            rows = connection.execute(
                "SELECT * FROM vocabulary_card_examples WHERE term=? ORDER BY position,id", (str(term),)
            ).fetchall()
        return [dict(row) for row in rows]

    def card_etymology_terms(self) -> set[str]:
        """Read preparation state for a list without opening a connection per word."""

        if not self.database.is_file():
            return set()
        with closing(self.connect()) as connection:
            if not self._has_table(connection, "vocabulary_card_etymology"):
                return set()
            return {str(row[0]).casefold() for row in connection.execute("SELECT term FROM vocabulary_card_etymology")}

    def book_aligned_etymology_terms(self) -> set[str]:
        """Words whose checked 字源 follows the 字汇书's split (one query for the whole book)."""

        if not self.database.is_file():
            return set()
        with closing(self.connect()) as connection:
            if not self._has_table(connection, "vocabulary_card_etymology"):
                return set()
            rows = connection.execute("SELECT term, evidence_json FROM vocabulary_card_etymology").fetchall()
        aligned = set()
        for term, evidence in rows:
            try:
                parsed = json.loads(evidence or "[]")
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("book_aligned"):
                aligned.add(str(term).casefold())
        return aligned

    def card_etymology(self, term: str) -> dict[str, Any] | None:
        """The AI-checked 字源 of one word (书中词条 or 词汇库 word), if it was checked."""

        if not self.database.is_file():
            return None
        with closing(self.connect(rows=True)) as connection:
            if not self._has_table(connection, "vocabulary_card_etymology"):
                return None
            row = connection.execute("SELECT * FROM vocabulary_card_etymology WHERE term=?", (str(term),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("morphemes_json", "evidence_json", "review_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field) or "[]")
        evidence = result["evidence"]
        result["evidence"], result["sources"] = (
            (evidence.get("points") or [], evidence.get("sources") or []) if isinstance(evidence, dict) else (evidence, [])
        )
        # Explained along the 字汇书's own split (书中词条); older rows were not.
        result["book_aligned"] = bool(evidence.get("book_aligned")) if isinstance(evidence, dict) else False
        return result

    def save_card_etymology(self, term: str, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("etymology_zh") or "").strip()
        if not str(term or "").strip() or not text:
            raise ValueError("没有可保存的字源。")
        self._ensure_review_schema()
        backup = self._backup_for_review()
        values = (
            term, str(payload.get("verdict") or ""),
            json.dumps(payload.get("morphemes") or [], ensure_ascii=False), text,
            json.dumps({"points": payload.get("evidence") or [], "sources": payload.get("sources") or [],
                        "book_aligned": bool(payload.get("book_aligned"))}, ensure_ascii=False),
            json.dumps(payload.get("review_issues") or [], ensure_ascii=False),
            str(payload.get("model") or ""),
        )
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO vocabulary_card_etymology(term,verdict,morphemes_json,etymology_zh,evidence_json,review_json,model) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(term) DO UPDATE SET verdict=excluded.verdict,"
                    "morphemes_json=excluded.morphemes_json,etymology_zh=excluded.etymology_zh,"
                    "evidence_json=excluded.evidence_json,review_json=excluded.review_json,model=excluded.model,"
                    "checked_at=CURRENT_TIMESTAMP",
                    values,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        stored = self.card_etymology(term)
        if stored is None or stored["etymology_zh"] != text:
            raise RuntimeError(f"字源写后回读不一致。恢复备份：{backup}")
        return {"etymology": stored, "backup_path": backup, "readback_verified": True}

    def example_audio_noncommercial(self) -> bool:
        """Whether example sentences may use CC BY-NC(-ND) volunteer recordings (personal study).

        On unless the learner turned it off (the learner chose this on 2026-10-26), so
        neither machine needs a database write for the default.
        """

        if not self.database.is_file():
            return EXAMPLE_AUDIO_NONCOMMERCIAL_DEFAULT
        with closing(self.connect()) as connection:
            try:
                row = connection.execute(
                    "SELECT value FROM vocabulary_metadata WHERE key='example_audio_noncommercial'"
                ).fetchone()
            except sqlite3.Error:
                row = None
        return EXAMPLE_AUDIO_NONCOMMERCIAL_DEFAULT if row is None else str(row[0]) == "1"

    def set_example_audio_noncommercial(self, allowed: bool) -> dict[str, Any]:
        self.ensure_schema()
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO vocabulary_metadata(key,value) VALUES('example_audio_noncommercial',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if allowed else "0",),
            )
        stored = self.example_audio_noncommercial()
        if stored != bool(allowed):
            raise RuntimeError("例句录音授权设置写后回读不一致。")
        return {"example_audio_noncommercial": stored, "readback_verified": True}

    def review_queue(
        self,
        *,
        card_keys: Iterable[str] | None = None,
        new_limit: int | None = None,
        now: datetime | None = None,
        pending_cards: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Due learning cards, due reviews, then new cards in book order."""

        moment = now or datetime.now()
        limit = self.review_new_per_day() if new_limit is None else max(0, int(new_limit))
        selected = None if card_keys is None else {str(key) for key in card_keys}
        ahead = (moment + timedelta(minutes=REVIEW_LEARN_AHEAD_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        day_start = self.study_day_start(moment).strftime("%Y-%m-%d %H:%M:%S")
        day_end = self.study_day_end(moment).strftime("%Y-%m-%d %H:%M:%S")
        rows: list[dict[str, Any]] = []
        existing: set[str] = set()
        introduced = 0
        if self.database.is_file():
            with closing(self.connect(rows=True)) as connection:
                if self._has_review_tables(connection):
                    stored = [dict(row) for row in connection.execute(
                        "SELECT * FROM vocabulary_review_cards ORDER BY due_at, sort_order, id"
                    )]
                    existing = {row["card_key"] for row in stored}
                    rows = [row for row in stored if row["state"] != "suspended"]
                    introduced = int(connection.execute(
                        "SELECT COUNT(*) FROM vocabulary_review_cards WHERE introduced_at>=?",
                        (day_start,),
                    ).fetchone()[0])
        # Preview unintroduced source cards in memory. A read-only AI query
        # must not create review tables or change the learner's database.
        for number, candidate in enumerate(pending_cards):
            key = str(candidate["card_key"])
            if key not in existing:
                rows.append({
                    "state": "new", "due_at": None, "interval_days": 0,
                    "reps": 0, "lapses": 0, "id": number,
                    **candidate,
                })
                existing.add(key)
            elif candidate.get("collected_at"):
                for row in rows:
                    if row["card_key"] == key and not row.get("collected_at"):
                        row["collected_at"] = candidate["collected_at"]
                        break
        if selected is not None:
            rows = [row for row in rows if row["card_key"] in selected]
        learning = [row for row in rows if row["state"] in {"learning", "relearning"} and row["due_at"] and row["due_at"] <= ahead]
        review = [row for row in rows if row["state"] == "review" and row["due_at"] and row["due_at"] < day_end]
        remaining = limit if new_limit is not None else max(0, limit - introduced)
        # Collected words come first, in the order they were collected; then the book in reading order.
        new = sorted(
            (row for row in rows if row["state"] == "new"),
            key=lambda row: (not row.get("collected_at"), str(row.get("collected_at") or ""), row["sort_order"], row["id"]),
        )
        new = new[:remaining]
        for row, kind in [*((row, "learning") for row in learning), *((row, "review") for row in review), *((row, "new") for row in new)]:
            row["queue"] = kind
        return {
            "cards": [*learning, *review, *new],
            "counts": {"learning": len(learning), "review": len(review), "new": len(new)},
            "new_limit": limit,
            "new_introduced_today": introduced,
        }

    def review_summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        queue = self.review_queue(now=now)
        moment = now or datetime.now()
        reviewed = 0
        if self.database.is_file():
            with closing(self.connect()) as connection:
                if self._has_review_tables(connection):
                    reviewed = int(connection.execute(
                        "SELECT COUNT(*) FROM vocabulary_review_log WHERE reviewed_at>=?",
                        (self.study_day_start(moment).strftime("%Y-%m-%d %H:%M:%S"),),
                    ).fetchone()[0])
        return {
            "due": queue["counts"]["learning"] + queue["counts"]["review"],
            "new": queue["counts"]["new"],
            "reviewed_today": reviewed,
            "new_limit": queue["new_limit"],
        }

    def record_review(
        self,
        card_key: str,
        grade: str,
        *,
        mode: str = "recognize",
        answer: str = "",
        correct: bool | None = None,
        elapsed_ms: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply one answer: reschedule, log it, sync familiarity, and read the card back."""

        if grade not in REVIEW_GRADES:
            raise ValueError(f"评分只能是：{'、'.join(REVIEW_GRADES)}")
        moment = (now or datetime.now()).replace(microsecond=0)
        self._ensure_review_schema()
        if self.review_card(card_key) is None:
            raise KeyError(f"复习卡片不存在：{card_key}")
        backup = self._backup_for_review()
        with closing(self.connect(rows=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                before = dict(connection.execute(
                    "SELECT * FROM vocabulary_review_cards WHERE card_key=?", (card_key,)
                ).fetchone())
                after = schedule_review(before, grade, moment)
                stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
                connection.execute(
                    """
                    UPDATE vocabulary_review_cards SET
                        state=?,step_index=?,due_at=?,interval_days=?,ease=?,reps=?,lapses=?,
                        last_review_at=?,introduced_at=COALESCE(introduced_at,?),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        after["state"], after["step_index"], after["due_at"], after["interval_days"],
                        after["ease"], after["reps"], after["lapses"], stamp, stamp, before["id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO vocabulary_review_log(
                        card_id,reviewed_at,mode,grade,answer,correct,elapsed_ms,
                        state_before,interval_before,interval_after,due_after
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        before["id"], stamp, str(mode or "recognize"), grade,
                        str(answer or "")[:500], None if correct is None else int(bool(correct)),
                        max(0, int(elapsed_ms or 0)), before["state"], float(before["interval_days"] or 0),
                        after["interval_days"], after["due_at"],
                    ),
                )
                familiarity = None
                if before.get("vocabulary_entry_id"):
                    if after["state"] == "review" and after["interval_days"] >= REVIEW_FAMILIAR_INTERVAL_DAYS:
                        familiarity = "familiar"
                    elif grade == "again" and before["state"] == "review":
                        familiarity = "unfamiliar"
                    if familiarity is not None:
                        connection.execute(
                            "UPDATE vocabulary_entries SET familiarity=?,updated_at=CURRENT_TIMESTAMP "
                            "WHERE term=(SELECT term FROM vocabulary_entries WHERE id=?) AND familiarity<>?",
                            (familiarity, before["vocabulary_entry_id"], familiarity),
                        )
                pending = dict(connection.execute(
                    "SELECT * FROM vocabulary_review_cards WHERE id=?", (before["id"],)
                ).fetchone())
                if pending["state"] != after["state"] or pending["due_at"] != after["due_at"]:
                    raise RuntimeError("复习记录回读不一致，事务已取消。")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        readback = self.review_card(card_key)
        if readback is None or readback["due_at"] != after["due_at"] or readback["state"] != after["state"]:
            raise RuntimeError(f"复习记录已提交，但写后回读不一致。恢复备份：{backup}")
        return {
            "card": readback,
            "grade": grade,
            "mode": mode,
            "familiarity_changed_to": familiarity,
            "backup_path": backup,
            "readback_verified": True,
        }

    def review_log(self, card_key: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self.database.is_file():
            return []
        with closing(self.connect(rows=True)) as connection:
            if not self._has_review_tables(connection):
                return []
            where, args = ("WHERE c.card_key=?", [card_key]) if card_key else ("", [])
            rows = connection.execute(
                f"SELECT l.*,c.card_key,c.term FROM vocabulary_review_log l "
                f"JOIN vocabulary_review_cards c ON c.id=l.card_id {where} "
                "ORDER BY l.reviewed_at DESC,l.id DESC LIMIT ?",
                [*args, max(1, min(int(limit), 5000))],
            ).fetchall()
        return [dict(row) for row in rows]


VOCABULARY_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS vocabulary_review_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_key TEXT NOT NULL UNIQUE,
    vocabulary_entry_id INTEGER,
    term TEXT NOT NULL COLLATE NOCASE,
    deck TEXT NOT NULL DEFAULT 'raydu',
    unit_key TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'new'
        CHECK(state IN ('new','learning','review','relearning','suspended')),
    step_index INTEGER NOT NULL DEFAULT 0,
    due_at TEXT,
    interval_days REAL NOT NULL DEFAULT 0,
    ease REAL NOT NULL DEFAULT 2.5,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    last_review_at TEXT,
    introduced_at TEXT,
    looked_up_at TEXT,
    collected_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(vocabulary_entry_id) REFERENCES vocabulary_entries(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_vocabulary_review_cards_due
ON vocabulary_review_cards(state, due_at);
CREATE INDEX IF NOT EXISTS idx_vocabulary_review_cards_unit
ON vocabulary_review_cards(unit_key, sort_order);

CREATE TABLE IF NOT EXISTS vocabulary_review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'recognize',
    grade TEXT NOT NULL CHECK(grade IN ('again','hard','good','easy')),
    answer TEXT NOT NULL DEFAULT '',
    correct INTEGER,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    state_before TEXT NOT NULL DEFAULT '',
    interval_before REAL NOT NULL DEFAULT 0,
    interval_after REAL NOT NULL DEFAULT 0,
    due_after TEXT,
    FOREIGN KEY(card_id) REFERENCES vocabulary_review_cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vocabulary_review_log_time
ON vocabulary_review_log(reviewed_at, card_id);

CREATE TABLE IF NOT EXISTS vocabulary_card_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL COLLATE NOCASE,
    position INTEGER NOT NULL DEFAULT 0,
    en TEXT NOT NULL,
    zh TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'ai',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vocabulary_card_examples_term
ON vocabulary_card_examples(term, position);

CREATE TABLE IF NOT EXISTS vocabulary_card_etymology (
    term TEXT PRIMARY KEY COLLATE NOCASE,
    verdict TEXT NOT NULL,
    morphemes_json TEXT NOT NULL DEFAULT '[]',
    etymology_zh TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    review_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Anki-style SM-2.  Every answer is logged, so a later switch to FSRS can be
# fitted from vocabulary_review_log without migrating the cards.
REVIEW_GRADES = ("again", "hard", "good", "easy")
REVIEW_LEARNING_STEPS_MINUTES = (10, 1440)
REVIEW_RELEARNING_STEPS_MINUTES = (10,)
REVIEW_GRADUATING_INTERVAL_DAYS = 3.0
REVIEW_EASY_INTERVAL_DAYS = 4.0
REVIEW_MINIMUM_EASE = 1.3
REVIEW_DEFAULT_NEW_PER_DAY = 20
REVIEW_DAY_START_HOUR = 4
REVIEW_LEARN_AHEAD_MINUTES = 20
REVIEW_FAMILIAR_INTERVAL_DAYS = 21.0
# Tatoeba's volunteer recordings are mostly CC BY-NC-ND; they are played unchanged,
# with attribution, for personal study only.
EXAMPLE_AUDIO_NONCOMMERCIAL_DEFAULT = True


def schedule_review(card: dict[str, Any], grade: str, now: datetime) -> dict[str, Any]:
    """Next state of one card after ``grade``; pure, so it is easy to test."""

    if grade not in REVIEW_GRADES:
        raise ValueError(f"评分只能是：{'、'.join(REVIEW_GRADES)}")
    state = str(card.get("state") or "new")
    step = int(card.get("step_index") or 0)
    interval = float(card.get("interval_days") or 0.0)
    ease = float(card.get("ease") or 2.5)
    lapses = int(card.get("lapses") or 0)

    def after_minutes(minutes: float) -> datetime:
        return now + timedelta(minutes=minutes)

    def after_days(days: float) -> datetime:
        # Reviews fall due at the start of a study day, not at the exact minute.
        return VocabularyManager.study_day_start(now) + timedelta(days=days)

    if state in {"new", "learning"}:
        steps = REVIEW_LEARNING_STEPS_MINUTES
        if grade == "again":
            state, step, due = "learning", 0, after_minutes(steps[0])
        elif grade == "hard":
            state = "learning"
            current = steps[min(step, len(steps) - 1)]
            following = steps[min(step + 1, len(steps) - 1)]
            due = after_minutes((current + following) / 2 if step == 0 and len(steps) > 1 else current)
        elif grade == "good" and step + 1 < len(steps):
            state, step = "learning", step + 1
            due = after_minutes(steps[step])
        else:
            interval = REVIEW_EASY_INTERVAL_DAYS if grade == "easy" else REVIEW_GRADUATING_INTERVAL_DAYS
            state, step, due = "review", 0, after_days(interval)
    elif state == "relearning":
        steps = REVIEW_RELEARNING_STEPS_MINUTES
        if grade == "again":
            step, due = 0, after_minutes(steps[0])
        elif grade == "hard":
            due = after_minutes(steps[min(step, len(steps) - 1)] * 1.5)
        elif grade == "good" and step + 1 < len(steps):
            step += 1
            due = after_minutes(steps[step])
        else:
            if grade == "easy":
                interval += 1
            state, step, due = "review", 0, after_days(max(1.0, interval))
    else:
        if grade == "again":
            lapses += 1
            ease = max(REVIEW_MINIMUM_EASE, ease - 0.2)
            interval = max(1.0, float(round(interval * 0.5)))
            state, step, due = "relearning", 0, after_minutes(REVIEW_RELEARNING_STEPS_MINUTES[0])
        else:
            if grade == "hard":
                ease = max(REVIEW_MINIMUM_EASE, ease - 0.15)
                interval = max(interval + 1, interval * 1.2)
            elif grade == "good":
                interval = max(interval + 1, interval * ease)
            else:
                interval = max(interval + 1, interval * ease * 1.3)
                ease += 0.15
            interval = float(round(interval))
            state, due = "review", after_days(interval)
    return {
        "state": state,
        "step_index": step,
        "due_at": due.strftime("%Y-%m-%d %H:%M:%S"),
        "interval_days": round(interval, 2),
        "ease": round(ease, 3),
        "reps": int(card.get("reps") or 0) + 1,
        "lapses": lapses,
    }
