from __future__ import annotations

import json
import mimetypes
import os
import re
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from shared.scripts.ai_agent_config import _dpapi_protect, _dpapi_unprotect
from shared.scripts.application_paths import APP_PATHS


ROOT_DIR = Path(__file__).resolve().parents[2]
COURSE_STORAGE_ROOT = APP_PATHS.online_course_root
SUMMARIZE_VERSION = "0.21.6"
SUMMARIZE_COMMIT = "67b6c475ba27b1601a0394c593977162fa2b5197"
SUMMARIZE_VENDOR_DIR = ROOT_DIR / "shared" / "vendor" / "summarize"
SUMMARIZE_PACKAGE = SUMMARIZE_VENDOR_DIR / f"steipete-summarize-{SUMMARIZE_VERSION}.tgz"
SUMMARIZE_RUNTIME_DIR = COURSE_STORAGE_ROOT / "runtime" / "summarize"
SUMMARIZE_SETTINGS_PATH = COURSE_STORAGE_ROOT / "media_engine_settings.json"
SUMMARIZE_CREDENTIALS_PATH = COURSE_STORAGE_ROOT / "media_engine_credentials.json"
MEDIA_TOOL_RUNTIME_DIR = COURSE_STORAGE_ROOT / "runtime" / "media_tools"
YT_DLP_VERSION = "2026.07.04"
PYSCENEDETECT_VERSION = "0.7.1"
CLAUDE_REAL_VIDEO_VERSION = "0.7.16"
FFMPEG_BUILD_LABEL = "yt-dlp-ffmpeg-builds-2026-07-26"
YT_DLP_URL = (
    f"https://github.com/yt-dlp/yt-dlp/releases/download/{YT_DLP_VERSION}/yt-dlp.exe"
)
YT_DLP_CHECKSUMS_URL = (
    f"https://github.com/yt-dlp/yt-dlp/releases/download/{YT_DLP_VERSION}/SHA2-256SUMS"
)
FFMPEG_ARCHIVE_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

TRANSCRIPTION_PROVIDERS: dict[str, tuple[str, str]] = {
    "groq": ("Groq Whisper", "GROQ_API_KEY"),
}
GROQ_MODELS_ENDPOINT = "https://api.groq.com/openai/v1/models"
GROQ_AUDIO_TRANSCRIPT_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_AUDIO_TRANSCRIPT_MODEL = "whisper-large-v3-turbo"
# Groq's edge protection rejects urllib's default Python User-Agent with HTTP
# 403/code 1010. Use a stable application identifier for both probe and ASR.
GROQ_HTTP_USER_AGENT = "MathProblemBankRecorder/1.0"

# Groq's published base limits for both Whisper models are 20 RPM, 2,000 RPD,
# 7,200 audio seconds/hour and 28,800 audio seconds/day.  RPM is the limiting
# factor for the recorder's short chunk workflow, so keep a small rolling-window
# cushion and also honor Groq's 429 retry delay when other clients share the
# organization quota.
GROQ_WHISPER_RPM_LIMIT = 20
GROQ_WHISPER_RPD_LIMIT = 2_000
GROQ_WHISPER_AUDIO_SECONDS_PER_HOUR_LIMIT = 7_200
GROQ_WHISPER_AUDIO_SECONDS_PER_DAY_LIMIT = 28_800
GROQ_WHISPER_RATE_WINDOW_SECONDS = 60.0
GROQ_WHISPER_RATE_WINDOW_CUSHION_SECONDS = 0.75
GROQ_WHISPER_MAX_RPM_RETRIES = 8


class NoSpeechDetectedError(RuntimeError):
    """The transcription runtime succeeded but found no usable speech."""


class GroqWhisperServerError(RuntimeError):
    """A retryable Groq 5xx persisted for one specific audio request."""

    def __init__(
        self,
        *,
        status_code: int,
        detail: str,
        diagnostics: dict[str, Any],
    ) -> None:
        self.status_code = int(status_code)
        self.detail = str(detail or "").strip()
        self.diagnostics = diagnostics
        super().__init__(
            f"Groq Whisper server error (HTTP {self.status_code}) after "
            f"{len(diagnostics.get('attempts') or [])} consecutive attempts: "
            f"{self.detail[:1000]}"
        )


