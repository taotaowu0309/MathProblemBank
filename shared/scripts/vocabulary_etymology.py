"""Word origins (字源) for 背单词 cards.

Work or Claude writes a 字源 from the evidence gathered here and saves it through
``save_vocabulary_etymology``; the project itself calls no model.  ``validate_etymology``
checks the result before it is stored.

Evidence = OpenEtymology morphemes and notes, Etymological WordNet origins and
cognates, MorphoLex/ECDICT word-root splits and the book's split.  The writer may state
well-established facts beyond it but must mark anything uncertain; no invented dates,
people or sources.

A word the book splits (《英文字汇》/《GRE字汇通》 ``VocabMorph``) is explained
along that split: the word parts must be the book's segments, in order, or the
result is rejected.  Words of the same root in the same book section are the
only book cognates the writer may cite.
"""

from __future__ import annotations

import re
from typing import Any

LANGUAGE_NAMES = {
    "eng": "英语", "enm": "中古英语", "ang": "古英语", "lat": "拉丁语", "la": "拉丁语", "grc": "古希腊语",
    "fra": "法语", "fro": "古法语", "frm": "中古法语", "xno": "盎格鲁-诺曼法语", "ita": "意大利语",
    "spa": "西班牙语", "por": "葡萄牙语", "deu": "德语", "goh": "古高地德语", "nld": "荷兰语",
    "dum": "中古荷兰语", "non": "古诺尔斯语", "gem": "原始日耳曼语", "gem-pro": "原始日耳曼语",
    "ine": "原始印欧语", "ine-pro": "原始印欧语", "ara": "阿拉伯语", "fas": "波斯语", "san": "梵语",
    "heb": "希伯来语", "lat-med": "中世纪拉丁语", "lat-lat": "晚期拉丁语", "lat-vul": "通俗拉丁语",
}

AUTHOR_PROMPT = (
    "你是严谨的英语词源编辑，为中国学习者的词汇卡片写“字源”。输入是一个单词、它的中文释义和本地词典给出的证据包。\n"
    "任务：若证据包中已有 OpenEtymology 字源，判断它是否准确、详细（交代来源语言与原词及原义、构词成分、词义如何演变到现义）。"
    "准确且详细就原样保留（verdict=pass，etymology_zh 必须与原文一字不差）；有错误、把民间词源当定论或过于简略就修订（verdict=revised）；"
    "没有现成字源就撰写（verdict=written）。\n"
    "写作要求：150–300 字简体中文；依次说明来源（语言、原词、原义）、构词成分各自含义、词义演变到现代义的过程，"
    "可以用一两个同源词帮助记忆。只写证据包支持的或公认可靠的词源事实；不编造年代、人名、出处；"
    "来源不确定的地方必须明说“来源不确定”或“一说……”。构词拆分只在确有词源依据时给出，不做民间词源拆字。\n"
    "只输出 JSON，不要 Markdown：{\"verdict\": \"pass|revised|written\", \"morphemes\": "
    "[{\"segment\": \"...\", \"meaning\": \"...\", \"note\": \"...\"}], \"etymology_zh\": \"...\", "
    "\"evidence\": [\"本说明依据的证据要点\"], \"uncertain\": [\"不确定之处\"]}"
)

REVIEW_PROMPT = (
    "你是英语词源审校。输入是一个单词、本地词典证据包和一份待审的“字源”说明（含构词成分）。"
    "逐句核对：来源语言与原词是否与证据或公认词源一致；构词拆分是否有词源依据（不是民间拆字）；"
    "词义演变是否合理；有没有编造的年代、人名、出处；不确定处是否已注明；内容是否足够详细（来源、构词、演变三部分齐全）。\n"
    "全部合格时 ok=true。存在问题但能改正时 ok=false，并在 corrected_etymology_zh / corrected_morphemes 给出改正后的完整版本"
    "（同样 150–300 字，满足上述全部要求）；无法判断时 ok=false 且不给改正版本。\n"
    "只输出 JSON：{\"ok\": true|false, \"issues\": [\"...\"], \"corrected_etymology_zh\": \"\", \"corrected_morphemes\": []}"
)


