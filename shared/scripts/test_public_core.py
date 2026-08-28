from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shared.scripts.ai_agent_acceptance import (
    load_acceptance_suite,
    load_math_capability_suite,
)
from shared.scripts.ai_agent_quality_dataset import MathQualityDataset
from shared.scripts.ai_agent_repository import GlobalProblemRepository
from shared.scripts.ai_agent_service import AiAgentService, LEARNER_PROFILE_PATH
from shared.scripts.ai_agent_learner_profile import learner_profile_path, load_learner_profile
from shared.scripts.application_paths import APP_PATHS
from shared.scripts.study_project_service import load_subjects
from shared.scripts.vocabulary_manager import workspace_vocabulary_paths


class PublicCoreTests(unittest.TestCase):
    def test_public_profile_is_math_only_and_uses_external_data(self) -> None:
        self.assertTrue(APP_PATHS.public_release)
        self.assertEqual(APP_PATHS.author_name, "MathProblemBank User")
        subjects = load_subjects("math")
        self.assertEqual(set(subjects), {"数学分析", "高等代数"})
        self.assertTrue(all(cfg["db"].is_relative_to(APP_PATHS.user_data_root) for cfg in subjects.values()))

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

    def test_public_ai_profile_and_training_are_synthetic(self) -> None:
        self.assertFalse(LEARNER_PROFILE_PATH.exists())
        user_profile = learner_profile_path()
        self.assertEqual(load_learner_profile(), "")
        self.assertFalse(user_profile.exists())
        self.assertNotIn(
            "<learner_profile>",
            AiAgentService()._system_prompt({}, user_text="解释有限维线性映射的核与像。"),
        )
        pairs = MathQualityDataset().all()
        self.assertTrue(pairs)
        self.assertTrue(all(pair.source == "synthetic_public_fixture" for pair in pairs))
        suites = [load_math_capability_suite(), load_acceptance_suite()]
        serialized = json.dumps(suites, ensure_ascii=False)
        self.assertNotIn("DM-" + "P", serialized)
        self.assertNotIn("DM-" + "C", serialized)
        self.assertNotIn("ChatGPT " + "网页版历史", serialized)
        self.assertTrue(
            all(
                str(item.get("id") or "").startswith("synthetic_")
                for suite in suites
                for item in suite
            )
        )

    def test_public_learner_profile_import_clear_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "user-data"
            fixture_root = root / "fixtures"
            environment = os.environ.copy()
            environment.update(
                {
                    "MATH_PROBLEM_BANK_PUBLIC_RELEASE": "1",
                    "MATH_PROBLEM_BANK_DATA_ROOT": str(data_root),
                    "PROFILE_FIXTURE_ROOT": str(fixture_root),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(APP_PATHS.application_root),
                }
            )
            script = r'''
import os
from pathlib import Path

from shared.scripts.ai_agent_learner_profile import (
    MAX_LEARNER_PROFILE_BYTES,
    clear_learner_profile,
    import_learner_profile,
    learner_profile_path,
    load_learner_profile,
)
from shared.scripts.ai_agent_service import AiAgentService
from shared.scripts.application_paths import APP_PATHS

fixtures = Path(os.environ["PROFILE_FIXTURE_ROOT"])
fixtures.mkdir(parents=True)
destination = learner_profile_path()
assert load_learner_profile() == "", repr(load_learner_profile())
assert not destination.exists(), str(destination)

source = fixtures / "synthetic-profile.md"
content = "对证明先声明假设，再逐步核对非平凡推论。"
source.write_text(content + "\n", encoding="utf-8")
status = import_learner_profile(source)
assert status["source"] == "user", status
assert destination.is_relative_to(APP_PATHS.settings_dir), (destination, APP_PATHS.settings_dir)
assert destination.is_relative_to(APP_PATHS.user_data_root), (destination, APP_PATHS.user_data_root)
assert not destination.is_relative_to(APP_PATHS.application_root), (destination, APP_PATHS.application_root)
assert load_learner_profile() == content, repr(load_learner_profile())
prompt = AiAgentService()._system_prompt({}, user_text="证明有限维线性映射的秩零化度定理。")
assert "<learner_profile>" in prompt and content in prompt, prompt[-500:]

invalid_utf8 = fixtures / "invalid-utf8.txt"
invalid_utf8.write_bytes(b"\xff\xfe")
wrong_suffix = fixtures / "profile.exe"
wrong_suffix.write_text(content, encoding="utf-8")
oversized = fixtures / "oversized.txt"
oversized.write_bytes(b"x" * (MAX_LEARNER_PROFILE_BYTES + 1))
for invalid in (invalid_utf8, wrong_suffix, oversized):
    try:
        import_learner_profile(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError(f"invalid learner profile was accepted: {invalid.name}")
assert load_learner_profile() == content, repr(load_learner_profile())

cleared = clear_learner_profile()
assert cleared["source"] == "empty", cleared
assert destination.read_bytes() == b"", destination.read_bytes()
assert load_learner_profile() == "", repr(load_learner_profile())
prompt = AiAgentService()._system_prompt({}, user_text="解释一个线性代数定义。")
assert "<learner_profile>" not in prompt, prompt[-500:]
assert APP_PATHS.public_release
print("PUBLIC_LEARNER_PROFILE_ROUND_TRIP_OK")
'''
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=APP_PATHS.application_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                raise AssertionError(
                    "public learner profile subprocess failed:\n"
                    + completed.stdout
                    + completed.stderr
                )
            self.assertIn("PUBLIC_LEARNER_PROFILE_ROUND_TRIP_OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
