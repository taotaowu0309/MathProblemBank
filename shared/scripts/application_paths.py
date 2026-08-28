from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


APPLICATION_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_RELEASE_MARKER = ".mathproblem-public-release.json"
PUBLIC_RELEASE_ENV = "MATH_PROBLEM_BANK_PUBLIC_RELEASE"
DATA_ROOT_ENV = "MATH_PROBLEM_BANK_DATA_ROOT"
COURSE_ROOT_ENV = "MATH_PROBLEM_BANK_COURSE_ROOT"
QUICK_TRANSCRIPT_ROOT_ENV = "MATH_PROBLEM_BANK_QUICK_TRANSCRIPT_ROOT"
REFERENCE_LIBRARY_ROOT_ENV = "MATH_PROBLEM_BANK_REFERENCE_ROOT"
RUNTIME_ROOT_ENV = "MATH_PROBLEM_BANK_RUNTIME_ROOT"
MMA_MCP_ROOT_ENV = "MATH_PROBLEM_BANK_MMA_MCP_ROOT"
WOLFRAM_KERNEL_ENV = "MATH_PROBLEM_BANK_WOLFRAM_KERNEL"
PRIVATE_PATHS_FILENAME = "private_paths.local.json"


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _configured_path(environ: Mapping[str, str], name: str) -> Path | None:
    value = str(environ.get(name) or "").strip()
    return Path(value).expanduser().resolve() if value else None


def _local_app_data(environ: Mapping[str, str]) -> Path:
    configured = str(environ.get("LOCALAPPDATA") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "AppData" / "Local").resolve()


def _private_path_overrides(application_root: Path) -> dict[str, str]:
    path = application_root / "shared" / PRIVATE_PATHS_FILENAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in raw.items()
        if str(value).strip()
    }


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    application_root: Path
    public_release: bool
    user_data_root: Path
    config_dir: Path
    settings_dir: Path
    cache_dir: Path
    log_dir: Path
    workspace_root: Path
    subjects_registry_path: Path
    vocabulary_root: Path
    online_course_root: Path
    quick_transcript_root: Path
    runtime_root: Path
    reference_library_root: Path
    mma_mcp_root: Path
    wolfram_kernel: Path
    author_name: str
    relay_api_base_url: str
    relay_profile_name: str
    relay_api_key_env: str

    def ensure_runtime_directories(self) -> None:
        for path in (
            self.user_data_root,
            self.config_dir,
            self.cache_dir,
            self.log_dir,
            self.workspace_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def resolve_application_paths(
    application_root: Path = APPLICATION_ROOT,
    environ: Mapping[str, str] | None = None,
) -> ApplicationPaths:
    env = os.environ if environ is None else environ
    root = Path(application_root).resolve()
    public_release = _truthy(env.get(PUBLIC_RELEASE_ENV)) or (root / PUBLIC_RELEASE_MARKER).is_file()
    configured_data_root = _configured_path(env, DATA_ROOT_ENV)

    isolated_data = public_release or configured_data_root is not None
    if isolated_data:
        user_data_root = configured_data_root or (_local_app_data(env) / "MathProblemBank")
        config_dir = user_data_root / "config"
        settings_dir = config_dir
        cache_dir = user_data_root / "cache"
        log_dir = user_data_root / "logs"
        workspace_root = user_data_root / "workspaces"
        subjects_registry_path = config_dir / "subjects.json"
        vocabulary_root = user_data_root
        legacy_course_root = user_data_root / "content" / "online_courses"
        legacy_quick_root = user_data_root / "content" / "quick_transcripts"
        legacy_runtime_root = user_data_root / "runtime"
        legacy_reference_root = user_data_root / "content" / "reference_library"
        legacy_mma_root = user_data_root / "integrations" / "mma-mcp"
        legacy_wolfram_kernel = user_data_root / "integrations" / "WolframKernel.exe"
    else:
        user_data_root = root
        config_dir = root / "shared"
        cache_dir = root / "shared" / "ui" / "cache"
        settings_dir = cache_dir
        log_dir = cache_dir
        workspace_root = root
        subjects_registry_path = root / "shared" / "subjects.json"
        vocabulary_root = root
        local_defaults = _local_app_data(env) / "MathProblemBank"
        private_paths = _private_path_overrides(root)
        legacy_course_root = Path(
            private_paths.get("online_course_root", local_defaults / "content" / "online_courses")
        )
        legacy_quick_root = Path(
            private_paths.get("quick_transcript_root", local_defaults / "content" / "quick_transcripts")
        )
        legacy_runtime_root = Path(
            private_paths.get("runtime_root", local_defaults / "runtime")
        )
        legacy_reference_root = Path(
            private_paths.get("reference_library_root", local_defaults / "content" / "reference_library")
        )
        legacy_mma_root = Path(
            private_paths.get("mma_mcp_root", local_defaults / "integrations" / "mma-mcp")
        )
        legacy_wolfram_kernel = Path(
            private_paths.get(
                "wolfram_kernel",
                local_defaults / "integrations" / "WolframKernel.exe",
            )
        )

    private_paths = {} if public_release else _private_path_overrides(root)
    author_name = (
        "MathProblemBank User"
        if public_release
        else str(private_paths.get("author_name") or "MathProblemBank User")
    )
    relay_api_base_url = (
        ""
        if public_release
        else str(private_paths.get("relay_api_base_url") or "")
    )
    relay_profile_name = (
        ""
        if public_release
        else str(private_paths.get("relay_profile_name") or "")
    )
    relay_api_key_env = (
        ""
        if public_release
        else str(private_paths.get("relay_api_key_env") or "")
    )

    return ApplicationPaths(
        application_root=root,
        public_release=public_release,
        user_data_root=user_data_root.resolve(),
        config_dir=config_dir.resolve(),
        settings_dir=settings_dir.resolve(),
        cache_dir=cache_dir.resolve(),
        log_dir=log_dir.resolve(),
        workspace_root=workspace_root.resolve(),
        subjects_registry_path=subjects_registry_path.resolve(),
        vocabulary_root=vocabulary_root.resolve(),
        online_course_root=(_configured_path(env, COURSE_ROOT_ENV) or legacy_course_root).resolve(),
        quick_transcript_root=(_configured_path(env, QUICK_TRANSCRIPT_ROOT_ENV) or legacy_quick_root).resolve(),
        runtime_root=(_configured_path(env, RUNTIME_ROOT_ENV) or legacy_runtime_root).resolve(),
        reference_library_root=(
            _configured_path(env, REFERENCE_LIBRARY_ROOT_ENV) or legacy_reference_root
        ).resolve(),
        mma_mcp_root=(_configured_path(env, MMA_MCP_ROOT_ENV) or legacy_mma_root).resolve(),
        wolfram_kernel=(_configured_path(env, WOLFRAM_KERNEL_ENV) or legacy_wolfram_kernel).resolve(),
        author_name=author_name,
        relay_api_base_url=relay_api_base_url,
        relay_profile_name=relay_profile_name,
        relay_api_key_env=relay_api_key_env,
    )


APP_PATHS = resolve_application_paths()
