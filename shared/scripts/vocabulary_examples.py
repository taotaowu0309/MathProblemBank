"""Bilingual example sentences with audio for 词汇库 words (Tatoeba).

Words outside the Ray Du book have no book examples, so their cards take up to
three English sentences with human Chinese translations from the Tatoeba
English-Mandarin pairs (CC BY 2.0 FR, via manythings.org).  Each sentence is
read aloud by a Tatoeba volunteer recording when its licence allows, otherwise
by the project's existing Edge TTS voice; both are cached as MP3 on first use.

Everything lives in the per-machine cache ``shared/ui/cache/vocabulary_examples``
(ignored by Git and Syncthing): the two source files, a rebuildable SQLite index
and the audio cache.  Nothing here is formal data.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import sqlite3
import tarfile
import tempfile
import queue
import threading
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


def timed_call(function, timeout: float):
    """Bound a network-only call; late results must not perform formal writes."""
    if timeout <= 0:
        raise TimeoutError("准备超时，网络或语音服务未及时响应。")
    result = queue.Queue(maxsize=1)

    def run():
        try:
            result.put((True, function()))
        except Exception as error:
            result.put((False, error))

    threading.Thread(target=run, name="vocabulary-network", daemon=True).start()
    try:
        ok, value = result.get(timeout=max(0.001, timeout))
    except queue.Empty as error:
        raise TimeoutError("准备超时，网络或语音服务未及时响应。") from error
    if not ok:
        raise value
    return value

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_EXAMPLE_ROOT = ROOT_DIR / "shared" / "ui" / "cache" / "vocabulary_examples"
INDEX_VERSION = 1
EDGE_VOICE = "en-US-AvaNeural"
SOURCES = {
    "pairs": {
        "file": "cmn-eng.zip",
        "url": "https://www.manythings.org/anki/cmn-eng.zip",
        "title": "Tatoeba English–Mandarin sentence pairs (manythings.org)",
        "license": "CC BY 2.0 FR",
    },
    "audio": {
        "file": "sentences_with_audio.tar.bz2",
        "url": "https://downloads.tatoeba.org/exports/sentences_with_audio.tar.bz2",
        "title": "Tatoeba sentences with audio (recording licences)",
        "license": "per recording",
    },
}
AUDIO_URL = "https://tatoeba.org/en/audio/download/{audio_id}"
# Recordings that may be reused freely.  CC BY-NC(-ND) recordings are allowed only
# when the learner turns on personal non-commercial use.
PERMISSIVE_LICENSES = {"CC BY 4.0", "CC BY-SA 4.0", "CC0 1.0"}
NONCOMMERCIAL_LICENSES = {"CC BY-NC 4.0", "CC BY-NC-ND 3.0"}
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sentences(
    id INTEGER PRIMARY KEY,
    en TEXT NOT NULL,
    zh TEXT NOT NULL,
    zh_id INTEGER,
    word_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings(
    sentence_id INTEGER NOT NULL,
    audio_id INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    license TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(sentence_id, audio_id)
);
CREATE TABLE IF NOT EXISTS words(word TEXT NOT NULL, sentence_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_words_word ON words(word);
"""


