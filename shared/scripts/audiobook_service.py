"""Audiobooks of the English workspace (有声书): listen while reading, then listen blind.

旋元佑's way to listening (the series preface): build written English and
reading speed through extensive reading, then listen to audiobooks of books
one can read.  Each book here pairs a public-domain LibriVox recording with
its Project Gutenberg text:

* the text is cut into the recording's chapters, paragraphs and sentences;
* each chapter's recording is downloaded once (``audio/NN.mp3``);
* Groq Whisper (the project's existing transcription service) gives word
  times, which are matched to the book's words so every sentence has a start
  and an end (``alignment/NN.json``); the raw word times are kept
  (``transcript/NN.json``) so aligning again never uploads again;
* Work writes the Chinese of every sentence and saves it through ``save_chapter_translation``,
  a passage at a time with the text before it as context
  (``translations/NN.json``), shown on request under the text.

Everything lives in ``English/audiobooks/<book>/`` (subject data, synced by
Syncthing like the rest of ``English/``).  Chapters are prepared one at a
time: a chapter can be heard as soon as it is ready, and Groq's hourly limit
only pauses the rest.  ``progress.json`` remembers where each book was left.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.english_learning_service import DEFAULT_ENGLISH_ROOT

DEFAULT_AUDIOBOOK_ROOT = DEFAULT_ENGLISH_ROOT / "audiobooks"
USER_AGENT = "MathProblemBank-Audiobooks/1.0 (personal study)"
GUTENBERG_TEXT_URL = "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt"
# Groq accepts uploads up to 25 MB; longer chapters are cut into pieces first.
UPLOAD_LIMIT_BYTES = 24 * 1024 * 1024
ALIGNMENT_VERSION = 1
TRANSLATION_RULES = (
    "逐句译成准确、通顺、忠实原文的简体中文：一句英文对应一句中文，不合并、不拆分；"
    "保留原句的语气与修辞，对白保留引号，人名、地名按通行译法。"
)


def _wind_chapter(number: int, roman: str, title: str, heading: str, seconds: int) -> dict[str, Any]:
    return {
        "title": title,
        "heading": [roman + ".", heading],
        "audio_url": f"https://www.archive.org/download/wind_in_the_willows_solo/wind_willows_{number:02d}_grahame_64kb.mp3",
        "seconds": seconds,
    }


# key -> the book, its Gutenberg text and the LibriVox recording of each chapter.
CATALOG: dict[str, dict[str, Any]] = {
    "childs-story": {
        "title": "The Child's Story",
        "title_zh": "孩子的故事",
        "author": "Charles Dickens",
        "year": "1852",
        "level": "初级",
        "episode": 12,
        "note": "旋老师书单 EP12。一个旅人走过一生的寓言，句子短、用词浅，适合第一次听读。",
        "gutenberg_id": 1467,
        "reader": "LibriVox《Some Short Christmas Stories》第 4 节，单人朗读",
        "librivox_url": "https://librivox.org/some-short-christmas-stories-by-charles-dickens/",
        "chapters": [{
            "title": "The Child's Story",
            "heading": ["THE CHILD’S STORY."],
            "end": "THE SCHOOLBOY’S STORY.",
            "audio_url": "https://www.archive.org/download/short_christmas_stories_jv_2012_librivox/shortxmasstories_04_dickens_64kb.mp3",
            "seconds": 755,
        }],
    },
    "wind-in-the-willows": {
        "title": "The Wind in the Willows",
        "title_zh": "柳林风声",
        "author": "Kenneth Grahame",
        "year": "1908",
        "level": "中级",
        "episode": 9,
        "note": "旋老师书单 EP09。河岸边鼹鼠、河鼠、獾和蟾蜍的故事；英国散文的经典，描写细腻。",
        "gutenberg_id": 289,
        "reader": "LibriVox 第 2 版，单人朗读全书",
        "librivox_url": "https://librivox.org/wind-in-the-willows-version-2-by-kenneth-grahame/",
        "chapters": [
            _wind_chapter(1, "I", "The River Bank", "THE RIVER BANK", 1795),
            _wind_chapter(2, "II", "The Open Road", "THE OPEN ROAD", 1775),
            _wind_chapter(3, "III", "The Wild Wood", "THE WILD WOOD", 1796),
            _wind_chapter(4, "IV", "Mr. Badger", "MR. BADGER", 1856),
            _wind_chapter(5, "V", "Dulce Domum", "DULCE DOMUM", 2280),
            _wind_chapter(6, "VI", "Mr. Toad", "MR. TOAD", 1935),
            _wind_chapter(7, "VII", "The Piper at the Gates of Dawn", "THE PIPER AT THE GATES OF DAWN", 1632),
            _wind_chapter(8, "VIII", "Toad's Adventures", "TOAD’S ADVENTURES", 2011),
            _wind_chapter(9, "IX", "Wayfarers All", "WAYFARERS ALL", 2621),
            _wind_chapter(10, "X", "The Further Adventures of Toad", "THE FURTHER ADVENTURES OF TOAD", 2394),
            _wind_chapter(11, "XI", "'Like Summer Tempests Came His Tears'", "“LIKE SUMMER TEMPESTS CAME HIS TEARS”", 2416),
            _wind_chapter(12, "XII", "The Return of Ulysses", "THE RETURN OF ULYSSES", 1913),
        ],
    },
}

if APP_PATHS.public_release:
    # The public release ships this one prepared public-domain book as its sample.
    CATALOG = {
        "childs-story": {
            **CATALOG["childs-story"],
            "note": "狄更斯的短篇寓言，句子短、用词浅。公版朗读与原文，附逐句中文译文；点“准备这本书”下载约 6 MB 录音后即可收听。",
            "episode": None,
        }
    }


# ---------------------------------------------------------------------------
# Text: chapters, paragraphs, sentences
# ---------------------------------------------------------------------------

_ABBREVIATIONS = ("mr", "mrs", "dr", "st", "mt", "messrs", "esq", "no", "vs", "etc", "e.g", "i.e")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"”’')\]]*\s+(?=[\"“‘'(\[]?[A-Z0-9])")
_BRACKET_LINE = re.compile(r"^\s*\[[^\]]*\]\s*$")


def split_sentences(paragraph: str) -> list[str]:
    """Sentences of one paragraph; "Mr. Toad" and similar abbreviations do not end one."""

    text = re.sub(r"\s+", " ", paragraph).strip()
    if not text:
        return []
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        piece = text[start:match.start()].rstrip()
        last = re.search(r"([A-Za-z.]+)[.]\W*$", piece)
        if last and last.group(1).casefold().rstrip(".") in _ABBREVIATIONS:
            continue
        sentences.append(text[start:match.end()].strip())
        start = match.end()
    if text[start:].strip():
        sentences.append(text[start:].strip())
    return sentences


def _heading_position(text: str, heading: list[str], start: int) -> tuple[int, int] | None:
    """Where ``heading`` (its lines, in order) begins and ends, searching from ``start``."""

    pattern = r"^" + r"[ \t]*\n[ \t]*".join(re.escape(line) for line in heading) + r"[ \t]*$"
    match = re.compile(pattern, re.MULTILINE).search(text, start)
    return (match.start(), match.end()) if match else None


def chapter_texts(source: str, chapters: list[dict[str, Any]]) -> list[list[list[str]]]:
    """Each chapter of a Gutenberg text as paragraphs of sentences.

    A chapter runs from its heading to the next chapter's heading, its own
    ``end`` marker, or the end of the Gutenberg text.  Bracketed lines
    ("[Illustration]", "[1852]") are left out.
    """

    text = source.replace("\r\n", "\n").replace("\r", "\n")
    end_of_book = text.find("*** END OF THE PROJECT GUTENBERG")
    if end_of_book < 0:
        end_of_book = len(text)
    positions = []
    cursor = 0
    for chapter in chapters:
        found = _heading_position(text, chapter["heading"], cursor)
        if found is None:
            raise ValueError(f"原文里找不到章节标题：{' / '.join(chapter['heading'])}")
        positions.append(found)
        cursor = found[1]
    result = []
    for index, (chapter, (_start, body_start)) in enumerate(zip(chapters, positions)):
        body_end = positions[index + 1][0] if index + 1 < len(positions) else end_of_book
        if chapter.get("end"):
            marker = text.find(chapter["end"], body_start)
            if marker >= 0:
                body_end = min(body_end, marker)
        paragraphs = []
        for block in re.split(r"\n\s*\n", text[body_start:body_end]):
            lines = [line for line in block.split("\n") if line.strip() and not _BRACKET_LINE.match(line)]
            sentences = split_sentences(" ".join(lines))
            if sentences:
                paragraphs.append(sentences)
        if not paragraphs:
            raise ValueError(f"章节没有正文：{chapter['title']}")
        result.append(paragraphs)
    return result


# ---------------------------------------------------------------------------
# Alignment: Whisper's word times onto the book's sentences
# ---------------------------------------------------------------------------

def words_of(text: str) -> list[str]:
    """Lower-case words without apostrophes; hyphenated words split ("spring-cleaning")."""

    return [word.replace("'", "") for word in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", str(text).replace("’", "'").casefold())]


def align_sentences(sentences: list[str], spoken: list[dict[str, Any]], *, duration_ms: int = 0) -> dict[str, Any]:
    """Start and end (ms) of every sentence from Whisper's timed words.

    The book's words and the heard words are matched in order; a sentence
    starts at its first matched word.  Sentences with no matched word (and the
    reader's additions, such as the LibriVox announcement, which match nothing
    in the book) get times between their neighbours in proportion to length.
    Each sentence ends where the next one starts.
    """

    book: list[tuple[str, int]] = []
    for index, sentence in enumerate(sentences):
        book.extend((word, index) for word in words_of(sentence))
    heard: list[tuple[str, int, int]] = []
    for item in spoken:
        for word in words_of(item.get("word", "")):
            heard.append((word, int(item.get("startMs") or 0), int(item.get("endMs") or 0)))
    matcher = difflib.SequenceMatcher(None, [word for word, _ in book], [word for word, _, _ in heard], autojunk=False)
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            _word, sentence = book[block.a + offset]
            _heard, start_ms, end_ms = heard[block.b + offset]
            first.setdefault(sentence, start_ms)
            last[sentence] = max(last.get(sentence, 0), end_ms)
            matched += 1
    starts: list[int | None] = [first.get(index) for index in range(len(sentences))]
    # Keep the times in order: a matched start earlier than the previous one is noise.
    previous = -1
    for index, value in enumerate(starts):
        if value is not None and value < previous:
            starts[index] = None
        elif value is not None:
            previous = value
    end_of_speech = max([end for _w, _s, end in heard] + [duration_ms, 0])
    lengths = [max(1, len(words_of(sentence))) for sentence in sentences]
    # Unknown starts lie between the known ones in proportion to the words
    # read in between; the book's start is 0 and its end the end of speech.
    index = 0
    while index < len(starts):
        if starts[index] is not None:
            index += 1
            continue
        gap_end = index
        while gap_end < len(starts) and starts[gap_end] is None:
            gap_end += 1
        anchor = index - 1  # the known sentence before the gap, or none
        begin = starts[anchor] if anchor >= 0 else 0
        finish = starts[gap_end] if gap_end < len(starts) else end_of_speech
        spans = lengths[max(anchor, 0):gap_end] if anchor >= 0 else [0, *lengths[index:gap_end]]
        total = sum(spans) or 1
        for offset, sentence in enumerate(range(index, gap_end)):
            before = sum(spans[: offset + 1])
            starts[sentence] = int(begin + (max(begin, finish) - begin) * before / total)
        index = gap_end
    timed = []
    for index, start in enumerate(starts):
        if index + 1 < len(starts):
            following = starts[index + 1]
        else:  # the last sentence ends with its last word, not with the reader's closing announcement
            following = min(end_of_speech, last[index] + 1000) if index in last else end_of_speech
        timed.append({"start_ms": int(start or 0), "end_ms": int(max(start or 0, following or 0))})
    return {
        "version": ALIGNMENT_VERSION,
        "sentences": timed,
        "matched_words": matched,
        "book_words": len(book),
        "matched_ratio": round(matched / len(book), 3) if book else 0.0,
        "duration_ms": int(end_of_speech),
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AudiobookPreparationPaused(RuntimeError):
    """Groq's limit was reached; the remaining chapters are prepared later."""


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _download(url: str, target: Path, *, timeout: int = 120) -> Path:
    """Download ``url`` to ``target`` through a partial file, so a broken download never looks complete."""

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(partial, "wb") as stream:
        while True:
            block = response.read(1 << 16)
            if not block:
                break
            stream.write(block)
    if partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"下载得到空文件：{url}")
    os.replace(partial, target)
    return target


