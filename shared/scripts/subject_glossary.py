"""Subject glossary for PDF word lookup: 词汇库 first, then this table, then the dictionary.

The table ``shared/templates/subject_glossary.tsv`` gives the meaning a word has
in mathematics, physics, computer science or microelectronics, where a general
English–Chinese dictionary misleads ("ring" 环, "field" 域/场, "gate" 栅极/门).
Phrases ("exact sequence") are looked up whole, with a plural last word reduced.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from shared.scripts.reading_help_text import word_lookup_candidates

GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "templates" / "subject_glossary.tsv"
# Terms taken from Wikidata (CC0); used only when the hand-written table has no entry.
WIKIDATA_GLOSSARY_PATH = GLOSSARY_PATH.with_name("subject_glossary_wikidata.tsv")
SUBJECT_ORDER = ("数学", "物理", "计算机", "微电子", "电工电子", "物理化学")


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().replace("‐", "-").replace("–", "-")).casefold()


@lru_cache(maxsize=4)
def load_glossary(path: str = str(GLOSSARY_PATH)) -> dict[str, list[dict[str, str]]]:
    entries: dict[str, list[dict[str, str]]] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip() or not parts[2].strip():
            continue
        extra = parts[3].strip() if len(parts) > 3 else ""
        from_wikidata = bool(re.fullmatch(r"Q\d+", extra))
        entry = {
            "term": parts[0].strip(),
            "subject": parts[1].strip(),
            "chinese": parts[2].strip(),
            "note": f"来源：Wikidata {extra}（维基条目名，请结合上下文判断）" if from_wikidata else extra,
        }
        entries.setdefault(_key(entry["term"]), []).append(entry)
    return entries


def _phrase_candidates(text: str) -> list[str]:
    words = _key(text).split(" ")
    if len(words) == 1:
        return [_key(candidate) for candidate in word_lookup_candidates(words[0])]
    # Only the last word of a phrase inflects: "exact sequences" → "exact sequence".
    return [" ".join(words[:-1] + [_key(candidate)]) for candidate in word_lookup_candidates(words[-1])]


def lookup_glossary(
    text: str,
    *,
    path: str = str(GLOSSARY_PATH),
    supplement_path: str = str(WIKIDATA_GLOSSARY_PATH),
) -> list[dict[str, str]]:
    """Every subject's meaning of a word or phrase, in a fixed subject order.

    The hand-written table wins (it separates the senses of "ring", "gate"...);
    the Wikidata supplement answers only words that table lacks.
    """

    for table_path in (path, supplement_path):
        table = load_glossary(table_path)
        for candidate in _phrase_candidates(text):
            rows = table.get(candidate)
            if rows:
                return sorted(rows, key=lambda row: SUBJECT_ORDER.index(row["subject"]) if row["subject"] in SUBJECT_ORDER else 99)
    return []


def glossary_entry_for_vocabulary(row: dict[str, Any], context: str) -> dict[str, str]:
    return {
        "term": str(row.get("term") or ""),
        "part_of_speech": "",
        "definition": f"{row.get('chinese', '')}（{row.get('subject', '')}）",
        "pronunciation": "",
        "source": "学科术语表",
        "note": "；".join(value for value in (str(row.get("note") or ""), context) if value),
    }
