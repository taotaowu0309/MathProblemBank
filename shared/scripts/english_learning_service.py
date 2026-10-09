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

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENGLISH_ROOT = ROOT_DIR / "English"
DEFAULT_DATABASE = DEFAULT_ENGLISH_ROOT / "data" / "english_learning.db"
DEFAULT_RAYDU_SERIES_ROOT = APP_PATHS.user_data_root / "external" / "RayDuSeries"
DEFAULT_RAYDU_AUDIO_ROOT = DEFAULT_RAYDU_SERIES_ROOT / "RayDu_Vocabulary" / "audio"


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
        if os.name != "nt" or process.poll() is not None:
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

        def worker() -> None:
            status = self.status()
            try:
                import winsound
            except ImportError:
                winsound = None  # type: ignore[assignment]
            try:
                for index, chunk in enumerate(chunks, start=1):
                    if stop_event.is_set():
                        break
                    while not self._resume_event.wait(0.1):
                        if stop_event.is_set():
                            return
                    if callable(on_status):
                        on_status(f"正在朗读第 {index} / {len(chunks)} 段")
                    if status["backend"].startswith("免费 Edge"):
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
                            stderr=subprocess.DEVNULL,
                            env=self._edge_environment(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        with self._process_lock:
                            self._active_process = process
                        while process.poll() is None:
                            if stop_event.wait(0.1):
                                process.terminate()
                                break
                            if not self._resume_event.is_set():
                                self._suspend_windows_process(process, True)
                                while not self._resume_event.wait(0.1):
                                    if stop_event.is_set():
                                        process.terminate()
                                        break
                        with self._process_lock:
                            if self._active_process is process:
                                self._active_process = None
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
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        assert process.stdin is not None
                        process.stdin.write(chunk)
                        process.stdin.close()
                        while process.poll() is None:
                            if stop_event.wait(0.1):
                                process.terminate()
                                break
            except Exception as error:
                if callable(on_status):
                    on_status(f"朗读失败：{error}")
            finally:
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

-- The user-facing English question bank is intentionally separate from the
-- Xuan Yu-You grammar exercise table above.  The latter is a textbook
-- companion used for reference and practice, while this table is reserved for
-- future real-exam material in the three visible categories.
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
                        "english_reading_placement",
                        "english_reading_profile_entries",
                        "english_reading_profiles",
                    )
                )
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
                "旋元佑七书相互连接的基础课程体系。",
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

    def question_bank_items(
        self,
        category: str = "grammar",
        keyword: str = "",
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return future real-exam questions for one visible English category.

        This deliberately reads ``english_question_bank_items`` instead of the
        textbook ``english_grammar_question_items`` table.  Textbook exercises
        remain available to the grammar/reference workflow and never leak into
        the user-facing real-exam bank.
        """

        normalized_category = str(category or "grammar").strip().casefold()
        if normalized_category not in {"grammar", "reading", "writing"}:
            raise ValueError("英语题库分类必须是 grammar、reading 或 writing。")
        clauses = ["category=?"]
        args: list[Any] = [normalized_category]
        value = str(keyword or "").strip()
        if value:
            pattern = f"%{value}%"
            clauses.append(
                "(question_code LIKE ? OR title LIKE ? OR prompt LIKE ? "
                "OR passage LIKE ? OR source_title LIKE ?)"
            )
            args.extend([pattern] * 5)
        args.append(max(1, min(int(limit), 2000)))
        where = " AND ".join(clauses)
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM english_question_bank_items
                WHERE {where}
                ORDER BY id
                LIMIT ?
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def question_bank_counts(self) -> dict[str, int]:
        with self.connect(rows=True) as connection:
            rows = connection.execute(
                "SELECT category,COUNT(*) AS count FROM english_question_bank_items GROUP BY category"
            ).fetchall()
        counts = {"grammar": 0, "reading": 0, "writing": 0}
        counts.update({str(row["category"]): int(row["count"]) for row in rows})
        return counts

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
