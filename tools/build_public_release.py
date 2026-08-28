from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
MARKER_NAME = ".mathproblem-public-release.json"
ROOT_FILES = (
    ".gitignore",
    "LaunchStudyProblemBank.vbs",
    "README.md",
    "OPEN_SOURCE_RELEASE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CLEAN_WINDOWS_E2E.md",
    "pyproject.toml",
    "requirements-public.txt",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)
PUBLIC_README_SOURCE = Path("shared/templates/public_release/README.md")
PUBLIC_PYTHON_REQUIRES = ">=3.12,<3.13"
RELEASE_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.dev\d+|\.post\d+)?$"
)
SOURCE_TREES = (
    Path(".github"),
    Path("tools"),
    Path("shared/scripts"),
    Path("shared/ui"),
    Path("shared/templates"),
    Path("shared/vendor"),
    Path("shared/browser_extensions/online_course_recorder"),
    Path("MathAnalysis/preamble"),
)
SKIPPED_RELATIVE_PREFIXES = (
    Path("shared/scripts/diagram_backends"),
    Path("shared/ui/config/ui_state.json"),
    Path("shared/templates/ai_math_learner_profile.txt"),
    Path("shared/templates/ai_physics_learner_profile.txt"),
    Path("shared/templates/ai_agent_training/lecture_quality"),
    Path("shared/templates/ai_agent_training/math_quality_pairs.json"),
    Path("shared/templates/ai_agent_training/math_capability_suite.json"),
    Path("shared/templates/ai_agent_training/system_acceptance_suite.json"),
    Path("shared/templates/ai_agent_training_public"),
    Path("shared/templates/public_release"),
)
PUBLIC_FIXTURE_FILES = (
    (
        Path("shared/templates/ai_agent_training_public/lecture_quality"),
        Path("shared/templates/ai_agent_training/lecture_quality"),
    ),
    (
        Path("shared/templates/ai_agent_training_public/math_quality_pairs.json"),
        Path("shared/templates/ai_agent_training/math_quality_pairs.json"),
    ),
    (
        Path("shared/templates/ai_agent_training_public/math_capability_suite.json"),
        Path("shared/templates/ai_agent_training/math_capability_suite.json"),
    ),
    (
        Path("shared/templates/ai_agent_training_public/system_acceptance_suite.json"),
        Path("shared/templates/ai_agent_training/system_acceptance_suite.json"),
    ),
)
SKIPPED_PARTS = {
    ".git",
    ".ruff_cache",
    "__pycache__",
    "cache",
    "legacy",
    "node_modules",
}
SKIPPED_SCRIPT_PREFIXES = ("backfill_", "renumber_")
PUBLIC_TEST_SCRIPT_NAMES = {
    "test_public_core.py",
    "test_release_engineering.py",
}
SKIPPED_SCRIPT_NAMES = {
    "ai_agent_account_usage.py",
    "export_mathanalysis_chapters.py",
    "regression_core.py",
    "test_ai_agent.py",
    "test_english_learning.py",
    "test_online_course_lecture_quality.py",
    "test_online_course_media_engine.py",
    "test_online_course_service.py",
    "test_pdf_vocabulary_lookup.py",
    "test_quick_video_transcript.py",
    "test_textbook_exercise_companion.py",
    "test_textbook_exercise_companion_ui.py",
}
SKIPPED_UI_ASSET_PREFIXES = (
    Path("shared/ui/assets/carousel"),
    Path("shared/ui/assets/carousel_original_before_2560x1600_2026-07-14"),
    Path("shared/ui/assets/carousel_original_before_text_fix_2026-07-21"),
)
FORBIDDEN_SUFFIXES = {
    ".aux",
    ".db",
    ".db-shm",
    ".db-wal",
    ".fdb_latexmk",
    ".fls",
    ".log",
    ".pdf",
    ".sqlite",
    ".sqlite3",
    ".synctex.gz",
    ".xdv",
}
FORBIDDEN_NAME_FRAGMENTS = {
    ".env",
    "credential",
    "private-backup",
    "secret",
    "token",
}
ALLOWED_ARCHIVES = {
    Path("shared/vendor/summarize/steipete-summarize-0.21.6.tgz"),
}
PUBLIC_GITIGNORE_APPEND = """

# Public-release runtime data must stay outside Git
user-data/
runtime-data/
*.db
*.db-shm
*.db-wal
*.sqlite
*.sqlite3
*.pdf
*.zip
*.7z
*.rar
*.synctex.gz
"""
PRIVATE_RELEASE_POLICY_PATH = Path("shared/private_release_policy.local.py")


