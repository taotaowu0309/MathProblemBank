from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from shared.scripts.application_paths import DATA_ROOT_ENV, resolve_application_paths
from tools.build_public_release import (
    _load_private_release_policy,
    _private_training_marker,
    _sensitive_marker,
    build_public_release,
    build_public_release_archive,
)


class ApplicationPathTests(unittest.TestCase):
    def test_public_release_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            application_root = base / "application"
            application_root.mkdir()
            (application_root / ".mathproblem-public-release.json").write_text("{}\n", encoding="utf-8")
            local_app_data = base / "local-app-data"
            paths = resolve_application_paths(
                application_root,
                {"LOCALAPPDATA": str(local_app_data)},
            )
            self.assertTrue(paths.public_release)
            self.assertEqual(paths.user_data_root, (local_app_data / "MathProblemBank").resolve())
            self.assertEqual(paths.workspace_root, (local_app_data / "MathProblemBank" / "workspaces").resolve())
            self.assertEqual(paths.settings_dir, (local_app_data / "MathProblemBank" / "config").resolve())
            self.assertNotEqual(paths.subjects_registry_path.parent, application_root / "shared")

    def test_explicit_data_root_overrides_release_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            application_root = base / "application"
            application_root.mkdir()
            data_root = base / "chosen-data"
            paths = resolve_application_paths(
                application_root,
                {
                    "MATH_PROBLEM_BANK_PUBLIC_RELEASE": "1",
                    DATA_ROOT_ENV: str(data_root),
                },
            )
            self.assertEqual(paths.user_data_root, data_root.resolve())
            self.assertEqual(paths.subjects_registry_path, (data_root / "config" / "subjects.json").resolve())


