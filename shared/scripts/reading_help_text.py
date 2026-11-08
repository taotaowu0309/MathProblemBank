"""Text helpers for reading-help marks: sentence splitting and PDF text anchoring.

Everything here is pure computation on strings and on the glyph geometry built
by ``pdf_word_geometry.build_page_geometry``.  No model is called; the Qt
reader and ``reading_help_service`` share these functions so a mark made in the
reader can be found again after the PDF is recompiled.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

BBox = tuple[float, float, float, float]

# Abbreviations whose trailing period never ends a sentence in mathematical
# English.  Compared case-insensitively without the period.
_ABBREVIATIONS = frozenset(
    {
        "e.g", "i.e", "cf", "resp", "etc", "vs", "viz", "al", "approx", "ca",
        "thm", "prop", "lem", "def", "cor", "rem", "ex", "eq", "eqs", "fig", "figs",
        "sec", "secs", "ch", "chap", "app", "no", "nos", "vol", "vols", "p", "pp",
        "ed", "eds", "prof", "dr", "mr", "mrs", "ms", "st", "jr", "sr", "op", "cit",
        "ibid", "id", "def'n", "ref", "refs", "art", "ass", "hyp", "const",
    }
)
_SENTENCE_END_RE = re.compile(r"[.!?](?:[\"'”’)\]]*)")
_OPENERS = "\"'“‘([{"


def clean_text(text: str) -> str:
    """Collapse PDF line breaks, join hyphenated line ends and normalise spaces."""

    value = unicodedata.normalize("NFC", str(text or ""))
    value = value.replace("­", "")
    value = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def match_key(text: str) -> str:
    """Letters and digits only, case-folded: robust to spacing and hyphenation changes."""

    return "".join(character.casefold() for character in clean_text(text) if character.isalnum())


def _is_boundary(text: str, end: int) -> bool:
    """Whether the terminator ending at ``end`` closes a sentence."""

    rest = text[end:]
    if not rest or not rest[0].isspace():
        return False
    following = rest.lstrip()
    if not following:
        return True
    first = following[0]
    if not (first.isupper() or first.isdigit() or first in _OPENERS or first == "$" or first == "\\"):
        return False
    start = end - 1
    while start >= 0 and text[start] in "\"'”’)]":
        start -= 1
    if start < 0 or text[start] not in ".!?":
        return True
    if text[start] != ".":
        return True
    token_start = start
    while token_start > 0 and not text[token_start - 1].isspace() and text[token_start - 1] not in "([":
        token_start -= 1
    token = text[token_start:start].strip("\"'“‘(")
    if not token:
        return True
    lowered = token.casefold()
    # A single letter before the period ("for all x.") is usually a variable
    # ending the sentence, so it is not treated as an initial.
    return not (lowered in _ABBREVIATIONS or lowered.rstrip(".") in _ABBREVIATIONS)


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences, keeping abbreviations and numbered labels intact."""

    value = clean_text(text)
    if not value:
        return []
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(value):
        end = match.end()
        if _is_boundary(value, end):
            piece = value[start:end].strip()
            if piece:
                sentences.append(piece)
            start = end
    tail = value[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def word_lookup_candidates(word: str) -> list[str]:
    """The word itself, then simple base forms (plural, past, -ing) for dictionary lookup."""

    value = str(word or "").strip()
    lowered = value.casefold()
    candidates = [value]
    if lowered != value:
        candidates.append(lowered)
    rules = (
        ("ies", "y"), ("ves", "f"), ("sses", "ss"), ("ches", "ch"), ("shes", "sh"), ("xes", "x"),
        ("es", "e"), ("s", ""), ("ied", "y"), ("ed", "e"), ("ed", ""), ("ing", "e"), ("ing", ""),
        ("ly", ""), ("er", ""), ("est", ""),
    )
    for suffix, replacement in rules:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 3:
            stem = lowered[: -len(suffix)]
            candidates.append(stem + replacement)
            if not replacement and len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] not in "aeiouls":
                candidates.append(stem[:-1])
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


# -- glyph geometry ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextSpan:
    """Ordered glyph ids on one page plus the readable text they spell."""

    page_index: int
    char_ids: tuple[int, ...]
    text: str


def _ordered_chars(geometry: Any) -> list[Any]:
    return [geometry.char(char_id) for char_id in geometry.ordered_char_ids]


def text_for_char_ids(geometry: Any, char_ids: Sequence[int]) -> str:
    """Readable text for glyphs in reading order, a space at each line change."""

    parts: list[str] = []
    previous_line: int | None = None
    for char_id in char_ids:
        glyph = geometry.char(int(char_id))
        if previous_line is not None and glyph.line_id != previous_line:
            parts.append("\n")
        parts.append(str(glyph.text))
        previous_line = glyph.line_id
    return clean_text("".join(parts))


def line_boxes(geometry: Any, char_ids: Iterable[int]) -> list[BBox]:
    """One highlight box per visual line covered by the glyphs."""

    boxes: dict[int, list[float]] = {}
    order: list[int] = []
    for char_id in char_ids:
        glyph = geometry.char(int(char_id))
        if not str(glyph.text).strip():
            continue
        x0, y0, x1, y1 = (float(value) for value in glyph.highlight_bbox)
        box = boxes.get(glyph.line_id)
        if box is None:
            boxes[glyph.line_id] = [x0, y0, x1, y1]
            order.append(glyph.line_id)
        else:
            box[0], box[1] = min(box[0], x0), min(box[1], y0)
            box[2], box[3] = max(box[2], x1), max(box[3], y1)
    return [tuple(boxes[line_id]) for line_id in order]  # type: ignore[misc]