def _load_private_release_policy(root: Path = ROOT) -> dict[str, tuple[str, ...]]:
    path = root / PRIVATE_RELEASE_POLICY_PATH
    if not path.is_file():
        return {"PRIVATE_TEXT_MARKERS": (), "PRIVATE_NAME_FRAGMENTS": ()}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise RuntimeError(f"无法读取私有发行策略：{path}: {error}") from error
    values: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in {
                "PRIVATE_TEXT_MARKERS",
                "PRIVATE_NAME_FRAGMENTS",
            }:
                continue
            try:
                raw = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError) as error:
                raise RuntimeError(f"私有发行策略字段无效：{target.id}") from error
            if not isinstance(raw, (list, tuple)) or not all(
                isinstance(item, str) and item for item in raw
            ):
                raise RuntimeError(f"私有发行策略字段必须是非空字符串列表：{target.id}")
            values[target.id] = tuple(raw)
    return {
        "PRIVATE_TEXT_MARKERS": values.get("PRIVATE_TEXT_MARKERS", ()),
        "PRIVATE_NAME_FRAGMENTS": values.get("PRIVATE_NAME_FRAGMENTS", ()),
    }


_PRIVATE_RELEASE_POLICY = _load_private_release_policy()
PRIVATE_TEXT_MARKERS = _PRIVATE_RELEASE_POLICY["PRIVATE_TEXT_MARKERS"]
PRIVATE_NAME_FRAGMENTS = _PRIVATE_RELEASE_POLICY["PRIVATE_NAME_FRAGMENTS"]
WINDOWS_USER_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/]{1,2}users[\\/]|\\\\[^\\/\s]+[\\/]+users[\\/])[^\s\"']+"
)
POSIX_USER_PATH_RE = re.compile(
    r"(?i)(?:/" + "home/" + r"|/" + "users/)" + r"[^\s\"']+"
)
URL_CREDENTIAL_RE = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@")
HIGH_CONFIDENCE_SECRET_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})"
)
PRIVATE_TRAINING_MARKERS = (
    "DM-" + "P",
    "DM-" + "C",
    "chatgpt_" + "web_history",
    "ChatGPT " + "网页版历史",
    "题库助手" + "历史",
    "paired_" + "user_feedback",
    "user " + "feedback",
    "用户" + "反馈",
    "近期数学" + "学习对话",
    "个人" + "学习画像",
    "private " + "learner profile",
)
SCANNED_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".pyw",
    ".tex",
    ".toml",
    ".txt",
    ".vbs",
    ".yaml",
    ".yml",
}


def _relative_files(root: Path) -> Iterable[Path]:
    for root_name in ROOT_FILES:
        path = root / root_name
        if path.is_file():
            yield Path(root_name)
    for source_tree in SOURCE_TREES:
        base = root / source_tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part.lower() in SKIPPED_PARTS for part in relative.parts):
                continue
            if any(relative.is_relative_to(prefix) for prefix in SKIPPED_UI_ASSET_PREFIXES):
                continue
            if any(relative.is_relative_to(prefix) for prefix in SKIPPED_RELATIVE_PREFIXES):
                continue
            if relative.parent == Path("shared/scripts"):
                if relative.name.startswith(SKIPPED_SCRIPT_PREFIXES):
                    continue
                if (
                    relative.name.startswith("test_")
                    and relative.name not in PUBLIC_TEST_SCRIPT_NAMES
                ):
                    continue
                if relative.name in SKIPPED_SCRIPT_NAMES:
                    continue
            if (
                relative.parent == Path("shared/templates/ai_agent_training")
                and relative.name.startswith("physics_")
            ):
                continue
            yield relative


