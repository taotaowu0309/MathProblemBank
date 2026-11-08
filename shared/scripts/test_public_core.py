from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shared.scripts.ai_agent_repository import GlobalProblemRepository
from shared.scripts.application_paths import APP_PATHS
from shared.scripts.study_project_service import (
    _declared_schema_objects,
    ensure_learning_schema,
    load_subjects,
    prepare_subject_database,
)
from shared.scripts.vocabulary_manager import workspace_vocabulary_paths


class PublicCoreTests(unittest.TestCase):
    def test_public_profile_supports_math_and_english_with_external_data(self) -> None:
        self.assertTrue(APP_PATHS.public_release)
        self.assertEqual(APP_PATHS.author_name, "MathProblemBank User")
        subjects = load_subjects("math")
        self.assertEqual(set(subjects), {"数学分析", "高等代数"})
        self.assertEqual(set(load_subjects("english")), {"英语学习"})
        self.assertTrue(all(cfg["db"].is_relative_to(APP_PATHS.user_data_root) for cfg in subjects.values()))

    def test_english_notes_reading_and_executor_share_external_data(self) -> None:
        from shared.scripts.ai_agent_repository import AI_OPERATION_REGISTRY, AiAgentToolExecutor
        from shared.scripts.english_learning_service import EnglishLearningService
        from shared.scripts.reading_help_service import ReadingHelpService
        from shared.scripts.textbook_notes_service import TextbookNotesService
        self.assertEqual(AI_OPERATION_REGISTRY.validate(AiAgentToolExecutor), [])
        english = EnglishLearningService()
        notes = TextbookNotesService()
        reading = ReadingHelpService()
        for service in (english, notes, reading):
            self.assertTrue(service.root.is_relative_to(APP_PATHS.user_data_root), service.root)
        with english.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        executor = AiAgentToolExecutor(GlobalProblemRepository(), discipline="english")
        executor.begin_turn("读取公开空环境", {}, write_authorized=False)
        result = executor.execute("list_subjects", {})
        self.assertTrue(result["ok"])
        self.assertNotIn("量子场论", str(result))
        self.assertEqual(executor._english_learning().root, english.root)
        self.assertEqual(executor._textbook_notes().root, notes.root)

    def test_first_subject_database_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update(
                {
                    "MATH_PROBLEM_BANK_DATA_ROOT": temporary,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(APP_PATHS.application_root),
                }
            )
            writer_script = '''
import sqlite3

from shared.scripts.study_project_service import ensure_subject_storage, load_subjects

name, config = next(iter(load_subjects("math").items()))
ensure_subject_storage(name)
with sqlite3.connect(config["db"]) as connection:
    connection.execute(
        """
        INSERT INTO canonical_problems(
            problem_code, chapter_code, chapter_name, title, statement_tex
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("SYN-0001", "1", "Synthetic", "Synthetic problem edited", "Prove that $1=1$."),
    )
    connection.execute(
        "UPDATE canonical_problems SET solution_tex=?, notes=? WHERE problem_code=?",
        ("The identity is immediate.", "persisted after restart", "SYN-0001"),
    )
    connection.commit()
print("PUBLIC_DATABASE_WRITE_OK")
'''
            writer = subprocess.run(
                [sys.executable, "-c", writer_script],
                cwd=APP_PATHS.application_root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertIn("PUBLIC_DATABASE_WRITE_OK", writer.stdout)

            reader_script = '''
import sqlite3

from shared.scripts.study_project_service import load_subjects

name, config = next(iter(load_subjects("math").items()))
with sqlite3.connect(config["db"]) as connection:
    row = connection.execute(
        "SELECT problem_code, title, solution_tex, notes FROM canonical_problems WHERE title=?",
        ("Synthetic problem edited",),
    ).fetchone()
assert row is not None, name
assert tuple(row) == (
    "SYN-0001",
    "Synthetic problem edited",
    "The identity is immediate.",
    "persisted after restart",
), row
print("PUBLIC_DATABASE_RESTART_READ_OK")
'''
            reader = subprocess.run(
                [sys.executable, "-c", reader_script],
                cwd=APP_PATHS.application_root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertIn("PUBLIC_DATABASE_RESTART_READ_OK", reader.stdout)

    def test_repository_dependency_injection_keeps_vocabulary_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected, _backups, _exports = workspace_vocabulary_paths("math", root_dir=root)
            repository = GlobalProblemRepository(root)
            self.assertEqual(repository.vocabulary_database, expected)

    def test_lecture_quality_fixtures_are_synthetic(self) -> None:
        directory = (
            APP_PATHS.application_root
            / "shared"
            / "templates"
            / "ai_agent_training"
            / "lecture_quality"
        )
        corpus = json.loads((directory / "corpus.json").read_text(encoding="utf-8"))
        self.assertEqual(corpus["dataset"], "synthetic-algebra-demo")
        self.assertFalse((directory / "historical_cases.json").exists())
        self.assertFalse((directory / "full_eval_report.json").exists())

class SubjectSchemaStartupTests(unittest.TestCase):
    def _settled_database(self, root: Path) -> tuple[Path, Path]:
        database = root / "subject.db"
        backups = root / "backups"
        self.assertTrue(prepare_subject_database(database, "测试学科", "TS", "math"))
        self.assertIsNotNone(ensure_learning_schema(database, backups))
        return database, backups

    def _table_names(self, database: Path) -> set[str]:
        connection = sqlite3.connect(database)
        try:
            return {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()

    def test_second_startup_does_not_touch_settled_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, backups = self._settled_database(Path(temporary))
            modified = database.stat().st_mtime_ns
            self.assertFalse(prepare_subject_database(database, "测试学科", "TS", "math"))
            self.assertIsNone(ensure_learning_schema(database, backups))
            self.assertEqual(database.stat().st_mtime_ns, modified)

    def test_missing_tag_tables_are_recreated_from_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, _backups = self._settled_database(Path(temporary))
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("DROP TABLE problem_tags")
                connection.execute("DROP TABLE tags")
                connection.commit()
            finally:
                connection.close()
            self.assertTrue(prepare_subject_database(database, "测试学科", "TS", "math"))
            self.assertTrue({"tags", "problem_tags"} <= self._table_names(database))

    def test_missing_collection_index_is_recreated_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, backups = self._settled_database(Path(temporary))
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP INDEX idx_collection_books_book")
                connection.commit()
            finally:
                connection.close()
            self.assertIsNone(ensure_learning_schema(database, backups))
            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_collection_books_book'"
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(row)

    def test_missing_canonical_extra_column_still_backs_up_and_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, backups = self._settled_database(Path(temporary))
            connection = sqlite3.connect(database)
            try:
                connection.execute("ALTER TABLE canonical_problems DROP COLUMN summary_tex")
                connection.commit()
            finally:
                connection.close()
            backup = ensure_learning_schema(database, backups)
            self.assertIsNotNone(backup)
            self.assertTrue(Path(backup).is_file())
            connection = sqlite3.connect(database)
            try:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(canonical_problems)")]
            finally:
                connection.close()
            self.assertIn("summary_tex", columns)

    def test_create_without_if_not_exists_is_not_treated_as_settled(self) -> None:
        self.assertIsNone(_declared_schema_objects("CREATE TABLE tags (id INTEGER PRIMARY KEY);"))
        with tempfile.TemporaryDirectory() as temporary:
            database, _backups = self._settled_database(Path(temporary))
            import shared.scripts.init_databases as init_databases

            original = init_databases.SCHEMA
            init_databases.SCHEMA = original + "\nCREATE TABLE extra_needs_work(id INTEGER PRIMARY KEY);\n"
            try:
                self.assertTrue(prepare_subject_database(database, "测试学科", "TS", "math"))
            finally:
                init_databases.SCHEMA = original
            self.assertIn("extra_needs_work", self._table_names(database))


if __name__ == "__main__":
    unittest.main()