class ExampleLibrary:
    """Tatoeba example sentences, their index and the cached audio of one machine."""

    SYNTHESIS_TIMEOUT = 60

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or DEFAULT_EXAMPLE_ROOT)
        self.index_path = self.root / "examples.db"
        self.audio_dir = self.root / "audio"
        self.tts_dir = self.root / "tts"
        self._fetch_lock = threading.Lock()
        self._fetching: set[int] = set()
        self._failed: set[int] = set()  # recordings that could not be fetched in this run

    # -- sources and index ---------------------------------------------------

    def source_path(self, name: str) -> Path:
        return self.root / SOURCES[name]["file"]

    def sources_present(self) -> bool:
        return all(self.source_path(name).is_file() for name in SOURCES)

    def _source_signature(self) -> str:
        digest = hashlib.sha256(f"v{INDEX_VERSION}".encode())
        for name in SOURCES:
            stat = self.source_path(name).stat()
            digest.update(f"{name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
        return digest.hexdigest()

    def index_ready(self) -> bool:
        if not (self.sources_present() and self.index_path.is_file()):
            return False
        try:
            with closing(sqlite3.connect(self.index_path)) as connection:
                row = connection.execute("SELECT value FROM meta WHERE key='signature'").fetchone()
        except sqlite3.Error:
            return False
        return bool(row) and row[0] == self._source_signature()

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "root": str(self.root),
            "sources": {
                name: {**info, "downloaded": self.source_path(name).is_file(),
                       "size": self.source_path(name).stat().st_size if self.source_path(name).is_file() else 0}
                for name, info in SOURCES.items()
            },
            "index_ready": self.index_ready(),
        }
        if result["index_ready"]:
            with closing(sqlite3.connect(self.index_path)) as connection:
                result["sentences"] = int(connection.execute("SELECT COUNT(*) FROM sentences").fetchone()[0])
                result["recordings"] = int(connection.execute("SELECT COUNT(*) FROM recordings").fetchone()[0])
        result["cached_audio_files"] = sum(1 for _ in self.audio_dir.glob("*.mp3")) if self.audio_dir.is_dir() else 0
        result["cached_tts_files"] = sum(1 for _ in self.tts_dir.glob("*.mp3")) if self.tts_dir.is_dir() else 0
        return result

    def download_sources(self, *, force: bool = False) -> dict[str, Any]:
        """Download the two source files (about 8 MB) and verify they can be read."""

        self.root.mkdir(parents=True, exist_ok=True)
        downloaded = []
        for name, info in SOURCES.items():
            target = self.source_path(name)
            if target.is_file() and not force:
                continue
            handle, temporary = tempfile.mkstemp(dir=str(self.root), suffix=".part")
            os.close(handle)
            try:
                request = urllib.request.Request(info["url"], headers={"User-Agent": "MathProblemBank vocabulary"})
                with urllib.request.urlopen(request, timeout=120) as response, open(temporary, "wb") as stream:
                    while chunk := response.read(1 << 16):
                        stream.write(chunk)
                self._verify_source(name, Path(temporary))
                os.replace(temporary, target)
                downloaded.append(info["file"])
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {"downloaded": downloaded, **self.build_index()}

    @staticmethod
    def _verify_source(name: str, path: Path) -> None:
        if name == "pairs":
            with zipfile.ZipFile(path) as archive:
                if "cmn.txt" not in archive.namelist():
                    raise RuntimeError("例句包里没有 cmn.txt。")
        else:
            with tarfile.open(path) as archive:
                if "sentences_with_audio.csv" not in archive.getnames():
                    raise RuntimeError("录音清单里没有 sentences_with_audio.csv。")

    def build_index(self, *, force: bool = False) -> dict[str, Any]:
        """Build the SQLite index from the two source files (derived, rebuildable)."""

        if not self.sources_present():
            raise FileNotFoundError("例句库还没有下载。")
        if self.index_ready() and not force:
            return self.status()
        sentences: dict[int, dict[str, Any]] = {}
        with zipfile.ZipFile(self.source_path("pairs")) as archive:
            text = archive.read("cmn.txt").decode("utf-8", errors="replace")
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            ids = [int(value) for value in re.findall(r"#(\d+)", parts[2])]
            if not ids:
                continue
            en, zh = parts[0].strip(), parts[1].strip()
            entry = sentences.setdefault(ids[0], {"en": en, "zh": [], "zh_id": ids[1] if len(ids) > 1 else None})
            if zh and zh not in entry["zh"]:
                entry["zh"].append(zh)
        recordings: list[tuple[int, int, str, str]] = []
        with tarfile.open(self.source_path("audio")) as archive:
            stream = archive.extractfile("sentences_with_audio.csv")
            assert stream is not None
            for raw in io.TextIOWrapper(stream, encoding="utf-8", errors="replace"):
                parts = raw.rstrip("\n").split("\t")
                if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
                    continue
                sentence_id = int(parts[0])
                if sentence_id in sentences:
                    recordings.append((sentence_id, int(parts[1]), parts[2], parts[3].replace("\\N", "")))
        self.root.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(self.root), suffix=".db")
        os.close(handle)
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.executescript(SCHEMA)
                connection.executemany(
                    "INSERT INTO sentences(id,en,zh,zh_id,word_count) VALUES(?,?,?,?,?)",
                    [
                        (sentence_id, item["en"], "\n".join(item["zh"]), item["zh_id"], len(_WORD_RE.findall(item["en"])))
                        for sentence_id, item in sentences.items()
                    ],
                )
                connection.executemany("INSERT OR IGNORE INTO recordings VALUES(?,?,?,?)", recordings)
                connection.executemany(
                    "INSERT INTO words(word,sentence_id) VALUES(?,?)",
                    [
                        (word, sentence_id)
                        for sentence_id, item in sentences.items()
                        for word in {token.casefold() for token in _WORD_RE.findall(item["en"])}
                    ],
                )
                connection.execute("INSERT INTO meta(key,value) VALUES('signature',?)", (self._source_signature(),))
                connection.commit()
                count = int(connection.execute("SELECT COUNT(*) FROM sentences").fetchone()[0])
                if count != len(sentences):
                    raise RuntimeError("例句索引写入数量不一致。")
            os.replace(temporary, self.index_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.status()

    # -- matching -------------------------------------------------------------

    def examples_for(
        self,
        term: str,
        forms: Iterable[str] = (),
        *,
        limit: int = 3,
        allow_noncommercial: bool = False,
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` sentences that use ``term`` (or one of its ``forms``).

        Sentences of 5–16 words come first, then those with a recording the
        learner allowed, then shorter ones; each result carries attribution.
        """

        if not self.index_ready():
            return []
        head = re.sub(r"\s+", " ", str(term or "")).strip()
        if not head:
            return []
        words = {head.casefold(), *(str(form).casefold() for form in forms if str(form).strip())}
        with closing(sqlite3.connect(self.index_path)) as connection:
            connection.row_factory = sqlite3.Row
            if " " in head:
                first = _WORD_RE.findall(head)
                rows = connection.execute(
                    "SELECT s.* FROM sentences s JOIN words w ON w.sentence_id=s.id WHERE w.word=?",
                    ((first[0] if first else head).casefold(),),
                ).fetchall()
                pattern = re.compile(r"\b" + r"\s+".join(map(re.escape, head.split())) + r"\b", re.IGNORECASE)
                rows = [row for row in rows if pattern.search(row["en"])]
            else:
                placeholders = ",".join("?" for _ in words)
                rows = connection.execute(
                    f"SELECT DISTINCT s.* FROM sentences s JOIN words w ON w.sentence_id=s.id WHERE w.word IN ({placeholders})",
                    sorted(words),
                ).fetchall()
            recordings: dict[int, list[sqlite3.Row]] = {}
            ids = [int(row["id"]) for row in rows]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for recording in connection.execute(
                    "SELECT * FROM recordings WHERE sentence_id IN (" + ",".join("?" for _ in chunk) + ")", chunk,
                ):
                    recordings.setdefault(int(recording["sentence_id"]), []).append(recording)
        allowed = set(PERMISSIVE_LICENSES) | (NONCOMMERCIAL_LICENSES if allow_noncommercial else set())
        results = []
        for row in rows:
            usable = sorted(
                (item for item in recordings.get(int(row["id"]), []) if item["license"] in allowed),
                key=lambda item: (item["license"] not in PERMISSIVE_LICENSES, int(item["audio_id"])),
            )
            recording = usable[0] if usable else None
            translations = [line for line in str(row["zh"]).split("\n") if line.strip()]
            simplified = next((line for line in translations if _is_simplified(line)), "")
            results.append({
                "sentence_id": int(row["id"]),
                "en": str(row["en"]),
                "zh": simplified or (translations[0] if translations else ""),
                "simplified": bool(simplified),
                "word_count": int(row["word_count"]),
                "audio_id": int(recording["audio_id"]) if recording else None,
                "audio_user": str(recording["username"]) if recording else "",
                "audio_license": str(recording["license"]) if recording else "",
            })
        results.sort(key=lambda item: (
            not item["simplified"],
            not 5 <= item["word_count"] <= 16,
            item["audio_id"] is None,
            abs(item["word_count"] - 9),
            item["sentence_id"],
        ))
        seen: set[str] = set()
        chosen = []
        for item in results:
            key = item["en"].casefold()
            if key in seen or (not item["simplified"] and (chosen or any(other["simplified"] for other in results))):
                continue  # traditional-only translations are a last resort, at most one
            seen.add(key)
            item["attribution"] = f"Tatoeba #{item['sentence_id']}" + (
                f" · 录音 {item['audio_user']}（{item['audio_license']}）" if item["audio_id"] else " · 朗读 Edge TTS"
            )
            chosen.append(item)
            if len(chosen) >= max(1, int(limit)):
                break
        return chosen

    # -- audio ------------------------------------------------------------------

    def recording_path(self, audio_id: int) -> Path:
        return self.audio_dir / f"tatoeba-{int(audio_id)}.mp3"

    def tts_path(self, text: str) -> Path:
        key = hashlib.sha1(f"{EDGE_VOICE}\n{text.strip()}".encode("utf-8")).hexdigest()
        return self.tts_dir / f"{key}.mp3"

    def cached_audio(self, text: str, audio_id: int | None = None) -> str:
        """The cached MP3 for a sentence or word, if it was fetched before."""

        if audio_id:
            path = self.recording_path(audio_id)
            if path.is_file():
                return str(path)
        path = self.tts_path(text)
        return str(path) if path.is_file() else ""

    def ensure_audio(self, text: str, audio_id: int | None = None, *, wait_for_recording: bool = False) -> str:
        """The MP3 to play now: the cached recording, else Edge TTS (synthesized once).

        Volunteer recordings download slowly, so by default a missing one is fetched in
        the background and used from the next time on; ``wait_for_recording`` blocks instead.
        """

        if audio_id and self.recording_path(audio_id).is_file():
            return str(self.recording_path(audio_id))
        if audio_id and int(audio_id) not in self._failed:
            if wait_for_recording:
                fetched = self.fetch_recording(int(audio_id))
                if fetched:
                    return fetched
            else:
                self.fetch_recordings_async([int(audio_id)])
        return self.synthesize(text)

    def synthesize(self, text: str) -> str:
        """Edge TTS once per text; returns the cached MP3 path or ''."""

        cached = self.tts_path(text)
        if cached.is_file():
            return str(cached)
        from shared.scripts.edge_tts_support import ensure_edge_tts

        problem = ensure_edge_tts()
        if problem:
            raise RuntimeError(f"朗读失败：{problem}")
        try:
            import edge_tts
        except ImportError as error:
            raise RuntimeError("朗读失败：语音组件不可用。") from error
        target = self.tts_path(text)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".mp3")
        os.close(handle)
        try:
            asyncio.run(asyncio.wait_for(
                edge_tts.Communicate(text.strip(), EDGE_VOICE).save(temporary),
                timeout=self.SYNTHESIS_TIMEOUT,
            ))
            if os.path.getsize(temporary) < 512:
                raise RuntimeError("语音服务没有返回有效录音。")
            os.replace(temporary, target)
            return str(target)
        except Exception as error:
            raise RuntimeError(f"朗读失败：网络或语音服务不可用（{str(error) or '合成超时'}）。") from error
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def fetch_recording(self, audio_id: int) -> str:
        """Download one volunteer recording into the cache; '' (and no retry this run) on failure."""

        with self._fetch_lock:
            if audio_id in self._fetching:
                return ""
            self._fetching.add(audio_id)
        try:
            return str(self._download(AUDIO_URL.format(audio_id=int(audio_id)), self.recording_path(audio_id)))
        except Exception:
            self._failed.add(int(audio_id))
            return ""
        finally:
            with self._fetch_lock:
                self._fetching.discard(audio_id)

    def fetch_recordings_async(self, audio_ids: Iterable[int]) -> None:
        missing = [
            int(audio_id) for audio_id in audio_ids
            if audio_id and not self.recording_path(audio_id).is_file()
            and int(audio_id) not in self._failed and int(audio_id) not in self._fetching
        ]
        if missing:
            threading.Thread(
                target=lambda: [self.fetch_recording(audio_id) for audio_id in missing],
                name="tatoeba-recordings", daemon=True,
            ).start()

    @staticmethod
    def _download(url: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        os.close(handle)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "MathProblemBank vocabulary"})
            with urllib.request.urlopen(request, timeout=90) as response, open(temporary, "wb") as stream:
                stream.write(response.read())
            with open(temporary, "rb") as stream:
                head = stream.read(3)
            if os.path.getsize(temporary) < 512 or not (head.startswith(b"ID3") or head[:1] == b"\xff"):
                raise RuntimeError("下载的录音不是 MP3。")
            os.replace(temporary, target)
            return target
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _is_simplified(text: str) -> bool:
    """Simplified-Chinese text fits GB2312; most traditional characters (環, 異, 護) do not."""

    try:
        text.encode("gb2312")
    except UnicodeEncodeError:
        return False
    return True


# ---------------------------------------------------------------------------
# AI-written examples for words Tatoeba does not cover
# ---------------------------------------------------------------------------