def _forbidden_reason(relative: Path) -> str | None:
    lowered = relative.as_posix().lower()
    if relative in ALLOWED_ARCHIVES:
        return None
    if any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return "user data or generated artifact suffix"
    if lowered.endswith((".zip", ".7z", ".rar", ".tgz")):
        return "archive is not explicitly allowlisted"
    if any(fragment in lowered for fragment in FORBIDDEN_NAME_FRAGMENTS):
        return "sensitive or private filename"
    if any(fragment.casefold() in lowered for fragment in PRIVATE_NAME_FRAGMENTS):
        return "filename blocked by private release policy"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sensitive_marker(
    text: str,
    private_markers: Iterable[str] = PRIVATE_TEXT_MARKERS,
) -> str | None:
    normalized = text.replace("\\\\", "\\")
    for marker in private_markers:
        if marker.lower() in normalized.lower() or marker.lower() in text.lower():
            return marker
    if WINDOWS_USER_PATH_RE.search(normalized) or WINDOWS_USER_PATH_RE.search(text):
        return "Windows user/profile path"
    if POSIX_USER_PATH_RE.search(text):
        return "POSIX user/profile path"
    if URL_CREDENTIAL_RE.search(text):
        return "URL credentials"
    return None


def _private_training_marker(relative: Path, text: str) -> str | None:
    training_root = Path("shared/templates/ai_agent_training")
    if not relative.is_relative_to(training_root) and relative.name not in {
        "ai_math_learner_profile.txt",
        "ai_physics_learner_profile.txt",
    }:
        return None
    folded = text.casefold()
    for marker in PRIVATE_TRAINING_MARKERS:
        if marker.casefold() in folded:
            return marker
    return None


def _selected_files(root: Path) -> list[tuple[Path, Path]]:
    public_readme = root / PUBLIC_README_SOURCE
    selected = [
        (
            PUBLIC_README_SOURCE
            if relative == Path("README.md") and public_readme.is_file()
            else relative,
            relative,
        )
        for relative in _relative_files(root)
    ]
    for source_path, target_path in PUBLIC_FIXTURE_FILES:
        source = root / source_path
        if not source.exists() and (root / MARKER_NAME).is_file():
            source = root / target_path
        if source.is_file():
            selected.append((source.relative_to(root), target_path))
            continue
        if not source.is_dir():
            continue
        for fixture in sorted(source.rglob("*")):
            if fixture.is_file():
                selected.append(
                    (
                        fixture.relative_to(root),
                        target_path / fixture.relative_to(source),
                    )
                )
    return list(dict.fromkeys(selected))


def _public_pyproject_text(source: Path, release_version: str | None) -> tuple[str, str]:
    text = source.read_text(encoding="utf-8")
    try:
        project = tomllib.loads(text).get("project") or {}
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise RuntimeError(f"无法解析 pyproject.toml：{error}") from error
    source_version = str(project.get("version") or "").strip()
    selected_version = str(release_version or source_version).strip()
    if not RELEASE_VERSION_RE.fullmatch(selected_version):
        raise ValueError(
            "公开发行版本必须使用受支持的 PEP 440 格式，例如 0.1.0rc1 或 0.1.0。"
        )
    replacements = {
        "version": selected_version,
        "requires-python": PUBLIC_PYTHON_REQUIRES,
    }
    for key, value in replacements.items():
        pattern = re.compile(rf'(?m)^{re.escape(key)}\s*=\s*"[^"]*"\s*$')
        text, count = pattern.subn(f'{key} = "{value}"', text, count=1)
        if count != 1:
            raise RuntimeError(f"pyproject.toml 缺少唯一的 project.{key} 字段。")
    try:
        exported_project = tomllib.loads(text).get("project") or {}
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"公开 pyproject.toml 转换后无效：{error}") from error
    if exported_project.get("version") != selected_version:
        raise RuntimeError("公开 pyproject.toml 版本写入后回读失败。")
    if exported_project.get("requires-python") != PUBLIC_PYTHON_REQUIRES:
        raise RuntimeError("公开 pyproject.toml Python 范围写入后回读失败。")
    return text, selected_version


