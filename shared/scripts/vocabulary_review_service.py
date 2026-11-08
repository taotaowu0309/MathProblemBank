"""Flash cards and spaced review for the Ray Du vocabulary books.

Card content is derived, never stored as a second formal copy.  《旋元佑英文字彙》
is parsed from the book's LaTeX source (``RayDu_Vocabulary/chapters/*.tex``)
and the page of every entry is read from the audio buttons of the formal PDF,
whose link annotations carry ``audio:<key>``.  《旋元佑GRE字彙通》 has no source
but its PDF and is parsed from it (``raydu_gre_parser``); its cards come after
the whole first book in the order of new words.  Both are cached per machine
in ``shared/ui/cache`` and rebuilt whenever a source file changes.

Review progress is formal data and lives in the workspace vocabulary database;
all of its reads and writes go through ``VocabularyManager``.  The words saved
on the 词汇库 page form one more unit ("词汇库"), so they are studied too.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import unicodedata
from datetime import datetime
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable

from shared.scripts.application_paths import resolve_stored_path
from shared.scripts.vocabulary_examples import ExampleLibrary, timed_call
from shared.scripts.vocabulary_manager import REVIEW_GRADES, VocabularyManager


ROOT_DIR = Path(__file__).resolve().parents[2]
RAYDU_VOCABULARY_MATERIAL_CODE = "XY-VOCABULARY"
RAYDU_BOOK_TITLE = "旋元佑英文字彙"
# The Ray Du book projects sit beside the checkout on both machines.
DEFAULT_RAYDU_VOCABULARY_PDF = (
    ROOT_DIR.parent / "EnglishProjects" / "RayDuSeries" / "RayDu_Vocabulary" / "output" / "raydu-vocabulary.pdf"
)
RAYDU_MAIN_TEX = "raydu-vocabulary-v2-font-final.tex"
CARD_INDEX_VERSION = 2
GRE_MATERIAL_CODE = "XY-GRE-VOCABULARY"
GRE_BOOK_TITLE = "旋元佑GRE字汇通"
DEFAULT_GRE_PDF = ROOT_DIR / "English" / "materials" / "originals" / f"{GRE_MATERIAL_CODE}.pdf"
# The book's own recordings come one track per root.  Cut into one MP3 per headword
# and example (``gre-<word>.mp3``, ``gre-<word>-example-<n>.mp3``) they are played
# like the 字汇 audio; until then, and for any file missing, the words are read by TTS.
DEFAULT_GRE_AUDIO_DIR = ROOT_DIR / "English" / "audio" / GRE_MATERIAL_CODE
GRE_INDEX_VERSION = 1
GRE_ORDER_OFFSET = 50_000  # GRE cards follow every card of 《英文字汇》 among the new words
BOOK_CARD_PREFIXES = ("raydu:", "gre:")
DEFAULT_CACHE_DIR = ROOT_DIR / "shared" / "ui" / "cache" / "vocabulary_cards"
VOCABULARY_UNIT_KEY = "vocabulary"
VOCABULARY_UNIT_TITLE = "词汇库"
LIBRARY_CARD_PREFIX = "vocab:"
REVIEW_MODES = {
    "learn": "学习新词",
    "recognize": "看英文想中文",
    "dictation_word": "听写单词",
    "dictation_cloze": "听例句填空",
    "spell": "看中文写英文",
}
POS_LABELS = {
    "名": "名", "动": "动", "形": "形", "副": "副", "介": "介", "连": "连", "代": "代",
    "n.": "名", "v.": "动", "adj.": "形", "adv.": "副", "prep.": "介", "conj.": "连", "pron.": "代",
}
VOCABULARY_POS = {
    "名": "n.", "动": "v.", "形": "adj.", "副": "adv.", "介": "prep.", "连": "conj.", "代": "pron.",
}


# ---------------------------------------------------------------------------
# LaTeX source parsing
# ---------------------------------------------------------------------------

def _read_group(text: str, position: int) -> tuple[str, int] | None:
    """Read one balanced ``{...}`` argument starting at ``position`` (spaces allowed)."""

    index = position
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    if index >= len(text) or text[index] != "{":
        return None
    depth = 0
    start = index + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return None


def _read_optional(text: str, position: int) -> tuple[str, int] | None:
    index = position
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text) or text[index] != "[":
        return None
    depth = 0
    for cursor in range(index, len(text)):
        if text[cursor] == "[":
            depth += 1
        elif text[cursor] == "]":
            depth -= 1
            if depth == 0:
                return text[index + 1:cursor], cursor + 1
    return None


def _read_arguments(text: str, position: int, count: int) -> tuple[list[str], int] | None:
    values: list[str] = []
    for _ in range(count):
        group = _read_group(text, position)
        if group is None:
            return None
        values.append(group[0])
        position = group[1]
    return values, position


def tex_plain_text(value: str) -> str:
    """Readable text for the small macro set used inside vocabulary entries."""

    text = str(value or "")
    for number, name in enumerate(("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten")):
        text = text.replace(f"\\RayDuCircled{name}", chr(0x2460 + number))
    text = text.replace("``", "“").replace("''", "”").replace("`", "‘")
    text = text.replace("\\\\", "\n").replace("~", " ").replace("\\,", " ")
    text = re.sub(r"\\(?:&|%|\$|#|_)", lambda match: match.group(0)[1:], text)
    for _ in range(4):
        text = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[A-Za-z@]+\*?", "", text)
    text = text.replace("{", "").replace("}", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _chapter_files(vocabulary_root: Path) -> list[Path]:
    """Chapter files in book order, taken from the active ``\\input`` lines of the main file."""

    chapters = vocabulary_root / "chapters"
    main = vocabulary_root / RAYDU_MAIN_TEX
    ordered: list[Path] = []
    if main.is_file():
        for line in main.read_text(encoding="utf-8", errors="replace").splitlines():
            code = line.split("%", 1)[0]
            match = re.search(r"\\input\{chapters/([^{}]+)\}", code)
            if match:
                name = match.group(1)
                path = chapters / (name if name.endswith(".tex") else f"{name}.tex")
                if path.is_file():
                    ordered.append(path)
    return ordered or sorted(chapters.glob("*.tex"))


def parse_vocabulary_book(vocabulary_root: Path | str) -> list[dict[str, Any]]:
    """Every ``VocabEntry`` of the book, in reading order, with all card fields."""

    root = Path(vocabulary_root)
    entries: list[dict[str, Any]] = []
    token = re.compile(
        r"\\(VocabSectionTitle|VocabPrefixBand|begin\{VocabEntry\}|end\{VocabEntry\}"
        r"|VocabMorph|VocabSense|VocabDerived|VocabExample|VocabDialogueExample)"
    )
    for source_file in _chapter_files(root):
        text = source_file.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"(?<!\\)%[^\n]*", "", text)
        section = ""
        band: dict[str, str] = {}
        current: dict[str, Any] | None = None
        position = 0
        while True:
            match = token.search(text, position)
            if match is None:
                break
            name = match.group(1)
            position = match.end()
            if name == "VocabSectionTitle":
                parsed = _read_arguments(text, position, 1)
                if parsed:
                    section = tex_plain_text(parsed[0][0])
                    band = {}
                    position = parsed[1]
            elif name == "VocabPrefixBand":
                parsed = _read_arguments(text, position, 2)
                if parsed:
                    band = {"root": tex_plain_text(parsed[0][0]), "meaning": tex_plain_text(parsed[0][1])}
                    position = parsed[1]
            elif name == "begin{VocabEntry}":
                parsed = _read_arguments(text, position, 3)
                if parsed:
                    term, pronunciation, audio_key = parsed[0]
                    position = parsed[1]
                    current = {
                        "term": tex_plain_text(term),
                        "pronunciation": tex_plain_text(pronunciation),
                        "audio_key": audio_key.strip(),
                        "morph": [],
                        "senses": [],
                        "derived": [],
                        "examples": [],
                        "section_title": section,
                        "band": dict(band),
                        "source_file": source_file.name,
                        "source_line": text.count("\n", 0, match.start()) + 1,
                    }
            elif name == "end{VocabEntry}":
                if current is not None and current["term"]:
                    current["order"] = len(entries)
                    entries.append(current)
                current = None
            elif current is None:
                continue
            elif name == "VocabMorph":
                parsed = _read_arguments(text, position, 2)
                if parsed:
                    current["morph"].append({
                        "segments": tex_plain_text(parsed[0][0]),
                        "meanings": tex_plain_text(parsed[0][1]),
                    })
                    position = parsed[1]
            elif name == "VocabSense":
                parsed = _read_arguments(text, position, 2)
                if parsed:
                    current["senses"].append({
                        "pos": tex_plain_text(parsed[0][0]),
                        "definition": tex_plain_text(parsed[0][1]),
                    })
                    position = parsed[1]
            elif name == "VocabDerived":
                parsed = _read_arguments(text, position, 3)
                if parsed:
                    current["derived"].append({
                        "term": tex_plain_text(parsed[0][0]),
                        "pos": tex_plain_text(parsed[0][1]),
                        "definition": tex_plain_text(parsed[0][2]),
                    })
                    position = parsed[1]
            else:  # VocabExample / VocabDialogueExample
                parsed = _read_arguments(text, position, 4)
                if parsed:
                    audio_term, number, english, chinese = parsed[0]
                    position = parsed[1]
                    display = _read_optional(text, position)
                    if display is not None and display[0].strip():
                        english = display[0]
                        position = display[1]
                    current["examples"].append({
                        "number": int(number) if number.strip().isdigit() else 0,
                        "audio_key": f"{audio_term.strip()}-example-{number.strip()}",
                        "en": tex_plain_text(english),
                        "zh": tex_plain_text(chinese),
                    })
    return entries


# ---------------------------------------------------------------------------
# Formal PDF: bookmarks and audio-button pages
# ---------------------------------------------------------------------------

def read_pdf_structure(pdf_path: Path | str) -> dict[str, Any]:
    """Bookmarks ``(level, title, page)`` and the pages of every ``audio:<key>`` button."""

    import fitz

    tools = getattr(fitz, "TOOLS", None)
    if tools is not None:
        try:
            tools.mupdf_display_errors(False)
        except Exception:
            pass
    audio_pages: dict[str, list[int]] = {}
    with fitz.open(str(pdf_path)) as document:
        toc = [
            (int(level), str(title).strip(), int(page))
            for level, title, page, *_rest in document.get_toc(simple=False)
            if int(page) > 0
        ]
        for page in document:
            for xref, _kind, _ident in page.annot_xrefs():
                kind, value = document.xref_get_key(xref, "Contents")
                if kind == "string" and value.startswith("audio:"):
                    pages = audio_pages.setdefault(value[6:].strip(), [])
                    if page.number + 1 not in pages:
                        pages.append(page.number + 1)
        page_count = int(document.page_count)
    return {"toc": toc, "audio_pages": audio_pages, "page_count": page_count}


def _toc_ranges(toc: list[tuple[int, str, int]], page_count: int) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    path: dict[int, str] = {}
    for index, (level, title, page) in enumerate(toc):
        path[level] = title
        for deeper in [key for key in path if key > level]:
            del path[deeper]
        end = page_count
        for later_level, _later_title, later_page in toc[index + 1:]:
            if later_level <= level:
                end = max(page, later_page - 1) if later_page > page else page
                break
        ranges.append({
            "index": index,
            "level": level,
            "title": title,
            "page_start": page,
            "page_end": end,
            "path": " › ".join(path[key] for key in sorted(path)),
        })
    return ranges


def _fold_title(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def build_card_index(vocabulary_root: Path | str, pdf_path: Path | str | None) -> dict[str, Any]:
    """Merge the TeX entries with PDF pages and bookmarks into cards and study units."""

    entries = parse_vocabulary_book(vocabulary_root)
    structure = (
        read_pdf_structure(pdf_path)
        if pdf_path is not None and Path(pdf_path).is_file()
        else {"toc": [], "audio_pages": {}, "page_count": 0}
    )
    ranges = _toc_ranges(structure["toc"], int(structure["page_count"]))
    audio_pages: dict[str, list[int]] = structure["audio_pages"]

    # One unit per section of one chapter file.  Section titles repeat between
    # Part 2 and Part 4, so they are matched to the bookmarks sequentially,
    # always moving forward through the outline.  Part 5 has no section titles
    # and is placed by the page of its first audio button instead.
    groups: list[list[dict[str, Any]]] = []
    for entry in entries:
        if groups and (groups[-1][0]["source_file"], groups[-1][0]["section_title"]) == (
            entry["source_file"], entry["section_title"]
        ):
            groups[-1].append(entry)
        else:
            groups.append([entry])
    units: list[dict[str, Any]] = []
    unit_of_entry: dict[int, dict[str, Any]] = {}
    cursor = 0
    floor_page = 1
    for group in groups:
        title = group[0]["section_title"]
        bookmark = None
        if title and units and units[-1]["section_title"] == title:
            bookmark = units[-1]["bookmark"]
        elif title:
            folded = _fold_title(title)
            for exact in (True, False):
                for index in range(cursor, len(ranges)):
                    candidate = _fold_title(ranges[index]["title"])
                    if candidate == folded if exact else candidate.startswith(folded):
                        bookmark = ranges[index]
                        cursor = index + 1
                        break
                if bookmark is not None:
                    break
        else:
            first_pages = [
                page for page in audio_pages.get(group[0]["audio_key"]) or [] if page >= floor_page
            ]
            if first_pages:
                covering = [
                    item for item in ranges
                    if item["page_start"] <= first_pages[0] <= item["page_end"]
                ]
                if covering:
                    bookmark = max(covering, key=lambda item: (item["level"], item["page_start"]))
        if bookmark is not None:
            floor_page = max(floor_page, int(bookmark["page_start"]))
        label = title or f"{group[0]['term']} – {group[-1]['term']}"
        unit = {
            "unit_key": f"raydu:{len(units) + 1:03d}",
            "title": label,
            "section_title": title,
            "path": (bookmark["path"] if bookmark else label) if title else (
                f"{bookmark['path']} › {label}" if bookmark else label
            ),
            "page_start": bookmark["page_start"] if bookmark else None,
            "page_end": bookmark["page_end"] if bookmark else None,
            "bookmark_index": bookmark["index"] if bookmark else None,
            "bookmark": bookmark,
            "floor_page": floor_page,
            "bands": [],
            "card_keys": [],
        }
        units.append(unit)
        for entry in group:
            unit_of_entry[int(entry["order"])] = unit

    cards: dict[str, dict[str, Any]] = {}
    for entry in entries:
        unit = unit_of_entry[int(entry["order"])]
        pages = audio_pages.get(entry["audio_key"]) or []
        page = None
        if unit["page_start"] is not None:
            page = next(
                (value for value in pages if unit["page_start"] <= value <= unit["page_end"]),
                None,
            )
        if page is None and pages:
            later = [value for value in pages if value >= unit["floor_page"]]
            page = (later or pages)[0]
        band = entry["band"].get("root", "")
        if band and band not in unit["bands"]:
            unit["bands"].append(band)
        location = {
            "unit_key": unit["unit_key"],
            "path": unit["path"],
            "band": band,
            "band_meaning": entry["band"].get("meaning", ""),
            "page": page,
            "source_file": entry["source_file"],
            "source_line": entry["source_line"],
        }
        for example in entry["examples"]:
            example_pages = audio_pages.get(example["audio_key"]) or []
            example["page"] = next(
                (value for value in example_pages if page is None or value >= page),
                example_pages[0] if example_pages else page,
            )
        card_key = f"raydu:{(entry['audio_key'] or entry['term']).casefold()}"
        card = cards.get(card_key)
        if card is None:
            card = {
                "card_key": card_key,
                "deck": "raydu",
                "term": entry["term"],
                "pronunciation": entry["pronunciation"],
                "audio_key": entry["audio_key"],
                "morph": list(entry["morph"]),
                "senses": list(entry["senses"]),
                "derived": list(entry["derived"]),
                "examples": list(entry["examples"]),
                "locations": [location],
                "order": entry["order"],
            }
            cards[card_key] = card
        else:
            # A headword repeated in a later section keeps one card; its extra
            # senses, derived words and examples are merged without duplicates.
            for field, identity in (
                ("morph", ("segments", "meanings")),
                ("senses", ("pos", "definition")),
                ("derived", ("term", "pos")),
                ("examples", ("audio_key", "en")),
            ):
                known = {tuple(item.get(name) for name in identity) for item in card[field]}
                for item in entry[field]:
                    if tuple(item.get(name) for name in identity) not in known:
                        card[field].append(item)
            card["locations"].append(location)
        if card_key not in unit["card_keys"]:
            unit["card_keys"].append(card_key)
    units = [unit for unit in units if unit["card_keys"]]
    for unit in units:
        if not unit["section_title"]:
            # Untitled Part 5 groups share one bookmark; narrow each to its own pages.
            pages = [
                location["page"]
                for key in unit["card_keys"]
                for location in cards[key]["locations"]
                if location["unit_key"] == unit["unit_key"] and location.get("page")
            ]
            if pages:
                unit["page_start"], unit["page_end"] = min(pages), max(pages)
        del unit["bookmark"], unit["floor_page"]
    return {
        "version": CARD_INDEX_VERSION,
        "book_title": RAYDU_BOOK_TITLE,
        "entry_count": len(entries),
        "cards": cards,
        "units": units,
    }


def _gre_slug(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode().casefold()).strip("-")


def build_gre_card_index(pdf_path: Path | str) -> dict[str, Any]:
    """Cards and study units of 《旋元佑GRE字汇通》 (one unit per root, then per letter)."""

    from shared.scripts.raydu_gre_parser import parse_book

    book = parse_book(pdf_path)
    if book["issues"]:
        raise ValueError(
            f"《{GRE_BOOK_TITLE}》PDF 有 {len(book['issues'])} 处无法可靠解析：\n" + "\n".join(book["issues"][:12])
        )
    sections = {1: "Section 1 常用字根字首", 2: "Section 2 冷僻字根"}
    units: list[dict[str, Any]] = []
    unit_of: dict[str, dict[str, Any]] = {}
    for item in book["units"]:
        root, _, meaning = item["title"].partition(":")
        title = item["title"] if item["section"] == 1 else f"{item['title']} 字首"
        unit = {
            "unit_key": item["unit_key"],
            "title": title,
            "section_title": title,
            "path": f"《{GRE_BOOK_TITLE}》{sections[item['section']]} › {title}",
            "page_start": item["page_start"],
            "page_end": item["page_end"],
            "bookmark_index": None,
            "bands": [root.strip()] if item["section"] == 1 else [],
            "band_meaning": meaning.strip() if item["section"] == 1 else "",
            "track": item["track"],
            "card_keys": [],
        }
        units.append(unit)
        unit_of[item["unit_key"]] = unit
    cards: dict[str, dict[str, Any]] = {}
    for entry in book["entries"]:
        unit = unit_of[entry["unit_key"]]
        slug = _gre_slug(entry["term"])
        key = f"gre:{entry['term'].casefold()}"
        examples = []
        for sense in entry["senses"]:
            for example in sense["examples"]:
                examples.append({
                    "number": len(examples) + 1,
                    "audio_key": f"gre-{slug}-example-{len(examples) + 1}",
                    "en": example["en"],
                    "zh": example["zh"],
                    "page": entry["page"],
                })
        cards[key] = {
            "card_key": key,
            "deck": "gre",
            "book_title": GRE_BOOK_TITLE,
            "term": entry["term"],
            "pronunciation": entry["pronunciation"],
            "audio_key": f"gre-{slug}",
            "morph": [{
                "segments": item["segments"],
                "meanings": " ".join(part for part in (item["meanings"], item.get("note", "")) if part),
            } for item in entry["morph"]],
            "senses": [{"pos": sense["pos"], "definition": sense["definition"]} for sense in entry["senses"]],
            "synonyms": entry["synonyms"],
            "derived": entry["derived"],
            "examples": examples,
            "locations": [{
                "unit_key": unit["unit_key"],
                "path": unit["path"],
                "band": unit["bands"][0] if unit["bands"] else "",
                "band_meaning": unit["band_meaning"],
                "page": entry["page"],
                "track": unit["track"],
            }],
            "order": GRE_ORDER_OFFSET + int(entry["order"]),
        }
        unit["card_keys"].append(key)
    return {
        "version": GRE_INDEX_VERSION,
        "book_title": GRE_BOOK_TITLE,
        "entry_count": len(book["entries"]),
        "cards": cards,
        "units": units,
        "warnings": book["warnings"],
    }


def _morph_key(segments: str) -> str:
    return re.sub(r"[\s()]", "", str(segments or "")).casefold()


def _same_text_key(text: str) -> str:
    """Letters and characters only: "尖酸，刻薄" and "尖酸；刻薄" are the same sense,
    and two examples differing by a comma are one example."""
    return re.sub(r"[\W_]+", "", str(text or "")).casefold()


_EXAMPLE_STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "on", "at", "for", "by", "with", "from", "and", "or", "but",
    "be", "is", "are", "was", "were", "been", "has", "have", "had", "it", "its", "he", "she", "his", "her",
    "they", "their", "we", "our", "you", "your", "i", "my", "me", "him", "them", "that", "this", "these",
    "those", "there", "as", "so", "not", "no", "do", "did", "s", "t", "one", "who", "which",
}


def _example_tokens(text: str) -> list[str]:
    tokens = []
    for word in re.findall(r"[a-z]+", str(text or "").replace("\u2019", "'").casefold()):
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
        tokens.append(word)
    return tokens


def _repeats_example(candidate: str, existing: str, term: str = "") -> bool:
    """Whether ``candidate`` says again what ``existing`` says.

    Measured by the share of the candidate's words found, in order, in the
    other sentence: "a stampede to the exits" in "The fire alarm caused a
    stampede to the exits." (1.0), "After several months in a hospital bed, my
    leg muscles had atrophied." in "After two months in the hospital, ..." (0.75).
    Two shared content words besides the headword also count from 0.6 on
    ("Cigarettes contain dozens of carcinogens." / "... contain several ...");
    two collocations sharing only the headword ("a tortuous conspiracy" /
    "a tortuous path") stay apart.
    """

    words, other = _example_tokens(candidate), _example_tokens(existing)
    if not words or not other:
        return False
    if _same_text_key(candidate) == _same_text_key(existing):
        return True
    blocks = difflib.SequenceMatcher(None, words, other, autojunk=False).get_matching_blocks()
    matched = [word for block in blocks for word in words[block.a:block.a + block.size]]
    share = len(matched) / len(words)
    head = set(_example_tokens(term))
    content = [word for word in matched if word not in _EXAMPLE_STOPWORDS and word not in head]
    return share >= 0.75 or (share >= 0.6 and len(content) >= 2)


def merge_book_cards(raydu: dict[str, Any], gre: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Both books' cards, a word of both books being one card.

    The 《英文字汇》 card keeps its key, headword, pronunciation and place among the
    new words; the GRE senses, word analysis (when written differently), examples,
    synonyms, derived words and location are added to it, marked ``source: gre``.
    The GRE unit lists the merged card in place of its own, so both books' units
    stay complete and the word is studied once.  The cached indexes are not changed.
    """

    cards: dict[str, Any] = dict(raydu["cards"])
    headwords = {card["term"].casefold(): key for key, card in raydu["cards"].items()}
    replaced: dict[str, str] = {}
    for key, card in gre["cards"].items():
        target = headwords.get(card["term"].casefold())
        if target is None:
            cards[key] = card
            continue
        merged = json.loads(json.dumps(cards[target], ensure_ascii=False))
        for sense in card["senses"]:
            # A GRE sense of a part of speech the card has already adds its new meanings
            # to that sense ("无底的；极度的；极糟的" + "极恶劣的"); another part of speech is a new sense.
            same = next((item for item in merged["senses"] if item.get("pos") == sense["pos"]), None)
            if same is None:
                merged["senses"].append({**sense, "source": "gre"})
                continue
            meanings = [part for part in re.split(r"[；;]", str(same.get("definition") or "")) if part.strip()]
            keys = {_same_text_key(part) for part in meanings}
            added = [part for part in re.split(r"[；;]", sense["definition"])
                     if part.strip() and _same_text_key(part) not in keys]
            if added:
                same["definition"] = "；".join([*meanings, *added])
        morphs = {_morph_key(item.get("segments")) for item in merged["morph"]}
        merged["morph"].extend({**item, "source": "gre"} for item in card["morph"] if _morph_key(item["segments"]) not in morphs)
        merged["synonyms"] = list(dict.fromkeys([*merged.get("synonyms", []), *card.get("synonyms", [])]))
        derived = {item["term"].casefold() for item in merged["derived"]}
        merged["derived"].extend(item for item in card["derived"] if item["term"].casefold() not in derived)
        # A GRE example that repeats one of the card's 字汇 examples (often the same
        # sentence cut to a phrase, or a light rewording) is left out.
        own = list(merged["examples"])
        merged["examples"].extend({**example, "source": "gre", "deck": "gre"} for example in card["examples"]
                                  if not any(_repeats_example(example["en"], item.get("en"), merged["term"]) for item in own))
        for number, example in enumerate(merged["examples"], start=1):
            example["number"] = number
        merged["locations"].extend({**location, "deck": "gre"} for location in card["locations"])
        merged["also_in"] = [GRE_BOOK_TITLE]
        cards[target] = merged
        replaced[key] = target
    units = [{**unit, "card_keys": [replaced.get(key, key) for key in unit["card_keys"]]} for unit in gre["units"]]
    return cards, units, len(replaced)