BOOK_TEXT_STYLE = (
    "6. 正文是写给学习者看的：直接说“书中拆为 de / mur”，不要出现“第一种拆分”“book_split”“证据包”“依据证据”之类的说法。"
)

BOOK_SPLIT_RULES = (
    "\n本词的证据包含 book_split：这是学习者正在用的字汇书对本词的拆分，卡片上紧挨着显示。必须严格按它讲解：\n"
    "1. morphemes 的 segment 必须与 book_split 第一种拆分的各段完全相同、顺序相同（照抄，含括号），meaning 采用书中的含义；"
    "不得另行拆词、合并或增减词素。\n"
    "2. 正文逐段讲：每个词素在拉丁语/希腊语等来源语言中的原词与原义，它们怎样拼合成字面义，再怎样演变到现代各义项。\n"
    "3. 书中拆分只是为记忆而简化、与真实词源略有出入时（如同化后的字首、连接字母、书中写法与原形不同），"
    "用一句话说明“书中记作 X，原形是 Y”，但仍按书中拆分讲，不推翻它。\n"
    "4. 若 book_same_root_words 不空，可点名其中一两个同节同字根的词帮助记忆；不在其中的词不得称为“同节”；书外同源词只用公认可靠的。\n"
    "5. 若给出了第二种拆分（另一本书），可在末尾用一句话提及差别。\n"
    + BOOK_TEXT_STYLE
)

BOOK_SPLIT_REVIEW_RULES = (
    "\n证据中有 book_split（学习者所用字汇书的拆分）：另须核对待审说明是否严格按书中第一种拆分逐段讲解、"
    "构词成分与书中各段完全一致、含义不与书中冲突；若说明另起拆分或推翻书中拆分，判为不合格并按书中拆分改正。"
    "说明中称为“同节”的词必须出现在 book_same_root_words 中，否则判为不合格。"
    "给出改正版本时同样遵守：" + BOOK_TEXT_STYLE
)


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(str(code or "").casefold(), str(code or ""))