def build_public_release(
    output: Path,
    *,
    release_version: str | None = None,
) -> dict[str, object]:
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"公开发行目标已存在，拒绝覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    selected = _selected_files(ROOT)
    forbidden = [
        f"{target_relative.as_posix()}: {_forbidden_reason(target_relative)}"
        for _source_relative, target_relative in selected
        if _forbidden_reason(target_relative)
    ]
    if forbidden:
        raise RuntimeError("白名单中出现禁止发布的文件：\n" + "\n".join(forbidden))

    staging = Path(tempfile.mkdtemp(prefix="mathproblem-public-", dir=str(output.parent)))
    try:
        manifest_files: list[dict[str, object]] = []
        for source_relative, target_relative in selected:
            source = ROOT / source_relative
            target = staging / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source_relative == Path(".gitignore"):
                target.write_text(
                    source.read_text(encoding="utf-8").rstrip()
                    + "\n"
                    + PUBLIC_GITIGNORE_APPEND.lstrip(),
                    encoding="utf-8",
                )
            elif target_relative == Path("pyproject.toml"):
                public_pyproject, selected_release_version = _public_pyproject_text(
                    source,
                    release_version,
                )
                target.write_text(public_pyproject, encoding="utf-8")
            else:
                shutil.copy2(source, target)
            if target.suffix.lower() in SCANNED_TEXT_SUFFIXES:
                text = target.read_text(encoding="utf-8", errors="ignore")
                marker = _sensitive_marker(text)
                if marker:
                    raise RuntimeError(f"公开发行文件包含私人文本标记：{target_relative}: {marker}")
                training_marker = _private_training_marker(target_relative, text)
                if training_marker:
                    raise RuntimeError(
                        "公开训练或画像文件包含私人来源标记："
                        f"{target_relative}: {training_marker}"
                    )
                if HIGH_CONFIDENCE_SECRET_RE.search(text):
                    raise RuntimeError(f"公开发行文件疑似包含密钥或 Bearer token：{target_relative}")
            manifest_files.append(
                {
                    "path": target_relative.as_posix(),
                    "bytes": target.stat().st_size,
                    "sha256": _sha256(target),
                }
            )

        marker = {
            "product": "MathProblemBank",
            "profile": "public-math-v0.1",
            "release_version": selected_release_version,
            "python_requires": PUBLIC_PYTHON_REQUIRES,
            "user_data_policy": "%LOCALAPPDATA%/MathProblemBank unless overridden",
        }
        (staging / MARKER_NAME).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "format_version": 1,
            "profile": "public-math-v0.1",
            "release_version": selected_release_version,
            "python_requires": PUBLIC_PYTHON_REQUIRES,
            "file_count": len(manifest_files),
            "files": manifest_files,
        }
        (staging / "PUBLIC_RELEASE_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_public_release_archive(
    output: Path,
    archive: Path,
    *,
    release_version: str | None = None,
) -> dict[str, object]:
    """Build a fresh release directory and a closed-world ZIP beside it.

    The archive is created only from the fresh directory. Callers should run
    tests against a separate staging directory and never mutate this output
    before publishing it.
    """
    archive = Path(archive).expanduser().resolve()
    if archive.exists():
        raise FileExistsError(f"公开发行压缩包已存在，拒绝覆盖：{archive}")
    manifest = build_public_release(output, release_version=release_version)
    output = Path(output).resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest_paths = {str(item["path"]) for item in manifest["files"]}
    metadata_paths = {MARKER_NAME, "PUBLIC_RELEASE_MANIFEST.json"}
    included_paths: set[str] = set()
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(output.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(output).as_posix()
            if relative not in manifest_paths and relative not in metadata_paths:
                raise RuntimeError(f"发行目录包含未声明文件，拒绝压缩：{relative}")
            bundle.write(path, f"{output.name}/{relative}")
            included_paths.add(relative)
    expected_paths = manifest_paths | metadata_paths
    if included_paths != expected_paths:
        missing = sorted(expected_paths - included_paths)
        extra = sorted(included_paths - expected_paths)
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"压缩包文件闭包校验失败：missing={missing}, extra={extra}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a clean, math-only public release view without Git history or user data."
    )
    parser.add_argument("--output", type=Path, required=True, help="new, non-existing output directory")
    parser.add_argument("--zip", type=Path, help="optional new, non-existing closed-world ZIP path")
    parser.add_argument(
        "--release-version",
        help="public PEP 440 version, for example 0.1.0rc1 or 0.1.0",
    )
    args = parser.parse_args()
    manifest = (
        build_public_release_archive(
            args.output,
            args.zip,
            release_version=args.release_version,
        )
        if args.zip is not None
        else build_public_release(args.output, release_version=args.release_version)
    )
    print(
        f"Public math release created: {args.output.resolve()} "
        f"({manifest['file_count']} files)"
    )
    if args.zip is not None:
        print(f"Public archive created: {args.zip.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
