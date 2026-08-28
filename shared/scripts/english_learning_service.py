from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from contextlib import closing
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENGLISH_ROOT = ROOT_DIR / "English"
DEFAULT_DATABASE = DEFAULT_ENGLISH_ROOT / "data" / "english_learning.db"


XUAN_BOOKS: tuple[dict[str, Any], ...] = (
    {
        "material_code": "XY-GRAMMAR",
        "title": "旋元佑文法",
        "english_title": "Xuan Yu-You Grammar",
        "role": "grammar",
        "published_on": "2019-10-05",
        "isbn": "9789575325329",
        "page_count_hint": 648,
        "has_audio": 0,
    },
    {
        "material_code": "XY-VOCABULARY",
        "title": "旋元佑英文字彙",
        "english_title": "Xuan Yu-You English Vocabulary",
        "role": "vocabulary",
        "published_on": "2019-12-10",
        "isbn": "9789575325350",
        "page_count_hint": 648,
        "has_audio": 1,
    },
    {
        "material_code": "XY-GRAMMAR-EXERCISES",
        "title": "旋元佑文法解題",
        "english_title": "Xuan Yu-You Grammar Exercises",
        "role": "grammar_exercises",
        "published_on": "2020-05-01",
        "isbn": "9789575325428",
        "page_count_hint": 640,
        "has_audio": 0,
    },
    {
        "material_code": "XY-READING",
        "title": "旋元佑英文閱讀",
        "english_title": "Xuan Yu-You English Reading",
        "role": "reading_training",
        "published_on": "2021-05-10",
        "isbn": "9789575325763",
        "page_count_hint": 592,
        "has_audio": 0,
    },
    {
        "material_code": "XY-WRITING",
        "title": "旋元佑英文寫作",
        "english_title": "Xuan Yu-You English Writing",
        "role": "writing",
        "published_on": "2022-04-01",
        "isbn": "9789575326005",
        "page_count_hint": 552,
        "has_audio": 0,
    },
)


GRAMMAR_CHAPTERS: tuple[tuple[str, str], ...] = (
    ("Part 1 · Simple Sentences", "Basic Sentence Patterns"),
    ("Part 1 · Simple Sentences", "Noun Phrases"),
    ("Part 1 · Simple Sentences", "Pronouns"),
    ("Part 1 · Simple Sentences", "Adjectives"),
    ("Part 1 · Simple Sentences", "Adverbs"),
    ("Part 1 · Simple Sentences", "Comparison"),
    ("Part 1 · Simple Sentences", "Prepositions"),
    ("Part 1 · Simple Sentences", "Participles"),
    ("Part 1 · Simple Sentences", "Tense and Aspect"),
    ("Part 1 · Simple Sentences", "Voice"),
    ("Part 1 · Simple Sentences", "Modal Auxiliaries"),
    ("Part 1 · Simple Sentences", "Mood"),
    ("Part 1 · Simple Sentences", "Gerunds"),
    ("Part 1 · Simple Sentences", "Infinitive Phrases"),
    ("Part 1 · Simple Sentences", "Coordinating Conjunctions"),
    ("Part 2 · Complex and Compound Sentences", "Coordinate Clauses"),
    ("Part 2 · Complex and Compound Sentences", "Noun Clauses"),
    ("Part 2 · Complex and Compound Sentences", "Adverb Clauses"),
    ("Part 2 · Complex and Compound Sentences", "Relative Clauses"),
    ("Part 2 · Complex and Compound Sentences", "Subject–Verb Agreement"),
    ("Part 3 · Reduced Clauses", "Inversion"),
    ("Part 3 · Reduced Clauses", "Reduced Clauses"),
    ("Part 3 · Reduced Clauses", "Reduced Relative Clauses"),
    ("Part 3 · Reduced Clauses", "Reduced Noun Clauses"),
    ("Part 3 · Reduced Clauses", "Reduced Adverb Clauses"),
)


