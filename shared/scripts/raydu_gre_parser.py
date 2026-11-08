"""Structured parser for 《字彙高點：旋元佑GRE字彙通》, read from the publisher's PDF.

There is no TeX source of this book; every entry is read from the PDF's
positioned text.  A page has two columns:

* left (x < 190): the headword (Arial Black), its KK phonetics in square
  brackets and the 解｜ word analysis (segments ``a/bysm/al`` on one line,
  their meanings ``without/bottom/(a.)`` and an optional Chinese note below);
* right: senses ``(a.) 无底的；极度的``, each followed by 例｜ examples
  (English, then Chinese), and the 同｜ synonyms and 衍｜ derived words.

An entry owns the left-column lines from its headword to the next headword
and the right-column lines from just above its headword to just above the
next one; lines at the top of a page before its first headword continue the
previous entry.  Section 1 groups the words by root (the white band
``a-: without`` with ``Track 002``), Section 2 by initial letter.

The KK phonetics are drawn with two symbol fonts (PhoneticSymbol and Kk) whose
letter keys stand for phonetic symbols; :data:`KK_SYMBOLS` maps them.  The
book is in Traditional characters; all Chinese is converted to Simplified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.scripts.chinese_simplify import to_simplified

BOOK_TITLE = "旋元佑GRE字汇通"
MATERIAL_CODE = "XY-GRE-VOCABULARY"
CARD_PREFIX = "gre:"
UNIT_PREFIX = "gre:"

# Keys of the PhoneticSymbol font → KK symbols (checked against the words they spell:
# [Anod1n] anodyne, [b0ntI] bounty, [sp35En] aspersion, [In7AntmEnt] enchantment ...).
KK_SYMBOLS = {
    "E": "ə", "I": "ɪ", "A": "æ", "W": "ɛ", "a": "ɑ", "C": "ɔ", "V": "ʌ", "U": "ʊ",
    "0": "aʊ", "1": "aɪ", "2": "ɚ", "3": "ɝ", "4": "ɔɪ", "5": "ʒ", "6": "ð", "7": "tʃ",
    "8": "θ", "9": "ŋ", "G": "dʒ", "S": "ʃ", "L": "ḷ", "M": "m̩", "N": "ṇ", ",": "ˌ",
}
# The Kk font only draws the stress marks.  They are written like the pronunciations
# of 《英文字汇》 (ˈ primary, ˌ secondary), as the two books are studied as one.
KK_STRESS = {"`": "ˈ", "\uf060": "ˈ", "'": "ˌ"}
PHONETIC_FONTS = ("PhoneticSymbol", "Kk")

# The book's parts of speech, shown like the 字汇 cards (名 / 动 / 形 ...).
POS_NAMES = {"n.": "名", "v.": "动", "a.": "形", "adj.": "形", "adv.": "副", "ad.": "副",
             "prep.": "介", "conj.": "连", "pron.": "代", "int.": "叹"}

LEFT_COLUMN = 190.0


class PdfParseError(ValueError):
    """The PDF does not have the layout the parser knows."""


@dataclass
class Span:
    text: str
    font: str
    size: float
    color: int


@dataclass
class Line:
    page: int  # 1-based
    x0: float
    top: float
    spans: list[Span]

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    @property
    def left(self) -> bool:
        return self.x0 < LEFT_COLUMN

    def first_font(self) -> str:
        return next((span.font for span in self.spans if span.text.strip()), "")


def extract_lines(pdf_path: Path | str) -> tuple[list[Line], list[list[Any]], int]:
    import fitz

    lines: list[Line] = []
    with fitz.open(str(pdf_path)) as document:
        toc = document.get_toc()
        page_count = document.page_count
        for index, page in enumerate(document):
            for block in page.get_text("dict")["blocks"]:
                for raw in block.get("lines", []):
                    spans = [Span(span["text"], span["font"], round(span["size"], 1), int(span["color"]))
                             for span in raw["spans"] if span["text"]]
                    # Page furniture: the side tab (Section / letter), page numbers.
                    spans = [span for span in spans if not span.font.startswith(("Billboard", "Eurostile"))]
                    if spans and "".join(span.text for span in spans).strip():
                        lines.append(Line(index + 1, raw["bbox"][0], raw["bbox"][1], spans))
    lines.sort(key=lambda line: (line.page, round(line.top), line.x0))
    return lines, toc, page_count


# ---------------------------------------------------------------------------
# Line kinds


def _is_headword(line: Line) -> bool:
    return line.left and line.first_font().startswith("Arial-Black") and line.spans[0].size >= 13


def _root_band(line: Line) -> str | None:
    """`` a-: without `` on the white band of a root (Section 1)."""
    span = line.spans[0]
    if span.font.startswith("Arial-Bold") and span.size >= 15 and span.color == 0xFFFFFF:
        return re.sub(r"\s+", " ", line.text).strip()
    return None


def _track(line: Line) -> str | None:
    match = re.fullmatch(r"\s*Track\s+(\d+(?:-\d+)?)\s*", line.text)
    return match.group(1) if match else None


def _letter(line: Line) -> str | None:
    if line.first_font().startswith("Magneto") and re.fullmatch(r"[A-Z]", line.text.strip()):
        return line.text.strip()
    return None


def _marker(line: Line) -> str:
    """例 / 同 / 衍 / 解 at the start of a line ("例｜")."""
    match = re.match(r"\s*([例同衍解])｜", line.text)
    return match.group(1) if match else ""


def _is_sense(line: Line) -> bool:
    span = next((span for span in line.spans if span.text.strip()), None)
    return bool(span and span.font.startswith("TimesNewRoman") and span.color != 0x231F20
                and re.match(r"\s*\([a-z., ]+\)", span.text))


def _is_phonetic(line: Line) -> bool:
    return line.text.strip().startswith("[") and any(span.font.startswith(PHONETIC_FONTS) for span in line.spans)


def phonetic_text(line: Line) -> str:
    out = []
    for span in line.spans:
        if span.font.startswith("PhoneticSymbol"):
            out.append("".join(KK_SYMBOLS.get(char, char) for char in span.text))
        elif span.font.startswith("Kk"):
            out.append("".join(KK_STRESS.get(char, char) for char in span.text))
        else:
            out.append(span.text)
    return re.sub(r"\s+", "", "".join(out))


def _english(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _join(previous: str, more: str) -> str:
    previous, more = previous.rstrip(), more.strip()
    if not previous:
        return more
    if not more:
        return previous
    cjk = "\u3000" <= previous[-1] <= "\uffef" or "\u3000" <= more[0] <= "\uffef" or "\u4e00" <= previous[-1] <= "\u9fff"
    if cjk or previous.endswith("-"):
        return previous + more
    return previous + " " + more


_HAN = re.compile(r"[\u3400-\u9fff\uff08\uff09\uff0c\uff1b\u3001\u3002\u300c\u300d]")


def _split_bilingual(line: Line) -> tuple[str, str]:
    """The English and the Chinese of one line: the Chinese starts at the first span
    with Chinese characters (a dash in a Chinese font inside the English stays English)."""
    english, chinese = [], []
    for span in line.spans:
        if chinese or _HAN.search(span.text):
            chinese.append(span.text)
        else:
            english.append(span.text)
    return _english("".join(english)), "".join(chinese).strip()


# ---------------------------------------------------------------------------
# Entries


def _pos_label(raw: str) -> str:
    parts = [part.strip() for part in raw.strip("() ").split(",") if part.strip()]
    return "、".join(POS_NAMES.get(part, part) for part in parts)


def _derived(text: str) -> list[dict[str, str]]:
    """``abyss (n.) 深渊；无底洞\u3000allude to 提到\u3000dormitory (n.) 宿舍（简写 dorm）``
    → term / pos / definition.  Items are separated by wide spaces or line ends;
    a phrase such as "allude to" has no part of speech."""
    items = []
    for chunk in re.split(r"\u3000+", text):
        match = re.fullmatch(r"\s*([A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f'\u2019\-. ]*?)\s*(?:\(([a-z., ]+)\))?\s*"
                             r"([\u3400-\u9fff\uff08\u300a].*?)\s*", chunk)
        if match:
            items.append({"term": _english(match.group(1)), "pos": _pos_label(match.group(2) or ""),
                          "definition": match.group(3)})
        elif chunk.strip():
            items.append({"term": "", "pos": "", "definition": chunk.strip()})  # reported by the caller
    return items


def _new_sense(pos_raw: str, definition: str) -> dict[str, Any]:
    return {"pos": _pos_label(pos_raw), "pos_raw": pos_raw, "definition": definition, "examples": [],
            "synonyms_text": "", "derived_text": ""}


def _build_entry(term: str, page: int, left: list[Line], right: list[Line]) -> tuple[dict[str, Any], list[str]]:
    """One entry from its lines.  Text stays Traditional while it is assembled and is
    converted at the end, so a phrase broken over two lines (著稱) converts as one."""

    term = _english(term)
    problems: list[str] = []
    corrections: list[str] = []
    pronunciation = ""
    segments, meanings, note = "", "", ""
    state = ""
    for line in left:
        if _is_phonetic(line):
            pronunciation = phonetic_text(line)
            state = "phonetic"
            continue
        if _marker(line) == "解":
            segments = _english(line.text.split("｜", 1)[1])
            state = "segments"
            continue
        if state not in {"segments", "meanings", "note"}:
            if line.text.strip():
                problems.append(f"{term}：左栏有无法归属的文字 {line.text.strip()[:30]}")
            continue
        english, chinese = _split_bilingual(line)
        if state == "note" or (not english and chinese) or line.text.strip().startswith("\uff08"):
            # The note in brackets under the meanings ("（与 light 有关）"), English included.
            note = _join(note, line.text)
            state = "note"
        elif state == "segments":
            meanings, state = english, "meanings"
            note = _join(note, chinese)
        else:
            meanings = _join(meanings, english)
            note = _join(note, chinese)
    senses: list[dict[str, Any]] = []
    entry_level = _new_sense("", "")  # 同 / 衍 printed before the first sense
    state = ""
    for line in right:
        marker = _marker(line)
        if _is_sense(line):
            first = next(span for span in line.spans if span.text.strip())
            match = re.match(r"\s*(\([a-z., ]+\))\s*(.*)", first.text)
            rest = (match.group(2) if match else "") + "".join(span.text for span in line.spans[line.spans.index(first) + 1:])
            senses.append(_new_sense(match.group(1) if match else "", rest.strip()))
            state = "sense"
            continue
        owner = senses[-1] if senses else entry_level
        if marker:
            body = Line(line.page, line.x0, line.top, [Span(line.spans[0].text.split("｜", 1)[1], line.spans[0].font,
                                                            line.spans[0].size, line.spans[0].color), *line.spans[1:]])
            if marker == "例":
                if not senses:
                    problems.append(f"{term}：例句前没有释义")
                    continue
                english, chinese = _split_bilingual(body)
                owner["examples"].append({"en": re.sub(r"^\d+\.\s*", "", english), "zh": chinese})
                state = "example"
            elif marker == "同":
                owner["synonyms_text"] = _join(owner["synonyms_text"], body.text) if owner["synonyms_text"] else body.text
                state = "synonyms"
            elif marker == "衍":
                owner["derived_text"] = (owner["derived_text"] + "\u3000" if owner["derived_text"] else "") + body.text
                state = "derived"
            else:
                problems.append(f"{term}：右栏出现“{marker}｜”")
            continue
        if state == "example":
            example = owner["examples"][-1]
            english, chinese = _split_bilingual(line)
            numbered = re.match(r"(\d+)\.\s*(.*)", english)
            if numbered and example["zh"]:
                # "2. environmentally benign 对环境无害": the next example of the sense.
                owner["examples"].append({"en": numbered.group(2), "zh": chinese})
                continue
            if english and chinese and (
                (english.isdigit() and re.search(r"(?<!\d)" + re.escape(english) + r"(?!\d)", example["en"]))
                or (re.fullmatch(r"[A-Z][a-z]+", english) and re.search(r"\b" + re.escape(english) + r"\b", example["en"]))
            ):
                # Translation lines may begin with a name/year already present in English.
                chinese, english = line.text.strip(), ""
            if english and not example["zh"]:
                example["en"] = _join(example["en"], english)
            elif english:
                problems.append(f"{term}：例句译文之后又有英文 {english[:30]}")
            if chinese:
                example["zh"] = _join(example["zh"], chinese)
        elif state == "synonyms":
            owner["synonyms_text"] = _join(owner["synonyms_text"], line.text)
        elif state == "derived":
            # A continued item (Chinese, or "（复数为 phenomena）") joins the item above;
            # a line starting with an English word is a new item.
            joined = line.text.strip()
            if joined.startswith("\uff08") or _HAN.match(joined):
                owner["derived_text"] = owner["derived_text"].rstrip(" \u3000") + joined
            else:
                owner["derived_text"] += "\u3000" + joined
        elif state == "sense" and all(span.color != 0x231F20 for span in line.spans if span.text.strip()):
            owner["definition"] = _join(owner["definition"], line.text)
        elif line.text.strip():
            problems.append(f"{term}：右栏有无法归属的文字 {line.text.strip()[:30]}")
    for sense in senses:
        for example in list(sense["examples"]):
            # The book once prints 例｜ for 同｜ ("例｜ disaster, catastrophe, calamity").
            if not example["zh"] and re.fullmatch(r"[a-z]+(?:, [a-z]+)+", example["en"]) and not sense["synonyms_text"]:
                sense["synonyms_text"] = example["en"]
                sense["examples"].remove(example)
                corrections.append(f"{term}：原书把同义字印成“例｜”，已归为同义字")
    # Convert, split the lists, and gather the entry's synonyms and derived words.
    synonyms: list[str] = []
    derived: list[dict[str, str]] = []
    for sense in [entry_level, *senses]:
        sense["definition"] = to_simplified(sense["definition"].strip())
        for example in sense["examples"]:
            example["zh"] = to_simplified(example["zh"])
            if not example["en"] or not example["zh"]:
                problems.append(f"{term}：例句缺英文或中文 {example}")
        sense["synonyms"] = [_english(item) for item in re.split(r"\s*[,，]\s*", sense.pop("synonyms_text")) if item.strip()]
        sense["derived"] = []
        for item in _derived(to_simplified(sense.pop("derived_text"))):
            if not item["term"]:
                problems.append(f"{term}：衍生字无法拆分 {item['definition'][:30]}")
                continue
            sense["derived"].append(item)
        synonyms.extend(item for item in sense["synonyms"] if item not in synonyms)
        derived.extend(item for item in sense["derived"]
                       if (item["term"], item["pos"]) not in {(known["term"], known["pos"]) for known in derived})
    if not senses:
        problems.append(f"{term}：没有释义")
    if not pronunciation:
        problems.append(f"{term}：没有音标")
    morph = []
    if segments:
        morph.append({"segments": segments, "meanings": meanings, "note": to_simplified(note.strip())})
    return {
        "term": term,
        "pronunciation": f"[{pronunciation.strip('[]')}]" if pronunciation else "",
        "morph": morph,
        "senses": senses,
        "synonyms": synonyms,
        "derived": derived,
        "page": page,
        "warnings": corrections,
    }, problems


def parse_book(pdf_path: Path | str) -> dict[str, Any]:
    """Every entry with its unit (root or letter), plus the problems found."""

    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"未找到《{BOOK_TITLE}》PDF：{path}")
    lines, toc, page_count = extract_lines(path)
    titles = {to_simplified(str(title).strip()): int(page) for _level, title, page, *_ in toc}
    section1 = next((page for title, page in titles.items() if title.startswith("Section 1")), None)
    section2 = next((page for title, page in titles.items() if title.startswith("Section 2")), None)
    appendix = next((page for title, page in titles.items() if title.startswith("附录一")), None)
    if not (section1 and section2 and appendix):
        raise PdfParseError(f"PDF 书签与《{BOOK_TITLE}》的结构不符（需要 Section 1、Section 2 和附录一）。")
    body = [line for line in lines if section1 <= line.page < appendix]
    units: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    issues: list[str] = []
    unit: dict[str, Any] | None = None
    letter = ""
    # Pending entry: headword, its lines in both columns.
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        entry, problems = _build_entry(current["term"], current["page"], current["left"], current["right"])
        issues.extend(problems)
        entry["unit_key"] = current["unit"]["unit_key"]
        entry["order"] = len(entries)
        entries.append(entry)
        current["unit"]["terms"].append(entry["term"])
        current = None

    def new_unit(title: str, section: int, track: str = "", page: int = 0) -> dict[str, Any]:
        item = {
            "unit_key": f"{UNIT_PREFIX}{len(units) + 1:03d}",
            "section": section,
            "letter": letter,
            "title": title,
            "track": track,
            "page_start": page,
            "page_end": page,
            "terms": [],
        }
        units.append(item)
        return item

    pages: dict[int, list[Line]] = {}
    for line in body:
        pages.setdefault(line.page, []).append(line)
    for page_number in sorted(pages):
        page_lines = pages[page_number]
        section = 2 if page_number >= section2 else 1
        # Lines come in (top, x) order: a headword precedes its own right-column lines,
        # which start a few points lower, and the lines at the top of a page continue
        # the entry of the previous page.
        for line in page_lines:
            if _letter(line):
                letter = _letter(line) or letter
                continue
            band = _root_band(line)
            track = _track(line)
            if band is not None or track is not None:
                finish()
                if band is not None and section == 1:
                    unit = new_unit(band, 1, page=page_number)
                elif track is not None:
                    if section == 2 and (unit is None or unit["section"] != 2 or unit["letter"] != letter):
                        unit = new_unit(f"{letter}", 2, track, page_number)
                    elif unit is not None and not unit["track"]:
                        unit["track"] = track
                continue
            if _is_headword(line):
                finish()
                if unit is None:
                    issues.append(f"第 {page_number} 页：{line.text.strip()} 前没有字根或字母分组")
                    unit = new_unit(letter or "?", section, page=page_number)
                current = {"term": line.text, "page": page_number, "left": [], "right": [], "unit": unit}
                unit["page_end"] = page_number
                continue
            if current is None:
                if page_number in (section1, section2):
                    continue  # the title page of a section
                if line.text.strip():
                    issues.append(f"第 {page_number} 页：词条之外的文字 {line.text.strip()[:30]}")
                continue
            (current["left"] if line.left else current["right"]).append(line)
    finish()
    codes = [entry["term"] for entry in entries]
    for unit_item in units:
        unit_item["card_count"] = len(unit_item["terms"])
    empty = [unit_item["title"] for unit_item in units if not unit_item["terms"]]
    if empty:
        issues.append("没有词条的分组：" + "、".join(empty))
    return {
        "book_title": BOOK_TITLE,
        "entries": entries,
        "units": units,
        "issues": issues,
        "warnings": [warning for entry in entries for warning in entry["warnings"]],
        "entry_count": len(codes),
        "page_count": page_count,
    }


__all__ = ["BOOK_TITLE", "CARD_PREFIX", "KK_SYMBOLS", "MATERIAL_CODE", "PdfParseError", "parse_book", "phonetic_text"]
