from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from shared.scripts.online_course_writing_materials import (
    BLOCK_SCHEMA,
    EDITOR_CONTRACT,
)


PORTABLE_SKILL_PROFILES_PATH = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "ai_agent_training"
    / "portable_skill_profiles.json"
)
REFERENCE_OUTLINE_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "online_course_reference_outline_prompt.txt"
)
LATEX_DRAWING_RULES_PATH = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "online_course_latex_drawing_rules.txt"
)


def _latex_drawing_contract() -> str:
    try:
        contract = LATEX_DRAWING_RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(
            f"Permanent LaTeX drawing contract is unavailable: "
            f"{LATEX_DRAWING_RULES_PATH}"
        ) from error
    if not contract:
        raise RuntimeError("Permanent LaTeX drawing contract is empty.")
    return contract


def _diagram_timestamp_seconds(value: str) -> int:
    match = re.fullmatch(r"\s*(\d{2}):(\d{2}):(\d{2})\s*", str(value or ""))
    if not match:
        return -1
    hours, minutes, seconds = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        return -1
    return hours * 3600 + minutes * 60 + seconds


def _normalize_window_diagram_materials(
    raw_materials: Any,
    *,
    session_id: str,
    window_index: int,
    start_video_time: float,
    end_video_time: float,
) -> list[dict[str, Any]]:
    """Lock possible relationships without choosing or generating a drawing."""
    if raw_materials is None:
        return []
    if not isinstance(raw_materials, list):
        raise RuntimeError("Recording-time window Agent returned invalid diagram_materials.")
    normalized: list[dict[str, Any]] = []
    for raw in raw_materials:
        if not isinstance(raw, dict):
            raise RuntimeError(
                "Recording-time window Agent returned a non-object diagram material."
            )
        time_label = str(raw.get("time") or "").strip()
        title = str(raw.get("title") or "").strip()
        description = str(raw.get("description") or "").strip()
        backend = str(raw.get("backend") or "").strip().lower()
        diagram_source = str(raw.get("source") or "").strip()
        legacy_latex = str(raw.get("latex") or "").strip()
        seconds = _diagram_timestamp_seconds(time_label)
        if (
            seconds < 0
            or not title
            or len(description) < 24
        ):
            raise RuntimeError(
                "Recording-time window Agent returned an incomplete diagram material."
            )
        if end_video_time > start_video_time and (
            seconds < int(start_video_time - 1)
            or seconds > int(end_video_time + 1)
        ):
            raise RuntimeError(
                "Recording-time window Agent returned an out-of-range diagram timestamp."
            )
        if backend or diagram_source or legacy_latex:
            raise RuntimeError(
                "Recording-time window Agent generated drawing source before the "
                "whole-lecture necessity decision."
            )
        identity = json.dumps(
            {
                "session_id": session_id,
                "window_index": int(window_index),
                "time": time_label,
                "title": title,
                "description": description,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        normalized.append(
            {
                "diagram_id": "diagram-"
                + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                "window_index": int(window_index),
                "time": time_label,
                "time_seconds": seconds,
                "title": title,
                "description": description,
                "candidate_status": "pending_whole_lecture_necessity_review",
            }
        )
    return normalized


# User rule (2026-11-06): a mathematics textbook does not open a new subsection
# every few minutes. A 40-minute lecture has at most 4 subsections.
MATH_OUTLINE_SECONDS_PER_SUBSECTION = 600.0


def maximum_math_outline_subsections(lecture_seconds: float) -> int:
    return max(1, int(max(0.0, float(lecture_seconds)) // MATH_OUTLINE_SECONDS_PER_SUBSECTION))


def outline_unit_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identify the subsection a returned outline row belongs to."""
    existing_id = int(row.get("existing_subsection_id") or 0)
    if str(row.get("assignment") or "new").casefold() == "existing" and existing_id > 0:
        return ("existing", existing_id)
    return (
        "new",
        int(row.get("chapter_number") or 0),
        int(row.get("section_number") or 0),
        int(row.get("subsection_number") or 0),
    )


def _reference_outline_contract(required: bool) -> str:
    if not required:
        return ""
    try:
        contract = REFERENCE_OUTLINE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(
            f"Reference-authoritative outline prompt is unavailable: "
            f"{REFERENCE_OUTLINE_PROMPT_PATH}"
        ) from error
    if not contract:
        raise RuntimeError("Reference-authoritative outline prompt is empty.")
    return "\n\nMANDATORY PERSISTENT OUTLINE CONTRACT:\n" + contract


def _section_only_outline_rule(context: dict[str, Any] | None) -> str:
    """Return the course-scoped rule that keeps Subsections out of the outline."""
    context = context or {}
    if str(context.get("outline_mode") or "") != "reference_section":
        return ""
    has_reference_catalog = bool(context.get("reference_section_catalog"))
    authority = (
        "the supplied reference_section_catalog"
        if has_reference_catalog
        else "the already confirmed manual mathematical outline"
    )
    return (
        "\n\nMANDATORY SECTION-ONLY RULE: This course writes Chapter/Section only. "
        f"Use {authority} as the directory authority. A physical episode, platform part, "
        "recording session, timestamp range, or episode title is evidence metadata only "
        "and is never a Chapter, Section, or Subsection. Do not create, split, expose, "
        "or name any Subsection, including headings such as 1.1.1; keep that material "
        "inside its enclosing Section. For schema compatibility set subsection_number to "
        "1 and subsection_title exactly equal to section_title."
    )


def _reference_guided_outline_rule(context: dict[str, Any] | None) -> str:
    """Keep three-level course outlines anchored to an imported textbook."""
    context = context or {}
    if (
        str(context.get("outline_mode") or "") == "reference_section"
        or not context.get("reference_section_catalog")
    ):
        return ""
    return (
        "\n\nMANDATORY REFERENCE-GUIDED OUTLINE RULE: This course has an imported "
        "reference textbook. Use the supplied reference_section_catalog as the "
        "Chapter/Section naming and numbering framework. Select only the catalog "
        "Sections actually supported by the lecture; the course need not reproduce "
        "the textbook's complete table of contents. Every returned Chapter and "
        "Section number and title must exactly match one supplied catalog entry. "
        "Subsections remain lecture-specific: derive their boundaries and concise "
        "academic-English titles from the mathematical content, and do not create a "
        "Subsection merely to mirror every textbook topic. Weak transcript evidence "
        "does not permit placeholder directory names such as unresolved content, "
        "insufficient evidence, or no discernible content; use the reference catalog "
        "together with the available board and recording evidence, and record any "
        "remaining uncertainty only in the segment's evidence field."
    )


def _compact_reference_outline_context(
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep the current reference Section and nearby transition candidates only."""
    compact = dict(context or {})
    if not compact.get("reference_section_catalog"):
        return compact
    catalog = [
        dict(item)
        for item in compact.get("reference_section_catalog") or []
        if isinstance(item, dict)
    ]
    if len(catalog) <= 5:
        return compact
    anchors = [
        dict(item)
        for item in compact.get("existing_outline") or []
        if isinstance(item, dict)
    ]
    if not anchors:
        anchors = [
            dict(item)
            for item in compact.get("existing_subsections") or []
            if isinstance(item, dict)
        ]
    if not anchors:
        return compact
    anchor = anchors[-1]
    anchor_index = next(
        (
            index
            for index, item in enumerate(catalog)
            if int(item.get("chapter_number") or 0)
            == int(anchor.get("chapter_number") or 0)
            and int(item.get("section_number") or 0)
            == int(anchor.get("section_number") or 0)
        ),
        None,
    )
    if anchor_index is None:
        return compact
    start = max(0, anchor_index - 1)
    end = min(len(catalog), anchor_index + 4)
    compact["reference_section_catalog"] = catalog[start:end]
    compact["reference_catalog_scope"] = {
        "full_catalog_count": len(catalog),
        "sent_catalog_start_index": start,
        "sent_catalog_end_index_exclusive": end,
        "policy": "previous_current_and_next_three_sections",
    }
    return compact


def _online_course_skill_training() -> str:
    try:
        payload = json.loads(PORTABLE_SKILL_PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for raw_profile in payload.get("profiles") or []:
        if not isinstance(raw_profile, dict):
            continue
        targets = {str(item) for item in raw_profile.get("target_agents") or []}
        if "online_course" not in targets:
            continue
        rules = [str(item).strip() for item in raw_profile.get("rules") or [] if str(item).strip()]
        if not rules:
            continue
        return (
            " Portable-skill profile `"
            + str(raw_profile.get("id") or "online-course-evidence")
            + "` runs in `"
            + str(raw_profile.get("execution_mode") or "evidence_only_no_tools")
            + "` mode: "
            + " ".join(rules)
        )
    return ""


def _strip_fence(value: str, languages: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().casefold().removeprefix("```") in {
        item.strip() for item in languages.split("|")
    }:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _repair_json_closing_noise(value: str) -> str:
    """Remove isolated model noise before a closer and restore forced closers.

    This is deliberately narrower than a general JSON repairer.  It only drops
    an invalid word outside strings when a complete JSON value precedes it and
    a delimiter follows it.  Missing closing brackets are inserted only after
    such noise was removed and only when the remaining closer makes the bracket
    stack unambiguous.  Schema and coverage validation still belong to callers.
    """
    text = str(value or "")
    repaired: list[str] = []
    stack: list[str] = []
    matching_open = {"]": "[", "}": "{"}
    matching_close = {"[": "]", "{": "}"}
    inside_string = False
    escaped = False
    removed_noise = False
    index = 0

    def previous_non_whitespace() -> str:
        for item in reversed(repaired):
            if not item.isspace():
                return item
        return ""

    while index < len(text):
        character = text[index]
        if inside_string:
            repaired.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                inside_string = False
            index += 1
            continue

        if character == '"':
            inside_string = True
            repaired.append(character)
            index += 1
            continue
        if character in "[{":
            stack.append(character)
            repaired.append(character)
            index += 1
            continue
        if character in "]}":
            expected = matching_open[character]
            if stack and stack[-1] != expected:
                if not removed_noise or expected not in stack:
                    repaired.append(character)
                    index += 1
                    continue
                while stack and stack[-1] != expected:
                    repaired.append(matching_close[stack.pop()])
            if stack and stack[-1] == expected:
                stack.pop()
            repaired.append(character)
            index += 1
            continue

        if character.isalpha():
            literal = next(
                (
                    item
                    for item in ("true", "false", "null")
                    if text.startswith(item, index)
                    and (
                        index + len(item) == len(text)
                        or not (
                            text[index + len(item)].isalnum()
                            or text[index + len(item)] == "_"
                        )
                    )
                ),
                "",
            )
            if literal:
                repaired.extend(literal)
                index += len(literal)
                continue
            end = index + 1
            while end < len(text) and (
                text[end].isalpha() or text[end] in "_-"
            ):
                end += 1
            next_non_whitespace = next(
                (item for item in text[end:] if not item.isspace()),
                "",
            )
            if (
                previous_non_whitespace() in ']}"0123456789eElL'
                and next_non_whitespace in ",]}"
            ):
                removed_noise = True
                index = end
                continue

        repaired.append(character)
        index += 1

    if removed_noise:
        while stack:
            repaired.append(matching_close[stack.pop()])
    return "".join(repaired)


def _parse_json_response(value: str) -> Any:
    """Parse model JSON without losing unescaped LaTeX backslashes."""
    text = _strip_fence(value, "json")

    def decode_one(source: str) -> Any:
        stripped = source.lstrip()
        parsed, end = json.JSONDecoder().raw_decode(stripped)
        trailing = stripped[end:].strip()
        trailing_without_fence = re.sub(
            r"^```(?:json)?\s*",
            "",
            trailing,
            flags=re.IGNORECASE,
        ).lstrip()
        if trailing_without_fence.startswith(("{", "[")):
            raise json.JSONDecodeError(
                "multiple top-level JSON values",
                source,
                len(source) - len(trailing_without_fence),
            )
        return parsed

    def contains_control_characters(parsed: Any) -> bool:
        strings: list[str] = []

        def collect(item: Any) -> None:
            if isinstance(item, str):
                strings.append(item)
            elif isinstance(item, dict):
                for key, nested in item.items():
                    collect(key)
                    collect(nested)
            elif isinstance(item, list):
                for nested in item:
                    collect(nested)

        collect(parsed)
        return any(
            any(ord(character) < 32 for character in item)
            for item in strings
        )

    try:
        parsed = decode_one(text)
        if not contains_control_characters(parsed):
            return parsed
    except json.JSONDecodeError:
        pass

    repaired: list[str] = []
    inside_string = False
    index = 0
    while index < len(text):
        character = text[index]
        if character == '"':
            preceding = 0
            cursor = index - 1
            while cursor >= 0 and text[cursor] == "\\":
                preceding += 1
                cursor -= 1
            if preceding % 2 == 0:
                inside_string = not inside_string
            repaired.append(character)
            index += 1
            continue
        if inside_string and character == "\\" and index + 1 < len(text):
            escaped = text[index + 1]
            if escaped in {'"', "\\", "/"}:
                repaired.extend((character, escaped))
                index += 2
                continue
            if (
                escaped == "u"
                and index + 5 < len(text)
                and all(value in "0123456789abcdefABCDEF" for value in text[index + 2 : index + 6])
            ):
                repaired.extend(text[index : index + 6])
                index += 6
                continue
            # Model-generated LaTeX often contains raw \frac, \theta, or
            # \subseteq. Some prefixes are valid JSON escapes and would be
            # silently converted to control characters, so use the following
            # alphabetic character to distinguish them from JSON whitespace.
            command_end = index + 1
            while command_end < len(text) and text[command_end].isalpha():
                command_end += 1
            command = text[index + 1 : command_end]
            json_prefix_latex_commands = {
                "bar",
                "beta",
                "bf",
                "binom",
                "boxed",
                "frac",
                "forall",
                "nabla",
                "neq",
                "notin",
                "nu",
                "operatorname",
                "partial",
                "phi",
                "rightarrow",
                "rho",
                "rm",
                "sqrt",
                "text",
                "theta",
                "times",
                "to",
            }
            latex_like = escaped.isalpha() and (
                escaped not in "bfnrt" or command in json_prefix_latex_commands
            )
            if latex_like or escaped not in "bfnrt":
                repaired.extend(("\\", "\\", escaped))
                index += 2
                continue
        repaired.append(character)
        index += 1
    latex_repaired = "".join(repaired)
    try:
        return decode_one(latex_repaired)
    except json.JSONDecodeError:
        structurally_repaired = _repair_json_closing_noise(latex_repaired)
        if structurally_repaired == latex_repaired:
            raise
        parsed = decode_one(structurally_repaired)
        if contains_control_characters(parsed):
            raise json.JSONDecodeError(
                "control character after conservative JSON repair",
                structurally_repaired,
                0,
            )
        return parsed


class OnlineCourseAiAgent:
    """Evidence-processing agent for recorded mathematics courses.

    The agent may transcribe and faithfully typeset evidence, and may audit a
    separately authored locked lecture draft. It is never allowed to author the
    final lecture notes or answer learner questions.
    """

    # Eighteen formula-preserving images fit the proven vision payload used by
    # board transcription. Reusing that limit reduces long-episode indexing
    # requests without discarding any candidate mathematics.
    BOARD_BATCH_SIZE = 18
    BOARD_TRANSCRIPTION_BATCH_SIZE = 18
    BOARD_BATCH_WORKERS = 6
    CURATION_CORRECTION_ATTEMPTS = 2
    REQUEST_TIMEOUT_SECONDS = 180
    # Provider transport code retries the same logical request twice. Repeating
    # a whole evidence request here could duplicate image billing, so every stage
    # remains one logical invocation with three connection attempts underneath.
    REQUEST_ATTEMPTS = 1
    PREFLIGHT_ATTEMPTS = 1
    REQUIRED_MODEL = "deepseek-flash"
    # Mathematical evidence reconstruction prioritizes rigorous reasoning over
    # minimum latency. Final text parts are still parallelized independently.
    REQUIRED_REASONING_EFFORT = "high"
    # The bounded end-of-recording synthesis is a merge/repair pass over
    # already locked evidence; keep its relay request at the medium setting
    # used by the persisted invocation contract.
    FINAL_SYNTHESIS_REASONING_EFFORT = "medium"
    EVIDENCE_REQUEST_MAX_IMAGES = 24
    EVIDENCE_REQUEST_MAX_TRANSCRIPT_CHARACTERS = 60000
    TARGETED_REVIEW_MAX_IMAGES = 16
    RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION = 1
    # A five-minute window can contain a long proof plus the required JSON
    # envelope.  4,500 tokens repeatedly truncated otherwise valid windows;
    # reserve enough headroom while remaining below the provider hard ceiling.
    RECORDING_WINDOW_OUTPUT_TOKENS = 12000
    FINAL_SYNTHESIS_OUTPUT_TOKENS = 48000
    FINAL_SYNTHESIS_PART_OUTPUT_TOKENS = 8000
    # Some relay gateways return HTTP 524 before a large, reasoning-heavy
    # request reaches the user's configured client timeout.  Five recording
    # windows keep the ordinary path below that upstream budget while avoiding
    # one serial request per five-minute window.  A failed multi-window request
    # is bisected until the existing single-window safe path is reached.
    FINAL_SYNTHESIS_WINDOWS_PER_REQUEST = 5
    # Final synthesis parts are independent bounded text patches.  Keep a
    # small fixed pool so a long episode does not serialize every relay round,
    # while leaving enough headroom for the provider and preserving the
    # deterministic local merge order below.  Failed parts still fall back to
    # the existing binary retry path.
    FINAL_SYNTHESIS_PARALLEL_REQUESTS = 3
    OUTLINE_ENDPOINT_TOLERANCE_SECONDS = 5.0
    EVIDENCE_RESPONSE_SCHEMA_VERSION = 5
    EVIDENCE_RESPONSE_SCHEMA = {
        "schema_version": 5,
        "sessions": [
            {
                "session_id": "string",
                "overlap_resolution": {
                    "compared_to_session_id": "string; empty only for the first recording session",
                    "status": "no_prior_session | no_duplicate | duplicate_prefix_removed",
                    "duplicate_prefix_start_video_time": "number",
                    "duplicate_prefix_end_video_time": "number; copy the program-fixed new-content boundary",
                    "first_new_content_time": "HH:MM:SS",
                    "repeated_content_summary": "string",
                    "evidence": "specific matching speech/formulas/board states",
                },
                "deduplicated_timestamped_transcript": "timestamped transcript containing only genuinely new content",
                "selected": ["integer frame index"],
                "rejected": ["integer frame index"],
                "mathematical_content_markdown": "timestamped Markdown",
                "evidence_status": "complete | needs_three_second_fallback",
                "fallback_reason": "string",
                "uncertainties": [
                    {
                        "time": "HH:MM:SS",
                        "kind": "notation | transcription | missing_context",
                        "detail": "string",
                        "materially_affects_mathematics": "boolean",
                    }
                ],
                "fallback_ranges": [
                    {
                        "start_video_time": "number",
                        "end_video_time": "number",
                        "gap_kind": "conflicting_core_formula | unreadable_core_statement",
                        "missing_evidence": "string",
                    }
                ],
                "diagram_materials": [
                    {
                        "time": "HH:MM:SS",
                        "title": "short candidate mathematical relationship title",
                        "description": "complete mathematical relationship that may benefit from a figure",
                    }
                ],
            }
        ],
    }
    MATHEMATICAL_TRAINING = (
        "Apply the existing mathematics-assistant quality rules: mathematical correctness, exact material fidelity, "
        "notation and quantifier preservation, explicit uncertainty instead of guessing, and truthful reporting of incomplete evidence. "
        "The evidence role overrides answer-writing examples: never turn transcription into an explanation or silently complete a proof. "
        "When the lecturer assigns an exercise, leaves a result to the reader, calls a proof obvious/easy, or states a conclusion without detailed proof, "
        "preserve it and add the exact marker `【待补证明：...】` identifying the proposition to prove; do not silently prove it in the evidence stage. "
        "When the payload contains `continuation_overlap`, the application has already removed the early repeated timeline and retained "
        "up to thirty seconds before the prior endpoint for continuity. Treat the program boundary as authoritative, preserve all genuinely "
        "new material after it, and never emit the same speech, formula, board state, or screenshot twice."
        + _online_course_skill_training()
    )

    @staticmethod
    def _english_course(course_info: dict[str, Any]) -> bool:
        return str(course_info.get("course_domain") or "math").casefold() == "english"

    def __init__(self) -> None:
        self.stage_timings: list[dict[str, Any]] = []
        self.provider = None
        self.request_profile = None
        self.profile = None
        self.increment_request_count = 0
        self._successful_evidence_requests = 0
        self._course_context: dict[str, Any] = {}

    @property
    def profile_label(self) -> str:
        profile = getattr(self, "request_profile", self.profile)
        return (
            f"{getattr(profile, 'name', getattr(self.profile, 'name', 'Agent'))} / "
            f"{getattr(profile, 'model', getattr(self.profile, 'model', 'unknown'))} / "
            f"{getattr(profile, 'provider_kind', 'configured provider')}"
        )

    @staticmethod
    def _is_transient_upstream_error(error: Exception) -> bool:
        status_code = int(getattr(error, "status_code", 0) or 0)
        error_text = str(error).casefold()
        transient_status_text = re.search(
            r"\bhttp\s*(?:408|409|429|500|502|503|504|520|522|524)\b",
            error_text,
        )
        return bool(
            status_code in {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}
            or transient_status_text
            or "upstream request failed" in error_text
            or "no available channel" in error_text
            or "timeout" in error_text
            or "timed out" in error_text
            or "temporarily unavailable" in error_text
            or (
                "模型 api 请求超过" in error_text
                and "秒时限" in error_text
            )
            or "模型 api 连接中断" in error_text
            or "请求超时" in error_text
        )

    def preflight(self, emit: Callable[[str], None]) -> dict[str, Any]:
        """There is no model route to check."""
        return {"model": "none"}

    def _run(self, payload: dict[str, Any], attachments: list[dict[str, Any]], system_prompt: str,
             emit: Callable[[str], None], *, stage: str) -> str:
        """Answered by ``WorkIntakeAgent``; the project calls no model."""
        raise NotImplementedError(stage)

    @staticmethod
    def _attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for item in items:
            path = str(
                item.get("agent_path")
                or item.get("source_path")
                or item.get("path")
                or ""
            )
            if not path:
                raise ValueError("Agent image attachment has no readable source path.")
            attachments.append(
                {
                    "path": path,
                    "name": str(
                        item.get("attachment_name")
                        or item.get("name")
                        or f"frame_{int(item.get('index') or 0):04d}.jpg"
                    ),
                    "mime_type": mimetypes.guess_type(path)[0] or "image/png",
                    "kind": "image",
                }
            )
        return attachments

    def detect_lecture_outline(
        self,
        course_info: dict[str, Any],
        transcript: str,
        board_markdown: str,
        emit: Callable[[str], None],
        *,
        coverage_start: float,
        coverage_end: float,
        existing_outline: list[dict[str, Any]],
        existing_subsections: list[dict[str, Any]] | None = None,
        prohibited_boundary_times: list[float] | None = None,
        maximum_segments: int | None = None,
        _rejected_outline: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        # Mathematical meaning chooses where the boundaries go; the ceiling,
        # when the caller supplies one, limits how many there may be.
        segment_limit = (
            max(1, int(maximum_segments))
            if maximum_segments is not None
            else None
        )
        if segment_limit is None:
            collapse_rule = (
                "Do not collapse several coherent mathematical subtopics into one subsection merely "
                "because they share a Section; start a new subsection, Section, or Chapter when the mathematical scope "
                "actually changes. "
            )
            duration_rule = (
                "There is no minimum, target, or preferred duration for a Subsection. A short but mathematically complete "
                "theorem, construction, counterexample, or computation remains its own unit when the source treats it as one; "
                "a long unit remains intact when its definitions, proof, and applications form one argument. Duration may be "
                "reported as evidence, but it must never decide a split or merge. "
                "No protocol ceiling is supplied for this request. "
                "Never use a recording-window boundary as the reason for a split or merge, and avoid crossing a genuine "
                "Chapter/Section transition. "
            )
        else:
            collapse_rule = ""
            duration_rule = (
                f"HARD LIMIT: return at most {segment_limit} distinct subsections for this range. These notes read as a "
                "mathematics textbook, and a textbook does not open a new subsection every few minutes; on average a "
                "subsection covers at least ten minutes of lecture. This limit overrides every other instruction here about "
                "keeping topics in separate units. To meet it, group neighbouring topics into one broader subsection under a "
                "title that covers them: several definitions, propositions, and examples in one subsection is normal. A topic "
                "lasting only a minute or two joins a neighbouring subsection. The limit is a ceiling, not a target, so return "
                "fewer when the content allows. A subsection may continue across a textbook Section or Chapter boundary; number "
                "and title it under the Section or Chapter where it starts. "
                "Never use a recording-window boundary as the reason for a split or merge. "
            )
        section_only_rule = _section_only_outline_rule(course_info)
        reference_guided_rule = _reference_guided_outline_rule(course_info)
        outline_contract = _reference_outline_contract(
            str(course_info.get("outline_mode") or "") == "reference_section"
            and bool(course_info.get("reference_section_catalog"))
        )
        system_prompt = (
            "You are the outline-detection component of a recorded university mathematics course. "
            "Return ONLY JSON with key `segments`. Detect the actual Chapter, Section, and Subsection hierarchy "
            "supported by the mathematical content in the requested time range. Use the timestamped audio, board "
            "notes, reference materials when supplied, and the explicitly separated outline context together. "
            "`current_episode_outline` contains only already-saved ranges from this same episode and is the only outline "
            "context allowed to contain episode-local seconds. `existing_subsections` is a course-wide identity catalog: "
            "it may be used to assign a segment to a stable existing subsection, but it contains no usable boundary "
            "times and must never be used to copy or infer this episode's time endpoints. Every episode requires an "
            "independent boundary decision from its own transcript and board notes. The episode title, episode "
            "number, platform part number, recording-session boundary, candidate-frame change, and recording-window "
            "boundary are metadata only: they are never a Chapter, "
            "Section, or Subsection and must never determine a directory split. Recording-session overlap only marks "
            "repeated evidence from the same episode; it never proves that two time ranges belong to the same subsection. "
            "Decide subsection continuity and every new boundary from a genuine mathematical-content transition. "
            "First build an internal chronological content map of the definitions, constructions, families of examples, "
            "new mathematical objects, named properties, theorem-and-proof arcs, and changes in the question being studied. "
            "Then partition that map into textbook-sized mathematical writing units. Each unit has one main mathematical "
            "question and a coherent chain of definitions, hypotheses, lemmas, theorem statements, proofs, and examples. "
            "Keep a theorem with the proof that establishes it, a construction with the properties proved from it, and an "
            "example family with the criterion or theorem it illustrates. A lone definition, theorem, example, or proof step "
            "does not automatically force a boundary; it belongs to the surrounding unit unless the lecture opens a new "
            "mathematical question. Create a boundary only when the question, principal object, or proof strategy changes "
            "in a way that would justify a new textbook subsection. Follow explicit source headings and reference-outline "
            "transitions when supplied. Do not manufacture a boundary because an episode, week, five-minute evidence window, "
            "page, board, formula, or isolated proof step ended. Never use generic labels such as 'Applications ... (part 1/3)' "
            "when the evidence supports a mathematical title. "
            "Each segment object must contain integer `chapter_number`, `section_number`, and `subsection_number`; "
            "concise academic-English `chapter_title`, `section_title`, and `subsection_title`; write every "
            "mathematical symbol or expression as inline LaTeX delimited by single dollar signs, for example "
            "`$L^1$`, `$L^p$`, or `$W^{1,p}$`, while leaving ordinary English as plain text; numeric "
            "`end_video_time`; `assignment` equal to `existing` or `new`; and integer "
            "`existing_subsection_id` (the exact supplied ID for a continuation, otherwise 0). Return segments in chronological order, covering the complete requested range without "
            "gaps. If the requested range continues the last row of `current_episode_outline`, repeat exactly that row's "
            "three numbers and titles for the first segment. For cross-episode continuation, use an ID from "
            "`existing_subsections` only for direct continuation of the same focused writing unit, such as an unfinished "
            "statement, proof, construction, or example family. Sharing a Chapter/Section, broad vocabulary, or a generic "
            "old title is insufficient. After an intervening mathematical topic, prefer a new narrowly titled Subsection "
            "unless the evidence explicitly resumes the unfinished old unit. Never use an existing broad Subsection as a "
            "catch-all for several newly taught topics. Independently choose the endpoint. Create a new segment only at a "
            "real lecture-outline transition, not for a mere inequality, isolated proof step, or change of board. The final segment must end at "
            "the supplied coverage end. Do not invent a hierarchy when evidence is uncertain; preserve the most "
            "specific hierarchy supported by the reference material, existing mathematical outline, and mathematical "
            "evidence. Never derive a Chapter/Section/Subsection title or number from an episode title. The returned "
            "hierarchy is the authoritative directory decision for this range; do not leave the structure for the "
            "application to infer. A lecture may revisit an earlier textbook section after a later section, so directory "
            "numbers need not increase across the recording; only the segment time endpoints must be strictly chronological. "
            + collapse_rule +
            "Avoid omnibus titles joined from unrelated concepts. Before returning, verify that every "
            "substantial item in the internal content map belongs to exactly one focused segment, every boundary has a "
            "content-based reason, every reused ID is a direct continuation, and every new Subsection number is unused "
            "inside its Chapter/Section. `prohibited_boundary_times` lists internal evidence-processing window endpoints. "
            + duration_rule +
            "Never return one of them as an internal segment endpoint, even if it looks like a convenient round time; "
            "move to the actual mathematical transition supported by the complete evidence. Include a "
            "concise `evidence` string in every segment explaining the mathematical boundary or continuity decision. "
            + self.MATHEMATICAL_TRAINING
            + section_only_rule
            + reference_guided_rule
            + outline_contract
        )
        if self._english_course(course_info):
            system_prompt = (
                "You are the outline-detection component of a recorded English-language course. Return ONLY JSON with key "
                "`segments`. Use the complete timestamped audio, evidence notes, references, and separated outline context. Build an "
                "internal map of grammar concepts and contrasts, example families, exercises and corrections, vocabulary/word-formation "
                "topics, reading strategies, writing principles, and changes in the teaching question; then create coherent formal "
                "writing units. Recording sessions, windows, frame changes, episode titles and platform part numbers are metadata, never "
                "outline boundaries. Each segment has integer chapter/section/subsection numbers, concise academic-English titles, "
                "numeric `end_video_time`, `assignment` (`existing` or `new`), `existing_subsection_id`, and a content-based `evidence` "
                "reason. Cover the requested range without gaps and end at the supplied coverage endpoint. Reuse an existing ID only for "
                "direct continuation of the same focused teaching unit; shared vocabulary is not enough. Partition by the "
                "mathematical question and its definition/proof/example chain, following explicit textbook headings when "
                "supplied. There is no duration minimum or duration-based maximum: a short complete mathematical unit stays "
                "separate, and a long coherent argument stays together. Never place an internal endpoint at a prohibited evidence-window "
                "time merely for convenience. Do not invent Xuan Yu-You terminology or book structure without supplied evidence. "
                + section_only_rule + reference_guided_rule + outline_contract
            )
        payload = {
            **course_info,
            "coverage_start_seconds": float(coverage_start),
            "coverage_end_seconds": float(coverage_end),
            "current_episode_outline": [
                dict(item) for item in existing_outline if isinstance(item, dict)
            ],
            "existing_subsections": [
                {
                    key: item.get(key)
                    for key in (
                        "existing_subsection_id",
                        "chapter_number",
                        "chapter_title",
                        "section_number",
                        "section_title",
                        "subsection_number",
                        "subsection_title",
                        "accumulated_lecture_seconds",
                    )
                }
                for item in (existing_subsections or [])
                if isinstance(item, dict)
            ],
            "prohibited_boundary_times": [
                float(value) for value in (prohibited_boundary_times or [])
            ],
            "maximum_distinct_subsections_for_range": segment_limit,
            "subsection_capacity_seconds": None,
            "minimum_new_subsection_seconds": None,
            "raw_timestamped_transcript": transcript[:220000],
            "ai_transcribed_board_reference": board_markdown[:80000],
        }
        if _rejected_outline:
            payload["rejected_outline_over_the_limit"] = [
                {key: item.get(key) for key in ("subsection_title", "end_video_time")}
                for item in _rejected_outline
            ]
        try:
            answer = self._run(
                payload,
                [],
                system_prompt,
                emit,
                stage="AI 讲义目录识别",
            )
        except Exception as error:
            # A full-episode outline request can exceed a relay's execution
            # budget even when the recording evidence is already complete. The
            # outline is the only stage that may safely be recomposed from
            # bounded chronological ranges: split the requested time range,
            # carry the left result as continuity context, and merge adjacent
            # identical directory units. This is course/episode agnostic and
            # preserves the existing content-based boundary rules.
            span_seconds = max(0.0, float(coverage_end) - float(coverage_start))
            if self._is_transient_upstream_error(error) and span_seconds > 900.0:
                midpoint = float(coverage_start) + span_seconds / 2.0
                emit(
                    "讲义目录识别遇到中转站瞬时超时，已按时间范围二分重试；"
                    f"原范围 {float(coverage_start):.3f}–{float(coverage_end):.3f} 秒。"
                )
                left_rows = self.detect_lecture_outline(
                    course_info,
                    self._filter_timestamped_transcript(
                        transcript, coverage_start, midpoint
                    ),
                    self._filter_timestamped_transcript(
                        board_markdown, coverage_start, midpoint
                    ),
                    emit,
                    coverage_start=coverage_start,
                    coverage_end=midpoint,
                    existing_outline=existing_outline,
                    existing_subsections=existing_subsections,
                    prohibited_boundary_times=prohibited_boundary_times,
                    maximum_segments=maximum_segments,
                )
                right_rows = self.detect_lecture_outline(
                    course_info,
                    self._filter_timestamped_transcript(
                        transcript, max(float(coverage_start), midpoint - 30.0), coverage_end
                    ),
                    self._filter_timestamped_transcript(
                        board_markdown, max(float(coverage_start), midpoint - 30.0), coverage_end
                    ),
                    emit,
                    coverage_start=midpoint,
                    coverage_end=coverage_end,
                    existing_outline=[*existing_outline, *left_rows],
                    existing_subsections=existing_subsections,
                    prohibited_boundary_times=prohibited_boundary_times,
                    maximum_segments=maximum_segments,
                )
                if left_rows and right_rows:
                    comparable_keys = (
                        "chapter_number",
                        "chapter_title",
                        "section_number",
                        "section_title",
                        "subsection_number",
                        "subsection_title",
                    )
                    if all(
                        left_rows[-1].get(key) == right_rows[0].get(key)
                        for key in comparable_keys
                    ):
                        left_rows[-1] = {
                            **left_rows[-1],
                            "end_video_time": right_rows[0].get("end_video_time"),
                        }
                        right_rows = right_rows[1:]
                return [*left_rows, *right_rows]
            raise
        parsed = _parse_json_response(answer)
        rows = parsed.get("segments") if isinstance(parsed, dict) else None
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("The outline agent returned no time-bounded segments.")
        required_keys = {
            "chapter_number",
            "chapter_title",
            "section_number",
            "section_title",
            "subsection_number",
            "subsection_title",
            "end_video_time",
            "assignment",
            "existing_subsection_id",
            "evidence",
        }
        normalized: list[dict[str, Any]] = []
        previous_end = float(coverage_start)
        prohibited = {
            float(value)
            for value in (prohibited_boundary_times or [])
            if float(coverage_start) + 0.5 < float(value) < float(coverage_end) - 0.5
        }
        for item in rows:
            if not isinstance(item, dict) or any(key not in item for key in required_keys):
                raise RuntimeError(
                    "The outline agent returned a segment without the complete directory decision."
                )
            candidate = dict(item)
            candidate["start_video_time"] = previous_end
            end_time = float(candidate.get("end_video_time") or 0)
            if end_time <= previous_end + 0.001:
                raise RuntimeError("The dedicated outline Agent returned non-chronological endpoints.")
            if any(abs(end_time - boundary) <= 0.75 for boundary in prohibited):
                raise RuntimeError(
                    "The dedicated outline Agent used a recording evidence-window endpoint."
                )
            normalized.append(candidate)
            previous_end = end_time
        if abs(previous_end - float(coverage_end)) > self.OUTLINE_ENDPOINT_TOLERANCE_SECONDS:
            raise RuntimeError("The dedicated outline Agent did not cover the requested endpoint.")
        unit_count = len({outline_unit_key(item) for item in normalized})
        if segment_limit is not None and unit_count > segment_limit and _rejected_outline is None:
            emit(f"讲义目录分成了 {unit_count} 节，超过上限 {segment_limit} 节；退回重分一次。")
            return self.detect_lecture_outline(
                course_info,
                transcript,
                board_markdown,
                emit,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                existing_outline=existing_outline,
                existing_subsections=existing_subsections,
                prohibited_boundary_times=prohibited_boundary_times,
                maximum_segments=maximum_segments,
                _rejected_outline=normalized,
            )
        return normalized

    def map_reference_parts_to_subsection(
        self,
        course_info: dict[str, Any],
        subsection: dict[str, Any],
        reference_catalog: list[dict[str, Any]],
        emit: Callable[[str], None],
    ) -> dict[str, Any]:
        """Select complete stable reference sections for one existing writing unit."""
        system_prompt = (
            "You map an already-partitioned mathematics reference catalog to exactly one existing formal lecture "
            "subsection. The target subsection exists now; do not reason about future course units and do not assign "
            "anything course-wide. Inspect the target number, title, episode/outline context, its locked Mathematical "
            "Atoms and Content Contracts when supplied in `formal_content_context`, and every compact catalog record. "
            "Treat those formal records as the most precise target evidence, not as permission to expand the target. "
            "Select only complete source-section part_ids that materially help the target subsection cover its "
            "mathematics. A lexical resemblance or a broad shared subject is insufficient. Preserve the complete source "
            "part boundary; never request individual pages or excerpts. Include prerequisite definitions only when they "
            "are directly needed by this target and are not merely general background. Empty selection is valid when no "
            "catalog section is reliably relevant. Return ONLY JSON with keys `selected_parts`, `overall_reason`, and "
            "`uncovered_target_topics`. `selected_parts` is an array of objects with `part_id`, `reason`, and `coverage`; "
            "coverage is a short array of target concepts supported by that source part. Every part_id must occur in the "
            "supplied catalog, must occur at most once, and the selected list must follow catalog order. Do not claim to "
            "have seen source pages or figures: this pass receives navigation records only; the complete selected files, "
            "page crops, formulas and figure manifests are attached to the later material package. "
            + self.MATHEMATICAL_TRAINING
        )
        if self._english_course(course_info):
            system_prompt = (
                "You map an already-partitioned English-learning reference catalog to exactly one existing formal lecture-note "
                "unit. Select only complete source-section part_ids that materially support this target's grammar rules, examples, "
                "usage, vocabulary, reading strategy, writing principle, exercise explanation, or documented contrast. Lexical "
                "resemblance alone is insufficient. Preserve complete source-section boundaries, and do not anticipate future units. "
                "Empty selection is valid. Return ONLY JSON with `selected_parts`, `overall_reason`, and "
                "`uncovered_target_topics`; each selected item has `part_id`, `reason`, and `coverage`, follows catalog order, and "
                "appears at most once. This pass sees navigation records only, so never claim to have inspected source pages."
            )
        payload = {
            **course_info,
            "workflow": "incremental_reference_mapping_for_one_existing_subsection",
            "target_subsection": subsection,
            "reference_catalog": reference_catalog,
        }
        answer = self._run(
            payload,
            [],
            system_prompt,
            emit,
            stage="AI 当前小节参考教材增量映射",
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict):
            raise RuntimeError("当前小节参考教材映射 Agent 没有返回对象。")
        selected = parsed.get("selected_parts")
        if not isinstance(selected, list):
            raise RuntimeError("当前小节参考教材映射 Agent 没有返回 selected_parts。")
        return {
            "selected_parts": [dict(item) for item in selected if isinstance(item, dict)],
            "overall_reason": str(parsed.get("overall_reason") or "").strip(),
            "uncovered_target_topics": [
                str(value).strip()
                for value in parsed.get("uncovered_target_topics") or []
                if str(value).strip()
            ],
        }

    def map_reference_parts_to_subsections(
        self,
        course_info: dict[str, Any],
        subsections: list[dict[str, Any]],
        reference_catalog: list[dict[str, Any]],
        emit: Callable[[str], None],
    ) -> dict[str, Any]:
        """Map one stable catalog to several existing writing units in one request."""
        if not subsections:
            return {"subsections": []}
        target_ids = [int(item.get("subsection_id") or 0) for item in subsections]
        if any(value <= 0 for value in target_ids) or len(target_ids) != len(
            set(target_ids)
        ):
            raise ValueError(
                "Batch reference mapping requires unique positive subsection IDs."
            )
        system_prompt = (
            "You map one already-partitioned mathematics reference catalog to several existing formal lecture "
            "subsections in one bounded request. Treat every target independently: a source section relevant to one "
            "target must not leak into another target merely because both are supplied together. Inspect each target's "
            "number, title, outline context, locked Mathematical Atoms and compact Content Contract, then inspect every "
            "stable catalog navigation record once. Select only complete source-section part_ids that materially help "
            "that exact target cover its mathematics. Lexical resemblance or a broad shared subject is insufficient. "
            "Preserve source-section boundaries; never request individual pages or excerpts. Include prerequisite "
            "definitions only when directly needed by that target. Empty selection is valid. Return ONLY JSON with key "
            "`subsections`, containing exactly one object for every supplied subsection_id and no other IDs. Every row "
            "must contain `subsection_id`, `selected_parts`, `overall_reason`, and `uncovered_target_topics`. "
            "`selected_parts` contains objects with `part_id`, `reason`, and `coverage`; each part_id must occur in the "
            "catalog, occur at most once within that target, and follow catalog order. `coverage` must always be a JSON "
            "array of short target-concept strings, including when it has zero or one item; never return a scalar, "
            "object, or null. `uncovered_target_topics` must likewise always be a JSON array of strings. Do not claim to have inspected "
            "source pages or figures: this request receives navigation records only. Later packaging attaches the "
            "complete selected source sections and performs independent file/hash validation. "
            + self.MATHEMATICAL_TRAINING
        )
        if self._english_course(course_info):
            system_prompt = (
                "Map one partitioned English-learning reference catalog to several existing formal lecture-note units in one "
                "bounded request. Treat each target independently; content relevant to one must not leak into another. Select only "
                "complete source-section part_ids materially supporting that target's grammar rules, examples, usage, vocabulary, "
                "reading strategy, writing principle, exercise explanation, or documented contrast. Lexical resemblance alone is "
                "insufficient. Return ONLY JSON with `subsections`, exactly one object per supplied subsection_id. Every object has "
                "`subsection_id`, `selected_parts`, `overall_reason`, and array `uncovered_target_topics`; every selected part has "
                "`part_id`, `reason`, and array `coverage`, follows catalog order, and occurs at most once. Empty selections are valid. "
                "Do not claim to inspect source pages because this pass receives navigation records only."
            )
        payload = {
            **course_info,
            "workflow": "batch_incremental_reference_mapping_for_existing_subsections",
            "target_subsections": subsections,
            "reference_catalog": reference_catalog,
            "response_schema": {
                "subsections": [
                    {
                        "subsection_id": "integer",
                        "selected_parts": [
                            {
                                "part_id": "integer",
                                "reason": "string",
                                "coverage": ["string"],
                            }
                        ],
                        "overall_reason": "string",
                        "uncovered_target_topics": ["string"],
                    }
                ]
            },
        }
        answer = self._run(
            payload,
            [],
            system_prompt,
            emit,
            stage=(
                "AI 当前小节参考教材批量增量映射"
                f"（{len(subsections)} 个目标）"
            ),
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict) or not isinstance(
            parsed.get("subsections"), list
        ):
            raise RuntimeError(
                "批量参考教材映射 Agent 没有返回 subsections 数组。"
            )
        return {
            "subsections": [
                dict(item)
                for item in parsed["subsections"]
                if isinstance(item, dict)
            ]
        }

    @staticmethod
    def _timestamp_seconds(value: str) -> int:
        match = re.search(r"(\d{2}):(\d{2}):(\d{2})", str(value or ""))
        if not match:
            return -1
        return (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + int(match.group(3))
        )

    @classmethod
    def _filter_timestamped_transcript(
        cls,
        transcript: str,
        start_video_time: float,
        end_video_time: float,
    ) -> str:
        """Slice timestamped ASR locally so a model never has to echo it back."""
        selected: list[str] = []
        include_block = False
        for line in str(transcript or "").splitlines():
            timestamp = cls._timestamp_seconds(line)
            if timestamp >= 0:
                include_block = (
                    float(start_video_time) - 0.5
                    <= timestamp
                    <= float(end_video_time) + 0.5
                )
            if include_block:
                selected.append(line)
        return "\n".join(selected).strip()

    @staticmethod
    def _window_index_groups(
        window_indexes: list[int],
        *,
        maximum_per_group: int,
    ) -> list[list[int]]:
        """Return stable bounded groups without changing recording order."""
        maximum = max(1, int(maximum_per_group))
        return [
            window_indexes[offset : offset + maximum]
            for offset in range(0, len(window_indexes), maximum)
        ]

    @staticmethod
    def _normalize_bounded_outline_segments(
        segments: list[dict[str, Any]],
        outline_context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        """Give independently generated window outlines stable local numbers."""
        existing_by_id = {
            int(item.get("subsection_id") or 0): dict(item)
            for item in outline_context.get("existing_subsections") or []
            if isinstance(item, dict) and int(item.get("subsection_id") or 0) > 0
        }
        title_by_number: dict[tuple[int, int, int], str] = {}
        number_by_title: dict[tuple[int, int, str], int] = {}
        id_by_title: dict[tuple[int, int, str], int] = {}
        for item in existing_by_id.values():
            chapter = int(item.get("chapter_number") or 0)
            section = int(item.get("section_number") or 0)
            subsection = int(item.get("subsection_number") or 0)
            title = str(item.get("subsection_title") or "").strip()
            if min(chapter, section, subsection) <= 0 or not title:
                continue
            title_by_number[(chapter, section, subsection)] = title
            number_by_title[(chapter, section, title)] = subsection
            id_by_title[(chapter, section, title)] = int(item["subsection_id"])

        normalized: list[dict[str, Any]] = []
        repairs = 0
        for raw in segments:
            item = dict(raw)
            chapter = max(1, int(item.get("chapter_number") or 0))
            section = max(1, int(item.get("section_number") or 0))
            title = str(item.get("subsection_title") or "").strip()
            existing_id = int(item.get("existing_subsection_id") or 0)
            established = existing_by_id.get(existing_id)
            if established is not None:
                chapter = int(established.get("chapter_number") or chapter)
                section = int(established.get("section_number") or section)
                title = str(established.get("subsection_title") or title).strip()
                item["chapter_number"] = chapter
                item["section_number"] = section
                item["subsection_title"] = title
                item["assignment"] = "existing"
            title_key = (chapter, section, title)
            known_number = number_by_title.get(title_key)
            requested_number = max(1, int(item.get("subsection_number") or 0))
            if known_number is not None:
                subsection = known_number
            elif (
                (chapter, section, requested_number) not in title_by_number
                or title_by_number[(chapter, section, requested_number)] == title
            ):
                subsection = requested_number
            else:
                subsection = max(
                    [
                        number
                        for (known_chapter, known_section, number) in title_by_number
                        if known_chapter == chapter and known_section == section
                    ]
                    or [0]
                ) + 1
                repairs += 1
            title_by_number[(chapter, section, subsection)] = title
            number_by_title[title_key] = subsection
            canonical_id = id_by_title.get(title_key, existing_id)
            if canonical_id:
                item["existing_subsection_id"] = canonical_id
                item["assignment"] = "existing"
            item["chapter_number"] = chapter
            item["section_number"] = section
            item["subsection_number"] = subsection
            normalized.append(item)
        return normalized, repairs

    def _process_authoritative_recording_windows(
        self,
        course_info: dict[str, Any],
        recording_sessions: list[dict[str, Any]],
        board_frames: list[dict[str, Any]],
        emit: Callable[[str], None],
        *,
        outline_request: dict[str, Any] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
        """Join image-verified recording windows without resending their images."""
        session_ids = {
            str(item.get("session_id") or "") for item in recording_sessions
        }
        targeted_review_cache_paths = {
            str(item.get("session_id") or ""): Path(cache_path)
            for item in recording_sessions
            if (
                cache_path := str(
                    dict(item.get("recording_period_window_manifest") or {}).get(
                        "targeted_review_cache_path"
                    )
                    or ""
                ).strip()
            )
        }
        frames_by_session: dict[str, list[dict[str, Any]]] = {
            session_id: [] for session_id in session_ids
        }
        for frame in board_frames:
            session_id = str(frame.get("session_id") or "")
            if session_id not in frames_by_session:
                return {}, [], f"candidate frame belongs to unknown session {session_id}"
            frames_by_session[session_id].append(dict(frame))

        payload_sessions: list[dict[str, Any]] = []
        expected_windows: dict[str, list[int]] = {}
        full_transcripts_by_session: dict[str, str] = {}
        window_notes_by_session: dict[str, list[dict[str, Any]]] = {}
        draft_window_blocks_by_session: dict[str, list[tuple[int, str]]] = {}
        for session in recording_sessions:
            session_id = str(session.get("session_id") or "")
            manifest = dict(
                session.get("recording_period_window_manifest") or {}
            )
            windows = [
                dict(item)
                for item in session.get("recording_period_window_notes") or []
                if isinstance(item, dict)
            ]
            for window in windows:
                window["diagram_materials"] = _normalize_window_diagram_materials(
                    window.get("diagram_materials", []),
                    session_id=session_id,
                    window_index=int(window.get("window_index") or 0),
                    start_video_time=float(window.get("start_video_time") or 0.0),
                    end_video_time=float(window.get("end_video_time") or 0.0),
                )
            cache_path = targeted_review_cache_paths.get(session_id)
            if cache_path is not None:
                try:
                    cached_payload = json.loads(
                        cache_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    cached_payload = {}
                cached_by_window: dict[int, dict[str, Any]] = {}
                for cached in dict(cached_payload.get("entries") or {}).values():
                    if not isinstance(cached, dict):
                        continue
                    window_index = int(cached.get("window_index") or 0)
                    if window_index <= 0 or str(
                        cached.get("evidence_status") or ""
                    ) not in {"complete", "source_error_corrected"}:
                        continue
                    previous = cached_by_window.get(window_index)
                    if previous is None or float(
                        cached.get("cached_at_unix") or 0.0
                    ) >= float(previous.get("cached_at_unix") or 0.0):
                        cached_by_window[window_index] = dict(cached)
                for window in windows:
                    cached = cached_by_window.get(
                        int(window.get("window_index") or 0)
                    )
                    if cached is None:
                        continue
                    cached_markdown = str(
                        cached.get("mathematical_content_markdown") or ""
                    ).strip()
                    if not cached_markdown:
                        continue
                    window["mathematical_content_markdown"] = cached_markdown
                    window["targeted_review"] = {
                        "completed": True,
                        "source_error_corrected": str(
                            cached.get("evidence_status") or ""
                        )
                        == "source_error_corrected",
                        "source_error": str(cached.get("source_error") or ""),
                        "correction": str(cached.get("correction") or ""),
                        "reused_from_cache": True,
                    }
                    window["evidence_status"] = "complete"
            expected = sorted(
                int(item.get("window_index") or 0)
                for item in windows
                if int(item.get("window_index") or 0) > 0
            )
            if (
                not bool(manifest.get("finalized"))
                or not windows
                or expected
                != list(range(1, int(manifest.get("window_count") or 0) + 1))
                or not bool(manifest.get("candidate_ranges_are_complete"))
            ):
                return (
                    {},
                    [],
                    f"recording session {session_id} has incomplete recording windows",
                )
            if any(
                str(item.get("evidence_status") or "")
                not in {"complete", "needs_review", "transcript_fallback"}
                or (
                    str(item.get("evidence_status") or "")
                    != "transcript_fallback"
                    and not str(
                        item.get("mathematical_content_markdown") or ""
                    ).strip()
                )
                for item in windows
            ):
                return (
                    {},
                    [],
                    f"recording session {session_id} contains an invalid evidence window",
                )
            expected_windows[session_id] = expected
            window_notes_by_session[session_id] = windows
            full_transcripts_by_session[session_id] = str(
                session.get("timestamped_audio_transcript") or ""
            )
            prior_reference = dict(session.get("prior_locked_reference") or {})
            draft_parts: list[str] = []
            for window in windows:
                window_index = int(window.get("window_index") or 0)
                start_seconds = max(
                    0, int(float(window.get("start_video_time") or 0.0))
                )
                timestamp = (
                    f"{start_seconds // 3600:02d}:"
                    f"{(start_seconds % 3600) // 60:02d}:"
                    f"{start_seconds % 60:02d}"
                )
                note = str(
                    window.get("mathematical_content_markdown") or ""
                ).strip()
                if not note:
                    note = (
                        f"【待核对：窗口 {window_index} 的子 Agent 未返回数学笔记；"
                        "仅依据本窗口时间范围内的完整转写补齐。】"
                    )
                draft_parts.append(f"## {timestamp}\n\n{note}")
            draft_window_blocks_by_session[session_id] = [
                (int(window.get("window_index") or 0), draft_part)
                for window, draft_part in zip(windows, draft_parts)
            ]
            draft_markdown = "\n\n".join(draft_parts).strip()
            payload_sessions.append(
                {
                    "session_id": session_id,
                    "session_order": int(session.get("session_order") or 0),
                    "captured_start_video_time": float(
                        session.get("captured_start_video_time") or 0
                    ),
                    "captured_end_video_time": float(
                        session.get("captured_end_video_time") or 0
                    ),
                    "program_context_start_video_time": float(
                        session.get("program_context_start_video_time") or 0
                    ),
                    "program_new_content_start_video_time": float(
                        session.get("program_new_content_start_video_time") or 0
                    ),
                    "timestamped_audio_transcript": str(
                        session.get("timestamped_audio_transcript") or ""
                    ),
                    "draft_mathematical_content_markdown": draft_markdown,
                    "recording_windows": [
                        {
                            "window_index": int(
                                window.get("window_index") or 0
                            ),
                            "start_video_time": float(
                                window.get("start_video_time") or 0.0
                            ),
                            "end_video_time": float(
                                window.get("end_video_time") or 0.0
                            ),
                            "evidence_status": str(
                                window.get("evidence_status") or ""
                            ),
                            "fallback_reason": str(
                                window.get("fallback_reason") or ""
                            ),
                            "diagram_materials": list(
                                window.get("diagram_materials") or []
                            ),
                        }
                        for window in windows
                    ],
                    "prior_locked_reference": {
                        "session_id": str(prior_reference.get("session_id") or ""),
                        "session_order": int(
                            prior_reference.get("session_order") or 0
                        ),
                        "timestamped_audio_transcript": str(
                            prior_reference.get("timestamped_audio_transcript") or ""
                        ),
                        "mathematical_content_markdown": str(
                            prior_reference.get("mathematical_content_markdown") or ""
                        ),
                        "program_time_boundary": dict(
                            prior_reference.get("program_time_boundary") or {}
                        ),
                    },
                }
            )

        response_schema = {
            "schema_version": self.RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION,
            "sessions": [
                {
                    "session_id": "string",
                    "covered_window_indexes": ["integer"],
                    "writing_materials": [BLOCK_SCHEMA],
                    "replacements": [
                        {
                            "window_index": (
                                "integer; window containing old; required when old is not "
                                "globally unique"
                            ),
                            "old": "exact substring copied from draft",
                            "new": "corrected replacement; may be empty",
                        }
                    ],
                    "additional_markdown": (
                        "string; only transcript-supported mathematics missing from draft"
                    ),
                    "diagram_decisions": [
                        {
                            "diagram_id": "exact locked candidate diagram_id",
                            "decision": "include | omit",
                            "reason": (
                                "specific mathematical comprehension need, or why prose/formulas suffice"
                            ),
                            "placement_time": "HH:MM:SS when included; empty when omitted",
                            "title": "English figure title when included; empty when omitted",
                            "description": (
                                "precise English acceptance specification when included; empty when omitted"
                            ),
                            "backend": "tikz when included; empty when omitted",
                            "source": "one complete body-local tikzpicture when included; empty when omitted",
                        }
                    ],
                    "source_errors": [
                        {
                            "time": "HH:MM:SS",
                            "window_index": "integer",
                            "source_statement": "string",
                            "mathematical_problem": "string",
                            "correction": "string",
                        }
                    ],
                    "proof_obligation_resolutions": [
                        {
                            "marker": "exact 【待补证明：...】 marker from the draft",
                            "statement": "string",
                            "hypotheses": ["string"],
                            "quantifier_scope": "string",
                            "status": "unproved | sketched | completed_later",
                            "introduced_at": "HH:MM:SS",
                            "completed_at": "HH:MM:SS or empty",
                            "source_reference": "precise later heading or evidence description",
                        }
                    ],
                    "uncertainties": [
                        {
                            "time": "HH:MM:SS",
                            "window_index": "integer",
                            "kind": "string",
                            "detail": "string",
                            "materially_affects_mathematics": "boolean",
                            "resolution_status": (
                                "unresolved | source_error_corrected | not_material"
                            ),
                        }
                    ],
                }
            ],
        }
        system_prompt = (
            "You are the sole final whole-lecture mathematical editor for a recorded mathematics lecture. "
            "Return ONLY JSON matching response_schema. Child agents locked evidence frames and "
            "source-free candidate relationships; images are not attached. Each session contains a complete timestamped transcript, "
            "a locally merged draft of all child notes, and compact window metadata. Check "
            "continuity and transcript completeness once. Do NOT rewrite or repeat the full draft. "
            "Return only minimal exact-substring replacements plus additional_markdown for genuinely "
            "missing transcript-supported mathematics. Each replacement.old must be copied exactly "
            "from the supplied draft. Include replacement.window_index for the window containing that "
            "exact occurrence. If identical old text is corrected in several windows, return one row "
            "per occurrence in chronological window order and use each row's distinct window_index. "
            "Preserve definitions, hypotheses, quantifiers, formulas, proof "
            "steps, examples, exercises, and corrections. Audit every `【待补证明：...】` marker against the entire "
            "later session evidence. Return one proof_obligation_resolutions row for every marker. Use "
            "`completed_later` only when the later evidence supplies a complete proof with all hypotheses and the same "
            "quantifier scope; remove that earlier marker through an exact replacement while preserving the later proof. "
            "Use `sketched` when later evidence gives only part of the argument, and `unproved` when it gives none; in "
            "both cases preserve the exact marker. Never silently weaken an arbitrary indexed chain to a countable "
            "sequence, and never invent an omitted proof. If evidence remains ambiguous, put `【待核对：...】` in additional_markdown "
            "and record it in uncertainties. covered_window_indexes must list every window exactly "
            "once. Use prior_locked_reference only for continuity. Return source_errors only for a "
            "demonstrable source error with one unique correction. The application applies your small "
            "patch locally to the locked draft; this is the only final content Agent call. You have no directory "
            "authority: do not propose, infer, label, split, merge, number, or return Chapters, Sections, "
            "Subsections, outline segments, or time boundaries. A separate directory Agent receives the complete "
            "final mathematical record only after this content reconstruction has finished. Each recording window "
            "also contains diagram_materials generated and locked by its child Agent. These are candidates, not "
            "mandatory lecture figures. After understanding the complete supplied mathematics, return exactly one "
            "diagram_decisions row for every candidate. Use include only when omitting the picture would materially "
            "impair understanding of a specific spatial, geometric, incidence, quotient, gluing, or multi-map "
            "relationship and prose plus displayed formulas cannot communicate it comparably clearly. A picture "
            "being drawable, appearing on the board or in a textbook, looking attractive, repeating a mentioned "
            "object, or being potentially useful is never sufficient. Prefer omit; zero included figures is fully "
            "valid. When several candidates serve the same mathematical purpose, omit the redundant ones, but do "
            "not include even one unless the independent necessity test passes. "
            "`previously_locked_episode_mathematical_record` and "
            "`previously_published_episode_diagrams`, when supplied, are part of the whole-episode context: omit a new "
            "candidate if an already published figure or the accumulated prose/formulas already serves its purpose. "
            "Do not recreate a previously published relationship with different labels or styling. For include, name the "
            "exact comprehension barrier and placement_time must identify where the argument needs it. Only after "
            "making that include decision, provide an English title, an English acceptance description, backend "
            "exactly `tikz`, and one complete body-local tikzpicture satisfying the permanent drawing contract. For omit, "
            "reason must state how prose/formulas already suffice; placement_time, title, description, backend, and "
            "source must all be empty. Do not render or claim visual-quality approval. Do not return diagram source "
            "in replacements or additional_markdown. "
            "\n\nMANDATORY PERMANENT LATEX DRAWING CONTRACT:\n"
            + _latex_drawing_contract()
        )
        if self._english_course(course_info):
            system_prompt = (
                "You are the sole final evidence editor for a recorded English-language course. Return ONLY JSON matching "
                "response_schema. Child windows already locked bounded evidence; no images are attached. Each session contains the "
                "complete timestamped transcript and a locally merged draft. Check chronology, continuity, and transcript coverage "
                "once, then return only minimal exact-substring replacements plus `additional_markdown` for genuinely missing, "
                "source-supported language-teaching content. Preserve quoted English examples exactly, especially intentional errors; "
                "keep corrections separate. Preserve grammar rules with restrictions and exceptions, sentence-pattern analyses, usage, "
                "collocations, word formation and etymology only when supported, pronunciation, exercises and learner answers, reading "
                "strategies, and writing advice. Chinese teacher explanations may remain in this evidence record; the later authoring "
                "contract, not this evidence pass, controls final English-first LaTeX. Never invent content or attribute terminology to "
                "Xuan Yu-You without evidence. The field name `draft_mathematical_content_markdown` is legacy infrastructure only. "
                "Do not perform mathematical proof, theorem, dependency, hypothesis, or continuity auditing. Return "
                "`proof_obligation_resolutions` and `diagram_decisions` as empty arrays. Use `source_errors` only for a demonstrable "
                "misstatement with a uniquely supported correction; use `uncertainties` for unresolved evidence. List every window "
                "exactly once in `covered_window_indexes`. You have no outline authority."
            )
        # This is part of the existing final response, never an additional
        # real-time/window call. English courses retain their existing route.
        if not self._english_course(course_info):
            system_prompt += "\n\n" + EDITOR_CONTRACT
        mathematical_only_system_prompt = (
            system_prompt.partition(
                " Each recording window also contains diagram_materials"
            )[0].rstrip()
            + " No diagram candidates are assigned to this bounded text part; "
            "return diagram_decisions as an empty array."
            + ("\n\n" + EDITOR_CONTRACT if not self._english_course(course_info) else "")
        )
        synthesis_payload = {
            **course_info,
            "workflow": "authoritative_recording_window_synthesis",
            "response_schema": response_schema,
            "recording_sessions": payload_sessions,
        }
        request_profile = getattr(self, "request_profile", None)
        synthesis_invocation_contract = {
            "model": str(
                getattr(request_profile, "model", self.REQUIRED_MODEL)
                or self.REQUIRED_MODEL
            ),
            "provider_kind": str(
                getattr(request_profile, "provider_kind", "openai_responses")
                or "openai_responses"
            ),
            "reasoning_effort": self.FINAL_SYNTHESIS_REASONING_EFFORT,
            "stream_responses": False,
            "timeout_seconds": int(
                getattr(request_profile, "timeout_seconds", 600) or 600
            ),
            "transport_retries": int(
                getattr(request_profile, "transport_retries", 1) or 0
            ),
        }
        response_cache_path = next(
            (
                path.parent / "recording_agent_final_text_response.json"
                for _, path in sorted(targeted_review_cache_paths.items())
            ),
            None,
        )
        synthesis_signature = hashlib.sha256(
            json.dumps(
                {
                    "payload": synthesis_payload,
                    "system_prompt": system_prompt,
                    "agent_profile": str(
                        getattr(self, "profile_label", "") or ""
                    ),
                    "invocation_contract": synthesis_invocation_contract,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        def reusable_signature(document: dict[str, Any]) -> bool:
            if document.get("synthesis_signature") == synthesis_signature:
                return True
            previous = document.get("invocation_contract") or {}
            # A cheaper scheduling policy does not invalidate already validated
            # mathematics. Recompute the exact old evidence/prompt fingerprint;
            # never accept a cache merely because its model matches.
            if not isinstance(previous, dict) or any(
                previous.get(key) != synthesis_invocation_contract.get(key)
                for key in ("model", "provider_kind", "stream_responses")
            ):
                return False
            if previous.get("reasoning_effort") not in {"low", "medium", "high"}:
                return False
            old_signature = hashlib.sha256(json.dumps({
                "payload": synthesis_payload,
                "system_prompt": system_prompt,
                "agent_profile": str(getattr(self, "profile_label", "") or ""),
                "invocation_contract": previous,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            return document.get("synthesis_signature") == old_signature

        cached_answer = ""
        if response_cache_path is not None:
            try:
                cached_response = json.loads(
                    response_cache_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                cached_response = {}
            if (
                isinstance(cached_response, dict)
                and isinstance(cached_response.get("version"), int)
                and cached_response.get("version", 0) >= 6
                and reusable_signature(cached_response)
            ):
                cached_answer = str(cached_response.get("answer") or "")

        def cache_answer(answer: str) -> None:
            if response_cache_path is None:
                return
            response_cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = response_cache_path.with_suffix(
                response_cache_path.suffix + ".tmp"
            )
            temporary.write_text(
                json.dumps(
                    {
                        # Version 6 additionally binds the cache to the exact
                        # fixed model/protocol/reasoning/transport contract.
                        # Version 5 requires source-free recording candidates
                        # and generates source only inside an explicit whole-
                        # lecture include decision. Older caches must not
                        # auto-publish or reuse pre-decision drawing source.
                        "version": 6,
                        "synthesis_signature": synthesis_signature,
                        "invocation_contract": synthesis_invocation_contract,
                        "answer": answer,
                        "cached_at_unix": time.time(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, response_cache_path)

        part_cache_path = (
            response_cache_path.with_name("recording_agent_final_text_parts.json")
            if response_cache_path is not None
            else None
        )
        cached_parts: dict[str, str] = {}
        if part_cache_path is not None:
            try:
                stored_parts = json.loads(part_cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored_parts = {}
            if (
                isinstance(stored_parts, dict)
                and int(stored_parts.get("version") or 0) >= 3
                and reusable_signature(stored_parts)
                and isinstance(stored_parts.get("answers"), dict)
            ):
                cached_parts = {
                    str(key): str(value)
                    for key, value in stored_parts["answers"].items()
                    if str(key).strip() and str(value).strip()
                }

        def cache_parts() -> None:
            if part_cache_path is None:
                return
            part_cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = part_cache_path.with_suffix(part_cache_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "synthesis_signature": synthesis_signature,
                        "invocation_contract": synthesis_invocation_contract,
                        "answers": cached_parts,
                        "cached_at_unix": time.time(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, part_cache_path)

        synthesis_parts: list[dict[str, Any]] = []
        synthesis_sessions_by_id = {
            str(item.get("session_id") or ""): item for item in payload_sessions
        }
        whole_lecture_locked_record = "\n\n".join(
            str(item.get("draft_mathematical_content_markdown") or "").strip()
            for item in payload_sessions
            if str(item.get("draft_mathematical_content_markdown") or "").strip()
        )
        whole_lecture_candidate_catalog = [
            dict(diagram)
            for session in payload_sessions
            for window in session.get("recording_windows") or []
            for diagram in window.get("diagram_materials") or []
            if isinstance(diagram, dict)
        ]

        def build_synthesis_part(
            payload_session: dict[str, Any], window_indexes: list[int]
        ) -> dict[str, Any]:
            session_id = str(payload_session.get("session_id") or "")
            windows_by_index = {
                int(item.get("window_index") or 0): dict(item)
                for item in payload_session.get("recording_windows") or []
                if int(item.get("window_index") or 0) > 0
            }
            notes_by_index = {
                int(item.get("window_index") or 0): dict(item)
                for item in window_notes_by_session.get(session_id, [])
                if int(item.get("window_index") or 0) > 0
            }
            selected_windows = [windows_by_index[index] for index in window_indexes]
            start_time = min(
                float(item.get("start_video_time") or 0.0)
                for item in selected_windows
            )
            end_time = max(
                float(item.get("end_video_time") or start_time)
                for item in selected_windows
            )
            draft_parts: list[str] = []
            for index in window_indexes:
                note = str(
                    notes_by_index.get(index, {}).get(
                        "mathematical_content_markdown"
                    )
                    or ""
                ).strip()
                if not note:
                    note = (
                        f"【待核对：窗口 {index} 的子 Agent 未返回数学笔记；"
                        "仅依据锁定转写补齐。】"
                    )
                seconds = max(
                    0,
                    int(float(windows_by_index[index].get("start_video_time") or 0)),
                )
                draft_parts.append(
                    f"## {seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}\n\n{note}"
                )
            part_session = dict(payload_session)
            part_session["writing_owned_range"] = {
                "start_video_time": (
                    float(payload_session.get("program_new_content_start_video_time") or
                          payload_session.get("captured_start_video_time") or 0)
                    if window_indexes[0] == min(windows_by_index) else start_time
                ),
                "end_video_time": (
                    float(payload_session.get("captured_end_video_time") or end_time)
                    if window_indexes[-1] == max(windows_by_index) else end_time
                ),
            }
            part_session["recording_windows"] = selected_windows
            part_session["timestamped_audio_transcript"] = (
                self._filter_timestamped_transcript(
                    full_transcripts_by_session.get(session_id, ""),
                    max(0.0, start_time - 30.0),
                    end_time,
                )
            )
            part_session["draft_mathematical_content_markdown"] = "\n\n".join(
                draft_parts
            ).strip()
            part_candidates = [
                dict(diagram)
                for window in selected_windows
                for diagram in window.get("diagram_materials") or []
                if isinstance(diagram, dict)
            ]
            if part_candidates:
                # Text patching is relay-bounded, but figure necessity is not a
                # local-window judgment. Give every candidate-bearing part the
                # complete locked mathematics and the full candidate catalog so
                # it can reject globally redundant or prose-sufficient figures.
                part_session["whole_lecture_diagram_review_context"] = {
                    "complete_locked_mathematical_record": whole_lecture_locked_record,
                    "all_candidate_relationships": whole_lecture_candidate_catalog,
                    "decision_scope_diagram_ids": [
                        str(item.get("diagram_id") or "")
                        for item in part_candidates
                        if str(item.get("diagram_id") or "")
                    ],
                }
            part_schema = dict(response_schema)
            if not part_candidates:
                # No figure can be returned here: do not send its large TikZ schema.
                row_schema = dict(response_schema["sessions"][0])
                row_schema["diagram_decisions"] = []
                part_schema["sessions"] = [row_schema]
            if window_indexes[0] != min(windows_by_index):
                # The leading continuity context is in the bounded ASR slice;
                # avoid repeatedly sending earlier full-session material.
                part_session["prior_locked_reference"] = {}
            part_key = f"{session_id}:{','.join(str(item) for item in window_indexes)}"
            return {
                "key": part_key,
                "session_id": session_id,
                "window_indexes": window_indexes,
                "draft_window_blocks": [
                    (index, draft_part)
                    for index, draft_part in zip(window_indexes, draft_parts)
                ],
                "payload": {
                    **synthesis_payload,
                    "response_schema": part_schema,
                    "recording_sessions": [part_session],
                },
            }

        def without_writing_materials(part: dict[str, Any]) -> dict[str, Any]:
            """The same bounded part, asking only for the pre-existing patch output.

            Writer materials are an optional derivative of this response; when
            they make a single window exceed the output budget, the locked
            evidence must still be patched exactly as before.
            """
            payload = dict(part["payload"])
            schema = dict(payload["response_schema"])
            row_schema = dict(schema["sessions"][0])
            row_schema.pop("writing_materials", None)
            schema["sessions"] = [row_schema]
            payload["response_schema"] = schema
            payload["recording_sessions"] = [
                {key: value for key, value in dict(session).items() if key != "writing_owned_range"}
                for session in payload.get("recording_sessions") or []
            ]
            return {**part, "key": str(part["key"]) + ":without_writing_materials",
                    "payload": payload, "omit_writing_materials": True}

        def system_prompt_for_part(part: dict[str, Any]) -> str:
            if part.get("omit_writing_materials"):
                return (
                    system_prompt_for_part({**part, "omit_writing_materials": False})
                    .replace("\n\n" + EDITOR_CONTRACT, "")
                    + "\n\nReturn writing_materials as an empty array for this part."
                )
            sessions = (part.get("payload") or {}).get("recording_sessions") or []
            has_diagram_scope = any(
                bool(
                    (
                        session.get("whole_lecture_diagram_review_context") or {}
                    ).get("decision_scope_diagram_ids")
                )
                for session in sessions
                if isinstance(session, dict)
            )
            return system_prompt if has_diagram_scope else mathematical_only_system_prompt

        total_window_count = sum(
            len(item.get("recording_windows") or []) for item in payload_sessions
        )
        if total_window_count > self.FINAL_SYNTHESIS_WINDOWS_PER_REQUEST:
            for payload_session in payload_sessions:
                session_id = str(payload_session.get("session_id") or "")
                windows_by_index = {
                    int(item.get("window_index") or 0): dict(item)
                    for item in payload_session.get("recording_windows") or []
                    if int(item.get("window_index") or 0) > 0
                }
                for window_indexes in (
                    self._window_index_groups(
                        sorted(windows_by_index),
                        maximum_per_group=self.FINAL_SYNTHESIS_WINDOWS_PER_REQUEST,
                    )
                ):
                    synthesis_parts.append(
                        build_synthesis_part(payload_session, window_indexes)
                    )

        def whitespace_equivalent_spans(
            source: str,
            target: str,
        ) -> list[tuple[int, int]]:
            compact_target = "".join(
                character for character in target if not character.isspace()
            )
            if not compact_target:
                return []
            compact_source_characters: list[str] = []
            source_indexes: list[int] = []
            for index, character in enumerate(source):
                if character.isspace():
                    continue
                compact_source_characters.append(character)
                source_indexes.append(index)
            compact_source = "".join(compact_source_characters)
            spans: list[tuple[int, int]] = []
            search_start = 0
            while True:
                match_start = compact_source.find(compact_target, search_start)
                if match_start < 0:
                    return spans
                match_end = match_start + len(compact_target)
                spans.append(
                    (
                        source_indexes[match_start],
                        source_indexes[match_end - 1] + 1,
                    )
                )
                search_start = match_start + 1

        def repair_overescaped_latex_commands(value: str) -> str:
            value = re.sub(r"\x08(?=[A-Za-z])", r"\\b", value)
            value = re.sub(r"\x0c(?=[A-Za-z])", r"\\f", value)
            value = re.sub(r"\t(?=[A-Za-z])", r"\\t", value)
            value = re.sub(
                r"(?<=[A-Za-z0-9_{}])\n(?=(?:e|eq|otin|abla|u)(?:\s|[_^{}()]))",
                r"\\n",
                value,
            )
            value = re.sub(
                r"\\\\(?=[A-Za-z;,:!%#_$&{}\[\]()])",
                r"\\",
                value,
            )
            # A model occasionally copies a display-math replacement with a
            # valid standalone ``\[`` opener but drops the backslash from the
            # standalone closing ``\]``. Repair only that stateful pair. A
            # normal Markdown line containing ``]`` outside an open display is
            # preserved byte-for-byte.
            repaired_lines: list[str] = []
            display_math_open = False
            for line in value.splitlines(keepends=True):
                content = line.rstrip("\r\n")
                line_ending = line[len(content) :]
                stripped = content.strip()
                if stripped == r"\[":
                    display_math_open = True
                elif stripped == r"\]":
                    display_math_open = False
                elif stripped == "]" and display_math_open:
                    indentation = content[: len(content) - len(content.lstrip())]
                    line = indentation + r"\]" + line_ending
                    display_math_open = False
                repaired_lines.append(line)
            return "".join(repaired_lines)

        def apply_synthesis_replacements(
            markdown: str,
            replacements: Any,
            *,
            session_id: str,
            window_blocks: list[tuple[int, str]] | None = None,
            unmatched_targets: list[dict[str, Any]] | None = None,
        ) -> str:
            normalized: list[dict[str, Any]] = []
            for replacement in replacements or []:
                if not isinstance(replacement, dict):
                    continue
                old = str(replacement.get("old") or "")
                if not old:
                    continue
                raw_window_index = replacement.get("window_index")
                window_index: int | None = None
                if raw_window_index not in (None, ""):
                    try:
                        window_index = int(raw_window_index)
                    except (TypeError, ValueError) as error:
                        raise RuntimeError(
                            "window synthesis correction returned an invalid window_index "
                            f"for session {session_id}: {raw_window_index!r}"
                        ) from error
                    if window_index <= 0:
                        raise RuntimeError(
                            "window synthesis correction returned an invalid window_index "
                            f"for session {session_id}: {raw_window_index!r}"
                        )
                normalized.append(
                    {
                        "old": old,
                        "new": str(replacement.get("new") or ""),
                        "window_index": window_index,
                    }
                )

            def exact_spans(source: str, target: str) -> list[tuple[int, int]]:
                spans: list[tuple[int, int]] = []
                search_start = 0
                while True:
                    match_start = source.find(target, search_start)
                    if match_start < 0:
                        return spans
                    match_end = match_start + len(target)
                    spans.append((match_start, match_end))
                    search_start = match_end

            def replace_spans(
                source: str,
                spans: list[tuple[int, int]],
                replacement_rows: list[dict[str, Any]],
                *,
                repair_new: bool = False,
            ) -> str:
                if len(spans) != len(replacement_rows):
                    raise RuntimeError("internal window synthesis replacement mismatch")
                pieces: list[str] = []
                cursor = 0
                for (start, end), replacement_row in zip(spans, replacement_rows):
                    if start < cursor or end < start:
                        raise RuntimeError(
                            "window synthesis returned overlapping correction targets "
                            f"for session {session_id}"
                        )
                    new = str(replacement_row["new"])
                    if repair_new:
                        new = repair_overescaped_latex_commands(new)
                    pieces.extend((source[cursor:start], new))
                    cursor = end
                pieces.append(source[cursor:])
                return "".join(pieces)

            def already_applied(
                source: str,
                replacement_rows: list[dict[str, Any]],
                *,
                repair_new: bool = False,
            ) -> bool:
                new_counts: dict[str, int] = {}
                for replacement_row in replacement_rows:
                    new = str(replacement_row["new"])
                    if repair_new:
                        new = repair_overescaped_latex_commands(new)
                    if not new:
                        return False
                    new_counts[new] = new_counts.get(new, 0) + 1
                return all(
                    len(whitespace_equivalent_spans(source, new)) == count
                    for new, count in new_counts.items()
                )

            def apply_unscoped(
                source: str,
                replacement_rows: list[dict[str, Any]],
                *, defer_unmatched: bool = False,
            ) -> str:
                source = repair_overescaped_latex_commands(source)

                def timestamped_paragraph_spans(
                    current_source: str,
                    old_text: str,
                    new_text: str,
                    rows: list[dict[str, Any]],
                ) -> list[tuple[int, int]]:
                    """Recover one uniquely identified Markdown paragraph.

                    Models occasionally copy a LaTeX paragraph with a damaged
                    delimiter (for example ``\\((U,\\varphi)\\in`` becomes
                    ``\\(U,\\varphi)\\in``). When the response supplies a
                    window anchor and both texts carry the same timestamp, a
                    unique timestamped paragraph plus a high lexical-overlap
                    check is safer than rejecting the entire multi-window
                    synthesis. This is deliberately narrower than fuzzy global
                    replacement and never applies to unanchored rows.
                    """
                    if not rows or any(
                        int(item.get("window_index") or 0) <= 0 for item in rows
                    ):
                        return []
                    old_time = re.search(
                        r"\[(\d{2}:\d{2}:\d{2})\]", old_text
                    )
                    new_time = re.search(
                        r"\[(\d{2}:\d{2}:\d{2})\]", new_text
                    )
                    if not old_time or not new_time or old_time.group(1) != new_time.group(1):
                        return []
                    timestamp = re.escape(old_time.group(1))
                    heading_pattern = re.compile(
                        rf"(?m)^\s*(?:\*\*\[{timestamp}\][^\n]*?\*\*|#+\s+[^\n]*?{timestamp}[^\n]*)"
                    )

                    def lexical_tokens(value: str) -> list[str]:
                        return re.findall(r"[a-z0-9]+", value.lower())

                    old_tokens = lexical_tokens(old_text)
                    candidates: list[tuple[int, int]] = []
                    for heading in heading_pattern.finditer(current_source):
                        start = heading.start()
                        end = current_source.find("\n\n", heading.end())
                        if end < 0:
                            end = len(current_source)
                        candidate = current_source[start:end]
                        candidate_tokens = lexical_tokens(candidate)
                        if not old_tokens or not candidate_tokens:
                            continue
                        score = SequenceMatcher(
                            None, old_tokens, candidate_tokens
                        ).ratio()
                        if score >= 0.72:
                            candidates.append((start, end))
                    return candidates

                grouped: dict[str, list[dict[str, Any]]] = {}
                for replacement_row in replacement_rows:
                    grouped.setdefault(str(replacement_row["old"]), []).append(
                        replacement_row
                    )
                for old, group in grouped.items():
                    expected_count = len(group)
                    occurrence_spans = exact_spans(source, old)
                    whitespace_spans: list[tuple[int, int]] = []
                    repaired_old = repair_overescaped_latex_commands(old)
                    repaired_spans: list[tuple[int, int]] = []
                    repaired_whitespace_spans: list[tuple[int, int]] = []
                    if len(occurrence_spans) == expected_count:
                        source = replace_spans(source, occurrence_spans, group)
                        continue
                    if not occurrence_spans:
                        whitespace_spans = whitespace_equivalent_spans(source, old)
                        if len(whitespace_spans) == expected_count:
                            source = replace_spans(source, whitespace_spans, group)
                            continue
                        if repaired_old != old:
                            repaired_spans = exact_spans(source, repaired_old)
                            if len(repaired_spans) == expected_count:
                                source = replace_spans(
                                    source,
                                    repaired_spans,
                                    group,
                                    repair_new=True,
                                )
                                continue
                            if not repaired_spans:
                                repaired_whitespace_spans = whitespace_equivalent_spans(
                                    source,
                                    repaired_old,
                                )
                                if len(repaired_whitespace_spans) == expected_count:
                                    source = replace_spans(
                                        source,
                                        repaired_whitespace_spans,
                                        group,
                                        repair_new=True,
                                    )
                                    continue
                        timestamped_spans = timestamped_paragraph_spans(
                            source,
                            old,
                            str(group[0].get("new") or ""),
                            group,
                        )
                        if len(timestamped_spans) == expected_count:
                            emit(
                                "结束阶段精确补丁文本存在 LaTeX 分隔符格式漂移；"
                                "已按同一窗口与唯一时间戳段落安全锚定修复。"
                            )
                            source = replace_spans(
                                source,
                                timestamped_spans,
                                group,
                                repair_new=True,
                            )
                            continue
                        if already_applied(source, group) or (
                            repaired_old != old
                            and already_applied(source, group, repair_new=True)
                        ):
                            continue
                        # Proof-obligation markers are intentionally removable
                        # corrections.  A cached/duplicated final response can
                        # legitimately repeat a deletion after an earlier local
                        # replay already removed the marker.  Treat that exact
                        # idempotent deletion as a no-op; keep every other
                        # absent target fail-closed so a misplaced mathematical
                        # correction still cannot be guessed or billed again.
                        if (
                            not any(str(item.get("new") or "") for item in group)
                            and all(
                                str(item.get("old") or "").startswith("【待补证明：")
                                and str(item.get("old") or "").endswith("】")
                                for item in group
                            )
                        ):
                            emit(
                                "结束阶段证明标记删除已在锁定稿中完成；"
                                "跳过重复删除，避免无效重试。"
                            )
                            continue
                        transcript_target = repaired_old if repaired_old != old else old
                        transcript_spans = whitespace_equivalent_spans(
                            full_transcripts_by_session.get(session_id, ""),
                            transcript_target,
                        )
                        if len(transcript_spans) == expected_count:
                            emit(
                                "窗口合成返回了仅命中锁定转写、未命中数学草稿的修正；"
                                "已保留原始转写并跳过该非草稿补丁。"
                            )
                            continue
                    replacement_count = sum(
                        len(whitespace_equivalent_spans(source, new))
                        for new in {str(item["new"]) for item in group}
                        if new
                    )
                    if unmatched_targets is not None and not defer_unmatched:
                        unmatched_targets.append(
                            {
                                "old": old,
                                "window_indexes": sorted(
                                    {
                                        int(item.get("window_index") or 0)
                                        for item in group
                                        if int(item.get("window_index") or 0) > 0
                                    }
                                ),
                            }
                        )
                        emit(
                            "结束阶段多窗口响应包含无法唯一定位到锁定稿的修正；"
                            "已保留原文并记录待核对，不再拆分重问同一数学内容。"
                        )
                        continue
                    raise RuntimeError(
                        "window synthesis correction target must occur exactly once "
                        "or match the number of ordered corrections "
                        f"for session {session_id}; found {len(occurrence_spans)}; "
                        f"corrections for target {expected_count}; "
                        f"replacement text found {replacement_count}; "
                        f"whitespace-equivalent target found {len(whitespace_spans)}; "
                        "whitespace-equivalent repaired target found "
                        f"{len(repaired_whitespace_spans)}: {old[:240]}"
                    )
                return source

            markdown = repair_overescaped_latex_commands(markdown)
            repaired_blocks = [
                (int(window_index), repair_overescaped_latex_commands(block))
                for window_index, block in window_blocks or []
            ]
            known_indexes = {window_index for window_index, _ in repaired_blocks}
            for replacement_row in normalized:
                window_index = replacement_row["window_index"]
                if (
                    window_index is not None
                    and repaired_blocks
                    and window_index not in known_indexes
                ):
                    raise RuntimeError(
                        "window synthesis correction referenced a window outside its "
                        f"assigned draft for session {session_id}: {window_index}"
                    )
            reconstructed = "\n\n".join(block for _, block in repaired_blocks).strip()
            if repaired_blocks and reconstructed == markdown:
                anchored_by_window: dict[int, list[dict[str, Any]]] = {}
                unscoped: list[dict[str, Any]] = []
                for replacement_row in normalized:
                    window_index = replacement_row["window_index"]
                    if window_index is None:
                        unscoped.append(replacement_row)
                        continue
                    anchored_by_window.setdefault(window_index, []).append(
                        replacement_row
                    )
                deferred_anchored: list[dict[str, Any]] = []
                applied_blocks: list[tuple[int, str]] = []
                for window_index, block in repaired_blocks:
                    anchored_rows = anchored_by_window.get(window_index, [])
                    try:
                        applied_block = apply_unscoped(block, anchored_rows, defer_unmatched=True)
                    except RuntimeError:
                        # A model can assign a boundary paragraph to the adjacent
                        # recording window even though replacement.old was copied
                        # exactly from this bounded draft. Defer only to the whole
                        # current part, where apply_unscoped still requires a unique
                        # exact/whitespace-equivalent target and therefore remains
                        # fail-closed for absent or ambiguous text.
                        deferred_anchored.extend(anchored_rows)
                        applied_block = block
                    applied_blocks.append((window_index, applied_block))
                repaired_blocks = applied_blocks
                markdown = "\n\n".join(block for _, block in repaired_blocks).strip()
                return apply_unscoped(markdown, [*unscoped, *deferred_anchored])
            return apply_unscoped(markdown, normalized)

        def parse_synthesis(
            answer: str,
            *,
            allow_targeted_review: bool = False,
        ) -> tuple[
            dict[str, dict[str, Any]],
            dict[str, dict[str, Any]],
        ]:
            parsed = _parse_json_response(answer)
            if not isinstance(parsed, dict):
                raise RuntimeError("window synthesis response must be a JSON object")
            schema_version = parsed.get("schema_version")
            if schema_version is not None and int(schema_version) != int(
                self.RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION
            ):
                raise RuntimeError("window synthesis response has an invalid schema version")
            rows = parsed.get("sessions")
            if not isinstance(rows, list):
                raise RuntimeError("window synthesis response omitted sessions")
            results: dict[str, dict[str, Any]] = {}
            targeted_reviews: dict[str, dict[str, Any]] = {}

            def request_targeted_review(
                session_id: str,
                window_index: int,
                uncertainty: dict[str, Any],
            ) -> None:
                entry = targeted_reviews.setdefault(
                    session_id,
                    {"window_indexes": [], "uncertainties": []},
                )
                if int(window_index) not in entry["window_indexes"]:
                    entry["window_indexes"].append(int(window_index))
                    entry["window_indexes"].sort()
                detail = str(uncertainty.get("detail") or "").strip()
                if detail and not any(
                    str(item.get("detail") or "").strip() == detail
                    for item in entry["uncertainties"]
                ):
                    entry["uncertainties"].append(dict(uncertainty))

            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError("window synthesis returned a non-object session")
                session_id = str(row.get("session_id") or "")
                if session_id not in expected_windows or session_id in results:
                    raise RuntimeError(
                        f"window synthesis returned unknown or duplicate session {session_id}"
                    )
                covered = sorted(
                    {
                        int(value)
                        for value in row.get("covered_window_indexes") or []
                        if str(value).strip().isdigit()
                    }
                )
                if covered != expected_windows[session_id]:
                    raise RuntimeError(
                        f"window synthesis omitted or duplicated windows for session {session_id}"
                    )
                resolved_session = next(
                    item
                    for item in payload_sessions
                    if str(item.get("session_id") or "") == session_id
                )
                markdown = str(
                    row.get("mathematical_content_markdown")
                    or resolved_session.get(
                        "draft_mathematical_content_markdown"
                    )
                    or ""
                ).strip()
                markdown = apply_synthesis_replacements(
                    markdown,
                    row.get("replacements"),
                    session_id=session_id,
                    window_blocks=draft_window_blocks_by_session.get(session_id),
                )
                original_markers = {
                    value
                    for window in window_notes_by_session.get(session_id, [])
                    for value in re.findall(
                        r"【待补证明：.*?】",
                        str(window.get("mathematical_content_markdown") or ""),
                    )
                }
                proof_resolutions: list[dict[str, Any]] = []
                resolved_markers: set[str] = set()
                for resolution in row.get("proof_obligation_resolutions") or []:
                    if not isinstance(resolution, dict):
                        raise RuntimeError(
                            f"window synthesis returned an invalid proof resolution for {session_id}"
                        )
                    marker = str(resolution.get("marker") or "").strip()
                    status = str(resolution.get("status") or "").strip()
                    if marker not in original_markers or marker in resolved_markers:
                        raise RuntimeError(
                            f"window synthesis returned an unknown or duplicate proof marker for {session_id}"
                        )
                    if status not in {"unproved", "sketched", "completed_later"}:
                        raise RuntimeError(
                            f"window synthesis returned an invalid proof status for {session_id}"
                        )
                    completed_at = str(resolution.get("completed_at") or "").strip()
                    source_reference = str(
                        resolution.get("source_reference") or ""
                    ).strip()
                    if status == "completed_later" and (
                        not completed_at or not source_reference
                    ):
                        raise RuntimeError(
                            f"completed proof resolution lacks provenance for {session_id}"
                        )
                    if status == "completed_later" and marker in markdown:
                        markdown = markdown.replace(marker, "", 1)
                    proof_resolutions.append(
                        {
                            "marker": marker,
                            "statement": str(resolution.get("statement") or "").strip(),
                            "hypotheses": [
                                str(value).strip()
                                for value in resolution.get("hypotheses") or []
                                if str(value).strip()
                            ],
                            "quantifier_scope": str(
                                resolution.get("quantifier_scope") or ""
                            ).strip(),
                            "status": status,
                            "introduced_at": str(
                                resolution.get("introduced_at") or ""
                            ).strip(),
                            "completed_at": completed_at,
                            "source_reference": source_reference,
                        }
                    )
                    resolved_markers.add(marker)
                for marker in sorted(original_markers - resolved_markers):
                    proof_resolutions.append(
                        {
                            "marker": marker,
                            "statement": marker[len("【待补证明：") : -1].strip(),
                            "hypotheses": [],
                            "quantifier_scope": "",
                            "status": "unproved",
                            "introduced_at": "",
                            "completed_at": "",
                            "source_reference": "recording-window marker; final synthesis omitted structured status",
                        }
                    )
                additional_markdown = str(
                    row.get("additional_markdown") or ""
                ).strip()
                if additional_markdown:
                    markdown = markdown.rstrip() + "\n\n" + additional_markdown
                unresolved_issues = [
                    str(value).strip()
                    for value in dict(row.get("correctness_review") or {}).get(
                        "unresolved_issues"
                    )
                    or []
                    if str(value).strip()
                ]
                if unresolved_issues:
                    markdown = (
                        markdown.rstrip()
                        + "\n\n### 待核对\n\n"
                        + "\n".join(
                            f"- 【待核对：{issue}】" for issue in unresolved_issues
                        )
                    )
                session = next(
                    item
                    for item in recording_sessions
                    if str(item.get("session_id") or "") == session_id
                )
                transcript = self._filter_timestamped_transcript(
                    full_transcripts_by_session.get(session_id, ""),
                    float(session.get("program_new_content_start_video_time") or 0.0),
                    float(session.get("captured_end_video_time") or 0.0),
                )
                if not re.search(r"(?m)^##\s+\d{2}:\d{2}:\d{2}\b", markdown):
                    raise RuntimeError(
                        f"window synthesis returned no timestamped mathematics for {session_id}"
                    )
                if not re.search(r"(?m)^\[\d{2}:\d{2}:\d{2}\]", transcript):
                    raise RuntimeError(
                        f"window synthesis returned no timestamped transcript for {session_id}"
                    )
                for window in window_notes_by_session.get(session_id, []):
                    if not isinstance(window, dict):
                        continue
                    window_markdown = str(
                        window.get("mathematical_content_markdown") or ""
                    )
                    obligations = set(
                        re.findall(r"【待补证明：.*?】", window_markdown)
                    )
                    completed_markers = {
                        item["marker"]
                        for item in proof_resolutions
                        if item["status"] == "completed_later"
                    }
                    missing_obligations = sorted(
                        value
                        for value in obligations
                        if value not in markdown and value not in completed_markers
                    )
                    if missing_obligations:
                        # Proof obligations are exact immutable markers. Restoring a
                        # marker omitted by the prose synthesis is deterministic and
                        # does not justify another visual model request.
                        markdown = (
                            markdown.rstrip()
                            + "\n\n### Preserved proof obligations\n\n"
                            + "\n\n".join(missing_obligations)
                        )
                new_start = float(
                    session.get("program_new_content_start_video_time") or 0
                )
                # Recording boundaries are organizational hints, not a reason to
                # discard an otherwise complete lecture package.  A continuation
                # deliberately retains context before ``new_start`` and timestamps
                # are routinely rounded, delayed, or shifted when a person pauses
                # and seeks.  The transcript has already been filtered locally;
                # keep mathematical Markdown from the retained recording evidence
                # even when one of its headings crosses the nominal boundary.
                uncertainties = []
                pending_source_error_uncertainties: list[dict[str, Any]] = []
                for item in row.get("uncertainties") or []:
                    if not isinstance(item, dict):
                        continue
                    detail = str(item.get("detail") or "").strip()
                    if not detail:
                        continue
                    uncertainty = {
                        "time": str(item.get("time") or "").strip(),
                        "window_index": int(item.get("window_index") or 0),
                        "kind": str(item.get("kind") or "").strip(),
                        "detail": detail,
                        "materially_affects_mathematics": bool(
                            item.get("materially_affects_mathematics")
                        ),
                        "resolution_status": str(
                            item.get("resolution_status") or ""
                        ).strip(),
                    }
                    if uncertainty["materially_affects_mathematics"]:
                        resolved_window = next(
                            (
                                value
                                for value in payload_sessions
                                if str(value.get("session_id") or "") == session_id
                            ),
                            {},
                        )
                        corrected_windows = [
                            value
                            for value in resolved_window.get("recording_windows") or []
                            if bool(
                                dict(value.get("targeted_review") or {}).get(
                                    "source_error_corrected"
                                )
                            )
                        ]
                        uncertainty_time = self._timestamp_seconds(
                            uncertainty["time"]
                        )
                        matched_correction = next(
                            (
                                value
                                for value in corrected_windows
                                if (
                                    uncertainty["window_index"]
                                    and int(value.get("window_index") or 0)
                                    == uncertainty["window_index"]
                                )
                                or (
                                    not uncertainty["window_index"]
                                    and uncertainty_time >= 0
                                    and float(
                                        value.get("start_video_time") or 0.0
                                    )
                                    - 2.0
                                    <= uncertainty_time
                                    <= float(value.get("end_video_time") or 0.0)
                                    + 2.0
                                )
                            ),
                            None,
                        )
                        correction_is_preserved = bool(
                            matched_correction is not None
                            and "Evidence correction" in markdown
                            and uncertainty["resolution_status"]
                            == "source_error_corrected"
                        )
                        if correction_is_preserved:
                            uncertainty["materially_affects_mathematics"] = False
                            uncertainty["resolved_by_targeted_review"] = True
                        elif (
                            uncertainty["resolution_status"]
                            == "source_error_corrected"
                            and "Evidence correction" in markdown
                        ):
                            # The final text-only correctness pass may discover
                            # and rigorously correct a source error even when the
                            # earlier visual window did not request targeted
                            # review. Validate the matching structured
                            # source_errors row below before accepting it.
                            pending_source_error_uncertainties.append(uncertainty)
                    uncertainties.append(uncertainty)
                selected_indexes = sorted(
                    int(item["index"])
                    for item in frames_by_session.get(session_id, [])
                )
                source_errors: list[dict[str, Any]] = []
                for source_error in row.get("source_errors") or []:
                    if not isinstance(source_error, dict):
                        raise RuntimeError(
                            f"window synthesis returned an invalid source error for {session_id}"
                        )
                    error_time = str(source_error.get("time") or "").strip()
                    window_index = int(source_error.get("window_index") or 0)
                    source_statement = str(
                        source_error.get("source_statement") or ""
                    ).strip()
                    mathematical_problem = str(
                        source_error.get("mathematical_problem") or ""
                    ).strip()
                    correction = str(source_error.get("correction") or "").strip()
                    matching_session = next(
                        (
                            value
                            for value in payload_sessions
                            if str(value.get("session_id") or "") == session_id
                        ),
                        {},
                    )
                    session_windows = [
                        value
                        for value in matching_session.get("recording_windows") or []
                        if isinstance(value, dict)
                    ]
                    matching_window = next(
                        (
                            value
                            for value in session_windows
                            if int(value.get("window_index") or 0) == window_index
                        ),
                        None,
                    )
                    error_seconds = self._timestamp_seconds(error_time)
                    time_matching_windows = [
                        value
                        for value in session_windows
                        if error_seconds >= 0
                        and float(value.get("start_video_time") or 0.0) - 2.0
                        <= error_seconds
                        <= float(value.get("end_video_time") or 0.0) + 2.0
                    ]
                    if (
                        time_matching_windows
                        and matching_window not in time_matching_windows
                    ):
                        matching_window = min(
                            time_matching_windows,
                            key=lambda value: abs(
                                error_seconds
                                - (
                                    float(value.get("start_video_time") or 0.0)
                                    + float(value.get("end_video_time") or 0.0)
                                )
                                / 2.0
                            ),
                        )
                        window_index = int(
                            matching_window.get("window_index") or 0
                        )
                    if not source_statement or not mathematical_problem or not correction:
                        # An incomplete optional correction must not invalidate all
                        # completed recording evidence.
                        continue
                    if matching_window is None and session_windows:
                        # Prefer the declared window when it exists; otherwise map a
                        # shifted/rounded timestamp to the nearest real window.
                        matching_window = min(
                            session_windows,
                            key=lambda value: abs(
                                error_seconds
                                - (
                                    float(value.get("start_video_time") or 0.0)
                                    + float(value.get("end_video_time") or 0.0)
                                )
                                / 2.0
                            ),
                        )
                        window_index = int(matching_window.get("window_index") or 0)
                    source_errors.append(
                        {
                            "time": error_time,
                            "window_index": window_index,
                            "source_statement": source_statement,
                            "mathematical_problem": mathematical_problem,
                            "correction": correction,
                        }
                    )
                for uncertainty in pending_source_error_uncertainties:
                    uncertainty_seconds = self._timestamp_seconds(
                        str(uncertainty.get("time") or "")
                    )
                    matching_source_error = next(
                        (
                            item
                            for item in source_errors
                            if (
                                int(uncertainty.get("window_index") or 0) > 0
                                and int(item.get("window_index") or 0)
                                == int(uncertainty.get("window_index") or 0)
                            )
                            or (
                                uncertainty_seconds >= 0
                                and abs(
                                    self._timestamp_seconds(
                                        str(item.get("time") or "")
                                    )
                                    - uncertainty_seconds
                                )
                                <= 2.0
                            )
                        ),
                        None,
                    )
                    if matching_source_error is None:
                        raise RuntimeError(
                            "window synthesis marked a material uncertainty as "
                            f"corrected without a matching source error for {session_id}"
                        )
                    uncertainty["materially_affects_mathematics"] = False
                    uncertainty["resolved_by_synthesis_source_error"] = True
                prior_reference = dict(
                    session.get("prior_locked_reference") or {}
                )
                compared_to = str(prior_reference.get("session_id") or "")
                context_start = float(
                    session.get("program_context_start_video_time") or new_start
                )
                has_context = bool(
                    compared_to and new_start > context_start + 0.05
                )
                diagram_materials = [
                    dict(diagram)
                    for window in window_notes_by_session.get(session_id, [])
                    for diagram in window.get("diagram_materials") or []
                    if isinstance(diagram, dict)
                ]
                candidate_by_id = {
                    str(item.get("diagram_id") or ""): item
                    for item in diagram_materials
                    if str(item.get("diagram_id") or "")
                }
                raw_diagram_decisions = row.get("diagram_decisions") or []
                if not isinstance(raw_diagram_decisions, list):
                    raise RuntimeError(
                        f"window synthesis returned invalid diagram decisions for {session_id}"
                    )
                if candidate_by_id and not raw_diagram_decisions:
                    # Old cached/fallback responses predate the necessity-decision
                    # schema. The safe migration is omission, never automatic
                    # publication. Fresh responses make explicit include/omit
                    # decisions under the current whole-lecture prompt.
                    raw_diagram_decisions = [
                        {
                            "diagram_id": diagram_id,
                            "decision": "omit",
                            "reason": (
                                "No explicit whole-lecture necessity decision was "
                                "returned; prose and displayed formulas remain the "
                                "authoritative safe fallback."
                            ),
                            "placement_time": "",
                        }
                        for diagram_id in candidate_by_id
                    ]
                diagram_decisions: list[dict[str, Any]] = []
                decided_ids: set[str] = set()
                for decision_row in raw_diagram_decisions:
                    if not isinstance(decision_row, dict):
                        raise RuntimeError(
                            f"window synthesis returned a non-object diagram decision for {session_id}"
                        )
                    diagram_id = str(decision_row.get("diagram_id") or "").strip()
                    decision = str(decision_row.get("decision") or "").strip().lower()
                    reason = str(decision_row.get("reason") or "").strip()
                    placement_time = str(
                        decision_row.get("placement_time") or ""
                    ).strip()
                    title = str(decision_row.get("title") or "").strip()
                    description = str(
                        decision_row.get("description") or ""
                    ).strip()
                    backend = str(decision_row.get("backend") or "").strip().lower()
                    source = str(decision_row.get("source") or "").strip()
                    has_non_english_text = bool(
                        re.search(
                            r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]",
                            "\n".join((title, description, source)),
                        )
                    )
                    include_payload_invalid = decision == "include" and (
                        not title
                        or len(description) < 24
                        or backend != "tikz"
                        or not source
                        or len(source) > 200_000
                        or has_non_english_text
                        or "???" in "\n".join((title, description, source))
                        or "\ufffd" in "\n".join((title, description, source))
                    )
                    candidate = candidate_by_id.get(diagram_id, {})
                    candidate_time = int(candidate.get("time_seconds") or -1)
                    placement_seconds = self._timestamp_seconds(placement_time)
                    include_time_invalid = decision == "include" and (
                        placement_seconds < 0
                        or (
                            candidate_time >= 0
                            and abs(placement_seconds - candidate_time) > 5 * 60
                        )
                    )
                    omit_payload_invalid = decision == "omit" and any(
                        (placement_time, title, description, backend, source)
                    )
                    if (
                        diagram_id not in candidate_by_id
                        or diagram_id in decided_ids
                        or decision not in {"include", "omit"}
                        or len(reason) < 24
                        or include_time_invalid
                        or include_payload_invalid
                        or omit_payload_invalid
                    ):
                        raise RuntimeError(
                            f"window synthesis returned an incomplete, duplicate, or invalid "
                            f"diagram decision for {session_id}: {diagram_id}"
                        )
                    decided_ids.add(diagram_id)
                    diagram_decisions.append(
                        {
                            "diagram_id": diagram_id,
                            "decision": decision,
                            "reason": reason,
                            "placement_time": placement_time,
                            "title": title,
                            "description": description,
                            "backend": backend,
                            "source": source,
                        }
                    )
                if decided_ids != set(candidate_by_id):
                    raise RuntimeError(
                        f"window synthesis omitted diagram necessity decisions for {session_id}"
                    )
                included_diagram_ids = {
                    item["diagram_id"]
                    for item in diagram_decisions
                    if item["decision"] == "include"
                }
                diagram_materials = [
                    {
                        **item,
                        **next(
                            {
                                "title": decision["title"],
                                "description": decision["description"],
                                "backend": decision["backend"],
                                "source": decision["source"],
                                "placement_time": decision["placement_time"],
                            }
                            for decision in diagram_decisions
                            if decision["diagram_id"] == str(item.get("diagram_id") or "")
                        ),
                        "candidate_status": "included_after_whole_lecture_necessity_review",
                        "necessity_decision": next(
                            decision for decision in diagram_decisions
                            if decision["diagram_id"] == str(item.get("diagram_id") or "")
                        ),
                    }
                    for item in diagram_materials
                    if str(item.get("diagram_id") or "") in included_diagram_ids
                ]
                results[session_id] = {
                    "selected_indexes": selected_indexes,
                    "decisions": [
                        {
                            "candidate_index": index,
                            "reason": (
                                "retained by a completed recording-time visual window; "
                                "the final synthesis does not discard locked evidence"
                            ),
                        }
                        for index in selected_indexes
                    ],
                    "duplicate_groups": [],
                    "mathematical_content_markdown": markdown,
                    "deduplicated_timestamped_transcript": transcript,
                    "overlap_resolution": {
                        "compared_to_session_id": compared_to,
                        "status": (
                            "duplicate_prefix_removed"
                            if has_context
                            else ("no_duplicate" if compared_to else "no_prior_session")
                        ),
                        "duplicate_prefix_start_video_time": (
                            context_start if has_context else new_start
                        ),
                        "duplicate_prefix_end_video_time": new_start,
                        "first_new_content_time": (
                            re.findall(
                                r"(?m)^\[(\d{2}:\d{2}:\d{2})\]", transcript
                            )
                            or [""]
                        )[0],
                        "repeated_content_summary": (
                            "The program-owned up-to-thirty-second continuity prefix was excluded."
                            if has_context
                            else ""
                        ),
                        "evidence": (
                            "The application supplied the fixed owned-content boundary."
                        ),
                    },
                    "evidence_status": "complete",
                    "fallback_reason": "",
                    "fallback_ranges": [],
                    "uncertainties": uncertainties,
                    "diagram_materials": diagram_materials,
                    "diagram_decisions": diagram_decisions,
                    "source_errors": source_errors,
                    "proof_obligation_resolutions": proof_resolutions,
                    # Invalid/missing optional derivatives never invalidate or
                    # trigger replay of already locked recording evidence.
                    "writing_materials": (row.get("writing_materials")
                        if isinstance(row.get("writing_materials"), list) else []),
                    "covered_window_indexes": covered,
                }
            if set(results) != session_ids:
                raise RuntimeError("window synthesis omitted a recording session")
            if "outline_segments" in parsed:
                emit(
                    "内容综合 Agent 返回了越权目录字段；该字段已被丢弃，绝不进入正式目录。"
                )
            return results, targeted_reviews

        def resolve_targeted_windows(
            review_requests: dict[str, dict[str, Any]],
        ) -> int:
            review_items: list[dict[str, Any]] = []
            sessions_by_id = {
                str(item.get("session_id") or ""): item
                for item in payload_sessions
            }
            for session_id, request in review_requests.items():
                session = sessions_by_id[session_id]
                transcript = full_transcripts_by_session.get(session_id, "")
                frames = {
                    int(item["index"]): item
                    for item in frames_by_session.get(session_id, [])
                    if str(item.get("index") or "").strip().isdigit()
                }
                windows = {
                    int(item.get("window_index") or 0): item
                    for item in session.get("recording_windows") or []
                    if isinstance(item, dict)
                }
                for window_index in request.get("window_indexes") or []:
                    window = windows.get(int(window_index))
                    if window is None:
                        raise RuntimeError(
                            f"targeted review requested unknown window "
                            f"{session_id}/{window_index}"
                        )
                    start = float(window.get("start_video_time") or 0.0)
                    end = float(window.get("end_video_time") or start)
                    transcript_lines = []
                    for line in transcript.splitlines():
                        timestamp = self._timestamp_seconds(line)
                        if timestamp < 0 or start - 2.0 <= timestamp <= end + 2.0:
                            transcript_lines.append(line)
                    selected_indexes = [
                        int(value)
                        for value in window.get("selected_candidate_indexes") or []
                        if str(value).strip().isdigit()
                    ]
                    selected_frames = [
                        dict(frames[index])
                        for index in selected_indexes
                        if index in frames
                    ]
                    if not selected_frames:
                        selected_frames = [
                            dict(item)
                            for item in frames.values()
                            if start - 0.05
                            <= float(item.get("video_time") or 0.0)
                            <= end + 0.05
                        ]
                    if len(selected_frames) > self.TARGETED_REVIEW_MAX_IMAGES:
                        raise RuntimeError(
                            f"targeted review window {session_id}/{window_index} "
                            "exceeds the bounded image limit"
                        )
                    review_items.append(
                        {
                            "session_id": session_id,
                            "window_index": int(window_index),
                            "start_video_time": start,
                            "end_video_time": end,
                            "candidate_signature": str(
                                window.get("candidate_signature") or ""
                            ),
                            "timestamped_audio_transcript": "\n".join(
                                transcript_lines
                            ),
                            "locked_mathematical_content_markdown": str(
                                window.get("mathematical_content_markdown") or ""
                            ),
                            "lead_agent_uncertainties": list(
                                request.get("uncertainties") or []
                            ),
                            "selected_frames": selected_frames,
                        }
                    )

            reviewed: set[tuple[str, int]] = set()
            cache_documents: dict[Path, dict[str, Any]] = {}

            def cache_key(item: dict[str, Any]) -> str:
                value = {
                    "schema_version": 1,
                    "session_id": str(item["session_id"]),
                    "window_index": int(item["window_index"]),
                    "candidate_signature": str(
                        item.get("candidate_signature") or ""
                    ),
                    "timestamped_audio_transcript": str(
                        item.get("timestamped_audio_transcript") or ""
                    ),
                    "locked_mathematical_content_markdown": str(
                        item.get("locked_mathematical_content_markdown") or ""
                    ),
                    "agent_profile": str(getattr(self, "profile_label", "") or ""),
                }
                return hashlib.sha256(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()

            def cache_document(path: Path) -> dict[str, Any]:
                if path in cache_documents:
                    return cache_documents[path]
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    value = {}
                if not isinstance(value, dict) or int(value.get("version") or 0) != 1:
                    value = {"version": 1, "entries": {}}
                if not isinstance(value.get("entries"), dict):
                    value["entries"] = {}
                cache_documents[path] = value
                return value

            def write_cache(
                item: dict[str, Any],
                normalized_row: dict[str, Any],
            ) -> None:
                path = targeted_review_cache_paths.get(str(item["session_id"]))
                if path is None:
                    return
                document = cache_document(path)
                document["entries"][cache_key(item)] = {
                    **normalized_row,
                    "cached_at_unix": time.time(),
                }
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, path)

            def apply_review_row(
                row: dict[str, Any],
                original: dict[str, Any],
            ) -> dict[str, Any]:
                key = (
                    str(row.get("session_id") or ""),
                    int(row.get("window_index") or 0),
                )
                expected_key = (
                    str(original["session_id"]),
                    int(original["window_index"]),
                )
                if key != expected_key or key in reviewed:
                    raise RuntimeError(
                        f"targeted window review returned unknown window {key}"
                    )
                evidence_status = str(row.get("evidence_status") or "")
                if evidence_status not in {
                    "complete",
                    "source_error_corrected",
                }:
                    raise RuntimeError(
                        f"targeted window review could not resolve {key}: "
                        f"{row.get('uncertainties') or []}"
                    )
                markdown = str(
                    row.get("mathematical_content_markdown") or ""
                ).strip()
                if not markdown:
                    raise RuntimeError(
                        f"targeted window review returned empty mathematics for {key}"
                    )
                source_error = ""
                correction = ""
                if evidence_status == "source_error_corrected":
                    source_error = str(row.get("source_error") or "").strip()
                    correction = str(row.get("correction") or "").strip()
                    if not source_error or not correction:
                        raise RuntimeError(
                            f"targeted window review did not document the "
                            f"source error and correction for {key}"
                        )
                    if "Evidence correction" not in markdown:
                        markdown = (
                            markdown.rstrip()
                            + "\n\n- **Evidence correction — source error:** "
                            + source_error
                            + "\n\n- **Rigorous correction:** "
                            + correction
                        )
                obligations = set(
                    re.findall(
                        r"【待补证明：.*?】",
                        str(
                            original[
                                "locked_mathematical_content_markdown"
                            ]
                        ),
                    )
                )
                missing_obligations = [
                    value for value in obligations if value not in markdown
                ]
                if missing_obligations:
                    markdown = (
                        markdown.rstrip()
                        + "\n\n### Preserved proof obligations\n\n"
                        + "\n\n".join(missing_obligations)
                    )
                session = sessions_by_id[key[0]]
                window = next(
                    item
                    for item in session.get("recording_windows") or []
                    if int(item.get("window_index") or 0) == key[1]
                )
                window["mathematical_content_markdown"] = markdown
                window["targeted_review"] = {
                    "completed": True,
                    "attachment_count": len(original["selected_frames"]),
                    "source_error_corrected": bool(source_error),
                    "source_error": source_error,
                    "correction": correction,
                }
                window["evidence_status"] = "complete"
                reviewed.add(key)
                return {
                    "session_id": key[0],
                    "window_index": key[1],
                    "mathematical_content_markdown": markdown,
                    "evidence_status": evidence_status,
                    "source_error": source_error,
                    "correction": correction,
                    "uncertainties": list(row.get("uncertainties") or []),
                }

            uncached_review_items: list[dict[str, Any]] = []
            for item in review_items:
                path = targeted_review_cache_paths.get(str(item["session_id"]))
                cached_row: Any = None
                if path is not None:
                    cached_row = cache_document(path)["entries"].get(cache_key(item))
                if isinstance(cached_row, dict):
                    try:
                        apply_review_row(dict(cached_row), item)
                    except RuntimeError:
                        uncached_review_items.append(item)
                    else:
                        emit(
                            f"录制窗口定点数学复核已锁定复用："
                            f"{item['session_id']}/{int(item['window_index'])}。"
                        )
                        continue
                else:
                    uncached_review_items.append(item)

            groups: list[list[dict[str, Any]]] = []
            for item in uncached_review_items:
                frame_count = len(item["selected_frames"])
                if (
                    not groups
                    or sum(
                        len(value["selected_frames"]) for value in groups[-1]
                    )
                    + frame_count
                    > self.EVIDENCE_REQUEST_MAX_IMAGES
                ):
                    groups.append([])
                groups[-1].append(item)

            for group_number, group in enumerate(groups, start=1):
                attachments = [
                    frame
                    for item in group
                    for frame in item["selected_frames"]
                ]
                review_payload = {
                    **course_info,
                    "workflow": "targeted_recording_window_mathematics_review",
                    "schema_version": 1,
                    "windows": [
                        {
                            key: value
                            for key, value in item.items()
                            if key != "selected_frames"
                        }
                        | {
                            "candidate_frames": [
                                {
                                    "index": int(frame["index"]),
                                    "time": str(frame.get("time") or ""),
                                    "attachment_name": str(
                                        frame.get("attachment_name") or ""
                                    ),
                                }
                                for frame in item["selected_frames"]
                            ]
                        }
                        for item in group
                    ],
                    "response_schema": {
                        "schema_version": 1,
                        "windows": [
                            {
                                "session_id": "string",
                                "window_index": "integer",
                                "mathematical_content_markdown": "string",
                                "evidence_status": (
                                    "complete | source_error_corrected | unresolved"
                                ),
                                "source_error": "string; required only for source_error_corrected",
                                "correction": "string; required only for source_error_corrected",
                                "uncertainties": ["string"],
                            }
                        ],
                    },
                }
                review_prompt = (
                    "You are a fail-closed mathematical evidence adjudicator. "
                    "Return ONLY JSON matching response_schema. Review only the supplied "
                    "recording windows. Compare every attached locked keyframe, the exact "
                    "time-bounded transcript, the locked window note, and the lead Agent's "
                    "uncertainties. Correct transcription or notation only when the evidence "
                    "supports it. Preserve every definition, hypothesis, theorem, proof step, "
                    "calculation, exercise, correction, transition, and exact "
                    "`【待补证明：...】` marker. Do not summarize, complete an omitted proof, "
                    "or invent mathematics. Return evidence_status complete only when the "
                    "window is fully and consistently reconstructed. If the lecture itself "
                    "contains a demonstrably false proof step but the supplied evidence and "
                    "standard theorem give one unique minimal rigorous repair, preserve the "
                    "source claim as an explicit error, add a clearly labelled `Evidence "
                    "correction` paragraph with the rigorous repair, return "
                    "source_error_corrected, and fill both source_error and correction. "
                    "Never silently rewrite the lecture. If the repair is not uniquely "
                    "justified, return unresolved with a precise uncertainty. The corrected "
                    "Markdown remains a time-bounded evidence note, not final lecture prose."
                )
                emit(
                    f"统一 Agent 要求定点复核 {len(group)} 个录制窗口；"
                    f"第 {group_number}/{len(groups)} 组只上传 "
                    f"{len(attachments)} 张窗口已选关键帧。"
                )
                answer = self._run(
                    review_payload,
                    self._attachments(attachments),
                    review_prompt,
                    emit,
                    stage="录制窗口定点数学复核",
                )
                parsed = _parse_json_response(answer)
                rows = parsed.get("windows") if isinstance(parsed, dict) else None
                if (
                    not isinstance(parsed, dict)
                    or int(parsed.get("schema_version") or 0) != 1
                    or not isinstance(rows, list)
                ):
                    raise RuntimeError(
                        "targeted window review returned invalid JSON"
                    )
                for row in rows:
                    if not isinstance(row, dict):
                        raise RuntimeError(
                            "targeted window review returned a non-object window"
                        )
                    key = (
                        str(row.get("session_id") or ""),
                        int(row.get("window_index") or 0),
                    )
                    expected_keys = {
                        (str(item["session_id"]), int(item["window_index"]))
                        for item in group
                    }
                    if key not in expected_keys:
                        raise RuntimeError(
                            f"targeted window review returned unknown window {key}"
                        )
                    original = next(
                        item
                        for item in group
                        if (
                            str(item["session_id"]),
                            int(item["window_index"]),
                        )
                        == key
                    )
                    normalized_row = apply_review_row(row, original)
                    write_cache(original, normalized_row)
                expected_keys = {
                    (str(item["session_id"]), int(item["window_index"]))
                    for item in group
                }
                if not expected_keys.issubset(reviewed):
                    raise RuntimeError(
                        "targeted window review omitted a requested window"
                    )
            return len(groups)

        def synthesize_in_bounded_parts() -> tuple[str, int]:
            """Run relay-safe final text patches and reconstruct content only."""
            merged_rows: dict[str, dict[str, Any]] = {
                session_id: {
                    "session_id": session_id,
                    "covered_window_indexes": [],
                    "window_drafts": [],
                    "additional_markdown": [],
                    "source_errors": [],
                    "proof_obligation_resolutions": [],
                    "uncertainties": [],
                    "diagram_decisions": [],
                    "writing_materials": [],
                }
                for session_id in expected_windows
            }
            actual_requests = 0
            prefetched_keys: set[str] = set()
            prefetched_answers: dict[str, str] = {}
            prefetch_errors: dict[str, Exception] = {}
            merged_proof_markers: dict[str, set[str]] = {
                session_id: set() for session_id in expected_windows
            }
            uncached_parts = [
                part
                for part in synthesis_parts
                if not cached_parts.get(str(part["key"]), "")
            ]
            if (
                len(uncached_parts) > 1
                and self.FINAL_SYNTHESIS_PARALLEL_REQUESTS > 1
            ):
                workers = min(
                    self.FINAL_SYNTHESIS_PARALLEL_REQUESTS,
                    len(uncached_parts),
                )
                emit(
                    f"结束阶段共有 {len(uncached_parts)} 个互不依赖的文本块，"
                    f"正在以 {workers} 个并发请求处理；失败块将自动二分回退。"
                )

                def prefetch_part(
                    position: int, part: dict[str, Any]
                ) -> tuple[str, str]:
                    expected_session_id = str(part["session_id"])
                    expected_coverage = list(part["window_indexes"])
                    part_prompt = (
                        system_prompt_for_part(part)
                        + "\n\nThis is one bounded relay-safe part of the final synthesis. "
                        + f"Return exactly one sessions row for session_id {expected_session_id!r}; "
                        + f"covered_window_indexes must be exactly {expected_coverage!r}. "
                        + "Review and patch only these supplied windows. Do not refer to or "
                        + "invent mathematics outside this bounded transcript and draft. A later "
                        + "For diagram decisions only, use whole_lecture_diagram_review_context: "
                        + "it contains the complete locked mathematical record and every candidate, "
                        + "so necessity and redundancy are whole-lecture judgments. Return decisions "
                        + "only for decision_scope_diagram_ids. "
                        + "local merge will validate coverage and apply the exact patches. Return a "
                        + "proof_obligation_resolutions row only for an exact `【待补证明：...】` "
                        + "marker already present in this supplied draft. Never convert a `【待核对：...】` "
                        + "item into a proof marker; report it only as an uncertainty."
                    )
                    answer = self._run(
                        dict(part["payload"]),
                        [],
                        part_prompt,
                        emit,
                        stage=(
                            "录制段数学正确性审核与统一生成"
                            f"（文本分块 {position}/{len(uncached_parts)}，并发）"
                        ),
                    )
                    return str(part["key"]), answer

                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="recording-final-synthesis",
                ) as executor:
                    futures = {
                        executor.submit(prefetch_part, position, part): part
                        for position, part in enumerate(uncached_parts, start=1)
                    }
                    actual_requests += len(futures)
                    for future in as_completed(futures):
                        part = futures[future]
                        part_key = str(part["key"])
                        try:
                            returned_key, answer = future.result()
                        except Exception as error:
                            prefetch_errors[part_key] = error
                            continue
                        prefetched_answers[returned_key] = answer
                        prefetched_keys.add(returned_key)

            pending_parts = list(synthesis_parts)
            completed_part_count = 0
            while pending_parts:
                part = pending_parts.pop(0)
                part_number = completed_part_count + 1
                current_total = completed_part_count + 1 + len(pending_parts)
                part_key = str(part["key"])
                expected_session_id = str(part["session_id"])
                expected_coverage = list(part["window_indexes"])
                prefetched_error = prefetch_errors.pop(part_key, None)
                if prefetched_error is not None and len(expected_coverage) > 1:
                    split_at = len(expected_coverage) // 2
                    payload_session = synthesis_sessions_by_id[
                        expected_session_id
                    ]
                    pending_parts[0:0] = [
                        build_synthesis_part(
                            payload_session, expected_coverage[:split_at]
                        ),
                        build_synthesis_part(
                            payload_session, expected_coverage[split_at:]
                        ),
                    ]
                    emit(
                        "结束阶段并发多窗口文本块请求失败，已自动二分并继续；"
                        f"窗口 {expected_coverage}，原因：{prefetched_error}"
                    )
                    continue
                answer = prefetched_answers.get(part_key, "") or cached_parts.get(
                    part_key, ""
                )
                used_timeout_fallback = False
                if answer:
                    if part_key in prefetched_keys:
                        emit(
                            "结束阶段并发纯文本块已完成："
                            f"第 {part_number}/{current_total} 块，"
                            f"窗口 {expected_coverage}。"
                        )
                    else:
                        emit(
                            "结束阶段纯文本分块响应已复用："
                            f"第 {part_number}/{current_total} 块，"
                            f"窗口 {expected_coverage}，模型调用为 0。"
                        )
                else:
                    emit(
                        "结束阶段纯文本分块合成："
                        f"第 {part_number}/{current_total} 块，"
                        f"窗口 {expected_coverage}；JSON 输入 "
                        f"{len(json.dumps(part['payload'], ensure_ascii=False))} 字符。"
                    )
                    part_prompt = (
                        system_prompt_for_part(part)
                        + "\n\nThis is one bounded relay-safe part of the final synthesis. "
                        + f"Return exactly one sessions row for session_id {expected_session_id!r}; "
                        + f"covered_window_indexes must be exactly {expected_coverage!r}. "
                        + "Review and patch only these supplied windows. Do not refer to or "
                        + "invent mathematics outside this bounded transcript and draft. A later "
                        + "For diagram decisions only, use whole_lecture_diagram_review_context: "
                        + "it contains the complete locked mathematical record and every candidate, "
                        + "so necessity and redundancy are whole-lecture judgments. Return decisions "
                        + "only for decision_scope_diagram_ids. "
                        + "local merge will validate coverage and apply the exact patches. Return a "
                        + "proof_obligation_resolutions row only for an exact `【待补证明：...】` "
                        + "marker already present in this supplied draft. Never convert a `【待核对：...】` "
                        + "item into a proof marker; report it only as an uncertainty."
                    )
                    actual_requests += 1
                    try:
                        answer = self._run(
                            dict(part["payload"]),
                            [],
                            part_prompt,
                            emit,
                            stage=(
                                "录制段数学正确性审核与统一生成"
                                f"（文本分块 {part_number}/{current_total}）"
                            ),
                        )
                    except Exception as error:
                        if len(expected_coverage) <= 1:
                            if not self._is_transient_upstream_error(error):
                                raise
                            assigned_sessions = list(
                                dict(part.get("payload") or {}).get(
                                    "recording_sessions"
                                )
                                or []
                            )
                            assigned_windows = [
                                dict(window)
                                for assigned_session in assigned_sessions
                                if isinstance(assigned_session, dict)
                                for window in assigned_session.get(
                                    "recording_windows"
                                )
                                or []
                                if isinstance(window, dict)
                            ]
                            if any(
                                str(window.get("evidence_status") or "")
                                == "transcript_fallback"
                                for window in assigned_windows
                            ):
                                emit(
                                    "结束阶段转写回退窗口的单窗口请求仍遇到瞬时网关错误；"
                                    "该窗口没有可发布的锁定数学稿，本次生成保持失败并等待重试，"
                                    "不会把占位文本写成完成结果。"
                                )
                                raise RuntimeError(
                                    "transcript-fallback window still requires a "
                                    "successful mathematical reconstruction; "
                                    f"session={expected_session_id}, windows={expected_coverage}; "
                                    f"upstream error: {error}"
                                ) from error
                            # A single recording window cannot be split further.
                            # Preserve its already locked mathematical draft and
                            # emit an explicit unresolved uncertainty instead of
                            # fabricating a correction or aborting the whole
                            # lecture package. Do not cache this no-op fallback;
                            # a later regeneration may retry the upstream review.
                            used_timeout_fallback = True
                            emit(
                                "结束阶段单窗口数学审核遇到中转站瞬时超时；"
                                "已保留该窗口锁定稿并记录待核对状态，不伪造修正。"
                            )
                            answer = json.dumps(
                                {
                                    "schema_version": self.RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION,
                                    "sessions": [
                                        {
                                            "session_id": expected_session_id,
                                            "covered_window_indexes": expected_coverage,
                                            "replacements": [],
                                            "additional_markdown": "",
                                            "source_errors": [],
                                            "proof_obligation_resolutions": [],
                                            "uncertainties": [
                                                {
                                                    "time": "",
                                                    "window_index": expected_coverage[0],
                                                    "kind": "final_synthesis_gateway_timeout",
                                                    "detail": (
                                                        "The final mathematical audit timed out at the upstream gateway; "
                                                        "the locked window draft was preserved without additional correction."
                                                    ),
                                                    "materially_affects_mathematics": True,
                                                    "resolution_status": "unresolved",
                                                }
                                            ],
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        if not used_timeout_fallback:
                            split_at = len(expected_coverage) // 2
                            payload_session = synthesis_sessions_by_id[
                                expected_session_id
                            ]
                            split_parts = [
                                build_synthesis_part(
                                    payload_session, expected_coverage[:split_at]
                                ),
                                build_synthesis_part(
                                    payload_session, expected_coverage[split_at:]
                                ),
                            ]
                            pending_parts[0:0] = split_parts
                            emit(
                                "结束阶段多窗口文本块请求失败，已自动二分并继续；"
                                f"窗口 {expected_coverage}，原因：{error}"
                            )
                            continue

                try:
                    parsed = _parse_json_response(answer)
                except (json.JSONDecodeError, UnicodeError) as error:
                    # Relay gateways and model streams can return a syntactically
                    # truncated JSON document even when the HTTP request itself
                    # succeeded. Retry the bounded part at a smaller scope rather
                    # than treating the whole lecture as failed. Keep the
                    # single-window path fail-closed because there is no safe
                    # local way to reconstruct a missing JSON response.
                    if part_key in cached_parts:
                        cached_parts.pop(part_key, None)
                        cache_parts()
                    if len(expected_coverage) <= 1:
                        if part.get("omit_writing_materials") or self._english_course(course_info):
                            raise
                        # The added writer record must never turn a window that
                        # used to fit into a failed stop-stage synthesis.
                        pending_parts[0:0] = [without_writing_materials(part)]
                        emit(
                            "结束阶段单窗口响应截断或破损；已去掉连续写作记录要求重试该窗口，"
                            "原有数学修正照常完成，连续写作底稿以后可单独整理。"
                            f"窗口 {expected_coverage}，原因：{error}"
                        )
                        continue
                    split_at = len(expected_coverage) // 2
                    payload_session = synthesis_sessions_by_id[
                        expected_session_id
                    ]
                    pending_parts[0:0] = [
                        build_synthesis_part(
                            payload_session, expected_coverage[:split_at]
                        ),
                        build_synthesis_part(
                            payload_session, expected_coverage[split_at:]
                        ),
                    ]
                    emit(
                        "结束阶段多窗口文本块返回了截断或破损 JSON，已丢弃该响应并自动二分重试；"
                        f"窗口 {expected_coverage}，原因：{error}"
                    )
                    continue
                rows = parsed.get("sessions") if isinstance(parsed, dict) else None
                if (
                    not isinstance(parsed, dict)
                    or int(parsed.get("schema_version") or self.RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION)
                    != self.RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION
                    or not isinstance(rows, list)
                    or len(rows) != 1
                    or not isinstance(rows[0], dict)
                ):
                    raise RuntimeError(
                        f"final synthesis part {part_number} returned invalid JSON"
                    )
                row = dict(rows[0])
                returned_session_id = str(row.get("session_id") or "")
                returned_coverage = sorted(
                    {
                        int(value)
                        for value in row.get("covered_window_indexes") or []
                        if str(value).strip().isdigit()
                    }
                )
                if (
                    returned_session_id != expected_session_id
                    or returned_coverage != expected_coverage
                ):
                    raise RuntimeError(
                        f"final synthesis part {part_number} omitted or duplicated windows "
                        "or changed its assigned windows"
                    )
                target = merged_rows[expected_session_id]
                local_session = (part.get("payload") or {}).get("recording_sessions") or []
                local_draft = ""
                if local_session and isinstance(local_session[0], dict):
                    local_draft = str(
                        local_session[0].get("draft_mathematical_content_markdown")
                        or ""
                    ).strip()
                if not local_draft:
                    raise RuntimeError(
                        f"final synthesis part {part_number} omitted its locked local draft"
                    )
                # Each bounded response is authored against this exact local draft.
                # Apply its patches before any other window can change the text.
                unmatched_targets: list[dict[str, Any]] = []
                try:
                    local_draft = apply_synthesis_replacements(
                        local_draft,
                        row.get("replacements"),
                        session_id=expected_session_id,
                        window_blocks=list(part.get("draft_window_blocks") or []),
                        unmatched_targets=(
                            unmatched_targets
                            if len(expected_coverage) > 1
                            else None
                        ),
                    )
                except RuntimeError as error:
                    # A syntactically valid model response can still violate the
                    # exact-patch contract by copying a target from another relay
                    # part or by altering the target while quoting it. Treat that
                    # like an oversized/failed relay request: discard any cached
                    # answer and retry smaller bounded parts. A single-window
                    # failure remains fail-closed, so no fuzzy mathematical edit
                    # is ever guessed locally.
                    if part_key in cached_parts:
                        cached_parts.pop(part_key, None)
                        cache_parts()
                    if len(expected_coverage) <= 1:
                        raise
                    split_at = len(expected_coverage) // 2
                    payload_session = synthesis_sessions_by_id[
                        expected_session_id
                    ]
                    pending_parts[0:0] = [
                        build_synthesis_part(
                            payload_session, expected_coverage[:split_at]
                        ),
                        build_synthesis_part(
                            payload_session, expected_coverage[split_at:]
                        ),
                    ]
                    emit(
                        "结束阶段多窗口文本块的精确补丁校验失败，已丢弃该响应并自动二分重试；"
                        f"窗口 {expected_coverage}，原因：{error}"
                    )
                    continue
                if unmatched_targets:
                    uncertainties = row.setdefault("uncertainties", [])
                    if not isinstance(uncertainties, list):
                        raise RuntimeError(
                            f"final synthesis part {part_number} returned an invalid uncertainties field"
                        )
                    for unmatched in unmatched_targets:
                        indexes = list(unmatched.get("window_indexes") or [])
                        uncertainties.append(
                            {
                                "time": "",
                                "window_index": indexes[0] if indexes else expected_coverage[0],
                                "kind": "unmatched_final_synthesis_patch",
                                "detail": (
                                    "A proposed final correction could not be uniquely located "
                                    "in the locked mathematical draft. The original evidence was "
                                    "preserved and the correction was not guessed."
                                ),
                                "materially_affects_mathematics": True,
                                "resolution_status": "unresolved",
                            }
                        )
                additional = str(row.get("additional_markdown") or "").strip()
                if additional:
                    local_draft = local_draft.rstrip() + "\n\n" + additional
                target["covered_window_indexes"].extend(returned_coverage)
                target["window_drafts"].append(
                    {
                        "window_indexes": list(returned_coverage),
                        "markdown": local_draft,
                    }
                )
                for field in (
                    "source_errors",
                    "proof_obligation_resolutions",
                    "uncertainties",
                    "diagram_decisions",
                    "writing_materials",
                ):
                    values = row.get(field) or []
                    if not isinstance(values, list):
                        if field == "writing_materials":
                            emit("写作派生材料格式不完整；保留已锁定证据，稍后可单独整理，不重跑录制批次。")
                            values = []
                        else:
                            raise RuntimeError(
                                f"final synthesis part {part_number} returned an invalid {field} field"
                            )
                    if field == "proof_obligation_resolutions":
                        allowed_markers = {
                            marker
                            for window_index in expected_coverage
                            for marker in re.findall(
                                r"【待补证明：.*?】",
                                str(
                                    window_notes_by_session.get(
                                        expected_session_id,
                                        [],
                                    )[window_index - 1].get(
                                        "mathematical_content_markdown"
                                    )
                                    or ""
                                ),
                            )
                        }
                        filtered_values: list[Any] = []
                        for value in values:
                            if not isinstance(value, dict):
                                filtered_values.append(value)
                                continue
                            marker = str(value.get("marker") or "").strip()
                            if marker not in allowed_markers:
                                emit(
                                    "结束阶段分块响应返回了未锚定的证明标记；"
                                    "已保留原窗口的【待核对】/【待补证明】原文并忽略该附加解析："
                                    + marker[:160]
                                )
                                continue
                            if marker in merged_proof_markers[expected_session_id]:
                                emit(
                                    "结束阶段分块响应重复解析了已锁定的证明标记；"
                                    "保留首个精确锚定解析："
                                    + marker[:160]
                                )
                                continue
                            merged_proof_markers[expected_session_id].add(marker)
                            filtered_values.append(value)
                        values = filtered_values
                    target[field].extend(values)
                if part_key not in cached_parts and not used_timeout_fallback:
                    # Cache only responses whose exact patches have passed local
                    # validation. Otherwise a restart would replay the same bad
                    # model output forever and never reach the binary fallback.
                    cached_parts[part_key] = answer
                    cache_parts()
                if "outline_segments" in parsed:
                    emit(
                        "最终内容 Agent 返回了越权目录字段；该字段已被丢弃。"
                    )
                completed_part_count += 1

            if False:
                emit(
                    "结束阶段分块目录存在重复编号但不同数学标题；"
                    "旧版目录重编号分支已禁用；目录决策不会在内容综合阶段生成。"
                )
            merged_sessions: list[dict[str, Any]] = []
            for session_id, row in merged_rows.items():
                if sorted(row["covered_window_indexes"]) != expected_windows[session_id]:
                    raise RuntimeError(
                        f"bounded final synthesis omitted windows for {session_id}"
                    )
                drafts = sorted(
                    row.pop("window_drafts"),
                    key=lambda item: min(item["window_indexes"]),
                )
                row["mathematical_content_markdown"] = "\n\n".join(
                    str(item["markdown"]).strip() for item in drafts if str(item["markdown"]).strip()
                )
                row.pop("additional_markdown", None)
                merged_sessions.append(row)
            return (
                json.dumps(
                    {
                        "schema_version": self.RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION,
                        "sessions": merged_sessions,
                    },
                    ensure_ascii=False,
                ),
                actual_requests,
            )

        try:
            if cached_answer:
                emit(
                    "录制段最终纯文本响应的证据指纹未变化；"
                    "直接重放模型原始结果并重新执行本地校验，模型调用为 0。"
                )
                answer = cached_answer
                self.increment_request_count = 0
            elif synthesis_parts:
                emit(
                    "结束阶段纯文本合成将按锁定录制窗口分块请求，"
                    f"共 {len(synthesis_parts)} 块；不上传图片，"
                    "每块成功后立即缓存，避免中转站 HTTP 524 迫使整集重做。"
                )
                answer, request_count = synthesize_in_bounded_parts()
                cache_answer(answer)
                self.increment_request_count = request_count
            else:
                emit(
                    "结束阶段纯文本合成即将发送："
                    f"{sum(len(item.get('recording_windows') or []) for item in payload_sessions)} "
                    "个录制期子 Agent 窗口，0 个图片附件；完整 ASR 与子 Agent 数学笔记"
                    "只发送给主 Agent这一次；"
                    f"JSON 输入 {len(json.dumps(synthesis_payload, ensure_ascii=False))} 字符。"
                )
                answer = self._run(
                    synthesis_payload,
                    [],
                    system_prompt,
                    emit,
                    stage="录制段数学正确性审核与统一生成（单次文本请求）",
                )
                cache_answer(answer)
                self.increment_request_count = 1
            results, _review_requests = parse_synthesis(answer)
            return results, [], ""
        except Exception as error:
            if cached_answer and response_cache_path is not None:
                try:
                    response_cache_path.unlink()
                except OSError:
                    pass
            return {}, [], str(error)

    def process_recording_increment(
        self,
        course_info: dict[str, Any],
        recording_sessions: list[dict[str, Any]],
        board_frames: list[dict[str, Any]],
        emit: Callable[[str], None],
        *,
        outline_request: dict[str, Any] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
        """Reconstruct new sessions in the fewest bounded, session-safe turns."""
        if not recording_sessions:
            return {}, [], ""
        recording_session_ids = [
            str(item.get("session_id") or "") for item in recording_sessions
        ]
        if (
            any(not value for value in recording_session_ids)
            or len(set(recording_session_ids)) != len(recording_session_ids)
        ):
            return {}, [], "recording session identifiers are missing or duplicated"
        window_manifests = [
            dict(item.get("recording_period_window_manifest") or {})
            for item in recording_sessions
        ]
        if any(window_manifests):
            if all(
                bool(item.get("authoritative"))
                or bool(item.get("structural_complete"))
                for item in window_manifests
            ):
                return self._process_authoritative_recording_windows(
                    course_info,
                    recording_sessions,
                    board_frames,
                    emit,
                    outline_request=outline_request,
                )
            emit(
                "录制期 Agent 窗口结构不完整；结束阶段只能使用完整时间戳转写和"
                "程序候选帧重新核验。已完整但标记 needs_review 的窗口不会进入此回退。"
            )
            return (
                {},
                [],
                "recording-time child windows are incomplete; image re-upload fallback is disabled",
            )
        if not any(window_manifests):
            return (
                {},
                [],
                "recording-time child windows are missing; image re-upload fallback is disabled",
            )
        session_ids = set(recording_session_ids)
        frames_by_session: dict[str, list[dict[str, Any]]] = {
            session_id: [] for session_id in session_ids
        }
        for item in board_frames:
            session_id = str(item.get("session_id") or "")
            if session_id not in frames_by_session:
                return {}, [], f"candidate frame belongs to unknown session {session_id}"
            frames_by_session[session_id].append(item)
        valid_indexes = {
            int(item["index"]): str(item.get("session_id") or "")
            for item in board_frames
        }
        if len(valid_indexes) != len(board_frames):
            return {}, [], "candidate frame indexes are not unique"

        system_prompt = (
            "You are the lead mathematical reconstruction agent for newly recorded increments of a university mathematics course. "
            "Return ONLY one JSON object matching the supplied `response_schema`, including `schema_version`. For each supplied "
            "recording session, compare its complete timestamped "
            "audio transcript with every supplied candidate-frame image and return exactly one session object containing: "
            "`session_id`; `selected` and `rejected` as disjoint integer-index arrays that together cover every candidate; "
            "`mathematical_content_markdown`; `diagram_materials`; "
            "`evidence_status` (`complete` or `needs_three_second_fallback`); `fallback_reason`; `uncertainties`; and `fallback_ranges`. "
            "The Markdown must reconstruct complete, coherent mathematics in chronological order with `## HH:MM:SS` headings. "
            "Treat diagram_materials as optional relationship candidates for later whole-lecture necessity review, never as a quota. Add a candidate only for a genuinely spatial, geometric, incidence, quotient, gluing, or multi-map relationship that might be materially harder to understand from prose and formulas alone. Do not propose a candidate merely because an object is visualizable, the board or textbook contains one, the topic is geometric, or a previous window used a related picture. At recording time, return only `time`, `title`, and a precise mathematical `description`; do not choose a backend and do not generate any drawing source. The whole-lecture main Agent alone decides whether a candidate is necessary and may generate source only after choosing include. Return an empty array whenever no candidate clears this threshold. "
            "Use the transcript and images to determine the lecturer's actual topic, notation, hypotheses, theorem statements, "
            "calculations, and proof route. Repair ASR errors and restore connective steps only when the intended mathematics is "
            "uniquely determined by the supplied evidence and standard definitions. Preserve every explicit hypothesis and the "
            "lecturer's notation. Never invent a materially new claim or silently choose between genuinely different formulas. "
            "A session may include `recording_period_window_notes` produced from bounded image windows while recording was still "
            "running. Use those notes as a chronological evidence index, but verify the final mathematics against the complete "
            "timestamped transcript and the supplied precurated frames; the notes are not permission to invent or omit content. "
            "If the lecturer omits or assigns a proof, preserve the exact `【待补证明：...】` obligation instead of writing a new "
            "proof in this evidence stage. Do not repeat conversational phrasing already available in the raw transcript: the "
            "Markdown is a normalized mathematical state, not a second transcript. "
            "Select the smallest image set that preserves every genuinely distinct mathematical board state. Reject a frame "
            "only when its mathematical content is fully represented by a clearer retained frame; visual similarity alone is "
            "not enough. Do not write per-frame reasons or repeat image contents outside the mathematical Markdown. "
            "Each non-first session includes `prior_locked_reference` plus program-fixed `program_context_start_video_time` and "
            "`program_new_content_start_video_time`. The application has already bounded the earlier overlap and retained up to thirty "
            "seconds before the previous session endpoint solely as continuity context. Do NOT search for or move the overlap boundary. "
            "Copy those program times into `overlap_resolution`, use the short locked tail only to make the mathematics connect cleanly, "
            "and put only utterances at or after `program_new_content_start_video_time` in `deduplicated_timestamped_transcript`. "
            "The mathematical Markdown and selected keyframes must also start at or after that fixed boundary and must not restate the "
            "locked predecessor. The prior session is immutable reference evidence and must never be rewritten. Normally return `complete`. Do not request three-second "
            "screenshots for omitted routine proof details, ASR noise, confidence checking, a topic continuing beyond the natural "
            "recording endpoint, or anything resolvable from mathematical context. Use `needs_three_second_fallback` only when a core "
            "formula or theorem statement remains genuinely ambiguous between materially different mathematical possibilities after "
            "reasoning from all supplied evidence. In that exceptional case, return `fallback_ranges` as a minimal array of objects "
            "with numeric `start_video_time`, numeric `end_video_time`, `gap_kind` equal to `conflicting_core_formula` or "
            "`unreadable_core_statement`, and a precise `missing_evidence` string. Otherwise return an empty array. "
            "Your responsibility ends with faithful, complete mathematical evidence for this session. You have no directory "
            "authority: do not propose, infer, label, split, merge, number, or return Chapters, Sections, Subsections, "
            "outline segments, or formal time boundaries. A separate directory Agent receives the complete final record "
            "only after all sessions have been reconstructed."
        )
        if self._english_course(course_info):
            system_prompt = (
                "You are the lead evidence-reconstruction agent for newly recorded increments of an English-language course. "
                "Return ONLY one JSON object matching response_schema. For every session, partition every candidate frame between "
                "`selected` and `rejected`, and use `mathematical_content_markdown` as a legacy field name for chronological, "
                "timestamped language-course evidence. Preserve all English examples verbatim, including intentionally incorrect "
                "ones, and keep teacher corrections separate. Reconstruct grammar rules, constraints, exceptions, sentence patterns, "
                "usage and collocations, vocabulary and supported word formation, pronunciation, exercises, reading strategies and "
                "writing advice from the transcript and frames. The teacher may explain in Chinese; retain the meaning faithfully in "
                "the evidence record. Do not write final lecture prose, silently polish examples, invent missing material, or use "
                "mathematical proof/theorem machinery. Return `diagram_materials` as an empty array. Use the program-fixed overlap "
                "boundary exactly, emit only genuinely new content after it, and use prior locked evidence only for continuity. "
                "Normally return `complete`; request fallback only for a specific materially ambiguous rule, example, correction, or "
                "written form that the supplied evidence cannot resolve. You have no outline authority."
            )
        payload_sessions: list[dict[str, Any]] = []
        reference_frames_by_session: dict[str, list[dict[str, Any]]] = {}
        for session in recording_sessions:
            session_id = str(session["session_id"])
            prior_reference = dict(session.get("prior_locked_reference") or {})
            reference_frames: list[dict[str, Any]] = []
            payload_reference_frames: list[dict[str, Any]] = []
            for reference_index, item in enumerate(
                prior_reference.get("keyframes") or [], start=1
            ):
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                attachment_name = str(
                    item.get("attachment_name")
                    or f"prior_locked_{session_id}_{reference_index:03d}.jpg"
                )
                reference_frames.append(
                    {
                        **item,
                        "path": path,
                        "attachment_name": attachment_name,
                        "evidence_role": "prior_locked_reference",
                    }
                )
                payload_reference_frames.append(
                    {
                        "time": str(item.get("time") or ""),
                        "attachment_name": attachment_name,
                        "evidence_role": "prior_locked_reference",
                    }
                )
            reference_frames_by_session[session_id] = reference_frames
            payload_sessions.append(
                {
                    **session,
                    "timestamped_audio_transcript": str(
                        session.get("timestamped_audio_transcript") or ""
                    ),
                    "candidate_frames": [
                        {
                            "index": int(item["index"]),
                            "time": str(item.get("time") or ""),
                            "attachment_name": str(item["attachment_name"]),
                            "change_type": str(item.get("change_type") or ""),
                            "changed_area_ratio": float(
                                item.get("changed_area_ratio") or 0.0
                            ),
                            "global_similarity": float(
                                item.get("global_similarity") or 0.0
                            ),
                            "stable_seconds": float(
                                item.get("stable_seconds") or 0.0
                            ),
                            "changed_regions": list(
                                item.get("changed_regions") or []
                            ),
                        }
                        for item in frames_by_session[session_id]
                    ],
                    "prior_locked_reference": {
                        "session_id": str(prior_reference.get("session_id") or ""),
                        "session_order": int(prior_reference.get("session_order") or 0),
                        "timestamped_audio_transcript": str(
                            prior_reference.get("timestamped_audio_transcript") or ""
                        ),
                        "mathematical_content_markdown": str(
                            prior_reference.get("mathematical_content_markdown") or ""
                        ),
                        "keyframes": payload_reference_frames,
                        "program_time_boundary": dict(
                            prior_reference.get("program_time_boundary") or {}
                        ),
                    },
                }
            )
        session_lookup = {
            str(item["session_id"]): item for item in payload_sessions
        }
        request_groups: list[list[str]] = []
        current_group: list[str] = []
        current_images = 0
        current_characters = 0
        ordered_sessions = sorted(
            recording_sessions,
            key=lambda item: (
                int(item.get("session_order") or 0),
                float(item.get("owned_start_video_time") or 0),
            ),
        )
        for session in ordered_sessions:
            session_id = str(session["session_id"])
            session_images = len(frames_by_session[session_id])
            session_images += len(reference_frames_by_session[session_id])
            session_characters = len(
                str(session.get("timestamped_audio_transcript") or "")
            )
            prior_reference = dict(session.get("prior_locked_reference") or {})
            session_characters += len(
                str(prior_reference.get("timestamped_audio_transcript") or "")
            ) + len(str(prior_reference.get("mathematical_content_markdown") or ""))
            exceeds = current_group and (
                current_images + session_images > self.EVIDENCE_REQUEST_MAX_IMAGES
                or current_characters + session_characters
                > self.EVIDENCE_REQUEST_MAX_TRANSCRIPT_CHARACTERS
            )
            if exceeds:
                request_groups.append(current_group)
                current_group = []
                current_images = 0
                current_characters = 0
            current_group.append(session_id)
            current_images += session_images
            current_characters += session_characters
        if current_group:
            request_groups.append(current_group)

        self.increment_request_count = 0
        all_results: dict[str, dict[str, Any]] = {}
        # Kept in the public signature for callers from older versions. Directory
        # data is intentionally excluded from every evidence-Agent request.
        base_outline_request = {"required": False}

        def parse_group_response(
            parsed: Any,
            group_ids: set[str],
            group_outline_request: dict[str, Any],
        ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
            if not isinstance(parsed, dict):
                raise RuntimeError("evidence response must be a JSON object")
            schema_version = parsed.get("schema_version")
            if schema_version is not None and int(schema_version) != self.EVIDENCE_RESPONSE_SCHEMA_VERSION:
                raise RuntimeError("evidence response has an unsupported schema_version")
            rows = parsed.get("sessions") if isinstance(parsed, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError("evidence response has no sessions array")
            results: dict[str, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError("evidence response contains a non-object session")
                session_id = str(row.get("session_id") or "")
                if session_id not in group_ids or session_id in results:
                    raise RuntimeError(
                        f"evidence response has an unknown or duplicate session {session_id}"
                    )
                session_definition = next(
                    item
                    for item in recording_sessions
                    if str(item.get("session_id") or "") == session_id
                )
                captured_start = float(
                    session_definition.get("captured_start_video_time")
                    if session_definition.get("captured_start_video_time") is not None
                    else session_definition.get("owned_start_video_time") or 0
                )
                captured_end = float(
                    session_definition.get("captured_end_video_time")
                    if session_definition.get("captured_end_video_time") is not None
                    else session_definition.get("owned_end_video_time") or captured_start
                )
                program_context_start = float(
                    session_definition.get("program_context_start_video_time")
                    if session_definition.get("program_context_start_video_time") is not None
                    else captured_start
                )
                program_new_start = float(
                    session_definition.get("program_new_content_start_video_time")
                    if session_definition.get("program_new_content_start_video_time") is not None
                    else captured_start
                )
                # Human recording can start late, seek backwards, pause, or cross
                # a continuation boundary imprecisely.  Normalize program hints to
                # the captured interval; never reject complete evidence over a
                # strict floating-point time assertion.
                if captured_end < captured_start:
                    captured_start, captured_end = captured_end, captured_start
                program_context_start = min(
                    captured_end, max(captured_start, program_context_start)
                )
                program_new_start = min(
                    captured_end, max(program_context_start, program_new_start)
                )
                prior_reference = dict(
                    session_definition.get("prior_locked_reference") or {}
                )
                prior_session_id = str(prior_reference.get("session_id") or "")
                resolution = row.get("overlap_resolution")
                if not isinstance(resolution, dict):
                    resolution = {}
                has_program_context = (
                    prior_session_id
                    and program_new_start > program_context_start + 0.05
                )
                if has_program_context:
                    resolution_status = "duplicate_prefix_removed"
                    compared_to = prior_session_id
                    duplicate_start = program_context_start
                    duplicate_end = program_new_start
                elif prior_session_id:
                    resolution_status = "no_duplicate"
                    compared_to = prior_session_id
                    duplicate_start = program_new_start
                    duplicate_end = program_new_start
                else:
                    resolution_status = "no_prior_session"
                    compared_to = ""
                    duplicate_start = captured_start
                    duplicate_end = captured_start
                new_content_start = duplicate_end
                expected_indexes = {
                    index
                    for index, owner in valid_indexes.items()
                    if owner == session_id
                }
                selected: list[dict[str, Any]] = []
                selected_indexes: set[int] = set()
                raw_selected = row.get("selected")
                raw_rejected = row.get("rejected", [])
                if not isinstance(raw_selected, list) or not isinstance(raw_rejected, list):
                    raise RuntimeError(
                        f"session {session_id} must return selected and rejected arrays"
                    )
                for item in raw_selected:
                    if isinstance(item, dict):
                        index = int(item.get("index") or 0)
                        reason = str(item.get("reason") or "").strip()
                    elif str(item).strip().isdigit():
                        index = int(item)
                        reason = ""
                    else:
                        continue
                    if index not in expected_indexes or index in selected_indexes:
                        continue
                    selected_indexes.add(index)
                    decision = {"candidate_index": index}
                    if reason:
                        decision["reason"] = reason
                    selected.append(decision)
                rejected_indexes = {
                    int(value)
                    for value in raw_rejected
                    if str(value).strip().isdigit()
                    and int(value) in expected_indexes
                }
                rejected_indexes.difference_update(selected_indexes)
                duplicate_groups: list[list[int]] = []
                rejected_covered: set[int] = set()
                for group in row.get("duplicate_groups") or []:
                    if not isinstance(group, list):
                        continue
                    values = [
                        int(value)
                        for value in group
                        if str(value).strip().isdigit()
                        and int(value) in expected_indexes
                    ]
                    if len(values) >= 2 and values[0] in selected_indexes:
                        duplicate_groups.append(values)
                        rejected_covered.update(values[1:])
                if rejected_indexes:
                    rejected_covered.update(rejected_indexes)
                frame_time_by_index = {
                    int(item["index"]): float(item.get("video_time") or 0)
                    for item in board_frames
                    if str(item.get("session_id") or "") == session_id
                }
                new_candidate_indexes = {
                    index
                    for index in expected_indexes
                    if frame_time_by_index.get(index, captured_end) >= new_content_start - 0.5
                }
                if new_candidate_indexes and not selected_indexes:
                    # Preserve one representative frame instead of failing the
                    # whole package because the model returned an empty selection.
                    index = max(new_candidate_indexes)
                    selected_indexes.add(index)
                    selected.append(
                        {
                            "candidate_index": index,
                            "reason": "retained automatically from the recorded evidence",
                        }
                    )
                markdown = str(row.get("mathematical_content_markdown") or "").strip()
                if not markdown or not re.search(
                    r"(?m)^##\s+\d{2}:\d{2}:\d{2}\b", markdown
                ):
                    raise RuntimeError(
                        f"session {session_id} has no timestamped mathematical content"
                    )
                deduplicated_transcript = str(
                    row.get("deduplicated_timestamped_transcript") or ""
                ).strip()
                if not deduplicated_transcript or not re.search(
                    r"(?m)^\[\d{2}:\d{2}:\d{2}\]", deduplicated_transcript
                ):
                    raise RuntimeError(
                        f"session {session_id} has no deduplicated timestamped transcript"
                    )

                def timestamp_seconds(value: str) -> int:
                    match = re.search(r"(\d{2}):(\d{2}):(\d{2})", value)
                    if not match:
                        return -1
                    return (
                        int(match.group(1)) * 3600
                        + int(match.group(2)) * 60
                        + int(match.group(3))
                    )

                # Earlier timestamps are legal continuity evidence.  Downstream
                # writable-range filtering still prevents accidental duplicate
                # imports, but evidence generation itself remains recoverable.
                evidence_status = str(row.get("evidence_status") or "").strip()
                if evidence_status not in {"complete", "needs_three_second_fallback"}:
                    raise RuntimeError(
                        f"session {session_id} has invalid evidence status"
                    )
                session_start = max(captured_start, new_content_start)
                session_end = captured_end
                fallback_ranges: list[dict[str, Any]] = []
                for item in row.get("fallback_ranges") or []:
                    if not isinstance(item, dict):
                        continue
                    gap_kind = str(item.get("gap_kind") or "").strip()
                    if gap_kind not in {
                        "conflicting_core_formula",
                        "unreadable_core_statement",
                    }:
                        continue
                    start = max(
                        session_start, float(item.get("start_video_time") or 0)
                    )
                    end = min(
                        session_end, float(item.get("end_video_time") or start)
                    )
                    if end <= start:
                        continue
                    fallback_ranges.append(
                        {
                            "start_video_time": start,
                            "end_video_time": end,
                            "gap_kind": gap_kind,
                            "missing_evidence": str(
                                item.get("missing_evidence") or ""
                            ).strip(),
                        }
                    )
                if evidence_status == "needs_three_second_fallback" and not fallback_ranges:
                    evidence_status = "complete"
                raw_uncertainties = row.get("uncertainties", [])
                if not isinstance(raw_uncertainties, list):
                    raise RuntimeError(
                        f"session {session_id} has invalid uncertainties"
                    )
                uncertainties: list[dict[str, Any]] = []
                for item in raw_uncertainties:
                    if not isinstance(item, dict):
                        raise RuntimeError(
                            f"session {session_id} has a non-object uncertainty"
                        )
                    detail = str(item.get("detail") or "").strip()
                    if not detail:
                        continue
                    uncertainties.append(
                        {
                            "time": str(item.get("time") or "").strip(),
                            "kind": str(item.get("kind") or "").strip(),
                            "detail": detail,
                            "materially_affects_mathematics": bool(
                                item.get("materially_affects_mathematics")
                            ),
                        }
                    )
                raw_diagram_materials = row.get("diagram_materials", [])
                if not isinstance(raw_diagram_materials, list):
                    raise RuntimeError(
                        f"session {session_id} has invalid diagram_materials"
                    )
                diagram_materials: list[dict[str, Any]] = []
                for item in raw_diagram_materials:
                    if not isinstance(item, dict):
                        raise RuntimeError(
                            f"session {session_id} has a non-object diagram material"
                        )
                    time_label = str(item.get("time") or "").strip()
                    title = str(item.get("title") or "").strip()
                    description = str(item.get("description") or "").strip()
                    seconds = timestamp_seconds(time_label)
                    if (
                        seconds < int(session_start - 1)
                        or seconds > int(session_end + 1)
                        or not title
                        or not description
                    ):
                        raise RuntimeError(
                            f"session {session_id} has an incomplete or out-of-range diagram material"
                        )
                    if any(
                        str(item.get(field) or "").strip()
                        for field in ("backend", "source", "latex")
                    ):
                        raise RuntimeError(
                            f"session {session_id} generated drawing source before "
                            "whole-lecture necessity review"
                        )
                    identity = json.dumps(
                        {
                            "session_id": session_id,
                            "time": time_label,
                            "title": title,
                            "description": description,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    diagram_materials.append(
                        {
                            "diagram_id": "diagram-"
                            + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                            "time": time_label,
                            "time_seconds": seconds,
                            "title": title,
                            "description": description,
                            "candidate_status": (
                                "pending_whole_lecture_necessity_review"
                            ),
                        }
                    )
                results[session_id] = {
                    "selected_indexes": sorted(selected_indexes),
                    "decisions": sorted(
                        selected, key=lambda item: int(item["candidate_index"])
                    ),
                    "duplicate_groups": duplicate_groups,
                    "mathematical_content_markdown": markdown,
                    "deduplicated_timestamped_transcript": deduplicated_transcript,
                    "overlap_resolution": {
                        "compared_to_session_id": compared_to,
                        "status": resolution_status,
                        "duplicate_prefix_start_video_time": duplicate_start,
                        "duplicate_prefix_end_video_time": duplicate_end,
                        "first_new_content_time": str(
                            resolution.get("first_new_content_time") or ""
                        ).strip(),
                        "repeated_content_summary": str(
                            resolution.get("repeated_content_summary") or ""
                        ).strip(),
                        "evidence": str(resolution.get("evidence") or "").strip(),
                    },
                    "evidence_status": evidence_status,
                    "fallback_reason": str(row.get("fallback_reason") or "").strip(),
                    "fallback_ranges": fallback_ranges,
                    "uncertainties": uncertainties,
                    "diagram_materials": diagram_materials,
                }
            missing_sessions = group_ids - set(results)
            if missing_sessions:
                raise RuntimeError(
                    "evidence response omitted recording sessions: "
                    + ", ".join(sorted(missing_sessions))
                )
            if "outline_segments" in parsed:
                emit(
                    "证据 Agent 返回了越权目录字段；该字段已被丢弃，目录由专门 Agent 负责。"
                )
            return results, []

        try:
            for group_index, group in enumerate(request_groups, start=1):
                group_ids = set(group)
                group_sessions = [session_lookup[session_id] for session_id in group]
                group_frames = [
                    item
                    for item in board_frames
                    if str(item.get("session_id") or "") in group_ids
                ]
                group_reference_frames = [
                    item
                    for session_id in group_ids
                    for item in reference_frames_by_session.get(session_id, [])
                ]
                group_outline_request = dict(base_outline_request)
                if bool(base_outline_request.get("required")):
                    requested_start = float(
                        base_outline_request.get("coverage_start_seconds") or 0
                    )
                    requested_end = float(
                        base_outline_request.get("coverage_end_seconds") or 0
                    )
                    group_start = max(
                        requested_start,
                        min(
                            float(
                                item.get("captured_start_video_time")
                                if item.get("captured_start_video_time") is not None
                                else item.get("owned_start_video_time") or 0
                            )
                            for item in group_sessions
                        ),
                    )
                    group_end = min(
                        requested_end,
                        max(
                            float(
                                item.get("captured_end_video_time")
                                if item.get("captured_end_video_time") is not None
                                else item.get("owned_end_video_time") or group_start
                            )
                            for item in group_sessions
                        ),
                    )
                    group_outline_request.update(
                        required=group_end > group_start + 0.5,
                        coverage_start_seconds=group_start,
                        coverage_end_seconds=group_end,
                        existing_outline=[
                            *list(base_outline_request.get("existing_outline") or []),
                        ],
                    )
                payload = {
                    **course_info,
                    "workflow": "bounded_session_safe_evidence_reconstruction",
                    "response_schema": self.EVIDENCE_RESPONSE_SCHEMA,
                    "recording_sessions": group_sessions,
                }
                answer = self._run(
                    payload,
                    self._attachments([*group_frames, *group_reference_frames]),
                    system_prompt,
                    emit,
                    stage=(
                        "AI 新增录制一次处理（数学内容与最终关键帧）"
                        if len(request_groups) == 1
                        else f"AI 新增录制有界处理 {group_index}/{len(request_groups)}（数学内容与最终关键帧）"
                    ),
                )
                self.increment_request_count += 1
                group_results, group_outline = parse_group_response(
                    _parse_json_response(answer), group_ids, group_outline_request
                )
                all_results.update(group_results)
            return all_results, [], ""
        except Exception as error:
            return {}, [], str(error)
