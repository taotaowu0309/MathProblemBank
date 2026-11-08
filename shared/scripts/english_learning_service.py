from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unicodedata
import uuid
import urllib.parse
import urllib.request
import wave
import zipfile
from contextlib import closing
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree

from shared.scripts.application_paths import APP_PATHS, resolve_stored_path

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENGLISH_ROOT = APP_PATHS.workspace_root / "English"
DEFAULT_DATABASE = DEFAULT_ENGLISH_ROOT / "data" / "english_learning.db"
DEFAULT_RAYDU_SERIES_ROOT = APP_PATHS.user_data_root / "external" / "RayDuSeries"
DEFAULT_RAYDU_AUDIO_ROOT = DEFAULT_RAYDU_SERIES_ROOT / "RayDu_Vocabulary" / "audio"
# The Ray Du book projects sit beside the checkout on both machines
# (see application_paths.resolve_stored_path).
SIBLING_RAYDU_SERIES_ROOT = (
    DEFAULT_RAYDU_SERIES_ROOT if APP_PATHS.public_release
    else ROOT_DIR.parent / "EnglishProjects" / "RayDuSeries"
)
RAYDU_GRAMMAR_EXERCISES_CODE = "XY-GRAMMAR-EXERCISES"
QUESTION_CATEGORIES = ("grammar", "reading", "writing")
RAYDU_READING_CODE = "XY-READING"
RAYDU_READING_TITLE = "旋元佑英文阅读"
RAYDU_READING_PREFIX = "XYRD"
READING_METHOD_CACHE_DIR = APP_PATHS.cache_dir / "raydu_reading"
READING_METHOD_CACHE_VERSION = 1
# 《旋元佑英文阅读通》: parsed from the publisher's PDF (no TeX source), merged into
# the reading category of the bank and into the 阅读方法 page.
RAYDU_HIGHPOINT_CODE = "XY-READING-HIGHPOINT"
RAYDU_HIGHPOINT_PREFIX = "XYHP"
RAYDU_HIGHPOINT_CACHE_VERSION = 1
READING_METHOD_PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS english_reading_method_progress (
    section_key TEXT PRIMARY KEY,
    read_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

RAYDU_WRITING_CODE = "XY-WRITING"
RAYDU_WRITING_TITLE = "旋元佑英文写作"
RAYDU_WRITING_PREFIX = "XYWR"
WRITING_METHOD_CACHE_DIR = APP_PATHS.cache_dir / "raydu_writing"
WRITING_METHOD_CACHE_VERSION = 1
WRITING_METHOD_PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS english_writing_method_progress (
    section_key TEXT PRIMARY KEY,
    read_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

RAYDU_GRAMMAR_CODE = "XY-GRAMMAR"
RAYDU_GRAMMAR_TITLE = "旋元佑文法"
RAYDU_GRAMMAR_EXERCISES_TITLE = "旋元佑文法解题"
GRAMMAR_METHOD_CACHE_DIR = APP_PATHS.cache_dir / "raydu_grammar"
GRAMMAR_METHOD_CACHE_VERSION = 1
GRAMMAR_METHOD_PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS english_grammar_method_progress (
    section_key TEXT PRIMARY KEY,
    read_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Columns added to english_question_bank_items for structured questions.
QUESTION_BANK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("question_type", "TEXT NOT NULL DEFAULT ''"),
    ("topic", "TEXT NOT NULL DEFAULT ''"),
    ("chapter_number", "INTEGER"),
    ("source_label", "TEXT NOT NULL DEFAULT ''"),
    ("group_id", "INTEGER REFERENCES english_question_groups(id) ON DELETE SET NULL"),
    ("sort_key", "INTEGER NOT NULL DEFAULT 0"),
    ("content_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("content_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("exam", "TEXT NOT NULL DEFAULT ''"),
    ("exam_year", "INTEGER"),
)


class EnglishSpeechService:
    """Natural speech playback for English learning text.

    The primary backend is the free Microsoft Edge neural voice bridge exposed
    by the optional ``edge-tts`` package.  It does not require an API key or
    paid OpenAI credits; Windows System.Speech remains a local fallback.
    Text is split at sentence and paragraph boundaries so a full article can
    be read continuously without sending one oversized request.
    """

    EDGE_VOICE = "en-US-AvaNeural"
    MAX_CHARS_PER_CHUNK = 900

    def __init__(self, root: Path | str | None = None) -> None:
        base = Path(root or DEFAULT_ENGLISH_ROOT)
        self.cache_dir = base / "audio" / "tts_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._speed = 1.0

    @staticmethod
    def split_text(text: str, *, mode: str = "document", max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not value:
            return []
        max_chars = max(int(max_chars), 80)

        def chunk_unit(unit: str) -> list[str]:
            pieces: list[str] = []
            remaining = unit.strip()
            while len(remaining) > max_chars:
                cut = remaining.rfind(" ", 0, max_chars + 1)
                if cut < 1:
                    cut = max_chars
                pieces.append(remaining[:cut].strip())
                remaining = remaining[cut:].strip()
            if remaining:
                pieces.append(remaining)
            return pieces

        if mode in {"word", "sentence"}:
            return [re.sub(r"\s+", " ", value).strip()]
        paragraphs = [
            re.sub(r"\s+", " ", part).strip()
            for part in re.split(r"\n\s*\n+", value)
            if part.strip()
        ]
        if mode == "paragraph":
            # Keep paragraph boundaries visible to the learner; a short
            # paragraph must never be silently merged with the next one.
            chunks: list[str] = []
            for paragraph in paragraphs or [re.sub(r"\s+", " ", value).strip()]:
                chunks.extend(chunk_unit(paragraph))
            return [chunk.strip() for chunk in chunks if chunk.strip()]
        else:
            source_units = []
            for paragraph in paragraphs or [re.sub(r"\s+", " ", value).strip()]:
                source_units.extend(
                    sentence.strip()
                    for sentence in re.split(r"(?<=[.!?])\s+", paragraph)
                    if sentence.strip()
                )
        chunks: list[str] = []
        current = ""
        for unit in source_units:
            pieces = chunk_unit(unit)
            for piece in pieces:
                if current and len(current) + 1 + len(piece) > max_chars:
                    chunks.append(current)
                    current = ""
                current = f"{current} {piece}".strip()
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _edge_playback_command() -> list[str] | None:
        """Find the edge-playback executable installed in either Python scope."""
        if sys.platform == "darwin":
            try:
                import edge_tts
                if edge_tts:
                    return [sys.executable, "-m", "shared.scripts.macos_speech"]
            except ImportError:
                pass
        candidates = [
            shutil.which("edge-playback"),
            str(Path(sysconfig.get_path("scripts") or "") / "edge-playback.exe"),
        ]
        try:
            import site

            user_scripts = (
                Path(site.getuserbase())
                / f"Python{sys.version_info.major}{sys.version_info.minor}"
                / "Scripts"
                / "edge-playback.exe"
            )
            candidates.append(str(user_scripts))
        except Exception:
            pass
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return [candidate]
        try:
            import edge_playback  # noqa: F401

            if edge_playback:
                return [sys.executable, "-c", "from edge_playback.__main__ import _main; _main()"]
        except ImportError:
            return None
        return None

    @staticmethod
    def _edge_environment() -> dict[str, str]:
        """Make user-local edge-tts scripts visible to edge-playback's child."""
        environment = os.environ.copy()
        script_dirs = {
            str(Path(sysconfig.get_path("scripts") or "").resolve()),
        }
        try:
            import site

            script_dirs.add(
                str(
                    (
                        Path(site.getuserbase())
                        / f"Python{sys.version_info.major}{sys.version_info.minor}"
                        / "Scripts"
                    ).resolve()
                )
            )
        except Exception:
            pass
        current_path = environment.get("PATH", "")
        environment["PATH"] = os.pathsep.join(
            [path for path in script_dirs if path and path not in current_path.split(os.pathsep)]
            + [current_path]
        )
        return environment

    @classmethod
    def status(cls) -> dict[str, Any]:
        edge_command = cls._edge_playback_command()
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if sys.platform == "darwin" and not edge_command and shutil.which("say"):
            return {
                "available": True, "backend": "macOS System Speech", "voice": "system default",
                "network_required": False, "natural_voice": False, "paid": False,
            }
        if edge_command:
            return {
                "available": True,
                "backend": "免费 Edge Neural TTS（在线）",
                "voice": cls.EDGE_VOICE,
                "network_required": True,
                "natural_voice": True,
                "paid": False,
            }
        return {
            "available": bool(os.name == "nt" and powershell),
            "backend": "Windows System.Speech fallback" if powershell and os.name == "nt" else "none",
            "voice": "",
            "network_required": False,
            "natural_voice": False,
            "paid": False,
        }

    def stop(self) -> None:
        self._stop_event.set()
        self._resume_event.set()
        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            try:
                self._suspend_windows_process(process, False)
                process.terminate()
            except OSError:
                pass
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except (AttributeError, ImportError, RuntimeError):
            pass

    @staticmethod
    def _suspend_windows_process(process: subprocess.Popen[str], suspend: bool) -> bool:
        """Suspend/resume the Edge playback process without adding psutil."""
        if process.poll() is not None:
            return False
        if os.name != "nt":
            try:
                import signal
                action = signal.SIGSTOP if suspend else signal.SIGCONT
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, action)
                else:
                    os.kill(process.pid, action)
                return True
            except OSError:
                return False
        try:
            import ctypes

            process_suspend_resume = 0x0800
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            handle = kernel32.OpenProcess(process_suspend_resume, False, process.pid)
            if not handle:
                return False
            try:
                function = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
                return int(function(handle)) == 0
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError):
            return False

    def pause(self) -> bool:
        self._resume_event.clear()
        return True

    def resume(self) -> bool:
        with self._process_lock:
            process = self._active_process
        resumed = process is None or self._suspend_windows_process(process, False)
        self._resume_event.set()
        return resumed

    def set_speed(self, speed: float) -> float:
        self._speed = min(max(float(speed), 0.6), 1.4)
        return self._speed

    def speed(self) -> float:
        return self._speed

    @staticmethod
    def _wav_duration(path: Path) -> float:
        """Return a conservative duration for an uncompressed WAV file."""
        try:
            with wave.open(str(path), "rb") as stream:
                frame_rate = max(int(stream.getframerate()), 1)
                return max(float(stream.getnframes()) / frame_rate, 0.05)
        except (OSError, EOFError, wave.Error):
            return 0.0

    def play(
        self,
        text: str,
        *,
        mode: str = "document",
        on_status: Any = None,
        on_done: Any = None,
    ) -> threading.Thread:
        chunks = self.split_text(text, mode=mode)
        if not chunks:
            raise ValueError("没有可朗读的英文文本。")
        self.stop()
        self._stop_event = threading.Event()
        self._resume_event.set()
        stop_event = self._stop_event

        def wait_process(process, deadline, errors):
            paused = False
            try:
                while process.poll() is None:
                    if stop_event.wait(0.1):
                        self._suspend_windows_process(process, False)
                        process.terminate()
                        return
                    if time.monotonic() >= deadline:
                        process.terminate()
                        raise TimeoutError("朗读超时，网络或语音服务未及时响应。")
                    should_pause = not self._resume_event.is_set()
                    if should_pause != paused:
                        self._suspend_windows_process(process, should_pause)
                        paused = should_pause
                if process.returncode and not stop_event.is_set():
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 600))
                    detail = errors.read().decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"语音播放器退出码 {process.returncode}：{detail or '网络或语音服务不可用'}")
            finally:
                if process.poll() is None:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)

        def worker() -> None:
            deadline = time.monotonic() + max(240, min(3600, len(text) / 8 + 180))
            process = None
            status = self.status()
            try:
                import winsound
            except ImportError:
                winsound = None  # type: ignore[assignment]
            errors = tempfile.TemporaryFile()
            try:
                for index, chunk in enumerate(chunks, start=1):
                    if stop_event.is_set():
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("朗读超时。")
                    while not self._resume_event.wait(0.1):
                        if stop_event.is_set():
                            return
                        if time.monotonic() >= deadline:
                            raise TimeoutError("朗读等待超时。")
                    if callable(on_status):
                        on_status(f"正在朗读第 {index} / {len(chunks)} 段")
                    if status["backend"].startswith("免费 Edge"):
                        from shared.scripts.edge_tts_support import ensure_edge_tts

                        problem = ensure_edge_tts()
                        if problem:
                            raise RuntimeError(problem)
                        command = self._edge_playback_command()
                        if command is None:
                            raise RuntimeError("免费 Edge TTS 组件不可用。")
                        process = subprocess.Popen(
                            command + [
                                "--voice", self.EDGE_VOICE,
                                f"--rate={round((self._speed - 1.0) * 100):+d}%",
                                "--text", chunk,
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=errors,
                            env=self._edge_environment(),
                            start_new_session=(sys.platform == "darwin"),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        with self._process_lock:
                            self._active_process = process
                        wait_process(process, deadline, errors)
                    elif status["backend"] == "macOS System Speech":
                        process = subprocess.Popen(
                            ["/usr/bin/say", "-r", str(round(175 * self._speed))],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                            text=True,
                        )
                        with self._process_lock:
                            self._active_process = process
                        assert process.stdin is not None
                        process.stdin.write(chunk)
                        process.stdin.close()
                        wait_process(process, deadline, errors)
                    else:
                        powershell = shutil.which("powershell") or shutil.which("pwsh")
                        if os.name != "nt" or not powershell:
                            raise RuntimeError("没有可用的免费朗读后端；请安装 edge-tts 或启用 Windows 语音。")
                        script = (
                            "Add-Type -AssemblyName System.Speech; "
                            "$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new(); "
                            "$speaker.Speak([Console]::In.ReadToEnd()); $speaker.Dispose()"
                        )
                        process = subprocess.Popen(
                            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                            text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        assert process.stdin is not None
                        process.stdin.write(chunk)
                        process.stdin.close()
                        with self._process_lock:
                            self._active_process = process
                        wait_process(process, deadline, errors)
            except Exception as error:
                if callable(on_status):
                    on_status(f"朗读失败：{error}")
            finally:
                errors.close()
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None
                if callable(on_done):
                    on_done()

        self._worker = threading.Thread(target=worker, name=f"english-tts-{uuid.uuid4().hex[:8]}", daemon=True)
        self._worker.start()
        return self._worker


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
    {
        "material_code": "XY-READING-HIGHPOINT",
        "title": "旋元佑英文閱讀通",
        "english_title": "Xuan Yu-You Highpoint English Reading",
        "role": "reading_training",
        "published_on": "",
        "isbn": "",
        "page_count_hint": 430,
        "has_audio": 0,
    },
    {
        "material_code": "XY-GRE-VOCABULARY",
        "title": "旋元佑GRE字彙通",
        "english_title": "Xuan Yu-You GRE Vocabulary",
        "role": "vocabulary",
        "published_on": "2019-03-01",
        "isbn": "9789869685276",
        "page_count_hint": 493,
        "has_audio": 0,
    },
)


# The placement library points to complete, human-written public files that are
# shipped locally with the project.  The source URL is retained only for
# attribution/licence details.  Every curated entry is intended to be at least
# 1,000 English words and is opened through the same selectable PDF reader as
# every other English material.
EXTENSIVE_READING_TOPICS: tuple[dict[str, Any], ...] = (
    {
        "key": "technology_and_everyday_life",
        "title": "科技与日常生活",
        "description": "技术如何改变工作、注意力、城市和人与人的连接。",
        "articles": (
            {"key": "tech_low", "difficulty": "低", "title": "Flatland: A Romance of Many Dimensions", "word_count": 1384, "source_key": "gutenberg", "gutenberg_id": 201, "author": "Edwin A. Abbott", "local_sample_path": "English/extensive_reading/placement_samples/tech_low.txt"},
            {"key": "tech_mid", "difficulty": "中", "title": "Erewhon", "word_count": 1582, "source_key": "gutenberg", "gutenberg_id": 1906, "author": "Samuel Butler", "local_sample_path": "English/extensive_reading/placement_samples/tech_mid.txt"},
            {"key": "tech_high", "difficulty": "高", "title": "The World Set Free", "word_count": 1869, "source_key": "gutenberg", "gutenberg_id": 1059, "author": "H. G. Wells", "local_sample_path": "English/extensive_reading/placement_samples/tech_high.txt"},
        ),
    },
    {
        "key": "nature_and_environment",
        "title": "自然与环境",
        "description": "气候、城市生态、动物与人类如何共同生活。",
        "articles": (
            {"key": "nature_low", "difficulty": "低", "title": "My First Summer in the Sierra", "word_count": 1234, "source_key": "gutenberg", "gutenberg_id": 32540, "author": "John Muir", "local_sample_path": "English/extensive_reading/placement_samples/nature_low.txt"},
            {"key": "nature_mid", "difficulty": "中", "title": "The Voyage of the Beagle", "word_count": 1553, "source_key": "gutenberg", "gutenberg_id": 944, "author": "Charles Darwin", "local_sample_path": "English/extensive_reading/placement_samples/nature_mid.txt"},
            {"key": "nature_high", "difficulty": "高", "title": "On the Origin of Species", "word_count": 1821, "source_key": "gutenberg", "gutenberg_id": 1228, "author": "Charles Darwin", "local_sample_path": "English/extensive_reading/placement_samples/nature_high.txt"},
        ),
    },
    {
        "key": "history_and_culture",
        "title": "历史与文化",
        "description": "城市记忆、日常物件、博物馆与跨文化故事。",
        "articles": (
            {"key": "culture_low", "difficulty": "低", "title": "The Story of My Life", "word_count": 1207, "source_key": "gutenberg", "gutenberg_id": 2397, "author": "Helen Keller", "local_sample_path": "English/extensive_reading/placement_samples/culture_low.txt"},
            {"key": "culture_mid", "difficulty": "中", "title": "Culture and Anarchy", "word_count": 1858, "source_key": "gutenberg", "gutenberg_id": 4212, "author": "Matthew Arnold", "local_sample_path": "English/extensive_reading/placement_samples/culture_mid.txt"},
            {"key": "culture_high", "difficulty": "高", "title": "The Education of Henry Adams", "word_count": 1923, "source_key": "gutenberg", "gutenberg_id": 2044, "author": "Henry Adams", "local_sample_path": "English/extensive_reading/placement_samples/culture_high.txt"},
        ),
    },
    {
        "key": "mind_and_health",
        "title": "心理与健康",
        "description": "睡眠、注意力、习惯与身心健康的公共议题。",
        "articles": (
            {"key": "mind_low", "difficulty": "低", "title": "The Story of My Life", "word_count": 1207, "source_key": "gutenberg", "gutenberg_id": 2397, "author": "Helen Keller", "local_sample_path": "English/extensive_reading/placement_samples/mind_low.txt"},
            {"key": "mind_mid", "difficulty": "中", "title": "The World I Live In", "word_count": 1549, "source_key": "gutenberg", "gutenberg_id": 27683, "author": "Helen Keller", "local_sample_path": "English/extensive_reading/placement_samples/mind_mid.txt"},
            {"key": "mind_high", "difficulty": "高", "title": "Little Masterpieces of Science: Mind", "word_count": 1975, "source_key": "gutenberg", "gutenberg_id": 64286, "author": "Various authors", "local_sample_path": "English/extensive_reading/placement_samples/mind_high.txt"},
        ),
    },
    {
        "key": "arts_and_literature",
        "title": "艺术与文学",
        "description": "书籍、叙事、创作和作品如何跨越时间与语言。",
        "articles": (
            {"key": "arts_low", "difficulty": "低", "title": "A Room with a View", "word_count": 1281, "source_key": "gutenberg", "gutenberg_id": 2641, "author": "E. M. Forster", "local_sample_path": "English/extensive_reading/placement_samples/arts_low.txt"},
            {"key": "arts_mid", "difficulty": "中", "title": "Far from the Madding Crowd", "word_count": 1620, "source_key": "gutenberg", "gutenberg_id": 27, "author": "Thomas Hardy", "local_sample_path": "English/extensive_reading/placement_samples/arts_mid.txt"},
            {"key": "arts_high", "difficulty": "高", "title": "Bleak House", "word_count": 2163, "source_key": "gutenberg", "gutenberg_id": 1023, "author": "Charles Dickens", "local_sample_path": "English/extensive_reading/placement_samples/arts_high.txt"},
        ),
    },
    {
        "key": "society_and_work",
        "title": "社会与工作",
        "description": "社区、公共空间、职业变化和普通人的选择。",
        "articles": (
            {"key": "society_low", "difficulty": "低", "title": "Looking Backward, 2000 to 1887", "word_count": 1217, "source_key": "gutenberg", "gutenberg_id": 624, "author": "Edward Bellamy", "local_sample_path": "English/extensive_reading/placement_samples/society_low.txt"},
            {"key": "society_mid", "difficulty": "中", "title": "The Souls of Black Folk", "word_count": 1543, "source_key": "gutenberg", "gutenberg_id": 408, "author": "W. E. B. Du Bois", "local_sample_path": "English/extensive_reading/placement_samples/society_mid.txt"},
            {"key": "society_high", "difficulty": "高", "title": "The Condition of the Working-Class in England", "word_count": 1984, "source_key": "gutenberg", "gutenberg_id": 17306, "author": "Friedrich Engels", "local_sample_path": "English/extensive_reading/placement_samples/society_high.txt"},
        ),
    },
)


# Curated public starting points for attribution and future user imports.  The
# bundled placement samples above are already local and do not require opening
# any of these sites before reading.
EXTENSIVE_READING_SOURCES: tuple[dict[str, str], ...] = (
    {
        "key": "voa",
        "title": "VOA Learning English",
        "description": "分级新闻、专题文章与音频，适合先建立不查词的广读习惯。",
        "url": "https://learningenglish.voanews.com/",
        "license_note": "公开学习网站；下载时保留原始来源信息。",
    },
    {
        "key": "gutenberg",
        "title": "Project Gutenberg",
        "description": "公共领域电子书，题材很广，也能找到部分有声书。",
        "url": "https://www.gutenberg.org/",
        "license_note": "优先选择明确标注公共领域的作品。",
    },
    {
        "key": "librivox",
        "title": "LibriVox",
        "description": "志愿者朗读的公共领域有声书，适合为完整文章配音频。",
        "url": "https://librivox.org/",
        "license_note": "确认具体版本和所在地区的公共领域状态。",
    },
    {
        "key": "archive",
        "title": "Internet Archive",
        "description": "可按主题查找书籍、文本和音频，适合扩展兴趣范围。",
        "url": "https://archive.org/details/texts",
        "license_note": "每个项目的下载与再使用权限不同，请逐项核对。",
    },
    {
        "key": "nasa",
        "title": "NASA Science Stories",
        "description": "面向大众的太空、地球与科技文章，专业性适中。",
        "url": "https://science.nasa.gov/stories/",
        "license_note": "下载前查看页面的版权和媒体使用说明。",
    },
    {
        "key": "owid",
        "title": "Our World in Data",
        "description": "全球议题的图表与说明文章，适合有兴趣的知识型广读。",
        "url": "https://ourworldindata.org/",
        "license_note": "按页面说明署名；第三方数据另看其许可。",
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


RAYDU_INITIAL_BOOK_CODES: tuple[str, ...] = (
    "XY-GRAMMAR",
    "XY-VOCABULARY",
    "XY-GRAMMAR-EXERCISES",
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

CREATE TABLE IF NOT EXISTS english_grammar_question_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exercise_id INTEGER NOT NULL UNIQUE,
    question_text TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL DEFAULT '',
    explanation_text TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    source_line INTEGER,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(exercise_id) REFERENCES english_grammar_exercises(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_english_question_items_source
ON english_grammar_question_items(source_file, source_line);

-- The user-facing English question bank: one table for the three visible
-- categories (grammar, reading, writing).  The questions of 《旋元佑文法解题》
-- live here in the grammar category, parsed from the book's chapter TeX; the
-- older english_grammar_exercises / english_grammar_question_items tables
-- above are kept only for existing references and are no longer displayed.
-- The structured question (styled stem, options, answer, corrected sentence,
-- translation, explanation) is stored in content_json; prompt, answer_text and
-- explanation_text keep plain text for search.  Columns added after the first
-- release are created by EnglishLearningService._migrate_question_bank.
CREATE TABLE IF NOT EXISTS english_question_bank_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK(category IN ('grammar','reading','writing')),
    question_code TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    passage TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL DEFAULT '',
    explanation_text TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_page INTEGER,
    source_line INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, question_code)
);
CREATE INDEX IF NOT EXISTS idx_english_question_bank_category
ON english_question_bank_items(category, id);

-- A reading passage or writing task shared by several questions.
CREATE TABLE IF NOT EXISTS english_question_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK(category IN ('grammar','reading','writing')),
    group_code TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    passage TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    source_title TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_page INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, group_code)
);

CREATE TABLE IF NOT EXISTS english_question_favorites (
    question_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(question_id) REFERENCES english_question_bank_items(id) ON DELETE CASCADE
);

-- One row per answer given in the question bank.  A wrong attempt stays in
-- the mistake view until it is marked resolved (mastered).
CREATE TABLE IF NOT EXISTS english_question_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    response TEXT NOT NULL DEFAULT '',
    correct_answer TEXT NOT NULL DEFAULT '',
    is_correct INTEGER NOT NULL CHECK(is_correct IN (0,1)),
    judged_by TEXT NOT NULL DEFAULT 'auto' CHECK(judged_by IN ('auto','self')),
    mistake_reason TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    FOREIGN KEY(question_id) REFERENCES english_question_bank_items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_english_question_attempts_question
ON english_question_attempts(question_id, attempted_at);

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

CREATE TABLE IF NOT EXISTS english_reading_placement (
    id INTEGER PRIMARY KEY CHECK(id=1),
    passage_key TEXT NOT NULL,
    passage_level TEXT NOT NULL,
    correct_count INTEGER NOT NULL DEFAULT 0,
    question_count INTEGER NOT NULL DEFAULT 0,
    fluency TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    assessed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS english_reading_profile_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    topic_title TEXT NOT NULL DEFAULT '',
    article_key TEXT NOT NULL,
    difficulty TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    understanding TEXT NOT NULL DEFAULT '',
    fluency TEXT NOT NULL DEFAULT '',
    interest TEXT NOT NULL DEFAULT '',
    reflection TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS english_reading_profiles (
    id INTEGER PRIMARY KEY CHECK(id=1),
    run_id TEXT NOT NULL,
    selected_topics_json TEXT NOT NULL DEFAULT '[]',
    inferred_level TEXT NOT NULL DEFAULT '',
    report_path TEXT NOT NULL DEFAULT '',
    report_markdown TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def _tex_preview(value: str) -> str:
    """Turn the small set of textbook macros into readable preview text.

    The PDF remains the authoritative rendering.  This is deliberately a
    conservative text preview for tables and question lists, rather than a
    second TeX renderer.
    """

    text = str(value or "")
    text = re.sub(r"\\syn\s*\{[^{}]*\}\s*\{([^{}]*)\}", r"\1", text)
    # Colour declarations carry only a style name; that name is not readable
    # exercise content and must not leak into the question text.
    text = re.sub(r"\\(?:color|pagecolor)\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\(?:uline|rdcolor|sourcebox|textcolor|blank)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:textbf|textit|emph|textnormal)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\begin\{[^{}]*\}(?:\[[^]]*\])?", "", text)
    text = re.sub(r"\\end\{[^{}]*\}", "", text)
    text = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^]]*\])?", "", text)
    text = text.replace("\\\\", "\n").replace("&", " ")
    text = text.replace("{", "").replace("}", "")
    text = text.replace("~", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _row_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _material_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a material row with stored Windows paths mapped to this machine.

    The synchronised database keeps the paths written on Windows; they are
    translated on read only, so the stored records stay unchanged.
    """
    material = _row_dict(row)
    if material is None:
        return None
    for field in ("source_path", "reading_path"):
        raw = str(material.get(field) or "").strip()
        if raw:
            material[field] = str(resolve_stored_path(raw))
    return material


READING_NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS english_reading_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guide_key TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('selection','sentence','paragraph','article')),
    paragraph INTEGER NOT NULL DEFAULT -1,
    start INTEGER NOT NULL DEFAULT -1,
    paragraph_end INTEGER NOT NULL DEFAULT -1,
    end INTEGER NOT NULL DEFAULT -1,
    quote TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_english_reading_notes_guide ON english_reading_notes(guide_key, paragraph, start);
"""


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
        self.download_dir = self.root / "downloads"
        self.audio_dir = self.root / "audio"
        self.writing_dir = self.root / "writing"
        for directory in (
            self.database_path.parent,
            self.backup_dir,
            self.original_dir,
            self.reading_dir,
            self.download_dir,
            self.audio_dir,
            self.writing_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.speech_service = EnglishSpeechService(self.root)
        self.ensure_schema()

    def _material_result(self, row):
        return _material_dict(row)

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
                existing_tables = {
                    str(row[0]) for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                needs_backup = any(
                    table not in existing_tables
                    for table in (
                        "english_programs",
                        "english_grammar_question_items",
                        "english_question_bank_items",
                        "english_question_groups",
                        "english_question_favorites",
                        "english_question_attempts",
                        "english_reading_placement",
                        "english_reading_profile_entries",
                        "english_reading_profiles",
                    )
                )
                if not needs_backup:
                    present = {
                        str(row[1]) for row in connection.execute(
                            "PRAGMA table_info(english_question_bank_items)"
                        ).fetchall()
                    }
                    needs_backup = any(name not in present for name, _ in QUESTION_BANK_COLUMNS)
        if needs_backup:
            self.backup("schema_migration")
        with self.connect() as connection:
            connection.executescript(ENGLISH_SCHEMA)
            self._migrate_question_bank(connection)
            self._seed_foundation(connection)
            self._integrity(connection)
            connection.commit()

    @staticmethod
    def _migrate_question_bank(connection: sqlite3.Connection) -> None:
        """Add the structured-question columns; old code simply ignores them."""

        present = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(english_question_bank_items)"
            ).fetchall()
        }
        for name, definition in QUESTION_BANK_COLUMNS:
            if name not in present:
                connection.execute(f"ALTER TABLE english_question_bank_items ADD COLUMN {name} {definition}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_english_question_bank_filter "
            "ON english_question_bank_items(category, question_type, chapter_number, sort_key)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_english_question_bank_group "
            "ON english_question_bank_items(group_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_english_question_bank_paper "
            "ON english_question_bank_items(category, exam, exam_year)"
        )

    @staticmethod
    def _seed_row(
        connection: sqlite3.Connection,
        table: str,
        key: dict[str, Any],
        values: dict[str, Any],
        insert_only: dict[str, Any] | None = None,
    ) -> int:
        """Insert or update one built-in row and return its id, writing only when needed.

        Every launch seeds the foundation rows.  A plain upsert or INSERT OR IGNORE
        on these AUTOINCREMENT tables advances sqlite_sequence even when the row
        already exists, so an unchanged database would still be rewritten (on the
        Mac each such rewrite turns the next Syncthing delivery into a conflict
        copy).  Looking the row up first leaves an up-to-date file untouched.
        """

        where = " AND ".join(f"{name}=?" for name in key)
        columns = list(values)
        row = connection.execute(
            f"SELECT id{''.join(f',{name}' for name in columns)} FROM {table} WHERE {where}",
            tuple(key.values()),
        ).fetchone()
        if row is None:
            fields = {**key, **values, **(insert_only or {})}
            cursor = connection.execute(
                f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                tuple(fields.values()),
            )
            return int(cursor.lastrowid)
        if tuple(row[1:]) != tuple(values.values()):
            connection.execute(
                f"UPDATE {table} SET {','.join(f'{name}=?' for name in columns)} WHERE id=?",
                (*values.values(), row[0]),
            )
        return int(row[0])

    def _seed_foundation(self, connection: sqlite3.Connection) -> None:
        program_id = self._seed_row(
            connection,
            "english_programs",
            {"program_code": "XY-FOUNDATION"},
            {
                "title": "旋元佑英语基础",
                "description": "旋元佑七书相互连接的基础课程体系。",
                "pedagogy": "Etymological analysis + sentence-pattern analysis + extensive reading",
            },
        )
        material_ids: dict[str, int] = {}
        for item in XUAN_BOOKS:
            material_ids[item["material_code"]] = self._seed_row(
                connection,
                "english_materials",
                {"material_code": item["material_code"]},
                {
                    "program_id": program_id,
                    "title": item["title"],
                    "english_title": item["english_title"],
                    "author": "旋元佑",
                    "publisher": "眾文圖書",
                    "isbn": item["isbn"],
                    "published_on": item["published_on"],
                    "role": item["role"],
                    "page_count_hint": item["page_count_hint"],
                    "authoritative": 1,
                    "has_audio": item["has_audio"],
                },
                {"material_type": "book"},
            )
        grammar_id = material_ids["XY-GRAMMAR"]
        exercise_id = material_ids["XY-GRAMMAR-EXERCISES"]
        for number, (part_title, title) in enumerate(GRAMMAR_CHAPTERS, 1):
            grammar_chapter, exercise_chapter = (
                self._seed_row(
                    connection,
                    "english_material_chapters",
                    {"material_id": material_id, "chapter_number": number},
                    {"part_title": part_title, "title": title},
                )
                for material_id in (grammar_id, exercise_id)
            )
            for from_chapter, to_chapter, relation in (
                (grammar_chapter, exercise_chapter, "theory_to_exercise"),
                (exercise_chapter, grammar_chapter, "exercise_to_theory"),
            ):
                self._seed_row(
                    connection,
                    "english_chapter_links",
                    {"from_chapter_id": from_chapter, "to_chapter_id": to_chapter, "relation_type": relation},
                    {},
                )

    @staticmethod
    def discover_raydu_initial_sources(
        series_root: Path | str = DEFAULT_RAYDU_SERIES_ROOT,
        audio_root: Path | str | None = None,
    ) -> dict[str, Any]:
        """Locate the three locally maintained Ray Du books without writing files."""
        root = Path(series_root).expanduser().resolve()
        vocabulary_root = root / "RayDu_Vocabulary"
        discovered_audio = Path(audio_root).expanduser().resolve() if audio_root else vocabulary_root / "audio"
        candidates: tuple[dict[str, Any], ...] = (
            {
                "material_code": "XY-GRAMMAR",
                "project_root": root / "RayDu_Grammar",
                "source_candidates": ("raydu-elements-v2-font-final.pdf",),
                "role": "grammar",
            },
            {
                "material_code": "XY-VOCABULARY",
                "project_root": vocabulary_root,
                "source_candidates": ("output/raydu-vocabulary.pdf",),
                "role": "vocabulary",
            },
            {
                "material_code": "XY-GRAMMAR-EXERCISES",
                "project_root": root / "RayDu_Grammar_Exercises",
                "source_candidates": ("旋元佑文法解题.pdf", "main.pdf", "project/output/pdf/旋元佑文法解题.pdf"),
                "role": "grammar_exercises",
            },
        )
        books: list[dict[str, Any]] = []
        for item in candidates:
            project_root = Path(item["project_root"])
            source: Path | None = None
            for relative in item["source_candidates"]:
                candidate = project_root / str(relative)
                if candidate.is_file():
                    source = candidate.resolve()
                    break
            books.append({
                "material_code": str(item["material_code"]),
                "role": str(item["role"]),
                "project_root": str(project_root),
                "source_path": str(source) if source else "",
                "exists": bool(source),
                "size": int(source.stat().st_size) if source else 0,
            })
        return {
            "series_root": str(root),
            "audio_root": str(discovered_audio),
            "audio_exists": discovered_audio.is_dir(),
            "books": books,
            "all_books_found": all(bool(item["exists"]) for item in books),
        }

    @staticmethod
    def parse_raydu_vocabulary_entries(
        series_root: Path | str = DEFAULT_RAYDU_SERIES_ROOT,
    ) -> list[dict[str, Any]]:
        """Read the structured VocabEntry macros from the local source tree.

        This produces a reviewable, read-only preview.  It does not write to
        the shared vocabulary database; the existing vocabulary manager still
        owns all vocabulary writes.
        """

        root = Path(series_root).expanduser().resolve() / "RayDu_Vocabulary" / "chapters"
        if not root.is_dir():
            return []
        pos_map = {
            "名": "n.", "名词": "n.", "动": "v.", "动词": "v.",
            "形": "adj.", "形容词": "adj.", "副": "adv.", "副词": "adv.",
            "介": "prep.", "介词": "prep.", "连": "conj.", "连词": "conj.",
            "代": "pron.", "代词": "pron.", "片": "phr.", "片语": "phr.",
        }
        entries: list[dict[str, Any]] = []
        entry_pattern = re.compile(
            r"\\begin\{VocabEntry\}\{([^{}]+)\}\{([^{}]*)\}\{([^{}]*)\}(.*?)"
            r"\\end\{VocabEntry\}",
            flags=re.S,
        )
        sense_pattern = re.compile(r"\\VocabSense\{([^{}]*)\}\{([^{}]*)\}")
        morph_pattern = re.compile(r"\\VocabMorph\{([^{}]*)\}\{([^{}]*)\}")
        example_pattern = re.compile(
            r"\\VocabExample\{([^{}]*)\}\{([^{}]*)\}\{([^{}]*)\}\{([^{}]*)\}"
        )
        for source_file in sorted(root.glob("*.tex")):
            text = source_file.read_text(encoding="utf-8", errors="replace")
            for match in entry_pattern.finditer(text):
                term = re.sub(r"\s+", " ", match.group(1)).strip()
                pronunciation = re.sub(r"\s+", " ", match.group(2)).strip()
                body = match.group(4)
                senses: dict[str, list[str]] = {}
                for raw_pos, raw_definition in sense_pattern.findall(body):
                    part_of_speech = pos_map.get(raw_pos.strip(), raw_pos.strip())
                    definition = _tex_preview(raw_definition)
                    if definition:
                        senses.setdefault(part_of_speech, []).append(definition)
                if not term or not senses:
                    continue
                morphs = [
                    f"{_tex_preview(left)} = {_tex_preview(right)}"
                    for left, right in morph_pattern.findall(body)
                ]
                examples = [
                    f"{_tex_preview(en)} / {_tex_preview(zh)}"
                    for _term, _number, en, zh in example_pattern.findall(body)
                    if _tex_preview(en)
                ]
                note_parts: list[str] = []
                if morphs:
                    note_parts.append("字源：" + "；".join(dict.fromkeys(morphs)))
                if examples:
                    note_parts.append("例句：" + "；".join(examples[:3]))
                source_line = text.count("\n", 0, match.start()) + 1
                for part_of_speech, definitions in senses.items():
                    entries.append({
                        "term": term,
                        "part_of_speech": part_of_speech,
                        "definition": "；".join(dict.fromkeys(definitions)),
                        "pronunciation": pronunciation,
                        "note": "；".join(note_parts),
                        "source": str(source_file),
                        "source_file": str(source_file),
                        "source_line": source_line,
                        "entry_kind": "phrase" if " " in term else "word",
                    })
        return entries

    @staticmethod
    def raydu_vocabulary_preview(
        series_root: Path | str = DEFAULT_RAYDU_SERIES_ROOT,
        *,
        keyword: str = "",
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        rows = EnglishLearningService.parse_raydu_vocabulary_entries(series_root)
        needle = str(keyword or "").strip().casefold()
        if needle:
            rows = [
                row for row in rows
                if needle in str(row.get("term") or "").casefold()
                or needle in str(row.get("definition") or "").casefold()
                or needle in str(row.get("note") or "").casefold()
            ]
        return rows[: max(1, min(int(limit), 2000))]

    def import_raydu_vocabulary(
        self,
        series_root: Path | str = DEFAULT_RAYDU_SERIES_ROOT,
    ) -> dict[str, Any]:
        """Import the structured vocabulary source through VocabularyManager."""

        from shared.scripts.vocabulary_manager import VocabularyManager

        raw_entries = self.parse_raydu_vocabulary_entries(series_root)
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for item in raw_entries:
            key = (str(item["term"]).casefold(), str(item["part_of_speech"]).casefold())
            current = merged.get(key)
            if current is None:
                merged[key] = dict(item)
                continue
            for field in ("definition", "note"):
                values = [
                    str(current.get(field) or "").strip(),
                    str(item.get(field) or "").strip(),
                ]
                current[field] = "；".join(dict.fromkeys(value for value in values if value))
        entries = list(merged.values())
        if not entries:
            raise FileNotFoundError("词汇书章节 TeX 中没有找到 VocabEntry。")
        manager = VocabularyManager.for_workspace("english", root_dir=self.root.parent)
        result = manager.import_entries(
            entries,
            merge_definitions=True,
            max_entries=max(500, len(entries)),
        )
        result.update({
            "source_root": str(Path(series_root).expanduser().resolve()),
            "source_entries": len(raw_entries),
            "unique_entries": len(entries),
        })
        return result

    @staticmethod
    def parse_raydu_grammar_exercises(
        series_root: Path | str = DEFAULT_RAYDU_SERIES_ROOT,
    ) -> list[dict[str, Any]]:
        root = Path(series_root).expanduser().resolve() / "RayDu_Grammar_Exercises" / "chapters"
        if not root.is_dir():
            return []
        heading_pattern = re.compile(
            r"\\bfseries\s*(\d+)\.\\quad\s*\\sourcebox\{([^}]*)\}"
        )
        parsed: list[dict[str, Any]] = []
        for source_file in sorted(root.glob("*.tex")):
            chapter_match = re.match(r"(\d+)-", source_file.name)
            if chapter_match is None:
                continue
            chapter_number = int(chapter_match.group(1))
            text = source_file.read_text(encoding="utf-8", errors="replace")
            headings = list(heading_pattern.finditer(text))
            for index, heading in enumerate(headings):
                block_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
                block = text[heading.end():block_end]
                solution = re.search(r"\\begin\{solutionbox\}(.*?)\\end\{solutionbox\}", block, flags=re.S)
                before_solution = block[: solution.start()] if solution else block
                answer = re.search(r"答案\s*\\quad\s*(.*?)(?:\\par|\\begin\{solutionbox\})", before_solution, flags=re.S)
                question_raw = before_solution[: answer.start()] if answer else before_solution
                answer_raw = answer.group(1) if answer else ""
                explanation_raw = solution.group(1) if solution else ""
                source_line = text.count("\n", 0, heading.start()) + 1
                parsed.append({
                    "chapter_number": chapter_number,
                    "question_number": int(heading.group(1)),
                    "question_reference": f"{chapter_number:02d}-{int(heading.group(1)):03d}",
                    "source_label": _tex_preview(heading.group(2)),
                    "question_text": _tex_preview(question_raw),
                    "answer_text": _tex_preview(answer_raw),
                    "explanation_text": _tex_preview(explanation_raw),
                    "source_file": str(source_file),
                    "source_line": source_line,
                })
        return parsed

    def import_raydu_grammar_exercises(
        self,
        series_root: Path | str = DEFAULT_RAYDU_SERIES_ROOT,
    ) -> dict[str, Any]:
        """Import question text from the maintained exercise TeX sources."""

        items = self.parse_raydu_grammar_exercises(series_root)
        if not items:
            raise FileNotFoundError(
                "未找到文法解题章节 TeX，或章节中没有可识别的题目；未写入题库。"
            )
        material = self.material_by_code("XY-GRAMMAR-EXERCISES")
        if material is None:
            raise ValueError("预置的文法解题材料不存在，请先初始化英语学习数据库。")
        backup = self.backup("raydu_grammar_questions")
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for item in items:
                    chapter = connection.execute(
                        "SELECT id FROM english_material_chapters WHERE material_id=? AND chapter_number=?",
                        (int(material["id"]), int(item["chapter_number"])),
                    ).fetchone()
                    chapter_id = int(chapter[0]) if chapter is not None else None
                    connection.execute(
                        """
                        INSERT INTO english_grammar_exercises(
                            material_id,chapter_id,question_reference,explanation_reference
                        ) VALUES(?,?,?,?)
                        ON CONFLICT(material_id,question_reference) DO UPDATE SET
                            chapter_id=excluded.chapter_id,
                            explanation_reference=excluded.explanation_reference
                        """,
                        (int(material["id"]), chapter_id, item["question_reference"], item["source_file"]),
                    )
                    exercise = connection.execute(
                        "SELECT id FROM english_grammar_exercises WHERE material_id=? AND question_reference=?",
                        (int(material["id"]), item["question_reference"]),
                    ).fetchone()
                    assert exercise is not None
                    connection.execute(
                        """
                        INSERT INTO english_grammar_question_items(
                            exercise_id,question_text,answer_text,explanation_text,source_file,source_line
                        ) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(exercise_id) DO UPDATE SET
                            question_text=excluded.question_text,
                            answer_text=excluded.answer_text,
                            explanation_text=excluded.explanation_text,
                            source_file=excluded.source_file,
                            source_line=excluded.source_line,
                            imported_at=CURRENT_TIMESTAMP
                        """,
                        (
                            int(exercise[0]), item["question_text"], item["answer_text"],
                            item["explanation_text"], item["source_file"], int(item["source_line"]),
                        ),
                    )
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            count = int(connection.execute(
                "SELECT COUNT(*) FROM english_grammar_question_items WHERE exercise_id IN "
                "(SELECT id FROM english_grammar_exercises WHERE material_id=?)",
                (int(material["id"]),),
            ).fetchone()[0])
        return {
            "imported": len(items),
            "stored": count,
            "source_root": str(Path(series_root).expanduser().resolve()),
            "backup_path": str(backup or ""),
            "readback_verified": count == len(items),
        }

    def grammar_question_items(self, keyword: str = "", *, limit: int = 2000) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if str(keyword or "").strip():
            pattern = f"%{str(keyword).strip()}%"
            clauses.append(
                "(q.question_text LIKE ? OR q.answer_text LIKE ? OR q.explanation_text LIKE ? "
                "OR e.question_reference LIKE ? OR c.title LIKE ?)"
            )
            args.extend([pattern] * 5)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(max(1, min(int(limit), 5000)))
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT q.*,e.question_reference,e.material_id,e.chapter_id,
                       c.chapter_number,c.title AS chapter_title,m.title AS material_title
                FROM english_grammar_question_items q
                JOIN english_grammar_exercises e ON e.id=q.exercise_id
                LEFT JOIN english_material_chapters c ON c.id=e.chapter_id
                JOIN english_materials m ON m.id=e.material_id
                {where}
                ORDER BY c.chapter_number,e.question_reference
                LIMIT ?
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    # -- the English question bank ------------------------------------------

    def raydu_grammar_exercises_root(self, series_root: Path | str | None = None) -> Path:
        """The 《文法解题》 book project on this machine (the folder holding chapters/)."""

        candidates: list[Path] = []
        if str(series_root or "").strip():
            explicit = Path(str(series_root)).expanduser()
            candidates.extend((explicit / "RayDu_Grammar_Exercises", explicit))
        material = self.material_by_code(RAYDU_GRAMMAR_EXERCISES_CODE)
        if material and str(material.get("source_path") or "").strip():
            candidates.append(Path(str(material["source_path"])).parent)
        candidates.extend((
            DEFAULT_RAYDU_SERIES_ROOT / "RayDu_Grammar_Exercises",
            SIBLING_RAYDU_SERIES_ROOT / "RayDu_Grammar_Exercises",
        ))
        for candidate in candidates:
            if (candidate / "chapters").is_dir():
                return candidate.resolve()
        raise FileNotFoundError(
            "未找到《文法解题》原书工程（需要其中的 chapters/ 目录）。已查找："
            + "；".join(str(item) for item in dict.fromkeys(candidates))
        )

    def _raydu_grammar_question_rows(self, series_root: Path | str | None = None) -> dict[str, Any]:
        from shared.scripts.raydu_grammar_exercise_parser import locate_pages, parse_grammar_exercise_book, runs_to_text

        book_root = self.raydu_grammar_exercises_root(series_root)
        parsed = parse_grammar_exercise_book(book_root / "chapters")
        pdf_path = book_root / "旋元佑文法解题.pdf"
        page_counts: dict[str, int] = {}
        if pdf_path.is_file() and parsed["questions"]:
            page_counts = locate_pages(parsed["questions"], pdf_path)
        with self.connect(rows=True) as connection:
            stored = connection.execute(
                "SELECT source_path FROM english_materials WHERE material_code=?",
                (RAYDU_GRAMMAR_EXERCISES_CODE,),
            ).fetchone()
        # Keep the path as the materials table stores it; it is mapped to the
        # current machine on read, like every other material path.
        source_path = str(stored[0] or "") if stored is not None and stored[0] else str(pdf_path)
        rows: list[dict[str, Any]] = []
        for item in parsed["questions"]:
            options = [
                {"label": option["label"], "runs": option["runs"]}
                for option in item["options"]
            ]
            option_text = {option["label"]: runs_to_text(option["runs"]) for option in options}
            answer = item["answer"]
            if item["answer_label"] and answer.strip() == f"({item['answer_label']})" and item["answer_label"] in option_text:
                answer = f"{answer} {option_text[item['answer_label']]}"
            prompt_lines = [item["prompt"]] + [f"({label}) {text}" for label, text in option_text.items()]
            content = {
                "version": 1,
                "book": "旋元佑文法解题",
                "material_code": RAYDU_GRAMMAR_EXERCISES_CODE,
                "reference": item["question_reference"],
                "chapter_title": item["chapter_title"],
                "question_number": item["question_number"],
                "type_name": item["type_name"],
                "school": item["school"],
                "topic_note": item["topic_note"],
                "stem": item["stem_runs"],
                "options": options,
                "option_layout": item["option_layout"],
                "error_spans": item["error_spans"],
                "answer": item["answer"],
                "answer_label": item["answer_label"],
                "corrected": item["corrected_runs"],
                "translation": item["translation_runs"],
                "explanation": item["explanation_runs"],
                "page_status": item.get("page_status") or "missing",
                "warnings": item.get("warnings") or [],
            }
            row = {
                "category": "grammar",
                "question_code": item["question_code"],
                "title": f"第 {item['chapter_number']} 章 {item['chapter_title']} · 第 {item['question_number']} 题",
                "prompt": "\n".join(line for line in prompt_lines if line),
                "passage": "",
                "answer_text": answer,
                "explanation_text": item["explanation"],
                "source_title": "旋元佑文法解题",
                "source_path": source_path,
                "source_page": item.get("source_page"),
                "source_line": int(item["source_line"]),
                "metadata_json": json.dumps(
                    {"importer": "raydu_grammar_exercises_v2", "source_file": item["source_file"]},
                    ensure_ascii=False, sort_keys=True,
                ),
                "question_type": item["question_type"],
                "topic": item["topic"],
                "chapter_number": int(item["chapter_number"]),
                "source_label": item["school"],
                "group_id": None,
                "sort_key": int(item["chapter_number"]) * 1000 + int(item["question_number"]),
                "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            }
            digest_source = json.dumps(
                {key: value for key, value in row.items() if key != "group_id"},
                ensure_ascii=False, sort_keys=True,
            )
            row["content_sha256"] = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            rows.append(row)
        return {
            "book_root": str(book_root),
            "pdf_path": str(pdf_path) if pdf_path.is_file() else "",
            "rows": rows,
            "issues": parsed["issues"],
            "warnings": parsed["warnings"],
            "page_counts": page_counts,
        }

    def _existing_question_digests(self, category: str) -> dict[str, str]:
        with self.connect(rows=True) as connection:
            return {
                str(row["question_code"]): str(row["content_sha256"] or "")
                for row in connection.execute(
                    "SELECT question_code,content_sha256 FROM english_question_bank_items WHERE category=?",
                    (category,),
                )
            }

    def preview_raydu_grammar_question_bank(
        self, series_root: Path | str | None = None, *, sample: int = 6,
    ) -> dict[str, Any]:
        """Parse the book source and compare it with the bank, without writing."""

        built = self._raydu_grammar_question_rows(series_root)
        rows = built["rows"]
        existing = self._existing_question_digests("grammar")
        codes = {row["question_code"] for row in rows}
        new = [row["question_code"] for row in rows if row["question_code"] not in existing]
        changed = [
            row["question_code"] for row in rows
            if row["question_code"] in existing and existing[row["question_code"]] != row["content_sha256"]
        ]
        by_type: dict[str, int] = {}
        for row in rows:
            by_type[row["question_type"]] = by_type.get(row["question_type"], 0) + 1
        step = max(1, len(rows) // max(1, int(sample)))
        missing_pages = [row["question_code"] for row in rows if not row["source_page"]]
        return {
            "book_root": built["book_root"],
            "pdf_path": built["pdf_path"],
            "question_count": len(rows),
            "by_type": by_type,
            "new": len(new),
            "updated": len(changed),
            "unchanged": len(rows) - len(new) - len(changed),
            "stale_in_bank": sorted(
                code for code in existing if code.startswith("XYGE-") and code not in codes
            ),
            "issues": built["issues"],
            "warnings": built["warnings"],
            "page_counts": built["page_counts"],
            "missing_pages": missing_pages,
            "can_import": not built["issues"] and bool(rows),
            "samples": [
                {
                    "question_code": row["question_code"],
                    "question_type": row["question_type"],
                    "title": row["title"],
                    "prompt": row["prompt"][:240],
                    "answer_text": row["answer_text"],
                    "source_page": row["source_page"],
                }
                for row in rows[::step][: max(1, int(sample))]
            ],
        }

    def import_raydu_grammar_question_bank(self, series_root: Path | str | None = None) -> dict[str, Any]:
        """Write every 《文法解题》 question into the grammar category.

        Backup, one transaction and a per-question digest readback.  Rows are
        upserted by question code, so favourites, attempts and note links keep
        pointing at the same questions after a re-import.
        """

        built = self._raydu_grammar_question_rows(series_root)
        rows = built["rows"]
        if built["issues"]:
            raise ValueError(
                f"《文法解题》源码有 {len(built['issues'])} 处无法可靠解析，未写入题库：\n"
                + "\n".join(built["issues"][:12])
            )
        if not rows:
            raise ValueError("《文法解题》源码中没有找到题目，未写入题库。")
        existing = self._existing_question_digests("grammar")
        pending = [row for row in rows if existing.get(row["question_code"]) != row["content_sha256"]]
        backup = self.backup("raydu_grammar_question_bank") if pending else None
        columns = [
            "category", "question_code", "title", "prompt", "passage", "answer_text",
            "explanation_text", "source_title", "source_path", "source_page", "source_line",
            "metadata_json", "question_type", "topic", "chapter_number", "source_label",
            "group_id", "sort_key", "content_json", "content_sha256",
        ]
        updates = ",".join(
            f"{name}=excluded.{name}" for name in columns if name not in {"category", "question_code"}
        )
        if pending:
            with self.connect(rows=True) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for row in pending:
                        connection.execute(
                            f"INSERT INTO english_question_bank_items({','.join(columns)}) "
                            f"VALUES({','.join('?' for _ in columns)}) "
                            f"ON CONFLICT(category,question_code) DO UPDATE SET {updates},"
                            "updated_at=CURRENT_TIMESTAMP",
                            [row[name] for name in columns],
                        )
                    self._integrity(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        stored = self._existing_question_digests("grammar")
        mismatched = [row["question_code"] for row in rows if stored.get(row["question_code"]) != row["content_sha256"]]
        return {
            "question_count": len(rows),
            "written": len(pending),
            "unchanged": len(rows) - len(pending),
            "stored": sum(1 for row in rows if row["question_code"] in stored),
            "warnings": built["warnings"],
            "page_counts": built["page_counts"],
            "book_root": built["book_root"],
            "backup_path": str(backup or ""),
            "readback_verified": not mismatched,
            "readback_mismatches": mismatched[:20],
        }

    # -- 《旋元佑英文阅读》: methodology examples and practice tests -------------

    # -- 旋元佑「广读学英文」 video guides (the 广读 page's reference material) ------

    def extensive_reading_guides(self) -> Any:
        """The 23 episode records in ``English/extensive_reading/xuan_videos`` (read only)."""

        from shared.scripts.xuan_video_guides import GUIDE_DIRNAME, XuanVideoGuides

        folder = self.root / GUIDE_DIRNAME
        guides = getattr(self, "_extensive_reading_guides", None)
        if guides is None or guides.folder != folder:
            guides = XuanVideoGuides(folder)
            self._extensive_reading_guides = guides
        return guides

    # -- the learner's notes on a 广读 passage ----------------------------------------
    # A note belongs to one study guide ("xuan:EP04") and covers a selection, a
    # sentence, a paragraph or the whole passage; selection, sentence and
    # paragraph notes keep the paragraph index, the character range and the
    # quoted text, so they can be found again.  The table is created on the
    # first write, so reading never changes a synchronised database.

    READING_NOTE_SCOPES = ("selection", "sentence", "paragraph", "article")

    @staticmethod
    def _ensure_reading_notes_table(connection: sqlite3.Connection) -> None:
        for statement in READING_NOTES_SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(english_reading_notes)")}
        if "paragraph_end" not in columns:  # a table made before notes could span paragraphs
            connection.execute("ALTER TABLE english_reading_notes ADD COLUMN paragraph_end INTEGER NOT NULL DEFAULT -1")

    def reading_notes(self, guide_key: str) -> list[dict[str, Any]]:
        with self.connect(rows=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='english_reading_notes'"
            ).fetchone()
            if not exists:
                return []
            columns = {row[1] for row in connection.execute("PRAGMA table_info(english_reading_notes)")}
            paragraph_end = "paragraph_end" if "paragraph_end" in columns else "-1 AS paragraph_end"
            rows = connection.execute(
                f"SELECT id,guide_key,scope,paragraph,start,{paragraph_end},end,quote,body,created_at,updated_at "
                "FROM english_reading_notes WHERE guide_key=? ORDER BY paragraph,start,id",
                (str(guide_key),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_reading_note(
        self,
        guide_key: str,
        body: str,
        *,
        scope: str = "selection",
        paragraph: int = -1,
        start: int = -1,
        end: int = -1,
        quote: str = "",
        note_id: int | None = None,
        paragraph_end: int | None = None,
    ) -> dict[str, Any]:
        """Add a note (or replace the text of note ``note_id``); daily backup, one
        transaction, read back.

        A note runs from character ``start`` of ``paragraph`` to character
        ``end`` of ``paragraph_end`` (the same paragraph unless given), so a
        selection over several paragraphs keeps its exact range."""

        guide_key = str(guide_key or "").strip()
        body = str(body or "").strip()
        if not guide_key:
            raise ValueError("笔记必须属于一篇广读材料。")
        if not body:
            raise ValueError("笔记内容不能为空。")
        if scope not in self.READING_NOTE_SCOPES:
            raise ValueError(f"笔记范围只能是 {', '.join(self.READING_NOTE_SCOPES)}。")
        if scope == "article":
            paragraph, start, end = -1, -1, -1
        last = int(paragraph) if paragraph_end is None or scope == "article" else int(paragraph_end)
        if scope != "article" and note_id is None:
            if int(paragraph) < 0:
                raise ValueError("这条笔记需要指出是第几段。")
            if last < int(paragraph):
                raise ValueError("笔记的结束段落不能在开始段落之前。")
        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_reading_notes_table(connection)
                if note_id is None:
                    cursor = connection.execute(
                        "INSERT INTO english_reading_notes(guide_key,scope,paragraph,start,paragraph_end,end,quote,body) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (guide_key, scope, int(paragraph), int(start), last, int(end), str(quote or ""), body),
                    )
                    note_id = int(cursor.lastrowid)
                else:
                    changed = connection.execute(
                        "UPDATE english_reading_notes SET body=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND guide_key=?",
                        (body, int(note_id), guide_key),
                    ).rowcount
                    if changed != 1:
                        raise ValueError(f"没有找到这条笔记（id={note_id}）。")
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        stored = next((note for note in self.reading_notes(guide_key) if note["id"] == int(note_id)), None)
        return {
            "note": stored, "backup_path": str(backup or ""),
            "readback_verified": stored is not None and stored["body"] == body,
        }

    def delete_reading_note(self, note_id: int) -> dict[str, Any]:
        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_reading_notes_table(connection)
                row = connection.execute("SELECT guide_key FROM english_reading_notes WHERE id=?", (int(note_id),)).fetchone()
                if row is None:
                    raise ValueError(f"没有找到这条笔记（id={note_id}）。")
                connection.execute("DELETE FROM english_reading_notes WHERE id=?", (int(note_id),))
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        remaining = {note["id"] for note in self.reading_notes(row["guide_key"])}
        return {"deleted": int(note_id), "backup_path": str(backup or ""),
                "readback_verified": int(note_id) not in remaining}

    def extensive_reading_study(self, episode: int | str) -> dict[str, Any]:
        """The study guide of one book (an episode number) or of one of the
        teacher's method lectures ("WS01"): paragraphs with the explanations."""

        return self.extensive_reading_guides().study(episode)

    def extensive_reading_appendix_html(self) -> str:
        """The appendix 《广读学英文》 (the same in all five Ray Du books) as rich text."""

        from shared.scripts.xuan_video_guides import appendix_html

        return appendix_html(self.raydu_reading_root() / "chapters" / "appendix-extensive-reading.tex")

    def raydu_reading_root(self, series_root: Path | str | None = None) -> Path:
        """The 《英文阅读》 book project on this machine (the folder holding chapters/)."""

        candidates: list[Path] = []
        if str(series_root or "").strip():
            explicit = Path(str(series_root)).expanduser()
            candidates.extend((explicit / "RayDu_Reading", explicit))
        material = self.material_by_code(RAYDU_READING_CODE)
        if material and str(material.get("source_path") or "").strip():
            candidates.append(Path(str(material["source_path"])).parent)
        candidates.extend((
            DEFAULT_RAYDU_SERIES_ROOT / "RayDu_Reading",
            SIBLING_RAYDU_SERIES_ROOT / "RayDu_Reading",
        ))
        for candidate in candidates:
            if (candidate / "chapters").is_dir():
                return candidate.resolve()
        raise FileNotFoundError(
            "未找到《旋元佑英文阅读》原书工程（需要其中的 chapters/ 目录）。已查找："
            + "；".join(str(item) for item in dict.fromkeys(candidates))
        )

    def _raydu_reading_source_path(self, pdf_path: Path) -> str:
        # Keep the path as the materials table stores it; it is mapped to the
        # current machine on read, like every other material path.
        with self.connect(rows=True) as connection:
            stored = connection.execute(
                "SELECT source_path FROM english_materials WHERE material_code=?", (RAYDU_READING_CODE,),
            ).fetchone()
        return str(stored[0] or "") if stored is not None and stored[0] else str(pdf_path)

    def raydu_reading_method(self, series_root: Path | str | None = None) -> dict[str, Any]:
        """Part 1 (methodology) parsed and located in the PDF, cached by source signature.

        The cache is derived data (``shared/ui/cache``); the database is not touched.
        """

        from shared.scripts import raydu_reading_parser
        from shared.scripts.raydu_reading_parser import PDF_NAME, locate_method, parse_method

        book_root = self.raydu_reading_root(series_root)
        pdf_path = book_root / PDF_NAME
        digest = hashlib.sha256(f"v{READING_METHOD_CACHE_VERSION}".encode())
        sources = sorted((book_root / "chapters").glob("part1-*.tex")) + [book_root / f"{RAYDU_READING_TITLE}.tex"]
        # The parser itself is part of the signature: a parser fix rebuilds the cache.
        for path in [*sources, pdf_path, Path(raydu_reading_parser.__file__)]:
            if path.is_file():
                stat = path.stat()
                digest.update(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
        cache_file = READING_METHOD_CACHE_DIR / f"raydu_reading_method_{digest.hexdigest()[:20]}.json"
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                cached["book_root"], cached["pdf_path"] = str(book_root), str(pdf_path) if pdf_path.is_file() else ""
                return cached
            except (OSError, json.JSONDecodeError):
                pass
        method = parse_method(book_root)
        method["page_counts"] = locate_method(method, pdf_path) if pdf_path.is_file() else {}
        try:
            READING_METHOD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for old in READING_METHOD_CACHE_DIR.glob("raydu_reading_method_*.json"):
                old.unlink(missing_ok=True)
            temporary = cache_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(method, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_file)
        except OSError:
            pass  # The cache only saves time; the parsed result is still returned.
        method["book_root"], method["pdf_path"] = str(book_root), str(pdf_path) if pdf_path.is_file() else ""
        return method

    @staticmethod
    def _reading_group_row(group: Mapping[str, Any], source_path: str) -> dict[str, Any]:
        from shared.scripts.raydu_reading_parser import BOOK_TITLE, blocks_to_runs, runs_to_text

        content = {
            "version": 1,
            "book": BOOK_TITLE,
            "material_code": RAYDU_READING_CODE,
            "book_part": group["book_part"],
            "reading_category": group["reading_category"],
            "category_title": group["category_title"],
            "chapter_number": group.get("chapter_number"),
            "heading": group["heading"],
            "title": group["title"],
            "blocks": group["blocks"],
            "translation_blocks": group["translation"],
            "option_bank": group["option_bank"],
            "explanation": group.get("explanation") or [],
            "explanation_source": "book",
            "section_key": group.get("section_key") or "",
        }
        return {
            "category": "reading",
            "group_code": group["group_code"],
            "title": group["title"],
            "passage": runs_to_text(blocks_to_runs(group["blocks"])),
            "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            "source_title": BOOK_TITLE,
            "source_path": source_path,
            "source_page": group.get("source_page"),
        }

    @staticmethod
    def _reading_question_row(item: Mapping[str, Any], source_path: str, sort_key: int) -> dict[str, Any]:
        from shared.scripts.raydu_reading_parser import BOOK_TITLE, TYPE_NAMES, runs_to_text

        options = [{"label": option["label"], "runs": option["runs"]} for option in item["options"]]
        option_text = {option["label"]: runs_to_text(option["runs"]) for option in options}
        instruction = runs_to_text(item["instruction"])
        stem = runs_to_text(item["stem"])
        prompt_lines = [instruction, stem] + [f"({label}) {text}" for label, text in option_text.items()]
        answer = item["answer"]
        key = item["answer_key"]
        if key["kind"] == "label" and key["value"] in option_text and item["question_type"] == "reading_choice":
            answer = f"{answer} {option_text[key['value']]}"
        if item.get("answer_note"):
            answer = f"{answer} {item['answer_note']}"
        method = item["book_part"] == "method"
        location = (
            f"方法论 · {item['heading']}" if method
            else f"Chapter {item['chapter_number']} · {item['category_title']}"
            + (f" · {item['heading']}" if item["heading"] != item["category_title"] else "")
        )
        content = {
            "version": 1,
            "book": BOOK_TITLE,
            "material_code": RAYDU_READING_CODE,
            "book_part": item["book_part"],
            "reading_category": item["reading_category"],
            "category_title": item["category_title"],
            "location": location,
            "heading": item["heading"],
            "section_key": item.get("section_key") or "",
            "question_number": int(item["number"]),
            "number_label": item["number_label"],
            "book_question_no": item["number_label"],
            "type_name": TYPE_NAMES[item["question_type"]],
            "instruction": item["instruction"],
            "stem": item["stem"],
            "options": options,
            "option_layout": "two_column" if item["option_layout"] == "two_column" else "one_column",
            "answer": item["answer"],
            "answer_note": item.get("answer_note") or "",
            "answer_key": key,
            "answer_label": key["value"] if item["question_type"] == "reading_choice" else "",
            "translation": item["zh"],
            "explanation": item["explanation"],
            "explanation_source": "book",
            "explanation_shared": bool(item.get("explanation_shared")),
            "page_status": item.get("page_status") or "missing",
            "warnings": item.get("warnings") or [],
        }
        row = {
            "category": "reading",
            "question_code": item["question_code"],
            "title": f"{location} · 第 {item['number_label']} 题",
            "prompt": "\n".join(line for line in prompt_lines if line),
            "passage": "",
            "answer_text": answer,
            "explanation_text": runs_to_text(item["explanation"]),
            "source_title": BOOK_TITLE,
            "source_path": source_path,
            "source_page": item.get("source_page"),
            "source_line": int(item.get("line") or 0),
            "metadata_json": json.dumps(
                {"importer": "raydu_reading_v1", "source_file": item["source_file"], "group_code": item["group_code"]},
                ensure_ascii=False, sort_keys=True,
            ),
            "question_type": item["question_type"],
            "topic": item["topic"],
            "chapter_number": item.get("chapter_number"),
            "source_label": "方法论" if method else item["category_title"],
            "group_id": None,
            "sort_key": int(sort_key),
            "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            "exam": "",
            "exam_year": None,
        }
        digest_source = json.dumps(
            {key: value for key, value in row.items() if key != "group_id"}, ensure_ascii=False, sort_keys=True,
        )
        row["content_sha256"] = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        row["group_code"] = item["group_code"]
        return row

    def _raydu_reading_rows(self, series_root: Path | str | None = None) -> dict[str, Any]:
        from shared.scripts.raydu_reading_parser import PDF_NAME, READING_CATEGORIES, locate_pages, parse_practice_tests

        book_root = self.raydu_reading_root(series_root)
        pdf_path = book_root / PDF_NAME
        method = self.raydu_reading_method(book_root)
        practice = parse_practice_tests(book_root)
        page_counts: dict[str, int] = {}
        if pdf_path.is_file() and practice["groups"]:
            part2 = max((section.get("source_page") or 1 for section in method["sections"]), default=1)
            page_counts = locate_pages(practice["groups"], practice["questions"], pdf_path, first_page=part2)
        source_path = self._raydu_reading_source_path(pdf_path)
        # Only Part 2 goes into the bank; the methodology's worked examples stay
        # on the 阅读方法 page, read from the book.
        group_rows = [self._reading_group_row(group, source_path) for group in practice["groups"]]
        category_order = {key: index for index, key in enumerate(READING_CATEGORIES)}
        group_order = {group["group_code"]: group for group in practice["groups"]}
        rows: list[dict[str, Any]] = []
        for item in practice["questions"]:
            group = group_order[item["group_code"]]
            sort_key = int(item["chapter_number"]) * 1_000_000 \
                + category_order[item["reading_category"]] * 100_000 \
                + int(group["article_number"]) * 1000 + int(item["number"])
            rows.append(self._reading_question_row(item, source_path, sort_key))
        return {
            "book_root": str(book_root),
            "pdf_path": str(pdf_path) if pdf_path.is_file() else "",
            "groups": group_rows,
            "rows": rows,
            "issues": practice["issues"],
            "warnings": practice["warnings"],
            "counts": practice["counts"],
            "page_counts": page_counts,
        }

    def _existing_reading_groups(self) -> dict[str, dict[str, Any]]:
        return self._existing_book_groups("reading", RAYDU_READING_PREFIX)

    def _existing_book_groups(self, category: str, prefix: str) -> dict[str, dict[str, Any]]:
        with self.connect(rows=True) as connection:
            return {
                str(row["group_code"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM english_question_groups WHERE category=? AND group_code LIKE ?",
                    (category, f"{prefix}-%"),
                )
            }

    @staticmethod
    def _group_changed(row: Mapping[str, Any], stored: Mapping[str, Any] | None) -> bool:
        if stored is None:
            return True
        return any(
            str(row[name] if row[name] is not None else "") != str(stored.get(name) if stored.get(name) is not None else "")
            for name in ("title", "passage", "content_json", "source_title", "source_path", "source_page")
        )

    def preview_raydu_reading_question_bank(
        self, series_root: Path | str | None = None, *, sample: int = 6,
    ) -> dict[str, Any]:
        """Parse 《英文阅读》 and compare it with the reading category, without writing."""

        built = self._raydu_reading_rows(series_root)
        rows = built["rows"]
        existing = {
            code: digest for code, digest in self._existing_question_digests("reading").items()
            if code.startswith(f"{RAYDU_READING_PREFIX}-")
        }
        stored_groups = self._existing_reading_groups()
        codes = {row["question_code"] for row in rows}
        new = [row["question_code"] for row in rows if row["question_code"] not in existing]
        changed = [
            row["question_code"] for row in rows
            if row["question_code"] in existing and existing[row["question_code"]] != row["content_sha256"]
        ]
        by_type: dict[str, int] = {}
        by_topic: dict[str, int] = {}
        for row in rows:
            by_type[row["question_type"]] = by_type.get(row["question_type"], 0) + 1
            by_topic[row["topic"] or "（未标题型）"] = by_topic.get(row["topic"] or "（未标题型）", 0) + 1
        step = max(1, len(rows) // max(1, int(sample)))
        return {
            "book_root": built["book_root"],
            "pdf_path": built["pdf_path"],
            "question_count": len(rows),
            "group_count": len(built["groups"]),
            "by_type": by_type,
            "by_topic": by_topic,
            "counts": built["counts"],
            "new": len(new),
            "updated": len(changed),
            "unchanged": len(rows) - len(new) - len(changed),
            "groups_changed": sum(
                1 for row in built["groups"] if self._group_changed(row, stored_groups.get(row["group_code"]))
            ),
            "stale_in_bank": sorted(code for code in existing if code not in codes),
            "issues": built["issues"],
            "warnings": built["warnings"],
            "page_counts": built["page_counts"],
            "missing_pages": [row["question_code"] for row in rows if not row["source_page"]],
            "can_import": not built["issues"] and bool(rows),
            "samples": [
                {
                    "question_code": row["question_code"],
                    "question_type": row["question_type"],
                    "topic": row["topic"],
                    "title": row["title"],
                    "prompt": row["prompt"][:240],
                    "answer_text": row["answer_text"],
                    "source_page": row["source_page"],
                }
                for row in rows[::step][: max(1, int(sample))]
            ],
        }

    def import_raydu_reading_question_bank(self, series_root: Path | str | None = None) -> dict[str, Any]:
        """Write the 《英文阅读》 passages and questions into the reading category.

        Passages go into english_question_groups, questions into the bank,
        both upserted by code so favourites, attempts and note links survive a
        re-import.  Backup, one transaction, then a digest readback of every
        question and passage.  Questions no longer in the source are reported,
        never deleted.
        """

        built = self._raydu_reading_rows(series_root)
        if built["issues"]:
            raise ValueError(
                f"《旋元佑英文阅读》源码有 {len(built['issues'])} 处无法可靠解析，未写入题库：\n"
                + "\n".join(built["issues"][:12])
            )
        if not built["rows"]:
            raise ValueError("《旋元佑英文阅读》源码中没有找到题目，未写入题库。")
        return {
            **self._write_book_question_set("reading", RAYDU_READING_PREFIX, built, "raydu_reading_question_bank"),
            "counts": built["counts"],
        }

    # -- 《旋元佑英文阅读通》: passages and questions from the PDF -------------------

    def raydu_highpoint_pdf(self) -> Path:
        """The PDF of 《旋元佑英文阅读通》 on this machine (registered under 基础课程)."""

        candidates: list[Path] = []
        material = self.material_by_code(RAYDU_HIGHPOINT_CODE)
        for field in ("source_path", "reading_path"):
            raw = str((material or {}).get(field) or "").strip()
            if raw:
                candidates.append(Path(raw))
        candidates.append(DEFAULT_ENGLISH_ROOT / "materials" / "originals" / f"{RAYDU_HIGHPOINT_CODE}.pdf")
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise FileNotFoundError(
            "未找到《旋元佑英文阅读通》PDF。已查找：" + "；".join(str(item) for item in dict.fromkeys(candidates))
        )

    def raydu_highpoint_book(self) -> dict[str, Any]:
        """The parsed book (methodology, passages, questions), cached by source signature.

        The cache is derived data (``shared/ui/cache``); the database is not touched.
        """

        from shared.scripts import chinese_simplify, raydu_highpoint_parser

        pdf_path = self.raydu_highpoint_pdf()
        digest = hashlib.sha256(f"v{RAYDU_HIGHPOINT_CACHE_VERSION}".encode())
        for path in (pdf_path, Path(raydu_highpoint_parser.__file__), Path(chinese_simplify.__file__)):
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
        cache_file = READING_METHOD_CACHE_DIR / f"raydu_highpoint_{digest.hexdigest()[:20]}.json"
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                cached["pdf_path"] = str(pdf_path)
                return cached
            except (OSError, json.JSONDecodeError):
                pass
        book = raydu_highpoint_parser.parse_book(pdf_path)
        try:
            READING_METHOD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for old in READING_METHOD_CACHE_DIR.glob("raydu_highpoint_*.json"):
                old.unlink(missing_ok=True)
            temporary = cache_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_file)
        except OSError:
            pass  # The cache only saves time; the parsed result is still returned.
        book["pdf_path"] = str(pdf_path)
        return book

    def _raydu_highpoint_source_path(self, pdf_path: Path) -> str:
        # As the materials table stores it (mapped to the current machine on read).
        with self.connect(rows=True) as connection:
            stored = connection.execute(
                "SELECT source_path FROM english_materials WHERE material_code=?", (RAYDU_HIGHPOINT_CODE,),
            ).fetchone()
        return str(stored[0] or "") if stored is not None and stored[0] else str(pdf_path)

    @staticmethod
    def _highpoint_group_row(group: Mapping[str, Any], source_path: str) -> dict[str, Any]:
        from shared.scripts.raydu_highpoint_parser import BOOK_TITLE
        from shared.scripts.raydu_reading_parser import blocks_to_runs, runs_to_text

        content = {
            "version": 1,
            "book": BOOK_TITLE,
            "material_code": RAYDU_HIGHPOINT_CODE,
            "book_part": "practice",
            "reading_category": group["subject"],
            "category_title": group["subject"],
            "chapter_number": group["chapter_number"],
            "chapter_label": f"第 {group['chapter_number']} 章",
            "heading": group["theme"],
            "title": group["title"],
            "blocks": group["blocks"],
            "translation_blocks": group["translation"],
            "option_bank": [],
            "explanation": [],
            "explanation_source": "book",
            "section_key": "",
        }
        return {
            "category": "reading",
            "group_code": group["group_code"],
            "title": group["title"],
            "passage": runs_to_text(blocks_to_runs(group["blocks"])),
            "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            "source_title": BOOK_TITLE,
            "source_path": source_path,
            "source_page": group.get("source_page"),
        }

    @staticmethod
    def _highpoint_question_row(item: Mapping[str, Any], source_path: str, sort_key: int) -> dict[str, Any]:
        from shared.scripts.raydu_highpoint_parser import BOOK_TITLE, OPTION_LABELS
        from shared.scripts.raydu_reading_parser import TYPE_NAMES, runs_to_text

        options = [{"label": option["label"], "runs": option["runs"]} for option in item["options"]]
        option_text = {option["label"]: runs_to_text(option["runs"]) for option in options}
        stem = runs_to_text(item["stem"])
        # Short, so it stays on one line of the card header; the passage card shows the subject.
        location = f"第 {item['chapter_number']} 章 · Reading {item['article_number']}"
        key = {"kind": "label", "value": item["answer"], "choices": list(OPTION_LABELS)}
        content = {
            "version": 1,
            "book": BOOK_TITLE,
            "material_code": RAYDU_HIGHPOINT_CODE,
            "book_part": "practice",
            "reading_category": item["subject"],
            "category_title": item["subject"],
            "chapter_label": f"第 {item['chapter_number']} 章",
            "location": location,
            "heading": item["theme"],
            "section_key": "",
            "question_number": int(item["number"]),
            "number_label": item["number_label"],
            "book_question_no": item["number_label"],
            "type_name": TYPE_NAMES["reading_choice"],
            "instruction": [],
            "stem": item["stem"],
            "options": options,
            "option_layout": "one_column",
            "answer": item["answer"],
            "answer_note": "",
            "answer_key": key,
            "answer_label": item["answer"],
            "translation": item["zh"],
            "explanation": item["explanation"],
            "explanation_source": "book",
            "explanation_shared": False,
            "page_status": "located",
            "warnings": item.get("warnings") or [],
        }
        row = {
            "category": "reading",
            "question_code": item["question_code"],
            "title": f"{location} · 第 {item['number_label']} 题",
            "prompt": "\n".join([stem] + [f"({label}) {text}" for label, text in option_text.items()]),
            "passage": "",
            "answer_text": f"{item['answer']} {option_text.get(item['answer'], '')}".strip(),
            "explanation_text": runs_to_text(item["explanation"]),
            "source_title": BOOK_TITLE,
            "source_path": source_path,
            "source_page": item.get("source_page"),
            "source_line": 0,
            "metadata_json": json.dumps(
                {"importer": "raydu_highpoint_v1", "source_file": Path(source_path).name or "",
                 "group_code": item["group_code"]},
                ensure_ascii=False, sort_keys=True,
            ),
            "question_type": "reading_choice",
            "topic": item["topic"],
            "chapter_number": item["chapter_number"],
            "source_label": item["subject"],
            "group_id": None,
            "sort_key": int(sort_key),
            "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            "exam": "",
            "exam_year": None,
        }
        digest_source = json.dumps(
            {key: value for key, value in row.items() if key != "group_id"}, ensure_ascii=False, sort_keys=True,
        )
        row["content_sha256"] = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        row["group_code"] = item["group_code"]
        return row

    def _raydu_highpoint_rows(self) -> dict[str, Any]:
        book = self.raydu_highpoint_book()
        pdf_path = Path(book["pdf_path"])
        source_path = self._raydu_highpoint_source_path(pdf_path)
        group_rows = [self._highpoint_group_row(group, source_path) for group in book["groups"]]
        rows = [
            self._highpoint_question_row(
                item, source_path,
                int(item["chapter_number"]) * 1_000_000 + int(item["article_number"]) * 1000 + int(item["number"]),
            )
            for item in book["questions"]
        ]
        return {
            "book_root": str(pdf_path.parent),
            "pdf_path": str(pdf_path),
            "groups": group_rows,
            "rows": rows,
            "issues": book["issues"],
            "warnings": book["warnings"],
            "counts": book["counts"],
            "page_counts": {"located": len(rows), "inferred": 0, "missing": 0},
        }

    def preview_raydu_highpoint_question_bank(self, *, sample: int = 6) -> dict[str, Any]:
        """Parse 《旋元佑英文阅读通》 and compare it with the reading category, without writing."""

        built = self._raydu_highpoint_rows()
        rows = built["rows"]
        existing = {
            code: digest for code, digest in self._existing_question_digests("reading").items()
            if code.startswith(f"{RAYDU_HIGHPOINT_PREFIX}-")
        }
        stored_groups = self._existing_book_groups("reading", RAYDU_HIGHPOINT_PREFIX)
        codes = {row["question_code"] for row in rows}
        new = [row["question_code"] for row in rows if row["question_code"] not in existing]
        changed = [
            row["question_code"] for row in rows
            if row["question_code"] in existing and existing[row["question_code"]] != row["content_sha256"]
        ]
        groups_changed = sum(
            1 for row in built["groups"] if self._group_changed(row, stored_groups.get(row["group_code"]))
        )
        by_topic: dict[str, int] = {}
        by_subject: dict[str, int] = {}
        for row in rows:
            by_topic[row["topic"] or "（未标题型）"] = by_topic.get(row["topic"] or "（未标题型）", 0) + 1
            by_subject[row["source_label"]] = by_subject.get(row["source_label"], 0) + 1
        step = max(1, len(rows) // max(1, int(sample)))
        return {
            "book_root": built["book_root"],
            "pdf_path": built["pdf_path"],
            "question_count": len(rows),
            "group_count": len(built["groups"]),
            "by_type": {"reading_choice": len(rows)},
            "by_topic": by_topic,
            "by_subject": by_subject,
            "counts": built["counts"],
            "new": len(new),
            "updated": len(changed),
            "unchanged": len(rows) - len(new) - len(changed),
            "groups_changed": groups_changed,
            "needs_import": bool(new or changed or groups_changed),
            "stale_in_bank": sorted(code for code in existing if code not in codes),
            "issues": built["issues"],
            "warnings": built["warnings"],
            "page_counts": built["page_counts"],
            "missing_pages": [row["question_code"] for row in rows if not row["source_page"]],
            "can_import": not built["issues"] and bool(rows),
            "samples": [
                {
                    "question_code": row["question_code"],
                    "topic": row["topic"],
                    "title": row["title"],
                    "prompt": row["prompt"][:240],
                    "answer_text": row["answer_text"],
                    "source_page": row["source_page"],
                }
                for row in rows[::step][: max(1, int(sample))]
            ],
        }

    def import_raydu_highpoint_question_bank(self) -> dict[str, Any]:
        """Write the 60 passages of 《旋元佑英文阅读通》 into the question groups and
        their 559 questions into the reading category, by code (favourites, attempts
        and note links survive a re-import); backup, one transaction, digest readback.
        Refused as a whole when anything in the PDF could not be parsed reliably."""

        built = self._raydu_highpoint_rows()
        if built["issues"]:
            raise ValueError(
                f"《旋元佑英文阅读通》PDF 有 {len(built['issues'])} 处无法可靠解析，未写入题库：\n"
                + "\n".join(built["issues"][:12])
            )
        if not built["rows"]:
            raise ValueError("《旋元佑英文阅读通》PDF 中没有找到题目，未写入题库。")
        return {
            **self._write_book_question_set("reading", RAYDU_HIGHPOINT_PREFIX, built, "raydu_highpoint_question_bank"),
            "counts": built["counts"],
        }

    def highpoint_method_supplements(self, section_key: str) -> list[dict[str, Any]]:
        """The 《旋元佑英文阅读通》 methodology sections that teach the same question
        type as a 阅读方法 section, with how many of the book's questions practise it."""

        try:
            book = self.raydu_highpoint_book()
        except (FileNotFoundError, ValueError):
            return []
        supplements = [section for section in book["method"] if section_key in section["targets"]]
        if not supplements:
            return []
        with self.connect(rows=True) as connection:
            counts = {
                str(row["topic"]): int(row["count"])
                for row in connection.execute(
                    "SELECT topic,COUNT(*) AS count FROM english_question_bank_items "
                    "WHERE category='reading' AND question_code LIKE ? GROUP BY topic",
                    (f"{RAYDU_HIGHPOINT_PREFIX}-%",),
                )
            }
        from shared.scripts.raydu_highpoint_parser import BOOK_TITLE

        result = []
        for section in supplements:
            # 消去法的细节型或推论型 serves two 阅读方法 sections: each practises its own topic.
            targets, topics = section["targets"], section["topics"]
            topic = topics[targets.index(section_key)] if len(topics) == len(targets) else (topics or [""])[0]
            result.append({
                "key": section["key"],
                "title": section["title"],
                "number": section["number"],
                "book_title": BOOK_TITLE,
                "source": f"book:{BOOK_TITLE}",
                "material_code": RAYDU_HIGHPOINT_CODE,
                "pdf_path": book["pdf_path"],
                "source_page": section["source_page"],
                "blocks": section["blocks"],
                "topics": topics,
                "topic": topic,
                "practice_count": counts.get(topic, 0) if topic else 0,
            })
        return result

    def _write_book_question_set(
        self, category: str, prefix: str, built: Mapping[str, Any], backup_name: str,
    ) -> dict[str, Any]:
        """Upsert a book's groups and questions by code (ids, favourites, attempts and
        note links survive), changed rows only; backup, one transaction, digest readback.
        Questions no longer in the source are reported by the preview, never deleted."""

        rows = built["rows"]
        existing = self._existing_question_digests(category)
        stored_groups = self._existing_book_groups(category, prefix)
        pending_groups = [
            row for row in built["groups"] if self._group_changed(row, stored_groups.get(row["group_code"]))
        ]
        pending = [row for row in rows if existing.get(row["question_code"]) != row["content_sha256"]]
        backup = self.backup(backup_name) if pending or pending_groups else None
        group_columns = [
            "category", "group_code", "title", "passage", "content_json", "source_title", "source_path", "source_page",
        ]
        question_columns = [
            "category", "question_code", "title", "prompt", "passage", "answer_text",
            "explanation_text", "source_title", "source_path", "source_page", "source_line",
            "metadata_json", "question_type", "topic", "chapter_number", "source_label",
            "group_id", "sort_key", "content_json", "content_sha256", "exam", "exam_year",
        ]
        if pending or pending_groups:
            with self.connect(rows=True) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for group in pending_groups:
                        connection.execute(
                            f"INSERT INTO english_question_groups({','.join(group_columns)}) "
                            f"VALUES({','.join('?' for _ in group_columns)}) "
                            "ON CONFLICT(category,group_code) DO UPDATE SET "
                            + ",".join(f"{name}=excluded.{name}" for name in group_columns
                                       if name not in {"category", "group_code"})
                            + ",updated_at=CURRENT_TIMESTAMP",
                            [group[name] for name in group_columns],
                        )
                    group_ids = {
                        str(row["group_code"]): int(row["id"])
                        for row in connection.execute(
                            "SELECT id,group_code FROM english_question_groups WHERE category=? "
                            "AND group_code LIKE ?",
                            (category, f"{prefix}-%"),
                        )
                    }
                    for row in pending:
                        values = {**row, "group_id": group_ids.get(row["group_code"])}
                        if values["group_id"] is None:
                            raise RuntimeError(f"{row['question_code']} 的题组 {row['group_code']} 未写入。")
                        connection.execute(
                            f"INSERT INTO english_question_bank_items({','.join(question_columns)}) "
                            f"VALUES({','.join('?' for _ in question_columns)}) "
                            "ON CONFLICT(category,question_code) DO UPDATE SET "
                            + ",".join(f"{name}=excluded.{name}" for name in question_columns
                                       if name not in {"category", "question_code"})
                            + ",updated_at=CURRENT_TIMESTAMP",
                            [values[name] for name in question_columns],
                        )
                    self._integrity(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        stored = self._existing_question_digests(category)
        stored_groups = self._existing_book_groups(category, prefix)
        mismatched = [row["question_code"] for row in rows if stored.get(row["question_code"]) != row["content_sha256"]]
        group_mismatches = [
            row["group_code"] for row in built["groups"]
            if self._group_changed(row, stored_groups.get(row["group_code"]))
        ]
        with self.connect(rows=True) as connection:
            unlinked = int(connection.execute(
                "SELECT COUNT(*) FROM english_question_bank_items q WHERE q.category=? "
                "AND q.question_code LIKE ? AND (q.group_id IS NULL OR NOT EXISTS("
                "SELECT 1 FROM english_question_groups g WHERE g.id=q.group_id "
                "AND g.group_code=json_extract(q.metadata_json,'$.group_code')))",
                (category, f"{prefix}-%"),
            ).fetchone()[0])
        return {
            "question_count": len(rows),
            "group_count": len(built["groups"]),
            "written": len(pending),
            "groups_written": len(pending_groups),
            "unchanged": len(rows) - len(pending),
            "stored": sum(1 for row in rows if row["question_code"] in stored),
            "warnings": built["warnings"],
            "page_counts": built["page_counts"],
            "book_root": built["book_root"],
            "backup_path": str(backup or ""),
            "readback_verified": not mismatched and not group_mismatches and not unlinked,
            "readback_mismatches": (mismatched + group_mismatches)[:20],
            "unlinked_questions": unlinked,
        }

    # -- the 阅读方法 page: sections, examples and reading progress ----------------

    def reading_method_outline(self) -> dict[str, Any]:
        """The methodology sections in book order with their reading/example progress."""

        method = self.raydu_reading_method()
        progress = self.reading_method_progress()
        sections = []
        for section in method["sections"]:
            sections.append({
                "key": section["key"], "title": section["title"], "level": section["level"],
                "number": section.get("number") or "", "parent": section.get("parent") or "",
                "source_page": section.get("source_page"),
                "examples": len(section["example_codes"]), "questions": len(section["question_codes"]),
                "read": section["key"] in progress, "read_at": progress.get(section["key"], ""),
            })
        return {
            "book_title": RAYDU_READING_TITLE,
            "pdf_path": method.get("pdf_path") or "",
            "sections": sections,
            "read_count": sum(1 for item in sections if item["read"]),
        }

    def reading_method_section(self, section_key: str) -> dict[str, Any]:
        """One methodology section: lecture blocks, worked examples and their questions.

        The worked examples are read from the book and are not in the question
        bank: their questions have no id, and answering one is only a self-check.
        """

        method = self.raydu_reading_method()
        sections = method["sections"]
        index = next((number for number, item in enumerate(sections) if item["key"] == str(section_key)), None)
        if index is None:
            raise ValueError(f"《旋元佑英文阅读》方法论中没有 {section_key} 这一节。")
        section = sections[index]
        source_path = self._raydu_reading_source_path(Path(method.get("pdf_path") or "."))
        groups = []
        for group in method["groups"]:
            if group["group_code"] not in section["example_codes"]:
                continue
            row = self._reading_group_row(group, source_path)
            questions = []
            for position, item in enumerate(method["questions"]):
                if item["group_code"] != group["group_code"]:
                    continue
                questions.append(self._question_dict({**self._reading_question_row(item, source_path, position), "id": 0}))
            groups.append({
                "group_code": group["group_code"],
                "group": {**row, "content": json.loads(row["content_json"]),
                          "source_path": str(resolve_stored_path(source_path)) if source_path else ""},
                "questions": questions,
            })
        return {
            "key": section["key"], "title": section["title"], "level": section["level"],
            "parent": section.get("parent") or "", "source_page": section.get("source_page"),
            "blocks": section["blocks"], "examples": groups,
            "book_title": RAYDU_READING_TITLE, "pdf_path": method.get("pdf_path") or "",
            # The same question type as taught by 《旋元佑英文阅读通》.
            "supplements": self.highpoint_method_supplements(section["key"]),
            "read": section["key"] in self.reading_method_progress(),
            "previous_key": sections[index - 1]["key"] if index else "",
            "next_key": sections[index + 1]["key"] if index + 1 < len(sections) else "",
        }

    def reading_method_progress(self) -> dict[str, str]:
        """``{section_key: read_at}``; empty until the first section is marked read."""

        with self.connect(rows=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='english_reading_method_progress'"
            ).fetchone()
            if exists is None:
                return {}
            return {
                str(row["section_key"]): str(row["read_at"] or "")
                for row in connection.execute("SELECT section_key,read_at FROM english_reading_method_progress")
            }

    def mark_reading_method_section_read(self, section_key: str, read: bool = True) -> dict[str, Any]:
        """Mark a methodology section as read (or not).  The progress table is created
        on the first write, so reading the page never changes a synchronised database."""

        key = str(section_key or "").strip()
        method = self.raydu_reading_method()
        if key not in {section["key"] for section in method["sections"]}:
            raise ValueError(f"《旋元佑英文阅读》方法论中没有 {key} 这一节。")
        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(READING_METHOD_PROGRESS_SCHEMA)
                if read:
                    connection.execute(
                        "INSERT INTO english_reading_method_progress(section_key,read_at) "
                        "VALUES(?,CURRENT_TIMESTAMP) ON CONFLICT(section_key) DO UPDATE SET "
                        "updated_at=CURRENT_TIMESTAMP",
                        (key,),
                    )
                else:
                    connection.execute("DELETE FROM english_reading_method_progress WHERE section_key=?", (key,))
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        stored = key in self.reading_method_progress()
        return {
            "section_key": key, "read": stored, "backup_path": str(backup or ""),
            "readback_verified": stored == bool(read),
        }

    # -- 《旋元佑英文写作》: the 写作方法 page --------------------------------------

    def raydu_writing_root(self, series_root: Path | str | None = None) -> Path:
        """The 《英文写作》 book project on this machine (the folder holding chapters/).

        Looked up from the material's stored PDF path (translated to this
        machine), then the default and sibling RayDuSeries folders.
        """

        candidates: list[Path] = []
        if str(series_root or "").strip():
            explicit = Path(str(series_root)).expanduser()
            candidates.extend((explicit / "RayDu_Writing", explicit))
        material = self.material_by_code(RAYDU_WRITING_CODE)
        if material and str(material.get("source_path") or "").strip():
            candidates.append(Path(str(material["source_path"])).parent)
        candidates.extend((
            DEFAULT_RAYDU_SERIES_ROOT / "RayDu_Writing",
            SIBLING_RAYDU_SERIES_ROOT / "RayDu_Writing",
        ))
        for candidate in candidates:
            if (candidate / "chapters").is_dir():
                return candidate.resolve()
        raise FileNotFoundError(
            "未找到《旋元佑英文写作》原书工程（需要其中的 chapters/ 目录）。已查找："
            + "；".join(str(item) for item in dict.fromkeys(candidates))
        )

    def raydu_writing_book(self, series_root: Path | str | None = None) -> dict[str, Any]:
        """The whole book parsed and located in the PDF, cached by source signature.

        The cache is derived data (``shared/ui/cache``); the database is not touched.
        """

        from shared.scripts import raydu_reading_parser, raydu_writing_parser
        from shared.scripts.raydu_writing_parser import PDF_NAME, locate, parse_book

        book_root = self.raydu_writing_root(series_root)
        pdf_path = book_root / PDF_NAME
        digest = hashlib.sha256(f"v{WRITING_METHOD_CACHE_VERSION}".encode())
        sources = sorted((book_root / "chapters").glob("*.tex")) + [book_root / f"{RAYDU_WRITING_TITLE}.tex"]
        # The parsers are part of the signature: a parser fix rebuilds the cache.
        for path in [*sources, pdf_path, Path(raydu_writing_parser.__file__), Path(raydu_reading_parser.__file__)]:
            if path.is_file():
                stat = path.stat()
                digest.update(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
        cache_file = WRITING_METHOD_CACHE_DIR / f"raydu_writing_{digest.hexdigest()[:20]}.json"
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                cached["book_root"], cached["pdf_path"] = str(book_root), str(pdf_path) if pdf_path.is_file() else ""
                return cached
            except (OSError, json.JSONDecodeError):
                pass
        book = parse_book(book_root)
        book["page_counts"] = locate(book, pdf_path) if pdf_path.is_file() and not book["issues"] else {}
        try:
            WRITING_METHOD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for old in WRITING_METHOD_CACHE_DIR.glob("raydu_writing_*.json"):
                old.unlink(missing_ok=True)
            temporary = cache_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_file)
        except OSError:
            pass  # The cache only saves time; the parsed result is still returned.
        book["book_root"], book["pdf_path"] = str(book_root), str(pdf_path) if pdf_path.is_file() else ""
        return book

    def writing_method_outline(self) -> dict[str, Any]:
        """Parts, chapters and sections in book order with the reading progress."""

        from shared.scripts.raydu_writing_parser import PART_NUMERALS

        book = self.raydu_writing_book()
        progress = self.writing_method_progress()
        chapters = {chapter["key"]: chapter for chapter in book["chapters"]}
        sections = []
        for section in book["sections"]:
            chapter = chapters[section["chapter_key"]]
            sections.append({
                "key": section["key"], "title": section["title"], "chapter_key": section["chapter_key"],
                "part": section["part"], "chapter": section["chapter"], "intro": section["intro"],
                "source_page": section.get("source_page"),
                "read": section["key"] in progress, "read_at": progress.get(section["key"], ""),
            })
        return {
            "book_title": RAYDU_WRITING_TITLE,
            "pdf_path": book.get("pdf_path") or "",
            "parts": [{"part": number, "title": f"{PART_NUMERALS[number]} {title}" if number < 4 else title}
                      for number, title in sorted({(item["part"], item["part_title"]) for item in book["chapters"]})],
            "chapters": [
                {"key": chapter["key"], "part": chapter["part"], "chapter": chapter["chapter"],
                 "title": chapter["title"], "source_page": chapter.get("source_page"),
                 "question_count": chapter["question_count"], "section_keys": chapter["section_keys"]}
                for chapter in book["chapters"]
            ],
            "sections": sections,
            "read_count": sum(1 for item in sections if item["read"]),
            "issues": book["issues"],
        }

    def writing_method_section(self, section_key: str) -> dict[str, Any]:
        """One section of the book: its lecture blocks (exercises are only a link to the bank)."""

        from shared.scripts.raydu_writing_parser import PART_NUMERALS

        book = self.raydu_writing_book()
        sections = book["sections"]
        position = next((index for index, item in enumerate(sections) if item["key"] == str(section_key)), None)
        if position is None:
            raise ValueError(f"《旋元佑英文写作》中没有 {section_key} 这一节。")
        section = sections[position]
        chapter = next(item for item in book["chapters"] if item["key"] == section["chapter_key"])
        return {
            "key": section["key"], "title": section["title"], "full_title": section["full_title"],
            "chapter_key": chapter["key"], "chapter_title": chapter["title"],
            "part": chapter["part"], "part_title": PART_NUMERALS[chapter["part"]] if chapter["part"] < 4 else "",
            "part_name": chapter["part_title"], "chapter": chapter["chapter"],
            "source_page": section.get("source_page"), "blocks": section["blocks"],
            "chapter_question_count": chapter["question_count"],
            "previous_key": sections[position - 1]["key"] if position else "",
            "next_key": sections[position + 1]["key"] if position + 1 < len(sections) else "",
            "book_title": RAYDU_WRITING_TITLE, "pdf_path": book.get("pdf_path") or "",
            "read": section["key"] in self.writing_method_progress(),
        }

    def writing_method_progress(self) -> dict[str, str]:
        """``{section_key: read_at}``; empty until the first section is marked read."""

        with self.connect(rows=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='english_writing_method_progress'"
            ).fetchone()
            if exists is None:
                return {}
            return {
                str(row["section_key"]): str(row["read_at"] or "")
                for row in connection.execute("SELECT section_key,read_at FROM english_writing_method_progress")
            }

    def mark_writing_method_section_read(self, section_key: str, read: bool = True) -> dict[str, Any]:
        """Mark a section of 《英文写作》 as read (or not).  The progress table is created
        on the first write, so reading the page never changes a synchronised database."""

        key = str(section_key or "").strip()
        book = self.raydu_writing_book()
        if key not in {section["key"] for section in book["sections"]}:
            raise ValueError(f"《旋元佑英文写作》中没有 {key} 这一节。")
        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(WRITING_METHOD_PROGRESS_SCHEMA)
                if read:
                    connection.execute(
                        "INSERT INTO english_writing_method_progress(section_key,read_at) "
                        "VALUES(?,CURRENT_TIMESTAMP) ON CONFLICT(section_key) DO UPDATE SET "
                        "updated_at=CURRENT_TIMESTAMP",
                        (key,),
                    )
                else:
                    connection.execute("DELETE FROM english_writing_method_progress WHERE section_key=?", (key,))
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        stored = key in self.writing_method_progress()
        return {
            "section_key": key, "read": stored, "backup_path": str(backup or ""),
            "readback_verified": stored == bool(read),
        }

    # -- 《旋元佑文法》: the 文法 page --------------------------------------------------

    def raydu_grammar_root(self, series_root: Path | str | None = None) -> Path:
        """The 《文法》 book project on this machine (the folder holding chapters/).

        Looked up from the material's stored PDF path (translated to this
        machine), then the default and sibling RayDuSeries folders.
        """

        candidates: list[Path] = []
        if str(series_root or "").strip():
            explicit = Path(str(series_root)).expanduser()
            candidates.extend((explicit / "RayDu_Grammar", explicit))
        material = self.material_by_code(RAYDU_GRAMMAR_CODE)
        if material and str(material.get("source_path") or "").strip():
            candidates.append(Path(str(material["source_path"])).parent)
        candidates.extend((
            DEFAULT_RAYDU_SERIES_ROOT / "RayDu_Grammar",
            SIBLING_RAYDU_SERIES_ROOT / "RayDu_Grammar",
        ))
        for candidate in candidates:
            if (candidate / "chapters").is_dir():
                return candidate.resolve()
        raise FileNotFoundError(
            "未找到《旋元佑文法》原书工程（需要其中的 chapters/ 目录）。已查找："
            + "；".join(str(item) for item in dict.fromkeys(candidates))
        )

    def raydu_grammar_book(self, series_root: Path | str | None = None) -> dict[str, Any]:
        """The whole book parsed and located in the PDF, cached by source signature.

        The cache is derived data (``shared/ui/cache``); the database is not touched.
        """

        from shared.scripts import raydu_grammar_parser, raydu_reading_parser, raydu_writing_parser
        from shared.scripts.raydu_grammar_parser import MAIN_TEX, PDF_NAME, locate, parse_book

        book_root = self.raydu_grammar_root(series_root)
        pdf_path = book_root / PDF_NAME
        digest = hashlib.sha256(f"v{GRAMMAR_METHOD_CACHE_VERSION}".encode())
        sources = [*sorted((book_root / "chapters").glob("*.tex")), *sorted((book_root / "integration").glob("*.tex")),
                   *sorted((book_root / "frontmatter").glob("*.tex")), book_root / MAIN_TEX]
        # The parsers are part of the signature: a parser fix rebuilds the cache.
        for path in [*sources, pdf_path, Path(raydu_grammar_parser.__file__), Path(raydu_writing_parser.__file__),
                     Path(raydu_reading_parser.__file__)]:
            if path.is_file():
                stat = path.stat()
                digest.update(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
        cache_file = GRAMMAR_METHOD_CACHE_DIR / f"raydu_grammar_{digest.hexdigest()[:20]}.json"
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                cached["book_root"], cached["pdf_path"] = str(book_root), str(pdf_path) if pdf_path.is_file() else ""
                return cached
            except (OSError, json.JSONDecodeError):
                pass
        book = parse_book(book_root)
        book["page_counts"] = locate(book, pdf_path) if pdf_path.is_file() and not book["issues"] else {}
        try:
            GRAMMAR_METHOD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for old in GRAMMAR_METHOD_CACHE_DIR.glob("raydu_grammar_*.json"):
                old.unlink(missing_ok=True)
            temporary = cache_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_file)
        except OSError:
            pass  # The cache only saves time; the parsed result is still returned.
        book["book_root"], book["pdf_path"] = str(book_root), str(pdf_path) if pdf_path.is_file() else ""
        return book

    def grammar_exercise_counts(self) -> dict[int, int]:
        """``{chapter: count}`` of the 《文法解题》 questions in the bank's grammar
        category; that book's chapters are numbered like 《文法》's."""

        with self.connect(rows=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='english_question_bank_items'"
            ).fetchone()
            if exists is None:
                return {}
            return {
                int(row["chapter_number"]): int(row["total"])
                for row in connection.execute(
                    "SELECT chapter_number, COUNT(*) AS total FROM english_question_bank_items "
                    "WHERE category='grammar' AND source_title=? AND exam='' AND chapter_number IS NOT NULL "
                    "GROUP BY chapter_number",
                    (RAYDU_GRAMMAR_EXERCISES_TITLE,),
                )
            }

    def grammar_method_outline(self) -> dict[str, Any]:
        """Parts, chapters and sections in book order with the reading progress."""

        from shared.scripts.raydu_grammar_parser import PART_NUMERALS

        book = self.raydu_grammar_book()
        progress = self.grammar_method_progress()
        exercises = self.grammar_exercise_counts()
        sections = []
        for section in book["sections"]:
            sections.append({
                "key": section["key"], "title": section["title"], "chapter_key": section["chapter_key"],
                "part": section["part"], "chapter": section["chapter"], "intro": section["intro"],
                "exercise": section["exercise"], "source_page": section.get("source_page"),
                "read": section["key"] in progress, "read_at": progress.get(section["key"], ""),
            })
        return {
            "book_title": RAYDU_GRAMMAR_TITLE,
            "pdf_path": book.get("pdf_path") or "",
            # The front matter (导读) and the appendix (附录) are parts without a numeral.
            "parts": [{"part": number, "title": title if unnumbered else f"{PART_NUMERALS[number]} {title}",
                       "abstract": abstract}
                      for number, title, unnumbered, abstract in sorted({
                          (item["part"], item["part_title"], bool(item["appendix"] or item["front"]),
                           item["part_abstract"]) for item in book["chapters"]})],
            "chapters": [
                {"key": chapter["key"], "part": chapter["part"], "chapter": chapter["chapter"],
                 "label": chapter["label"], "appendix": chapter["appendix"], "front": chapter["front"],
                 "title": chapter["title"],
                 "source_page": chapter.get("source_page"),
                 "exercise_count": exercises.get(chapter["chapter"], 0) if chapter["chapter"] else 0,
                 "section_keys": chapter["section_keys"]}
                for chapter in book["chapters"]
            ],
            "sections": sections,
            "read_count": sum(1 for item in sections if item["read"]),
            "issues": book["issues"],
        }

    def grammar_method_section(self, section_key: str) -> dict[str, Any]:
        """One section of the book: its lecture blocks and, for the chapter, the
        number of 《文法解题》 questions the page links to."""

        from shared.scripts.raydu_grammar_parser import PART_NUMERALS

        book = self.raydu_grammar_book()
        sections = book["sections"]
        position = next((index for index, item in enumerate(sections) if item["key"] == str(section_key)), None)
        if position is None:
            raise ValueError(f"《旋元佑文法》中没有 {section_key} 这一节。")
        section = sections[position]
        chapter = next(item for item in book["chapters"] if item["key"] == section["chapter_key"])
        return {
            "key": section["key"], "title": section["title"],
            "chapter_key": chapter["key"], "chapter_title": chapter["title"], "chapter_label": chapter["label"],
            "part": chapter["part"],
            "part_title": "" if chapter["appendix"] or chapter["front"] else PART_NUMERALS[chapter["part"]],
            "appendix": chapter["appendix"], "front": chapter["front"],
            # The part's one-line abstract opens its first section, as on the book's part page.
            "part_abstract": chapter["part_abstract"] if position == 0
            or sections[position - 1]["part"] != section["part"] else "",
            "part_name": chapter["part_title"], "chapter": chapter["chapter"],
            "exercise": section["exercise"], "answers_at": section["answers_at"],
            "source_page": section.get("source_page"), "blocks": section["blocks"],
            "chapter_exercise_count": self.grammar_exercise_counts().get(chapter["chapter"], 0)
            if chapter["chapter"] else 0,
            "chapter_last": section["key"] == chapter["section_keys"][-1],
            "previous_key": sections[position - 1]["key"] if position else "",
            "next_key": sections[position + 1]["key"] if position + 1 < len(sections) else "",
            "book_title": RAYDU_GRAMMAR_TITLE, "pdf_path": book.get("pdf_path") or "",
            "read": section["key"] in self.grammar_method_progress(),
        }

    def grammar_method_progress(self) -> dict[str, str]:
        """``{section_key: read_at}``; empty until the first section is marked read."""

        with self.connect(rows=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='english_grammar_method_progress'"
            ).fetchone()
            if exists is None:
                return {}
            return {
                str(row["section_key"]): str(row["read_at"] or "")
                for row in connection.execute("SELECT section_key,read_at FROM english_grammar_method_progress")
            }

    def mark_grammar_method_section_read(self, section_key: str, read: bool = True) -> dict[str, Any]:
        """Mark a section of 《文法》 as read (or not).  The progress table is created
        on the first write, so reading the page never changes a synchronised database."""

        key = str(section_key or "").strip()
        book = self.raydu_grammar_book()
        if key not in {section["key"] for section in book["sections"]}:
            raise ValueError(f"《旋元佑文法》中没有 {key} 这一节。")
        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(GRAMMAR_METHOD_PROGRESS_SCHEMA)
                if read:
                    connection.execute(
                        "INSERT INTO english_grammar_method_progress(section_key,read_at) "
                        "VALUES(?,CURRENT_TIMESTAMP) ON CONFLICT(section_key) DO UPDATE SET "
                        "updated_at=CURRENT_TIMESTAMP",
                        (key,),
                    )
                else:
                    connection.execute("DELETE FROM english_grammar_method_progress WHERE section_key=?", (key,))
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        stored = key in self.grammar_method_progress()
        return {
            "section_key": key, "read": stored, "backup_path": str(backup or ""),
            "readback_verified": stored == bool(read),
        }

    # -- 《旋元佑英文写作》: the closing exercises in the bank's writing category -------

    def _raydu_writing_source_path(self, pdf_path: Path) -> str:
        # Keep the path as the materials table stores it; it is mapped to the
        # current machine on read, like every other material path.
        with self.connect(rows=True) as connection:
            stored = connection.execute(
                "SELECT source_path FROM english_materials WHERE material_code=?", (RAYDU_WRITING_CODE,),
            ).fetchone()
        return str(stored[0] or "") if stored is not None and stored[0] else str(pdf_path)

    @staticmethod
    def _writing_instruction(group: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The group's requirement as shown on its cards: English, Chinese and the book's
        own note for the group; a group without an English line shows the region's text."""

        parts = [group["instruction"], group["instruction_zh"], *group["notes"]]
        if not group["instruction"]:
            parts = [*group["intro"], *parts]
        runs: list[dict[str, Any]] = []
        for part in parts:
            if part:
                if runs:
                    runs.append({"line": True})
                runs.extend(part)
        return runs

    @classmethod
    def _writing_group_row(cls, group: Mapping[str, Any], source_path: str) -> dict[str, Any]:
        from shared.scripts.raydu_writing_parser import BOOK_TITLE, runs_to_text

        instruction = cls._writing_instruction(group)
        content = {
            "version": 1, "book": BOOK_TITLE, "material_code": RAYDU_WRITING_CODE,
            "part": group["part"], "part_title": group["part_title"], "chapter": group["chapter"],
            "chapter_title": group["chapter_title"], "chapter_key": group["chapter_key"],
            "letter": group["letter"], "title": group["title"], "region_title": group["region_title"],
            "instruction": instruction, "question_type": group["question_type"],
            "answer_size": group["answer_size"], "context": group.get("context"),
        }
        return {
            "category": "writing",
            "group_code": group["group_code"],
            "title": cls._writing_location(group),
            "passage": runs_to_text(instruction),
            "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            "source_title": BOOK_TITLE,
            "source_path": source_path,
            "source_page": group.get("source_page"),
        }

    @staticmethod
    def _writing_location(item: Mapping[str, Any]) -> str:
        from shared.scripts.raydu_writing_parser import PART_NUMERALS

        title = str(item.get("group_title") if "group_title" in item else item.get("title") or "")
        return " · ".join(part for part in (
            f"{PART_NUMERALS[int(item['part'])]} 第 {int(item['chapter'])} 章 {item['chapter_title']}",
            title if title and not title.startswith("写作练习") else "",
        ) if part)

    @classmethod
    def _writing_question_row(
        cls, item: Mapping[str, Any], group: Mapping[str, Any], source_path: str, sort_key: int,
    ) -> dict[str, Any]:
        from shared.scripts.raydu_writing_parser import BOOK_TITLE, PART_NUMERALS, runs_to_text

        instruction = cls._writing_instruction(group)
        location = cls._writing_location(item)
        prompt_task = item["number_label"] == "写作"
        answer = runs_to_text(item["model_answer"])
        content = {
            "version": 1, "book": BOOK_TITLE, "material_code": RAYDU_WRITING_CODE,
            "location": location, "part": item["part"], "part_title": item["part_title"],
            "chapter": item["chapter"], "chapter_title": item["chapter_title"], "chapter_key": item["chapter_key"],
            "chapter_label": f"{PART_NUMERALS[int(item['part'])]} 第 {int(item['chapter'])} 章 {item['chapter_title']}",
            "letter": item["letter"], "group_title": item["group_title"],
            "question_number": None if prompt_task else int(item["number"]),
            "number_label": "写作练习" if prompt_task else str(item["number_label"]),
            "number_is_label": prompt_task,
            "book_question_no": item["number_label"],
            "type_name": item["type_name"], "answer_size": item["answer_size"],
            "instruction": instruction, "stem": item["stem"],
            "answer": answer, "answer_key": item.get("answer_key"),
            "model_answer": item["model_answer"], "model_answer_zh": item["model_answer_zh"],
            "reference_label": item["reference_label"], "reference_source": "book",
            "explanation": item["explanation"], "explanation_source": "book",
            "context": item.get("context"),
            "page_status": item.get("page_status") or "missing",
            "warnings": item.get("warnings") or [],
        }
        if not content["answer_key"]:
            content.pop("answer_key")
        row = {
            "category": "writing",
            "question_code": item["question_code"],
            "title": f"{location} · " + ("写作" if prompt_task else f"第 {item['number_label']} 题"),
            "prompt": "\n".join(text for text in (runs_to_text(instruction), runs_to_text(item["stem"])) if text),
            "passage": "",
            "answer_text": answer,
            "explanation_text": runs_to_text(item["explanation"]),
            "source_title": BOOK_TITLE,
            "source_path": source_path,
            "source_page": item.get("source_page"),
            "source_line": int(item.get("line") or 0),
            "metadata_json": json.dumps(
                {"importer": "raydu_writing_v1", "source_file": item["source_file"], "group_code": item["group_code"]},
                ensure_ascii=False, sort_keys=True,
            ),
            "question_type": item["question_type"],
            "topic": item["chapter_title"],
            "chapter_number": int(item["part"]) * 100 + int(item["chapter"]),
            "source_label": item["part_title"],
            "group_id": None,
            "sort_key": int(sort_key),
            "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            "exam": "",
            "exam_year": None,
        }
        digest_source = json.dumps(
            {key: value for key, value in row.items() if key != "group_id"}, ensure_ascii=False, sort_keys=True,
        )
        row["content_sha256"] = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        row["group_code"] = item["group_code"]
        return row

    def _raydu_writing_rows(self, series_root: Path | str | None = None) -> dict[str, Any]:
        from shared.scripts.raydu_writing_parser import PDF_NAME

        book = self.raydu_writing_book(series_root)
        book_root = Path(book["book_root"])
        source_path = self._raydu_writing_source_path(book_root / PDF_NAME)
        groups = {group["group_code"]: group for group in book["groups"]}
        rows = [
            self._writing_question_row(item, groups[item["group_code"]], source_path, position + 1)
            for position, item in enumerate(book["questions"])
        ]
        by_type: dict[str, int] = {}
        for row in rows:
            by_type[row["question_type"]] = by_type.get(row["question_type"], 0) + 1
        return {
            "book_root": str(book_root),
            "pdf_path": book.get("pdf_path") or "",
            "groups": [self._writing_group_row(group, source_path) for group in book["groups"]],
            "rows": rows,
            "issues": book["issues"],
            "warnings": book["warnings"],
            "by_type": by_type,
            "page_counts": book.get("page_counts") or {},
        }

    def preview_raydu_writing_question_bank(
        self, series_root: Path | str | None = None, *, sample: int = 6,
    ) -> dict[str, Any]:
        """Parse 《英文写作》's closing exercises and compare them with the writing category, without writing."""

        built = self._raydu_writing_rows(series_root)
        rows = built["rows"]
        existing = {
            code: digest for code, digest in self._existing_question_digests("writing").items()
            if code.startswith(f"{RAYDU_WRITING_PREFIX}-")
        }
        stored_groups = self._existing_book_groups("writing", RAYDU_WRITING_PREFIX)
        codes = {row["question_code"] for row in rows}
        new = [row["question_code"] for row in rows if row["question_code"] not in existing]
        changed = [
            row["question_code"] for row in rows
            if row["question_code"] in existing and existing[row["question_code"]] != row["content_sha256"]
        ]
        groups_changed = sum(
            1 for row in built["groups"] if self._group_changed(row, stored_groups.get(row["group_code"]))
        )
        step = max(1, len(rows) // max(1, int(sample)))
        return {
            "book_root": built["book_root"],
            "pdf_path": built["pdf_path"],
            "question_count": len(rows),
            "group_count": len(built["groups"]),
            "by_type": built["by_type"],
            "new": len(new),
            "updated": len(changed),
            "updated_codes": changed[:40],
            "unchanged": len(rows) - len(new) - len(changed),
            "groups_changed": groups_changed,
            "needs_import": bool(new or changed or groups_changed),
            "stale_in_bank": sorted(code for code in existing if code not in codes),
            "removed_question_codes": sorted(code for code in existing if code not in codes),
            "issues": built["issues"],
            "warnings": built["warnings"],
            "page_counts": built["page_counts"],
            "missing_pages": [row["question_code"] for row in rows if not row["source_page"]],
            "can_import": not built["issues"] and bool(rows),
            "samples": [
                {
                    "question_code": row["question_code"], "question_type": row["question_type"],
                    "topic": row["topic"], "title": row["title"], "prompt": row["prompt"][:240],
                    "answer_text": row["answer_text"][:240], "source_page": row["source_page"],
                }
                for row in rows[::step][: max(1, int(sample))]
            ],
        }

    def import_raydu_writing_question_bank(self, series_root: Path | str | None = None) -> dict[str, Any]:
        """Write 《英文写作》's closing exercises into the writing category.

        Groups (one per A/B/C set or essay task) go into english_question_groups,
        questions into the bank, upserted by code so favourites, attempts and note
        links survive a re-import.  The lecture's own examples and exam prompts
        stay on the 写作方法 page.  Backup, one transaction, digest readback;
        questions no longer in the source are reported, never deleted.
        """

        built = self._raydu_writing_rows(series_root)
        if built["issues"]:
            raise ValueError(
                f"《旋元佑英文写作》源码有 {len(built['issues'])} 处无法可靠解析，未写入题库：\n"
                + "\n".join(built["issues"][:12])
            )
        if not built["rows"]:
            raise ValueError("《旋元佑英文写作》源码中没有找到写作练习，未写入题库。")
        return {
            **self._write_book_question_set("writing", RAYDU_WRITING_PREFIX, built, "raydu_writing_question_bank"),
            "by_type": built["by_type"],
        }

    def writing_grading_prompt(self, question_id: int, response: str = "") -> str:
        """A prompt for an outside chat model to grade one writing answer (copied, never sent).

        It carries the task, the learner's answer (the given text, else the last
        recorded one), the book's model and its note, and the chapter's section
        titles as the points the answer is judged by.
        """

        question = self.question_bank_item(int(question_id))
        if question is None:
            raise ValueError(f"英语题库中没有 id={question_id} 的题目。")
        content = question.get("content") or {}
        if content.get("book") != RAYDU_WRITING_TITLE:
            raise ValueError("只有《旋元佑英文写作》的写作练习可以生成批改提示词。")
        from shared.scripts.raydu_writing_parser import runs_to_text

        answer = str(response or "").strip() or str(question.get("last_response") or "").strip()
        points: list[str] = []
        try:
            book = self.raydu_writing_book()
            points = [section["full_title"] for section in book["sections"]
                      if section["chapter_key"] == content.get("chapter_key") and section["numbered"]]
        except (FileNotFoundError, OSError):
            pass
        lines = [
            "请你担任英文写作老师，批改我在《旋元佑英文写作》中的一道写作练习。",
            "", f"出处：{content.get('location') or question.get('title')}（{question['question_code']}，"
                f"原书第 {question.get('source_page') or '?'} 页）",
        ]
        if points:
            lines += ["", f"本章「{content.get('chapter_title')}」的讲解要点（按原书小节）："]
            lines += [f"{index}. {title}" for index, title in enumerate(points, 1)]
        instruction = runs_to_text(content.get("instruction") or [])
        if instruction:
            lines += ["", "题目要求：", instruction]
        lines += ["", "题目：", runs_to_text(content.get("stem") or [])]
        lines += ["", "我的作答：", answer or "（尚未作答）"]
        lines += ["", f"{content.get('reference_label') or '原书范文'}（仅供参照，不是唯一答案）：",
                  runs_to_text(content.get("model_answer") or [])]
        note = runs_to_text(content.get("explanation") or [])
        if note:
            lines += ["", "原书说明：", note]
        lines += [
            "", "批改要求：",
            "1. 先用一两句话总评，再按本章讲解要点逐条指出我的作答做到和没做到的地方；",
            "2. 列出语法、用词、标点和衔接上的具体错误，每条给出原句、改法和理由；",
            "3. 只修改有问题的句子，不要替我整篇重写；",
            "4. 最后给出两三条下次写作时可以直接用上的建议。",
            "请用简体中文回答，引用英文原句时保留英文。",
        ]
        return "\n".join(lines).strip()

    def mark_question_attempt_correct(self, attempt_id: int) -> dict[str, Any]:
        """The learner overrules an automatic "wrong" (a fill-in answer the key did not list):
        the attempt becomes a self-judged correct answer and leaves the mistake view."""

        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            row = connection.execute(
                "SELECT a.*,q.question_code FROM english_question_attempts a "
                "JOIN english_question_bank_items q ON q.id=a.question_id WHERE a.id=?",
                (int(attempt_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"没有 id={attempt_id} 的做题记录。")
            if int(row["is_correct"]):
                raise ValueError("这条做题记录已经是答对的。")
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE english_question_attempts SET is_correct=1,judged_by='self' WHERE id=?",
                    (int(attempt_id),),
                )
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            stored = connection.execute(
                "SELECT is_correct,judged_by FROM english_question_attempts WHERE id=?", (int(attempt_id),),
            ).fetchone()
        return {
            "attempt_id": int(attempt_id), "question_id": int(row["question_id"]),
            "question_code": str(row["question_code"]), "response": str(row["response"]),
            "correct_answer": str(row["correct_answer"]), "is_correct": True, "judged_by": "self",
            "backup_path": str(backup or ""),
            "readback_verified": stored is not None and int(stored["is_correct"]) == 1 and stored["judged_by"] == "self",
        }

    @staticmethod
    def _question_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try:
            item["content"] = json.loads(str(item.get("content_json") or "{}"))
        except json.JSONDecodeError:
            item["content"] = {}
        item.pop("content_json", None)
        if str(item.get("source_path") or "").strip():
            item["source_path"] = str(resolve_stored_path(str(item["source_path"])))
        item["favorite"] = bool(item.get("favorite"))
        return item

    _QUESTION_SELECT = """
        SELECT q.*,
               (SELECT 1 FROM english_question_favorites f WHERE f.question_id=q.id) AS favorite,
               (SELECT COUNT(*) FROM english_question_attempts a WHERE a.question_id=q.id) AS attempt_count,
               (SELECT COUNT(*) FROM english_question_attempts a
                 WHERE a.question_id=q.id AND a.is_correct=0 AND a.resolved=0) AS open_mistakes,
               (SELECT a.is_correct FROM english_question_attempts a WHERE a.question_id=q.id
                 ORDER BY a.attempted_at DESC, a.id DESC LIMIT 1) AS last_correct,
               (SELECT a.response FROM english_question_attempts a WHERE a.question_id=q.id
                 ORDER BY a.attempted_at DESC, a.id DESC LIMIT 1) AS last_response
        FROM english_question_bank_items q
    """

    @staticmethod
    def _question_filter(
        category: str,
        keyword: str = "",
        *,
        question_type: str = "",
        chapter_number: int | None = None,
        topic: str = "",
        paper: str = "",
        source: str = "",
        reading_category: str = "",
        group_id: int | None = None,
        favorites_only: bool = False,
        mistakes_only: bool = False,
    ) -> tuple[str, list[Any]]:
        normalized_category = str(category or "grammar").strip().casefold()
        if normalized_category not in QUESTION_CATEGORIES:
            raise ValueError("英语题库分类必须是 grammar、reading 或 writing。")
        clauses = ["q.category=?"]
        args: list[Any] = [normalized_category]
        source = str(source or "").strip()
        if source.startswith("book:"):
            # "book:旋元佑英文阅读": the questions of one book (not of an exam paper).
            clauses.append("q.source_title=? AND q.exam=''")
            args.append(source[len("book:"):])
        elif source:
            paper = source
        if str(reading_category or "").strip():
            clauses.append("q.source_label=?")
            args.append(str(reading_category).strip())
        if str(question_type or "").strip():
            clauses.append("q.question_type=?")
            args.append(str(question_type).strip())
        if chapter_number:
            clauses.append("q.chapter_number=?")
            args.append(int(chapter_number))
        if str(topic or "").strip():
            clauses.append("q.topic=?")
            args.append(str(topic).strip())
        if str(paper or "").strip():
            # "考研英语一|2025": one exam paper.
            exam, _bar, year = str(paper).partition("|")
            clauses.append("q.exam=? AND q.exam_year=?")
            args.extend([exam.strip(), int(year or 0)])
        if group_id is not None:
            # 0 selects the questions that have no passage.
            if int(group_id):
                clauses.append("q.group_id=?")
                args.append(int(group_id))
            else:
                clauses.append("q.group_id IS NULL")
        if favorites_only:
            clauses.append("EXISTS(SELECT 1 FROM english_question_favorites f WHERE f.question_id=q.id)")
        if mistakes_only:
            clauses.append(
                "EXISTS(SELECT 1 FROM english_question_attempts a "
                "WHERE a.question_id=q.id AND a.is_correct=0 AND a.resolved=0)"
            )
        value = str(keyword or "").strip()
        if value:
            pattern = f"%{value}%"
            clauses.append(
                "(q.question_code LIKE ? OR q.title LIKE ? OR q.prompt LIKE ? OR q.passage LIKE ? "
                "OR q.answer_text LIKE ? OR q.explanation_text LIKE ? OR q.topic LIKE ? "
                "OR q.source_label LIKE ? OR q.source_title LIKE ?)"
            )
            args.extend([pattern] * 9)
        return " AND ".join(clauses), args

    def question_bank_page(
        self,
        category: str = "grammar",
        keyword: str = "",
        *,
        question_type: str = "",
        chapter_number: int | None = None,
        topic: str = "",
        paper: str = "",
        source: str = "",
        reading_category: str = "",
        group_id: int | None = None,
        favorites_only: bool = False,
        mistakes_only: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """One page of questions in book order, with favourite and practice state."""

        where, args = self._question_filter(
            category, keyword, question_type=question_type, chapter_number=chapter_number,
            topic=topic, paper=paper, source=source, reading_category=reading_category,
            group_id=group_id,
            favorites_only=favorites_only, mistakes_only=mistakes_only,
        )
        size = max(1, min(int(limit), 2000))
        start = max(0, int(offset))
        with self.connect(rows=True) as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM english_question_bank_items q WHERE {where}", args
            ).fetchone()[0])
            rows = connection.execute(
                f"{self._QUESTION_SELECT} WHERE {where} ORDER BY q.sort_key, q.id LIMIT ? OFFSET ?",
                [*args, size, start],
            ).fetchall()
        return {
            "items": [self._question_dict(row) for row in rows],
            "total": total,
            "offset": start,
            "limit": size,
        }

    def question_bank_sections(self, category: str = "grammar", keyword: str = "", **filters: Any) -> list[dict[str, Any]]:
        """The passages of the matching questions in paper order, for reading one passage at a time.

        Each entry is ``{group_id, count}``; questions without a passage are
        gathered into one entry with ``group_id`` 0 at the position of the first.
        """

        where, args = self._question_filter(category, keyword, **filters)
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"SELECT COALESCE(q.group_id,0) AS group_id,COUNT(*) AS count,MIN(q.sort_key) AS first_key "
                f"FROM english_question_bank_items q WHERE {where} "
                "GROUP BY COALESCE(q.group_id,0) ORDER BY first_key",
                args,
            ).fetchall()
        return [{"group_id": int(row["group_id"]), "count": int(row["count"])} for row in rows]

    def question_bank_items(
        self,
        category: str = "grammar",
        keyword: str = "",
        *,
        limit: int = 500,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """Questions of one visible English category (grammar, reading, writing)."""

        return self.question_bank_page(category, keyword, limit=limit, **filters)["items"]

    def question_bank_item(
        self, question_id: int | None = None, *, question_code: str = "", category: str = "grammar",
    ) -> dict[str, Any] | None:
        with self.connect(rows=True) as connection:
            if question_id:
                row = connection.execute(f"{self._QUESTION_SELECT} WHERE q.id=?", (int(question_id),)).fetchone()
            else:
                row = connection.execute(
                    f"{self._QUESTION_SELECT} WHERE q.category=? AND q.question_code=?",
                    (str(category or "grammar"), str(question_code or "").strip()),
                ).fetchone()
        if row is None:
            return None
        question = self._question_dict(row)
        if question.get("group_id"):
            question["group"] = self.question_groups([question["group_id"]]).get(int(question["group_id"]))
        return question

    def question_bank_filters(self, category: str = "grammar") -> dict[str, Any]:
        """Question types, chapters and exam points present in one category.

        ``sources`` lists the books and exam papers of the category, with the
        chapters, test kinds (主流/特殊/托福/雅思) and question types of each.
        """

        where, args = self._question_filter(category)
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"SELECT q.question_type,q.chapter_number,q.topic,q.title,q.content_json,q.exam,q.exam_year,"
                f"q.source_title,q.source_label,q.question_code "
                f"FROM english_question_bank_items q WHERE {where} ORDER BY q.sort_key,q.id",
                args,
            ).fetchall()
            favorites = int(connection.execute(
                f"SELECT COUNT(*) FROM english_question_bank_items q WHERE {where} AND "
                "EXISTS(SELECT 1 FROM english_question_favorites f WHERE f.question_id=q.id)",
                args,
            ).fetchone()[0])
            mistakes = int(connection.execute(
                f"SELECT COUNT(*) FROM english_question_bank_items q WHERE {where} AND "
                "EXISTS(SELECT 1 FROM english_question_attempts a "
                "WHERE a.question_id=q.id AND a.is_correct=0 AND a.resolved=0)",
                args,
            ).fetchone()[0])
        types: dict[str, int] = {}
        chapters: dict[int, dict[str, Any]] = {}
        papers: dict[str, dict[str, Any]] = {}
        books: dict[str, dict[str, Any]] = {}
        for row in rows:
            kind = str(row["question_type"] or "")
            types[kind] = types.get(kind, 0) + 1
            if str(category).strip().casefold() != "grammar" and row["source_title"] and not row["exam"]:
                # A book in the reading/writing category (《英文阅读》), filtered by its own chapters.
                key = f"book:{row['source_title']}"
                book = books.setdefault(key, {"source": key, "title": str(row["source_title"]), "kind": "book",
                                              "count": 0, "chapters": {}, "categories": {}, "topics": {},
                                              "chapter_titles": {}})
                book["count"] += 1
                if row["chapter_number"] is not None:
                    number = int(row["chapter_number"])
                    book["chapters"][number] = book["chapters"].get(number, 0) + 1
                    if number not in book["chapter_titles"]:
                        try:
                            label = str(json.loads(str(row["content_json"] or "{}")).get("chapter_label") or "")
                        except json.JSONDecodeError:
                            label = ""
                        book["chapter_titles"][number] = label or f"Chapter {number}"
                label = str(row["source_label"] or "")
                if label:
                    book["categories"][label] = book["categories"].get(label, 0) + 1
                topic = str(row["topic"] or "")
                if topic:
                    book["topics"][topic] = book["topics"].get(topic, 0) + 1
                continue
            if row["exam"] and row["exam_year"]:
                key = f"{row['exam']}|{int(row['exam_year'])}"
                if key not in papers:
                    papers[key] = {
                        "paper": key, "exam": str(row["exam"]), "year": int(row["exam_year"]),
                        "title": f"{int(row['exam_year'])} {row['exam']}", "count": 0, "topics": {},
                    }
                papers[key]["count"] += 1
                topic = str(row["topic"] or "")
                if topic:
                    papers[key]["topics"][topic] = papers[key]["topics"].get(topic, 0) + 1
            if row["chapter_number"] is None:
                continue
            number = int(row["chapter_number"])
            if number not in chapters:
                try:
                    title = str(json.loads(str(row["content_json"] or "{}")).get("chapter_title") or "")
                except json.JSONDecodeError:
                    title = ""
                chapters[number] = {"chapter_number": number, "title": title, "count": 0, "topics": {}}
            chapter = chapters[number]
            chapter["count"] += 1
            topic = str(row["topic"] or "")
            if topic:
                chapter["topics"][topic] = chapter["topics"].get(topic, 0) + 1
        return {
            "category": str(category),
            "total": len(rows),
            "types": types,
            "chapters": [
                {**chapter, "topics": [{"topic": key, "count": value} for key, value in chapter["topics"].items()]}
                for _number, chapter in sorted(chapters.items())
            ],
            "papers": [
                {**paper, "topics": [{"topic": key, "count": value} for key, value in paper["topics"].items()]}
                for paper in sorted(papers.values(), key=lambda item: (-item["year"], item["exam"]))
            ],
            "books": [
                {
                    **{name: value for name, value in book.items() if name != "chapter_titles"},
                    "chapters": [{"chapter_number": number, "title": book["chapter_titles"][number], "count": count}
                                 for number, count in sorted(book["chapters"].items())],
                    "categories": [{"key": key, "count": value} for key, value in book["categories"].items()],
                    "topics": [{"topic": key, "count": value} for key, value in book["topics"].items()],
                }
                for book in books.values()
            ],
            "favorites": favorites,
            "mistakes": mistakes,
        }

    def question_bank_topics(self, category: str = "reading", **filters: Any) -> list[dict[str, Any]]:
        """Question types (topics) and counts among the questions matching ``filters``."""

        filters.pop("topic", None)
        where, args = self._question_filter(category, **filters)
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"SELECT q.topic,COUNT(*) AS count,MIN(q.sort_key) AS first FROM english_question_bank_items q "
                f"WHERE {where} AND q.topic<>'' GROUP BY q.topic ORDER BY first",
                args,
            ).fetchall()
        return [{"topic": str(row["topic"]), "count": int(row["count"])} for row in rows]

    def question_bank_counts(self) -> dict[str, int]:
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                "SELECT category,COUNT(*) AS count FROM english_question_bank_items GROUP BY category"
            ).fetchall()
        counts = {"grammar": 0, "reading": 0, "writing": 0}
        counts.update({str(row["category"]): int(row["count"]) for row in rows})
        return counts

    def _practice_backup(self) -> Path | None:
        """One recovery copy per day for frequent practice writes (favourites, answers)."""

        stamp = datetime.now().strftime("%Y-%m-%d")
        existing = sorted(self.backup_dir.glob(f"english_learning_before_question_practice_{stamp}_*.db"))
        return existing[-1] if existing else self.backup("question_practice")

    def _require_question(self, connection: sqlite3.Connection, question_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id,category,question_code,question_type,content_json FROM english_question_bank_items WHERE id=?",
            (int(question_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"英语题库中没有 id={question_id} 的题目。")
        return row

    def set_question_favorite(self, question_id: int, favorite: bool = True) -> dict[str, Any]:
        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                question = self._require_question(connection, question_id)
                if favorite:
                    connection.execute(
                        "INSERT OR IGNORE INTO english_question_favorites(question_id) VALUES(?)",
                        (int(question_id),),
                    )
                else:
                    connection.execute("DELETE FROM english_question_favorites WHERE question_id=?", (int(question_id),))
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            stored = connection.execute(
                "SELECT 1 FROM english_question_favorites WHERE question_id=?", (int(question_id),)
            ).fetchone() is not None
        return {
            "question_id": int(question_id),
            "question_code": str(question["question_code"]),
            "favorite": stored,
            "backup_path": str(backup or ""),
            "readback_verified": stored == bool(favorite),
        }

    @staticmethod
    def _choice_label(response: str) -> str:
        match = re.fullmatch(r"\s*\(?\s*([A-Ha-h])\s*\)?\s*", str(response or ""))
        return match.group(1).upper() if match else ""

    def record_question_attempt(
        self,
        question_id: int,
        response: str,
        *,
        self_judged_correct: bool | None = None,
        mistake_reason: str = "",
    ) -> dict[str, Any]:
        """Record one answer.  Choice and error-choice questions are judged automatically;
        a correction answer is compared by the learner, who passes ``self_judged_correct``."""

        with self.connect(rows=True) as connection:
            question = self._require_question(connection, question_id)
        content = json.loads(str(question["content_json"] or "{}"))
        kind = str(question["question_type"] or "")
        answer_label = str(content.get("answer_label") or "")
        judged = self.judge_question_response(content, response) if content.get("answer_key") else None
        if judged is not None:
            # 《英文阅读》: letters, T/F/NG, roman numerals, a set of choices or words.
            stored_response, correct_answer = judged["response"], judged["correct_answer"]
            if judged["decided"]:
                is_correct, judged_by = bool(judged["is_correct"]), "auto"
            elif self_judged_correct is None:
                raise ValueError("答案没有自动对上，需要你对照原书答案后自己判断对错（self_judged_correct）。")
            else:
                is_correct, judged_by = bool(self_judged_correct), "self"
        elif answer_label:
            label = self._choice_label(response)
            if not label:
                raise ValueError("请选择 A–H 中的一个选项。")
            stored_response, correct_answer = label, answer_label
            is_correct, judged_by = label == answer_label, "auto"
        else:
            if self_judged_correct is None:
                raise ValueError("这道题需要你对照答案后自己判断对错（self_judged_correct）。")
            stored_response = str(response or "").strip()
            correct_answer = str(content.get("answer") or "")
            is_correct, judged_by = bool(self_judged_correct), "self"
        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO english_question_attempts(
                        question_id,response,correct_answer,is_correct,judged_by,mistake_reason
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (int(question_id), stored_response, correct_answer, int(is_correct), judged_by,
                     str(mistake_reason or "").strip()),
                )
                attempt_id = int(cursor.lastrowid)
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            stored = connection.execute(
                "SELECT * FROM english_question_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return {
            "attempt_id": attempt_id,
            "question_id": int(question_id),
            "question_code": str(question["question_code"]),
            "response": stored_response,
            "correct_answer": correct_answer,
            "is_correct": bool(is_correct),
            "judged_by": judged_by,
            "backup_path": str(backup or ""),
            "readback_verified": stored is not None and int(stored["is_correct"]) == int(is_correct),
        }

    @staticmethod
    def _normal_words(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        text = re.sub(r"[‘’`´]", "'", text)
        text = re.sub(r"[^\w\s'-]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def judge_question_response(cls, content: Mapping[str, Any], response: Any) -> dict[str, Any]:
        """Judge a response against ``content["answer_key"]`` without writing.

        ``label`` keys accept one of the choices (case-insensitive), ``set``
        keys a comma-separated set (order ignored).  ``text`` keys (IELTS word
        completion) are decided only when the words match an accepted answer;
        otherwise ``decided`` is False and the learner compares for themself.
        """

        key = dict(content.get("answer_key") or {})
        kind = str(key.get("kind") or "")
        raw = str(response or "").strip()
        if kind == "label":
            choices = [str(item) for item in key.get("choices") or []]
            wanted = re.sub(r"^\(|\)$", "", raw).strip()
            match = next((choice for choice in choices if choice.casefold() == wanted.casefold()), "")
            if not match:
                raise ValueError(f"请从 {'、'.join(choices)} 中选一个。")
            return {"decided": True, "is_correct": match == str(key.get("value")), "response": match,
                    "correct_answer": str(key.get("value"))}
        if kind == "set":
            choices = [str(item) for item in key.get("choices") or []]
            picked = sorted({part.strip() for part in re.split(r"[,，、\s]+", raw) if part.strip()},
                            key=lambda item: choices.index(item) if item in choices else 99)
            unknown = [item for item in picked if item not in choices]
            if not picked or unknown:
                raise ValueError(f"请从 {'、'.join(choices)} 中选择（用逗号分隔）。")
            wanted = sorted(str(item) for item in key.get("value") or [])
            return {"decided": True, "is_correct": sorted(picked) == wanted, "response": ", ".join(picked),
                    "correct_answer": ", ".join(wanted)}
        if kind == "blanks":
            # 《英文写作》 fill-ins: one answer per blank, "a / b" between blanks.
            from shared.scripts.raydu_writing_parser import check_blanks

            blanks = key.get("blanks") or []
            parts = [part.strip() for part in raw.split(" / ")] if len(blanks) > 1 else [raw]
            if not raw or len(parts) != len(blanks) or not all(parts):
                raise ValueError(f"请填写全部 {len(blanks)} 个空格。" if len(blanks) > 1 else "请先填写空格。")
            return {"decided": True, "is_correct": check_blanks(key, parts), "response": " / ".join(parts),
                    "correct_answer": str(content.get("answer") or " / ".join(items[0] for items in blanks))}
        if kind == "text":
            accepted = [str(item) for item in key.get("value") or []]
            hit = bool(raw) and cls._normal_words(raw) in {cls._normal_words(item) for item in accepted}
            return {"decided": hit, "is_correct": True if hit else None, "response": raw,
                    "correct_answer": str(content.get("answer") or " / ".join(accepted))}
        raise ValueError(f"无法识别的答案格式：{kind or '（空）'}")

    def resolve_question_mistakes(self, question_id: int) -> dict[str, Any]:
        """Mark a question as mastered: its open wrong attempts leave the mistake view."""

        backup = self._practice_backup()
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                question = self._require_question(connection, question_id)
                cursor = connection.execute(
                    "UPDATE english_question_attempts SET resolved=1,resolved_at=CURRENT_TIMESTAMP "
                    "WHERE question_id=? AND is_correct=0 AND resolved=0",
                    (int(question_id),),
                )
                resolved = int(cursor.rowcount)
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            remaining = int(connection.execute(
                "SELECT COUNT(*) FROM english_question_attempts WHERE question_id=? AND is_correct=0 AND resolved=0",
                (int(question_id),),
            ).fetchone()[0])
        return {
            "question_id": int(question_id),
            "question_code": str(question["question_code"]),
            "resolved": resolved,
            "backup_path": str(backup or ""),
            "readback_verified": remaining == 0,
        }

    def question_groups(self, group_ids: Any) -> dict[int, dict[str, Any]]:
        """Passages (reading texts, translation passages) by id, with parsed content."""

        ids = sorted({int(value) for value in group_ids or [] if value})
        if not ids:
            return {}
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"SELECT * FROM english_question_groups WHERE id IN ({','.join('?' for _ in ids)})", ids
            ).fetchall()
        groups: dict[int, dict[str, Any]] = {}
        for row in rows:
            group = dict(row)
            try:
                group["content"] = json.loads(str(group.pop("content_json") or "{}"))
            except json.JSONDecodeError:
                group["content"] = {}
            if str(group.get("source_path") or "").strip():
                group["source_path"] = str(resolve_stored_path(str(group["source_path"])))
            groups[int(group["id"])] = group
        return groups

    def _exam_paper_changes(self, built: Mapping[str, Any]) -> dict[str, Any]:
        prefix = built["paper_code"] + "-"
        with self.connect(rows=True) as connection:
            existing = {
                row["question_code"]: row["content_sha256"] for row in connection.execute(
                    "SELECT question_code,content_sha256 FROM english_question_bank_items "
                    "WHERE substr(question_code,1,?)=?", (len(prefix), prefix),
                ) if row["question_code"].rsplit("-", 1)[0] == built["paper_code"]
            }
            groups = {
                row["group_code"]: dict(row) for row in connection.execute(
                    "SELECT * FROM english_question_groups WHERE substr(group_code,1,?)=?",
                    (len(prefix), prefix),
                ) if row["group_code"].rsplit("-", 1)[0] == built["paper_code"]
            }
        new = sum(row["question_code"] not in existing for row in built["questions"])
        updated = sum(row["question_code"] in existing and existing[row["question_code"]] != row["content_sha256"]
                      for row in built["questions"])
        new_groups = sum(group["group_code"] not in groups for group in built["groups"])
        updated_groups = sum(
            group["group_code"] in groups and any(
                groups[group["group_code"]].get(key) != value
                for key, value in group.items() if key not in {"source_path", "content_sha256"}
            ) for group in built["groups"]
        )
        return {
            "new": new, "updated": updated, "new_groups": new_groups, "updated_groups": updated_groups,
            "removed_question_codes": sorted(set(existing) - {row["question_code"] for row in built["questions"]}),
            "removed_group_codes": sorted(set(groups) - {group["group_code"] for group in built["groups"]}),
            "needs_import": bool(new or updated or new_groups or updated_groups),
        }

    # -- ChatGPT Work tasks -----------------------------------------------------

    @staticmethod
    def _write_task_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"任务文件写后回读失败：{path}")

    def exam_import_task_dirs(self) -> list[Path]:
        """Exam-import task folders, newest first."""

        root = self.root / "chatgpt_work" / "exam_import"
        folders = [path for path in root.iterdir() if (path / "TASK.md").is_file()] if root.is_dir() else []
        return sorted(folders, key=lambda path: (path / "TASK.md").stat().st_mtime, reverse=True)

    def exam_import_task(self, task_dir: Path | str) -> dict[str, Any]:
        """Describe one exam-import task folder, its EXAM.json draft and last check report."""

        folder = Path(task_dir)
        task_file = folder / "TASK.md"
        output_file = folder / "EXAM.json"
        report = folder / "CHECK_REPORT.md"
        try:
            meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        return {
            "task_dir": str(folder),
            "task_file": str(task_file),
            "output_file": str(output_file),
            "task_ready": task_file.is_file(),
            "draft_exists": output_file.is_file() and bool(output_file.read_text(encoding="utf-8").strip()),
            "source_pdf": str(meta.get("source_pdf") or ""),
            "pdf_name": str(meta.get("pdf_name") or folder.name),
            "check_report": report.read_text(encoding="utf-8") if report.is_file() else "",
            "task_line": f"请阅读 {task_file} 并按其中要求完成，结果只写入同一文件夹的 EXAM.json。",
        }

    def prepare_exam_import_task(self, pdf_path: Path | str) -> dict[str, Any]:
        """Write the task folder ChatGPT Work needs to turn one exam PDF into EXAM.json:
        the PDF, its text page by page, the complete JSON format and the instructions."""

        from shared.scripts.english_exam_paper import EXAM_PAPER_FORMAT, read_pdf_text

        source = Path(pdf_path).expanduser().resolve()
        if source.suffix.casefold() != ".pdf" or not source.is_file():
            raise FileNotFoundError(f"试卷 PDF 不存在：{source}")
        stem = re.sub(r'[\\/:*?"<>|\s]+', "_", source.stem).strip("._") or "exam"
        folder = self.root / "chatgpt_work" / "exam_import" / stem
        folder.mkdir(parents=True, exist_ok=True)
        target_pdf = folder / "source.pdf"
        if not target_pdf.is_file() or _sha256(target_pdf) != _sha256(source):
            temporary = folder / "source.tmp.pdf"
            shutil.copy2(source, temporary)
            os.replace(temporary, target_pdf)
        text = read_pdf_text(target_pdf, max_chars=10_000_000)
        self._write_task_text(
            folder / "PDF_TEXT.md",
            "# 试卷 PDF 文字层（逐页）\n\n"
            + "\n\n".join(f"## 第 {page['page']} 页\n\n{page['text']}" for page in text["pages"])
            + "\n",
        )
        self._write_task_text(
            folder / "EXAM_FORMAT.json", json.dumps(EXAM_PAPER_FORMAT, ensure_ascii=False, indent=2) + "\n"
        )
        self._write_task_text(
            folder / "task.json",
            json.dumps({"source_pdf": str(source), "pdf_name": source.name}, ensure_ascii=False, indent=2) + "\n",
        )
        (folder / "CHECK_REPORT.md").unlink(missing_ok=True)
        scanned = "" if text["text_layer"] else (
            "\n注意：这份 PDF 没有文字层（扫描件），`PDF_TEXT.md` 是空的。不要凭图片猜文字；"
            "先在对话里告诉我，不要写 EXAM.json。\n"
        )
        self._write_task_text(folder / "TASK.md", f"""# ChatGPT Work 任务：把一整套英语试卷整理成题库导入文件

本文件中的路径都相对于本文件所在的文件夹。只写 `EXAM.json`，不要创建、修改或删除其他文件。

## 材料

- `source.pdf`：试卷原卷（{source.name}，共 {text['page_count']} 页）。版面、分组、划线句和表格以它为准。
- `PDF_TEXT.md`：程序从 PDF 文字层逐页抽出的文字。文章、题干、选项从这里逐字照抄。
- `EXAM_FORMAT.json`：试卷 JSON 的完整格式——字段、标记语法、题型分类和一个示例。
- `CHECK_REPORT.md`：只有程序核对发现问题后才会出现，是上一次核对的问题清单。
{scanned}
## 产出

把整套试卷写成一个 JSON 对象，保存为本文件夹的 `EXAM.json`（UTF-8，只写 JSON，不加代码块或说明）。

## 步骤

1. 完整阅读 `EXAM_FORMAT.json`，严格按它的字段和标记语法写。
2. 完整阅读 `PDF_TEXT.md`，不要漏页；对照 `source.pdf` 确认文章分组、划线句和表格。
3. 整理整套试卷：
   - exam 写考试名称（如“考研英语一”“考研英语二”），year 写四位年份，paper_code 如 KY-E1-2025。卡片上会显示“年份 + 英一/英二”，必须写清楚。
   - 每篇文章一个 group；完形的空写成 [[题号]]，翻译划线句用 <u>…</u>；题号与原卷完全一致。
   - 文章、题干、选项逐字照抄：不改写、不纠错、不补全；PDF 自动折行要接回整句，段落之间空一行。
   - source_pdf 写 "source.pdf"；source_page 写题目所在页。
4. 答案（最重要，宁缺勿错）：
   - 试卷 PDF 自带答案，或我在对话里给了答案/答案来源时，以它为准。
   - 否则只用可靠的公开答案，至少两个独立来源一致才采用；不一致或查不到的题，answer 留空，并在对话里告诉我是哪几题。
   - answer_source 写清楚答案来自哪里；非官方答案要写“非官方参考答案”。
   - 解析写简短中文，说明依据在原文哪一句；explanation_source 写 ai。翻译、写作题的 reference 是你写的参考译文/范文，同样标 ai。
5. 写完后自查（程序收取时会自动核对同样几项）：文章、题干、选项与 PDF 逐词一致；完形 [[n]] 与完形题号一致；翻译句能在原文里找到；题号没有缺漏；答案都在选项里。
6. 写好后核对：在项目文件夹运行 `python -m shared.scripts.chatgpt_work exam check "{folder}"`
   （Mac 用 `.venv/bin/python`；不能运行命令时就告诉我，我在控制中心点“核对”）。程序会逐词对照 PDF：
   - 有问题时程序会写 `CHECK_REPORT.md`。读它，逐条回到 PDF 改正 `EXAM.json`，再核对，直到通过。
   - 确实是原卷本身的问题（如印刷错误）就保留原样，并在对话里说明。
   - 核对通过后把预览（题数、题型、新增/更新、答案来源、留空的题）告诉我，等我确认后才导入。

## 不要做的事

- 不要编造答案，不要为了通过核对改动原卷文字。
- 不要只写一部分；整套试卷是一个 JSON 对象。
""")
        return self.exam_import_task(folder)

    def check_exam_import_draft(self, task_dir: Path | str) -> dict[str, Any]:
        """Check EXAM.json against the PDF; write CHECK_REPORT.md for ChatGPT Work when it has problems."""

        from shared.scripts.english_exam_paper import check_exam_paper, load_paper

        task = self.exam_import_task(task_dir)
        if not task["draft_exists"]:
            raise ValueError(f"ChatGPT Work 还没有写好 EXAM.json：\n{task['output_file']}")
        folder = Path(task["task_dir"])
        try:
            paper = load_paper(paper_file=task["output_file"])
        except (ValueError, json.JSONDecodeError) as error:
            check = {"problems": [f"EXAM.json 不是合法的试卷 JSON 对象：{error}"], "notes": []}
            paper = None
        else:
            check = check_exam_paper(paper, folder / "source.pdf")
        report = folder / "CHECK_REPORT.md"
        if check.get("problems"):
            lines = ["# EXAM.json 核对问题", "", "逐条修改 `EXAM.json` 后告诉我，我会再次核对。", ""]
            lines += [f"- {item}" for item in check["problems"]]
            mismatches = check.get("text_mismatches") or []
            if mismatches:
                lines += ["", "## 与 PDF 对不上的文字", ""]
                lines += [f"- {json.dumps(item, ensure_ascii=False)}" for item in mismatches[:80]]
            if check.get("notes"):
                lines += ["", "## 提醒（不一定要改）", ""] + [f"- {item}" for item in check["notes"]]
            self._write_task_text(report, "\n".join(lines) + "\n")
        else:
            report.unlink(missing_ok=True)
        preview = self.preview_exam_paper(paper) if paper is not None and not check.get("problems") else None
        return {**task, "paper": paper, "check": check, "preview": preview,
                "can_import": preview is not None and bool(preview.get("can_import"))}

    def import_exam_import_draft(self, task_dir: Path | str, *, to_inbox: bool) -> dict[str, Any]:
        """Import a checked EXAM.json (or put it in the import inbox), then move the draft aside."""

        checked = self.check_exam_import_draft(task_dir)
        if not checked["can_import"]:
            raise ValueError("EXAM.json 还没有通过核对，问题已写入 CHECK_REPORT.md。")
        paper = dict(checked["paper"])
        original = Path(checked["source_pdf"]) if checked["source_pdf"] else None
        if original is None or not original.is_file():
            original = Path(checked["task_dir"]) / "source.pdf"
        paper["source_pdf"] = str(original)
        if to_inbox:
            from shared.scripts.import_inbox import ImportInbox

            result = ImportInbox().send_exam_paper(paper=paper)
        else:
            result = self.import_exam_paper(paper)
        output = Path(checked["output_file"])
        archived = output.parent / "imported_drafts" / f"EXAM_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        archived.parent.mkdir(parents=True, exist_ok=True)
        os.replace(output, archived)
        return {**result, "archived_draft": str(archived), "to_inbox": bool(to_inbox)}

    def writing_grading_task(self, question_id: int) -> dict[str, Any]:
        question = self.question_bank_item(int(question_id))
        if question is None:
            raise ValueError(f"英语题库中没有 id={question_id} 的题目。")
        folder = self.root / "chatgpt_work" / "writing_grading" / re.sub(
            r"[^0-9A-Za-z._-]+", "_", str(question["question_code"])
        )
        task_file = folder / "TASK.md"
        feedback = folder / "FEEDBACK.md"
        return {
            "task_dir": str(folder),
            "task_file": str(task_file),
            "output_file": str(feedback),
            "task_ready": task_file.is_file(),
            "feedback_exists": feedback.is_file() and bool(feedback.read_text(encoding="utf-8").strip()),
            "feedback": feedback.read_text(encoding="utf-8") if feedback.is_file() else "",
            "task_line": f"请阅读 {task_file} 并按其中要求完成，结果只写入同一文件夹的 FEEDBACK.md。",
        }

    def prepare_writing_grading_task(self, question_id: int, response: str = "") -> dict[str, Any]:
        """Write the grading task for one writing answer; an earlier FEEDBACK.md moves to history/."""

        prompt = self.writing_grading_prompt(int(question_id), response)
        task = self.writing_grading_task(int(question_id))
        folder = Path(task["task_dir"])
        feedback = Path(task["output_file"])
        if feedback.is_file():
            history = folder / "history" / f"FEEDBACK_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.md"
            history.parent.mkdir(parents=True, exist_ok=True)
            os.replace(feedback, history)
        self._write_task_text(
            folder / "TASK.md",
            "# ChatGPT Work 任务：批改一道英文写作练习\n\n"
            "下面是完整的批改请求（题目、我的作答、原书范文、本章讲解要点和批改要求）。"
            "把批改结果用 Markdown 写入本文件所在文件夹的 `FEEDBACK.md`；"
            "请求里的“回答”都指 `FEEDBACK.md` 的内容。不要创建、修改或删除其他文件。\n\n"
            "## 批改请求\n\n" + prompt + "\n",
        )
        return self.writing_grading_task(int(question_id))

    def preview_exam_paper(self, paper: Any = None, *, paper_file: str | Path | None = None) -> dict[str, Any]:
        """Validate an exam-paper JSON and compare it with the bank, without writing."""

        from shared.scripts.english_exam_paper import build_exam_rows, load_paper

        built = build_exam_rows(load_paper(paper, paper_file))
        changes = self._exam_paper_changes(built)
        new, changed = changes["new"], changes["updated"]
        return {
            "exam": built["exam"], "year": built["year"], "paper_code": built["paper_code"],
            "title": built["title"], "paper_label": built["paper_label"],
            "question_count": len(built["questions"]), "group_count": len(built["groups"]),
            "by_type": built["by_type"],
            "by_category": {
                category: sum(1 for row in built["questions"] if row["category"] == category)
                for category in QUESTION_CATEGORIES
            },
            **changes, "unchanged": len(built["questions"]) - new - changed,
            "issues": built["issues"], "warnings": built["warnings"],
            "can_import": not built["issues"],
            "samples": [
                {"question_code": row["question_code"], "type": row["question_type"],
                 "prompt": row["prompt"][:200], "answer_text": row["answer_text"][:200]}
                for row in built["questions"][:: max(1, len(built["questions"]) // 6)][:6]
            ],
        }

    def import_exam_paper(self, paper: Any = None, *, paper_file: str | Path | None = None) -> dict[str, Any]:
        """Write one exam paper: passages into english_question_groups, questions into
        the reading/writing categories.  Backup, one transaction, digest readback;
        rows are upserted by code, so favourites and attempts survive a re-import."""

        from shared.scripts.english_exam_paper import build_exam_rows, load_paper

        built = build_exam_rows(load_paper(paper, paper_file))
        if built["issues"]:
            raise ValueError(
                f"试卷有 {len(built['issues'])} 处问题，未写入题库：\n" + "\n".join(built["issues"][:15])
            )
        changes = self._exam_paper_changes(built)
        if not changes["needs_import"]:
            return {
                **changes, "paper_code": built["paper_code"], "paper_label": built["paper_label"],
                "title": built["title"], "question_count": len(built["questions"]),
                "stored": len(built["questions"]), "group_count": len(built["groups"]),
                "stored_groups": len(built["groups"]), "by_type": built["by_type"],
                "warnings": built["warnings"], "backup_path": "", "readback_verified": True,
                "readback_mismatches": [],
            }
        backup = self.backup("exam_paper_" + built["paper_code"])
        group_columns = [
            "category", "group_code", "title", "passage", "content_json",
            "source_title", "source_path", "source_page",
        ]
        question_columns = [
            "category", "question_code", "title", "prompt", "passage", "answer_text",
            "explanation_text", "source_title", "source_path", "source_page", "source_line",
            "metadata_json", "question_type", "topic", "chapter_number", "source_label",
            "group_id", "sort_key", "content_json", "content_sha256", "exam", "exam_year",
        ]
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                group_ids: dict[str, int] = {}
                for group in built["groups"]:
                    connection.execute(
                        f"INSERT INTO english_question_groups({','.join(group_columns)}) "
                        f"VALUES({','.join('?' for _ in group_columns)}) "
                        "ON CONFLICT(category,group_code) DO UPDATE SET "
                        + ",".join(f"{name}=excluded.{name}" for name in group_columns
                                   if name not in {"category", "group_code"})
                        + ",updated_at=CURRENT_TIMESTAMP",
                        [group[name] for name in group_columns],
                    )
                    key = group["group_code"].rsplit("-", 1)[-1]
                    group_ids[key] = int(connection.execute(
                        "SELECT id FROM english_question_groups WHERE category=? AND group_code=?",
                        (group["category"], group["group_code"]),
                    ).fetchone()[0])
                for row in built["questions"]:
                    values = {**row, "group_id": group_ids.get(row["group_key"]) if row["group_key"] else None}
                    connection.execute(
                        f"INSERT INTO english_question_bank_items({','.join(question_columns)}) "
                        f"VALUES({','.join('?' for _ in question_columns)}) "
                        "ON CONFLICT(category,question_code) DO UPDATE SET "
                        + ",".join(f"{name}=excluded.{name}" for name in question_columns
                                   if name not in {"category", "question_code"})
                        + ",updated_at=CURRENT_TIMESTAMP",
                        [values[name] for name in question_columns],
                    )
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            codes = [row["question_code"] for row in built["questions"]]
            stored = {
                str(row["question_code"]): str(row["content_sha256"])
                for row in connection.execute(
                    f"SELECT question_code,content_sha256 FROM english_question_bank_items "
                    f"WHERE question_code IN ({','.join('?' for _ in codes)})",
                    codes,
                )
            }
            stored_groups = int(connection.execute(
                f"SELECT COUNT(*) FROM english_question_groups WHERE group_code IN "
                f"({','.join('?' for _ in built['groups']) or 'NULL'})",
                [group["group_code"] for group in built["groups"]],
            ).fetchone()[0])
        remaining = self._exam_paper_changes(built)
        mismatched = [row["question_code"] for row in built["questions"]
                      if stored.get(row["question_code"]) != row["content_sha256"]]
        return {
            **changes,
            "paper_code": built["paper_code"], "paper_label": built["paper_label"], "title": built["title"],
            "question_count": len(built["questions"]), "stored": len(stored),
            "group_count": len(built["groups"]), "stored_groups": stored_groups,
            "by_type": built["by_type"], "warnings": built["warnings"],
            "backup_path": str(backup or ""),
            "readback_verified": not mismatched and not remaining["needs_import"] and stored_groups == len(built["groups"]),
            "readback_mismatches": mismatched[:20],
        }

    @staticmethod
    def question_note_link(question: Mapping[str, Any]) -> dict[str, Any]:
        """The textbook-notes link (stable key, header title, page) for one question."""

        content = dict(question.get("content") or {})
        detail = " · ".join(part for part in (str(content.get("school") or ""), str(content.get("type_name") or "")) if part)
        if content.get("book") == RAYDU_WRITING_TITLE and content.get("location"):
            # 《英文写作》: "第一部分 第 4 章 一致性 · B 组 › 第 2 题（填空）".
            label = content.get("number_label") or ""
            title = f"{content['location']} › " + ("写作" if content.get("number_is_label") else f"第 {label} 题")
            detail = str(content.get("type_name") or "")
        elif content.get("book") == RAYDU_READING_TITLE and content.get("location"):
            # 《英文阅读》: "Chapter 1 · 主流阅读测验 · 阅读测验 ① › 第 3 题（字汇题）".
            title = f"{content['location']} › 第 {content.get('number_label') or content.get('question_number')} 题"
            detail = str(question.get("topic") or content.get("type_name") or "")
        elif content.get("paper_label") and content.get("question_number"):
            # Exam papers: "2025 英语一 › Part A Text 1 › 第 21 题（阅读选择）".
            title = " › ".join(part for part in (
                str(content["paper_label"]), str(content.get("section") or ""),
                f"第 {int(content['question_number'])} 题",
            ) if part)
            detail = str(content.get("type_name") or "")
        elif question.get("chapter_number") and content.get("question_number"):
            title = (
                f"第 {int(question['chapter_number'])} 章 {content.get('chapter_title') or ''} › "
                f"第 {int(content['question_number'])} 题"
            )
        else:
            title = str(question.get("title") or question.get("question_code") or "")
        if detail:
            title += f"（{detail}）"
        return {
            "question_key": f"english:{question['category']}:{question['question_code']}",
            "question_title": re.sub(r"\s+", " ", title).strip(),
            "book_title": str(question.get("source_title") or ""),
            "source_page": question.get("source_page"),
        }

    @staticmethod
    def question_prompt_text(question: Mapping[str, Any]) -> str:
        """The whole question as plain text for a ChatGPT note prompt."""

        def text(runs: Any) -> str:
            parts: list[str] = []
            for run in runs or []:
                if run.get("br") or run.get("line"):
                    parts.append("\n")
                elif run.get("blank"):
                    parts.append(f"__{run['n']}__" if run.get("n") else "_____")
                elif "box" in run:
                    parts.append(f"[{run['box']}]")
                elif "circ" in run:
                    parts.append(f"({run['circ']}) ")
                elif "pageref" in run:
                    parts.append(str(run.get("page") or ""))
                elif "table" in run:
                    parts.append("\n" + "\n".join(
                        " | ".join(text(cell) for cell in row) for row in run["table"]
                    ) + "\n")
                else:
                    value = str(run.get("t") or "")
                    if run.get("syn"):
                        value = f"{value} ({str(run['syn']).upper()})"
                    if run.get("u"):
                        value = f"<u>{value}</u>"
                    parts.append(value)
            return re.sub(r"[  ]+", " ", "".join(parts)).strip()

        content = dict(question.get("content") or {})
        link = EnglishLearningService.question_note_link(question)
        source = " · ".join(
            part for part in (
                "" if content.get("paper_code") else str(content.get("school") or ""),
                str(content.get("type_name") or ""),
                f"{'原卷' if content.get('paper_code') else '原书'}第 {int(question['source_page'])} 页"
                if question.get("source_page") else "",
            ) if part
        )
        lines = [f"题目：{link['book_title']} {link['question_title']}（{question['question_code']}）"]
        if source:
            lines.append(f"来源：{source}")
        if question.get("topic"):
            topic = str(question["topic"])
            if content.get("topic_note"):
                topic += f"（{content['topic_note']}）"
            label = "部分" if content.get("paper_code") else ("题型" if content.get("book") == RAYDU_READING_TITLE else "考点")
            lines.append(f"{label}：{topic}")
        group = dict(question.get("group") or {})
        group_content = dict(group.get("content") or {})
        if group_content.get("blocks"):
            # 《英文阅读》: the whole passage, with its option lists and tables.
            from shared.scripts.raydu_reading_parser import blocks_to_runs

            lines += ["", f"{group_content.get('title') or group.get('title') or '文章'}：",
                      text(blocks_to_runs(group_content["blocks"]))]
        if group_content.get("passage"):
            lines += ["", f"{group_content.get('title') or '文章'}：", text(group_content["passage"])]
        if group_content.get("options"):
            lines += ["", "候选段落："] + [
                f"{option['label']}. {text(option['runs'])}" for option in group_content["options"]
            ]
        if content.get("instruction"):
            lines += ["", "题目说明：", text(content["instruction"])]
        stem = text(content.get("stem")) or str(question.get("prompt") or "")
        lines += ["", "题干：", stem]
        table = content.get("table") or {}
        if table:
            lines += ["", str(table.get("caption") or "表格")]
            lines += [" | ".join(str(cell) for cell in table.get("header") or [])]
            lines += [" | ".join(str(cell) for cell in row) for row in table.get("rows") or []]
        options = content.get("options") or []
        if options:
            lines += ["", "选项："] + [f"({option['label']}) {text(option['runs'])}" for option in options]
        if not content.get("model_answer"):
            lines += ["", f"答案：{question.get('answer_text') or content.get('answer') or ''}"]
        if content.get("answer_note"):
            lines.append(f"（{content['answer_note']}）")
        explanation_label = "AI 解析" if content.get("explanation_source") == "ai" else (
            "原书解析" if content.get("chapter_title") or content.get("explanation_source") == "book" else "解析"
        )
        translation_label = "题干与选项的中文（原书）" if content.get("book") == RAYDU_READING_TITLE else "原书翻译"
        for label, key in (
            ("改正后的句子", "corrected"), (translation_label, "translation"),
            (str(content.get("reference_label") or "参考答案"), "model_answer"), ("原书译文", "model_answer_zh"),
            (explanation_label, "explanation"),
        ):
            value = text(content.get(key))
            if value:
                lines += ["", f"{label}：", value]
        if not content:
            lines += ["", "解析：", str(question.get("explanation_text") or "")]
        return "\n".join(lines).strip()

    def question_attempts(self, question_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                "SELECT * FROM english_question_attempts WHERE question_id=? "
                "ORDER BY attempted_at DESC, id DESC LIMIT ?",
                (int(question_id), max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def register_raydu_initial_sources(
        self,
        series_root: Path | str = DEFAULT_RAYDU_SERIES_ROOT,
        audio_root: Path | str | None = None,
    ) -> dict[str, Any]:
        """Register the three local Ray Du PDFs as read-only external references."""
        catalog = self.discover_raydu_initial_sources(series_root, audio_root)
        missing = [item["material_code"] for item in catalog["books"] if not item["exists"]]
        if missing:
            raise FileNotFoundError("未找到三本书原件：" + ", ".join(missing))
        backup = self.backup("raydu_initial_catalog")
        registered: list[dict[str, Any]] = []
        audio_resource: dict[str, Any] | None = None
        with self.connect(rows=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for item in catalog["books"]:
                    source = Path(str(item["source_path"]))
                    inspection = self.inspect_pdf_text_layer(source)
                    digest = _sha256(source)
                    update_cursor = connection.execute(
                        """
                        UPDATE english_materials
                        SET source_path=?, reading_path=?, source_kind='external_reference',
                            source_hash=?, text_layer_status=?, page_count=?, has_audio=?,
                            notes=CASE WHEN notes='' THEN ? ELSE notes END,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE material_code=?
                        """,
                        (
                            str(source), str(source), digest, inspection["status"],
                            int(inspection["page_count"]),
                            1 if item["material_code"] == "XY-VOCABULARY" and catalog["audio_exists"] else 0,
                            f"外部只读原件：{catalog['series_root']}", str(item["material_code"]),
                        ),
                    )
                    if update_cursor.rowcount != 1:
                        raise ValueError(f"预置材料不存在：{item['material_code']}")
                    row = connection.execute(
                        "SELECT * FROM english_materials WHERE material_code=?",
                        (str(item["material_code"]),),
                    ).fetchone()
                    assert row is not None
                    registered.append({
                        "material_code": str(item["material_code"]),
                        "title": str(row["title"]),
                        "source_path": str(row["source_path"]),
                        "text_layer_status": str(row["text_layer_status"]),
                        "page_count": int(row["page_count"]),
                        "source_kind": str(row["source_kind"]),
                    })
                if catalog["audio_exists"]:
                    vocabulary = connection.execute(
                        "SELECT id FROM english_materials WHERE material_code='XY-VOCABULARY'"
                    ).fetchone()
                    if vocabulary is not None:
                        existing_audio = connection.execute(
                            """
                            SELECT * FROM english_audio_resources
                            WHERE material_id=? AND resource_kind='external_directory' AND path_or_url=?
                            ORDER BY id LIMIT 1
                            """,
                            (int(vocabulary[0]), str(catalog["audio_root"])),
                        ).fetchone()
                        if existing_audio is None:
                            connection.execute(
                                """
                                INSERT INTO english_audio_resources(
                                    material_id,title,resource_kind,path_or_url,notes
                                ) VALUES(?,?,?,?,?)
                                """,
                                (
                                    int(vocabulary[0]), "旋元佑英文字汇音频目录", "external_directory",
                                    str(catalog["audio_root"]), "只登记目录；按词头匹配 MP3，原目录保持只读。",
                                ),
                            )
                            existing_audio = connection.execute(
                                "SELECT * FROM english_audio_resources WHERE id=?",
                                (int(connection.execute("SELECT last_insert_rowid()").fetchone()[0]),),
                            ).fetchone()
                        if existing_audio is not None:
                            audio_resource = dict(existing_audio)
                self._integrity(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "books": registered,
            "audio_root": catalog["audio_root"],
            "audio_exists": catalog["audio_exists"],
            "audio_resource": audio_resource,
            "backup_path": str(backup or ""),
            "readback_verified": True,
        }

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
        reading_count = int(counts[2])
        return {
            "material_count": int(counts[0]),
            "bound_material_count": int(counts[1]),
            "reading_count": reading_count,
            "usage_count": int(counts[3]),
            "grammar_encounter_count": int(counts[4]),
            "unresolved_misconception_count": int(counts[5]),
            "writing_practice_count": int(counts[6]),
            "recent_materials": [dict(row) for row in recent],
        }

    def learning_overview(self, *, books: bool = True) -> dict[str, Any]:
        """Read-only figures for the 总览 page: the reading progress of the three
        book pages, the question bank (answered, unresolved mistakes,
        favourites), the materials and the words marked for later lookup.

        Each part is read on its own; a part that cannot be read (a book
        project missing on this machine) carries an ``error`` instead of
        failing the whole overview.  ``books=False`` skips the books: a page's
        read count comes from its progress table and ``total`` is ``None``
        (parsing a book whose cache is out of date takes a second or two).
        """

        methods: list[dict[str, Any]] = []
        for page, reader, progress in (
            ("文法", self.grammar_method_outline, self.grammar_method_progress),
            ("阅读方法", self.reading_method_outline, self.reading_method_progress),
            ("写作方法", self.writing_method_outline, self.writing_method_progress),
        ):
            if not books:
                methods.append({"page": page, "book_title": "", "read": len(progress()), "total": None})
                continue
            try:
                outline = reader()
                methods.append({"page": page, "book_title": outline["book_title"], "read": outline["read_count"],
                                "total": len(outline["sections"])})
            except Exception as error:
                methods.append({"page": page, "book_title": "", "read": 0, "total": 0, "error": str(error)})
        with self.connect(rows=True) as connection:
            bank = connection.execute(
                """
                SELECT
                    (SELECT COUNT(DISTINCT question_id) FROM english_question_attempts),
                    (SELECT COUNT(*) FROM english_question_attempts),
                    (SELECT COUNT(*) FROM english_question_attempts WHERE is_correct=1),
                    (SELECT COUNT(DISTINCT question_id) FROM english_question_attempts
                     WHERE is_correct=0 AND resolved=0),
                    (SELECT COUNT(*) FROM english_question_favorites),
                    (SELECT COUNT(*) FROM english_deferred_lookups WHERE resolved=0)
                """
            ).fetchone()
        summary = self.summary()
        return {
            "methods": methods,
            "question_bank": {
                "counts": self.question_bank_counts(),
                "answered": int(bank[0]), "attempts": int(bank[1]), "correct": int(bank[2]),
                "mistakes": int(bank[3]), "favorites": int(bank[4]),
            },
            "materials": {
                "count": summary["material_count"], "reading": summary["reading_count"],
                "extensive": len(self.list_materials(role="extensive_reading")),
                "recent": [
                    {"id": int(item["id"]), "title": str(item.get("title") or ""),
                     "role": str(item.get("role") or ""), "last_opened_at": str(item.get("last_opened_at") or "")}
                    for item in summary["recent_materials"]
                ],
            },
            "deferred_lookups": int(bank[5]),
        }

    def reading_placement(self) -> dict[str, Any] | None:
        """Return the most recent extensive-reading placement, if one exists."""
        with self.connect(rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM english_reading_placement WHERE id=1"
            ).fetchone()
        return _row_dict(row)

    def save_reading_placement(
        self,
        *,
        passage_key: str,
        passage_level: str,
        correct_count: int,
        question_count: int,
        fluency: str,
        recommendation: str,
    ) -> dict[str, Any]:
        """Persist one auditable, replaceable placement result."""
        key = str(passage_key or "").strip()
        level = str(passage_level or "").strip()
        if not key or not level:
            raise ValueError("广读定位缺少文章或难度信息。")
        total = max(0, int(question_count))
        correct = min(max(0, int(correct_count)), total)
        backup = self.backup("reading_placement")
        with self.connect(rows=True) as connection:
            connection.execute(
                """
                INSERT INTO english_reading_placement(
                    id,passage_key,passage_level,correct_count,question_count,
                    fluency,recommendation,assessed_at
                ) VALUES(1,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    passage_key=excluded.passage_key,
                    passage_level=excluded.passage_level,
                    correct_count=excluded.correct_count,
                    question_count=excluded.question_count,
                    fluency=excluded.fluency,
                    recommendation=excluded.recommendation,
                    assessed_at=CURRENT_TIMESTAMP
                """,
                (key, level, correct, total, str(fluency), str(recommendation)),
            )
            self._integrity(connection)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM english_reading_placement WHERE id=1"
            ).fetchone()
        result = dict(row)
        result["backup_path"] = str(backup or "")
        return result

    @staticmethod
    def extensive_reading_topics() -> list[dict[str, Any]]:
        """Return the curated topic/article metadata used by the placement UI."""
        source_by_key = {str(item["key"]): item for item in EXTENSIVE_READING_SOURCES}
        topics: list[dict[str, Any]] = []
        for topic in EXTENSIVE_READING_TOPICS:
            copied = dict(topic)
            articles: list[dict[str, Any]] = []
            for article in topic.get("articles") or ():
                item = dict(article)
                source = source_by_key.get(str(item.get("source_key") or ""), {})
                item["source_name"] = str(source.get("title") or "公开来源")
                ebook_id = int(item.get("gutenberg_id") or 0)
                if ebook_id:
                    # Project Gutenberg keeps stable, downloadable EPUB and
                    # plain-text paths alongside the attribution page.  The
                    # reader uses EPUB by default; TXT remains available for
                    # small devices and users who prefer a plain text source.
                    item["source_url"] = f"https://www.gutenberg.org/ebooks/{ebook_id}"
                    item["download_url"] = (
                        f"https://www.gutenberg.org/cache/epub/{ebook_id}/"
                        f"pg{ebook_id}-images.epub"
                    )
                    item["download_text_url"] = (
                        f"https://www.gutenberg.org/cache/epub/{ebook_id}/pg{ebook_id}.txt"
                    )
                    item["download_text_fallback_url"] = (
                        f"https://www.gutenberg.org/files/{ebook_id}/{ebook_id}-0.txt"
                    )
                    item["download_format"] = "EPUB"
                else:
                    item["source_url"] = str(item.get("article_url") or source.get("url") or "")
                    item["download_url"] = str(item.get("download_url") or "")
                    item["download_text_url"] = str(item.get("download_text_url") or "")
                    item["download_text_fallback_url"] = str(item.get("download_text_fallback_url") or "")
                    item["download_format"] = str(item.get("download_format") or "")
                sample_raw = Path(str(item.get("local_sample_path") or ""))
                if sample_raw:
                    sample_path = sample_raw if sample_raw.is_absolute() else (ROOT_DIR / sample_raw)
                    item["local_sample_available"] = sample_path.is_file()
                    if sample_path.is_file():
                        try:
                            sample_text = EnglishLearningService.extract_text(sample_path)
                            item["local_sample_word_count"] = len(
                                re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", sample_text)
                            )
                        except (OSError, ValueError, zipfile.BadZipFile):
                            item["local_sample_available"] = False
                            item["local_sample_word_count"] = 0
                else:
                    item["local_sample_available"] = False
                    item["local_sample_word_count"] = 0
                articles.append(item)
            copied["articles"] = articles
            topics.append(copied)
        return topics

    def ensure_local_placement_material(self, passage_key: str) -> dict[str, Any]:
        """Register one bundled placement sample in the normal local reader.

        Curated samples are shipped with the project as text files.  The first
        time a learner opens one, it is copied through ``import_material`` so
        the same selectable PDF reader, reading position, vocabulary capture,
        and Agent context are used as for every other English material.
        """
        key = str(passage_key or "").strip()
        passage: dict[str, Any] | None = None
        for topic in self.extensive_reading_topics():
            for article in topic.get("articles") or ():
                if str(article.get("key") or "") == key:
                    passage = dict(article)
                    break
            if passage is not None:
                break
        if passage is None:
            raise ValueError("广读定位文章不存在。")
        raw_path = Path(str(passage.get("local_sample_path") or ""))
        source = (raw_path if raw_path.is_absolute() else ROOT_DIR / raw_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"本地定位文章不存在：{source}")
        text = self.extract_text(source)
        word_count = len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", text))
        if word_count < 1000:
            raise ValueError(
                f"本地定位文章需要至少 1,000 个英文词；当前约 {word_count:,} 词。"
            )
        digest = _sha256(source)
        with self.connect(rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM english_materials WHERE source_hash=? AND source_hash<>''",
                (digest,),
            ).fetchone()
        if row is not None:
            result = dict(row)
            result.update({
                "local_sample_path": str(source),
                "word_count": word_count,
                "created": False,
                "readback_verified": True,
            })
            return result

        material_code = "ER-PLACEMENT-" + _safe_component(key, "sample").upper()
        result = self.import_material(
            source,
            title=str(passage.get("title") or source.stem),
            role="extensive_reading",
            material_type="placement_sample",
            author=str(passage.get("author") or ""),
            source_url=str(passage.get("source_url") or ""),
            material_code=material_code,
            enforce_long_reading=True,
        )
        result.update({
            "local_sample_path": str(source),
            "word_count": word_count,
            "created": True,
            "readback_verified": True,
        })
        return result

    def extensive_reading_profile(self) -> dict[str, Any] | None:
        with self.connect(rows=True) as connection:
            profile = connection.execute(
                "SELECT * FROM english_reading_profiles WHERE id=1"
            ).fetchone()
            if profile is None:
                return None
            entries = connection.execute(
                "SELECT * FROM english_reading_profile_entries WHERE run_id=? ORDER BY id",
                (str(profile["run_id"]),),
            ).fetchall()
        result = dict(profile)
        try:
            result["selected_topics"] = json.loads(str(result.get("selected_topics_json") or "[]"))
        except json.JSONDecodeError:
            result["selected_topics"] = []
        result["entries"] = [dict(row) for row in entries]
        return result

    def save_extensive_reading_profile(
        self,
        *,
        selected_topics: list[str] | tuple[str, ...],
        reflections: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    ) -> dict[str, Any]:
        """Persist reflection-only placement data and create a Markdown profile."""
        catalog = self.extensive_reading_topics()
        topic_index = {str(topic["key"]): topic for topic in catalog}
        selected = [str(key).strip() for key in selected_topics if str(key).strip() in topic_index]
        if not selected:
            raise ValueError("请至少选择一个广读主题。")
        article_index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for topic in catalog:
            for article in topic.get("articles") or ():
                article_index[str(article["key"])] = (topic, article)
        normalized: list[dict[str, Any]] = []
        for raw in reflections:
            item = dict(raw)
            article_key = str(item.get("article_key") or "").strip()
            pair = article_index.get(article_key)
            if pair is None or str(pair[0]["key"]) not in selected:
                continue
            understanding = str(item.get("understanding") or "").strip()
            fluency = str(item.get("fluency") or "").strip()
            interest = str(item.get("interest") or "").strip()
            if not understanding or not fluency or not interest:
                raise ValueError("每篇文章都需要填写看懂程度、阅读状态和兴趣程度。")
            topic, article = pair
            normalized.append({
                "topic_key": str(topic["key"]),
                "topic_title": str(topic.get("title") or ""),
                "article_key": article_key,
                "difficulty": str(article.get("difficulty") or ""),
                "title": str(article.get("title") or ""),
                "source_url": str(article.get("source_url") or ""),
                "word_count": max(
                    1000,
                    int(article.get("local_sample_word_count") or article.get("word_count") or 0),
                ),
                "understanding": understanding,
                "fluency": fluency,
                "interest": interest,
                "reflection": str(item.get("reflection") or "").strip(),
            })
        if not normalized:
            raise ValueError("请至少完成一篇文章的阅读反思。")

        level_order = {"低": 1, "中": 2, "高": 3}
        understanding_score = {"clear": 2, "gist": 1, "hard": 0}
        fluency_score = {"smooth": 2, "pauses": 1, "backtrack": 0}
        qualified = [
            level_order.get(item["difficulty"], 0)
            for item in normalized
            if understanding_score.get(item["understanding"], 0) >= 1
            and fluency_score.get(item["fluency"], 0) >= 1
        ]
        level_number = max(qualified) if qualified else 1
        inferred_level = {1: "从低难度开始", 2: "可从中等难度开始", 3: "可尝试高难度"}[level_number]
        run_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        created = datetime.now().strftime("%Y-%m-%d %H:%M")
        topics_text = ", ".join(str(topic_index[key].get("title") or key) for key in selected)
        lines = [
            "# 广读学习画像",
            "",
            f"> 生成时间：{created}",
            "> 本报告来自用户完成整篇长文后的自我观察，不是理解题分数。",
            "",
            "## 自测结论",
            "",
            f"- 感兴趣主题：{topics_text}",
            f"- 当前建议：**{inferred_level}**",
            "- 广读规则：整篇阅读时不查词、不暂停、不回头；难度以能否抓住大意并读得顺为准。",
            "",
            "## 文章反思",
            "",
            "| 主题 | 难度 | 文章 | 字数 | 看懂程度 | 阅读状态 | 兴趣 |",
            "| --- | --- | --- | ---: | --- | --- | --- |",
        ]
        understanding_labels = {"clear": "主旨清楚", "gist": "大意但有断点", "hard": "基本抓不到"}
        fluency_labels = {"smooth": "一气呵成", "pauses": "有几处停顿", "backtrack": "需要停顿或回读"}
        interest_labels = {"like": "很喜欢", "okay": "一般", "dislike": "不感兴趣"}
        for item in normalized:
            lines.append(
                f"| {item['topic_title']} | {item['difficulty']} | {item['title']} | {item['word_count']} | "
                f"{understanding_labels.get(item['understanding'], item['understanding'])} | "
                f"{fluency_labels.get(item['fluency'], item['fluency'])} | "
                f"{interest_labels.get(item['interest'], item['interest'])} |"
            )
        lines.extend(["", "## 阅读感受", ""])
        for item in normalized:
            lines.append(f"### {item['title']}")
            lines.append("")
            lines.append(item["reflection"] or "（未填写文字感受）")
            lines.append(f"来源：{item['source_url']}")
            lines.append("")
        lines.extend([
            "## 可交给 ChatGPT 的检索提示",
            "",
            "请根据这份广读学习画像，围绕我感兴趣的主题，寻找可合法下载的英文长文章或有声材料。",
            f"我的主题是：{topics_text}。目前建议从{inferred_level}的材料开始。",
            "材料需是完整连贯的真人文章，优先 1000 词以上；可以是科普、文化、社会或生活议题，避免论文、教材和需要逐句精读的专业材料。请给出原始网址、下载格式、音频（如有）、版权/授权说明与难度理由。",
        ])
        markdown = "\n".join(lines).strip() + "\n"
        reports_dir = self.root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"extensive_reading_profile_{run_id}.md"
        temporary = report_path.with_suffix(".md.tmp")
        temporary.write_text(markdown, encoding="utf-8")
        temporary.replace(report_path)
        if report_path.read_text(encoding="utf-8") != markdown:
            raise RuntimeError("广读 Markdown 报告写入校验失败。")

        backup = self.backup("reading_profile")
        with self.connect(rows=True) as connection:
            connection.execute("DELETE FROM english_reading_profile_entries")
            for item in normalized:
                connection.execute(
                    """
                    INSERT INTO english_reading_profile_entries(
                        run_id,topic_key,topic_title,article_key,difficulty,title,source_url,word_count,
                        understanding,fluency,interest,reflection
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id, item["topic_key"], item["topic_title"], item["article_key"],
                        item["difficulty"], item["title"], item["source_url"], item["word_count"],
                        item["understanding"], item["fluency"], item["interest"], item["reflection"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO english_reading_profiles(
                    id,run_id,selected_topics_json,inferred_level,report_path,report_markdown,created_at
                ) VALUES(1,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    run_id=excluded.run_id, selected_topics_json=excluded.selected_topics_json,
                    inferred_level=excluded.inferred_level, report_path=excluded.report_path,
                    report_markdown=excluded.report_markdown, created_at=CURRENT_TIMESTAMP
                """,
                (run_id, json.dumps(selected, ensure_ascii=False), inferred_level, str(report_path), markdown),
            )
            self._integrity(connection)
            connection.commit()
            profile = connection.execute(
                "SELECT * FROM english_reading_profiles WHERE id=1"
            ).fetchone()
        if profile is None:
            raise RuntimeError("广读画像写入后无法回读。")
        result = dict(profile)
        result["backup_path"] = str(backup or "")
        result["report_path"] = str(report_path)
        result["selected_topics"] = selected
        result["entries"] = normalized
        return result

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
        return [self._material_result(row) for row in rows]

    def material(self, material_id: int) -> dict[str, Any] | None:
        with self.connect(rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM english_materials WHERE id=?", (int(material_id),)
            ).fetchone()
        return self._material_result(row)

    def material_by_code(self, code: str) -> dict[str, Any] | None:
        with self.connect(rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM english_materials WHERE material_code=?", (str(code),)
            ).fetchone()
        return self._material_result(row)

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
    def _docx_body_to_html(path: Path, extracted_root: Path) -> str:
        """Render the common DOCX layout primitives into Story HTML.

        A plain-text DOCX conversion destroys headings, emphasis, tables and
        inline figures.  This deliberately small OOXML reader keeps those
        primitives without adding a third-party office dependency.  The
        original DOCX is still retained beside the generated PDF, so an
        unsupported OOXML feature never replaces the user's source.
        """
        namespace = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        with zipfile.ZipFile(path) as archive:
            document_root = ElementTree.fromstring(archive.read("word/document.xml"))
            rels: dict[str, str] = {}
            try:
                rel_root = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
                for relation in rel_root:
                    rid = str(relation.attrib.get("Id") or "")
                    target = str(relation.attrib.get("Target") or "").replace("\\", "/")
                    if rid and target:
                        rels[rid] = posixpath.normpath(posixpath.join("word", target))
            except (KeyError, ElementTree.ParseError):
                pass

            def run_html(run: ElementTree.Element) -> str:
                text_parts: list[str] = []
                props = run.find("w:rPr", namespace)
                bold = props is not None and props.find("w:b", namespace) is not None
                italic = props is not None and props.find("w:i", namespace) is not None
                underline = props is not None and props.find("w:u", namespace) is not None
                for child in run:
                    local = child.tag.rsplit("}", 1)[-1]
                    if local == "t":
                        text_parts.append(html.escape(child.text or ""))
                    elif local == "tab":
                        text_parts.append("&emsp;")
                    elif local == "br":
                        text_parts.append("<br/>")
                    elif local == "drawing":
                        blip = child.find(".//a:blip", namespace)
                        rid = str(blip.attrib.get("{%s}embed" % namespace["r"]) or "") if blip is not None else ""
                        relative = rels.get(rid, "")
                        if relative and (extracted_root / relative).is_file():
                            text_parts.append(f'<img src="{html.escape(relative, quote=True)}"/>')
                value = "".join(text_parts)
                if bold:
                    value = f"<strong>{value}</strong>"
                if italic:
                    value = f"<em>{value}</em>"
                if underline:
                    value = f"<u>{value}</u>"
                return value

            def paragraph_html(paragraph: ElementTree.Element) -> str:
                props = paragraph.find("w:pPr", namespace)
                style = ""
                if props is not None:
                    style_node = props.find("w:pStyle", namespace)
                    style = str(style_node.attrib.get("{%s}val" % namespace["w"]) or "") if style_node is not None else ""
                content = "".join(run_html(run) for run in paragraph.findall("w:r", namespace))
                if not content.strip():
                    return "<p>&nbsp;</p>"
                heading_match = re.search(r"heading([1-6])", style, flags=re.IGNORECASE)
                if heading_match:
                    level = heading_match.group(1)
                    return f"<h{level}>{content}</h{level}>"
                return f"<p>{content}</p>"

            def table_html(table: ElementTree.Element) -> str:
                rows: list[str] = []
                for row in table.findall("w:tr", namespace):
                    cells: list[str] = []
                    for cell in row.findall("w:tc", namespace):
                        cell_content = "".join(
                            paragraph_html(item) if item.tag.rsplit("}", 1)[-1] == "p"
                            else table_html(item)
                            for item in cell
                        )
                        cells.append(f"<td>{cell_content}</td>")
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                return "<table>" + "".join(rows) + "</table>"

            body = document_root.find(".//w:body", namespace)
            if body is None:
                raise ValueError(f"Word 文档没有正文：{path.name}")
            blocks: list[str] = []
            for child in body:
                local = child.tag.rsplit("}", 1)[-1]
                if local == "p":
                    blocks.append(paragraph_html(child))
                elif local == "tbl":
                    blocks.append(table_html(child))
            return "".join(blocks)

    @staticmethod
    def build_reading_pdf_from_docx(
        source: Path | str,
        target: Path | str,
        title: str,
        author: str = "",
    ) -> Path:
        """Convert a DOCX into a selectable PDF while retaining basic layout."""
        source_path = Path(source).resolve()
        temporary_root = Path(tempfile.mkdtemp(prefix="english-docx-"))
        try:
            root_resolved = temporary_root.resolve()
            with zipfile.ZipFile(source_path) as archive:
                for member in archive.infolist():
                    destination = (temporary_root / member.filename).resolve()
                    if not destination.is_relative_to(root_resolved):
                        raise ValueError("DOCX 包含越界文件路径，已拒绝生成阅读副本。")
                    archive.extract(member, temporary_root)
            fragment = EnglishLearningService._docx_body_to_html(source_path, temporary_root)
            story_html = (
                "<html><body>"
                + EnglishLearningService._reading_title_html(title, author)
                + fragment
                + "</body></html>"
            )
            import fitz
            return EnglishLearningService._story_to_pdf(
                story_html,
                target,
                title=title,
                author=author,
                archive=fitz.Archive(str(temporary_root)),
            )
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    @staticmethod
    def _epub_document_order(archive: zipfile.ZipFile) -> list[str]:
        """Return EPUB reading documents in the OPF spine order.

        Filename order is not a reading order: publishers commonly number
        files by production order, and some EPUBs intentionally place an
        introduction or appendix elsewhere.  Fall back to a deterministic
        filename order only for malformed or minimal EPUB fixtures.
        """
        candidates = {
            name for name in archive.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm"))
        }
        fallback = sorted(candidates)
        try:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(
                element for element in container.iter()
                if element.tag.rsplit("}", 1)[-1] == "rootfile"
            )
            opf_name = str(rootfile.attrib.get("full-path") or "").replace("\\", "/")
            if not opf_name:
                return fallback
            opf = ElementTree.fromstring(archive.read(opf_name))
            manifest: dict[str, str] = {}
            for element in opf.iter():
                if element.tag.rsplit("}", 1)[-1] != "item":
                    continue
                item_id = str(element.attrib.get("id") or "")
                href = str(element.attrib.get("href") or "")
                media_type = str(element.attrib.get("media-type") or "").lower()
                if not item_id or not href:
                    continue
                if media_type in {"application/xhtml+xml", "text/html"} or href.lower().split("?", 1)[0].endswith((".xhtml", ".html", ".htm")):
                    href_path = urllib.parse.unquote(href.split("#", 1)[0]).replace("\\", "/")
                    manifest[item_id] = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name), href_path))
            ordered: list[str] = []
            for element in opf.iter():
                if element.tag.rsplit("}", 1)[-1] != "itemref":
                    continue
                item_path = manifest.get(str(element.attrib.get("idref") or ""))
                if item_path in candidates and item_path not in ordered:
                    ordered.append(item_path)
            ordered.extend(name for name in fallback if name not in ordered)
            return ordered or fallback
        except (KeyError, ElementTree.ParseError, OSError, ValueError):
            return fallback

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
        if suffix == ".epub":
            paragraphs: list[str] = []
            with zipfile.ZipFile(source) as archive:
                for name in EnglishLearningService._epub_document_order(archive):
                    parser = _VisibleTextExtractor()
                    parser.feed(archive.read(name).decode("utf-8", errors="replace"))
                    if parser.parts:
                        paragraphs.append("\n\n".join(parser.parts))
            if not paragraphs:
                raise ValueError(f"EPUB 中没有可读取的正文：{source.name}")
            return "\n\n".join(paragraphs).strip()
        raise ValueError("当前阅读导入支持 PDF、EPUB、TXT、Markdown、HTML、DOCX 和 TeX。")

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
    def _reading_css() -> str:
        """Stable styles shared by plain text, HTML and EPUB reader copies."""

        return """
        body { font-family: serif; font-size: 11.2pt; line-height: 1.46;
               color: #172033; background: #ffffff; }
        .reader-title { font-family: sans-serif; color: #163b65; font-size: 22pt;
                        line-height: 1.14; margin: 0 0 5pt 0; }
        .reader-author { font-family: sans-serif; color: #667085; font-size: 10.5pt;
                         margin: 0 0 17pt 0; }
        h1, h2, h3, h4 { font-family: sans-serif; color: #245b87;
                         line-height: 1.2; margin: 17pt 0 7pt 0; }
        h1 { font-size: 17pt; } h2 { font-size: 14pt; } h3 { font-size: 12.5pt; }
        p { margin: 0 0 8pt 0; text-align: justify; }
        .reader-title-block { border-bottom: 1.2pt solid #9fb5c8; margin-bottom: 15pt;
                              padding-bottom: 9pt; }
        blockquote { border-left: 3pt solid #b6c9da; margin: 8pt 0 10pt 12pt;
                     padding: 2pt 0 2pt 10pt; color: #344054; }
        ul, ol { margin: 4pt 0 9pt 20pt; }
        li { margin: 0 0 4pt 0; }
        img { max-width: 100%; max-height: 470pt; }
        figure { margin: 9pt 0 12pt 0; text-align: center; }
        figcaption { font-family: sans-serif; color: #667085; font-size: 9.5pt; }
        table { border-collapse: collapse; width: 100%; margin: 9pt 0 12pt 0;
                font-size: 10.2pt; }
        td, th { border: 0.55pt solid #b8c4d2; padding: 4pt 6pt; vertical-align: top; }
        th { background: #eaf1f7; color: #163b65; font-family: sans-serif; }
        pre, code { font-family: monospace; font-size: 9.2pt; }
        pre { background: #f5f7fa; border: 0.5pt solid #d0d5dd; padding: 7pt;
              white-space: pre-wrap; }
        a { color: #245b87; text-decoration: underline; }
        """

    @staticmethod
    def _reading_title_html(title: str, author: str = "") -> str:
        author_html = (
            f'<div class="reader-author">{html.escape(author)}</div>'
            if str(author or "").strip() else ""
        )
        return (
            '<div class="reader-title-block">'
            f'<div class="reader-title">{html.escape(str(title or "阅读材料"))}</div>'
            f'{author_html}'
            '</div>'
        )

    @staticmethod
    def _plain_text_to_reader_html(text: str, title: str, author: str = "") -> str:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        blocks: list[str] = []
        for raw_block in re.split(r"\n\s*\n", value):
            lines = [line.rstrip() for line in raw_block.split("\n")]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            if not lines:
                continue
            joined = "<br/>".join(html.escape(line) for line in lines)
            blocks.append(f"<p>{joined}</p>")
        return "<html><body>" + EnglishLearningService._reading_title_html(title, author) + "".join(blocks) + "</body></html>"

    @staticmethod
    def _story_to_pdf(
        story_html: str,
        target: Path | str,
        *,
        title: str,
        author: str = "",
        archive: Any = None,
    ) -> Path:
        """Flow a document continuously through pages without hard chunk breaks."""

        import fitz

        output = Path(target)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp.pdf")
        page_rect = fitz.paper_rect("a4")
        where = fitz.Rect(54, 54, page_rect.width - 54, page_rect.height - 56)
        writer = fitz.DocumentWriter(str(temporary))
        story = fitz.Story(
            html=story_html,
            user_css=EnglishLearningService._reading_css(),
            archive=archive,
        )
        page_count = 0
        try:
            for _ in range(20000):
                page_count += 1
                device = writer.begin_page(page_rect)
                more, _filled = story.place(where)
                story.draw(device)
                writer.end_page()
                if not more:
                    break
            else:
                raise RuntimeError("阅读材料分页超过安全上限，疑似存在无法收束的版式。")
        finally:
            writer.close()
            # PyMuPDF's Windows handle is released when the wrapper is
            # destroyed, not only when close() returns.
            del story
            del writer

        # Header/footer live in the page margin, leaving the Story flow untouched.
        decorated = output.with_suffix(output.suffix + ".decorated.pdf")
        document = fitz.open(str(temporary))
        try:
            clean_title = re.sub(r"\s+", " ", str(title or "阅读材料")).strip()
            for index, page in enumerate(document):
                page.insert_text(
                    (54, 31), clean_title[:92], fontname="helv", fontsize=8.2,
                    color=(0.35, 0.42, 0.50), overlay=True,
                )
                page.draw_line(
                    (54, 38), (page.rect.width - 54, 38),
                    color=(0.66, 0.74, 0.81), width=0.55, overlay=True,
                )
                footer = f"{index + 1} / {page_count}"
                page.insert_text(
                    (page.rect.width - 93, page.rect.height - 25), footer,
                    fontname="helv", fontsize=8.2, color=(0.35, 0.42, 0.50), overlay=True,
                )
            metadata = document.metadata or {}
            metadata.update({
                "title": str(title or "阅读材料"),
                "author": str(author or "English Learning Project"),
                "subject": "Selectable reading copy with preserved document structure",
            })
            document.set_metadata(metadata)
            document.save(str(decorated), garbage=4, deflate=True)
        finally:
            document.close()
        temporary.unlink(missing_ok=True)
        check = EnglishLearningService.inspect_pdf_text_layer(decorated)
        if check["status"] != "selectable":
            decorated.unlink(missing_ok=True)
            raise RuntimeError("生成的阅读 PDF 没有通过文字层与可选词检查。")
        os.replace(decorated, output)
        return output

    @staticmethod
    def build_reading_pdf(text: str, target: Path | str, title: str, author: str = "") -> Path:
        return EnglishLearningService._story_to_pdf(
            EnglishLearningService._plain_text_to_reader_html(text, title, author),
            target,
            title=title,
            author=author,
        )

    @staticmethod
    def _rewrite_local_html_assets(fragment: str, source_file: Path, archive_root: Path) -> str:
        """Make local image/SVG references resolvable inside a Story archive."""

        def replace(match: re.Match[str]) -> str:
            prefix, value, suffix = match.group(1), match.group(2), match.group(3)
            raw = html.unescape(value).strip()
            if not raw or raw.startswith(("http:", "https:", "data:", "#")):
                return match.group(0)
            try:
                resolved = (source_file.parent / urllib.parse.unquote(raw.split("#", 1)[0])).resolve()
                relative = resolved.relative_to(archive_root.resolve()).as_posix()
            except (OSError, ValueError):
                return match.group(0)
            return prefix + html.escape(relative, quote=True) + suffix

        return re.sub(
            r'((?:src|xlink:href)\s*=\s*["\'])(.*?)(["\'])',
            replace,
            fragment,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _html_body_fragment(source: Path, archive_root: Path) -> str:
        raw = source.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"<body\b[^>]*>(.*?)</body\s*>", raw, flags=re.IGNORECASE | re.DOTALL)
        fragment = match.group(1) if match else raw
        fragment = re.sub(r"<script\b.*?</script\s*>", "", fragment, flags=re.IGNORECASE | re.DOTALL)
        fragment = re.sub(r"<style\b.*?</style\s*>", "", fragment, flags=re.IGNORECASE | re.DOTALL)
        return EnglishLearningService._rewrite_local_html_assets(fragment, source, archive_root)

    @staticmethod
    def build_reading_pdf_from_html(
        source: Path | str,
        target: Path | str,
        title: str,
        author: str = "",
    ) -> Path:
        source_path = Path(source).resolve()
        fragment = EnglishLearningService._html_body_fragment(source_path, source_path.parent)
        story_html = (
            "<html><body>"
            + EnglishLearningService._reading_title_html(title, author)
            + fragment
            + "</body></html>"
        )
        import fitz
        return EnglishLearningService._story_to_pdf(
            story_html, target, title=title, author=author,
            archive=fitz.Archive(str(source_path.parent)),
        )

    @staticmethod
    def build_reading_pdf_from_epub(
        source: Path | str,
        target: Path | str,
        title: str,
        author: str = "",
    ) -> Path:
        source_path = Path(source).resolve()
        temporary_root = Path(tempfile.mkdtemp(prefix="english-epub-"))
        try:
            with zipfile.ZipFile(source_path) as archive:
                root_resolved = temporary_root.resolve()
                for member in archive.infolist():
                    destination = (temporary_root / member.filename).resolve()
                    if not destination.is_relative_to(root_resolved):
                        raise ValueError("EPUB 包含越界文件路径，已拒绝生成阅读副本。")
                    archive.extract(member, temporary_root)
            fragments: list[str] = []
            with zipfile.ZipFile(source_path) as source_archive:
                ordered_names = EnglishLearningService._epub_document_order(source_archive)
            for name in ordered_names:
                item = (temporary_root / name).resolve()
                if not item.is_file():
                    continue
                fragment = EnglishLearningService._html_body_fragment(item, temporary_root)
                if fragment.strip():
                    fragments.append(fragment)
            if not fragments:
                raise ValueError(f"EPUB 中没有可读取的正文：{source_path.name}")
            story_html = (
                "<html><body>"
                + EnglishLearningService._reading_title_html(title, author)
                + "".join(fragments)
                + "</body></html>"
            )
            import fitz
            return EnglishLearningService._story_to_pdf(
                story_html, target, title=title, author=author,
                archive=fitz.Archive(str(temporary_root)),
            )
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

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

    @staticmethod
    def _download_extension(
        download_url: str,
        content_disposition: str = "",
        content_type: str = "",
    ) -> str:
        """Resolve a safe supported extension without trusting a remote name."""
        filename = ""
        match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^;\"]+)", content_disposition, re.I)
        if match:
            filename = urllib.parse.unquote(match.group(1).strip().strip('"'))
        path_name = urllib.parse.urlparse(download_url).path
        extension = Path(filename or path_name).suffix.lower()
        if extension == ".markdown":
            extension = ".md"
        if not extension:
            media_type = str(content_type or "").split(";", 1)[0].strip().lower()
            extension = {
                "application/pdf": ".pdf",
                "application/epub+zip": ".epub",
                "text/plain": ".txt",
                "text/markdown": ".md",
                "text/html": ".html",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                "application/x-tex": ".tex",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/flac": ".flac",
                "audio/ogg": ".ogg",
            }.get(media_type, "")
        return extension

    def _download_public_file(
        self,
        download_url: str,
        *,
        title: str,
        allowed_extensions: set[str],
        timeout: int = 90,
    ) -> Path:
        """Download one public file into English/downloads atomically.

        This is deliberately a small standard-library downloader so a public
        EPUB/TXT/PDF or audio file can be made local before any reader or
        dictionary operation touches it.  It never follows a browser page as
        if it were an article; the URL must resolve to a supported file.
        """
        url = str(download_url or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("下载地址必须是完整的 HTTP 或 HTTPS 文件直链。")
        temporary = self.download_dir / f".{_safe_component(title, 'download')}.part"
        extension = ""
        target: Path | None = None
        temporary.unlink(missing_ok=True)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "EnglishLearningProject/1.0 (local reading import)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                extension = self._download_extension(
                    url,
                    str(response.headers.get("Content-Disposition") or ""),
                    str(response.headers.get("Content-Type") or ""),
                )
                if extension not in allowed_extensions:
                    raise ValueError(
                        "下载地址没有返回支持的文件格式；请使用 PDF、EPUB、TXT、Markdown、HTML、DOCX、TeX 或音频直链。"
                    )
                length_header = response.headers.get("Content-Length")
                if length_header and int(length_header) > 256 * 1024 * 1024:
                    raise ValueError("单个下载文件不能超过 256 MB。")
                with temporary.open("wb") as stream:
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 256 * 1024 * 1024:
                            raise ValueError("单个下载文件不能超过 256 MB。")
                        stream.write(chunk)
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise RuntimeError("下载完成但没有得到有效文件。")
            target = self.download_dir / (
                f"{_safe_component(title, 'download')}-{_sha256(temporary)[:12]}{extension}"
            )
            os.replace(temporary, target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            if target is not None:
                target.unlink(missing_ok=True)
            raise

    def download_and_import_material(
        self,
        download_url: str,
        *,
        fallback_urls: list[str] | tuple[str, ...] = (),
        title: str = "",
        role: str = "extensive_reading",
        material_type: str = "document",
        author: str = "",
        source_url: str = "",
        enforce_long_reading: bool = True,
    ) -> dict[str, Any]:
        """Download a public document, keep it locally, and import its reader copy."""
        clean_title = str(title or "公开广读材料").strip()
        candidates = [str(download_url or "").strip()]
        candidates.extend(str(item or "").strip() for item in fallback_urls)
        source: Path | None = None
        selected_url = ""
        last_error: Exception | None = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                source = self._download_public_file(
                    candidate,
                    title=clean_title,
                    allowed_extensions={".pdf", ".epub", ".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".tex"},
                )
                selected_url = candidate
                break
            except Exception as error:
                last_error = error
        if source is None:
            raise RuntimeError(f"文件直链下载失败：{last_error or '没有可用地址'}")
        try:
            result = self.import_material(
                source,
                title=clean_title,
                role=role,
                material_type=material_type,
                author=author,
                source_url=str(source_url or selected_url),
                enforce_long_reading=enforce_long_reading,
            )
        except Exception:
            source.unlink(missing_ok=True)
            raise
        result.update({
            "download_path": str(source),
            "download_url": selected_url,
            "readback_verified": True,
        })
        return result

    def download_and_import_audio(
        self,
        download_url: str,
        *,
        title: str = "公开音频",
        material_id: int | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Download a public audio file, retain it, and register a local copy."""
        clean_title = str(title or "公开音频").strip()
        source = self._download_public_file(
            download_url,
            title=clean_title,
            allowed_extensions={".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".wma"},
        )
        try:
            result = self.import_local_audio(source, title=clean_title, material_id=material_id, notes=notes)
        except Exception:
            source.unlink(missing_ok=True)
            raise
        result.update({"download_path": str(source), "download_url": str(download_url), "readback_verified": True})
        return result

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
        enforce_long_reading: bool = False,
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        source_suffix = source.suffix.lower()
        if source_suffix not in {".pdf", ".epub", ".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".tex"}:
            raise ValueError("不支持的英语文章格式。")
        title = str(title or source.stem).strip()
        digest = _sha256(source)
        article_text = ""
        imported_word_count = 0
        if role.strip() == "extensive_reading" and enforce_long_reading:
            if source.suffix.lower() == ".pdf":
                import fitz

                with fitz.open(source) as document:
                    article_text = "\n".join(page.get_text("text") for page in document)
            else:
                article_text = self.extract_text(source)
            imported_word_count = len(re.findall(r"[A-Za-z]+", article_text))
            if imported_word_count < 1000:
                raise ValueError(
                    f"广读材料需要完整长文（至少 1,000 个英文词）；当前约 {imported_word_count:,} 词。"
                )
        backup = self.backup("material_import")
        with self.connect() as connection:
            duplicate = connection.execute(
                "SELECT id FROM english_materials WHERE source_hash=? AND source_hash<>''",
                (digest,),
            ).fetchone()
            if duplicate is not None:
                raise ValueError(f"相同原件已经导入，材料 ID={int(duplicate[0])}。")
            requested_code = material_code.strip()
            if requested_code:
                existing_code = connection.execute(
                    "SELECT id FROM english_materials WHERE material_code=?",
                    (requested_code,),
                ).fetchone()
                if existing_code is not None:
                    raise ValueError(f"材料编号已经存在：{requested_code}。请换用新的编号。")
            code = requested_code or self._next_material_code(connection)
            program_id = None
            if program_code.strip():
                row = connection.execute(
                    "SELECT id FROM english_programs WHERE program_code=?", (program_code.strip(),)
                ).fetchone()
                if row is None:
                    raise ValueError(f"英语课程体系不存在：{program_code}")
                program_id = int(row[0])
        safe = _safe_component(code)
        original = self.original_dir / f"{safe}{source_suffix}"
        reading_path = original
        temporary_original = original.with_suffix(original.suffix + ".tmp")
        try:
            temporary_original.unlink(missing_ok=True)
            shutil.copy2(source, temporary_original)
            os.replace(temporary_original, original)
            if source_suffix == ".pdf":
                inspection = self.inspect_pdf_text_layer(original)
            else:
                reading_path = self.reading_dir / f"{safe}.pdf"
                if source_suffix == ".epub":
                    self.build_reading_pdf_from_epub(original, reading_path, title, author)
                elif source_suffix in {".html", ".htm"}:
                    # Build from the user's original directory so relative image
                    # and stylesheet assets beside an HTML page remain available;
                    # the byte-for-byte source copy is still stored separately.
                    self.build_reading_pdf_from_html(source, reading_path, title, author)
                elif source_suffix == ".docx":
                    self.build_reading_pdf_from_docx(original, reading_path, title, author)
                else:
                    self.build_reading_pdf(article_text or self.extract_text(original), reading_path, title, author)
                inspection = self.inspect_pdf_text_layer(reading_path)
        except Exception:
            temporary_original.unlink(missing_ok=True)
            original.unlink(missing_ok=True)
            if reading_path != original:
                reading_path.unlink(missing_ok=True)
            raise
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
        if role.strip() == "extensive_reading" and enforce_long_reading:
            result["word_count"] = imported_word_count
        return result

    def bind_material_file(self, material_id: int, source_path: Path | str) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        material = self.material(material_id)
        if material is None:
            raise ValueError("英语材料不存在。")
        if not source.is_file():
            raise FileNotFoundError(source)
        source_suffix = source.suffix.lower()
        if source_suffix not in {".pdf", ".epub", ".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".tex"}:
            raise ValueError("不支持的英语文章格式。")
        old_source = str(material.get("source_path") or "")
        old_reading = str(material.get("reading_path") or "")
        backup = self.backup("material_rebind")
        safe = _safe_component(str(material["material_code"]))
        original = self.original_dir / f"{safe}{source_suffix}"
        tmp = original.with_suffix(original.suffix + ".tmp")
        reading = original
        owned_paths = {str(original), str(reading)} - {old_source, old_reading}
        try:
            tmp.unlink(missing_ok=True)
            shutil.copy2(source, tmp)
            os.replace(tmp, original)
            if source_suffix == ".pdf":
                inspection = self.inspect_pdf_text_layer(original)
            else:
                reading = self.reading_dir / f"{safe}.pdf"
                owned_paths = {str(original), str(reading)} - {old_source, old_reading}
                if source_suffix == ".epub":
                    self.build_reading_pdf_from_epub(
                        original, reading, str(material["title"]), str(material.get("author") or "")
                    )
                elif source_suffix in {".html", ".htm"}:
                    self.build_reading_pdf_from_html(
                        source, reading, str(material["title"]), str(material.get("author") or "")
                    )
                elif source_suffix == ".docx":
                    self.build_reading_pdf_from_docx(
                        original, reading, str(material["title"]), str(material.get("author") or "")
                    )
                else:
                    self.build_reading_pdf(
                        self.extract_text(original), reading, str(material["title"]), str(material.get("author") or "")
                    )
                inspection = self.inspect_pdf_text_layer(reading)
        except Exception:
            tmp.unlink(missing_ok=True)
            for path in owned_paths:
                Path(path).unlink(missing_ok=True)
            raise
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
                tmp.unlink(missing_ok=True)
                for path in owned_paths:
                    Path(path).unlink(missing_ok=True)
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
        if session_id is not None and session_id < 0:
            session_id = None  # Local reading sessions have no formal foreign key.
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

    def import_local_audio(
        self,
        source_path: Path | str,
        *,
        title: str = "",
        material_id: int | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Copy a user-selected audio file into the English project and register it."""
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".wma"}:
            raise ValueError("不支持的音频格式。")
        digest = _sha256(source)
        target = self.audio_dir / f"{_safe_component(title or source.stem)}-{digest[:12]}{source.suffix.lower()}"
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        try:
            result = self.add_audio_resource(
                title or source.stem,
                str(target),
                material_id=material_id,
                resource_kind="local_file",
                notes=notes,
            )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        result["original_path"] = str(source)
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
        status = EnglishSpeechService.status()
        status["command"] = shutil.which("powershell") or shutil.which("pwsh") or ""
        return status

    def play_speech(
        self,
        text: str,
        *,
        mode: str = "document",
        on_status: Any = None,
        on_done: Any = None,
    ) -> threading.Thread:
        return self.speech_service.play(
            text, mode=mode, on_status=on_status, on_done=on_done
        )

    def stop_speech(self) -> None:
        self.speech_service.stop()

    def pause_speech(self) -> bool:
        return self.speech_service.pause()

    def resume_speech(self) -> bool:
        return self.speech_service.resume()

    def set_speech_speed(self, speed: float) -> float:
        return self.speech_service.set_speed(speed)

    def speech_speed(self) -> float:
        return self.speech_service.speed()

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
    "EnglishSpeechService",
    "EnglishLearningService",
    "XUAN_BOOKS",
    "EXTENSIVE_READING_TOPICS",
    "EXTENSIVE_READING_SOURCES",
    "GRAMMAR_CHAPTERS",
    "ENGLISH_SCHEMA",
]