def gather_evidence(
    term: str, dictionary: Any, *, senses: str = "", book_morph: list[dict[str, str]] | None = None,
    book_section: str = "", same_root: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Everything the local dictionaries know about where ``term`` comes from."""

    evidence: dict[str, Any] = {"word": term, "chinese_senses": senses}
    entry: dict[str, Any] = {}
    if dictionary is not None:
        try:
            found = dictionary.lookup(term.casefold(), limit=1)
        except Exception:
            found = []
        entry = found[0] if found else {}
    local = entry.get("local_dictionary") or {}
    if str(local.get("etymology_zh") or "").strip():
        evidence["openetymology"] = {
            "source_book": local.get("source_book"),
            "morphemes": local.get("morphemes") or [],
            "etymology_zh": str(local.get("etymology_zh")).strip(),
        }
    morphology = entry.get("morphology") or {}
    if morphology.get("parts") and "OpenEtymology" not in str(morphology.get("source") or ""):
        evidence["morphology_split"] = {
            "source": morphology.get("source"),
            "segments": morphology.get("segments"),
            "parts": [
                {"segment": part.get("segment"), "kind": part.get("kind"), "meaning": part.get("meaning"), "origin": part.get("origin")}
                for part in morphology["parts"]
            ],
        }
    relations: list[str] = []
    if dictionary is not None and hasattr(dictionary, "etymology"):
        try:
            rows = dictionary.etymology(term, limit=24, depth=2)
        except Exception:
            rows = []
        for row in rows:
            related = str(row.get("related_word") or "")
            language = str(row.get("related_language") or "")
            relation = str(row.get("relation") or "").split(":")[-1]
            if not related:
                continue
            if language != "eng" or relation in {"etymology", "etymologically_related"}:
                relations.append(f"{relation}: {language_name(language)} {related}")
    if relations:
        evidence["etymological_wordnet"] = list(dict.fromkeys(relations))[:16]
    splits = [
        {"segments": book_segments(item.get("segments")), "meanings": book_segments(item.get("meanings"))}
        for item in book_morph or [] if book_segments(item.get("segments"))
    ]
    if splits:
        evidence["book_split"] = splits
        if book_section:
            evidence["book_section"] = book_section
        if same_root:
            evidence["book_same_root_words"] = same_root
    return evidence


def book_segments(text: Any) -> list[str]:
    """``"dis / creet"`` → ``["dis", "creet"]`` (the book writes ``/`` with or without spaces)."""

    return [part.strip() for part in str(text or "").split("/") if part.strip()]


def _segment_key(segment: str) -> str:
    return re.sub(r"[^a-z]", "", str(segment).casefold())


def follows_book_split(morphemes: list[dict[str, str]], evidence: dict[str, Any]) -> bool:
    """Whether the word parts are one of the book's splits, segment by segment."""

    splits = evidence.get("book_split") or []
    if not splits:
        return True
    got = [_segment_key(item["segment"]) for item in morphemes]
    return any(got == [_segment_key(part) for part in split["segments"]] for split in splits)


def evidence_sources(evidence: dict[str, Any]) -> list[str]:
    """Names of the local dictionaries that supplied evidence, for the card's 依据 line."""

    sources = []
    if evidence.get("openetymology"):
        sources.append(str(evidence["openetymology"].get("source_book") or "OpenEtymology"))
    if evidence.get("etymological_wordnet"):
        sources.append("Etymological WordNet")
    if evidence.get("morphology_split"):
        sources.append(str(evidence["morphology_split"].get("source") or "构词词典").replace(" · local", ""))
    if evidence.get("book_split"):
        sources.insert(0, "字汇书拆分")
    return sources


def _clean_morphemes(items: Any) -> list[dict[str, str]]:
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        segment = str(item.get("segment") or "").strip()
        if segment:
            cleaned.append({
                "segment": segment,
                "meaning": str(item.get("meaning") or "").strip(),
                "note": str(item.get("note") or "").strip(),
            })
    return cleaned


def _detailed(text: str) -> bool:
    """Long enough to cover origin, word parts and development."""

    return len(re.sub(r"\s+", "", str(text or ""))) >= 80


def validate_etymology(term: str, payload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Check a written 字源 against the local evidence; returns the stored payload or raises ValueError."""

    book = bool(evidence.get("book_split"))
    verdict = str(payload.get("verdict") or "written").strip()
    text = str(payload.get("etymology_zh") or "").strip()
    morphemes = _clean_morphemes(payload.get("morphemes"))
    if verdict not in {"pass", "revised", "written", "corrected"} or not _detailed(text):
        raise ValueError("字源不完整：需要讲清来源、构词和词义发展。")
    if not follows_book_split(morphemes, evidence):
        raise ValueError("字源的构词与书中拆分不一致。")
    if len(morphemes) == 1 and morphemes[0]["segment"].strip("-").casefold() == term.casefold():
        morphemes = []  # the whole word is not a split
    return {
        "verdict": verdict,
        "morphemes": morphemes,
        "sources": evidence_sources(evidence),
        "etymology_zh": text,
        "evidence": [str(item) for item in payload.get("evidence") or []][:8],
        "uncertain": [str(item) for item in payload.get("uncertain") or []][:6],
        "review_issues": [],
        "model": str(payload.get("model") or ""),
        "book_aligned": book,
    }


VERDICT_LABELS = {
    "pass": "本地字源 · AI 核对通过",
    "revised": "AI 依据本地字源修订 · 已审校",
    "written": "AI 依据本地词源证据撰写 · 已审校",
    "corrected": "AI 审校后改正",
}