ENGLISH_SCHEMA = r"""
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS english_programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    pedagogy TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS english_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_code TEXT NOT NULL UNIQUE,
    program_id INTEGER,
    title TEXT NOT NULL,
    english_title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    isbn TEXT NOT NULL DEFAULT '',
    published_on TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'extensive_reading',
    material_type TEXT NOT NULL DEFAULT 'book',
    source_kind TEXT NOT NULL DEFAULT 'local_file',
    source_path TEXT NOT NULL DEFAULT '',
    reading_path TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    text_layer_status TEXT NOT NULL DEFAULT 'not_checked',
    page_count INTEGER NOT NULL DEFAULT 0,
    page_count_hint INTEGER NOT NULL DEFAULT 0,
    reading_status TEXT NOT NULL DEFAULT 'unread',
    last_page INTEGER NOT NULL DEFAULT 1,
    last_anchor_y REAL NOT NULL DEFAULT 0,
    suitability TEXT NOT NULL DEFAULT '',
    interest TEXT NOT NULL DEFAULT '',
    authoritative INTEGER NOT NULL DEFAULT 0 CHECK(authoritative IN (0,1)),
    has_audio INTEGER NOT NULL DEFAULT 0 CHECK(has_audio IN (0,1)),
    notes TEXT NOT NULL DEFAULT '',
    last_opened_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(program_id) REFERENCES english_programs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_english_materials_role_status
ON english_materials(role, reading_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_english_materials_hash
ON english_materials(source_hash);

CREATE TABLE IF NOT EXISTS english_material_chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    part_title TEXT NOT NULL DEFAULT '',
    chapter_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    progress_status TEXT NOT NULL DEFAULT 'not_started',
    progress_note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE CASCADE,
    UNIQUE(material_id, chapter_number)
);

CREATE TABLE IF NOT EXISTS english_chapter_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_chapter_id INTEGER NOT NULL,
    to_chapter_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(from_chapter_id) REFERENCES english_material_chapters(id) ON DELETE CASCADE,
    FOREIGN KEY(to_chapter_id) REFERENCES english_material_chapters(id) ON DELETE CASCADE,
    UNIQUE(from_chapter_id, to_chapter_id, relation_type)
);

CREATE TABLE IF NOT EXISTS english_reading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'extensive',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    start_page INTEGER NOT NULL DEFAULT 1,
    end_page INTEGER NOT NULL DEFAULT 1,
    lookup_count INTEGER NOT NULL DEFAULT 0,
    deferred_lookup_count INTEGER NOT NULL DEFAULT 0,
    encounter_count INTEGER NOT NULL DEFAULT 0,
    new_vocabulary_count INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS english_deferred_lookups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    session_id INTEGER,
    selected_text TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    page_number INTEGER NOT NULL DEFAULT 1,
    anchor_y REAL NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES english_reading_sessions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS english_usage_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_kind TEXT NOT NULL DEFAULT 'sentence',
    text TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    material_id INTEGER,
    page_number INTEGER,
    anchor_y REAL NOT NULL DEFAULT 0,
    user_note TEXT NOT NULL DEFAULT '',
    agent_analysis TEXT NOT NULL DEFAULT '',
    writing_technique TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_english_usage_material_page
ON english_usage_items(material_id, page_number, created_at DESC);

CREATE TABLE IF NOT EXISTS english_grammar_concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    framework TEXT NOT NULL DEFAULT 'xuan',
    material_id INTEGER,
    chapter_id INTEGER,
    source_page INTEGER,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE SET NULL,
    FOREIGN KEY(chapter_id) REFERENCES english_material_chapters(id) ON DELETE SET NULL,
    UNIQUE(canonical_name, framework, material_id, chapter_id)
);

CREATE TABLE IF NOT EXISTS english_grammar_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    terminology_source TEXT NOT NULL DEFAULT 'common',
    relation_type TEXT NOT NULL DEFAULT 'alias',
    FOREIGN KEY(concept_id) REFERENCES english_grammar_concepts(id) ON DELETE CASCADE,
    UNIQUE(concept_id, alias, terminology_source)
);

CREATE TABLE IF NOT EXISTS english_grammar_encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER,
    material_id INTEGER NOT NULL,
    selected_sentence TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    page_number INTEGER NOT NULL DEFAULT 1,
    anchor_y REAL NOT NULL DEFAULT 0,
    analysis TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(concept_id) REFERENCES english_grammar_concepts(id) ON DELETE SET NULL,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS english_grammar_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    chapter_id INTEGER,
    question_reference TEXT NOT NULL,
    source_page INTEGER,
    grammar_concept_id INTEGER,
    explanation_reference TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE CASCADE,
    FOREIGN KEY(chapter_id) REFERENCES english_material_chapters(id) ON DELETE SET NULL,
    FOREIGN KEY(grammar_concept_id) REFERENCES english_grammar_concepts(id) ON DELETE SET NULL,
    UNIQUE(material_id, question_reference)
);

CREATE TABLE IF NOT EXISTS english_exercise_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exercise_id INTEGER NOT NULL,
    selected_answer TEXT NOT NULL DEFAULT '',
    correct_answer TEXT NOT NULL DEFAULT '',
    is_correct INTEGER CHECK(is_correct IS NULL OR is_correct IN (0,1)),
    mistake_reason TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(exercise_id) REFERENCES english_grammar_exercises(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS english_misconceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER,
    exercise_id INTEGER,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    FOREIGN KEY(concept_id) REFERENCES english_grammar_concepts(id) ON DELETE SET NULL,
    FOREIGN KEY(exercise_id) REFERENCES english_grammar_exercises(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS english_morphemes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_form TEXT NOT NULL,
    morpheme_type TEXT NOT NULL CHECK(morpheme_type IN ('prefix','root','suffix')),
    variants TEXT NOT NULL DEFAULT '',
    semantic_idea TEXT NOT NULL DEFAULT '',
    material_id INTEGER,
    source_page INTEGER,
    source_kind TEXT NOT NULL DEFAULT 'user',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE SET NULL,
    UNIQUE(canonical_form, morpheme_type, material_id, source_page)
);

CREATE TABLE IF NOT EXISTS english_vocabulary_morphemes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vocabulary_entry_id INTEGER NOT NULL,
    morpheme_id INTEGER NOT NULL,
    position_index INTEGER NOT NULL DEFAULT 0,
    surface_segment TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'confirmed',
    source_kind TEXT NOT NULL DEFAULT 'user',
    FOREIGN KEY(morpheme_id) REFERENCES english_morphemes(id) ON DELETE CASCADE,
    UNIQUE(vocabulary_entry_id, morpheme_id, position_index)
);

CREATE TABLE IF NOT EXISTS english_etymologies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vocabulary_entry_id INTEGER NOT NULL,
    decomposition TEXT NOT NULL DEFAULT '',
    semantic_development TEXT NOT NULL DEFAULT '',
    word_family TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'unverified',
    source_kind TEXT NOT NULL DEFAULT 'user',
    material_id INTEGER,
    source_page INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS english_reading_training_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    chapter_id INTEGER,
    passage_reference TEXT NOT NULL,
    question_type TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER,
    correct_count INTEGER,
    question_count INTEGER,
    error_reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE CASCADE,
    FOREIGN KEY(chapter_id) REFERENCES english_material_chapters(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS english_writing_practices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    material_id INTEGER,
    chapter_id INTEGER,
    status TEXT NOT NULL DEFAULT 'drafting',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE SET NULL,
    FOREIGN KEY(chapter_id) REFERENCES english_material_chapters(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS english_writing_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    practice_id INTEGER NOT NULL,
    revision_number INTEGER NOT NULL,
    revision_kind TEXT NOT NULL DEFAULT 'user',
    content TEXT NOT NULL,
    diagnostic_feedback TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(practice_id) REFERENCES english_writing_practices(id) ON DELETE CASCADE,
    UNIQUE(practice_id, revision_number)
);

CREATE TABLE IF NOT EXISTS english_audio_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER,
    title TEXT NOT NULL,
    resource_kind TEXT NOT NULL DEFAULT 'local_file',
    path_or_url TEXT NOT NULL,
    track_number INTEGER,
    chapter_id INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE CASCADE,
    FOREIGN KEY(chapter_id) REFERENCES english_material_chapters(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS english_shadowing_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER,
    usage_item_id INTEGER,
    source_text TEXT NOT NULL,
    source_audio_id INTEGER,
    user_recording_path TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(material_id) REFERENCES english_materials(id) ON DELETE SET NULL,
    FOREIGN KEY(usage_item_id) REFERENCES english_usage_items(id) ON DELETE SET NULL,
    FOREIGN KEY(source_audio_id) REFERENCES english_audio_resources(id) ON DELETE SET NULL
);
"""


class _VisibleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data).strip()
        if value:
            self.parts.append(value)


class _ClosingConnection(sqlite3.Connection):
    """SQLite context manager that also releases the Windows file handle."""

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_component(value: str, fallback: str = "material") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return cleaned or fallback


def _row_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