_MEANING_STOPWORDS = {"a", "an", "the", "of", "and", "or", "intensifier"}


def _band_variants(band: str) -> list[str]:
    """``in- (il-, im-, ir-)`` / ``acr, acu`` → the spellings of the root."""
    return [token for token in (part.strip("*\u2019'").casefold() for part in re.split(r"[,()\s/]+", band)) if token]


def _band_meanings(meaning: str) -> list[str]:
    return [part for part in (item.strip(" *").replace("\u2019", "'").casefold() for item in re.split(r"[,;]", meaning))
            if part]


def _meaning_words(meaning: str) -> set[str]:
    return {word for word in re.findall(r"[a-z']+", meaning.replace("\u2019", "'").casefold())
            if word not in _MEANING_STOPWORDS}


def _same_root(first: tuple[str, str], second: tuple[str, str]) -> bool:
    """Two root labels name one root when they share a spelling and a meaning
    ("pass (pati, path): feel, suffer" and "path, pass, pati: feel"); "in-: not"
    and "in-: in" share only the spelling and stay apart."""
    if not set(_band_variants(first[0])) & set(_band_variants(second[0])):
        return False
    meanings = _meaning_words(first[1]), _meaning_words(second[1])
    return bool(meanings[0] & meanings[1]) or not meanings[0] or not meanings[1]


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def build_merged_directory(raydu: dict[str, Any], cards: dict[str, Any], *, chunk: int = 30) -> list[dict[str, Any]]:
    """The directory of 《英文字汇》, with the other book's words placed in it.

    Its Parts and sections stay as they are, each word once (a word the book repeats
    stays in its first section).  A word of the other book goes into the section of
    the same root (after that root's words); words of a root the book does not have
    go into new sections at the end of Part 1 (其他字首) and Part 2 (其他字根①②…, by
    root in alphabetical order); words without a root join Part 5 (其他), which is
    regrouped alphabetically into sections of about ``chunk`` words.
    """

    units: list[dict[str, Any]] = []
    placed: set[str] = set()
    labels: list[tuple[tuple[str, str], int]] = []  # the book's root labels and their unit
    other_book = [key for key in cards if not key.startswith("raydu:")]
    untitled: list[str] = []
    part_names = {"1": "", "2": "", "5": ""}
    for unit in raydu["units"]:
        part = str(unit["path"]).partition(" › ")[0]
        number = re.match(r"Part (\d+)", part)
        if number and number.group(1) in part_names and not part_names[number.group(1)]:
            part_names[number.group(1)] = part
        if not unit["section_title"]:
            untitled.extend(key for key in unit["card_keys"] if key not in placed and key in cards)
            placed.update(unit["card_keys"])
            continue
        keys = [key for key in unit["card_keys"] if key not in placed and key in cards]
        placed.update(keys)
        position = len(units)
        units.append({**unit, "card_keys": keys, "bands": list(unit["bands"])})
        for key in keys:
            for location in cards[key]["locations"]:
                if location.get("unit_key") == unit["unit_key"] and location.get("band"):
                    label = (str(location["band"]), str(location.get("band_meaning") or ""))
                    if not any(known == label for known, _unit in labels):
                        labels.append((label, position))
    # The other book's words: into the section of their root, after that root's words.
    new_roots: dict[tuple[str, str], list[str]] = {}
    for key in other_book:
        location = cards[key]["locations"][0]
        if not location.get("band"):
            untitled.append(key)
            continue
        label = (str(location["band"]), str(location.get("band_meaning") or ""))
        target = next(((known, position) for known, position in labels if _same_root(known, label)), None)
        if target is None:
            new_roots.setdefault(label, []).append(key)
            continue
        known, position = target
        unit = units[position]
        same_root = [index for index, card_key in enumerate(unit["card_keys"])
                     if any(_same_root(known, (str(item.get("band") or ""), str(item.get("band_meaning") or "")))
                            for item in cards[card_key]["locations"] if item.get("band"))]
        unit["card_keys"].insert((same_root[-1] + 1) if same_root else len(unit["card_keys"]), key)

    def section(part: str, title: str, keys: list[str], bands: list[str], key: str) -> dict[str, Any]:
        return {"unit_key": key, "title": title, "section_title": title, "path": f"{part} › {title}" if part else title,
                "page_start": None, "page_end": None, "bookmark_index": None, "bands": bands, "card_keys": keys}

    def insert_after_part(number: str, extra: list[dict[str, Any]]) -> None:
        part = part_names[number]
        last = max((index for index, unit in enumerate(units) if str(unit["path"]).startswith(part + " › ")), default=len(units) - 1)
        units[last + 1:last + 1] = extra

    def sort_key(label: tuple[str, str]) -> str:
        return re.sub(r"[^a-z]", "", (_band_variants(label[0]) or [""])[0])

    prefixes = sorted((label for label in new_roots if (_band_variants(label[0]) or [""])[0].endswith("-")), key=sort_key)
    roots = sorted((label for label in new_roots if label not in prefixes), key=sort_key)
    if prefixes:
        keys = [key for label in prefixes for key in new_roots[label]]
        bands = [(_band_variants(label[0]) or [label[0]])[0] for label in prefixes]
        insert_after_part("1", [section(part_names["1"], "其他字首", keys, bands, "extra:prefixes")])
    extra: list[dict[str, Any]] = []
    group: list[tuple[str, str]] = []

    def close_group() -> None:
        if group:
            number = _CIRCLED[len(extra)] if len(extra) < len(_CIRCLED) else f"({len(extra) + 1})"
            extra.append(section(part_names["2"], f"其他字根{number}", [key for label in group for key in new_roots[label]],
                                 [(_band_variants(label[0]) or [label[0]])[0] for label in group],
                                 f"extra:roots-{len(extra) + 1}"))
            group.clear()

    for label in roots:
        if group and sum(len(new_roots[item]) for item in group) + len(new_roots[label]) > chunk * 2:
            close_group()
        group.append(label)
    close_group()
    if extra:
        insert_after_part("2", extra)
    words = sorted(untitled, key=lambda key: cards[key]["term"].casefold())
    for start in range(0, len(words), chunk):
        part = words[start:start + chunk]
        title = f"{cards[part[0]]['term']} – {cards[part[-1]]['term']}"
        units.append(section(part_names["5"] or "其他字汇", title, part, [], f"other:{start // chunk + 1}"))
    return [unit for unit in units if unit["card_keys"]]