def hidden_subprocess_options() -> dict[str, Any]:
    """Prevent command processors and console tools from flashing a window."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "shell": False,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass(slots=True)
class MediaTranscript:
    text: str
    provider: str
    source: str
    segments: list[dict[str, Any]]


@dataclass(slots=True)
class PreparedMediaChunk:
    source: Path
    audio_path: Path
    duration: float
    scene_ranges: list[tuple[float, float]]
    board_candidates: list[dict[str, Any]] = field(default_factory=list)
    board_analysis_version: int = 0


class OnlineCourseMediaEngine:
    """Thin, pinned adapter around steipete/summarize.

    The Node dependency tree lives on the D drive with the recordings. Only the
    reviewed upstream package, license, and source metadata are kept in Git.
    Provider credentials are encrypted with the same Windows DPAPI mechanism as
    the AI assistant credentials and are never placed on a command line.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path = SUMMARIZE_RUNTIME_DIR,
        package_path: Path = SUMMARIZE_PACKAGE,
        settings_path: Path = SUMMARIZE_SETTINGS_PATH,
        credentials_path: Path = SUMMARIZE_CREDENTIALS_PATH,
        tool_runtime_dir: Path = MEDIA_TOOL_RUNTIME_DIR,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.package_path = Path(package_path)
        self.settings_path = Path(settings_path)
        self.credentials_path = Path(credentials_path)
        self.tool_runtime_dir = Path(tool_runtime_dir)
        self._groq_rate_condition = threading.Condition()
        self._groq_request_started_at: deque[float] = deque()
        self._quick_groq_bypass_proxy: bool | None = None

    @property
    def yt_dlp_path(self) -> Path:
        return self.tool_runtime_dir / "yt-dlp.exe"

    @property
    def ffmpeg_path(self) -> Path:
        return self.tool_runtime_dir / "ffmpeg.exe"

    @property
    def ffprobe_path(self) -> Path:
        return self.tool_runtime_dir / "ffprobe.exe"

    @property
    def scenedetect_runtime_dir(self) -> Path:
        return self.tool_runtime_dir / "python"

    @property
    def toolchain_manifest_path(self) -> Path:
        return self.tool_runtime_dir / "toolchain_manifest.json"

    @property
    def command_path(self) -> Path:
        suffix = ".cmd" if os.name == "nt" else ""
        return self.runtime_dir / "node_modules" / ".bin" / f"summarize{suffix}"

    @property
    def cli_path(self) -> Path:
        return (
            self.runtime_dir
            / "node_modules"
            / "@steipete"
            / "summarize"
            / "dist"
            / "cli.js"
        )

    @staticmethod
    def _node_path() -> Path:
        executable = shutil.which("node")
        if not executable:
            raise RuntimeError("找不到 Node.js，无法启动 Summarize 媒体引擎。")
        return Path(executable)

    def _summarize_command(self, arguments: list[str]) -> list[str]:
        if not self.cli_path.is_file():
            raise RuntimeError(f"Summarize CLI 文件不存在：{self.cli_path}")
        return [str(self._node_path()), str(self.cli_path), *arguments]

    @classmethod
    def _npm_command(cls) -> list[str]:
        node = cls._node_path()
        npm_cli = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if not npm_cli.is_file():
            raise RuntimeError(f"找不到 npm 的直接 Node 入口：{npm_cli}")
        return [str(node), str(npm_cli)]

    @property
    def installed_package_path(self) -> Path:
        return self.runtime_dir / "node_modules" / "@steipete" / "summarize" / "package.json"

    def _settings(self) -> dict[str, Any]:
        if not self.settings_path.is_file():
            return {"version": 1, "provider": "groq"}
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "provider": "groq"}
        return value if isinstance(value, dict) else {"version": 1, "provider": "groq"}

    def _credentials(self) -> dict[str, str]:
        if not self.credentials_path.is_file():
            return {}
        try:
            value = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}

    def provider(self) -> str:
        value = str(self._settings().get("provider") or "groq").casefold()
        return value if value in TRANSCRIPTION_PROVIDERS else "groq"

    def has_api_key(self, provider: str | None = None) -> bool:
        selected = str(provider or self.provider()).casefold()
        if selected not in TRANSCRIPTION_PROVIDERS:
            return False
        env_name = TRANSCRIPTION_PROVIDERS[selected][1]
        if not env_name:
            return False
        return bool(os.environ.get(env_name, "").strip() or self._credentials().get(selected, ""))

    def configured_providers(self) -> list[str]:
        return [
            key
            for key, (_label, env_name) in TRANSCRIPTION_PROVIDERS.items()
            if env_name and self.has_api_key(key)
        ]

    def configure(self, provider: str, api_key: str | None = None) -> dict[str, Any]:
        selected = str(provider or "").strip().casefold()
        if selected not in TRANSCRIPTION_PROVIDERS:
            raise ValueError(f"不支持的转写服务：{provider}")
        settings = self._settings()
        settings.update({"version": 1, "provider": selected})
        _atomic_json(self.settings_path, settings)
        if api_key is not None and api_key.strip():
            credentials = self._credentials()
            credentials[selected] = _dpapi_protect(api_key.strip())
            _atomic_json(self.credentials_path, credentials)
        return self.status()

    def clear_api_key(self, provider: str | None = None) -> dict[str, Any]:
        selected = str(provider or self.provider()).casefold()
        credentials = self._credentials()
        if selected in credentials:
            credentials.pop(selected)
            _atomic_json(self.credentials_path, credentials)
        return self.status()

    def _api_key(self) -> tuple[str, str]:
        selected = self.provider()
        env_name = TRANSCRIPTION_PROVIDERS[selected][1]
        from_environment = os.environ.get(env_name, "").strip()
        if from_environment:
            return env_name, from_environment
        encoded = self._credentials().get(selected, "")
        if encoded:
            try:
                return env_name, _dpapi_unprotect(encoded)
            except (ValueError, OSError):
                raise RuntimeError("已保存的网课转写 API Key 无法解密，请重新填写。") from None
        raise RuntimeError(
            f"尚未配置{TRANSCRIPTION_PROVIDERS[selected][0]} API Key。"
            "请在“网课讲义”页面点击“设置转写 API”。"
        )

    def _api_environment(self) -> dict[str, str]:
        credentials = self._credentials()
        resolved: dict[str, str] = {}
        for provider, (_label, env_name) in TRANSCRIPTION_PROVIDERS.items():
            if not env_name:
                continue
            from_environment = os.environ.get(env_name, "").strip()
            if from_environment:
                resolved[env_name] = from_environment
                continue
            encoded = credentials.get(provider, "")
            if not encoded:
                continue
            try:
                resolved[env_name] = _dpapi_unprotect(encoded)
            except (ValueError, OSError):
                raise RuntimeError(
                    f"已保存的{TRANSCRIPTION_PROVIDERS[provider][0]} API Key 无法解密，请重新填写。"
                ) from None
        if not resolved:
            raise RuntimeError("尚未配置 Groq Whisper API Key。")
        return resolved

    def preflight_transcription_access(self) -> dict[str, str]:
        """Check Groq credentials without uploading media."""
        selected = self.provider()
        label = TRANSCRIPTION_PROVIDERS[selected][0]
        _env_name, api_key = self._api_key()
        request = urllib.request.Request(
            GROQ_MODELS_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": GROQ_HTTP_USER_AGENT,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if int(getattr(response, "status", 200)) >= 400:
                    raise RuntimeError(f"HTTP {int(response.status)}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            if error.code == 403 and "1010" in detail:
                message = (
                    f"{label} 请求被 Groq 边缘防护拒绝（HTTP 403/code 1010），"
                    "不是音频内容错误；请检查网络出口或稍后重试。"
                )
            else:
                message = f"{label} 认证预检失败（HTTP {error.code}）：{detail}"
            raise RuntimeError(
                message
            ) from error
        except (OSError, urllib.error.URLError) as error:
            raise RuntimeError(f"{label} 认证预检无法连接：{error}") from error
        return {"provider": selected, "provider_label": label}

    def quick_transcription_bypass_proxy(
        self, emit: Callable[[str], None] | None = None
    ) -> bool:
        """Choose the faster reachable Groq route for the quick transcript workflow."""
        if self._quick_groq_bypass_proxy is not None:
            return self._quick_groq_bypass_proxy
        _env_name, api_key = self._api_key()
        timings: dict[bool, float] = {}
        errors: dict[bool, str] = {}
        for bypass_proxy in (True, False):
            request = urllib.request.Request(
                GROQ_MODELS_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "User-Agent": GROQ_HTTP_USER_AGENT,
                },
                method="GET",
            )
            started = time.monotonic()
            try:
                if bypass_proxy:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    response_context = opener.open(request, timeout=10)
                else:
                    response_context = urllib.request.urlopen(request, timeout=10)
                with response_context as response:
                    if int(getattr(response, "status", 200)) >= 400:
                        raise RuntimeError(f"HTTP {int(response.status)}")
                timings[bypass_proxy] = time.monotonic() - started
            except (OSError, RuntimeError, urllib.error.URLError) as error:
                errors[bypass_proxy] = str(error)
        if timings:
            selected = min(timings, key=timings.get)
            self._quick_groq_bypass_proxy = selected
            if emit is not None:
                label = "直连" if selected else "系统代理"
                emit(f"Groq 网络探测完成：使用{label}（{timings[selected]:.2f} 秒）。")
            return selected
        if emit is not None:
            emit(
                "Groq 直连和系统代理预检均未成功；将按系统网络设置请求，"
                f"直连：{errors.get(True, '未知')}；代理：{errors.get(False, '未知')}。"
            )
        self._quick_groq_bypass_proxy = False
        return False

    def _wait_for_groq_request_slot(
        self,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        """Reserve one request inside Groq's rolling 20 RPM window."""
        if self.provider() != "groq":
            return
        announced_wait = False
        window = (
            GROQ_WHISPER_RATE_WINDOW_SECONDS
            + GROQ_WHISPER_RATE_WINDOW_CUSHION_SECONDS
        )
        with self._groq_rate_condition:
            while True:
                now = time.monotonic()
                while (
                    self._groq_request_started_at
                    and now - self._groq_request_started_at[0] >= window
                ):
                    self._groq_request_started_at.popleft()
                if len(self._groq_request_started_at) < GROQ_WHISPER_RPM_LIMIT:
                    self._groq_request_started_at.append(now)
                    return
                wait_seconds = max(
                    0.05,
                    window - (now - self._groq_request_started_at[0]),
                )
                if emit is not None and not announced_wait:
                    emit(
                        "Groq Whisper 已达到 20 RPM 滚动窗口；"
                        f"自动等待 {wait_seconds:.1f} 秒后继续转写。"
                    )
                    announced_wait = True
                self._groq_rate_condition.wait(timeout=wait_seconds)

    @staticmethod
    def _groq_rpm_retry_seconds(error: Exception) -> float | None:
        text = str(error or "")
        lowered = text.casefold()
        if "429" not in lowered or not (
            "requests per minute" in lowered or "rpm" in lowered
        ):
            return None
        match = re.search(
            r"(?:try again in|retry[- ]after[^0-9]*)\s*"
            r"(?:(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?\s*)?"
            r"(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?)?",
            lowered,
        )
        if match is not None and (match.group(1) or match.group(2)):
            return (
                float(match.group(1) or 0.0) * 60.0
                + float(match.group(2) or 0.0)
                + GROQ_WHISPER_RATE_WINDOW_CUSHION_SECONDS
            )
        return 5.0

    def status(self) -> dict[str, Any]:
        selected = self.provider()
        installed = self.cli_path.is_file()
        detected_version = ""
        error = ""
        if installed and self.installed_package_path.is_file():
            try:
                package = json.loads(self.installed_package_path.read_text(encoding="utf-8"))
                detected_version = str(package.get("version") or "").strip() if isinstance(package, dict) else ""
                if not detected_version:
                    error = "Summarize package.json 缺少版本号。"
            except (OSError, json.JSONDecodeError) as exc:
                error = str(exc)
        elif installed:
            error = "Summarize 命令存在，但安装包元数据缺失。"
        configured_providers = self.configured_providers()
        manifest: dict[str, Any] = {}
        try:
            loaded = json.loads(self.toolchain_manifest_path.read_text(encoding="utf-8"))
            manifest = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass
        yt_dlp_installed = (
            self.yt_dlp_path.is_file()
            and str(manifest.get("yt_dlp_version") or "") == YT_DLP_VERSION
        )
        ffmpeg_installed = (
            self.ffmpeg_path.is_file()
            and self.ffprobe_path.is_file()
            and bool(str(manifest.get("ffmpeg_version") or "").strip())
        )
        scenedetect_installed = (
            (self.scenedetect_runtime_dir / "scenedetect" / "__init__.py").is_file()
            and str(manifest.get("scenedetect_version") or "") == PYSCENEDETECT_VERSION
        )
        claude_real_video_installed = (
            (self.scenedetect_runtime_dir / "claude_real_video" / "core.py").is_file()
            and str(manifest.get("claude_real_video_version") or "")
            == CLAUDE_REAL_VIDEO_VERSION
        )
        support_tools_installed = (
            yt_dlp_installed
            and ffmpeg_installed
            and scenedetect_installed
            and claude_real_video_installed
        )
        return {
            "installed": installed and detected_version == SUMMARIZE_VERSION,
            "command_exists": installed,
            "version": detected_version,
            "expected_version": SUMMARIZE_VERSION,
            "upstream_commit": SUMMARIZE_COMMIT,
            "provider": selected,
            "provider_label": TRANSCRIPTION_PROVIDERS[selected][0],
            "api_key_configured": self.has_api_key(selected),
            "transcription_ready": self.has_api_key(selected),
            "any_api_key_configured": bool(configured_providers),
            "configured_providers": configured_providers,
            "configured_provider_labels": [
                TRANSCRIPTION_PROVIDERS[key][0] for key in configured_providers
            ],
            "provider_limits": (
                {
                    "requests_per_minute": GROQ_WHISPER_RPM_LIMIT,
                    "requests_per_day": GROQ_WHISPER_RPD_LIMIT,
                    "audio_seconds_per_hour": GROQ_WHISPER_AUDIO_SECONDS_PER_HOUR_LIMIT,
                    "audio_seconds_per_day": GROQ_WHISPER_AUDIO_SECONDS_PER_DAY_LIMIT,
                    "rolling_rpm_limiter": True,
                    "same_chunk_rpm_retries": GROQ_WHISPER_MAX_RPM_RETRIES,
                }
                if selected == "groq"
                else {}
            ),
            "runtime_dir": str(self.runtime_dir),
            "tool_runtime_dir": str(self.tool_runtime_dir),
            "vendor_package": str(self.package_path),
            "support_tools_installed": support_tools_installed,
            "all_installed": installed and detected_version == SUMMARIZE_VERSION and support_tools_installed,
            "yt_dlp_installed": yt_dlp_installed,
            "yt_dlp_version": str(manifest.get("yt_dlp_version") or ""),
            "ffmpeg_installed": ffmpeg_installed,
            "ffmpeg_version": str(manifest.get("ffmpeg_version") or ""),
            "scenedetect_installed": scenedetect_installed,
            "scenedetect_version": str(manifest.get("scenedetect_version") or ""),
            "claude_real_video_installed": claude_real_video_installed,
            "claude_real_video_version": str(
                manifest.get("claude_real_video_version") or ""
            ),
            "error": error,
        }

    @staticmethod
    def _download(
        url: str,
        target: Path,
        emit: Callable[[str], None],
        *,
        label: str,
    ) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "MathProblemBank/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                reported = -1
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    received += len(block)
                    percent = int(received * 100 / total) if total else 0
                    if percent // 10 != reported // 10:
                        reported = percent
                        emit(f"正在下载 {label}：{percent}%" if total else f"正在下载 {label}……")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, target)
        return target

    @staticmethod
    def _run_checked(
        command: list[str],
        *,
        timeout: int = 180,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=environment,
            **hidden_subprocess_options(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-3000:]
            raise RuntimeError(f"媒体工具执行失败（{Path(command[0]).name}）：{detail}")
        return completed

    def _install_summarize(self, emit: Callable[[str], None]) -> None:
        current = self.status()
        if current["installed"]:
            emit(f"Summarize {current['version']} 已就绪。")
            return
        emit = emit or (lambda _message: None)
        if not self.package_path.is_file():
            raise FileNotFoundError(f"缺少固定版本的 Summarize 安装包：{self.package_path}")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        command = [
            *self._npm_command(),
            "install",
            "--prefix",
            str(self.runtime_dir),
            str(self.package_path),
            "--omit=dev",
            "--no-audit",
            "--no-fund",
        ]
        emit(f"正在安装 Summarize {SUMMARIZE_VERSION} 到 {self.runtime_dir}……")
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_options(),
        )
        if completed.returncode != 0:
            detail = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()[-3000:]
            raise RuntimeError("Summarize 安装失败：\n" + detail)
        result = self.status()
        if not result["installed"]:
            raise RuntimeError("Summarize 安装结束，但版本写后回读失败。")
        emit(f"Summarize {result['version']} 已安装并验证。")

    def _verify_yt_dlp_checksum(self, executable: Path) -> None:
        try:
            request = urllib.request.Request(
                YT_DLP_CHECKSUMS_URL, headers={"User-Agent": "MathProblemBank/1.0"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                checksums = response.read().decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError):
            return
        match = re.search(r"^([0-9a-fA-F]{64})\s+\*?yt-dlp\.exe\s*$", checksums, re.MULTILINE)
        if not match:
            return
        import hashlib

        actual = hashlib.sha256(executable.read_bytes()).hexdigest()
        if actual.casefold() != match.group(1).casefold():
            executable.unlink(missing_ok=True)
            raise RuntimeError("yt-dlp 官方 SHA-256 校验失败，已删除不可信下载文件。")

    def _install_support_tools(self, emit: Callable[[str], None]) -> None:
        self.tool_runtime_dir.mkdir(parents=True, exist_ok=True)
        yt_version = ""
        if self.yt_dlp_path.is_file():
            try:
                yt_version = self._run_checked(
                    [str(self.yt_dlp_path), "--version"], timeout=30
                ).stdout.strip()
            except Exception:
                self.yt_dlp_path.unlink(missing_ok=True)
        if yt_version != YT_DLP_VERSION:
            self.yt_dlp_path.unlink(missing_ok=True)
            self._download(YT_DLP_URL, self.yt_dlp_path, emit, label=f"yt-dlp {YT_DLP_VERSION}")
            self._verify_yt_dlp_checksum(self.yt_dlp_path)
            yt_version = self._run_checked(
                [str(self.yt_dlp_path), "--version"], timeout=30
            ).stdout.strip()
        if yt_version != YT_DLP_VERSION:
            raise RuntimeError(f"yt-dlp 版本不一致：期望 {YT_DLP_VERSION}，实际 {yt_version or '未知'}")
        emit(f"yt-dlp {yt_version} 已就绪。")

        ffmpeg_line = ""
        if self.ffmpeg_path.is_file() and self.ffprobe_path.is_file():
            try:
                ffmpeg_line = self._run_checked(
                    [str(self.ffmpeg_path), "-version"], timeout=30
                ).stdout.splitlines()[0]
                self._run_checked([str(self.ffprobe_path), "-version"], timeout=30)
            except Exception:
                self.ffmpeg_path.unlink(missing_ok=True)
                self.ffprobe_path.unlink(missing_ok=True)
                ffmpeg_line = ""
        if not ffmpeg_line:
            with tempfile.TemporaryDirectory(prefix="ffmpeg-install-", dir=self.tool_runtime_dir) as temporary_text:
                temporary = Path(temporary_text)
                archive = self._download(
                    FFMPEG_ARCHIVE_URL,
                    temporary / "ffmpeg.zip",
                    emit,
                    label="FFmpeg/ffprobe",
                )
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(temporary / "unpacked")
                ffmpeg = next((path for path in (temporary / "unpacked").rglob("ffmpeg.exe")), None)
                ffprobe = next((path for path in (temporary / "unpacked").rglob("ffprobe.exe")), None)
                if ffmpeg is None or ffprobe is None:
                    raise RuntimeError("FFmpeg 压缩包中缺少 ffmpeg.exe 或 ffprobe.exe。")
                shutil.copy2(ffmpeg, self.ffmpeg_path)
                shutil.copy2(ffprobe, self.ffprobe_path)
            ffmpeg_line = self._run_checked(
                [str(self.ffmpeg_path), "-version"], timeout=30
            ).stdout.splitlines()[0]
            self._run_checked([str(self.ffprobe_path), "-version"], timeout=30)
        emit("FFmpeg 与 ffprobe 已就绪。")

        scene_init = self.scenedetect_runtime_dir / "scenedetect" / "__init__.py"
        scene_environment = os.environ.copy()
        scene_environment["PYTHONPATH"] = os.pathsep.join(
            [str(self.scenedetect_runtime_dir), scene_environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        scene_version = ""
        if scene_init.is_file():
            try:
                scene_version = self._run_checked(
                    [sys.executable, "-c", "import scenedetect; print(scenedetect.__version__)"],
                    timeout=30,
                    environment=scene_environment,
                ).stdout.strip()
            except Exception:
                scene_version = ""
        if scene_version != PYSCENEDETECT_VERSION:
            emit(
                f"正在安装 PySceneDetect {PYSCENEDETECT_VERSION} 到网课媒体运行时目录："
                f"{self.scenedetect_runtime_dir}"
            )
            self.scenedetect_runtime_dir.mkdir(parents=True, exist_ok=True)
            self._run_checked(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--upgrade",
                    "--target",
                    str(self.scenedetect_runtime_dir),
                    f"scenedetect[opencv-headless]=={PYSCENEDETECT_VERSION}",
                ],
                timeout=600,
            )
            scene_version = self._run_checked(
                [sys.executable, "-c", "import scenedetect; print(scenedetect.__version__)"],
                timeout=30,
                environment=scene_environment,
            ).stdout.strip()
        if scene_version != PYSCENEDETECT_VERSION:
            raise RuntimeError(
                f"PySceneDetect 版本不一致：期望 {PYSCENEDETECT_VERSION}，实际 {scene_version or '未知'}"
            )
        emit(f"PySceneDetect {scene_version} 已就绪。")

        crv_core = self.scenedetect_runtime_dir / "claude_real_video" / "core.py"
        crv_version = ""
        if crv_core.is_file():
            try:
                crv_version = self._run_checked(
                    [
                        sys.executable,
                        "-c",
                        "import importlib.metadata; print(importlib.metadata.version('claude-real-video'))",
                    ],
                    timeout=30,
                    environment=scene_environment,
                ).stdout.strip()
            except Exception:
                crv_version = ""
        if crv_version != CLAUDE_REAL_VIDEO_VERSION:
            emit(
                f"正在安装 claude-real-video {CLAUDE_REAL_VIDEO_VERSION} 的本地屏幕去重器……"
            )
            self.scenedetect_runtime_dir.mkdir(parents=True, exist_ok=True)
            self._run_checked(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--upgrade",
                    "--no-deps",
                    "--target",
                    str(self.scenedetect_runtime_dir),
                    f"claude-real-video=={CLAUDE_REAL_VIDEO_VERSION}",
                ],
                timeout=600,
            )
            crv_version = self._run_checked(
                [
                    sys.executable,
                    "-c",
                    "import importlib.metadata; print(importlib.metadata.version('claude-real-video'))",
                ],
                timeout=30,
                environment=scene_environment,
            ).stdout.strip()
        if crv_version != CLAUDE_REAL_VIDEO_VERSION:
            raise RuntimeError(
                "claude-real-video 版本不一致："
                f"期望 {CLAUDE_REAL_VIDEO_VERSION}，实际 {crv_version or '未知'}"
            )
        emit(f"claude-real-video {crv_version} 屏幕去重器已就绪。")
        _atomic_json(
            self.toolchain_manifest_path,
            {
                "version": 2,
                "yt_dlp_version": yt_version,
                "ffmpeg_version": ffmpeg_line,
                "ffmpeg_build": FFMPEG_BUILD_LABEL,
                "scenedetect_version": scene_version,
                "claude_real_video_version": crv_version,
                "sources": {
                    "yt_dlp": YT_DLP_URL,
                    "ffmpeg": FFMPEG_ARCHIVE_URL,
                    "scenedetect": "https://github.com/Breakthrough/PySceneDetect",
                    "claude_real_video": (
                        "https://github.com/HUANGCHIHHUNGLeo/claude-real-video"
                    ),
                },
            },
        )

    def install(self, emit: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Install and verify the complete course-media toolchain on D:."""
        emit = emit or (lambda _message: None)
        self._install_summarize(emit)
        self._install_support_tools(emit)
        result = self.status()
        if not result["all_installed"]:
            raise RuntimeError("媒体工具安装结束，但完整状态写后回读失败。")
        emit("网课媒体工具链已全部安装并验证。")
        return result

    def _tool_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            [str(self.tool_runtime_dir), environment.get("PATH", "")]
        ).rstrip(os.pathsep)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(self.scenedetect_runtime_dir), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        return environment

    def probe_media(self, path: Path) -> dict[str, Any]:
        source = Path(path).resolve()
        payload = self._run_checked(
            [
                str(self.ffprobe_path),
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,codec_name,width,height",
                "-of",
                "json",
                str(source),
            ],
            timeout=60,
        )
        try:
            value = json.loads(payload.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ffprobe 没有返回有效媒体信息：{source.name}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"ffprobe 返回结构异常：{source.name}")
        duration = float((value.get("format") or {}).get("duration") or 0)
        streams = value.get("streams") if isinstance(value.get("streams"), list) else []
        if duration <= 0 or not streams:
            raise RuntimeError(f"录制分块损坏或为空：{source.name}")
        return {"duration": duration, "streams": streams, "source": str(source)}

    def normalize_audio(self, path: Path, target: Path) -> Path:
        source = Path(path).resolve()
        output = Path(target).resolve()
        if output.is_file() and output.stat().st_size > 1024:
            return output
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".tmp" + output.suffix)
        self._run_checked(
            [
                str(self.ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "flac",
                str(temporary),
            ],
            timeout=180,
        )
        os.replace(temporary, output)
        self.probe_media(output)
        return output

    def audio_signal_metrics(self, path: Path) -> dict[str, float]:
        """Measure audio level without invoking ASR.

        FFmpeg's volumedetect filter reports in stderr even on success. A peak
        at or below -80 dBFS is treated as digital silence; this deliberately
        leaves very quiet but potentially recoverable speech to the ASR model.
        """
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"待检测媒体不存在：{source}")
        completed = self._run_checked(
            [
                str(self.ffmpeg_path),
                "-hide_banner",
                "-nostats",
                "-i",
                str(source),
                "-vn",
                "-af",
                "volumedetect",
                "-f",
                "null",
                os.devnull,
            ],
            timeout=90,
        )
        output = "\n".join((completed.stdout or "", completed.stderr or ""))

        def level(name: str) -> float:
            match = re.search(
                rf"\b{re.escape(name)}\s*:\s*(-?inf|[-+]?\d+(?:\.\d+)?)\s*dB",
                output,
                flags=re.IGNORECASE,
            )
            if not match:
                raise RuntimeError(
                    f"FFmpeg 未返回可验证的音量指标（缺少 {name}）：{source.name}"
                )
            raw = match.group(1).casefold()
            return float("-inf") if raw == "-inf" else float(raw)

        return {
            "mean_volume_db": level("mean_volume"),
            "max_volume_db": level("max_volume"),
        }

    def analyze_visual_content(self, path: Path) -> dict[str, Any]:
        source = Path(path).resolve()
        worker = ROOT_DIR / "shared" / "scripts" / "online_course_scene_worker.py"
        completed = self._run_checked(
            [sys.executable, str(worker), str(source)],
            timeout=180,
            environment=self._tool_environment(),
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("PySceneDetect 没有返回有效场景数据。") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("视觉识别程序返回了未知数据结构。")
        rows = payload.get("scenes")
        scenes = [
            (max(0.0, float(row["start"])), max(0.0, float(row["end"])))
            for row in (rows or [])
            if isinstance(row, dict) and float(row.get("end") or 0) > float(row.get("start") or 0)
        ]
        candidates = [
            dict(row)
            for row in (payload.get("board_candidates") or [])
            if isinstance(row, dict) and float(row.get("time") or 0) >= 0
        ]
        return {
            "scenes": scenes,
            "board_candidates": candidates,
            "analysis_version": int(payload.get("analysis_version") or 0),
            "duration": float(payload.get("duration") or 0),
            "decoded_frame_count": int(payload.get("decoded_frame_count") or 0),
        }

    def detect_scenes(self, path: Path) -> list[tuple[float, float]]:
        return list(self.analyze_visual_content(path)["scenes"])

    def deduplicate_screen_frames(
        self,
        paths: list[Path],
        times: list[float],
        *,
        threshold: float = 8.0,
        window: int = 4,
    ) -> dict[str, Any]:
        """Run claude-real-video's settled-local comparator on safe copies."""
        if len(paths) != len(times):
            raise ValueError("屏幕去重输入的图片与时间数量不一致。")
        if not paths:
            return {
                "source_count": 0,
                "kept_count": 0,
                "kept_indices": [],
                "records": [],
                "upstream": "claude-real-video",
                "upstream_version": CLAUDE_REAL_VIDEO_VERSION,
            }
        if not bool(self.status().get("claude_real_video_installed")):
            raise RuntimeError(
                "claude-real-video 本地屏幕去重器尚未安装，请先更新网课媒体工具。"
            )
        worker = ROOT_DIR / "shared" / "scripts" / "online_course_crv_worker.py"
        self.tool_runtime_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="crv-screen-dedup-", dir=self.tool_runtime_dir
        ) as temporary_text:
            temporary = Path(temporary_text)
            frames_dir = temporary / "frames"
            dropped_dir = temporary / "dropped"
            frames_dir.mkdir(parents=True)
            for index, source in enumerate(paths):
                path = Path(source)
                if not path.is_file():
                    raise FileNotFoundError(path)
                shutil.copy2(path, frames_dir / f"candidate_{index:06d}.jpg")
            request_path = temporary / "request.json"
            _atomic_json(
                request_path,
                {
                    "frames_dir": str(frames_dir),
                    "dropped_dir": str(dropped_dir),
                    "times": [float(value) for value in times],
                    "threshold": float(threshold),
                    "window": int(window),
                },
            )
            completed = self._run_checked(
                [sys.executable, str(worker), str(request_path)],
                timeout=180,
                environment=self._tool_environment(),
            )
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    "claude-real-video 屏幕去重器没有返回有效结果。"
                ) from error
        records = [
            dict(item)
            for item in payload.get("records") or []
            if isinstance(item, dict)
        ]
        unused_indices = set(range(len(times)))
        kept_indices: list[int] = []
        for item in records:
            if not bool(item.get("kept")) or item.get("t") is None:
                continue
            timestamp = float(item["t"])
            if not unused_indices:
                break
            index = min(unused_indices, key=lambda value: abs(times[value] - timestamp))
            if abs(float(times[index]) - timestamp) > 0.001:
                raise RuntimeError(
                    "claude-real-video 返回的关键帧时间无法映射到原截图："
                    f"{timestamp:.3f} 秒。"
                )
            unused_indices.remove(index)
            kept_indices.append(index)
        kept_indices.sort()
        kept_count = int(payload.get("kept_count") or 0)
        if kept_count != len(kept_indices):
            raise RuntimeError(
                "claude-real-video 屏幕去重写后回读失败："
                f"报告 {kept_count} 张，实际映射 {len(kept_indices)} 张。"
            )
        return {
            "source_count": len(paths),
            "kept_count": kept_count,
            "kept_indices": kept_indices,
            "records": records,
            "threshold": float(threshold),
            "window": int(window),
            "upstream": "claude-real-video",
            "upstream_version": CLAUDE_REAL_VIDEO_VERSION,
        }

    def extract_frame(self, path: Path, seconds: float, target: Path) -> Path:
        source = Path(path).resolve()
        output = Path(target).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".tmp" + output.suffix)
        temporary.unlink(missing_ok=True)
        self._run_checked(
            [
                str(self.ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{max(0.0, float(seconds)):.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-c:v",
                "mjpeg",
                "-threads",
                "1",
                "-strict",
                "unofficial",
                "-pix_fmt",
                "yuvj420p",
                "-q:v",
                "2",
                str(temporary),
            ],
            timeout=90,
        )
        if not temporary.is_file() or temporary.stat().st_size < 1024:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(
                f"FFmpeg 未在 {max(0.0, float(seconds)):.3f}s 产出场景截图：{source.name}"
            )
        os.replace(temporary, output)
        if not output.is_file() or output.stat().st_size < 1024:
            raise RuntimeError(f"FFmpeg 场景截图写后回读失败：{source.name}")
        return output

    def extract_frames(
        self,
        path: Path,
        requests: list[tuple[float, Path]],
    ) -> list[Path]:
        """Extract several timestamps from one chunk in one hidden FFmpeg process."""
        source = Path(path).resolve()
        pending = [
            (max(0.0, float(seconds)), Path(target).resolve())
            for seconds, target in requests
        ]
        if not pending:
            return []
        for _seconds, output in pending:
            output.parent.mkdir(parents=True, exist_ok=True)
        temporary = [
            output.with_name(output.stem + ".tmp" + output.suffix)
            for _seconds, output in pending
        ]
        for output in temporary:
            output.unlink(missing_ok=True)
        command = [str(self.ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y"]
        for seconds, _output in pending:
            command.extend(["-ss", f"{seconds:.3f}", "-i", str(source)])
        for input_index, output in enumerate(temporary):
            command.extend(
                [
                    "-map",
                    f"{input_index}:v:0",
                    "-frames:v",
                    "1",
                    "-c:v",
                    "mjpeg",
                    "-threads",
                    "1",
                    "-strict",
                    "unofficial",
                    "-pix_fmt",
                    "yuvj420p",
                    "-q:v",
                    "2",
                    str(output),
                ]
            )
        batch_error: Exception | None = None
        try:
            self._run_checked(command, timeout=max(90, 45 * len(pending)))
        except Exception as error:
            batch_error = error
        missing: list[tuple[float, Path]] = []
        try:
            for (seconds, target), output in zip(pending, temporary):
                if not output.is_file() or output.stat().st_size < 1024:
                    missing.append((seconds, target))
                    output.unlink(missing_ok=True)
                    continue
                os.replace(output, target)
                if not target.is_file() or target.stat().st_size < 1024:
                    missing.append((seconds, target))

            retry_errors: list[str] = []
            for seconds, target in missing:
                target.unlink(missing_ok=True)
                last_error: Exception | None = None
                for offset in (0.25, 0.5, 1.0):
                    try:
                        self.extract_frame(source, max(0.0, seconds - offset), target)
                        last_error = None
                        break
                    except Exception as error:
                        last_error = error
                if last_error is not None:
                    retry_errors.append(f"{seconds:.3f}s: {last_error}")
            unresolved = [
                target
                for _seconds, target in pending
                if not target.is_file() or target.stat().st_size < 1024
            ]
            if retry_errors or unresolved:
                batch_note = f"；批量命令错误：{batch_error}" if batch_error else ""
                raise RuntimeError(
                    f"FFmpeg 批量截图仍缺少 {len(unresolved)} 个输出："
                    + ", ".join(path.name for path in unresolved)
                    + batch_note
                    + ("；重试错误：" + " | ".join(retry_errors) if retry_errors else "")
                )
        except Exception:
            for output in temporary:
                output.unlink(missing_ok=True)
            raise
        finally:
            for output in temporary:
                output.unlink(missing_ok=True)
        return [target for _seconds, target in pending]

    def prepare_chunk(self, path: Path, derived_dir: Path) -> PreparedMediaChunk:
        source = Path(path).resolve()
        destination = Path(derived_dir).resolve()
        audio = destination / "audio" / f"{source.stem}.flac"
        manifest = destination / "scenes" / f"{source.stem}.json"
        scene_ranges: list[tuple[float, float]] = []
        board_candidates: list[dict[str, Any]] = []
        board_analysis_version = 0
        stored_duration = 0.0
        manifest_valid = False
        if manifest.is_file():
            try:
                stored = json.loads(manifest.read_text(encoding="utf-8"))
                scene_ranges = [(float(row[0]), float(row[1])) for row in stored.get("scenes", [])]
                board_candidates = [
                    dict(row)
                    for row in (stored.get("board_candidates") or [])
                    if isinstance(row, dict)
                ]
                board_analysis_version = int(stored.get("board_analysis_version") or 0)
                stored_duration = float(stored.get("duration") or 0)
                manifest_valid = (
                    isinstance(stored.get("scenes"), list)
                    and isinstance(stored.get("board_candidates"), list)
                    and board_analysis_version >= 2
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                scene_ranges = []
                board_candidates = []
                board_analysis_version = 0
                stored_duration = 0.0
                manifest_valid = False
        if manifest_valid and audio.is_file() and audio.stat().st_size > 1024:
            duration = stored_duration or (
                max(end for _start, end in scene_ranges) if scene_ranges else 1.0
            )
            return PreparedMediaChunk(
                source=source,
                audio_path=audio,
                duration=duration,
                scene_ranges=scene_ranges,
                board_candidates=board_candidates,
                board_analysis_version=board_analysis_version,
            )
        info = self.probe_media(source)
        audio = self.normalize_audio(source, audio)
        analysis = self.analyze_visual_content(source)
        scene_ranges = list(analysis["scenes"])
        board_candidates = list(analysis["board_candidates"])
        board_analysis_version = int(analysis["analysis_version"])
        _atomic_json(
            manifest,
            {
                "source": str(source),
                "duration": float(info["duration"]),
                "scenes": scene_ranges,
                "board_analysis_version": board_analysis_version,
                "decoded_frame_count": int(analysis["decoded_frame_count"]),
                "board_candidates": board_candidates,
            },
        )
        return PreparedMediaChunk(
            source=source,
            audio_path=audio,
            duration=float(info["duration"]),
            scene_ranges=scene_ranges,
            board_candidates=board_candidates,
            board_analysis_version=board_analysis_version,
        )

    def source_metadata(self, url: str) -> dict[str, Any]:
        completed = self._run_checked(
            [
                str(self.yt_dlp_path),
                "--ignore-config",
                "--no-playlist",
                "--skip-download",
                "--no-warnings",
                "--dump-single-json",
                str(url),
            ],
            timeout=120,
            environment=self._tool_environment(),
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("yt-dlp 没有返回有效的视频元数据。") from exc
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _preferred_subtitle_language(metadata: dict[str, Any]) -> str:
        available: set[str] = set()
        for field in ("subtitles", "automatic_captions"):
            value = metadata.get(field)
            if isinstance(value, dict):
                available.update(str(key) for key in value if str(key).casefold() != "live_chat")
        priorities = (
            "zh-Hans",
            "zh-CN",
            "zh",
            "ai-zh",
            "zh-Hant",
            "zh-TW",
            "en-orig",
            "en",
        )
        for preferred in priorities:
            match = next((item for item in available if item.casefold() == preferred.casefold()), "")
            if match:
                return match
        return sorted(available)[0] if available else ""

    @staticmethod
    def _vtt_seconds(value: str) -> float:
        pieces = value.strip().replace(",", ".").split(":")
        if len(pieces) == 2:
            hours = 0.0
            minutes, seconds = pieces
        elif len(pieces) == 3:
            hours, minutes, seconds = pieces
        else:
            raise ValueError(value)
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)

    @classmethod
    def _parse_vtt(cls, path: Path) -> list[dict[str, Any]]:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        segments: list[dict[str, Any]] = []
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if "-->" not in line:
                index += 1
                continue
            timing = line.split("-->", 1)
            start_text = timing[0].strip().split()[0]
            end_text = timing[1].strip().split()[0]
            index += 1
            text_lines: list[str] = []
            while index < len(lines) and lines[index].strip():
                text_lines.append(lines[index].strip())
                index += 1
            text = re.sub(r"<[^>]+>", "", " ".join(text_lines))
            text = re.sub(r"\s+", " ", text).strip()
            if text and (not segments or text != segments[-1]["text"]):
                try:
                    start = cls._vtt_seconds(start_text)
                    end = cls._vtt_seconds(end_text)
                except ValueError:
                    continue
                segments.append(
                    {
                        "startMs": round(start * 1000),
                        "endMs": round(max(start, end) * 1000),
                        "text": text,
                    }
                )
        return segments

    def extract_source_captions(
        self,
        url: str,
        output_dir: Path,
        emit: Callable[[str], None] | None = None,
    ) -> MediaTranscript | None:
        """Fetch only an existing subtitle track; never download the video."""
        emit = emit or (lambda _message: None)
        metadata = self.source_metadata(url)
        language = self._preferred_subtitle_language(metadata)
        if not language:
            return None
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        for old in destination.glob("source_subtitle*.vtt"):
            old.unlink(missing_ok=True)
        emit(f"正在通过 yt-dlp 提取现有字幕轨道（{language}）……")
        self._run_checked(
            [
                str(self.yt_dlp_path),
                "--ignore-config",
                "--no-playlist",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                language,
                "--sub-format",
                "vtt/best",
                "--paths",
                str(destination),
                "--output",
                "source_subtitle.%(ext)s",
                str(url),
            ],
            timeout=180,
            environment=self._tool_environment(),
        )
        subtitle = next(destination.glob("source_subtitle*.vtt"), None)
        if subtitle is None:
            return None
        segments = self._parse_vtt(subtitle)
        if not segments:
            return None
        text = "\n".join(str(row["text"]) for row in segments)
        return MediaTranscript(text=text, provider="yt-dlp", source=str(url), segments=segments)

    def _run_json(
        self,
        arguments: list[str],
        *,
        timeout: int,
        require_api_key: bool,
        allow_text_output: bool = False,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        status = self.status()
        if not status["installed"]:
            raise RuntimeError("Summarize 媒体引擎尚未安装或版本不一致，请先点击“安装/检查媒体引擎”。")
        environment = os.environ.copy()
        if require_api_key:
            environment.update(self._api_environment())
        environment["NO_COLOR"] = "1"
        if emit:
            emit("正在调用 Summarize 媒体提取引擎……")
        completed = subprocess.run(
            self._summarize_command(arguments),
            text=True,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=environment,
            **hidden_subprocess_options(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-3000:]
            if not detail:
                detail = "未输出诊断信息"
            raise RuntimeError(
                f"Summarize 处理失败（退出码 {completed.returncode}，{detail}）。"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            plain_text = (completed.stdout or "").strip()
            if allow_text_output and plain_text:
                return {
                    "extracted": {
                        "content": plain_text,
                        "transcriptSource": "summarize",
                    }
                }
            raise RuntimeError("Summarize 没有返回有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Summarize 返回了未知数据结构。")
        return payload

    @staticmethod
    def _transcript_from_payload(payload: dict[str, Any], source: str) -> MediaTranscript:
        extracted = payload.get("extracted")
        if not isinstance(extracted, dict):
            raise RuntimeError("Summarize 没有返回可用的 extracted 结果。")
        raw_segments = extracted.get("transcriptSegments")
        segments = [dict(item) for item in raw_segments if isinstance(item, dict)] if isinstance(raw_segments, list) else []
        text = "\n".join(str(item.get("text") or "").strip() for item in segments if str(item.get("text") or "").strip())
        if not text:
            text = str(extracted.get("transcriptTimedText") or "").strip()
        if not text:
            content = str(extracted.get("content") or "").strip()
            text = content.removeprefix("Transcript:").strip()
        if not text:
            raise RuntimeError("Summarize 没有提取到任何字幕或语音文本。")
        provider = str(extracted.get("transcriptionProvider") or extracted.get("transcriptSource") or "unknown")
        return MediaTranscript(text=text, provider=provider, source=source, segments=segments)

    def extract_youtube_captions(
        self, url: str, emit: Callable[[str], None] | None = None
    ) -> MediaTranscript:
        payload = self._run_json(
            [url, "--extract", "--json", "--timestamps", "--youtube", "web", "--timeout", "90s", "--no-color"],
            timeout=120,
            require_api_key=False,
            emit=emit,
        )
        return self._transcript_from_payload(payload, url)

    def _groq_audio_request(
        self,
        source: Path,
        api_key: str,
        *,
        model: str = GROQ_AUDIO_TRANSCRIPT_MODEL,
        language: str = "",
        prompt: str = "",
        word_timestamps: bool = False,
        bypass_proxy: bool = False,
    ) -> MediaTranscript:
        """Send one recording chunk directly to Groq's OpenAI-compatible ASR endpoint."""
        boundary = "----MathProblemBank" + uuid.uuid4().hex
        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        body = bytearray()
        fields = [
            ("model", str(model or GROQ_AUDIO_TRANSCRIPT_MODEL)),
            ("response_format", "verbose_json"),
            ("temperature", "0"),
            ("timestamp_granularities[]", "segment"),
        ]
        if word_timestamps:
            fields.append(("timestamp_granularities[]", "word"))
        if language:
            fields.append(("language", language))
        if prompt:
            fields.append(("prompt", prompt[:1200]))
        for name, value in fields:
            body.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n".encode("utf-8")
            )
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{source.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8")
        )
        body.extend(source.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
        request = urllib.request.Request(
            GROQ_AUDIO_TRANSCRIPT_ENDPOINT,
            data=bytes(body),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": GROQ_HTTP_USER_AGENT,
            },
            method="POST",
        )
        if bypass_proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            response_context = opener.open(request, timeout=360)
        else:
            response_context = urllib.request.urlopen(request, timeout=360)
        with response_context as response:
            raw = response.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("Groq Whisper returned invalid JSON.") from error
        if not isinstance(payload, dict):
            raise RuntimeError("Groq Whisper returned an invalid transcription payload.")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise NoSpeechDetectedError("Groq Whisper returned no speech for this audio chunk.")
        segments: list[dict[str, Any]] = []
        for item in payload.get("segments") or []:
            if not isinstance(item, dict):
                continue
            segment_text = str(item.get("text") or "").strip()
            if not segment_text:
                continue
            start_ms = max(0, int(round(float(item.get("start") or 0.0) * 1000)))
            end_ms = max(start_ms, int(round(float(item.get("end") or 0.0) * 1000)))
            segment = {
                "startMs": start_ms,
                "endMs": end_ms,
                "text": segment_text,
            }
            for source_key, target_key in (
                ("avg_logprob", "avgLogprob"),
                ("compression_ratio", "compressionRatio"),
                ("no_speech_prob", "noSpeechProb"),
            ):
                value = item.get(source_key)
                if isinstance(value, (int, float)):
                    segment[target_key] = float(value)
            segments.append(segment)
        words: list[dict[str, Any]] = []
        for item in payload.get("words") or []:
            if not isinstance(item, dict) or not str(item.get("word") or "").strip():
                continue
            words.append(
                {
                    "startMs": max(0, int(round(float(item.get("start") or 0.0) * 1000))),
                    "endMs": max(0, int(round(float(item.get("end") or 0.0) * 1000))),
                    "word": str(item.get("word") or "").strip(),
                }
            )
        if words:
            for segment in segments:
                segment["words"] = [
                    word
                    for word in words
                    if int(word["endMs"]) > int(segment["startMs"])
                    and int(word["startMs"]) < int(segment["endMs"])
                ]
        return MediaTranscript(
            text=text,
            provider="groq",
            source=str(source),
            segments=segments,
        )

    def transcribe_file(
        self,
        path: Path,
        emit: Callable[[str], None] | None = None,
        *,
        model: str = GROQ_AUDIO_TRANSCRIPT_MODEL,
        language: str = "",
        prompt: str = "",
        word_timestamps: bool = False,
        bypass_proxy: bool = False,
        max_retries: int = GROQ_WHISPER_MAX_RPM_RETRIES,
        retry_jitter_seconds: float = 0.0,
    ) -> MediaTranscript:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"待转写媒体不存在：{source}")
        if self.ffmpeg_path.is_file():
            metrics = self.audio_signal_metrics(source)
            if metrics["max_volume_db"] <= -80.0:
                raise NoSpeechDetectedError(
                    "录制音轨为数字静音"
                    f"（峰值 {metrics['max_volume_db']:.1f} dBFS），"
                    "未调用转写 API，避免生成幻听字幕。"
                )
        duration_seconds: float | None = None
        if self.ffprobe_path.is_file():
            try:
                duration_seconds = float(self.probe_media(source).get("duration") or 0.0)
            except (OSError, RuntimeError, ValueError):
                duration_seconds = None
        _env_name, api_key = self._api_key()
        if emit is not None:
            emit(f"正在直接通过 Groq Whisper 转写：{source.name}")
        retry_limit = max(0, min(int(max_retries), GROQ_WHISPER_MAX_RPM_RETRIES))
        server_attempts: list[dict[str, Any]] = []
        for attempt in range(retry_limit + 1):
            self._wait_for_groq_request_slot(emit)
            retry_error: Exception | None = None
            attempt_started = time.monotonic()
            try:
                if (
                    model == GROQ_AUDIO_TRANSCRIPT_MODEL
                    and not language
                    and not prompt
                    and not word_timestamps
                    and not bypass_proxy
                    and max_retries == GROQ_WHISPER_MAX_RPM_RETRIES
                    and retry_jitter_seconds <= 0
                ):
                    return self._groq_audio_request(source, api_key)
                return self._groq_audio_request(
                    source,
                    api_key,
                    model=model,
                    language=language,
                    prompt=prompt,
                    word_timestamps=word_timestamps,
                    bypass_proxy=bypass_proxy,
                )
            except urllib.error.HTTPError as error:
                retry_error = error
                detail = error.read().decode("utf-8", errors="replace")[:4000]
                elapsed_seconds = time.monotonic() - attempt_started
                if error.code in (401, 403):
                    raise RuntimeError(
                        f"Groq Whisper authentication/access failed (HTTP {error.code}); "
                        "check the Groq API key and network access. No fallback is used."
                    ) from error
                if error.code == 429:
                    if any(
                        marker in detail.casefold()
                        for marker in ("audio seconds", "per day", "per hour", "rpd", "asd")
                    ):
                        raise RuntimeError(
                            f"Groq Whisper quota exhausted (HTTP 429): {detail}"
                        ) from error
                    retry_seconds = self._groq_rpm_retry_seconds(
                        error
                    ) or min(60.0, 2.0 ** attempt)
                    retry_reason = "Groq Whisper rate limited the request"
                elif 500 <= error.code <= 599:
                    headers = error.headers
                    request_id = ""
                    if headers is not None:
                        request_id = str(
                            headers.get("x-request-id")
                            or headers.get("request-id")
                            or headers.get("cf-ray")
                            or ""
                        ).strip()
                    server_attempts.append(
                        {
                            "attempt": len(server_attempts) + 1,
                            "http_status": int(error.code),
                            "response_body": detail,
                            "request_id": request_id,
                            "elapsed_seconds": round(elapsed_seconds, 3),
                        }
                    )
                    if len(server_attempts) >= min(3, retry_limit + 1):
                        diagnostics = {
                            "recorded_at_unix": time.time(),
                            "chunk": source.name,
                            "source": str(source),
                            "duration_seconds": duration_seconds,
                            "file_size": source.stat().st_size,
                            "route": "direct" if bypass_proxy else "system_proxy",
                            "model": str(model or GROQ_AUDIO_TRANSCRIPT_MODEL),
                            "language": language,
                            "response_format": "verbose_json",
                            "prompt_length": len(prompt),
                            "word_timestamps": bool(word_timestamps),
                            "attempts": server_attempts,
                        }
                        diagnostic_path = source.with_name(
                            f"{source.stem}.groq_error.json"
                        )
                        _atomic_json(diagnostic_path, diagnostics)
                        if emit is not None:
                            emit(
                                f"{source.name} 连续 {len(server_attempts)} 次收到 Groq "
                                f"HTTP {error.code}；已保存诊断并交由递归拆分恢复。"
                            )
                        raise GroqWhisperServerError(
                            status_code=error.code,
                            detail=detail,
                            diagnostics=diagnostics,
                        ) from error
                    retry_seconds = min(60.0, 2.0 ** attempt)
                    retry_reason = f"Groq Whisper server error (HTTP {error.code})"
                else:
                    raise RuntimeError(
                        f"Groq Whisper transcription failed (HTTP {error.code}): {detail}"
                    ) from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                retry_error = error
                retry_seconds = min(60.0, 2.0 ** attempt)
                retry_reason = "Unable to reach Groq Whisper"
            except NoSpeechDetectedError:
                raise
            except RuntimeError:
                raise
            if attempt >= retry_limit:
                raise RuntimeError(
                    f"{retry_reason}; retry limit reached: {retry_error}"
                ) from retry_error
            retry_seconds += random.uniform(0.0, max(0.0, float(retry_jitter_seconds)))
            if emit is not None:
                emit(
                    f"{retry_reason}; retrying this audio chunk in "
                    f"{retry_seconds:.1f}s ({attempt + 1}/{retry_limit})."
                )
            time.sleep(retry_seconds)