def _is_rate_limit(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".casefold()
    return "429" in text or "rate limit" in text or "rate_limit" in text or "限额" in text or "rpm" in text


class AudiobookService:
    """Books, their chapters and sentence times, preparation and listening progress."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        catalog: dict[str, dict[str, Any]] | None = None,
        downloader: Callable[[str, Path], Path] | None = None,
        transcriber: Callable[[Path], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.root = Path(root or DEFAULT_AUDIOBOOK_ROOT)
        self.catalog_data = catalog if catalog is not None else CATALOG
        self.downloader = downloader or _download
        self._transcriber = transcriber
        self._lock = threading.Lock()
        self._texts: dict[str, list[list[list[str]]]] = {}

    # -- paths ------------------------------------------------------------

    def book_dir(self, key: str) -> Path:
        self._spec(key)
        return self.root / key

    def audio_path(self, key: str, chapter: int) -> Path:
        return self.book_dir(key) / "audio" / f"{chapter + 1:02d}.mp3"

    def alignment_path(self, key: str, chapter: int) -> Path:
        return self.book_dir(key) / "alignment" / f"{chapter + 1:02d}.json"

    def transcript_path(self, key: str, chapter: int) -> Path:
        return self.book_dir(key) / "transcript" / f"{chapter + 1:02d}.json"

    def translation_path(self, key: str, chapter: int) -> Path:
        return self.book_dir(key) / "translations" / f"{chapter + 1:02d}.json"

    def source_path(self, key: str) -> Path:
        return self.book_dir(key) / "source" / f"pg{self._spec(key)['gutenberg_id']}.txt"

    def _spec(self, key: str) -> dict[str, Any]:
        spec = self.catalog_data.get(str(key))
        if spec is None:
            raise KeyError(f"没有这本有声书：{key}")
        return spec

    # -- reading ------------------------------------------------------------

    def catalog(self) -> list[dict[str, Any]]:
        """Every book with what is ready: downloaded and aligned chapters, total time, where it was left."""

        progress = self.load_progress()
        rows = []
        for key, spec in self.catalog_data.items():
            chapters = spec["chapters"]
            rows.append({
                "key": key,
                **{name: spec.get(name, "") for name in ("title", "title_zh", "author", "year", "level", "note", "reader", "episode")},
                "chapter_count": len(chapters),
                "seconds": sum(int(chapter.get("seconds") or 0) for chapter in chapters),
                "downloaded": sum(1 for index in range(len(chapters)) if self.audio_path(key, index).is_file()),
                "aligned": sum(1 for index in range(len(chapters)) if self.alignment_path(key, index).is_file()),
                "translated": sum(1 for index in range(len(chapters)) if self.translation_path(key, index).is_file()),
                "text_ready": self.source_path(key).is_file(),
                "progress": progress.get(key, {}),
            })
        return rows

    def chapter_paragraphs(self, key: str) -> list[list[list[str]]]:
        """The book's chapters as paragraphs of sentences (from the downloaded Gutenberg text)."""

        if key not in self._texts:
            source = self.source_path(key)
            if not source.is_file():
                raise FileNotFoundError(f"《{self._spec(key)['title']}》的原文还没有下载。")
            self._texts[key] = chapter_texts(source.read_text(encoding="utf-8"), self._spec(key)["chapters"])
        return self._texts[key]

    def chapter(self, key: str, index: int) -> dict[str, Any]:
        """One chapter: its paragraphs, flat sentence list, audio path and sentence times when ready."""

        spec = self._spec(key)
        chapters = spec["chapters"]
        if not 0 <= int(index) < len(chapters):
            raise IndexError(f"《{spec['title']}》没有第 {int(index) + 1} 章。")
        index = int(index)
        paragraphs = self.chapter_paragraphs(key)[index]
        sentences = [sentence for paragraph in paragraphs for sentence in paragraph]
        alignment = None
        path = self.alignment_path(key, index)
        if path.is_file():
            alignment = json.loads(path.read_text(encoding="utf-8"))
            if len(alignment.get("sentences", [])) != len(sentences):
                alignment = None  # the text changed since; align again from the kept transcript
        translations = None
        path = self.translation_path(key, index)
        if path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
            if len(stored.get("sentences", [])) == len(sentences):
                translations = stored["sentences"]
        audio = self.audio_path(key, index)
        return {
            "book": key,
            "index": index,
            "title": chapters[index]["title"],
            "paragraphs": paragraphs,
            "sentences": sentences,
            "audio_path": str(audio) if audio.is_file() else "",
            "alignment": alignment,
            "translations": translations,
            "seconds": int(chapters[index].get("seconds") or 0),
        }

    # -- preparing -------------------------------------------------------------

    def ensure_text(self, key: str) -> Path:
        path = self.source_path(key)
        if not path.is_file():
            self.downloader(GUTENBERG_TEXT_URL.format(id=self._spec(key)["gutenberg_id"]), path)
            self._texts.pop(key, None)
        self.chapter_paragraphs(key)  # fails here when the text does not cut into the recording's chapters
        return path

    def _transcribe(self, audio: Path) -> list[dict[str, Any]]:
        if self._transcriber is not None:
            return self._transcriber(audio)
        from shared.scripts.online_course_media_engine import OnlineCourseMediaEngine

        engine = OnlineCourseMediaEngine()
        pieces = [(audio, 0)]
        if audio.stat().st_size > UPLOAD_LIMIT_BYTES:
            pieces = self._cut_for_upload(engine, audio)
        words: list[dict[str, Any]] = []
        for piece, offset_ms in pieces:
            transcript = engine.transcribe_file(piece, word_timestamps=True, language="en")
            seen: set[tuple[int, str]] = set()
            for segment in transcript.segments:
                for word in segment.get("words") or []:
                    marker = (int(word["startMs"]), str(word["word"]))
                    if marker in seen:
                        continue  # a word on a segment boundary is listed in both segments
                    seen.add(marker)
                    words.append({"word": word["word"], "startMs": int(word["startMs"]) + offset_ms,
                                  "endMs": int(word["endMs"]) + offset_ms})
            if piece != audio:
                piece.unlink(missing_ok=True)
        words.sort(key=lambda item: item["startMs"])
        return words

    def _cut_for_upload(self, engine: Any, audio: Path) -> list[tuple[Path, int]]:
        import subprocess

        ffmpeg = Path(getattr(engine, "ffmpeg_path", ""))
        if not ffmpeg.is_file():
            raise RuntimeError(f"这一章录音超过 24 MB，需要 ffmpeg 切成小段再转写：{audio.name}")
        piece_seconds = 20 * 60
        pieces = []
        for number in range(1000):
            start = number * piece_seconds
            target = Path(tempfile.gettempdir()) / f"audiobook_{os.getpid()}_{audio.stem}_{number}.mp3"
            subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-ss", str(start), "-t", str(piece_seconds),
                            "-i", str(audio), "-c", "copy", str(target)], check=True)
            if not target.is_file() or target.stat().st_size < 4096:
                target.unlink(missing_ok=True)
                break
            pieces.append((target, start * 1000))
        return pieces

    def save_chapter_translation(self, key: str, index: int, translations: list[str], *, model: str = "") -> dict[str, Any]:
        """Store the Chinese of every sentence of a chapter, written by Work; read back before returning."""

        from shared.scripts.chinese_simplify import to_simplified

        sentences = self.chapter(key, index)["sentences"]
        if len(translations) != len(sentences):
            raise ValueError(f"第 {index + 1} 章有 {len(sentences)} 句，收到 {len(translations)} 句译文。")
        result = [to_simplified(str(text or "").strip()) for text in translations]
        for number, text in enumerate(result):
            if not re.search(r"[\u4e00-\u9fff]", text):
                raise ValueError(f"第 {index + 1} 章第 {number + 1} 句没有中文译文。")
        stored = {"sentences": result, "model": str(model or ""),
                  "translated_at": datetime.now().isoformat(timespec="seconds")}
        _atomic_json(self.translation_path(key, index), stored)
        if self.chapter(key, index)["translations"] != result:
            raise RuntimeError(f"第 {index + 1} 章译文写入后回读不一致。")
        return {"book": key, "chapter": index + 1, "sentences": len(result), "readback_verified": True}

    def align_chapter(self, key: str, index: int) -> dict[str, Any]:
        """Sentence times of a chapter from its kept word times (no upload)."""

        transcript = self.transcript_path(key, index)
        if not transcript.is_file():
            raise FileNotFoundError(f"第 {index + 1} 章还没有转写。")
        words = json.loads(transcript.read_text(encoding="utf-8"))["words"]
        chapter = self.chapter(key, index)
        alignment = align_sentences(chapter["sentences"], words,
                                    duration_ms=int(self._spec(key)["chapters"][index].get("seconds") or 0) * 1000)
        alignment["aligned_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_json(self.alignment_path(key, index), alignment)
        return alignment

    def prepare_chapter(self, key: str, index: int, *, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Download, transcribe (once) and align one chapter; returns what was done."""

        say = progress or (lambda _text: None)
        spec = self._spec(key)
        chapter = spec["chapters"][index]
        label = f"第 {index + 1}/{len(spec['chapters'])} 章 {chapter['title']}"
        self.ensure_text(key)
        done = {"chapter": index, "title": chapter["title"], "downloaded": False, "transcribed": False}
        audio = self.audio_path(key, index)
        if not audio.is_file():
            say(f"正在下载{label}的录音…")
            self.downloader(chapter["audio_url"], audio)
            done["downloaded"] = True
        transcript = self.transcript_path(key, index)
        if not transcript.is_file():
            say(f"正在用 Groq Whisper 转写{label}（约 {round(int(chapter.get('seconds') or 0) / 60)} 分钟录音）…")
            try:
                words = self._transcribe(audio)
            except Exception as error:
                if _is_rate_limit(error):
                    raise AudiobookPreparationPaused(
                        f"Groq 转写达到限额，{label}及之后的章节稍后再准备（已准备好的章节可以先听）。"
                    ) from error
                raise
            _atomic_json(transcript, {"words": words, "provider": "groq", "audio_bytes": audio.stat().st_size,
                                      "transcribed_at": datetime.now().isoformat(timespec="seconds")})
            done["transcribed"] = True
        say(f"正在对齐{label}的句子…")
        alignment = self.align_chapter(key, index)
        done["matched_ratio"] = alignment["matched_ratio"]
        done["sentences"] = len(alignment["sentences"])
        if alignment["matched_ratio"] < 0.6:
            done["warning"] = f"只对上 {round(alignment['matched_ratio'] * 100)}% 的词，句子高亮可能偏移。"
        done["translation_pending"] = not self.translation_path(key, index).is_file()
        return done

    def prepare(
        self,
        key: str,
        chapters: Iterable[int] | None = None,
        *,
        progress: Callable[[str], None] | None = None,
        stop: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Prepare the given chapters (default: every chapter not aligned yet), in order.

        A Groq limit stops the run with ``paused`` set; already prepared chapters stay.
        """

        spec = self._spec(key)
        indexes = list(range(len(spec["chapters"]))) if chapters is None else sorted({int(item) for item in chapters})
        for index in indexes:
            if not 0 <= index < len(spec["chapters"]):
                raise IndexError(f"《{spec['title']}》没有第 {index + 1} 章。")
        results: list[dict[str, Any]] = []
        paused = ""
        with self._lock:
            for index in indexes:
                if stop is not None and stop.is_set():
                    paused = "已停止，剩下的章节下次继续准备。"
                    break
                if chapters is None and self.alignment_path(key, index).is_file():
                    continue
                try:
                    results.append(self.prepare_chapter(key, index, progress=progress))
                except AudiobookPreparationPaused as error:
                    paused = str(error)
                    break
        aligned = [index for index in range(len(spec["chapters"])) if self.alignment_path(key, index).is_file()]
        return {
            "book": key,
            "title": spec["title"],
            "prepared": results,
            "paused": paused,
            "aligned_chapters": [index + 1 for index in aligned],
            "chapter_count": len(spec["chapters"]),
            "readback_verified": all(self.alignment_path(key, item["chapter"]).is_file() for item in results),
        }

    # -- listening progress ---------------------------------------------------

    def _progress_path(self) -> Path:
        return self.root / "progress.json"

    def load_progress(self) -> dict[str, Any]:
        try:
            data = json.loads(self._progress_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_progress(self, key: str, *, chapter: int, sentence: int, finished_chapter: bool = False) -> dict[str, Any]:
        """Remember where a book was left; a chapter heard to its end is marked heard."""

        self._spec(key)
        with self._lock:
            data = self.load_progress()
            entry = dict(data.get(key) or {})
            entry.update({"chapter": int(chapter), "sentence": max(0, int(sentence)),
                          "updated_at": datetime.now().isoformat(timespec="seconds")})
            heard = set(entry.get("heard_chapters") or [])
            if finished_chapter:
                heard.add(int(chapter))
            entry["heard_chapters"] = sorted(heard)
            data[key] = entry
            _atomic_json(self._progress_path(), data)
        return entry


__all__ = [
    "AudiobookPreparationPaused",
    "AudiobookService",
    "CATALOG",
    "align_sentences",
    "chapter_texts",
    "split_sentences",
    "words_of",
]