def paragraph_char_ids(geometry: Any, char_id: int) -> tuple[int, ...]:
    """Glyphs of the PDF text block (paragraph) that contains ``char_id``."""

    block = geometry.char(int(char_id)).block_id
    return tuple(glyph.char_id for glyph in _ordered_chars(geometry) if glyph.block_id == block)


def sentence_char_ids(geometry: Any, char_id: int) -> tuple[int, ...]:
    """Glyphs of the sentence (inside its paragraph) that contains ``char_id``."""

    paragraph = paragraph_char_ids(geometry, char_id)
    if not paragraph:
        return ()
    # Build the paragraph text while remembering which glyph produced each character.
    characters: list[str] = []
    owners: list[int | None] = []
    previous_line: int | None = None
    for glyph_id in paragraph:
        glyph = geometry.char(glyph_id)
        if previous_line is not None and glyph.line_id != previous_line:
            if characters and characters[-1] == "-" and str(glyph.text)[:1].islower():
                characters.pop()
                owners.pop()
            else:
                characters.append(" ")
                owners.append(None)
        for character in str(glyph.text):
            characters.append(character)
            owners.append(glyph_id)
        previous_line = glyph.line_id
    text = "".join(characters)
    cuts = [0]
    for match in _SENTENCE_END_RE.finditer(text):
        if _is_boundary(text, match.end()):
            cuts.append(match.end())
    cuts.append(len(text))
    target_index = next((index for index, owner in enumerate(owners) if owner == int(char_id)), 0)
    for start, end in zip(cuts, cuts[1:]):
        if start <= target_index < end:
            ids: list[int] = []
            for owner in owners[start:end]:
                if owner is not None and (not ids or ids[-1] != owner):
                    ids.append(owner)
            # Drop leading/trailing whitespace glyphs.
            while ids and not str(geometry.char(ids[0]).text).strip():
                ids.pop(0)
            while ids and not str(geometry.char(ids[-1]).text).strip():
                ids.pop()
            return tuple(ids)
    return paragraph


def _key_stream(geometry: Any) -> tuple[str, list[int]]:
    """Match key of a whole page with the glyph id behind every key character."""

    keys: list[str] = []
    owners: list[int] = []
    for glyph in _ordered_chars(geometry):
        for character in unicodedata.normalize("NFC", str(glyph.text)):
            if character.isalnum():
                keys.append(character.casefold())
                owners.append(glyph.char_id)
    return "".join(keys), owners


def locate_text(
    page_count: int,
    get_geometry: Callable[[int], Any],
    text: str,
    preferred_page: int = 0,
) -> list[TextSpan] | None:
    """Find ``text`` again in a PDF, trying pages nearest ``preferred_page`` first.

    The search ignores spacing, punctuation, hyphenation and case, and allows a
    passage to continue onto the next page.  ``get_geometry`` returns the glyph
    geometry of a page (or None) and is only called for pages actually tried.
    Returns one span per page, or None when the text is no longer in the PDF.
    """

    target = match_key(text)
    if not target or page_count <= 0:
        return None
    streams: dict[int, tuple[str, list[int]] | None] = {}

    def stream(index: int) -> tuple[str, list[int]] | None:
        if index not in streams:
            geometry = get_geometry(index)
            streams[index] = _key_stream(geometry) if geometry is not None else None
        return streams[index]

    preferred = min(max(0, int(preferred_page)), page_count - 1)
    pages = sorted(range(page_count), key=lambda index: (abs(index - preferred), index))
    for index in pages:
        current = stream(index)
        if current is None:
            continue
        keys, owners = current
        position = keys.find(target)
        if position >= 0:
            ids = tuple(dict.fromkeys(owners[position:position + len(target)]))
            return [TextSpan(index, ids, text_for_char_ids(get_geometry(index), ids))]
        if index + 1 < page_count:
            following = stream(index + 1)
            if following is None:
                continue
            next_keys, next_owners = following
            position = (keys + next_keys).find(target)
            if 0 <= position < len(keys) < position + len(target):
                first = tuple(dict.fromkeys(owners[position:]))
                second = tuple(dict.fromkeys(next_owners[: position + len(target) - len(keys)]))
                return [
                    TextSpan(index, first, text_for_char_ids(get_geometry(index), first)),
                    TextSpan(index + 1, second, text_for_char_ids(get_geometry(index + 1), second)),
                ]
    return None


def context_around(geometry: Any, char_ids: Sequence[int], *, limit: int = 600) -> tuple[str, str]:
    """Paragraph text before and after the selection, trimmed to ``limit`` characters."""

    if not char_ids:
        return "", ""
    paragraph = paragraph_char_ids(geometry, int(char_ids[0]))
    order = {glyph_id: index for index, glyph_id in enumerate(geometry.ordered_char_ids)}
    first = min(order.get(int(value), 0) for value in char_ids)
    last = max(order.get(int(value), 0) for value in char_ids)
    before = [glyph_id for glyph_id in paragraph if order.get(glyph_id, 0) < first]
    after = [glyph_id for glyph_id in paragraph if order.get(glyph_id, 0) > last]
    return (
        text_for_char_ids(geometry, before)[-limit:],
        text_for_char_ids(geometry, after)[:limit],
    )
