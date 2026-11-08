"""Writer-facing derivatives of locked recording evidence, never a second recorder.

Older episodes carry a teacher-only draft for each formal subsection, written from the
locked board notes and transcript. New episodes have none. Raw evidence is never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

VERSION = 1
JSON_FILENAME = "WRITING_MATERIALS.json"
MARKDOWN_FILENAME = "WRITING_MATERIALS.md"
SOURCE_FILES = ("BOARD_NOTES.md", "AUDIO_TRANSCRIPT.md", "BOARD_KEYFRAMES.md",
                "LECTURE_ERRORS_AND_CORRECTIONS.md")

BLOCK_SCHEMA = {
    "start_video_time": "number; first covered second, not a formal outline boundary",
    "end_video_time": "number; exclusive last covered second",
    "title": "precise mathematical topic, distinguish objects and properties",
    "content_markdown": "complete connected source-supported mathematical record, NOT a short summary",
    "terminology": ["ASR wording -> normalized term, with reason or explicit uncertainty"],
    "teacher_intent": ["teaching motivation, analogy, warning or important explanation; do not invent quotations"],
    "expansion_tasks": ["exact statement/hypotheses and missing argument or calculation the writer must expand"],
    "uncertainties": ["specific unresolved reading or incompatible statement; identify what needs checking"],
    "source_refs": [{"file": "BOARD_NOTES.md | AUDIO_TRANSCRIPT.md", "time": "HH:MM:SS",
                     "quote": "optional exact source substring, never a normalized quotation"}],
}

# A record far shorter than the board notes it covers is a summary, not a draft.
# Very short units are exempt because a brief statement can be complete.
MIN_BOARD_NOTES_RATIO = 0.35
MIN_BOARD_NOTES_CHECKED = 1500

EDITOR_CONTRACT = """
WRITER MATERIALS (not another review):
Record only what this teacher presented. You see no textbook, and you must not add
material from memory; a later writer combines this record with the textbooks. Correct
transcription errors and smooth the teacher's slips and broken sentences, but keep all
of the teacher's mathematics and explanation. This is the full account of the class, so
it should stay close to the length of the mathematical notes it covers, not shrink to a
summary. Return writing_materials as chronological blocks matching writing_materials_schema.
Cover the supplied owned time range completely, without gaps or overlapping blocks.
These are evidence spans, NOT Chapters/Sections/Subsections or directory decisions.
Write a continuous, detailed mathematical record: preserve EVERY definition,
hypothesis, quantifier, formula, domain/codomain, example, counterexample, proof
step, teacher motivation, warning and substantive spoken explanation. Remove
conversational repetition, not mathematics. Resolve cross-window cut sentences
from their continuation; distinguish review of an old claim from a new claim.
Correct ASR terminology only when context supports it. Do not give a property the
name of its underlying object. Preserve notation and map directions explicitly.
Detect skipped or sketched proofs from the actual speech EVEN WHEN no legacy
proof marker exists: put the exact proposition, assumptions and missing steps
in expansion_tasks. Do NOT invent a completed classroom proof or claim the
teacher proved what the teacher omitted. The writer will expand these arguments
using the textbook. A later completed proof resolves the earlier gap; explain
that continuation rather than repeating the old missing-proof warning.
Separate source-supported statements from uncertain readings and proposed
textbook expansions. Mark ambiguity explicitly; never silently choose a fluent
guess. A geometrical analogy is not a proof. Keep significant teacher intentions
in teacher_intent, with exact quotations only in source_refs.quote.
Every block needs source_refs to the supplied timestamped BOARD_NOTES.md or
AUDIO_TRANSCRIPT.md. Copy a timestamp that actually exists in that exact file;
do not assign an ASR timestamp to board notes that use different time anchors.
quote, when present, must be verbatim. No new diagram source,
no invented textbook references, no independent audit or formal lecture LaTeX.
The record must be sufficient for the writer to organize a detailed lecture
without reconstructing all raw files again; it is not a table of contents.
Before returning, privately check the mathematical formulation of this record
and its expansion tasks once: retain all hypotheses, both directions of an iff,
and the precise local/global quantifiers. Do not turn a suggested
expansion into a false theorem by omitting these restrictions. This is your
own formulation check in the same response, not an additional reviewer call.
Expansion tasks concern claims actually developed in this range. Merely naming
a future subject, book, historical topic or prerequisite does not assign its
full theory to this writing unit. Keep optional background conditional and do
not turn bibliographic chatter into mandatory new definitions and proofs.
""".strip()


def fingerprint(source_texts: dict[str, str], ranges: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps({"version": VERSION, "sources": source_texts,
        "ranges": ranges}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def seconds(value: str) -> float:
    match = re.fullmatch(r"(\d{2,}):([0-5]\d):([0-5]\d)", value)
    if not match:
        raise ValueError("Writer material source time must be HH:MM:SS.")
    h, m, s = map(int, match.groups())
    return float(h * 3600 + m * 60 + s)


def normalize_blocks(raw: Any, sources: dict[str, str], start: float, end: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("Writer materials are missing; raw evidence remains the fallback.")
    result = []
    cursor = start
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("Writer material block must be an object.")
        left, right = float(row["start_video_time"]), float(row["end_video_time"])
        if not (math.isfinite(left) and math.isfinite(right)) or right <= left:
            raise ValueError("Writer material interval is invalid.")
        if abs(left - cursor) > 0.01 or right > end + 0.01:
            raise ValueError("Writer material intervals have a gap, overlap or scope overflow.")
        block = {"start_video_time": left, "end_video_time": right}
        for name in ("title", "content_markdown"):
            if not isinstance(row.get(name), str) or not row[name].strip():
                raise ValueError("Writer material lacks " + name)
            block[name] = row[name].strip()
        for name in ("terminology", "teacher_intent", "expansion_tasks", "uncertainties"):
            values = row.get(name)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError("Writer material requires a string list: " + name)
            block[name] = list(dict.fromkeys(v.strip() for v in values))
        refs = row.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise ValueError("Writer material has no source anchors.")
        block["source_refs"] = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("Invalid writer source anchor.")
            filename, timestamp = str(ref.get("file") or ""), str(ref.get("time") or "")
            when = seconds(timestamp)
            # A continuation may cite an earlier block in this same writing
            # unit. Keep the unit boundary strict, not an artificial block wall.
            if filename not in ("BOARD_NOTES.md", "AUDIO_TRANSCRIPT.md") or not start <= when < end + 0.001:
                raise ValueError("Writer source anchor is outside its assigned evidence range.")
            text = sources.get(filename, "")
            # Anchor to an actual timestamp line, not an incidental time in prose.
            anchors = list(re.finditer(r"(?m)^\s*(?:#{1,6}\s+)?(?:[-*]\s+)?(?:\*\*)?\[?(\d{2,}:\d{2}:\d{2})(?=\]|\b)", text))
            matches = [i for i, m in enumerate(anchors) if m.group(1) == timestamp]
            if not matches:
                block["uncertainties"].append(
                    f"出处待核对：模型标记 {filename} @ {timestamp}，该文件没有此时间锚点；未把此引用当作已验证出处。")
                continue
            quote = str(ref.get("quote") or "")
            if quote and not any(quote in text[anchors[i].end():anchors[i + 1].start() if i + 1 < len(anchors) else len(text)] for i in matches):
                block["uncertainties"].append(
                    f"原话摘引待核对：{filename} @ {timestamp} 的模型摘引并非逐字原文，未采纳为引语：{quote[:160]}")
                quote = ""
            block["source_refs"].append({"file": filename, "time": timestamp, "quote": quote})
        if not block["source_refs"]:
            raise ValueError("Writer material block has no valid source anchors.")
        block["uncertainties"] = list(dict.fromkeys(block["uncertainties"]))
        result.append(block)
        cursor = right
    if abs(cursor - end) > 0.01:
        raise ValueError("Writer material omits the end of its assigned range.")
    return result


def _visible_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def completeness_problem(blocks: list[dict[str, Any]], sources: dict[str, str]) -> str:
    """Why these blocks are too short to be the class record, or an empty string."""
    board = _visible_length(sources.get("BOARD_NOTES.md", ""))
    written = sum(_visible_length(str(block.get("content_markdown") or "")) for block in blocks)
    if board >= MIN_BOARD_NOTES_CHECKED and written < board * MIN_BOARD_NOTES_RATIO:
        return (f"Writer materials are a summary: {written} characters for {board} characters of "
                f"board notes (at least {MIN_BOARD_NOTES_RATIO:.0%} is required).")
    return ""


def render_packet(packet: dict[str, Any]) -> str:
    parts = ["# 连续写作材料", "课堂内容的规范整理；不是正式讲义，也不替代对应教材。",
        "完整阅读本文件；原始转写和板书按来源时间定位回查，不要求每次全量重读。",
        "时间覆盖和出处已作本地结构检查，不代表数学内容已经独立审核。",
        "未展开的论证必须由写作者补齐；有疑点时只回查指定证据，不能静默猜测。"]
    for block in packet["blocks"]:
        parts += [f"## {block['title']}",
            (f"证据分集ID：{block['source_episode_id']}。" if block.get('source_episode_id') else ""),
            f"来源时间：{block['start_video_time']:g}–{block['end_video_time']:g} 秒。",
            block["content_markdown"]]
        for field, title in (("terminology", "术语整理"), ("teacher_intent", "老师的讲解意图"),
                             ("expansion_tasks", "论证展开与范围提醒"), ("uncertainties", "疑点及定点回查")):
            if block[field]:
                parts += ["### " + title, "\n".join("- " + v for v in block[field])]
        parts += ["来源定位：" + "；".join(f"{r['file']} @ {r['time']}" for r in block['source_refs'])]
    return "\n\n".join(parts) + "\n"
