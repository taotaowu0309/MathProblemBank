from pathlib import Path
import sqlite3


# 当前文件位于：
# MathProblemBank/shared/scripts/init_databases.py
# parents[2] 因而是 MathProblemBank 根目录。
ROOT = Path(__file__).resolve().parents[2]


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    edition TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    publication_year INTEGER,
    pdf_path TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS canonical_problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    problem_code TEXT NOT NULL UNIQUE,

    chapter_code TEXT NOT NULL,
    chapter_name TEXT NOT NULL,

    section_code TEXT NOT NULL DEFAULT '',
    section_name TEXT NOT NULL DEFAULT '',

    problem_order INTEGER,
    title TEXT NOT NULL DEFAULT '',
    summary_tex TEXT NOT NULL DEFAULT '',

    statement_tex TEXT NOT NULL,
    normalized_text TEXT NOT NULL DEFAULT '',
    structure_signature TEXT NOT NULL DEFAULT '',

    mastery_status TEXT NOT NULL DEFAULT 'unrated'
        CHECK (
            mastery_status IN (
                'unrated',
                'mastered',
                'familiar',
                'unfamiliar',
                'unknown'
            )
        ),

    solution_status TEXT
        CHECK (
            solution_status IS NULL
            OR solution_status IN (
                'Answered',
                'Deferred',
                'Open'
            )
        ),

    difficulty INTEGER
        CHECK (
            difficulty IS NULL
            OR difficulty BETWEEN 1 AND 5
        ),

    main_method TEXT NOT NULL DEFAULT '',
    solution_tex TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_canonical_chapter
ON canonical_problems(
    chapter_code,
    section_code,
    problem_order
);

CREATE INDEX IF NOT EXISTS idx_canonical_mastery
ON canonical_problems(mastery_status);

CREATE INDEX IF NOT EXISTS idx_canonical_normalized
ON canonical_problems(normalized_text);

CREATE INDEX IF NOT EXISTS idx_canonical_signature
ON canonical_problems(structure_signature);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS problem_tags (
    canonical_problem_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,

    PRIMARY KEY(canonical_problem_id, tag_id),

    FOREIGN KEY(canonical_problem_id)
        REFERENCES canonical_problems(id)
        ON DELETE CASCADE,

    FOREIGN KEY(tag_id)
        REFERENCES tags(id)
        ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS update_canonical_problem_time
AFTER UPDATE ON canonical_problems
FOR EACH ROW
BEGIN
    UPDATE canonical_problems
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = OLD.id;
END;
"""


def initialize_database(
    database_path: Path,
    subject: str,
    prefix: str,
) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA)

        connection.execute(
            """
            INSERT INTO metadata(key, value)
            VALUES('subject', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (subject,),
        )

        connection.execute(
            """
            INSERT INTO metadata(key, value)
            VALUES('problem_prefix', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (prefix,),
        )

        connection.execute(
            """
            INSERT INTO metadata(key, value)
            VALUES('schema_version', '1')
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """
        )

        connection.commit()

    print(f"已创建或检查数据库：{database_path}")


def main() -> None:
    from shared.scripts.study_project_service import ensure_subject_registry

    subjects = ensure_subject_registry()
    for subject_name, cfg in subjects.items():
        if not cfg.get("enabled", True):
            continue
        initialize_database(
            database_path=ROOT / cfg["db"],
            subject=subject_name,
            prefix=str(cfg.get("prefix") or ""),
        )

    print("题库数据库初始化完成。")


if __name__ == "__main__":
    main()