class PublicReleaseBuilderTests(unittest.TestCase):
    def test_sensitive_scanner_catches_raw_escaped_and_credential_paths(self) -> None:
        slash = chr(92)
        raw_path = "C:" + slash + "Users" + slash + "ExampleUser" + slash + "AppData" + slash + "Local" + slash + "file.json"
        escaped_path = raw_path.replace(slash, slash + slash)
        self.assertIsNotNone(_sensitive_marker(raw_path))
        self.assertIsNotNone(_sensitive_marker(escaped_path))
        user = "user"
        password = "secret"
        host = "example.invalid"
        credential_url = "https://" + user + ":" + password + "@" + host + "/v1"
        self.assertIsNotNone(_sensitive_marker(credential_url))
        self.assertIsNone(_sensitive_marker("https://api.openai.com/v1"))

        marker = "private-release-fixture.example.invalid"
        self.assertEqual(_sensitive_marker(marker, (marker,)), marker)

    def test_optional_private_release_policy_is_data_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "shared" / "private_release_policy.local.py"
            policy.parent.mkdir(parents=True)
            policy.write_text(
                "PRIVATE_TEXT_MARKERS = ('private.example.invalid',)\n"
                "PRIVATE_NAME_FRAGMENTS = ('private-export',)\n",
                encoding="utf-8",
            )
            loaded = _load_private_release_policy(root)
            self.assertEqual(loaded["PRIVATE_TEXT_MARKERS"], ("private.example.invalid",))
            self.assertEqual(loaded["PRIVATE_NAME_FRAGMENTS"], ("private-export",))

            public_root = root / "public"
            public_root.mkdir()
            self.assertEqual(
                _load_private_release_policy(public_root),
                {"PRIVATE_TEXT_MARKERS": (), "PRIVATE_NAME_FRAGMENTS": ()},
            )

    def test_training_provenance_gate_rejects_private_history_and_fixture_ids(self) -> None:
        training_path = Path("shared/templates/ai_agent_training/example.json")
        private_markers = (
            "DM-" + "P000005",
            "ChatGPT " + "网页版历史",
            "题库助手" + "历史",
            "paired_" + "user_feedback",
            "近期数学" + "学习对话",
        )
        for marker in private_markers:
            with self.subTest(marker=marker):
                self.assertIsNotNone(_private_training_marker(training_path, marker))
        self.assertIsNone(
            _private_training_marker(
                training_path,
                '{"source":"synthetic_public_fixture","problem_ref":"SYN-MA-P000001"}',
            )
        )

    def test_release_contains_only_allowlisted_program_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "public-release"
            manifest = build_public_release(output)
            self.assertGreater(int(manifest["file_count"]), 0)
            self.assertTrue((output / ".mathproblem-public-release.json").is_file())
            self.assertTrue((output / "PUBLIC_RELEASE_MANIFEST.json").is_file())
            self.assertTrue((output / "shared" / "scripts" / "problem_bank_center_qt.py").is_file())
            self.assertTrue((output / "tools" / "build_public_release.py").is_file())
            self.assertFalse((output / "shared" / "private_release_policy.local.py").exists())
            self.assertFalse((output / "shared" / "templates" / "public_release").exists())
            self.assertTrue((output / "shared" / "scripts" / "public_regression_core.py").is_file())
            self.assertFalse((output / "shared" / "scripts" / "regression_core.py").exists())
            self.assertFalse((output / "shared" / "scripts" / "diagram_backends").exists())
            self.assertFalse((output / "shared" / "scripts" / "ai_agent_account_usage.py").exists())
            self.assertFalse((output / "shared" / "ui" / "config" / "ui_state.json").exists())
            self.assertFalse((output / "shared" / "templates" / "ai_math_learner_profile.txt").exists())
            public_readme = (output / "README.md").read_text(encoding="utf-8")
            self.assertIn("sanitized public view", public_readme)
            self.assertNotIn("本仓库仍是私人开发工作区", public_readme)
            for relative in (
                "README.md",
                "README.zh-CN.md",
                "GETTING_STARTED.md",
                "GETTING_STARTED.zh-CN.md",
                "USER_GUIDE.md",
                "USER_GUIDE.zh-CN.md",
            ):
                self.assertTrue((output / relative).is_file(), relative)
            self.assertIn("README.zh-CN.md", public_readme)
            self.assertIn("GETTING_STARTED.md", (output / "README.zh-CN.md").read_text(encoding="utf-8"))
            self.assertIn("GETTING_STARTED.zh-CN.md", (output / "GETTING_STARTED.md").read_text(encoding="utf-8"))
            self.assertIn("USER_GUIDE.zh-CN.md", (output / "USER_GUIDE.md").read_text(encoding="utf-8"))
            self.assertTrue((output / "LICENSE").is_file())
            self.assertTrue((output / "THIRD_PARTY_NOTICES.md").is_file())
            public_project = tomllib.loads(
                (output / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]
            marker = json.loads(
                (output / ".mathproblem-public-release.json").read_text(encoding="utf-8")
            )
            self.assertEqual(public_project["version"], marker["release_version"])
            self.assertEqual(public_project["requires-python"], ">=3.12,<3.13")
            self.assertIn(
                "Python 3.12",
                (output / "README.md").read_text(encoding="utf-8"),
            )
            for relative in (
                "shared/scripts/ai_agent_repository.py",
                "shared/scripts/online_course_media_engine.py",
                "shared/scripts/problem_bank_center_qt.py",
            ):
                public_source = (output / relative).read_text(encoding="utf-8")
                self.assertNotIn("D 盘", public_source)
                self.assertNotIn("D盘", public_source)
            self.assertTrue((output / "MathAnalysis" / "preamble").is_dir())
            self.assertFalse((output / "shared" / "subjects.json").exists())
            self.assertFalse((output / "Physics").exists())
            self.assertFalse((output / "English").exists())
            self.assertFalse((output / "shared" / "ui" / "assets" / "carousel").exists())
            training = output / "shared" / "templates" / "ai_agent_training" / "lecture_quality"
            self.assertTrue((training / "README.md").is_file())
            self.assertTrue((training / "corpus.json").is_file())
            self.assertFalse((training / "historical_cases.json").exists())
            self.assertFalse((training / "full_eval_report.json").exists())
            self.assertFalse(
                (output / "shared" / "templates" / "ai_agent_training" / "physics_style_guide.md").exists()
            )
            training_root = output / "shared" / "templates" / "ai_agent_training"
            quality_pairs = json.loads(
                (training_root / "math_quality_pairs.json").read_text(encoding="utf-8")
            )
            self.assertTrue(quality_pairs)
            self.assertTrue(
                all(item.get("source") == "synthetic_public_fixture" for item in quality_pairs)
            )
            capability_suite = json.loads(
                (training_root / "math_capability_suite.json").read_text(encoding="utf-8")
            )
            acceptance_suite = json.loads(
                (training_root / "system_acceptance_suite.json").read_text(encoding="utf-8")
            )
            serialized_public_training = json.dumps(
                [quality_pairs, capability_suite, acceptance_suite],
                ensure_ascii=False,
            )
            self.assertNotIn("DM-" + "P", serialized_public_training)
            self.assertNotIn("DM-" + "C", serialized_public_training)
            self.assertNotIn("ChatGPT " + "网页版历史", serialized_public_training)
            self.assertTrue(
                all(str(item.get("id") or "").startswith("synthetic_") for item in capability_suite)
            )
            self.assertTrue(
                all(str(item.get("id") or "").startswith("synthetic_") for item in acceptance_suite)
            )
            self.assertFalse(
                (output / "shared" / "templates" / "ai_physics_learner_profile.txt").exists()
            )
            public_test_names = {
                path.name
                for path in (output / "shared" / "scripts").glob("test_*.py")
            }
            self.assertEqual(
                public_test_names,
                {"test_public_core.py", "test_release_engineering.py"},
            )
            forbidden_suffixes = (".db", ".sqlite", ".pdf", ".xdv", ".aux", ".log")
            forbidden = [
                path.relative_to(output).as_posix()
                for path in output.rglob("*")
                if path.is_file() and path.name.lower().endswith(forbidden_suffixes)
            ]
            self.assertEqual(forbidden, [])

    def test_public_exporter_self_hosts_without_private_policy_or_readme_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "first-public-release"
            second = base / "second-public-release"
            build_public_release(first)
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(first),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/build_public_release.py",
                    "--output",
                    str(second),
                ],
                cwd=first,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertIn("Public math release created", completed.stdout)
            self.assertTrue((second / "README.md").is_file())
            self.assertFalse((second / "shared" / "private_release_policy.local.py").exists())

    def test_release_first_run_creates_math_data_outside_program_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "public-release"
            data_root = base / "user-data"
            local_app_data = base / "local-app-data"
            build_public_release(output)
            environment = os.environ.copy()
            environment.update(
                {
                    "LOCALAPPDATA": str(local_app_data),
                    "MATH_PROBLEM_BANK_DATA_ROOT": str(data_root),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(output),
                    "STUDY_BANK_WORKSPACE": "math",
                }
            )
            script = (
                "import json\n"
                "from shared.scripts.study_project_service import "
                "APP_PATHS, ensure_subject_storage, load_subjects\n"
                "subjects = load_subjects('math')\n"
                "assert subjects\n"
                "name = next(iter(subjects))\n"
                "ensure_subject_storage(name)\n"
                "print(json.dumps({'name': name, 'db': str(subjects[name]['db']), "
                "'registry': str(APP_PATHS.subjects_registry_path), "
                "'data_root': str(APP_PATHS.user_data_root)}, ensure_ascii=False))\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=output,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            result = json.loads(completed.stdout.strip())
            reported_data_root = Path(result["data_root"])
            database = Path(result["db"])
            registry = Path(result["registry"])
            self.assertTrue(database.is_file())
            self.assertTrue(registry.is_file())
            self.assertTrue(reported_data_root.samefile(data_root))
            self.assertTrue(database.is_relative_to(reported_data_root))
            self.assertTrue(registry.is_relative_to(reported_data_root))
            self.assertFalse(any(output.rglob("*.db")))

    def test_release_qt_shell_starts_without_private_assets_or_integrations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "public-release"
            data_root = base / "user-data"
            local_app_data = base / "local-app-data"
            build_public_release(output)
            environment = os.environ.copy()
            environment.update(
                {
                    "LOCALAPPDATA": str(local_app_data),
                    "MATH_PROBLEM_BANK_DATA_ROOT": str(data_root),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(output),
                    "QT_QPA_PLATFORM": "offscreen",
                    "STUDY_BANK_WORKSPACE": "math",
                }
            )
            script = (
                "from PySide6.QtWidgets import QApplication\n"
                "from shared.scripts.ai_agent_qt import AiAgentPanel\n"
                "from shared.scripts.ai_agent_learner_profile import learner_profile_path\n"
                "from shared.scripts.problem_bank_center_qt import "
                "APP_PATHS, BackgroundWindow, SUBJECTS\n"
                "app = QApplication.instance() or QApplication([])\n"
                "window = BackgroundWindow()\n"
                "panel = AiAgentPanel(lambda: {})\n"
                "assert APP_PATHS.public_release\n"
                "assert SUBJECTS and window.workspace == 'math'\n"
                "assert not window.background_paths\n"
                "assert panel.account_usage_monitor is None\n"
                "assert panel.account_usage_button is None\n"
                "assert not learner_profile_path().exists()\n"
                "panel.close()\n"
                "window.close()\n"
                "app.processEvents()\n"
                "print('QT_RELEASE_SMOKE_OK')\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=output,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertIn("QT_RELEASE_SMOKE_OK", completed.stdout)
            self.assertFalse(any(output.rglob("*.db")))

    def test_public_user_background_directory_is_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "public-release"
            data_root = base / "user-data"
            local_app_data = base / "local-app-data"
            build_public_release(output, release_version="0.1.0rc1")
            environment = os.environ.copy()
            environment.update(
                {
                    "LOCALAPPDATA": str(local_app_data),
                    "MATH_PROBLEM_BANK_DATA_ROOT": str(data_root),
                    "PYTHONPATH": str(output),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            user_backgrounds = data_root / "config" / "backgrounds"
            user_backgrounds.mkdir(parents=True)
            (user_backgrounds / "custom-cover.png").write_bytes(b"synthetic png placeholder")
            (user_backgrounds / "custom-cover.jpg").write_bytes(b"synthetic jpg placeholder")
            script = (
                "from shared.scripts.problem_bank_center_qt import DashboardService, discover_backgrounds, USER_BACKGROUND_DIR\n"
                "paths = discover_backgrounds()\n"
                "assert USER_BACKGROUND_DIR.is_dir()\n"
                "assert {path.name for path in paths} == {'custom-cover.png', 'custom-cover.jpg'}, paths\n"
                "assert len({str(path.resolve()).casefold() for path in paths}) == len(paths)\n"
                "service = DashboardService.__new__(DashboardService)\n"
                "first = service.next_project_pdf_cover_background()\n"
                "second = service.next_project_pdf_cover_background()\n"
                "third = service.next_project_pdf_cover_background()\n"
                "assert first is not None and second is not None and third is not None\n"
                "assert first.resolve() != second.resolve(), (first, second)\n"
                "assert third.resolve() in {first.resolve(), second.resolve()}, third\n"
                "print('PUBLIC_USER_BACKGROUND_DIRECTORY_OK')\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=output,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            self.assertIn("PUBLIC_USER_BACKGROUND_DIRECTORY_OK", completed.stdout)

    def test_release_builder_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "public-release"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                build_public_release(output)

    def test_release_version_metadata_is_explicit_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "rc1"
            manifest = build_public_release(output, release_version="0.1.0rc1")
            project = tomllib.loads(
                (output / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]
            marker = json.loads(
                (output / ".mathproblem-public-release.json").read_text(encoding="utf-8")
            )
            self.assertEqual(project["version"], "0.1.0rc1")
            self.assertEqual(project["requires-python"], ">=3.12,<3.13")
            self.assertEqual(manifest["release_version"], "0.1.0rc1")
            self.assertEqual(manifest["python_requires"], ">=3.12,<3.13")
            self.assertEqual(marker["release_version"], "0.1.0rc1")
            self.assertEqual(marker["python_requires"], ">=3.12,<3.13")

            with self.assertRaisesRegex(ValueError, "PEP 440"):
                build_public_release(
                    base / "invalid",
                    release_version="v0.1.0-rc1",
                )

    def test_release_archive_is_closed_world_and_excludes_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "release"
            archive = base / "release.zip"
            manifest = build_public_release_archive(output, archive)
            self.assertTrue(archive.is_file())
            import zipfile

            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
            expected = {
                f"{output.name}/{item['path']}" for item in manifest["files"]
            } | {
                f"{output.name}/.mathproblem-public-release.json",
                f"{output.name}/PUBLIC_RELEASE_MANIFEST.json",
            }
            self.assertEqual(names, expected)
            self.assertFalse(any(name.endswith(".pyc") for name in names))

    def test_public_regression_entrypoint_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "public-release"
            data_root = base / "user-data"
            build_public_release(output)
            environment = os.environ.copy()
            environment.update(
                {
                    "LOCALAPPDATA": str(base / "local-app-data"),
                    "MATH_PROBLEM_BANK_DATA_ROOT": str(data_root),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(output),
                    "QT_QPA_PLATFORM": "offscreen",
                }
            )
            completed = subprocess.run(
                [sys.executable, "shared/scripts/public_regression_core.py"],
                cwd=output,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            self.assertIn("all public regressions passed", completed.stdout)
            self.assertFalse(any(output.rglob("*.db")))
            self.assertFalse(any(output.rglob("*.pyc")))

    def test_public_default_ai_profiles_are_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "public-release"
            build_public_release(output)
            environment = os.environ.copy()
            environment.update(
                {
                    "MATH_PROBLEM_BANK_PUBLIC_RELEASE": "1",
                    "MATH_PROBLEM_BANK_DATA_ROOT": str(root / "data"),
                    "PYTHONPATH": str(output),
                }
            )
            code = (
                "from shared.scripts.ai_agent_config import default_profiles\n"
                "profiles = default_profiles()\n"
                "assert profiles and profiles[0].name == 'OpenAI'\n"
                "assert profiles[0].base_url == 'https://api.openai.com/v1'\n"
                "assert all(not p.name.startswith('题库管理中心') for p in profiles)\n"
                "print(profiles[0].base_url)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=output,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertIn("api.openai.com", completed.stdout)


if __name__ == "__main__":
    unittest.main()
