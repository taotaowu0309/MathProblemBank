from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

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
    @unittest.skipUnless(shutil.which("git"), "Git is required for the publication check")
    def test_manifest_files_can_be_staged_without_force_or_private_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            manifest = build_public_release(output)
            expected = {row["path"] for row in manifest["files"]} | {
                ".mathproblem-public-release.json", "PUBLIC_RELEASE_MANIFEST.json",
            }
            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(output), "-c", "core.autocrlf=false", *args],
                    check=True, capture_output=True, text=True, encoding="utf-8",
                )
            git("init", "--template=", "--initial-branch=main")
            git("add", "--", *sorted(expected))
            self.assertEqual(set(git("ls-files", "-z").stdout.split("\0")) - {""}, expected)

    def test_sensitive_scanner_catches_raw_escaped_and_credential_paths(self) -> None:
        slash = chr(92)
        raw_path = "C:" + slash + "Users" + slash + "ExampleUser" + slash + "AppData" + slash + "Local" + slash + "file.json"
        escaped_path = raw_path.replace(slash, slash + slash)
        self.assertIsNotNone(_sensitive_marker(raw_path))
        self.assertIsNotNone(_sensitive_marker(escaped_path))
        private_path = "D:" + "/EnglishProjects/" + "RayDuSeries"
        self.assertIsNotNone(_sensitive_marker(private_path))
        self.assertIsNone(_sensitive_marker("D:" + "/Temp/example-release"))
        user = "user"
        password = "secret"
        host = "example.invalid"
        credential_url = "https://" + user + ":" + password + "@" + host + "/v1"
        self.assertIsNotNone(_sensitive_marker(credential_url))
        self.assertIsNone(_sensitive_marker("https://api.openai.com/v1"))

        marker = "private-release-fixture.example.invalid"
        self.assertEqual(_sensitive_marker(marker, (marker,)), marker)

    def test_secret_patterns_cover_github_cloud_and_private_keys(self) -> None:
        from tools.build_public_release import HIGH_CONFIDENCE_SECRET_RE
        samples = (
            "ghp" + "_" + "x" * 40,
            "github" + "_pat_" + "x" * 70,
            "AK" + "IA" + "X" * 16,
            "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        )
        for sample in samples:
            self.assertIsNotNone(HIGH_CONFIDENCE_SECRET_RE.search(sample))

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

    def test_release_version_is_consistent_in_user_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "public-release"
            build_public_release(output, release_version="0.1.0rc3")
            for relative in (
                "README.md",
                "README.zh-CN.md",
                "GETTING_STARTED.md",
                "GETTING_STARTED.zh-CN.md",
            ):
                text = (output / relative).read_text(encoding="utf-8")
                self.assertIn("0.1.0-rc3", text, relative)
                self.assertNotRegex(text, r"0\.1\.0-rc[12]|0\.1\.0rc[12]")

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
            self.assertIn("Public release created", completed.stdout)
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

    def test_release_contains_workflow_prompts_and_no_personal_examples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            manifest = build_public_release(output)
            paths = {row["path"] for row in manifest["files"]}
            for name in (
                "LaunchStudyProblemBank.command", "AGENTS.md",
                ".agents/skills/mathproblembank-workflow/SKILL.md",
                "docs/CHATGPT_WORK_GUIDE.zh-CN.md", "docs/AGENT_PROMPTS.zh-CN.md",
                "docs/PROMPTS.zh-CN.md", "shared/skills/vendor/mathtranslations/LICENSE",
                "shared/scripts/diagram_backends/graphviz_render.mjs",
            ):
                self.assertIn(name, paths)
            self.assertEqual(manifest["profile"], "public-math-english-v0.2")
            for name in ("textbook_note_example.tex", "question_note_example.tex"):
                example = (output / "shared/templates" / name).read_text(encoding="utf-8")
                self.assertIn("合成", example)
                self.assertNotIn("【我的想法】", example)
            repair = json.loads((output / "shared/skills/mathtranslations_source_repair/references/accepted_repairs.json").read_text(encoding="utf-8"))
            self.assertEqual(repair["examples"][0]["status"], "synthetic_public_fixture")
            self.assertNotIn("project_code", json.dumps(repair))

    def test_archive_rejects_tampered_file_and_removes_partial_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            archive = Path(temporary) / "release.zip"
            def tamper(*args, **kwargs):
                manifest = build_public_release(*args, **kwargs)
                (output / "README.md").write_text("changed", encoding="utf-8")
                return manifest
            with patch("tools.build_public_release.build_public_release", side_effect=tamper):
                with self.assertRaisesRegex(RuntimeError, "哈希"):
                    build_public_release_archive(output, archive)
            self.assertFalse(archive.exists())

    def test_builder_rejects_symlink_and_unreviewed_binary(self) -> None:
        from tools import build_public_release as builder
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "private.txt"
            source.write_text("not public", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(source)
            except OSError:
                self.skipTest("Creating symbolic links is unavailable on this platform")
            with patch.object(builder, "ROOT", root), patch.object(builder, "_selected_files", return_value=[(Path("linked.txt"), Path("linked.txt"))]):
                with self.assertRaisesRegex(RuntimeError, "符号链接"):
                    builder.build_public_release(root / "out")
        self.assertIsNotNone(builder._forbidden_reason(Path("shared/ui/assets/personal.png")))
        self.assertIsNotNone(builder._sensitive_marker("/" + "Volumes/ExampleVolume/private/file"))

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

if __name__ == "__main__":
    unittest.main()