class EnglishLearningService:
    """Domain service for English materials, reading activity and language learning.

    The service intentionally owns no Qt widgets.  Both UI actions and registered
    AI operations call these methods so that validation, backups and readback are
    shared.  Mathematical problem tables remain outside this model.
    """

    def __init__(self, root: Path | str = DEFAULT_ENGLISH_ROOT) -> None:
        self.root = Path(root).resolve()
        self.database_path = self.root / "data" / "english_learning.db"
        self.backup_dir = self.root / "backups"
        self.material_root = self.root / "materials"
        self.original_dir = self.material_root / "originals"
        self.reading_dir = self.material_root / "reading"
        self.audio_dir = self.root / "audio"
        self.writing_dir = self.root / "writing"
        for directory in (
            self.database_path.parent,
            self.backup_dir,
            self.original_dir,
            self.reading_dir,
            self.audio_dir,
            self.writing_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def connect(self, *, rows: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            factory=_ClosingConnection,
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if rows:
            connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> None:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"英语学习数据库完整性检查失败：{result}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"英语学习数据库存在外键错误：{foreign_keys}")

    def backup(self, reason: str) -> Path | None:
        if not self.database_path.is_file():
            return None
        safe = _safe_component(reason, "change")
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        target = self.backup_dir / f"english_learning_before_{safe}_{stamp}.db"
        with closing(sqlite3.connect(self.database_path, timeout=30)) as source:
            with closing(sqlite3.connect(target, timeout=30)) as destination:
                self._integrity(source)
                source.backup(destination)
                destination.commit()
        shutil.copy2(target, self.backup_dir / "english_learning_latest.db")
        return target

    def ensure_schema(self) -> None:
        existed = self.database_path.is_file() and self.database_path.stat().st_size > 0
        needs_backup = False
        if existed:
            with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
                needs_backup = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='english_programs'"
                ).fetchone() is None
        if needs_backup:
            self.backup("schema_migration")
        with self.connect() as connection:
            connection.executescript(ENGLISH_SCHEMA)
            self._seed_foundation(connection)
            self._integrity(connection)
            connection.commit()

    def _seed_foundation(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO english_programs(program_code,title,description,pedagogy)
            VALUES('XY-FOUNDATION','旋元佑英语基础',?,?)
            ON CONFLICT(program_code) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                pedagogy=excluded.pedagogy
            """,
            (
                "旋元佑五书相互连接的基础课程体系。",
                "Etymological analysis + sentence-pattern analysis + extensive reading",
            ),
        )
        program_id = int(
            connection.execute(
                "SELECT id FROM english_programs WHERE program_code='XY-FOUNDATION'"
            ).fetchone()[0]
        )
        for item in XUAN_BOOKS:
            connection.execute(
                """
                INSERT INTO english_materials(
                    material_code,program_id,title,english_title,author,publisher,isbn,
                    published_on,role,material_type,page_count_hint,authoritative,has_audio
                ) VALUES(?,?,?,?,?,?,?,?,?,'book',?,1,?)
                ON CONFLICT(material_code) DO UPDATE SET
                    program_id=excluded.program_id,
                    title=excluded.title,
                    english_title=excluded.english_title,
                    author=excluded.author,
                    publisher=excluded.publisher,
                    isbn=excluded.isbn,
                    published_on=excluded.published_on,
                    role=excluded.role,
                    page_count_hint=excluded.page_count_hint,
                    authoritative=1,
                    has_audio=excluded.has_audio
                """,
                (
                    item["material_code"], program_id, item["title"], item["english_title"],
                    "旋元佑", "眾文圖書", item["isbn"], item["published_on"],
                    item["role"], item["page_count_hint"], item["has_audio"],
                ),
            )
        material_ids = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT material_code,id FROM english_materials WHERE program_id=?",
                (program_id,),
            )
        }
        grammar_id = material_ids["XY-GRAMMAR"]
        exercise_id = material_ids["XY-GRAMMAR-EXERCISES"]
        for number, (part_title, title) in enumerate(GRAMMAR_CHAPTERS, 1):
            for material_id in (grammar_id, exercise_id):
                connection.execute(
                    """
                    INSERT INTO english_material_chapters(
                        material_id,part_title,chapter_number,title
                    ) VALUES(?,?,?,?)
                    ON CONFLICT(material_id,chapter_number) DO UPDATE SET
                        part_title=excluded.part_title,title=excluded.title
                    """,
                    (material_id, part_title, number, title),
                )
            grammar_chapter = int(connection.execute(
                "SELECT id FROM english_material_chapters WHERE material_id=? AND chapter_number=?",
                (grammar_id, number),
            ).fetchone()[0])
            exercise_chapter = int(connection.execute(
                "SELECT id FROM english_material_chapters WHERE material_id=? AND chapter_number=?",
                (exercise_id, number),
            ).fetchone()[0])
            connection.execute(
                """
                INSERT OR IGNORE INTO english_chapter_links(
                    from_chapter_id,to_chapter_id,relation_type
                ) VALUES(?,?,'theory_to_exercise')
                """,
                (grammar_chapter, exercise_chapter),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO english_chapter_links(
                    from_chapter_id,to_chapter_id,relation_type
                ) VALUES(?,?,'exercise_to_theory')
                """,
                (exercise_chapter, grammar_chapter),
            )

    def summary(self) -> dict[str, Any]:
        with self.connect(rows=True) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM english_materials),
                    (SELECT COUNT(*) FROM english_materials WHERE source_path<>''),
                    (SELECT COUNT(*) FROM english_materials WHERE reading_status='reading'),
                    (SELECT COUNT(*) FROM english_usage_items),
                    (SELECT COUNT(*) FROM english_grammar_encounters),
                    (SELECT COUNT(*) FROM english_misconceptions WHERE resolved=0),
                    (SELECT COUNT(*) FROM english_writing_practices)
                """
            ).fetchone()
            recent = connection.execute(
                """
                SELECT * FROM english_materials
                WHERE last_opened_at IS NOT NULL
                ORDER BY last_opened_at DESC LIMIT 5
                """
            ).fetchall()
        return {
            "material_count": int(counts[0]),
            "bound_material_count": int(counts[1]),
            "reading_count": int(counts[2]),
            "usage_count": int(counts[3]),
            "grammar_encounter_count": int(counts[4]),
            "unresolved_misconception_count": int(counts[5]),
            "writing_practice_count": int(counts[6]),
            "recent_materials": [dict(row) for row in recent],
        }

    def list_programs(self) -> list[dict[str, Any]]:
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                "SELECT * FROM english_programs ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_materials(
        self,
        *,
        role: str = "",
        status: str = "",
        keyword: str = "",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if role:
            clauses.append("m.role=?")
            args.append(role)
        if status:
            clauses.append("m.reading_status=?")
            args.append(status)
        if keyword.strip():
            pattern = f"%{keyword.strip()}%"
            clauses.append(
                "(m.title LIKE ? OR m.english_title LIKE ? OR m.author LIKE ? "
                "OR m.notes LIKE ? OR m.material_code LIKE ?)"
            )
            args.extend([pattern] * 5)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT m.*,p.title AS program_title,
                       (SELECT COUNT(*) FROM english_material_chapters c
                        WHERE c.material_id=m.id) AS chapter_count
                FROM english_materials m
                LEFT JOIN english_programs p ON p.id=m.program_id
                {where}
                ORDER BY CASE m.role
                    WHEN 'grammar' THEN 1 WHEN 'grammar_exercises' THEN 2
                    WHEN 'vocabulary' THEN 3 WHEN 'reading_training' THEN 4
                    WHEN 'writing' THEN 5 ELSE 10 END,
                    m.last_opened_at DESC,m.updated_at DESC,m.id
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def material(self, material_id: int) -> dict[str, Any] | None:
        with self.connect(rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM english_materials WHERE id=?", (int(material_id),)
            ).fetchone()
        return _row_dict(row)

    def material_by_code(self, code: str) -> dict[str, Any] | None:
        with self.connect(rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM english_materials WHERE material_code=?", (str(code),)
            ).fetchone()
        return _row_dict(row)

    def material_for_pdf(self, path: Path | str) -> dict[str, Any] | None:
        try:
            target = os.path.normcase(str(Path(path).resolve()))
        except (OSError, ValueError):
            return None
        for row in self.list_materials():
            for field in ("reading_path", "source_path"):
                raw = str(row.get(field) or "").strip()
                if not raw:
                    continue
                try:
                    if os.path.normcase(str(Path(raw).resolve())) == target:
                        return row
                except (OSError, ValueError):
                    continue
        return None

    def chapters(self, material_id: int) -> list[dict[str, Any]]:
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM english_material_chapters
                WHERE material_id=? ORDER BY chapter_number
                """,
                (int(material_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_chapter_progress(
        self,
        chapter_id: int,
        progress_status: str,
        *,
        progress_note: str = "",
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> dict[str, Any]:
        valid = {"not_started", "reading", "practising", "reviewing", "completed"}
        status = str(progress_status or "").strip()
        if status not in valid:
            raise ValueError("章节状态无效。")
        if page_start is not None and int(page_start) < 1:
            raise ValueError("章节起始页必须大于零。")
        if page_end is not None and int(page_end) < 1:
            raise ValueError("章节结束页必须大于零。")
        if page_start is not None and page_end is not None and int(page_end) < int(page_start):
            raise ValueError("章节结束页不能早于起始页。")
        backup = self.backup("chapter_progress")
        with self.connect(rows=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE english_material_chapters
                SET progress_status=?,progress_note=?,page_start=COALESCE(?,page_start),
                    page_end=COALESCE(?,page_end),updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status, str(progress_note or "").strip(), page_start, page_end, int(chapter_id)),
            )
            if connection.total_changes != 1:
                connection.rollback()
                raise ValueError("英语章节不存在。")
            self._integrity(connection)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM english_material_chapters WHERE id=?", (int(chapter_id),)
            ).fetchone()
        result = dict(row)
        result.update({"backup_path": str(backup or ""), "readback_verified": True})
        return result

    @staticmethod
    def inspect_pdf_text_layer(path: Path | str) -> dict[str, Any]:
        import fitz

        pdf_path = Path(path).resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
        with fitz.open(pdf_path) as document:
            page_count = int(document.page_count)
            if page_count <= 0:
                return {"status": "empty", "page_count": 0, "text_pages": 0, "character_count": 0}
            sample_indexes = sorted({0, page_count // 4, page_count // 2, (3 * page_count) // 4, page_count - 1})
            text_pages = 0
            character_count = 0
            word_count = 0
            for page_index in sample_indexes:
                page = document.load_page(page_index)
                text = str(page.get_text("text") or "")
                words = page.get_text("words") or []
                visible = len(re.sub(r"\s+", "", text))
                character_count += visible
                word_count += len(words)
                if visible >= 20 and words:
                    text_pages += 1
        ratio = text_pages / max(1, len(sample_indexes))
        # Short quotations and practice sentences can be perfectly selectable;
        # the text-layer test should measure actual words, not article length.
        status = (
            "selectable"
            if ratio >= 0.6 and character_count >= 20 and word_count >= 3
            else "scanned_or_unselectable"
        )
        return {
            "status": status,
            "page_count": page_count,
            "sampled_pages": len(sample_indexes),
            "text_pages": text_pages,
            "character_count": character_count,
            "word_count": word_count,
        }

    @staticmethod
    def ocr_status() -> dict[str, Any]:
        executable = shutil.which("ocrmypdf")
        tesseract = shutil.which("tesseract")
        return {
            "available": bool(executable),
            "backend": "OCRmyPDF + Tesseract" if executable else "none",
            "ocrmypdf": executable or "",
            "tesseract": tesseract or "",
            "note": (
                "可生成不覆盖原件的可搜索阅读副本。" if executable
                else "未检测到 OCRmyPDF；扫描件仍保留原件并明确标为不可选词。"
            ),
        }

    def create_searchable_reading_copy(self, material_id: int) -> dict[str, Any]:
        material = self.material(material_id)
        if material is None:
            raise ValueError("英语材料不存在。")
        source = Path(str(material.get("source_path") or ""))
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise ValueError("只有已经绑定的 PDF 扫描件需要 OCR 阅读副本。")
        status = self.ocr_status()
        if not status["available"]:
            raise RuntimeError(status["note"])
        backup = self.backup("material_ocr")
        target = self.reading_dir / f"{_safe_component(str(material['material_code']))}-ocr.pdf"
        temporary = target.with_suffix(".tmp.pdf")
        temporary.unlink(missing_ok=True)
        command = [
            str(status["ocrmypdf"]), "--skip-text", "--deskew", "--rotate-pages",
            "--optimize", "1", str(source), str(temporary),
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=3600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            raise RuntimeError("OCR 阅读副本生成失败：" + (completed.stderr or completed.stdout)[-2000:])
        inspection = self.inspect_pdf_text_layer(temporary)
        if inspection["status"] != "selectable":
            temporary.unlink(missing_ok=True)
            raise RuntimeError("OCR 产物没有通过真实文字层选词检查；原件与既有阅读副本未修改。")
        os.replace(temporary, target)
        with self.connect(rows=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE english_materials SET reading_path=?,text_layer_status='selectable',
                    page_count=?,updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (str(target), int(inspection["page_count"]), int(material_id)),
            )
            self._integrity(connection)
            connection.commit()
            row = connection.execute("SELECT * FROM english_materials WHERE id=?", (int(material_id),)).fetchone()
        result = dict(row)
        result.update({"backup_path": str(backup or ""), "inspection": inspection, "readback_verified": True})
        return result

    @staticmethod
    def _extract_docx_text(path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs: list[str] = []
        for paragraph in root.iter(namespace + "p"):
            text = "".join(node.text or "" for node in paragraph.iter(namespace + "t")).strip()
            if text:
                paragraphs.append(text)
        return "\n\n".join(paragraphs)

    @staticmethod
    def extract_text(path: Path | str) -> str:
        source = Path(path)
        suffix = source.suffix.lower()
        if suffix in {".txt", ".md", ".markdown", ".tex"}:
            for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
                try:
                    value = source.read_text(encoding=encoding)
                    break
                except UnicodeError:
                    continue
            else:
                raise ValueError(f"无法识别文本编码：{source.name}")
            if suffix in {".md", ".markdown"}:
                value = re.sub(r"```.*?```", "", value, flags=re.DOTALL)
                value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
                value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
                value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.MULTILINE)
                value = re.sub(r"[*_`~]", "", value)
            return value.strip()
        if suffix in {".html", ".htm"}:
            parser = _VisibleTextExtractor()
            parser.feed(source.read_text(encoding="utf-8", errors="replace"))
            return "\n\n".join(parser.parts).strip()
        if suffix == ".docx":
            return EnglishLearningService._extract_docx_text(source)
        raise ValueError("当前阅读导入支持 PDF、TXT、Markdown、HTML、DOCX 和 TeX。")

    @staticmethod
    def _paragraph_chunks(text: str, max_chars: int = 2600) -> list[str]:
        paragraphs = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n\s*\n", text)]
        paragraphs = [item for item in paragraphs if item]
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for paragraph in paragraphs:
            pieces = [paragraph[index:index + max_chars] for index in range(0, len(paragraph), max_chars)] or [""]
            for piece in pieces:
                if current and size + len(piece) > max_chars:
                    chunks.append("\n\n".join(current))
                    current = []
                    size = 0
                current.append(piece)
                size += len(piece)
        if current:
            chunks.append("\n\n".join(current))
        return chunks or [""]

    @staticmethod
    def build_reading_pdf(text: str, target: Path | str, title: str) -> Path:
        import fitz

        output = Path(target)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp.pdf")
        document = fitz.open()
        css = "body{font-family:serif;font-size:11pt;line-height:1.5;color:#172033} h1{font-size:20pt} p{margin:0 0 9pt 0}"
        chunks = EnglishLearningService._paragraph_chunks(text)
        for index, chunk in enumerate(chunks):
            page = document.new_page(width=595, height=842)
            heading = f"<h1>{html.escape(title)}</h1>" if index == 0 else ""
            paragraphs = "".join(
                f"<p>{html.escape(item)}</p>"
                for item in re.split(r"\n\s*\n", chunk)
                if item.strip()
            )
            page.insert_htmlbox(fitz.Rect(54, 50, 541, 792), heading + paragraphs, css=css, scale_low=0.72)
        metadata = document.metadata or {}
        metadata.update({"title": title, "author": "English Learning Project", "subject": "Selectable reading copy"})
        document.set_metadata(metadata)
        document.save(temporary, garbage=4, deflate=True)
        document.close()
        check = EnglishLearningService.inspect_pdf_text_layer(temporary)
        if check["status"] != "selectable":
            temporary.unlink(missing_ok=True)
            raise RuntimeError("生成的阅读 PDF 没有通过文字层与可选词检查。")
        os.replace(temporary, output)
        return output

    def _next_material_code(self, connection: sqlite3.Connection, prefix: str = "ENG-M") -> str:
        rows = connection.execute(
            "SELECT material_code FROM english_materials WHERE material_code LIKE ?",
            (prefix + "%",),
        ).fetchall()
        largest = 0
        for row in rows:
            match = re.fullmatch(re.escape(prefix) + r"(\d+)", str(row[0]))
            if match:
                largest = max(largest, int(match.group(1)))
        return f"{prefix}{largest + 1:05d}"

    def import_material(
        self,
        source_path: Path | str,
        *,
        title: str = "",
        role: str = "extensive_reading",
        material_type: str = "document",
        program_code: str = "",
        author: str = "",
        source_url: str = "",
        material_code: str = "",
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in {".pdf", ".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".tex"}:
            raise ValueError("不支持的英语材料格式。")
        title = str(title or source.stem).strip()
        digest = _sha256(source)
        backup = self.backup("material_import")
        with self.connect() as connection:
            duplicate = connection.execute(
                "SELECT id FROM english_materials WHERE source_hash=? AND source_hash<>''",
                (digest,),
            ).fetchone()
            if duplicate is not None:
                raise ValueError(f"相同原件已经导入，材料 ID={int(duplicate[0])}。")
            code = material_code.strip() or self._next_material_code(connection)
            program_id = None
            if program_code.strip():
                row = connection.execute(
                    "SELECT id FROM english_programs WHERE program_code=?", (program_code.strip(),)
                ).fetchone()
                if row is None:
                    raise ValueError(f"英语课程体系不存在：{program_code}")
                program_id = int(row[0])
        safe = _safe_component(code)
        original = self.original_dir / f"{safe}{source.suffix.lower()}"
        temporary_original = original.with_suffix(original.suffix + ".tmp")
        shutil.copy2(source, temporary_original)
        os.replace(temporary_original, original)
        reading_path = original
        if source.suffix.lower() == ".pdf":
            inspection = self.inspect_pdf_text_layer(original)
        else:
            reading_path = self.reading_dir / f"{safe}.pdf"
            self.build_reading_pdf(self.extract_text(original), reading_path, title)
            inspection = self.inspect_pdf_text_layer(reading_path)
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO english_materials(
                        material_code,program_id,title,author,role,material_type,
                        source_path,reading_path,source_url,source_hash,text_layer_status,
                        page_count,reading_status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'unread')
                    """,
                    (
                        code, program_id, title, author.strip(), role.strip() or "extensive_reading",
                        material_type.strip() or "document", str(original), str(reading_path),
                        source_url.strip(), digest, inspection["status"], int(inspection["page_count"]),
                    ),
                )
                material_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                original.unlink(missing_ok=True)
                if reading_path != original:
                    reading_path.unlink(missing_ok=True)
                raise
            row = connection.execute("SELECT * FROM english_materials WHERE id=?", (material_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        result["inspection"] = inspection
        return result

    def bind_material_file(self, material_id: int, source_path: Path | str) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        material = self.material(material_id)
        if material is None:
            raise ValueError("英语材料不存在。")
        if not source.is_file():
            raise FileNotFoundError(source)
        old_source = str(material.get("source_path") or "")
        old_reading = str(material.get("reading_path") or "")
        backup = self.backup("material_rebind")
        safe = _safe_component(str(material["material_code"]))
        original = self.original_dir / f"{safe}{source.suffix.lower()}"
        tmp = original.with_suffix(original.suffix + ".tmp")
        shutil.copy2(source, tmp)
        os.replace(tmp, original)
        reading = original
        if source.suffix.lower() == ".pdf":
            inspection = self.inspect_pdf_text_layer(original)
        else:
            reading = self.reading_dir / f"{safe}.pdf"
            self.build_reading_pdf(self.extract_text(original), reading, str(material["title"]))
            inspection = self.inspect_pdf_text_layer(reading)
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE english_materials SET source_path=?,reading_path=?,source_hash=?,
                        text_layer_status=?,page_count=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (str(original), str(reading), _sha256(source), inspection["status"], inspection["page_count"], int(material_id)),
                )
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            row = connection.execute("SELECT * FROM english_materials WHERE id=?", (int(material_id),)).fetchone()
        result = dict(row)
        result.update({"backup_path": str(backup or ""), "inspection": inspection, "previous_source": old_source, "previous_reading": old_reading})
        return result

    def update_reading_position(
        self,
        material_id: int,
        page_number: int,
        anchor_y: float = 0.0,
        *,
        status: str = "reading",
    ) -> dict[str, Any]:
        valid_statuses = {"unread", "reading", "completed"}
        if status not in valid_statuses:
            raise ValueError("阅读状态无效。")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                UPDATE english_materials SET last_page=?,last_anchor_y=?,reading_status=?,
                    last_opened_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (max(1, int(page_number)), max(0.0, float(anchor_y)), status, int(material_id)),
            )
            if connection.total_changes != 1:
                raise ValueError("英语材料不存在。")
            connection.commit()
            row = connection.execute("SELECT * FROM english_materials WHERE id=?", (int(material_id),)).fetchone()
        return dict(row)

    def start_reading_session(self, material_id: int, *, mode: str = "extensive") -> dict[str, Any]:
        material = self.material(material_id)
        if material is None:
            raise ValueError("英语材料不存在。")
        with self.connect(rows=True) as connection:
            connection.execute(
                "INSERT INTO english_reading_sessions(material_id,mode,start_page,end_page) VALUES(?,?,?,?)",
                (int(material_id), mode, int(material.get("last_page") or 1), int(material.get("last_page") or 1)),
            )
            session_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.commit()
            row = connection.execute("SELECT * FROM english_reading_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row)

    def finish_reading_session(self, session_id: int, *, end_page: int, note: str = "") -> dict[str, Any]:
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                UPDATE english_reading_sessions SET ended_at=CURRENT_TIMESTAMP,end_page=?,note=?
                WHERE id=? AND ended_at IS NULL
                """,
                (max(1, int(end_page)), str(note), int(session_id)),
            )
            if connection.total_changes != 1:
                raise ValueError("阅读会话不存在或已经结束。")
            connection.commit()
            row = connection.execute("SELECT * FROM english_reading_sessions WHERE id=?", (int(session_id),)).fetchone()
        return dict(row)

    def mark_for_later(
        self,
        material_id: int,
        selected_text: str,
        *,
        context: str = "",
        page_number: int = 1,
        anchor_y: float = 0.0,
        session_id: int | None = None,
    ) -> dict[str, Any]:
        selected_text = re.sub(r"\s+", " ", selected_text).strip()
        if not selected_text:
            raise ValueError("没有可标记的文本。")
        backup = self.backup("deferred_lookup")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_deferred_lookups(
                    material_id,session_id,selected_text,context,page_number,anchor_y
                ) VALUES(?,?,?,?,?,?)
                """,
                (int(material_id), session_id, selected_text, context, max(1, int(page_number)), max(0.0, float(anchor_y))),
            )
            item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            if session_id:
                connection.execute(
                    "UPDATE english_reading_sessions SET deferred_lookup_count=deferred_lookup_count+1 WHERE id=?",
                    (int(session_id),),
                )
            connection.commit()
            row = connection.execute("SELECT * FROM english_deferred_lookups WHERE id=?", (item_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def deferred_lookups(self, *, resolved: bool | None = False, keyword: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if resolved is not None:
            clauses.append("d.resolved=?")
            args.append(int(bool(resolved)))
        if keyword.strip():
            pattern = f"%{keyword.strip()}%"
            clauses.append("(d.selected_text LIKE ? OR d.context LIKE ? OR m.title LIKE ?)")
            args.extend([pattern, pattern, pattern])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT d.*,m.title AS material_title,m.reading_path
                FROM english_deferred_lookups d
                JOIN english_materials m ON m.id=d.material_id
                {where}
                ORDER BY d.created_at DESC,d.id DESC
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_deferred_lookup(self, lookup_id: int) -> dict[str, Any]:
        backup = self.backup("deferred_lookup_resolve")
        with self.connect(rows=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE english_deferred_lookups
                SET resolved=1,resolved_at=CURRENT_TIMESTAMP
                WHERE id=? AND resolved=0
                """,
                (int(lookup_id),),
            )
            if connection.total_changes != 1:
                connection.rollback()
                raise ValueError("稍后查记录不存在或已经处理。")
            self._integrity(connection)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM english_deferred_lookups WHERE id=?", (int(lookup_id),)
            ).fetchone()
        result = dict(row)
        result.update({"backup_path": str(backup or ""), "readback_verified": True})
        return result

    def save_usage(
        self,
        text: str,
        *,
        usage_kind: str = "sentence",
        context: str = "",
        material_id: int | None = None,
        page_number: int | None = None,
        anchor_y: float = 0.0,
        user_note: str = "",
        agent_analysis: str = "",
        writing_technique: str = "",
    ) -> dict[str, Any]:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            raise ValueError("句子或 Usage 文本不能为空。")
        backup = self.backup("usage_save")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_usage_items(
                    usage_kind,text,context,material_id,page_number,anchor_y,
                    user_note,agent_analysis,writing_technique
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (usage_kind, value, context, material_id, page_number, max(0.0, float(anchor_y)), user_note, agent_analysis, writing_technique),
            )
            item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._integrity(connection)
            connection.commit()
            row = connection.execute("SELECT * FROM english_usage_items WHERE id=?", (item_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def usage_items(self, keyword: str = "") -> list[dict[str, Any]]:
        where = ""
        args: list[Any] = []
        if keyword.strip():
            where = "WHERE u.text LIKE ? OR u.context LIKE ? OR u.user_note LIKE ? OR u.agent_analysis LIKE ?"
            args = [f"%{keyword.strip()}%"] * 4
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT u.*,m.title AS material_title,m.reading_path
                FROM english_usage_items u
                LEFT JOIN english_materials m ON m.id=u.material_id
                {where}
                ORDER BY u.created_at DESC,u.id DESC
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_grammar_encounter(
        self,
        material_id: int,
        selected_sentence: str,
        *,
        analysis: str = "",
        context: str = "",
        page_number: int = 1,
        anchor_y: float = 0.0,
        concept_id: int | None = None,
    ) -> dict[str, Any]:
        sentence = re.sub(r"\s+", " ", selected_sentence).strip()
        if not sentence:
            raise ValueError("句型 encounter 必须包含英文句子。")
        backup = self.backup("grammar_encounter")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_grammar_encounters(
                    concept_id,material_id,selected_sentence,context,page_number,anchor_y,analysis
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (concept_id, int(material_id), sentence, context, max(1, int(page_number)), max(0.0, float(anchor_y)), analysis),
            )
            item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.commit()
            row = connection.execute("SELECT * FROM english_grammar_encounters WHERE id=?", (item_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def grammar_encounters(self, keyword: str = "") -> list[dict[str, Any]]:
        where = ""
        args: list[Any] = []
        if keyword.strip():
            where = "WHERE g.selected_sentence LIKE ? OR g.context LIKE ? OR g.analysis LIKE ?"
            args = [f"%{keyword.strip()}%"] * 3
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT g.*,m.title AS material_title,m.reading_path,
                       c.canonical_name AS concept_name
                FROM english_grammar_encounters g
                JOIN english_materials m ON m.id=g.material_id
                LEFT JOIN english_grammar_concepts c ON c.id=g.concept_id
                {where}
                ORDER BY g.created_at DESC,g.id DESC
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_exercise_attempt(
        self,
        exercise_id: int,
        *,
        selected_answer: str,
        correct_answer: str,
        mistake_reason: str = "",
        misconception_category: str = "",
    ) -> dict[str, Any]:
        is_correct = int(selected_answer.strip() == correct_answer.strip())
        backup = self.backup("exercise_attempt")
        with self.connect(rows=True) as connection:
            exercise = connection.execute(
                "SELECT * FROM english_grammar_exercises WHERE id=?", (int(exercise_id),)
            ).fetchone()
            if exercise is None:
                raise ValueError("文法练习不存在。")
            connection.execute(
                """
                INSERT INTO english_exercise_attempts(
                    exercise_id,selected_answer,correct_answer,is_correct,mistake_reason,resolved
                ) VALUES(?,?,?,?,?,?)
                """,
                (int(exercise_id), selected_answer, correct_answer, is_correct, mistake_reason, is_correct),
            )
            attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            if not is_correct:
                connection.execute(
                    """
                    INSERT INTO english_misconceptions(concept_id,exercise_id,category,description)
                    VALUES(?,?,?,?)
                    """,
                    (exercise["grammar_concept_id"], int(exercise_id), misconception_category or "unclassified", mistake_reason),
                )
            connection.commit()
            row = connection.execute("SELECT * FROM english_exercise_attempts WHERE id=?", (attempt_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def record_grammar_exercise_attempt(
        self,
        material_id: int,
        question_reference: str,
        *,
        selected_answer: str,
        correct_answer: str,
        chapter_id: int | None = None,
        source_page: int | None = None,
        explanation_reference: str = "",
        mistake_reason: str = "",
        misconception_category: str = "",
    ) -> dict[str, Any]:
        reference = str(question_reference or "").strip()
        if not reference:
            raise ValueError("练习题号不能为空。")
        with self.connect(rows=True) as connection:
            material = connection.execute(
                "SELECT role FROM english_materials WHERE id=?", (int(material_id),)
            ).fetchone()
            if material is None:
                raise ValueError("英语材料不存在。")
            if str(material["role"]) != "grammar_exercises":
                raise ValueError("练习记录只能关联文法解题材料。")
            connection.execute(
                """
                INSERT INTO english_grammar_exercises(
                    material_id,chapter_id,question_reference,source_page,explanation_reference
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(material_id,question_reference) DO UPDATE SET
                    chapter_id=COALESCE(excluded.chapter_id,english_grammar_exercises.chapter_id),
                    source_page=COALESCE(excluded.source_page,english_grammar_exercises.source_page),
                    explanation_reference=CASE WHEN excluded.explanation_reference<>''
                        THEN excluded.explanation_reference ELSE english_grammar_exercises.explanation_reference END
                """,
                (int(material_id), chapter_id, reference, source_page, str(explanation_reference or "").strip()),
            )
            row = connection.execute(
                "SELECT id FROM english_grammar_exercises WHERE material_id=? AND question_reference=?",
                (int(material_id), reference),
            ).fetchone()
            connection.commit()
        assert row is not None
        result = self.save_exercise_attempt(
            int(row["id"]),
            selected_answer=selected_answer,
            correct_answer=correct_answer,
            mistake_reason=mistake_reason,
            misconception_category=misconception_category,
        )
        result["exercise_id"] = int(row["id"])
        return result

    def record_reading_training_attempt(
        self,
        material_id: int,
        passage_reference: str,
        *,
        duration_seconds: int | None = None,
        correct_count: int | None = None,
        question_count: int | None = None,
        question_type: str = "",
        error_reason: str = "",
        note: str = "",
        chapter_id: int | None = None,
    ) -> dict[str, Any]:
        reference = str(passage_reference or "").strip()
        if not reference:
            raise ValueError("阅读篇章编号不能为空。")
        if duration_seconds is not None and int(duration_seconds) < 0:
            raise ValueError("阅读用时不能为负数。")
        if question_count is not None and int(question_count) < 0:
            raise ValueError("题目数不能为负数。")
        if correct_count is not None and int(correct_count) < 0:
            raise ValueError("正确数不能为负数。")
        if correct_count is not None and question_count is not None and int(correct_count) > int(question_count):
            raise ValueError("正确数不能超过题目数。")
        backup = self.backup("reading_training_attempt")
        with self.connect(rows=True) as connection:
            material = connection.execute(
                "SELECT role FROM english_materials WHERE id=?", (int(material_id),)
            ).fetchone()
            if material is None or str(material["role"]) != "reading_training":
                raise ValueError("阅读训练记录只能关联阅读训练材料。")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO english_reading_training_attempts(
                    material_id,chapter_id,passage_reference,question_type,duration_seconds,
                    correct_count,question_count,error_reason,note
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(material_id), chapter_id, reference, str(question_type or "").strip(),
                    duration_seconds, correct_count, question_count,
                    str(error_reason or "").strip(), str(note or "").strip(),
                ),
            )
            attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._integrity(connection)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM english_reading_training_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        result = dict(row)
        result.update({"backup_path": str(backup or ""), "readback_verified": True})
        return result

    def save_morpheme(
        self,
        canonical_form: str,
        morpheme_type: str,
        *,
        semantic_idea: str = "",
        variants: str = "",
        source_kind: str = "user",
        material_id: int | None = None,
        source_page: int | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        if morpheme_type not in {"prefix", "root", "suffix"}:
            raise ValueError("构词成分类型必须是 prefix、root 或 suffix。")
        form = canonical_form.strip()
        if not form:
            raise ValueError("构词成分不能为空。")
        backup = self.backup("morpheme_save")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_morphemes(
                    canonical_form,morpheme_type,variants,semantic_idea,material_id,
                    source_page,source_kind,notes
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical_form,morpheme_type,material_id,source_page)
                DO UPDATE SET variants=excluded.variants,semantic_idea=excluded.semantic_idea,
                    source_kind=excluded.source_kind,notes=excluded.notes,updated_at=CURRENT_TIMESTAMP
                """,
                (form, morpheme_type, variants, semantic_idea, material_id, source_page, source_kind, notes),
            )
            row = connection.execute(
                """
                SELECT * FROM english_morphemes WHERE canonical_form=? AND morpheme_type=?
                    AND material_id IS ? AND source_page IS ?
                """,
                (form, morpheme_type, material_id, source_page),
            ).fetchone()
            connection.commit()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def create_writing_practice(
        self,
        title: str,
        *,
        prompt: str = "",
        original_draft: str = "",
        material_id: int | None = None,
        chapter_id: int | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("写作练习标题不能为空。")
        backup = self.backup("writing_practice")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_writing_practices(title,prompt,material_id,chapter_id)
                VALUES(?,?,?,?)
                """,
                (title.strip(), prompt, material_id, chapter_id),
            )
            practice_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            if original_draft.strip():
                connection.execute(
                    """
                    INSERT INTO english_writing_revisions(
                        practice_id,revision_number,revision_kind,content
                    ) VALUES(?,1,'original',?)
                    """,
                    (practice_id, original_draft),
                )
            connection.commit()
            row = connection.execute("SELECT * FROM english_writing_practices WHERE id=?", (practice_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def add_writing_revision(
        self,
        practice_id: int,
        content: str,
        *,
        revision_kind: str = "user",
        diagnostic_feedback: str = "",
    ) -> dict[str, Any]:
        if not content.strip():
            raise ValueError("写作版本内容不能为空。")
        backup = self.backup("writing_revision")
        with self.connect(rows=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM english_writing_practices WHERE id=?", (int(practice_id),)
            ).fetchone()
            if exists is None:
                raise ValueError("写作练习不存在。")
            number = int(connection.execute(
                "SELECT COALESCE(MAX(revision_number),0)+1 FROM english_writing_revisions WHERE practice_id=?",
                (int(practice_id),),
            ).fetchone()[0])
            connection.execute(
                """
                INSERT INTO english_writing_revisions(
                    practice_id,revision_number,revision_kind,content,diagnostic_feedback
                ) VALUES(?,?,?,?,?)
                """,
                (int(practice_id), number, revision_kind, content, diagnostic_feedback),
            )
            revision_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                "UPDATE english_writing_practices SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(practice_id),),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM english_writing_revisions WHERE id=?", (revision_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def writing_practices(self) -> list[dict[str, Any]]:
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                """
                SELECT p.*,COUNT(r.id) AS revision_count
                FROM english_writing_practices p
                LEFT JOIN english_writing_revisions r ON r.practice_id=p.id
                GROUP BY p.id ORDER BY p.updated_at DESC,p.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def writing_revisions(self, practice_id: int) -> list[dict[str, Any]]:
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM english_writing_revisions
                WHERE practice_id=? ORDER BY revision_number,id
                """,
                (int(practice_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_audio_resource(
        self,
        title: str,
        path_or_url: str,
        *,
        material_id: int | None = None,
        resource_kind: str = "local_file",
        track_number: int | None = None,
        chapter_id: int | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        value = path_or_url.strip()
        if not title.strip() or not value:
            raise ValueError("音频资源标题和路径/URL不能为空。")
        if resource_kind == "local_file" and not Path(value).expanduser().is_file():
            raise FileNotFoundError(value)
        backup = self.backup("audio_resource")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_audio_resources(
                    material_id,title,resource_kind,path_or_url,track_number,chapter_id,notes
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (material_id, title.strip(), resource_kind, value, track_number, chapter_id, notes),
            )
            item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.commit()
            row = connection.execute("SELECT * FROM english_audio_resources WHERE id=?", (item_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def audio_resources(self, *, material_id: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE a.material_id=?" if material_id is not None else ""
        args = [int(material_id)] if material_id is not None else []
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT a.*,m.title AS material_title
                FROM english_audio_resources a
                LEFT JOIN english_materials m ON m.id=a.material_id
                {where}
                ORDER BY COALESCE(a.track_number,2147483647),a.created_at DESC,a.id DESC
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def tts_status() -> dict[str, Any]:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        return {
            "available": bool(os.name == "nt" and powershell),
            "backend": "Windows System.Speech" if os.name == "nt" and powershell else "none",
            "command": powershell or "",
            "network_required": False,
        }

    @staticmethod
    def speak_text(text: str) -> subprocess.Popen[str]:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            raise ValueError("没有可朗读的英文文本。")
        status = EnglishLearningService.tts_status()
        if not status["available"]:
            raise RuntimeError("当前系统没有可用的本地 Windows TTS。")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new(); "
            "$speaker.Speak([Console]::In.ReadToEnd()); $speaker.Dispose()"
        )
        process = subprocess.Popen(
            [str(status["command"]), "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdin is not None
        process.stdin.write(value)
        process.stdin.close()
        return process

    def record_shadowing_attempt(
        self,
        source_text: str,
        *,
        material_id: int | None = None,
        usage_item_id: int | None = None,
        source_audio_id: int | None = None,
        user_recording_path: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        if not source_text.strip():
            raise ValueError("跟读文本不能为空。")
        if user_recording_path and not Path(user_recording_path).expanduser().is_file():
            raise FileNotFoundError(user_recording_path)
        backup = self.backup("shadowing_attempt")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_shadowing_attempts(
                    material_id,usage_item_id,source_text,source_audio_id,user_recording_path,note
                ) VALUES(?,?,?,?,?,?)
                """,
                (material_id, usage_item_id, source_text, source_audio_id, user_recording_path, note),
            )
            item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.commit()
            row = connection.execute("SELECT * FROM english_shadowing_attempts WHERE id=?", (item_id,)).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    def shadowing_attempts(self, *, material_id: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE s.material_id=?" if material_id is not None else ""
        args = [int(material_id)] if material_id is not None else []
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT s.*,m.title AS material_title,a.title AS source_audio_title
                FROM english_shadowing_attempts s
                LEFT JOIN english_materials m ON m.id=s.material_id
                LEFT JOIN english_audio_resources a ON a.id=s.source_audio_id
                {where}
                ORDER BY s.attempted_at DESC,s.id DESC
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def unified_search(self, query: str, *, limit: int = 100) -> list[dict[str, Any]]:
        value = query.strip()
        if not value:
            return []
        pattern = f"%{value}%"
        results: list[dict[str, Any]] = []
        with self.connect(rows=True) as connection:
            for row in connection.execute(
                """
                SELECT id,title,role,reading_path,last_page FROM english_materials
                WHERE title LIKE ? OR english_title LIKE ? OR notes LIKE ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (pattern, pattern, pattern, int(limit)),
            ):
                results.append({"kind": "material", "id": row["id"], "title": row["title"], "snippet": row["role"], "material_id": row["id"], "page_number": row["last_page"], "reading_path": row["reading_path"]})
            for row in connection.execute(
                """
                SELECT u.id,u.text,u.context,u.material_id,u.page_number,m.reading_path
                FROM english_usage_items u LEFT JOIN english_materials m ON m.id=u.material_id
                WHERE u.text LIKE ? OR u.context LIKE ? OR u.agent_analysis LIKE ?
                ORDER BY u.created_at DESC LIMIT ?
                """,
                (pattern, pattern, pattern, int(limit)),
            ):
                results.append({"kind": "usage", "id": row["id"], "title": row["text"], "snippet": row["context"], "material_id": row["material_id"], "page_number": row["page_number"], "reading_path": row["reading_path"]})
            for row in connection.execute(
                """
                SELECT g.id,g.selected_sentence,g.analysis,g.material_id,g.page_number,m.reading_path
                FROM english_grammar_encounters g JOIN english_materials m ON m.id=g.material_id
                WHERE g.selected_sentence LIKE ? OR g.analysis LIKE ?
                ORDER BY g.created_at DESC LIMIT ?
                """,
                (pattern, pattern, int(limit)),
            ):
                results.append({"kind": "grammar_encounter", "id": row["id"], "title": row["selected_sentence"], "snippet": row["analysis"], "material_id": row["material_id"], "page_number": row["page_number"], "reading_path": row["reading_path"]})
            for row in connection.execute(
                """
                SELECT d.id,d.selected_text,d.context,d.material_id,d.page_number,m.reading_path
                FROM english_deferred_lookups d JOIN english_materials m ON m.id=d.material_id
                WHERE d.selected_text LIKE ? OR d.context LIKE ?
                ORDER BY d.created_at DESC LIMIT ?
                """,
                (pattern, pattern, int(limit)),
            ):
                results.append({"kind": "deferred", "id": row["id"], "title": row["selected_text"], "snippet": row["context"], "material_id": row["material_id"], "page_number": row["page_number"], "reading_path": row["reading_path"]})
        return results[: max(1, int(limit))]


__all__ = [
    "EnglishLearningService",
    "XUAN_BOOKS",
    "GRAMMAR_CHAPTERS",
    "ENGLISH_SCHEMA",
]