# ---------------------------------------------------------------------------
# Answer checking
# ---------------------------------------------------------------------------

def normalize_answer(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip(" .,!?;:\"“”")


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def grade_typed_answer(answer: str, expected: str | Iterable[str]) -> dict[str, Any]:
    """Suggest a grade for a typed answer: exact → good, one slip → hard, otherwise again."""

    targets = [expected] if isinstance(expected, str) else list(expected)
    typed = normalize_answer(answer)
    best = None
    for target in targets:
        wanted = normalize_answer(target)
        if not wanted:
            continue
        distance = _edit_distance(typed, wanted)
        if best is None or distance < best[0]:
            best = (distance, target)
    if best is None:
        raise ValueError("没有可核对的标准答案。")
    distance, target = best
    if distance == 0:
        grade = "good"
    elif typed and distance == 1 and len(normalize_answer(target)) >= 4:
        grade = "hard"
    else:
        grade = "again"
    return {"correct": distance == 0, "distance": distance, "expected": target, "grade": grade}


_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")


def _stem(value: str) -> str:
    word = value.casefold()
    if len(word) > 4 and word.endswith(("e", "y")):
        return word[:-1]
    return word


def cloze_example(sentence: str, term: str, derived: Iterable[str] = ()) -> dict[str, Any] | None:
    """Blank the form of ``term`` (or one of its derived words) that the sentence actually uses."""

    text = str(sentence or "")
    head = str(term or "").strip()
    if not text or not head:
        return None
    if " " in head:
        match = re.search(re.escape(head), text, flags=re.IGNORECASE)
        if match is None:
            return None
        return {
            "prompt": text[:match.start()] + "_____" + text[match.end():],
            "answer": match.group(0),
        }
    candidates = [head, *[item for item in derived if item and " " not in item]]
    tokens = list(_WORD_RE.finditer(text))
    chosen = None
    for rank, (exact_only, words) in enumerate(((True, candidates), (False, candidates))):
        del rank
        for word in words:
            for token in tokens:
                surface = token.group(0).casefold()
                if exact_only and surface == word.casefold():
                    chosen = token
                elif not exact_only and len(_stem(word)) >= 3 and surface.startswith(_stem(word)) \
                        and len(surface) - len(word) <= 4:
                    chosen = token
                if chosen is not None:
                    break
            if chosen is not None:
                break
        if chosen is not None:
            break
    if chosen is None:
        return None
    return {
        "prompt": text[:chosen.start()] + "_____" + text[chosen.end():],
        "answer": chosen.group(0),
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class VocabularyReviewService:
    """Cards, units, review queues and grading shared by Qt and the AI executor."""

    def __init__(
        self,
        vocabulary_manager: VocabularyManager | None = None,
        *,
        english_service: Any = None,
        dictionary_service: Any = None,
        cache_dir: Path | str | None = None,
        vocabulary_root: Path | str | None = None,
        pdf_path: Path | str | None = None,
        examples: ExampleLibrary | None = None,
        gre_pdf_path: Path | str | None = None,
        gre_audio_dir: Path | str | None = None,
    ) -> None:
        self.manager = vocabulary_manager or VocabularyManager.for_workspace("english")
        self.english_service = english_service
        self.dictionary_service = dictionary_service
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self._vocabulary_root = Path(vocabulary_root) if vocabulary_root else None
        self._pdf_path = Path(pdf_path) if pdf_path else None
        self._explicit_pdf_path = self._pdf_path
        self._explicit_vocabulary_root = self._vocabulary_root
        # 《GRE字汇通》 is found on its own unless the service was built for a given
        # 《英文字汇》 source (tests, tools): then only an explicit PDF adds it.
        self._gre_pdf_path = Path(gre_pdf_path) if gre_pdf_path else None
        self._gre_enabled = bool(gre_pdf_path) or (vocabulary_root is None and pdf_path is None)
        self._gre_audio_dir = Path(gre_audio_dir) if gre_audio_dir else DEFAULT_GRE_AUDIO_DIR
        self._raydu_index: dict[str, Any] | None = None
        self._gre_index: dict[str, Any] | None = None
        self._index: dict[str, Any] | None = None
        self._index_lock = threading.Lock()
        self._lookup_cache: dict[str, dict[str, Any]] = {}
        self._derived_cache: dict[str, list[dict[str, str]]] = {}
        self._english_cache: dict[str, tuple[str, str]] = {}
        self.examples = examples or ExampleLibrary()
        self._data_signature: tuple[Any, ...] | None = None
        self._term_map_index: Any = None
        self._term_map: dict[str, str] = {}

    def refresh_data_caches(self) -> None:
        paths = [self.manager.database, Path(str(self.manager.database) + "-wal")]
        if self.english_service is not None:
            paths.append(self.english_service.database_path)
        if getattr(self.dictionary_service, "database_path", None) is not None:
            paths.append(self.dictionary_service.database_path)
        signature = []
        for path in paths:
            try:
                stat = path.stat()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                signature.append((str(path), None))
        current = tuple(signature)
        if current != self._data_signature:
            self._lookup_cache.clear()
            self._english_cache.clear()
            self._pdf_path = self._explicit_pdf_path
            self._vocabulary_root = self._explicit_vocabulary_root
            self._data_signature = current

    # -- sources ----------------------------------------------------------

    def book_pdf_path(self) -> Path:
        if self._pdf_path is None:
            path = DEFAULT_RAYDU_VOCABULARY_PDF
            material = self._book_material()
            stored = str((material or {}).get("reading_path") or (material or {}).get("source_path") or "")
            if stored and resolve_stored_path(stored).is_file():
                path = resolve_stored_path(stored)
            self._pdf_path = path
        return self._pdf_path

    def vocabulary_root(self) -> Path:
        if self._vocabulary_root is None:
            self._vocabulary_root = self.book_pdf_path().parent.parent
        return self._vocabulary_root

    def audio_dir(self) -> Path:
        return self.vocabulary_root() / "audio"

    def gre_pdf_path(self) -> Path | None:
        """The PDF of 《GRE字汇通》 on this machine (registered under 基础课程), if any."""

        if not self._gre_enabled:
            return None
        if self._gre_pdf_path is None:
            path = DEFAULT_GRE_PDF
            material = self._book_material(GRE_MATERIAL_CODE)
            for field in ("reading_path", "source_path"):
                stored = str((material or {}).get(field) or "")
                if stored and resolve_stored_path(stored).is_file():
                    path = resolve_stored_path(stored)
                    break
            self._gre_pdf_path = path
        return self._gre_pdf_path if self._gre_pdf_path.is_file() else None

    def gre_audio_dir(self) -> Path:
        return self._gre_audio_dir

    def deck_audio_dir(self, deck: str) -> Path:
        return self.gre_audio_dir() if deck == "gre" else self.audio_dir()

    def deck_pdf_path(self, deck: str) -> Path | None:
        return self.gre_pdf_path() if deck == "gre" else self.book_pdf_path()

    def _book_material(self, code: str = RAYDU_VOCABULARY_MATERIAL_CODE) -> dict[str, Any] | None:
        service = self.english_service
        if service is None:
            return None
        try:
            with service.connect(rows=True) as connection:
                row = connection.execute(
                    "SELECT * FROM english_materials WHERE material_code=?", (code,),
                ).fetchone()
        except Exception:
            return None
        return dict(row) if row is not None else None

    def book_deck_for_pdf(self, pdf_path: Path | str | None) -> str | None:
        """``raydu`` or ``gre`` when ``pdf_path`` is one of the vocabulary books."""

        if not pdf_path:
            return None
        try:
            target = Path(pdf_path).resolve()
            if target == self.book_pdf_path().resolve():
                return "raydu"
            gre = self.gre_pdf_path()
            if gre is not None and target == gre.resolve():
                return "gre"
        except OSError:
            return None
        return None

    def is_book_pdf(self, pdf_path: Path | str | None) -> bool:
        return self.book_deck_for_pdf(pdf_path) is not None

    # -- card index (derived cache) ------------------------------------------

    def _source_signature(self) -> str:
        root = self.vocabulary_root()
        digest = hashlib.sha256(f"v{CARD_INDEX_VERSION}".encode())
        files = [root / RAYDU_MAIN_TEX, *_chapter_files(root), self.book_pdf_path()]
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
        return digest.hexdigest()

    def _cache_path(self) -> Path:
        return self.cache_dir / "raydu_vocabulary_cards.json"

    def card_index(self, *, force: bool = False) -> dict[str, Any]:
        """Both books' cards and units: 《英文字汇》 first, then 《GRE字汇通》."""

        self.refresh_data_caches()
        with self._index_lock:
            raydu = self._load_raydu_index(force)
            gre = self._load_gre_index(force)
            if force and not raydu["cards"] and not gre["cards"]:
                raise FileNotFoundError(f"没有找到《{RAYDU_BOOK_TITLE}》源码目录：{self.vocabulary_root()}")
            parts = (raydu.get("source_signature"), gre.get("source_signature"))
            if self._index is not None and not force and self._index.get("parts") == parts:
                return self._index
            self._lookup_cache.clear()
            cards, gre_units, merged = merge_book_cards(raydu, gre)
            units = [*raydu["units"], *gre_units]
            if raydu["cards"] and gre["cards"]:
                # Both books: the directory of 《英文字汇》 with the other book's words in it, each word once.
                units = build_merged_directory(raydu, cards)
                unit_path = {key: unit["path"] for unit in units for key in unit["card_keys"]}
                # Shallow copies: the books' cached indexes stay as they were.
                cards = {key: {**card, "unit_path": unit_path.get(key, "")} for key, card in cards.items()}
            index = {
                "version": CARD_INDEX_VERSION,
                "book_title": RAYDU_BOOK_TITLE,
                "entry_count": int(raydu.get("entry_count") or 0) + int(gre.get("entry_count") or 0),
                "cards": cards,
                "units": units,
                "unified": bool(raydu["cards"] and gre["cards"]),
                "books": {
                    "raydu": {"title": RAYDU_BOOK_TITLE, "entries": int(raydu.get("entry_count") or 0),
                              "cards": len(raydu["cards"]), "units": len(raydu["units"])},
                    "gre": {"title": GRE_BOOK_TITLE, "entries": int(gre.get("entry_count") or 0),
                            "cards": len(gre["cards"]), "units": len(gre["units"]),
                            "merged_into_raydu": merged,
                            "warnings": list(gre.get("warnings") or [])},
                },
                "source_signature": "|".join(str(part or "") for part in parts),
                "parts": parts,
            }
            self._index = index
            return index

    def _write_cache(self, path: Path, index: dict[str, Any]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(self.cache_dir), suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(index, stream, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _load_raydu_index(self, force: bool) -> dict[str, Any]:
        signature = self._source_signature()
        if self._raydu_index is not None and not force and self._raydu_index.get("source_signature") == signature:
            return self._raydu_index
        if not (self.vocabulary_root() / "chapters").is_dir():
            # Collected words remain usable even when this machine has
            # not installed the optional book project yet.
            return {"cards": {}, "units": [], "entry_count": 0}
        cache = self._cache_path()
        if not force and cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if cached.get("source_signature") == signature:
                    self._raydu_index = cached
                    return cached
            except (OSError, ValueError):
                pass
        index = build_card_index(self.vocabulary_root(), self.book_pdf_path())
        index["source_signature"] = signature
        index["built_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_cache(cache, index)
        self._raydu_index = index
        return index

    def _gre_signature(self, pdf: Path) -> str:
        from shared.scripts import chinese_simplify, raydu_gre_parser

        digest = hashlib.sha256(f"gre-v{GRE_INDEX_VERSION}".encode())
        for path in (pdf, Path(raydu_gre_parser.__file__), Path(chinese_simplify.__file__), Path(__file__)):
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
        return digest.hexdigest()

    def _load_gre_index(self, force: bool) -> dict[str, Any]:
        pdf = self.gre_pdf_path()
        if pdf is None:
            return {"cards": {}, "units": [], "entry_count": 0}
        signature = self._gre_signature(pdf)
        if self._gre_index is not None and not force and self._gre_index.get("source_signature") == signature:
            return self._gre_index
        cache = self.cache_dir / "gre_vocabulary_cards.json"
        if not force and cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if cached.get("source_signature") == signature:
                    self._gre_index = cached
                    return cached
            except (OSError, ValueError):
                pass
        index = build_gre_card_index(pdf)
        index["source_signature"] = signature
        index["built_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_cache(cache, index)
        self._gre_index = index
        return index

    def rebuild_card_index(self) -> dict[str, Any]:
        index = self.card_index(force=True)
        missing_pages = sum(
            1 for card in index["cards"].values()
            if not any(location.get("page") for location in card["locations"])
        )
        missing_audio = {
            deck: sum(
                1 for card in index["cards"].values()
                if card.get("deck", "raydu") == deck
                and not (self.deck_audio_dir(deck) / f"{card['audio_key']}.mp3").is_file()
            )
            for deck in ("raydu", "gre")
        }
        return {
            "entries": index["entry_count"],
            "cards": len(index["cards"]),
            "units": len(index["units"]),
            "cards_without_page": missing_pages,
            "cards_without_audio": missing_audio["raydu"],
            "gre_cards": index["books"]["gre"]["cards"],
            "gre_units": index["books"]["gre"]["units"],
            "gre_cards_without_audio": missing_audio["gre"],
            "gre_warnings": index["books"]["gre"]["warnings"],
            "cache_path": str(self._cache_path()),
        }

    # -- units --------------------------------------------------------------

    def list_units(self) -> list[dict[str, Any]]:
        """The 词汇库 unit, then the book's sections in order, with progress counts."""

        index = self.card_index()
        progress = self.manager.review_progress(self._study_day_end())

        def counts(keys: list[str]) -> dict[str, int]:
            states = [progress[key] for key in keys if key in progress]
            return {
                "total": len(keys),
                "learned": sum(1 for state in states if state["state"] != "new"),
                "due": sum(1 for state in states if state["due"]),
            }

        library = [row["card_key"] for row in self.library_card_rows()]
        units: list[dict[str, Any]] = [{
            "unit_key": VOCABULARY_UNIT_KEY,
            "title": VOCABULARY_UNIT_TITLE,
            "path": VOCABULARY_UNIT_TITLE,
            "page_start": None,
            "page_end": None,
            "bands": [],
            **counts(library),
        }]
        for unit in index["units"]:
            units.append({
                "unit_key": unit["unit_key"],
                "title": unit["title"],
                "path": unit["path"],
                "page_start": unit["page_start"],
                "page_end": unit["page_end"],
                "bands": list(unit["bands"]),
                **counts(list(unit["card_keys"])),
            })
        return units

    def unit(self, unit_key: str) -> dict[str, Any] | None:
        if unit_key == VOCABULARY_UNIT_KEY:
            return {"unit_key": VOCABULARY_UNIT_KEY, "title": VOCABULARY_UNIT_TITLE, "path": VOCABULARY_UNIT_TITLE}
        return next((unit for unit in self.card_index()["units"] if unit["unit_key"] == unit_key), None)

    def unit_for_page(self, page_number: int, deck: str = "raydu") -> dict[str, Any] | None:
        """The section of book ``deck`` (raydu / gre) being read at ``page_number`` (1-based)."""

        page = int(page_number)
        index = self.card_index()
        if index.get("unified"):
            # One directory for both books: the unit holding most words printed on that page
            # (or, between entries, on the nearest page before it).
            unit_of = self._unit_of()
            for candidate in range(page, 0, -1):
                found: dict[str, int] = {}
                for key, card in index["cards"].items():
                    if any(location.get("page") == candidate and location.get("deck", card.get("deck", "raydu")) == deck
                           for location in card["locations"]):
                        found[unit_of[key]] = found.get(unit_of[key], 0) + 1
                if found:
                    best = max(found, key=lambda unit_key: found[unit_key])
                    return next(unit for unit in index["units"] if unit["unit_key"] == best)
            return None
        units = [unit for unit in index["units"] if unit["unit_key"].startswith(f"{deck}:")]
        candidates = [
            unit for unit in units
            if unit["page_start"] is not None and unit["page_start"] <= page <= unit["page_end"]
        ]
        if candidates:
            return max(candidates, key=lambda unit: unit["page_start"])
        before = [
            unit for unit in units
            if unit["page_start"] is not None and unit["page_start"] <= page
        ]
        return before[-1] if before else None

    # -- cards ---------------------------------------------------------------

    def _book_rows(self, card_keys: Iterable[str]) -> list[dict[str, Any]]:
        index = self.card_index()
        unit_of: dict[str, str] = {}
        for unit in index["units"]:
            for key in unit["card_keys"]:
                unit_of.setdefault(key, unit["unit_key"])
        rows = []
        for key in card_keys:
            card = index["cards"].get(key)
            if card is not None:
                rows.append({
                    "card_key": key,
                    "term": card["term"],
                    "deck": card.get("deck", "raydu"),
                    "unit_key": unit_of.get(key, ""),
                    "sort_order": int(card["order"]),
                })
        return rows

    def _ensure_book_rows(self, card_keys: Iterable[str]) -> None:
        self.manager.ensure_review_cards(self._book_rows(card_keys))

    def _book_term_map(self, index: dict[str, Any] | None = None) -> dict[str, str]:
        """Lower-cased headwords, then derived words, to the book card that teaches them."""

        index = self.card_index() if index is None else index
        if self._term_map_index is index:
            return self._term_map
        mapping: dict[str, str] = {}
        for key, card in index["cards"].items():
            mapping.setdefault(card["term"].casefold(), key)
        for key, card in index["cards"].items():
            for item in card.get("derived", []):
                mapping.setdefault(str(item.get("term") or "").casefold(), key)
        self._term_map_index, self._term_map = index, mapping
        return mapping

    def book_card_for(self, *forms: str, _mapping: dict[str, str] | None = None) -> str | None:
        """The book card for a looked-up word, trying regular base forms of each form."""

        mapping = self._book_term_map() if _mapping is None else _mapping
        for form in forms:
            for base in inflection_bases(form):
                if base in mapping:
                    return mapping[base]
        return None

    def _unit_of(self) -> dict[str, str]:
        unit_of: dict[str, str] = {}
        for unit in self.card_index()["units"]:
            for key in unit["card_keys"]:
                unit_of.setdefault(key, unit["unit_key"])
        return unit_of

    def library_card_rows(self) -> list[dict[str, Any]]:
        """Review rows for the words saved on the 词汇库 page, oldest first.

        A saved word that is also a book headword reuses its book card; any
        other word gets a ``vocab:`` card built from the library and the local
        dictionaries.  Reading only; ``sync_library_cards`` stores the rows.
        """

        index = self.card_index()
        unit_of = self._unit_of()
        mapping = self._book_term_map(index)
        rows: dict[str, dict[str, Any]] = {}
        entries = self.manager.list_entries(reading_only=True, limit=None) if self.manager.database.is_file() else []
        for entry in sorted(entries, key=lambda row: (str(row.get("created_at") or ""), int(row["id"]))):
            term = re.sub(r"\s+", " ", str(entry.get("term") or "")).strip()
            if not term:
                continue
            book_key = self.book_card_for(term, _mapping=mapping)
            key = book_key or f"{LIBRARY_CARD_PREFIX}{term.casefold()}"
            if key in rows:
                continue
            if book_key is not None:
                card = index["cards"][book_key]
                rows[key] = {
                    "card_key": key, "term": card["term"], "deck": card.get("deck", "raydu"),
                    "unit_key": unit_of.get(key, ""), "sort_order": int(card["order"]),
                    "collected_at": entry.get("created_at"),
                }
            else:
                rows[key] = {
                    "card_key": key, "term": term, "deck": "vocabulary",
                    "unit_key": VOCABULARY_UNIT_KEY, "sort_order": 100000 + len(rows),
                    "vocabulary_entry_id": int(entry["id"]), "collected_at": entry.get("created_at"),
                }
        # Book cards collected from a card's lookup popup join the unit too, by collection time.
        collected = sorted(
            (state["collected_at"], key) for key, state in self.manager.review_progress().items()
            if key.startswith(BOOK_CARD_PREFIXES) and state.get("collected") and key in index["cards"]
        )
        for collected_at, key in collected:
            if key not in rows:
                card = index["cards"][key]
                rows[key] = {
                    "card_key": key, "term": card["term"], "deck": card.get("deck", "raydu"),
                    "unit_key": unit_of.get(key, ""), "sort_order": int(card["order"]),
                    "collected_at": collected_at,
                }
        return list(rows.values())

    def library_overview(self, query: str = "", familiarity: str = "all") -> list[dict[str, Any]]:
        """One line per 词汇库 word for the 词汇库 page: IPA, short Chinese and English, audio, state."""

        index = self.card_index()
        entries = self.manager.list_entries(reading_only=True, limit=None)
        by_term: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            by_term.setdefault(str(entry["term"]).casefold(), []).append(entry)
        needle = str(query or "").strip().casefold()
        rows = self.library_card_rows()
        english_matches = self._english_search_matches(needle, [row["term"] for row in rows]) if needle else set()
        matching = []
        for row in rows:
            key, term = row["card_key"], row["term"]
            own = by_term.get(term.casefold(), [])
            card = index["cards"].get(key)
            chinese = ("；".join(f"{sense['pos']} {sense['definition']}".strip() for sense in card["senses"][:2])
                       if card is not None else "；".join(str(entry.get("definition") or "") for entry in own))
            if needle and needle not in term.casefold() and needle not in chinese.casefold() and term.casefold() not in english_matches:
                continue
            matching.append((row, own, card, chinese))
        self._warm_short_english([row["term"] for row, _own, _card, _chinese in matching])
        prepared = self.manager
        prepared_terms = prepared.card_etymology_terms()
        if prepared is not self.manager:
            prepared_terms |= self.manager.card_etymology_terms()
        book_entries: dict[str, list[dict[str, Any]]] = {}
        if any(card is not None and not own for _row, own, card, _chinese in matching):
            for entry in self.manager.list_entries(limit=None):
                book_entries.setdefault(str(entry["term"]).casefold(), []).append(entry)
        result = []
        for row, own, card, chinese in matching:
            key, term = row["card_key"], row["term"]
            if key in index["cards"]:
                card = index["cards"][key]
                kind = "book"
                pronunciation = card["pronunciation"]
                chinese = "；".join(f"{sense['pos']} {sense['definition']}".strip() for sense in card["senses"][:2])
                audio = self.deck_audio_dir(card.get("deck", "raydu")) / f"{card['audio_key']}.mp3"
                audio_path = str(audio) if audio.is_file() else ""
                state = "原书卡片"
                familiarity_entries = own or book_entries.get(term.casefold(), [])
            else:
                kind = "vocabulary"
                pronunciation = next((str(entry.get("pronunciation") or "") for entry in own if entry.get("pronunciation")), "")
                chinese = "；".join(str(entry.get("definition") or "") for entry in own if entry.get("definition"))
                audio_path = self.examples.cached_audio(term)
                state = "" if term.casefold() in prepared_terms else "待准备"
                familiarity_entries = own
            english, ecdict_ipa = self._short_english(term)
            pronunciation = pronunciation or ecdict_ipa
            if pronunciation and not pronunciation.startswith(("[", "UK", "US", "/")):
                pronunciation = f"[{pronunciation}]"
            familiar = bool(familiarity_entries) and all(
                str(entry.get("familiarity")) == "familiar" for entry in familiarity_entries
            )
            item = {
                "card_key": key, "term": term, "kind": kind, "pronunciation": pronunciation,
                "chinese": re.sub(r"\s+", " ", chinese).strip(), "english": english,
                "part_of_speech": "；".join(dict.fromkeys(
                    str(entry.get("part_of_speech") or "") for entry in own
                )) or ("；".join(dict.fromkeys(sense["pos"] for sense in card["senses"])) if kind == "book" else ""),
                "note": "；".join(str(entry["note"]) for entry in own if entry.get("note")),
                "audio_path": audio_path, "state": state,
                "familiarity": "familiar" if familiar else "unfamiliar",
                "entry_ids": [int(entry["id"]) for entry in familiarity_entries],
                "collected_at": str(row.get("collected_at") or ""),
            }
            if familiarity in {"familiar", "unfamiliar"} and item["familiarity"] != familiarity:
                continue
            result.append(item)
        return result

    def _english_search_matches(self, needle: str, terms: list[str]) -> set[str]:
        """Find English-gloss matches in batches before building any row state."""
        database = getattr(self.dictionary_service, "database_path", None)
        if database is not None and Path(database).is_file():
            try:
                matches = set()
                with closing(sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro", uri=True)) as connection:
                    for start in range(0, len(terms), 500):
                        batch = terms[start:start + 500]
                        for row in connection.execute(
                            "SELECT word FROM ecdict_entries WHERE word IN (" + ",".join("?" for _ in batch)
                            + ") AND instr(lower(definition_en),?)>0", [*batch, needle],
                        ):
                            matches.add(str(row[0]).casefold())
                return matches
            except sqlite3.Error:
                pass
        if self.dictionary_service is None:
            return set()
        return {term.casefold() for term in terms if needle in self._short_english(term)[0].casefold()}

    @staticmethod
    def _short_english_values(row: dict[str, Any]) -> tuple[str, str]:
        first = re.split(r"\\n|\n", str(row.get("definition_en") or ""))[0]
        english = re.sub(r"^(?:[a-z]{1,5}\.\s*|[sr]\s+(?=\())", "", first.strip())
        return english, str(row.get("phonetic") or "").strip()

    def _warm_short_english(self, terms: list[str]) -> None:
        database = getattr(self.dictionary_service, "database_path", None)
        missing = list(dict.fromkeys(term.casefold() for term in terms if term.casefold() not in self._english_cache))
        if not missing or database is None or not Path(database).is_file():
            return
        try:
            found = {}
            with closing(sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro", uri=True)) as connection:
                for start in range(0, len(missing), 500):
                    batch = missing[start:start + 500]
                    for word, phonetic, english, chinese in connection.execute(
                        "SELECT word,phonetic,definition_en,translation_zh FROM ecdict_entries "
                        "WHERE word COLLATE NOCASE IN (" + ",".join("?" for _ in batch) + ")", batch,
                    ):
                        translation = str(chinese or "").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n").strip()
                        if translation:
                            found[str(word).casefold()] = self._short_english_values({"definition_en": english, "phonetic": phonetic})
            self._english_cache.update({term: found.get(term, ("", "")) for term in missing})
        except sqlite3.Error:
            # Optional/older dictionaries keep using the established single-word path.
            return

    def _short_english(self, term: str) -> tuple[str, str]:
        """First English gloss and IPA from ECDICT (one indexed query)."""

        cache = self._english_cache
        if term.casefold() in cache:
            return cache[term.casefold()]
        english, phonetic = "", ""
        if self.dictionary_service is not None and hasattr(self.dictionary_service, "lookup_chinese"):
            try:
                rows = self.dictionary_service.lookup_chinese(term, limit=1)
            except Exception:
                rows = []
            row = next((item for item in rows if str(item.get("term") or "").casefold() == term.casefold()), None)
            if row is not None:
                english, phonetic = self._short_english_values(row)
        cache[term.casefold()] = (english, phonetic)
        return english, phonetic

    def export_library(self, kind: str, familiarity: str = "all") -> dict[str, Any]:
        """Export the same collected words the library displays, including book marks."""

        if kind not in {"txt", "pdf"}:
            raise ValueError("词汇库只支持 TXT 或 PDF 导出。")
        rows = [{
            "term": item["term"], "definition": item["chinese"],
            "pronunciation": item["pronunciation"], "familiarity": item["familiarity"],
            "part_of_speech": item["part_of_speech"], "note": item["note"] or item["english"],
        } for item in self.library_overview(familiarity=familiarity)]
        export = self.manager.export_txt if kind == "txt" else self.manager.export_pdf
        return export(familiarity, entries=rows)

    def remove_from_library(self, card_keys: Iterable[str]) -> dict[str, Any]:
        """Take words out of the 词汇库: delete the saved entry of other words, un-collect book cards."""

        keys = list(dict.fromkeys(str(key) for key in card_keys))
        entries = self.manager.list_entries(reading_only=True, limit=None)
        by_card: dict[str, list[int]] = {}
        for entry in entries:
            term = str(entry["term"]).casefold()
            key = self.book_card_for(term) or f"{LIBRARY_CARD_PREFIX}{term}"
            by_card.setdefault(key, []).append(int(entry["id"]))
        book = [key for key in keys if key.startswith(BOOK_CARD_PREFIXES)]
        others = [key.split(":", 1)[1] for key in keys if key.startswith(LIBRARY_CARD_PREFIX)]
        # Reader saves can include a derived word mapping to the same book
        # card. Remove those entries and collection marks in one transaction.
        ids = [entry_id for key in keys for entry_id in by_card.get(key, [])]
        removed = self.manager.clear_collected(book, entry_ids=ids)
        result: dict[str, Any] = {**removed, "uncollected": book, "deleted": others}
        remaining = {row["card_key"] for row in self.library_card_rows()}
        if set(keys) & remaining:
            raise RuntimeError("移出词汇库后回读不一致，请使用返回操作的备份恢复。")
        result["readback_verified"] = True
        return result

    def sync_library_cards(self) -> dict[str, Any]:
        """Store review cards for newly saved 词汇库 words (writes nothing when none are new)."""

        rows = self.library_card_rows()
        if not rows:
            return {"cards": 0, "inserted": 0, "marked": 0, "backup_path": "", "readback_verified": True}
        result = self.manager.ensure_review_cards(rows)
        result["cards"] = len(rows)
        return result

    def card(self, card_key: str) -> dict[str, Any]:
        """The full card content (book or 词汇库 word) merged with its review state."""

        key = str(card_key or "").strip()
        index = self.card_index()
        state = self.manager.review_card(key)
        if key in index["cards"]:
            content = json.loads(json.dumps(index["cards"][key], ensure_ascii=False))
            content.setdefault("book_title", RAYDU_BOOK_TITLE)
            content.setdefault("deck", "raydu")
            content["source_kind"] = "book"
            content.update(self._book_card_etymology(content))
        elif key.startswith(LIBRARY_CARD_PREFIX):
            content = self._library_card(key, state)
        else:
            raise KeyError(f"没有找到卡片：{key}")
        if content["source_kind"] == "book":
            # 《GRE字汇通》 words without a recording of their own are read by TTS (empty path).
            audio = self.deck_audio_dir(content["deck"])
            word_audio = audio / f"{content.get('audio_key') or ''}.mp3"
            content["audio_path"] = str(word_audio) if content.get("audio_key") and word_audio.is_file() else ""
            for example in content.get("examples", []):
                # A merged card's GRE examples are recorded in the GRE book's folder.
                path = self.deck_audio_dir(example.get("deck") or content["deck"]) / f"{example.get('audio_key') or ''}.mp3"
                example["audio_path"] = str(path) if example.get("audio_key") and path.is_file() else ""
        else:
            # 词汇库 words: cached volunteer recordings or Edge TTS, see prepare_card_audio.
            content["audio_path"] = self.examples.cached_audio(content["term"])
            for example in content.get("examples", []):
                example["audio_path"] = self.examples.cached_audio(example.get("en", ""), example.get("audio_id"))
            # Recordings arrive slowly; fetch missing ones in the background for next time.
            self.examples.fetch_recordings_async(
                example["audio_id"] for example in content.get("examples", []) if example.get("audio_id")
            )
        content["derived_more"] = self.dictionary_derived(content)
        content["review"] = state
        return content

    def dictionary_derived(self, card: dict[str, Any], *, limit: int = 6) -> list[dict[str, str]]:
        """派生 words the card's own 衍生 list lacks, from the local dictionaries (read-only, cached)."""

        term = str(card.get("term") or "").strip()
        lookup = getattr(self.dictionary_service, "derived_forms", None)
        if not term or " " in term or not callable(lookup):
            return []
        key = term.casefold()
        if key not in self._derived_cache:
            try:
                found = lookup(term, limit=limit + 4)
            except Exception:
                found = []
            self._derived_cache[key] = [
                {**_dictionary_line(item["definition_zh"]), "term": item["term"], "source": "dictionary"}
                for item in found
            ]
        shown = {key, *(str(item.get("term") or "").casefold() for item in card.get("derived", []))}
        more = [dict(item) for item in self._derived_cache[key] if item["term"].casefold() not in shown]
        return more[: max(0, limit - len(card.get("derived", [])))]

    def _library_card(self, key: str, state: dict[str, Any] | None) -> dict[str, Any]:
        """A 词汇库 word in the book's card layout: library senses, local word parts and origin."""

        if key in self._lookup_cache:
            return json.loads(json.dumps(self._lookup_cache[key], ensure_ascii=False))
        term = str((state or {}).get("term") or key.split(":", 1)[1])
        entries = self.manager.list_entries(term, limit=20) if hasattr(self.manager, "list_entries") else []
        entries = [row for row in entries if str(row.get("term") or "").casefold() == term.casefold()]
        senses = [
            {"pos": POS_LABELS.get(str(row.get("part_of_speech") or ""), str(row.get("part_of_speech") or "")),
             "definition": str(row.get("definition") or "").strip()}
            for row in entries if str(row.get("definition") or "").strip()
        ]
        pronunciation = next((str(row.get("pronunciation") or "") for row in entries if row.get("pronunciation")), "")
        if (not senses or not pronunciation) and self.dictionary_service is not None:
            found_senses, found_pronunciation = self._dictionary_senses(term)
            senses = senses or found_senses
            pronunciation = pronunciation or found_pronunciation
        morph: list[dict[str, str]] = []
        etymology_zh = ""
        if self.dictionary_service is not None:
            try:
                found = self.dictionary_service.lookup(term.casefold(), limit=1)
            except Exception:
                found = []
            local = (found[0].get("local_dictionary") if found else None) or {}
            morphemes = [item for item in local.get("morphemes") or [] if str(item.get("segment") or "").strip()]
            if morphemes:
                morph = [{
                    "segments": " / ".join(str(item["segment"]) for item in morphemes),
                    "meanings": " / ".join(str(item.get("meaning") or "—") for item in morphemes),
                }]
            etymology_zh = str(local.get("etymology_zh") or "").strip()
            if not pronunciation:
                pronunciation = str(local.get("pronunciation") or "").strip()
        etymology_note = "本地字源 · 待 AI 核对" if etymology_zh else ""
        checked = self.manager.card_etymology(term)
        if checked is not None:
            # The AI-checked 字源 replaces the unchecked local one.
            from shared.scripts.vocabulary_etymology import VERDICT_LABELS

            etymology_zh = checked["etymology_zh"]
            # An empty checked split means the word should not be split (no folk etymology).
            parts = [item for item in checked["morphemes"] if item["segment"].strip("-").casefold() != term.casefold()]
            checked["morphemes"] = parts if len(parts) == len(checked["morphemes"]) else []
            morph = [{
                "segments": " / ".join(item["segment"] for item in checked["morphemes"]),
                "meanings": " / ".join(item.get("meaning") or "—" for item in checked["morphemes"]),
            }] if checked["morphemes"] else []
            sources = "、".join(checked.get("sources") or [])
            etymology_note = VERDICT_LABELS.get(checked["verdict"], "AI 已核对") + (f" · 依据：{sources}" if sources else "")
        if pronunciation and not pronunciation.startswith(("[", "UK", "US")):
            pronunciation = f"[{pronunciation.strip('/')}]"
        encounters = [
            row for row in self.manager.list_encounters(query=term, limit=50)
            if str(row.get("term") or row.get("surface_form") or "").casefold() == term.casefold()
            or str(row.get("surface_form") or "").casefold() == term.casefold()
        ]
        examples = []
        locations = []
        for row in encounters:
            context = _sentence_around(str(row.get("context") or ""), str(row.get("surface_form") or term))
            if context and all(item["en"] != context for item in examples):
                examples.append({"number": len(examples) + 1, "audio_key": "", "en": context, "zh": ""})
            if any(
                item["material"] == str(row.get("material_title") or "") and item["page"] == row.get("page_number")
                for item in locations
            ):
                continue
            locations.append({
                "material": str(row.get("material_title") or ""),
                "page": row.get("page_number"),
                "source_path": str(row.get("source_path") or ""),
                "anchor_y": float(row.get("anchor_y") or 0.0),
                "looked_up_at": str(row.get("created_at") or ""),
            })
        examples = examples[:1] + self.tatoeba_examples(term, limit=3 - min(1, len(examples)))
        if len(examples) < 3:
            # Words Tatoeba does not cover get the AI-written examples stored for them.
            stored_examples = self.manager.card_examples(term) or self.manager.card_examples(term)
            for row in stored_examples[: 3 - len(examples)]:
                examples.append({
                    "number": 0, "audio_key": "", "audio_id": None, "en": row["en"], "zh": row["zh"],
                    "attribution": f"AI 例句（{row['model'] or 'AI'}）· 朗读 Edge TTS", "source": "ai",
                })
        for number, example in enumerate(examples, start=1):
            example["number"] = number
        content = {
            "card_key": key,
            "deck": "vocabulary",
            "term": term,
            "pronunciation": pronunciation,
            "audio_key": "",
            "morph": morph,
            "etymology_zh": etymology_zh,
            "etymology_note": etymology_note,
            "etymology_checked": checked is not None,
            "senses": senses,
            "derived": [],
            "examples": examples,
            "locations": locations[:3],
            "source_kind": "vocabulary",
        }
        self._lookup_cache[key] = content
        return json.loads(json.dumps(content, ensure_ascii=False))

    def word_forms(self, term: str) -> list[str]:
        """Inflected forms from ECDICT's exchange field (protect → protects, protected, protecting)."""

        if self.dictionary_service is None or not hasattr(self.dictionary_service, "lookup_chinese"):
            return []
        try:
            rows = self.dictionary_service.lookup_chinese(term, limit=1)
        except Exception:
            return []
        row = next((item for item in rows if str(item.get("term") or "").casefold() == term.casefold()), None)
        forms = []
        for part in str((row or {}).get("exchange") or "").split("/"):
            kind, _, value = part.partition(":")
            if kind in {"p", "d", "i", "3", "s", "r", "t"} and value.strip():
                forms.append(value.strip())
        return list(dict.fromkeys(forms))

    def tatoeba_examples(self, term: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """Bilingual Tatoeba sentences for a 词汇库 word, shaped like book examples."""

        if limit <= 0:
            return []
        library = self.examples
        if not library.index_ready():
            if not library.sources_present():
                return []
            try:
                library.build_index()
            except Exception:
                return []
        found = library.examples_for(
            term, self.word_forms(term), limit=limit,
            allow_noncommercial=self.manager.example_audio_noncommercial(),
        )
        return [{
            "number": 0,
            "audio_key": "",
            "audio_id": item["audio_id"],
            "en": item["en"],
            "zh": item["zh"],
            "attribution": item["attribution"],
        } for item in found]

    def prepare_card_audio(self, card: dict[str, Any]) -> dict[str, Any]:
        """Fetch or synthesize the word and example audio of a 词汇库 card (network, run off the UI thread)."""

        if card.get("source_kind") == "book":
            return {"audio_path": card.get("audio_path", ""), "examples": [item.get("audio_path", "") for item in card.get("examples", [])]}
        from concurrent.futures import ThreadPoolExecutor

        jobs = [(card["term"], None), *((item.get("en", ""), item.get("audio_id")) for item in card.get("examples", []))]
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda job: self.examples.ensure_audio(*job), jobs))
        return {"audio_path": paths[0], "examples": paths[1:]}

    def etymology_evidence(self, term: str, senses: list[dict[str, Any]]) -> dict[str, Any]:
        """The evidence for one word's 字源: local dictionaries, the book's split, its section and same-root words."""

        from shared.scripts.vocabulary_etymology import gather_evidence

        joined = "；".join(f"{item.get('pos') or ''} {item.get('definition') or ''}".strip() for item in senses)
        book_key = self.book_card_for(term)
        book = self.card_index()["cards"][book_key] if book_key else None
        return gather_evidence(
            term, self.dictionary_service, senses=joined,
            book_morph=(book or {}).get("morph") or None,
            book_section=str((book or {}).get("unit_path") or "").split("›")[-1].strip(),
            same_root=self._same_root_words(book_key) if book_key else None,
        )

    def _same_root_words(self, card_key: str, *, limit: int = 6) -> list[dict[str, str]]:
        """Other words of the card's book section that share one of its word parts (suffixes aside)."""

        cards = self.card_index()["cards"]
        card = cards[card_key]

        def roots(item: dict[str, Any]) -> set[str]:
            found = set()
            for morph in item.get("morph") or []:
                segments = [part.strip() for part in str(morph.get("segments") or "").split("/")]
                meanings = [part.strip() for part in str(morph.get("meanings") or "").split("/")]
                for index, segment in enumerate(segments):
                    meaning = meanings[index] if index < len(meanings) else ""
                    key = re.sub(r"[^a-z]", "", segment.casefold())
                    if len(key) >= 3 and "字尾" not in meaning and not meaning.startswith("("):
                        found.add(key)
            return found

        own = roots(card)
        if not own or not card.get("unit_path"):
            return []
        result = []
        for key, other in cards.items():
            if key == card_key or other.get("unit_path") != card["unit_path"] or not (roots(other) & own):
                continue
            first = (other.get("morph") or [{}])[0]
            result.append({
                "term": other["term"],
                "split": f"{first.get('segments', '')} = {first.get('meanings', '')}",
                "meaning": "；".join(sense["definition"] for sense in other.get("senses", [])[:1]),
            })
            if len(result) >= limit:
                break
        return result

    def _book_card_etymology(self, card: dict[str, Any]) -> dict[str, Any]:
        """The checked 字源 of a 书中词条; one written before it followed the book's split is not shown."""

        from shared.scripts.vocabulary_etymology import VERDICT_LABELS

        checked = self.manager.card_etymology(card["term"])
        if checked is None or (card.get("morph") and not checked.get("book_aligned")):
            return {"etymology_checked": False}
        sources = "、".join(checked.get("sources") or [])
        return {
            "etymology_checked": True,
            "etymology_zh": checked["etymology_zh"],
            "etymology_note": VERDICT_LABELS.get(checked["verdict"], "AI 已核对") + (f" · 依据：{sources}" if sources else ""),
        }

    def book_etymology_pending(self) -> list[str]:
        """书中词条 whose 字源 is missing or was not written along the book's split, one card per word."""

        cards = self.card_index()["cards"]
        aligned = self.manager.book_aligned_etymology_terms() if hasattr(self.manager, "book_aligned_etymology_terms") else set()
        checked = self.manager.card_etymology_terms()
        pending, seen = [], set()
        for key, card in cards.items():
            term = card["term"].casefold()
            if term in seen:
                continue
            seen.add(term)
            done = term in aligned if card.get("morph") else term in checked
            if not done:
                pending.append(key)
        return pending

    def save_card_etymology(self, term: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a 字源 written by Work or Claude against the local evidence, then store it."""

        from shared.scripts.vocabulary_etymology import validate_etymology

        match = next((card for card in self.card_index()["cards"].values() if card["term"].casefold() == term.casefold()), None)
        evidence = self.etymology_evidence(term, (match or {}).get("senses", []))
        saved = self.manager.save_card_etymology(term, validate_etymology(term, payload, evidence))
        self._lookup_cache.clear()
        return saved

    def card_is_ready(self, card: dict[str, Any]) -> bool:
        """A 词汇库 card is ready once its word and example audio are cached."""

        if card.get("source_kind") != "vocabulary":
            return True
        return bool(card.get("audio_path")) and all(item.get("audio_path") for item in card.get("examples", []))

    def library_cards_needing_preparation(self) -> list[str]:
        keys = [row["card_key"] for row in self.library_card_rows() if row["card_key"].startswith(LIBRARY_CARD_PREFIX)]
        pending = []
        for key in keys:
            try:
                if not self.card_is_ready(self.card(key)):
                    pending.append(key)
            except KeyError:
                continue
        return pending

    def prepare_library_cards(
        self, card_keys: Iterable[str] | None = None, *, progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """Cache the audio of 词汇库 cards one after another (TTS, recordings); returns per-card results."""

        self.sync_library_cards()
        keys = list(card_keys) if card_keys is not None else self.library_cards_needing_preparation()
        results = []
        for index, key in enumerate(keys, start=1):
            if progress is not None:
                progress(index, len(keys), key)
            try:
                outcome = self.prepare_library_card(key)
                results.append({"card_key": key, "ready": self.card_is_ready(outcome["card"]), "error": ""})
            except Exception as failure:
                results.append({"card_key": key, "ready": False, "error": str(failure)})
        return {"prepared": results, "ready": sum(1 for item in results if item["ready"]), "total": len(results)}

    def prepare_library_card(self, card_key: str) -> dict[str, Any]:
        """Cache a 词汇库 card's word and example audio. Network work; run it off the UI thread."""

        card = self.card(card_key)
        audio = timed_call(lambda: self.prepare_card_audio(card), 240)
        card["audio_path"] = audio["audio_path"] or card.get("audio_path", "")
        for example, path in zip(card.get("examples", []), audio["examples"]):
            example["audio_path"] = path or example.get("audio_path", "")
        return {"card": card}

    def example_library_status(self) -> dict[str, Any]:
        status = self.examples.status()
        status["example_audio_noncommercial"] = self.manager.example_audio_noncommercial()
        return status

    def set_example_audio_noncommercial(self, allowed: bool) -> dict[str, Any]:
        result = self.manager.set_example_audio_noncommercial(bool(allowed))
        self._lookup_cache.clear()  # cards pick their recordings again
        return result

    def prepare_example_library(self) -> dict[str, Any]:
        """Download the Tatoeba files when missing (about 8 MB) and build the local index."""

        result = self.examples.download_sources() if not self.examples.sources_present() else self.examples.build_index()
        self._lookup_cache.clear()
        return result

    def _dictionary_senses(self, term: str) -> tuple[list[dict[str, str]], str]:
        """Chinese senses from local ECDICT, else English WordNet senses, one line each."""

        service = self.dictionary_service
        for candidate in dict.fromkeys([term, term.casefold(), *inflection_bases(term)[1:]]):
            try:
                rows = service.lookup_chinese(candidate, limit=1) if hasattr(service, "lookup_chinese") else []
            except Exception:
                rows = []
            row = next((item for item in rows if str(item.get("term") or "").casefold() == candidate.casefold()), None)
            if row and str(row.get("definition_zh") or "").strip():
                lines = re.split(r"\\n|\n", str(row["definition_zh"]))
                return [_dictionary_line(line) for line in lines if line.strip()][:5], str(row.get("phonetic") or "")
        try:
            found = service.lookup(term.casefold(), limit=1)
        except Exception:
            found = []
        if not found:
            return [], ""
        lines = str(found[0].get("definition_en") or "").splitlines()
        return [_dictionary_line(re.sub(r"^\d+\.\s*", "", line)) for line in lines if line.strip()][:5], str(
            found[0].get("pronunciation") or ""
        )

    def lookup_word(self, text: str) -> dict[str, Any]:
        """Meaning of a word selected on a card: the book first, then the local dictionaries."""

        selected = re.sub(r"\s+", " ", str(text or "")).strip(" \t\n.,;:!?\"“”‘’'()[]")
        if not selected or len(selected) > 80:
            raise ValueError("请选中一个英文单词或短语。")
        lemma = selected.casefold()
        dictionary = self.dictionary_service
        row = None
        if dictionary is not None and hasattr(dictionary, "lookup_chinese"):
            for candidate in dict.fromkeys([selected, selected.casefold()]):
                try:
                    found = dictionary.lookup_chinese(candidate, limit=1)
                except Exception:
                    found = []
                row = next((item for item in found if str(item.get("term") or "").casefold() == candidate.casefold()), None)
                if row is not None:
                    break
            # ECDICT's exchange field names the base form of an inflection ("0:visitor").
            # Keep the selected form when it has meanings of its own ("living"); move to
            # the base form only when the entry is just "the plural/past of ...".
            base = re.search(r"(?:^|/)0:([^/]+)", str((row or {}).get("exchange") or ""))
            inflection_only = row is None or bool(
                re.search(r"复数|过去式|过去分词|现在分词|第三人称|比较级|最高级", str(row.get("definition_zh") or ""))
            ) or not str(row.get("definition_zh") or "").strip()
            if base:
                base_form = base.group(1).casefold()
            if base and base_form != lemma and inflection_only:
                lemma = base_form
                try:
                    found = dictionary.lookup_chinese(lemma, limit=1)
                except Exception:
                    found = []
                row = next((item for item in found if str(item.get("term") or "").casefold() == lemma), row)
        book_key = self.book_card_for(lemma, selected)
        book = None
        if book_key is not None:
            card = self.card_index()["cards"][book_key]
            book = {
                "card_key": book_key,
                "term": card["term"],
                "pronunciation": card["pronunciation"],
                "senses": card["senses"],
                "derived": [item for item in card.get("derived", []) if item["term"].casefold() in {lemma, selected.casefold()}],
                # The merged books show as one: the unit in the one directory, no book name.
                "path": card.get("unit_path") or (card["locations"][0]["path"] if card.get("locations") else ""),
                "book_title": "" if card.get("unit_path") else card.get("book_title", RAYDU_BOOK_TITLE),
            }
        chinese: list[dict[str, str]] = []
        chinese_source = ""
        pronunciation = ""
        if row is not None:
            chinese = [_dictionary_line(line) for line in re.split(r"\\n|\n", str(row.get("definition_zh") or "")) if line.strip()][:5]
            chinese_source = "ECDICT"
            pronunciation = str(row.get("phonetic") or "").strip()
        # The same combined local lookup as the reader: WordNet senses, the
        # OpenEtymology learner wordbooks (Chinese senses, morphemes, word origin),
        # MorphoLex/ECDICT word roots and Etymological WordNet word families.
        english: list[dict[str, str]] = []
        morphology: dict[str, Any] | None = None
        etymology_zh = ""
        word_family = ""
        if dictionary is not None:
            try:
                found = dictionary.lookup(lemma, limit=1)
            except Exception:
                found = []
            entry = found[0] if found else {}
            lines = [re.sub(r"^\d+\.\s*", "", line) for line in str(entry.get("definition_en") or "").splitlines()]
            english = [_dictionary_line(line) for line in lines if line.strip()][:3]
            local = entry.get("local_dictionary") or {}
            if str(local.get("definitions_zh") or "").strip():
                chinese = [_dictionary_line(line) for line in str(local["definitions_zh"]).splitlines() if line.strip()][:5]
                chinese_source = str(local.get("source_book") or "OpenEtymology")
            etymology_zh = str(local.get("etymology_zh") or "").strip()
            pronunciation = str(local.get("pronunciation") or "").strip() or pronunciation or str(entry.get("pronunciation") or "").strip()
            morphology = _readable_morphology(entry.get("morphology"))
            word_family = str(entry.get("etymology") or "").strip()
        if pronunciation and not pronunciation.startswith(("[", "UK", "US")):
            pronunciation = f"[{pronunciation.strip('/')}]"
        # Book words already have their own card; other words can be saved to the 词汇库.
        if book_key is None:
            in_library = bool(self.manager.resolve_entries(terms=[lemma]))
        else:
            in_library = bool((self.manager.review_card(book_key) or {}).get("collected_at"))
        base_form_note = ""
        if dictionary is not None and row is not None:
            base = re.search(r"(?:^|/)0:([^/]+)", str(row.get("exchange") or ""))
            if base and base.group(1).casefold() != lemma:
                base_form_note = base.group(1)
        return {
            "selected": selected,
            "lemma": lemma,
            "pronunciation": (book or {}).get("pronunciation") or pronunciation,
            "book": book,
            "chinese": chinese,
            "chinese_source": chinese_source,
            "morphology": morphology,
            "etymology_zh": etymology_zh,
            "word_family": word_family,
            "english": english,
            "base_form": base_form_note,
            "in_library": in_library,
        }

    # -- queues and grading --------------------------------------------------

    @staticmethod
    def _study_day_end(now: datetime | None = None) -> datetime:
        return VocabularyManager.study_day_end(now)

    def unit_card_keys(self, unit_key: str) -> list[str] | None:
        if not unit_key:
            return None
        if unit_key == VOCABULARY_UNIT_KEY:
            return [row["card_key"] for row in self.library_card_rows()]
        unit = self.unit(unit_key)
        if unit is None:
            raise KeyError(f"没有找到学习单元：{unit_key}")
        return list(unit["card_keys"])

    def review_queue(
        self,
        *,
        unit_key: str = "",
        new_limit: int | None = None,
        now: datetime | None = None,
        include_content: bool = False,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Cards due now plus today's new cards; a unit restricts both to that unit."""

        if unit_key == VOCABULARY_UNIT_KEY:
            if persist:
                self.sync_library_cards()
            keys = self.unit_card_keys(unit_key)
        elif unit_key:
            keys = self.unit_card_keys(unit_key)
            if persist:
                self._ensure_book_rows(keys or [])
        else:
            # Today's queue: the whole book plus the words currently in the 词汇库;
            # a word deleted from the 词汇库 leaves the queue with it.
            if persist:
                self._ensure_book_rows(self.card_index()["cards"].keys())
                self.sync_library_cards()
            keys = list(self.card_index()["cards"]) + [row["card_key"] for row in self.library_card_rows()]
        if unit_key and new_limit is None:
            new_limit = 10_000
        pending = [] if persist else [*self._book_rows(keys or []), *self.library_card_rows()]
        queue = self.manager.review_queue(card_keys=keys, new_limit=new_limit, now=now, pending_cards=pending)
        if unit_key:
            # Inside one unit new words follow the unit's own order.
            position = {key: index for index, key in enumerate(keys or [])}
            due = [row for row in queue["cards"] if row["queue"] != "new"]
            fresh = sorted(
                (row for row in queue["cards"] if row["queue"] == "new"),
                key=lambda row: position.get(row["card_key"], len(position)),
            )
            queue["cards"] = due + fresh
        if include_content:
            queue["cards"] = [self.card(row["card_key"]) for row in queue["cards"]]
        return queue

    def today_summary(self, now: datetime | None = None) -> dict[str, Any]:
        queue = self.review_queue(now=now, persist=False)
        summary = self.manager.review_summary(now=now)
        summary["due"] = queue["counts"]["learning"] + queue["counts"]["review"]
        summary["new"] = queue["counts"]["new"]
        summary["streak"] = self.manager.review_streak(now) if hasattr(self.manager, "review_streak") else 0
        return summary

    def record_review(
        self,
        card_key: str,
        grade: str,
        *,
        mode: str = "recognize",
        answer: str = "",
        correct: bool | None = None,
        elapsed_ms: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if mode not in REVIEW_MODES:
            raise ValueError(f"复习方式只能是：{'、'.join(REVIEW_MODES)}")
        if grade not in REVIEW_GRADES:
            raise ValueError(f"评分只能是：{'、'.join(REVIEW_GRADES)}")
        key = str(card_key or "").strip()
        if key.startswith(BOOK_CARD_PREFIXES):
            self._ensure_book_rows([key])
        elif key.startswith(LIBRARY_CARD_PREFIX):
            rows = [row for row in self.library_card_rows() if row["card_key"] == key]
            if not rows:
                raise KeyError(f"复习卡片不存在：{key}")
            self.manager.ensure_review_cards(rows)
        return self.manager.record_review(
            key, grade, mode=mode, answer=answer, correct=correct,
            elapsed_ms=elapsed_ms, now=now,
        )

    def collect_terms(self, terms: Iterable[str]) -> dict[str, Any]:
        """Save words outside the book to the 词汇库 (the reader's 收藏) so they are studied.

        Senses come from the local dictionaries.  Book words (headwords and their
        derived words) keep their book card: they are only marked as collected,
        which puts that card in the 词汇库 unit and first among the new words.
        """

        entries: list[dict[str, Any]] = []
        book_terms: list[str] = []
        book_keys: list[str] = []
        missing: list[str] = []
        for raw in terms:
            term = re.sub(r"\s+", " ", str(raw or "")).strip()
            if not term:
                continue
            result = self.lookup_word(term)
            if result.get("book"):
                book_keys.append(result["book"]["card_key"])
                book_terms.append(result["book"]["term"])
                continue
            senses = result.get("chinese") or result.get("english") or []
            if not senses:
                missing.append(term)
                continue
            first_pos = next((sense.get("pos") for sense in senses if sense.get("pos")), "")
            entries.append({
                "term": result["lemma"],
                "part_of_speech": VOCABULARY_POS.get(str(first_pos), ""),
                "definition": "；".join(dict.fromkeys(
                    str(sense.get("definition") or "").strip() for sense in senses if str(sense.get("definition") or "").strip()
                )),
                "pronunciation": str(result.get("pronunciation") or ""),
                "source": "背单词卡片查词",
            })
        if not entries and not book_terms:
            raise ValueError("本地词典没有这些词的释义，未收藏：" + "、".join(missing) if missing else "没有要收藏的单词。")
        imported: dict[str, Any] = {}
        if entries:
            imported = self.manager.import_entries(entries, merge_definitions=True)
            self.sync_library_cards()
        if book_keys:
            index = self.card_index()
            unit_of = self._unit_of()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.manager.ensure_review_cards([
                {"card_key": key, "term": index["cards"][key]["term"], "deck": index["cards"][key].get("deck", "raydu"),
                 "unit_key": unit_of.get(key, ""), "sort_order": int(index["cards"][key]["order"]),
                 "collected_at": now}
                for key in dict.fromkeys(book_keys)
            ])
            if any(not (self.manager.review_card(key) or {}).get("collected_at") for key in book_keys):
                raise RuntimeError("原书卡片收藏标记写后回读不一致。")
        return {
            "collected": [entry["term"] for entry in entries],
            "book_terms": book_terms,
            "not_found": missing,
            "import": imported,
            "readback_verified": bool(imported.get("readback_verified", True)) if entries else True,
        }

    def choose_mode(self, card: dict[str, Any], preferred: str = "mixed") -> str:
        """Pick the review mode: young cards are recognised, mature ones are produced."""

        review = card.get("review") or {}
        if review.get("state") in (None, "new"):
            # A word is first studied with the whole card; testing starts afterwards.
            return "learn"
        has_cloze = any(
            cloze_example(example.get("en", ""), card["term"], [item["term"] for item in card.get("derived", [])])
            for example in card.get("examples", [])
        )
        if preferred != "mixed":
            if preferred == "dictation_cloze" and not has_cloze:
                return "dictation_word"
            return preferred
        if review.get("state") in ("learning", "relearning") or float(review.get("interval_days") or 0) < 3:
            return "recognize"
        options = ["spell", "dictation_word"]
        if has_cloze:
            options.append("dictation_cloze")
        return options[int(review.get("reps") or 0) % len(options)]

    def question_for(self, card: dict[str, Any], mode: str) -> dict[str, Any]:
        """What the front of the card shows and what a typed answer must match."""

        derived = [item["term"] for item in card.get("derived", [])]
        if mode == "dictation_cloze":
            for example in card.get("examples", []):
                blank = cloze_example(example.get("en", ""), card["term"], derived)
                if blank is not None:
                    return {"mode": mode, "prompt": blank["prompt"], "answer": blank["answer"],
                            "audio_path": example.get("audio_path", ""), "speak": example.get("en", ""),
                            "hint": example.get("zh", "")}
            mode = "dictation_word"
        if mode in {"dictation_word", "spell"}:
            return {"mode": mode, "prompt": "", "answer": card["term"],
                    "audio_path": card.get("audio_path", ""), "speak": card["term"], "hint": ""}
        return {"mode": "learn" if mode == "learn" else "recognize", "prompt": card["term"], "answer": "",
                "audio_path": card.get("audio_path", ""), "speak": card["term"], "hint": ""}


def inflection_bases(word: str) -> list[str]:
    """The word itself, then plausible base forms of a regular English inflection."""

    value = str(word or "").strip().casefold()
    bases = [value]
    if " " in value or len(value) < 4:
        return bases

    def add(candidate: str) -> None:
        if len(candidate) >= 2 and candidate not in bases:
            bases.append(candidate)

    for suffix in ("ies", "ied"):
        if value.endswith(suffix):
            add(value[: -len(suffix)] + "y")
    for suffix in ("ing", "ed", "es", "s", "er", "est"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 3:
            stem = value[: -len(suffix)]
            add(stem)
            if suffix in {"ing", "ed", "er", "est"}:
                add(stem + "e")
                if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiouls":
                    add(stem[:-1])
    return bases


_DICTIONARY_POS = {
    "n": "名", "v": "动", "vt": "动", "vi": "动", "a": "形", "adj": "形", "s": "形",
    "ad": "副", "adv": "副", "r": "副", "prep": "介", "conj": "连", "pron": "代", "int": "叹",
}


def _dictionary_line(line: str) -> dict[str, str]:
    """``a. 高山的`` or ``[adj.] living ...`` → ``{pos: 形, definition: ...}``."""

    text = line.strip()
    match = re.match(r"^\[?([a-z]{1,5})\.\]?\s+(.*)$", text)
    if match and match.group(1) in _DICTIONARY_POS:
        return {"pos": _DICTIONARY_POS[match.group(1)], "definition": match.group(2).strip()}
    return {"pos": "", "definition": text}


def _readable_morphology(morphology: Any) -> dict[str, Any] | None:
    """Word-part analysis worth showing: OpenEtymology splits, or fallback parts that carry a meaning."""

    if not isinstance(morphology, dict) or not morphology.get("parts"):
        return None
    parts = []
    for part in morphology["parts"]:
        meaning = str(part.get("meaning") or "").strip()
        if "暂未提供" in meaning:
            meaning = ""
        parts.append({
            "segment": str(part.get("segment") or ""),
            "meaning": meaning,
            "note": str(part.get("origin") or part.get("note") or "").strip(),
        })
    if not any(part["meaning"] for part in parts):
        return None
    return {
        "segments": str(morphology.get("segments") or " / ".join(part["segment"] for part in parts)),
        "parts": parts,
        "source": str(morphology.get("source") or ""),
        "confidence": str(morphology.get("confidence") or ""),
    }


def _sentence_around(context: str, surface: str) -> str:
    text = re.sub(r"\s+", " ", str(context or "")).strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    needle = str(surface or "").casefold()
    for sentence in sentences:
        if needle and needle in sentence.casefold():
            return sentence[:400]
    return sentences[0][:400]
