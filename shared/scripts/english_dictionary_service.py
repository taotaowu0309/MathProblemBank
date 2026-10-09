from __future__ import annotations

"""Offline English dictionary resources used by the English PDF reader.

The downloaded archives remain the source of truth.  A small SQLite index is
created beside them so a word lookup never needs network access or a Python
dictionary package.  The index is derived and can be rebuilt at any time.
"""

import json
import csv
import html
import re
import sqlite3
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DICTIONARY_ROOT = ROOT_DIR / "English" / "dictionaries"
OEWN_ARCHIVE = "english-wordnet-2025-json.zip"
ETYMWN_ARCHIVE = "etymwn-20130208.zip"
ECDICT_SOURCE = "ecdict.csv"
WORDROOT_SOURCE = "wordroot.txt"
INDEX_NAME = "english_dictionary.db"
MANIFEST_NAME = "manifest.json"
MORPHOLEX_SOURCE = "MorphoLEX_en.xlsx"
OPENETYMOLOGY_FILES: tuple[tuple[str, str], ...] = (
    ("OpenEtymology_CET4.epub", "OpenEtymology CET4"),
    ("OpenEtymology_CET6.epub", "OpenEtymology CET6"),
    ("OpenEtymology_TEM8.epub", "OpenEtymology TEM8"),
    ("OpenEtymology_TOEFL.epub", "OpenEtymology TOEFL"),
    ("OpenEtymology_GRE8000.epub", "OpenEtymology GRE8000"),
)


DICTIONARY_SOURCES: tuple[dict[str, str], ...] = (
    {
        "id": "oewn-2025",
        "title": "Open English WordNet 2025",
        "archive": OEWN_ARCHIVE,
        "download_url": "https://en-word.net/static/english-wordnet-2025-json.zip",
        "license": "CC BY 4.0; underlying Princeton WordNet license also applies",
        "license_url": "https://github.com/globalwordnet/english-wordnet/blob/main/LICENSE.md",
        "purpose": "English senses, part of speech, examples and semantic relations",
    },
    {
        "id": "etymwn-2013",
        "title": "Etymological Wordnet 2013-02-08",
        "archive": ETYMWN_ARCHIVE,
        "download_url": "https://archive.org/download/etymwn-20130208/etymwn-20130208.zip",
        "license": "CC BY-SA 3.0 (derived from Wiktionary data)",
        "license_url": "https://en.wiktionary.org/wiki/Wiktionary:Copyrights",
        "purpose": "Word-origin relations and historical language links",
    },
    {
        "id": "ecdict-2024",
        "title": "ECDICT English-Chinese Dictionary",
        "archive": ECDICT_SOURCE,
        "download_url": "https://github.com/skywind3000/ECDICT/raw/master/ecdict.csv",
        "license": "MIT; source acknowledgements remain applicable",
        "license_url": "https://github.com/skywind3000/ECDICT/blob/master/LICENSE",
        "purpose": "Broad English-to-Chinese translations, pronunciation, parts of speech and word-form notes",
    },
    {
        "id": "ecdict-wordroot",
        "title": "ECDICT Word Root Catalog",
        "archive": WORDROOT_SOURCE,
        "download_url": "https://github.com/skywind3000/ECDICT/raw/master/wordroot.txt",
        "license": "MIT; source acknowledgements remain applicable",
        "license_url": "https://github.com/skywind3000/ECDICT/blob/master/LICENSE",
        "purpose": "Prefix, root and suffix meanings used for constructive word-part analysis",
    },
    {
        "id": "openetymology-wordbooks",
        "title": "OpenEtymology public wordbooks",
        "archive": "OpenEtymology_CET4.epub; OpenEtymology_CET6.epub; OpenEtymology_TEM8.epub; OpenEtymology_TOEFL.epub; OpenEtymology_GRE8000.epub",
        "download_url": "https://github.com/openetymology/OpenEtymology",
        "license": "CC BY-SA 4.0 (public wordbook files only)",
        "license_url": "https://github.com/openetymology/OpenEtymology/blob/main/DATA_LICENSE.md",
        "purpose": "Local Chinese definitions, compact morpheme splits and learner-facing etymology notes",
    },
    {
        "id": "morpholex-en",
        "title": "MorphoLex-en",
        "archive": MORPHOLEX_SOURCE,
        "download_url": "https://github.com/hugomailhot/MorphoLex-en",
        "license": "CC BY-NC-SA 4.0",
        "license_url": "https://github.com/hugomailhot/MorphoLex-en/blob/master/LICENSE.md",
        "purpose": "Structured morphological segmentation fallback for words absent from the learner wordbooks",
    },
)


_MORPHEME_MEANINGS_ZH = {
    "in, into, on": "在……内；进入；在……上",
    "not, without": "不；无；非",
    "again; back or back again": "再；重新；向后",
    "new": "新的",
    "believe": "相信；信任",
    "state or quality of being": "……的状态或性质",
    "the act, process, or result of": "行为、过程或结果",
    "capable of being, doing, or undergoing": "能够被……；能够……；可经受……",
}

_MORPHEME_OVERRIDES: dict[str, dict[str, Any]] = {
    "-el": {
        "class": "word-forming suffix",
        "root": "-el",
        "meaning": "",
        "origin": "Latin",
        "example": ["novel"],
    },
}

_MORPHOLOGY_WORD_OVERRIDES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "innovation": (("in", "in-1"), ("nov", "nov"), ("ation", "-ation")),
    "novel": (("nov", "nov"), ("el", "-el")),
    "incredulity": (
        ("in", "in-2"),
        ("cred", "cred, credit"),
        ("ul", None),
        ("ity", "-ty, -ity, -uity, -eity"),
    ),
    "incredible": (("in", "in-2"), ("cred", "cred, credit"), ("ible", "-able, -ible, -ble")),
}


def _clean_word(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


class EnglishDictionaryService:
    def __init__(self, dictionary_root: Path | str = DEFAULT_DICTIONARY_ROOT) -> None:
        self.dictionary_root = Path(dictionary_root).resolve()
        self.dictionary_root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.dictionary_root / INDEX_NAME
        self.manifest_path = self.dictionary_root / MANIFEST_NAME
        self._wordroot_cache: dict[str, dict[str, Any]] | None = None
        self._open_etymology_cache: dict[str, dict[str, Any]] | None = None
        self._morpholex_cache: dict[str, dict[str, Any]] | None = None
        self._optional_sources_lock = threading.Lock()
        self._optional_sources_ready = False
        self._optional_sources_loading = False
        self._optional_sources_thread: threading.Thread | None = None
        self._lookup_cache_thread: threading.Thread | None = None
        self._lookup_cache_loading = False

    def warm_lookup_caches_async(self) -> None:
        """Parse the larger morphology catalogs before the first user lookup."""
        if self._lookup_cache_loading:
            return
        self._lookup_cache_loading = True

        def worker() -> None:
            try:
                self._wordroot_catalog()
                self._morpholex_catalog()
            finally:
                self._lookup_cache_loading = False

        self._lookup_cache_thread = threading.Thread(
            target=worker,
            name="english-dictionary-morphology-cache",
            daemon=True,
        )
        self._lookup_cache_thread.start()

    def warm_optional_sources_async(self) -> None:
        """Load optional wordbooks without blocking the PDF reader's UI thread."""
        with self._optional_sources_lock:
            if self._optional_sources_ready or self._optional_sources_loading:
                return
            self._optional_sources_loading = True

        def worker() -> None:
            try:
                self._ensure_optional_sources()
            finally:
                with self._optional_sources_lock:
                    self._optional_sources_loading = False
                    self._optional_sources_ready = True

        self._optional_sources_thread = threading.Thread(
            target=worker,
            name="english-dictionary-optional-sources",
            daemon=True,
        )
        self._optional_sources_thread.start()

    def status(self) -> dict[str, Any]:
        sources = []
        for source in DICTIONARY_SOURCES:
            archive = self.dictionary_root / source["archive"]
            sources.append({
                **source,
                "path": str(archive),
                "downloaded": archive.is_file() and archive.stat().st_size > 0,
                "size": archive.stat().st_size if archive.is_file() else 0,
            })
        index_ready = self.database_path.is_file() and self.database_path.stat().st_size > 0
        ecdict_ready = self._has_rows("ecdict_entries")
        result: dict[str, Any] = {
            "root": str(self.dictionary_root),
            "index_path": str(self.database_path),
            "index_ready": index_ready,
            "ecdict_ready": ecdict_ready,
            "sources": sources,
        }
        if self.manifest_path.is_file():
            try:
                result["manifest"] = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result["manifest"] = {}
        return result

    def _has_rows(self, table: str) -> bool:
        if not self.database_path.is_file():
            return False
        try:
            connection = sqlite3.connect(self.database_path)
            try:
                return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) > 0
            finally:
                connection.close()
        except sqlite3.Error:
            return False

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS wn_synsets(
                synset_id TEXT PRIMARY KEY,
                part_of_speech TEXT NOT NULL DEFAULT '',
                definition_en TEXT NOT NULL DEFAULT '',
                examples_json TEXT NOT NULL DEFAULT '[]',
                members_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS wn_lemmas(
                lemma TEXT NOT NULL COLLATE NOCASE,
                part_of_speech TEXT NOT NULL DEFAULT '',
                synset_id TEXT NOT NULL,
                pronunciation TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(lemma, part_of_speech, synset_id),
                FOREIGN KEY(synset_id) REFERENCES wn_synsets(synset_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_wn_lemmas_term ON wn_lemmas(lemma COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS etym_edges(
                source_word TEXT NOT NULL COLLATE NOCASE,
                source_language TEXT NOT NULL,
                relation TEXT NOT NULL,
                target_word TEXT NOT NULL COLLATE NOCASE,
                target_language TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_etym_source ON etym_edges(source_word COLLATE NOCASE, source_language);
            CREATE INDEX IF NOT EXISTS idx_etym_target ON etym_edges(target_word COLLATE NOCASE, target_language);
            CREATE TABLE IF NOT EXISTS ecdict_entries(
                word TEXT PRIMARY KEY COLLATE NOCASE,
                stripped_word TEXT NOT NULL DEFAULT '',
                phonetic TEXT NOT NULL DEFAULT '',
                definition_en TEXT NOT NULL DEFAULT '',
                translation_zh TEXT NOT NULL DEFAULT '',
                part_of_speech TEXT NOT NULL DEFAULT '',
                collins TEXT NOT NULL DEFAULT '',
                oxford TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ecdict_stripped_word ON ecdict_entries(stripped_word);
            CREATE TABLE IF NOT EXISTS open_etymology_entries(
                word TEXT PRIMARY KEY COLLATE NOCASE,
                pronunciation TEXT NOT NULL DEFAULT '',
                definitions_zh TEXT NOT NULL DEFAULT '',
                morphemes_json TEXT NOT NULL DEFAULT '[]',
                etymology_zh TEXT NOT NULL DEFAULT '',
                source_book TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_open_etymology_word ON open_etymology_entries(word COLLATE NOCASE);
            """
        )

    def ensure_index(self, *, force: bool = False) -> dict[str, Any]:
        with self._optional_sources_lock:
            self._optional_sources_ready = False
        if not force and self.database_path.is_file() and self.database_path.stat().st_size > 0:
            self._ensure_optional_sources()
            return self.status()
        oewn_path = self.dictionary_root / OEWN_ARCHIVE
        etym_path = self.dictionary_root / ETYMWN_ARCHIVE
        if not oewn_path.is_file() or not etym_path.is_file():
            missing = [path.name for path in (oewn_path, etym_path) if not path.is_file()]
            raise FileNotFoundError("缺少本地词典压缩包：" + ", ".join(missing))

        temporary = self.database_path.with_suffix(".tmp.db")
        temporary.unlink(missing_ok=True)
        connection = sqlite3.connect(temporary)
        try:
            self._schema(connection)
            self._load_oewn(connection, oewn_path)
            self._load_etymwn(connection, etym_path)
            ecdict_path = self.dictionary_root / ECDICT_SOURCE
            if ecdict_path.is_file():
                self._load_ecdict(connection, ecdict_path)
            connection.commit()
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.commit()
        finally:
            connection.close()
        temporary.replace(self.database_path)
        self._write_manifest()
        # Explicit index builds are maintenance actions, so they may finish
        # the optional local wordbooks synchronously.  Ordinary lookups never
        # call this path; the application warms it in a background thread.
        self._ensure_optional_sources()
        return self.status()

    def _ensure_optional_sources(self) -> None:
        """Add newly installed optional dictionary data without rebuilding WordNet."""

        with self._optional_sources_lock:
            if self._optional_sources_ready and not self._optional_sources_loading:
                return
        if not self.database_path.is_file():
            with self._optional_sources_lock:
                self._optional_sources_ready = True
            return
        try:
            connection = sqlite3.connect(self.database_path, timeout=30)
            try:
                self._schema(connection)
                changed = False
                ecdict_path = self.dictionary_root / ECDICT_SOURCE
                if ecdict_path.is_file() and int(connection.execute("SELECT COUNT(*) FROM ecdict_entries").fetchone()[0]) == 0:
                    self._load_ecdict(connection, ecdict_path)
                    changed = True
                if int(connection.execute("SELECT COUNT(*) FROM open_etymology_entries").fetchone()[0]) == 0:
                    epub_paths = [
                        (self.dictionary_root / filename, book)
                        for filename, book in OPENETYMOLOGY_FILES
                        if (self.dictionary_root / filename).is_file()
                    ]
                    if epub_paths:
                        self._load_open_etymology(connection, epub_paths)
                        changed = True
                if changed:
                    connection.commit()
                    self._write_manifest()
            finally:
                connection.close()
        finally:
            with self._optional_sources_lock:
                self._optional_sources_ready = True

    @staticmethod
    def _clean_markup(value: str) -> str:
        value = re.sub(r"<br\s*/?>", " ", str(value or ""), flags=re.IGNORECASE)
        value = re.sub(r"<[^>]+>", " ", value)
        return re.sub(r"\s+", " ", html.unescape(value)).strip()

    @classmethod
    def _parse_open_etymology_epub(cls, archive_path: Path, source_book: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        entry_pattern = re.compile(
            r'<section\s+class="word-entry"\s+id="([^"]+)">(.*?)(?=<section\s+class="word-entry"\s+id=|</body>|\Z)',
            re.IGNORECASE | re.DOTALL,
        )
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                if not name.endswith((".xhtml", ".html")):
                    continue
                text = archive.read(name).decode("utf-8", errors="replace")
                for word_id, block in entry_pattern.findall(text):
                    word_match = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.IGNORECASE | re.DOTALL)
                    word = cls._clean_markup(word_match.group(1) if word_match else word_id).casefold()
                    if not re.fullmatch(r"[a-z][a-z' -]*", word):
                        continue
                    pronunciation_match = re.search(
                        r'<p\s+class="pronunciation"[^>]*>(.*?)</p>', block, re.IGNORECASE | re.DOTALL
                    )
                    definitions_section = re.search(
                        r'<h3>Definitions</h3>(.*?)(?:</section>)', block, re.IGNORECASE | re.DOTALL
                    )
                    definitions = []
                    if definitions_section:
                        definitions = [
                            cls._clean_markup(item)
                            for item in re.findall(r"<li[^>]*>(.*?)</li>", definitions_section.group(1), re.IGNORECASE | re.DOTALL)
                        ]
                    morphemes_section = re.search(
                        r'<h3>Morphemes</h3>(.*?)(?:</section>)', block, re.IGNORECASE | re.DOTALL
                    )
                    morphemes: list[dict[str, str]] = []
                    if morphemes_section:
                        for item in re.findall(r"<li[^>]*>(.*?)</li>", morphemes_section.group(1), re.IGNORECASE | re.DOTALL):
                            strong = re.search(r"<strong[^>]*>(.*?)</strong>", item, re.IGNORECASE | re.DOTALL)
                            if not strong:
                                continue
                            segment = cls._clean_markup(strong.group(1)).strip("-").casefold()
                            rest = item[strong.end():]
                            note_match = re.search(r'<span\s+class="note"[^>]*>(.*?)</span>', rest, re.IGNORECASE | re.DOTALL)
                            note = cls._clean_markup(note_match.group(1) if note_match else "")
                            gloss = cls._clean_markup(re.sub(r'<span\s+class="note"[^>]*>.*?</span>', " ", rest, flags=re.IGNORECASE | re.DOTALL))
                            if segment:
                                morphemes.append({"segment": segment, "meaning": gloss, "note": note})
                    etymology_section = re.search(
                        r'<h3>Etymology</h3>(.*?)(?:</section>)', block, re.IGNORECASE | re.DOTALL
                    )
                    etymology = ""
                    if etymology_section:
                        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", etymology_section.group(1), re.IGNORECASE | re.DOTALL)
                        etymology = cls._clean_markup(" ".join(paragraphs))
                    entries.append({
                        "word": word,
                        "pronunciation": cls._clean_markup(pronunciation_match.group(1) if pronunciation_match else ""),
                        "definitions_zh": "\n".join(item for item in definitions if item),
                        "morphemes": morphemes,
                        "etymology_zh": etymology,
                        "source_book": source_book,
                    })
        return entries

    @classmethod
    def _load_open_etymology(cls, connection: sqlite3.Connection, sources: list[tuple[Path, str]]) -> None:
        for archive_path, source_book in sources:
            for entry in cls._parse_open_etymology_epub(archive_path, source_book):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO open_etymology_entries(
                        word,pronunciation,definitions_zh,morphemes_json,etymology_zh,source_book
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        entry["word"],
                        entry["pronunciation"],
                        entry["definitions_zh"],
                        json.dumps(entry["morphemes"], ensure_ascii=False),
                        entry["etymology_zh"],
                        entry["source_book"],
                    ),
                )
        connection.commit()

    @staticmethod
    def _load_ecdict(connection: sqlite3.Connection, csv_path: Path) -> None:
        batch: list[tuple[str, str, str, str, str, str, str, str, str, str]] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="", errors="replace") as stream:
            reader = csv.DictReader(stream)
            for row in reader:
                word = str(row.get("word") or "").strip()
                if not word:
                    continue
                batch.append(
                    (
                        word,
                        re.sub(r"[^a-z0-9]+", "", word.casefold()),
                        str(row.get("phonetic") or "").strip(),
                        str(row.get("definition") or "").strip(),
                        str(row.get("translation") or "").strip(),
                        str(row.get("pos") or "").strip(),
                        str(row.get("collins") or "").strip(),
                        str(row.get("oxford") or "").strip(),
                        str(row.get("tag") or "").strip(),
                        str(row.get("exchange") or "").strip(),
                    )
                )
                if len(batch) >= 5000:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO ecdict_entries(
                            word,stripped_word,phonetic,definition_en,translation_zh,
                            part_of_speech,collins,oxford,tags,exchange
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        batch,
                    )
                    batch.clear()
            if batch:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO ecdict_entries(
                        word,stripped_word,phonetic,definition_en,translation_zh,
                        part_of_speech,collins,oxford,tags,exchange
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    batch,
                )
        connection.commit()

    @staticmethod
    def _load_oewn(connection: sqlite3.Connection, archive_path: Path) -> None:
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                if not name.endswith(".json"):
                    continue
                data = json.loads(archive.read(name).decode("utf-8"))
                if name.startswith("entries-"):
                    for lemma, pos_map in data.items():
                        if not isinstance(pos_map, dict):
                            continue
                        for part_of_speech, payload in pos_map.items():
                            if not isinstance(payload, dict):
                                continue
                            pronunciation = ""
                            pronunciations = payload.get("pronunciation") or []
                            if pronunciations and isinstance(pronunciations[0], dict):
                                pronunciation = str(pronunciations[0].get("value") or "")
                            for sense in payload.get("sense") or []:
                                if not isinstance(sense, dict) or not sense.get("synset"):
                                    continue
                                connection.execute(
                                    """
                                    INSERT OR IGNORE INTO wn_lemmas(lemma,part_of_speech,synset_id,pronunciation)
                                    VALUES(?,?,?,?)
                                    """,
                                    (str(lemma), str(part_of_speech), str(sense["synset"]), pronunciation),
                                )
                elif name.split("/", 1)[-1].split(".", 1)[0] in {"noun", "verb", "adj", "adv"}:
                    for synset_id, payload in data.items():
                        if not isinstance(payload, dict):
                            continue
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO wn_synsets(
                                synset_id,part_of_speech,definition_en,examples_json,members_json
                            ) VALUES(?,?,?,?,?)
                            """,
                            (
                                str(synset_id),
                                str(payload.get("partOfSpeech") or ""),
                                " ; ".join(str(item) for item in payload.get("definition") or []),
                                json.dumps(payload.get("example") or [], ensure_ascii=False),
                                json.dumps(payload.get("members") or [], ensure_ascii=False),
                            ),
                        )
        connection.commit()

    @staticmethod
    def _node(value: str) -> tuple[str, str]:
        match = re.match(r"^([a-z]{3}):\s*(.*)$", str(value or ""), re.IGNORECASE)
        if not match:
            return "", ""
        return match.group(1).lower(), match.group(2).strip()

    @classmethod
    def _load_etymwn(cls, connection: sqlite3.Connection, archive_path: Path) -> None:
        batch: list[tuple[str, str, str, str, str]] = []
        with zipfile.ZipFile(archive_path) as archive, archive.open("etymwn.tsv") as stream:
            for raw_line in stream:
                parts = raw_line.decode("utf-8", errors="replace").rstrip("\r\n").split("\t")
                if len(parts) != 3:
                    continue
                source_language, source_word = cls._node(parts[0])
                relation = parts[1].replace("rel:", "", 1).strip()
                target_language, target_word = cls._node(parts[2])
                if not relation or not source_word or not target_word:
                    continue
                if source_language != "eng" and target_language != "eng":
                    continue
                batch.append((source_word, source_language, relation, target_word, target_language))
                if len(batch) >= 5000:
                    connection.executemany(
                        "INSERT INTO etym_edges(source_word,source_language,relation,target_word,target_language) VALUES(?,?,?,?,?)",
                        batch,
                    )
                    batch.clear()
            if batch:
                connection.executemany(
                    "INSERT INTO etym_edges(source_word,source_language,relation,target_word,target_language) VALUES(?,?,?,?,?)",
                    batch,
                )
        connection.commit()

    def _count(self, table: str) -> int:
        connection = sqlite3.connect(self.database_path)
        try:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()

    def _write_manifest(self) -> None:
        manifest = {
            "index_version": 2,
            "sources": list(DICTIONARY_SOURCES),
            "oewn_synsets": self._count("wn_synsets"),
            "oewn_lemmas": self._count("wn_lemmas"),
            "etymology_edges": self._count("etym_edges"),
            "ecdict_entries": self._count("ecdict_entries"),
        }
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _connection(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            self.ensure_index()
        return sqlite3.connect(self.database_path)

    @staticmethod
    def _language_name(language: str) -> str:
        names = {
            "ang": "古英语",
            "enm": "中古英语",
            "en": "英语",
            "eng": "英语",
            "la": "拉丁语",
            "lat": "拉丁语",
            "grc": "古希腊语",
            "de": "德语",
            "deu": "德语",
            "fr": "法语",
            "fra": "法语",
            "it": "意大利语",
            "ita": "意大利语",
            "es": "西班牙语",
            "spa": "西班牙语",
        }
        return names.get(str(language or "").casefold(), str(language or "").casefold())

    def _direct_etymology_edges(self, word: str, *, limit: int = 80) -> list[dict[str, str]]:
        """Read direct relations only; the graph's first rows are often derived forms."""

        connection = self._connection()
        try:
            rows = connection.execute(
                """
                SELECT source_word,source_language,relation,target_word,target_language
                FROM etym_edges
                WHERE (source_word=? COLLATE NOCASE AND source_language='eng')
                   OR (target_word=? COLLATE NOCASE AND target_language='eng')
                ORDER BY
                    CASE
                        WHEN source_word=? COLLATE NOCASE
                             AND relation IN ('etymology','etymological_origin_of')
                             AND target_language<>'eng' THEN 0
                        WHEN target_word=? COLLATE NOCASE
                             AND relation IN ('etymology','etymological_origin_of')
                             AND source_language<>'eng' THEN 1
                        WHEN relation='etymologically_related' THEN 2
                        WHEN relation IN ('has_derived_form','is_derived_from') THEN 3
                        ELSE 4
                    END,
                    source_language,target_language,relation,target_word
                LIMIT ?
                """,
                (word, word, word, word, max(1, min(int(limit), 200))),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "source_word": str(source_word),
                "source_language": str(source_language),
                "relation": str(relation),
                "target_word": str(target_word),
                "target_language": str(target_language),
            }
            for source_word, source_language, relation, target_word, target_language in rows
        ]

    def _friendly_etymology(self, word: str) -> str:
        """Turn Etymological Wordnet relations into a short learner-facing note.

        The source is a relation graph, so labels such as ``is_derived_from``
        are not suitable for the popup.  We only state an early-language
        origin when the local data contains one; otherwise we label the result
        as a word-family clue instead of pretending it is a proven root.
        """

        edges = self._direct_etymology_edges(word, limit=120)
        origins: list[tuple[str, str]] = []
        family: list[str] = []
        seen_origins: set[tuple[str, str]] = set()
        seen_family: set[str] = set()
        has_related_family = False
        for edge in edges:
            relation = edge["relation"]
            source = edge["source_word"]
            source_language = edge["source_language"]
            target = edge["target_word"]
            target_language = edge["target_language"]
            if relation in {"etymology", "etymological_origin_of"}:
                if source.casefold() == word.casefold() and target_language != "eng":
                    candidate = (target, target_language)
                elif target.casefold() == word.casefold() and source_language != "eng":
                    candidate = (source, source_language)
                else:
                    candidate = None
                if candidate and candidate not in seen_origins:
                    seen_origins.add(candidate)
                    origins.append(candidate)
                continue
            if relation == "etymologically_related":
                has_related_family = True
                candidate = target if source.casefold() == word.casefold() else source
                if candidate.casefold() != word.casefold() and candidate not in seen_family:
                    seen_family.add(candidate)
                    family.append(candidate)
                continue
            if relation in {"has_derived_form", "is_derived_from"}:
                candidate = target if source.casefold() == word.casefold() else source
                # Phrases make the note noisy; keep compact word-family items.
                if (
                    candidate.casefold() != word.casefold()
                    and " " not in candidate
                    and len(candidate) <= 28
                    and candidate not in seen_family
                ):
                    seen_family.add(candidate)
                    family.append(candidate)

        lines: list[str] = []
        if origins:
            root, language = origins[0]
            lines.append(f"来源：{self._language_name(language)} {root} → {word}")
            if len(origins) > 1:
                more = "、".join(root_word for root_word, _ in origins[1:3])
                if more:
                    lines.append(f"其他历史形式：{more}")
        elif not family:
            return "本地字源库没有足够的早期词源资料。"
        if family:
            label = (
                "同源/词族"
                if has_related_family and not origins
                else "同源/派生"
                if origins
                else "派生词"
            )
            lines.append(f"{label}：" + "、".join(family[:6]))
        return "\n".join(lines)

    def etymology(self, term: str, *, limit: int = 10, depth: int = 2) -> list[dict[str, str]]:
        word = _clean_word(term)
        if not word:
            return []
        connection = self._connection()
        try:
            frontier = {word}
            seen: set[tuple[str, str, str]] = set()
            result: list[dict[str, str]] = []
            for _ in range(max(1, min(int(depth), 4))):
                if not frontier or len(result) >= limit:
                    break
                next_frontier: set[str] = set()
                for current in frontier:
                    rows = connection.execute(
                        """
                        SELECT source_word,source_language,relation,target_word,target_language
                        FROM etym_edges
                        WHERE (source_word=? COLLATE NOCASE AND source_language='eng')
                           OR (target_word=? COLLATE NOCASE AND target_language='eng')
                        ORDER BY CASE
                            WHEN target_word=? COLLATE NOCASE AND target_language='eng' THEN 0
                            ELSE 1
                        END, source_word, target_word
                        LIMIT 100
                        """,
                        (current, current, current),
                    ).fetchall()
                    for source_word, source_language, relation, target_word, target_language in rows:
                        if target_language == "eng" and target_word.casefold() == current:
                            related_word, related_language, direction = source_word, source_language, f"origin:{relation}"
                        else:
                            related_word, related_language, direction = target_word, target_language, f"derived:{relation}"
                        key = (current, direction, related_word)
                        if key in seen:
                            continue
                        seen.add(key)
                        result.append({
                            "term": current,
                            "relation": direction,
                            "related_word": related_word,
                            "related_language": related_language,
                        })
                        if related_language == "eng" and related_word.casefold() != current:
                            next_frontier.add(related_word.casefold())
                        if len(result) >= limit:
                            break
                    if len(result) >= limit:
                        break
                frontier = next_frontier
            return result[: max(1, int(limit))]
        finally:
            connection.close()

    def lookup(self, term: str, *, limit: int = 8) -> list[dict[str, Any]]:
        word = _clean_word(term)
        if not word:
            return []
        connection = self._connection()
        try:
            rows = connection.execute(
                """
                SELECT l.lemma,l.part_of_speech,l.pronunciation,l.synset_id,
                       s.definition_en,s.examples_json
                FROM wn_lemmas AS l JOIN wn_synsets AS s ON s.synset_id=l.synset_id
                WHERE l.lemma=? COLLATE NOCASE
                ORDER BY l.part_of_speech,l.synset_id
                LIMIT ?
                """,
                (word, max(1, min(int(limit) * 40, 240))),
            ).fetchall()
        finally:
            connection.close()
        grouped: dict[str, dict[str, Any]] = {}
        pos_names = {"n": "n.", "v": "v.", "a": "adj.", "r": "adv."}
        for lemma, part_of_speech, pronunciation, synset_id, definition_en, examples_json in rows:
            try:
                examples = json.loads(examples_json)
            except json.JSONDecodeError:
                examples = []
            key = str(lemma).casefold()
            entry = grouped.setdefault(
                key,
                {
                    "term": lemma,
                    "part_of_speech": "",
                    "senses": [],
                    "pronunciation": pronunciation,
                    "source": "Open English WordNet 2025 · local",
                    "entry_kind": "open_wordnet",
                },
            )
            sense = {
                "part_of_speech": pos_names.get(str(part_of_speech), str(part_of_speech)),
                "definition": str(definition_en or "").strip(),
                "definition_en": str(definition_en or "").strip(),
                "examples": examples if isinstance(examples, list) else [],
                "synset_id": str(synset_id),
            }
            if not any(
                existing["part_of_speech"] == sense["part_of_speech"]
                and existing["definition_en"] == sense["definition_en"]
                for existing in entry["senses"]
            ):
                entry["senses"].append(sense)
        result: list[dict[str, Any]] = []
        for entry in grouped.values():
            positions: list[str] = []
            definition_lines: list[str] = []
            all_examples: list[Any] = []
            for index, sense in enumerate(entry["senses"], start=1):
                pos = str(sense.get("part_of_speech") or "")
                if pos and pos not in positions:
                    positions.append(pos)
                prefix = f"[{pos}] " if pos else ""
                definition_lines.append(f"{index}. {prefix}{sense['definition_en']}")
                all_examples.extend(sense.get("examples") or [])
            etymology_text = self._friendly_etymology(str(entry["term"]))
            morphology = self.morphology(str(entry["term"]))
            local_entry = self._open_etymology_entry(str(entry["term"]))
            entry.update({
                "part_of_speech": " / ".join(positions),
                "definition": "\n".join(definition_lines),
                "definition_en": "\n".join(definition_lines),
                "examples": all_examples,
                "sense_count": len(entry["senses"]),
                "etymology": etymology_text,
                "etymology_source": "Etymological Wordnet 2013 · local",
                "morphology": morphology,
                "local_dictionary": local_entry,
                "local_definition_zh": str(local_entry.get("definitions_zh") or "").strip() if local_entry else "",
            })
            result.append(entry)
        if not result:
            local_entry = self._open_etymology_entry(word)
            local_morphology = self.morphology(word)
            if local_entry is not None or local_morphology is not None:
                local_entry = local_entry or {}
                result.append({
                    "term": word,
                    "part_of_speech": "",
                    "senses": [],
                    "pronunciation": str(local_entry.get("pronunciation") or ""),
                    "source": f"{local_entry.get('source_book') or 'Local morphology'} · local",
                    "entry_kind": "local_etymology",
                    "definition": "",
                    "definition_en": "",
                    "examples": [],
                    "sense_count": 0,
                    "etymology": str(local_entry.get("etymology_zh") or ""),
                    "etymology_source": str(local_entry.get("source_book") or "OpenEtymology") + " · local",
                    "morphology": local_morphology,
                    "local_dictionary": local_entry,
                    "local_definition_zh": str(local_entry.get("definitions_zh") or "").strip(),
                })
        return result[: max(1, min(int(limit), 20))]

    def _wordroot_catalog(self) -> dict[str, dict[str, Any]]:
        if self._wordroot_cache is not None:
            return self._wordroot_cache
        path = self.dictionary_root / WORDROOT_SOURCE
        if not path.is_file():
            self._wordroot_cache = {}
            return self._wordroot_cache
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        self._wordroot_cache = {
            str(key): value
            for key, value in payload.items()
            if isinstance(value, dict)
        } if isinstance(payload, dict) else {}
        for key, value in _MORPHEME_OVERRIDES.items():
            self._wordroot_cache.setdefault(key, value)
        return self._wordroot_cache

    def _open_etymology_entry(self, word: str) -> dict[str, Any] | None:
        if not self.database_path.is_file():
            return None
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT word,pronunciation,definitions_zh,morphemes_json,etymology_zh,source_book
                FROM open_etymology_entries
                WHERE word=? COLLATE NOCASE
                LIMIT 1
                """,
                (_clean_word(word),),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        if row is None:
            return None
        try:
            morphemes = json.loads(str(row[3] or "[]"))
        except json.JSONDecodeError:
            morphemes = []
        return {
            "word": str(row[0] or ""),
            "pronunciation": str(row[1] or ""),
            "definitions_zh": str(row[2] or ""),
            "morphemes": morphemes if isinstance(morphemes, list) else [],
            "etymology_zh": str(row[4] or ""),
            "source_book": str(row[5] or ""),
        }

    def _open_etymology_morphology(self, word: str) -> dict[str, Any] | None:
        entry = self._open_etymology_entry(word)
        if not entry or not entry.get("morphemes"):
            return None
        parts: list[dict[str, Any]] = []
        for raw_part in entry["morphemes"]:
            if not isinstance(raw_part, dict):
                continue
            segment = str(raw_part.get("segment") or "").strip()
            if not segment:
                continue
            meaning = str(raw_part.get("meaning") or "").strip()
            kind = "前缀" if "前缀" in meaning else "后缀" if "后缀" in meaning else "词根"
            parts.append({
                "segment": segment,
                "kind": kind,
                "meaning": meaning or "不单独表义",
                "origin": str(raw_part.get("note") or "").strip(),
                "source_key": "OpenEtymology",
            })
        if not parts:
            return None
        return {
            "term": _clean_word(word),
            "segments": " / ".join(part["segment"] for part in parts),
            "parts": parts,
            "confidence": "高",
            "source": f"{entry.get('source_book') or 'OpenEtymology'} · local",
            "local_definition_zh": str(entry.get("definitions_zh") or "").strip(),
            "local_etymology_zh": str(entry.get("etymology_zh") or "").strip(),
        }

    def _morpholex_catalog(self) -> dict[str, dict[str, Any]]:
        if self._morpholex_cache is not None:
            return self._morpholex_cache
        path = self.dictionary_root / MORPHOLEX_SOURCE
        self._morpholex_cache = {}
        if not path.is_file():
            return self._morpholex_cache
        ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        try:
            with zipfile.ZipFile(path) as archive:
                shared_strings: list[str] = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    shared_strings = [
                        "".join(node.text or "" for node in item.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
                        for item in shared.findall("main:si", ns)
                    ]
                for filename in archive.namelist():
                    if not filename.startswith("xl/worksheets/sheet") or not filename.endswith(".xml"):
                        continue
                    sheet = ET.fromstring(archive.read(filename))
                    rows = sheet.findall(".//main:row", ns)
                    if not rows:
                        continue
                    headers = self._morpholex_row_values(rows[0], shared_strings)
                    if "Word" not in headers or "MorphoLexSegm" not in headers:
                        continue
                    positions = {name: index for index, name in enumerate(headers)}
                    for row in rows[1:]:
                        values = self._morpholex_row_values(row, shared_strings, len(headers))
                        word = _clean_word(values[positions["Word"]])
                        segmentation = str(values[positions["MorphoLexSegm"]] or "").strip()
                        if word and segmentation and word not in self._morpholex_cache:
                            self._morpholex_cache[word] = {
                                "segmentation": segmentation,
                                "morphemes": max(0, int(values[positions.get("Nmorph", 0)] or 0) if str(values[positions.get("Nmorph", 0)] or "").isdigit() else 0),
                                "source": "MorphoLex-en · local",
                            }
        except (OSError, KeyError, ET.ParseError, ValueError, zipfile.BadZipFile):
            self._morpholex_cache = {}
        return self._morpholex_cache

    @staticmethod
    def _morpholex_row_values(row: ET.Element, shared_strings: list[str], width: int | None = None) -> list[str]:
        values = [""] * (width or 0)
        for cell in row.findall("main:c", {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}):
            reference = str(cell.attrib.get("r") or "")
            letters = "".join(character for character in reference if character.isalpha())
            position = 0
            for character in letters.upper():
                position = position * 26 + ord(character) - 64
            position -= 1
            value_node = cell.find("main:v", {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"})
            value = "" if value_node is None else str(value_node.text or "")
            if cell.attrib.get("t") == "s" and value:
                try:
                    value = shared_strings[int(value)]
                except (IndexError, ValueError):
                    value = ""
            if position >= len(values):
                values.extend([""] * (position + 1 - len(values)))
            if position >= 0:
                values[position] = value
        return values

    def _morpholex_morphology(self, word: str, *, allow_load: bool = True) -> dict[str, Any] | None:
        if self._morpholex_cache is None and not allow_load:
            return None
        entry = self._morpholex_catalog().get(_clean_word(word))
        if not entry:
            return None
        tokens = [token.casefold() for token in re.findall(r"[a-z]+", str(entry.get("segmentation") or ""))]
        if len(tokens) < 2 or len(tokens) != int(entry.get("morphemes") or len(tokens)):
            return None
        catalog = self._wordroot_catalog()
        parts: list[dict[str, Any]] = []
        for index, token in enumerate(tokens):
            root_entry = catalog.get(token) or catalog.get(f"-{token}") or catalog.get(f"{token}-")
            if index == 0 and len(tokens) > 2:
                kind = "前缀"
            elif index == len(tokens) - 1 and len(tokens) > 2:
                kind = "后缀"
            else:
                kind = "词根"
            dictionary_entry = self._ecdict_base_entry(token)
            dictionary_meaning = str((dictionary_entry or {}).get("translation_zh") or "").strip()
            if not dictionary_meaning:
                dictionary_meaning = str((dictionary_entry or {}).get("definition_en") or "").strip()
            # MorphoLex marks many ordinary compound words as roots.  Calling
            # them “词基” makes the learner-facing explanation accurate: each
            # part is a complete word contributing its ordinary meaning.
            if dictionary_entry is not None and len(tokens) == 2:
                kind = "词基"
            meaning = self._morpheme_meaning(root_entry) if root_entry else ""
            if not meaning and dictionary_meaning:
                meaning = dictionary_meaning
            parts.append({
                "segment": token,
                "kind": kind,
                "meaning": meaning,
                "dictionary_meaning": dictionary_meaning,
                "origin": str(root_entry.get("origin") or "") if root_entry else ("独立英语词" if dictionary_entry else ""),
                "source_key": "MorphoLex",
            })
        return {
            "term": _clean_word(word),
            "segments": " / ".join(tokens),
            "parts": parts,
            "composition": " + ".join(
                f"{part['segment']}（{part.get('meaning') or '构词成分'}）"
                for part in parts
            ),
            "confidence": "中",
            "source": "MorphoLex-en · local",
        }

    def _compound_word_morphology(self, word: str) -> dict[str, Any] | None:
        """Explain a two-word compound quickly from already indexed ECDICT rows."""
        if len(word) < 5 or not self._has_rows("ecdict_entries"):
            return None
        candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for split in range(2, len(word) - 1):
            left = self._ecdict_base_entry(word[:split])
            right = self._ecdict_base_entry(word[split:])
            if left is None or right is None:
                continue
            candidates.append((abs(len(word) // 2 - split), left, right))
        if not candidates:
            return None
        _, left, right = sorted(candidates, key=lambda item: item[0])[0]
        parts: list[dict[str, Any]] = []
        for entry in (left, right):
            meaning = str(entry.get("translation_zh") or entry.get("definition_en") or "").strip()
            parts.append({
                "segment": str(entry.get("word") or "").strip(),
                "kind": "词基",
                "meaning": meaning,
                "dictionary_meaning": meaning,
                "origin": "独立英语词",
                "source_key": "ECDICT",
            })
        whole = self._ecdict_base_entry(word)
        whole_meaning = str((whole or {}).get("translation_zh") or "").strip()
        composition = (
            f"{left['word']}（{left.get('translation_zh') or left.get('definition_en') or '词基义'}）"
            f" + {right['word']}（{right.get('translation_zh') or right.get('definition_en') or '词基义'}）"
        )
        if whole_meaning:
            composition += f"；整词常用义：{whole_meaning}"
        return {
            "term": word,
            "segments": " / ".join(str(part["segment"]) for part in parts),
            "parts": parts,
            "composition": composition,
            "confidence": "中",
            "source": "ECDICT 复合词拆分 · local",
        }

    @staticmethod
    def _morpheme_forms(key: str, entry: dict[str, Any]) -> list[str]:
        raw = str(entry.get("root") or key)
        forms: list[str] = []
        for item in raw.split(","):
            token = item.strip().strip("-")
            token = re.sub(r"-\d+$", "", token)
            if token and token not in forms:
                forms.append(token.casefold())
        return forms

    @staticmethod
    def _morpheme_kind(entry: dict[str, Any]) -> str:
        category = str(entry.get("class") or "").casefold()
        if "prefix" in category:
            return "前缀"
        if "suffix" in category:
            return "后缀"
        if "root" in category:
            return "词根"
        return "构词成分"

    @staticmethod
    def _morpheme_meaning(entry: dict[str, Any]) -> str:
        english = re.sub(r"\s+", " ", str(entry.get("meaning") or "")).strip()
        chinese = _MORPHEME_MEANINGS_ZH.get(english.casefold(), "")
        if chinese:
            return chinese
        if not english:
            return "不单独表义"
        return "构词含义暂未提供中文说明"

    def morphology(self, term: str) -> dict[str, Any] | None:
        """Split a word into attested prefixes, roots and suffixes.

        This is intentionally separate from historical etymology.  A result is
        returned only when the local root catalog supports the main segments;
        an unverified spelling split is never presented as a fact.
        """

        word = _clean_word(term)
        open_etymology = self._open_etymology_morphology(word)
        if open_etymology is not None:
            return open_etymology
        catalog = self._wordroot_catalog()
        if not word:
            return None
        if not catalog:
            return self._morpholex_morphology(word)

        override = _MORPHOLOGY_WORD_OVERRIDES.get(word)
        if override:
            final_parts: list[dict[str, Any]] = []
            for segment, source_key in override:
                entry = catalog.get(source_key or "")
                if entry is None:
                    final_parts.append({
                        "segment": segment,
                        "kind": "连接成分",
                        "meaning": "构词连接段，不单独表义",
                        "origin": "",
                        "source_key": "",
                    })
                    continue
                final_parts.append({
                    "segment": segment,
                    "kind": self._morpheme_kind(entry),
                    "meaning": self._morpheme_meaning(entry),
                    "origin": str(entry.get("origin") or "").strip(),
                    "source_key": source_key or "",
                })
            return {
                "term": word,
                "segments": " / ".join(part["segment"] for part in final_parts),
                "parts": final_parts,
                "confidence": "高",
                "source": "ECDICT Word Root Catalog · local",
            }
        candidates: dict[str, list[dict[str, Any]]] = {"前缀": [], "词根": [], "后缀": []}
        for key, entry in catalog.items():
            kind = self._morpheme_kind(entry)
            if kind not in candidates:
                continue
            examples = {
                _clean_word(str(item))
                for item in (entry.get("example") or [])
                if str(item).strip()
            }
            for form in self._morpheme_forms(key, entry):
                if len(form) < 2:
                    continue
                candidates[kind].append({
                    "form": form,
                    "entry": entry,
                    "key": key,
                    "example_match": word in examples,
                })

        prefixes = [item for item in candidates["前缀"] if word.startswith(item["form"])]
        suffixes = [item for item in candidates["后缀"] if word.endswith(item["form"])]
        roots = [item for item in candidates["词根"] if item["form"] in word]
        best: tuple[int, dict[str, Any]] | None = None
        for prefix in [None] + sorted(prefixes, key=lambda item: len(item["form"]), reverse=True):
            prefix_form = prefix["form"] if prefix else ""
            for suffix in [None] + sorted(suffixes, key=lambda item: len(item["form"]), reverse=True):
                suffix_form = suffix["form"] if suffix else ""
                if len(prefix_form) + len(suffix_form) >= len(word):
                    continue
                core_end = len(word) - len(suffix_form) if suffix_form else len(word)
                core = word[len(prefix_form):core_end]
                for root in roots:
                    root_form = root["form"]
                    if not root["example_match"]:
                        continue
                    if not core.startswith(root_form):
                        continue
                    connector = core[len(root_form):]
                    score = len(root_form) * 4 + len(prefix_form) + len(suffix_form)
                    score += 12 if root["example_match"] else 0
                    score += 8 if prefix and prefix["example_match"] else 0
                    score += 8 if suffix and suffix["example_match"] else 0
                    score -= len(connector)
                    parts: list[dict[str, Any]] = []
                    if prefix:
                        parts.append({"segment": prefix_form, "kind": "前缀", "entry": prefix})
                    parts.append({"segment": root_form, "kind": "词根", "entry": root})
                    if connector:
                        parts.append({
                            "segment": connector,
                            "kind": "连接成分",
                            "meaning": "构词连接段，不单独表义",
                            "origin": "",
                        })
                    if suffix:
                        parts.append({"segment": suffix_form, "kind": "后缀", "entry": suffix})
                    candidate = {"parts": parts, "score": score}
                    if best is None or score > best[0]:
                        best = (score, candidate)
        if best is None:
            morpholex = self._morpholex_morphology(
                word,
                allow_load=not self._lookup_cache_loading,
            )
            if morpholex is not None:
                return morpholex
            compound = self._compound_word_morphology(word)
            if compound is not None:
                return compound
            # The root catalog focuses on classical roots and does not list
            # every productive English base (for example ``turn`` in
            # ``return``).  Use the broad ECDICT headword list as a guarded
            # fallback for a prefix + ordinary base-word split.
            base_candidates: list[tuple[int, dict[str, Any], str, dict[str, Any]]] = []
            for prefix in sorted(prefixes, key=lambda item: len(item["form"]), reverse=True):
                remainder = word[len(prefix["form"]):]
                if len(remainder) < 2:
                    continue
                base_entry = self._ecdict_base_entry(remainder)
                if base_entry is not None:
                    base_candidates.append((len(prefix["form"]), prefix, remainder, base_entry))
            if base_candidates:
                _, prefix, remainder, base_entry = base_candidates[0]
                base_meaning = str(base_entry.get("translation_zh") or "").strip()
                if not base_meaning:
                    base_meaning = str(base_entry.get("definition_en") or "").strip()
                return {
                    "term": word,
                    "segments": f"{prefix['form']} / {remainder}",
                    "parts": [
                        {
                            "segment": prefix["form"],
                            "kind": "前缀",
                            "meaning": self._morpheme_meaning(prefix["entry"]),
                            "origin": str(prefix["entry"].get("origin") or "").strip(),
                            "source_key": str(prefix.get("key") or ""),
                        },
                        {
                            "segment": remainder,
                            "kind": "词基",
                            # The full ECDICT translation belongs in the
                            # Chinese dictionary section, not in the compact
                            # morpheme analysis card.
                            "meaning": "",
                            "dictionary_meaning": base_meaning,
                            "origin": "英语词基",
                            "source_key": "ECDICT",
                        },
                    ],
                    "confidence": "中",
                    "source": "ECDICT Word Root Catalog + ECDICT headwords · local",
                }
            return None
        final_parts: list[dict[str, Any]] = []
        for part in best[1]["parts"]:
            entry_info = part.get("entry")
            if entry_info:
                entry = entry_info["entry"]
                final_parts.append({
                    "segment": part["segment"],
                    "kind": part["kind"],
                    "meaning": self._morpheme_meaning(entry),
                    "origin": str(entry.get("origin") or "").strip(),
                    "source_key": str(entry_info.get("key") or ""),
                })
            else:
                final_parts.append({
                    "segment": part["segment"],
                    "kind": part["kind"],
                    "meaning": part.get("meaning", "不单独表义"),
                    "origin": "",
                    "source_key": "",
                })
        return {
            "term": word,
            "segments": " / ".join(str(part["segment"]) for part in final_parts),
            "parts": final_parts,
            "confidence": "高" if best[0] >= 25 else "中",
            "source": "ECDICT Word Root Catalog · local",
        }

    def _ecdict_base_entry(self, word: str) -> dict[str, Any] | None:
        if not self._has_rows("ecdict_entries"):
            return None
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT word,definition_en,translation_zh,part_of_speech
                FROM ecdict_entries
                WHERE word=? COLLATE NOCASE
                LIMIT 1
                """,
                (_clean_word(word),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "word": str(row[0] or ""),
            "definition_en": str(row[1] or ""),
            "translation_zh": (
                str(row[2] or "")
                .replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\r", "\n")
            ),
            "part_of_speech": str(row[3] or ""),
        }

    def lookup_chinese(self, term: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Return independent Chinese explanations from the local ECDICT data."""

        word = _clean_word(term)
        if not word:
            return []
        if not self._has_rows("ecdict_entries"):
            return []
        stripped = re.sub(r"[^a-z0-9]+", "", word)
        connection = self._connection()
        try:
            rows = connection.execute(
                """
                SELECT word,phonetic,definition_en,translation_zh,part_of_speech,
                       collins,oxford,tags,exchange
                FROM ecdict_entries
                WHERE word=? COLLATE NOCASE
                   OR (stripped_word=? AND ?<>'')
                ORDER BY CASE WHEN word=? COLLATE NOCASE THEN 0 ELSE 1 END, word
                LIMIT ?
                """,
                (word, stripped, stripped, word, max(1, min(int(limit), 20))),
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            (
                entry_word,
                phonetic,
                definition_en,
                translation_zh,
                part_of_speech,
                collins,
                oxford,
                tags,
                exchange,
            ) = row
            translation = (
                str(translation_zh or "")
                .replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\r", "\n")
                .strip()
            )
            if not translation:
                continue
            key = str(entry_word).casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "term": str(entry_word),
                    "phonetic": str(phonetic or "").strip(),
                    "definition_zh": translation,
                    "definition_en": str(definition_en or "").strip(),
                    "part_of_speech": str(part_of_speech or "").strip(),
                    "collins": str(collins or "").strip(),
                    "oxford": str(oxford or "").strip(),
                    "tags": str(tags or "").strip(),
                    "exchange": str(exchange or "").strip(),
                    "source": "ECDICT · local",
                }
            )
        return result[: max(1, min(int(limit), 20))]

