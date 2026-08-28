from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import is_dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from shared.scripts.ai_agent_config import AiAgentSettingsStore
from shared.scripts.ai_agent_providers import create_provider
from shared.scripts.online_course_agent_prompts import (
    DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
    load_course_agent_instructions,
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
    REQUIRED_MODEL = "gpt-5.6-sol"
    REQUIRED_REASONING_EFFORT = "medium"
    EVIDENCE_REQUEST_MAX_IMAGES = 24
    EVIDENCE_REQUEST_MAX_TRANSCRIPT_CHARACTERS = 60000
    TARGETED_REVIEW_MAX_IMAGES = 16
    RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION = 1
    RECORDING_WINDOW_OUTPUT_TOKENS = 4500
    FINAL_SYNTHESIS_OUTPUT_TOKENS = 4500
    FINAL_SYNTHESIS_PART_OUTPUT_TOKENS = 8000
    # Some relay gateways return HTTP 524 before a large, reasoning-heavy
    # request reaches the user's configured client timeout.  Five recording
    # windows keep the ordinary path below that upstream budget while avoiding
    # one serial request per five-minute window.  A failed multi-window request
    # is bisected until the existing single-window safe path is reached.
    FINAL_SYNTHESIS_WINDOWS_PER_REQUEST = 5
    FINAL_SYNTHESIS_PARALLEL_REQUESTS = 1
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
        self.store = AiAgentSettingsStore()
        self.profile = self._highest_configured_profile()
        self.api_key = self.store.resolve_api_key(self.profile)
        request_profile = self.profile
        if is_dataclass(self.profile):
            # Keep the user's configured ceiling here.  Individual image and
            # catalogue stages apply their own small budgets in ``_run``, while
            # the final text-only synthesis may legitimately take much longer.
            # Clamping it here made a configured 600-second relay timeout
            # unreachable and caused long lectures to fail after 105 seconds.
            overrides: dict[str, Any] = {
                "timeout_seconds": int(
                    getattr(self.profile, "timeout_seconds", self.REQUEST_TIMEOUT_SECONDS)
                )
            }
            # The UI-only ``adaptive`` effort is resolved to a documented
            # tier for the evidence pipeline, while the configured model and
            # native Responses protocol remain deterministic.
            if (
                str(getattr(self.profile, "provider_kind", ""))
                == "openai_responses"
                and str(getattr(self.profile, "model", "")).casefold()
                == self.REQUIRED_MODEL
            ):
                overrides.update(
                    provider_kind="openai_responses",
                    routing_strategy="fixed",
                    reasoning_effort="medium",
                    stream_responses=False,
                    transport_retries=2,
                )
            request_profile = replace(self.profile, **overrides)
        self.request_profile = request_profile
        self.provider = create_provider(request_profile, self.api_key)
        self._successful_evidence_requests = 0
        self._course_context: dict[str, Any] = {}
        self._course_storage_root = DEFAULT_ONLINE_COURSE_STORAGE_ROOT

    def configure_course_context(
        self,
        course: dict[str, Any],
        *,
        storage_root: Path = DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
    ) -> None:
        self._course_context = {
            "course_id": int(course.get("id") or course.get("course_id") or 0),
            "course_code": str(course.get("course_code") or "").strip(),
            "course_title": str(
                course.get("title") or course.get("course_title") or ""
            ).strip(),
        }
        self._course_storage_root = Path(storage_root)

    def _persistent_course_instructions(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        context = dict(getattr(self, "_course_context", {}) or {})
        return load_course_agent_instructions(
            course_id=int(payload.get("course_id") or context.get("course_id") or 0),
            course_code=str(
                payload.get("course_code") or context.get("course_code") or ""
            ),
            course_title=str(
                payload.get("course_title") or context.get("course_title") or ""
            ),
            storage_root=Path(
                getattr(
                    self,
                    "_course_storage_root",
                    DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
                )
            ),
        )

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

    @staticmethod
    def _gateway_preflight_diagnostic(profile: Any, error: Exception) -> str:
        """Explain relay-edge failures that happen before a model sees the request."""
        base_url = str(getattr(profile, "base_url", "") or "").casefold()
        status_code = int(getattr(error, "status_code", 0) or 0)
        error_text = str(error).casefold()
        gateway_edge_forbidden = bool(
            base_url
            and status_code == 403
            and (
                "nginx" in error_text
                or "<html" in error_text
                or "403 forbidden" in error_text
            )
        )
        if not gateway_edge_forbidden:
            return ""
        return (
            "配置的 API 网关在模型处理请求前返回了 nginx HTTP 403："
            "当前代理节点/出口 IP 被网关拒绝。这不是 Responses、图片、"
            "推理参数或模型能力不支持；请切换到能够访问该 API 网关"
            "的代理节点后重新开始录制"
        )

    def _preflight_route_candidates(self) -> list[Any]:
        primary = self.request_profile
        if not is_dataclass(primary):
            return [primary]
        provider_kind = str(getattr(primary, "provider_kind", "") or "")
        model = str(getattr(primary, "model", "") or "")
        if (
            model.casefold() != self.REQUIRED_MODEL
            or provider_kind != "openai_responses"
        ):
            raise RuntimeError(
                "网课录制期子 Agent 与结束阶段主 Agent 只允许使用 "
                f"{self.REQUIRED_MODEL} / {self.REQUIRED_REASONING_EFFORT} / "
                "openai_responses；"
                f"当前配置为 {model or 'unknown'} / {provider_kind or 'unknown'}。"
            )
        return [primary]

    def preflight(self, emit: Callable[[str], None]) -> dict[str, Any]:
        """Verify the configured text route before sending heavy visual evidence."""
        marker = "ONLINE_COURSE_API_OK"
        started = time.monotonic()
        routes = self._preflight_route_candidates()
        total_attempts = 0
        routes_tried: list[dict[str, str]] = []
        last_error: Exception | None = None
        for route_profile in routes:
            preflight_profile = route_profile
            if is_dataclass(route_profile):
                preflight_profile = replace(
                    route_profile,
                    timeout_seconds=int(
                        getattr(route_profile, "timeout_seconds", 600)
                    ),
                    reasoning_effort=self.REQUIRED_REASONING_EFFORT,
                    max_output_tokens=32,
                    stream_responses=False,
                )
            model = str(getattr(preflight_profile, "model", "unknown") or "unknown")
            protocol = str(
                getattr(
                    preflight_profile,
                    "provider_kind",
                    "configured provider",
                )
            )
            routes_tried.append({"model": model, "protocol": protocol})
            try:
                emit(
                    "【网课 Agent API 预检】"
                    f"模型：{model}；协议：{protocol}；"
                    f"文本请求，无附件，{getattr(preflight_profile, 'timeout_seconds', 600)} 秒上限。"
                )
            except (OSError, UnicodeError):
                pass
            provider = create_provider(preflight_profile, self.api_key)
            route_attempts = self.PREFLIGHT_ATTEMPTS
            for route_attempt in range(1, route_attempts + 1):
                total_attempts += 1
                try:
                    result = provider.run_turn(
                        [
                            {
                                "role": "user",
                                "content": (
                                    f"Return exactly {marker}. Do not add punctuation, "
                                    "Markdown, explanation, or any other text."
                                ),
                                "attachments": [],
                            }
                        ],
                        (
                            "This is a connectivity preflight. Follow the user's "
                            "exact output contract."
                        ),
                        [],
                        lambda _name, _arguments: {
                            "ok": False,
                            "error": "Tools are disabled during API preflight.",
                        },
                        lambda _message: None,
                    )
                    answer = str(result.answer or "").strip()
                    if answer != marker:
                        raise RuntimeError(
                            "网课 Agent API 预检返回了非预期内容："
                            f"期望 {marker}，实际 {answer[:160]!r}。"
                            "未发送课程转写或图片。"
                        )
                    self.request_profile = route_profile
                    self.provider = create_provider(route_profile, self.api_key)
                    self._successful_evidence_requests = 0
                    elapsed = time.monotonic() - started
                    timing = {
                        "stage": "网课 Agent API 预检",
                        "seconds": round(elapsed, 3),
                        "attempts": total_attempts,
                        "routes_tried": routes_tried,
                        "selected_model": model,
                        "selected_protocol": protocol,
                        "attachment_count": 0,
                        "response_characters": len(answer),
                        "response_model": str(
                            getattr(result, "response_model", "") or ""
                        ),
                        "usage": dict(getattr(result, "usage", {}) or {}),
                    }
                    self.stage_timings.append(timing)
                    try:
                        emit(
                            f"网课 Agent API 预检通过：{elapsed:.1f} 秒；"
                            f"已锁定模型 {timing['response_model'] or model}，"
                            f"协议 {protocol}。后续单次正确性审核与统一生成"
                            "使用该路由。"
                        )
                    except (OSError, UnicodeError):
                        pass
                    return timing
                except Exception as error:
                    last_error = error
                    break

        elapsed = time.monotonic() - started
        diagnostic = self._gateway_preflight_diagnostic(
            self.request_profile,
            last_error or RuntimeError("no response"),
        )
        error_detail = diagnostic or str(last_error or "no response")
        timing = {
            "stage": "网课 Agent API 预检",
            "seconds": round(elapsed, 3),
            "attempts": total_attempts,
            "routes_tried": routes_tried,
            "attachment_count": 0,
            "response_characters": 0,
            "error": error_detail,
        }
        self.stage_timings.append(timing)
        raise RuntimeError(
            f"网课 Agent API 预检失败（{elapsed:.1f} 秒，共尝试 "
            f"{total_attempts} 次、{len(routes_tried)} 条路由）："
            f"{error_detail}。未发送课程转写或图片。"
        ) from last_error

    def _highest_configured_profile(self) -> Any:
        configured = []
        for candidate in self.store.profiles:
            try:
                available = (not candidate.requires_api_key) or bool(
                    self.store.resolve_api_key(candidate)
                )
            except (OSError, ValueError):
                available = False
            if available:
                configured.append(candidate)
        if not configured:
            configured = [self.store.active_profile()]

        required = [
            profile
            for profile in configured
            if str(profile.model or "").casefold() == self.REQUIRED_MODEL
            and str(
                getattr(profile, "provider_kind", "openai_responses")
                or "openai_responses"
            )
            == "openai_responses"
        ]
        if not required:
            raise RuntimeError(
                "网课录制期子 Agent 与结束阶段主 Agent 只允许使用 "
                f"{self.REQUIRED_MODEL} / {self.REQUIRED_REASONING_EFFORT} / "
                "openai_responses；"
                "当前没有可用的对应模型配置。"
            )
        return max(
            required,
            key=lambda profile: int(profile.id == self.store.active_profile_id),
        )

    def _run(
        self,
        payload: dict[str, Any],
        attachments: list[dict[str, Any]],
        system_prompt: str,
        emit: Callable[[str], None],
        *,
        stage: str,
    ) -> str:
        def safe_emit(message: str) -> None:
            try:
                emit(message)
            except (OSError, UnicodeError):
                # Diagnostics must never turn a valid model response into a
                # failed evidence stage (for example on a GBK console).
                return

        # Recording-time child requests must stay small. Course-wide catalogue
        # and reference instructions belong only to the final lead request.
        persistent = (
            {}
            if stage.startswith("录制期窗口")
            else self._persistent_course_instructions(payload)
        )
        if str(persistent.get("text") or ""):
            system_prompt = (
                system_prompt.rstrip()
                + "\n\n"
                + str(persistent["text"])
            )
            payload = {
                **payload,
                "persistent_course_instruction_files": list(
                    persistent.get("files") or []
                ),
            }

        stage_profile = self.request_profile
        stage_provider = self.provider
        if is_dataclass(self.request_profile):
            transcript_length = len(str(payload.get("raw_timestamped_transcript") or ""))
            if not transcript_length:
                transcript_length = sum(
                    len(str(item.get("timestamped_audio_transcript") or ""))
                    for item in payload.get("recording_sessions") or []
                    if isinstance(item, dict)
                )
            coverage_audit = "窗口完整性审计" in stage
            recording_window_stage = "录制期窗口" in stage
            bounded_dedup = (
                "关键帧批次内去重" in stage or "去重清单纠错" in stage
                or coverage_audit
            )
            single_pass_increment = (
                "新增录制" in stage and "数学内容与最终关键帧" in stage
            )
            final_window_synthesis = (
                "录制段数学正确性审核与统一生成" in stage
            )
            if recording_window_stage:
                stage_output_tokens = min(
                    int(getattr(self.request_profile, "max_output_tokens", 4500)),
                    self.RECORDING_WINDOW_OUTPUT_TOKENS,
                )
            elif coverage_audit:
                stage_output_tokens = min(
                    int(getattr(self.request_profile, "max_output_tokens", 6000)),
                    6000,
                )
            elif single_pass_increment:
                stage_output_tokens = min(
                    int(getattr(self.request_profile, "max_output_tokens", 48000)),
                    max(18000, transcript_length * 2),
                )
            elif final_window_synthesis:
                stage_output_tokens = min(
                    int(getattr(self.request_profile, "max_output_tokens", 12000)),
                    (
                        self.FINAL_SYNTHESIS_PART_OUTPUT_TOKENS
                        if "文本分块" in stage
                        else self.FINAL_SYNTHESIS_OUTPUT_TOKENS
                    ),
                )
            elif "截图价值判断" in stage or "板书" in stage:
                stage_output_tokens = min(
                    int(getattr(self.request_profile, "max_output_tokens", 12000)),
                    8000 if bounded_dedup else (12000 if "板书" in stage else 6000),
                )
            else:
                stage_output_tokens = min(
                    int(getattr(self.request_profile, "max_output_tokens", 48000)),
                    max(6000, transcript_length * 2),
                )
            configured_timeout_seconds = int(
                getattr(self.request_profile, "timeout_seconds", self.REQUEST_TIMEOUT_SECONDS)
            )
            stage_profile = replace(
                self.request_profile,
                model=self.REQUIRED_MODEL,
                provider_kind="openai_responses",
                reasoning_effort=self.REQUIRED_REASONING_EFFORT,
                max_output_tokens=stage_output_tokens,
                timeout_seconds=configured_timeout_seconds,
                # Retry the same synchronous relay request twice, but never
                # switch model, protocol, or transport.
                transport_retries=2,
                stream_responses=False,
            )
            stage_provider = create_provider(stage_profile, self.api_key)
        audit_payload = dict(payload)
        transcript = str(audit_payload.get("raw_timestamped_transcript") or "")
        if transcript:
            audit_payload["raw_timestamped_transcript"] = {
                "characters_sent": len(transcript),
                "preview": transcript[:1600],
                "note": "The complete timestamped transcript was sent; the UI preview is shortened to remain responsive.",
            }
        audit_sessions: list[dict[str, Any]] = []
        for item in audit_payload.get("recording_sessions") or []:
            if not isinstance(item, dict):
                continue
            audit_item = dict(item)
            session_transcript = str(
                audit_item.get("timestamped_audio_transcript") or ""
            )
            if session_transcript:
                audit_item["timestamped_audio_transcript"] = {
                    "characters_sent": len(session_transcript),
                    "preview": session_transcript[:1600],
                    "note": (
                        "The complete session transcript was sent; only this UI "
                        "preview is shortened."
                    ),
                }
            audit_sessions.append(audit_item)
        if audit_sessions:
            audit_payload["recording_sessions"] = audit_sessions
        board_reference = str(audit_payload.get("ai_transcribed_board_reference") or "")
        if board_reference:
            audit_payload["ai_transcribed_board_reference"] = {
                "characters_sent": len(board_reference),
                "preview": board_reference[:1200],
                "note": "The complete AI-transcribed board reference was sent; this UI preview is shortened.",
            }
        safe_emit(
            f"【实际提示词 · {stage}】\n"
            f"SYSTEM\n{system_prompt}\n\nUSER PAYLOAD\n"
            + json.dumps(audit_payload, ensure_ascii=False, indent=2)
            + ("\n\nATTACHMENTS\n" + "\n".join(item["name"] for item in attachments) if attachments else "")
        )
        safe_emit(
            f"【调用配置 · {stage}】\n"
            f"模型：{getattr(stage_profile, 'model', 'unknown')}\n"
            f"协议：{getattr(stage_profile, 'provider_kind', 'configured provider')}\n"
            f"推理强度：{getattr(stage_profile, 'reasoning_effort', 'provider default')}\n"
            f"最大输出：{getattr(stage_profile, 'max_output_tokens', 'provider default')} tokens\n"
            f"单次请求超时：{getattr(stage_profile, 'timeout_seconds', 'provider default')} 秒\n"
            f"连接级重试：{getattr(stage_profile, 'transport_retries', 0)} 次\n"
            "响应传输：同步 JSON（固定）\n"
            f"转写输入：{transcript_length if is_dataclass(self.request_profile) else len(transcript)} 字符\n"
            f"附件：{len(attachments)} 个"
        )
        last_error: Exception | None = None
        stage_started = time.monotonic()
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                result = stage_provider.run_turn(
                    [
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                            "attachments": attachments,
                        }
                    ],
                    system_prompt,
                    [],
                    lambda _name, _arguments: {
                        "ok": False,
                        "error": "The online-course evidence agent cannot call tools.",
                    },
                    safe_emit,
                )
                answer = str(result.answer or "")
                preview = answer[:12000]
                if len(answer) > len(preview):
                    preview += f"\n\n[UI preview truncated; full model output contains {len(answer)} characters.]"
                safe_emit(f"【模型返回 · {stage}】\n{preview}")
                elapsed = time.monotonic() - stage_started
                timing = {
                    "stage": stage,
                    "seconds": round(elapsed, 3),
                    "attempts": attempt,
                    "attachment_count": len(attachments),
                    "response_characters": len(answer),
                    "response_model": str(
                        getattr(result, "response_model", "") or ""
                    ),
                    "usage": dict(getattr(result, "usage", {}) or {}),
                }
                timings = getattr(self, "stage_timings", None)
                if isinstance(timings, list):
                    timings.append(timing)
                safe_emit(
                    f"【阶段耗时 · {stage}】{elapsed:.1f} 秒；"
                    f"请求 {attempt} 次；附件 {len(attachments)} 个；"
                    f"返回 {len(answer)} 字符。"
                )
                self._successful_evidence_requests = (
                    int(
                        getattr(
                            self,
                            "_successful_evidence_requests",
                            0,
                        )
                        or 0
                    )
                    + 1
                )
                return answer
            except Exception as error:
                last_error = error
                if attempt < self.REQUEST_ATTEMPTS:
                    safe_emit(
                        f"{stage} 第 {attempt} 次请求未成功：{error}\n"
                        f"将在 {attempt * 2} 秒后自动重试（共 {self.REQUEST_ATTEMPTS} 次）。"
                    )
                    time.sleep(attempt * 2)
        elapsed = time.monotonic() - stage_started
        timings = getattr(self, "stage_timings", None)
        if isinstance(timings, list):
            timings.append(
                {
                    "stage": stage,
                    "seconds": round(elapsed, 3),
                    "attempts": self.REQUEST_ATTEMPTS,
                    "attachment_count": len(attachments),
                    "timeout_seconds": getattr(stage_profile, "timeout_seconds", None),
                    "transport_retries": getattr(stage_profile, "transport_retries", None),
                    "response_characters": 0,
                    "error": str(last_error or "no response"),
                }
            )
        if last_error is not None and int(getattr(last_error, "status_code", 0) or 0) == 502:
            raise RuntimeError(
                "中转站已接收到请求，但其模型上游返回 HTTP 502。"
                "网课 Agent 已使用兼容的固定 Responses 配置并自动重试；"
                "若仍失败，则属于本次上游服务异常，并非 API 密钥未接入。"
            ) from last_error
        if last_error is not None and (
            "timeout" in str(last_error).casefold() or "超时" in str(last_error)
        ):
            if int(getattr(last_error, "status_code", 0) or 0):
                # Preserve typed upstream HTTP failures so callers can distinguish
                # a gateway timeout from a local request deadline without causing
                # a second model request or route failover.
                raise last_error
            raise RuntimeError(
                f"{stage} 在单次 {getattr(stage_profile, 'timeout_seconds', 'configured')} 秒"
                "请求时限内未收到模型完成响应；"
                f"连接级自动重试 {getattr(stage_profile, 'transport_retries', 0)} 次后仍失败。"
            ) from last_error
        raise last_error or RuntimeError("The evidence agent returned no response.")

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
    ) -> list[dict[str, Any]]:
        subsection_capacity_seconds = 10 * 60.0
        duration_segment_limit = max(
            1,
            int(
                max(0.0, float(coverage_end) - float(coverage_start))
                // subsection_capacity_seconds
            )
            + (1 if existing_outline else 0),
        )
        segment_limit = (
            duration_segment_limit
            if maximum_segments is None
            else min(duration_segment_limit, max(1, int(maximum_segments)))
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
            "Then partition that map into coherent writing units. A lone definition, theorem, example, or proof step does "
            "not automatically force a boundary; however, a sustained cluster organized around a new definition or "
            "construction, a new class of examples, a new property, a theorem with its proof, or a new mathematical goal "
            "is normally its own Subsection. Treat transitions such as basic examples to separation/countability, local "
            "structure to compactness, construction to classification, or statement to a different theorem family as "
            "strong boundary evidence even when the parent Section stays the same. "
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
            "Do not collapse several coherent mathematical subtopics into one subsection merely "
            "because they share a Section; start a new subsection, Section, or Chapter when the mathematical scope "
            "actually changes. Avoid omnibus titles joined from unrelated concepts. Before returning, verify that every "
            "substantial item in the internal content map belongs to exactly one focused segment, every boundary has a "
            "content-based reason, every reused ID is a direct continuation, and every new Subsection number is unused "
            "inside its Chapter/Section. `prohibited_boundary_times` lists internal evidence-processing window endpoints. "
            f"Assign at most {segment_limit} distinct Subsections in this {float(coverage_end) - float(coverage_start):.3f}-second "
            "range. More importantly, every distinct formal Subsection must own at least 600 seconds of 1x lecture "
            "evidence after all of its segments are accumulated. Disjoint segments and cross-episode continuations "
            "assigned to the same stable existing Subsection contribute to the same total; use the supplied "
            "`accumulated_lecture_seconds` when continuing an existing ID. A short definition, theorem, example, "
            "exercise cluster, transition, or final tail is not allowed to become its own Subsection merely because "
            "it has a genuine local boundary. Place it inside the mathematically closest coherent adjacent writing "
            "unit and choose a focused title broad enough to cover both. Before returning, calculate the accumulated "
            "duration of every proposed Subsection and verify that each is at least 600 seconds. Never use a "
            "recording-window boundary as the reason for a split "
            "or merge, and avoid crossing a genuine Chapter/Section transition. "
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
                "direct continuation of the same focused teaching unit; shared vocabulary is not enough. Every formal Subsection must "
                "accumulate at least 600 seconds of 1x lecture evidence; merge a short topic or final tail into the mathematically closest "
                f"coherent adjacent unit, and return at most {segment_limit} distinct subsections. Never place an internal endpoint at a prohibited evidence-window "
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
            "subsection_capacity_seconds": subsection_capacity_seconds,
            "minimum_new_subsection_seconds": subsection_capacity_seconds,
            "raw_timestamped_transcript": transcript[:220000],
            "ai_transcribed_board_reference": board_markdown[:80000],
        }
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
        return normalized

    def repair_lecture_outline_titles(
        self,
        course_info: dict[str, Any],
        segments: list[dict[str, Any]],
        emit: Callable[[str], None],
    ) -> list[dict[str, Any]]:
        """Repair title language without allowing the model to alter evidence.

        The evidence pass occasionally follows the source language even though
        the application-owned LaTeX outline requires English titles.  This
        bounded pass may change only the three title fields; all numbering,
        timing, assignment, and evidence are copied from the original rows.
        """
        if not segments:
            return []
        system_prompt = (
            "You repair lecture-outline title language for a university mathematics course. "
            "Return ONLY JSON with key `segments`, one item for each input item and in the same order. "
            "Translate or rewrite ONLY `chapter_title`, `section_title`, and `subsection_title` into concise "
            "academic English. Do not translate word-for-word when a standard mathematical English title is clearer. "
            "Write every mathematical symbol or expression as inline LaTeX delimited by single dollar signs, "
            "for example `$L^1$`, `$L^p$`, or `$W^{1,p}$`; never return bare `^` or `_` outside math mode. "
            "Copy every numeric field, `end_video_time`, `existing_subsection_id`, `assignment`, and `evidence` exactly. "
            "Do not add, remove, split, merge, reorder, or retime segments. Every title must contain no Chinese characters. "
            "The source-language titles are retained by the application for audit and must not be returned in place of English titles."
        )
        answer = self._run(
            {**course_info, "segments": segments},
            [],
            system_prompt,
            emit,
            stage="AI 讲义目录标题修复",
        )
        parsed = _parse_json_response(answer)
        rows = parsed.get("segments") if isinstance(parsed, dict) else None
        if not isinstance(rows, list) or len(rows) != len(segments):
            raise RuntimeError("outline title repair returned an invalid segment count")
        repaired: list[dict[str, Any]] = []
        for original, candidate in zip(segments, rows):
            if not isinstance(candidate, dict):
                raise RuntimeError("outline title repair returned a non-object segment")
            merged = dict(original)
            for key in ("chapter_title", "section_title", "subsection_title"):
                value = str(candidate.get(key) or "").strip()
                if not value:
                    raise RuntimeError(f"outline title repair omitted {key}")
                merged[key] = value
            repaired.append(merged)
        return repaired

    def inspect_reference_material_pages(
        self,
        course_info: dict[str, Any],
        pages: list[dict[str, Any]],
        emit: Callable[[str], None],
    ) -> list[dict[str, Any]]:
        """Read at most four scanned reference pages as actual visual evidence."""
        if not pages:
            return []
        if len(pages) > 4:
            raise ValueError("一次参考资料视觉读取最多允许 4 页。")
        page_numbers = [int(item.get("page_number") or 0) for item in pages]
        system_prompt = (
            "You are visually reading scanned pages from a mathematics-course reference, not merely reviewing OCR. "
            "Read every attached page image directly and return ONLY JSON with key `pages`. Return exactly one item "
            "per supplied page, in the same order. Each item must contain integer `page_number`, string "
            "`visible_heading`, string `topic_path`, boolean `starts_new_coherent_part`, string "
            "`boundary_reason`, string `continues_from_previous`, string `continues_to_next`, array "
            "`content_inventory`, array `section_starts`, boolean `continues_existing_section_at_page_start`, "
            "and string `uncertainty`. `section_starts` must list every distinct document section or subsection "
            "whose heading and content begin on this page, in page order. Do not put chapter titles, theorem names, "
            "examples, proof labels, or informal topic markers in `section_starts`. Set "
            "`continues_existing_section_at_page_start` only when substantive content before the first new section "
            "continues a section begun on an earlier page. `content_inventory` must name every visible kind of "
            "mathematical content needed for later partitioning (definitions, theorem/proof, derivation, example, "
            "exercise, warning, table or diagram), but it is not a replacement transcription. OCR text is only a "
            "fallible navigation hint: formulas, symbols, headings and page structure must be decided from the image. "
            "Set `starts_new_coherent_part` on every page containing the start of a real reference-document section or "
            "subsection, even when that page begins by finishing the preceding section. A chapter containing several "
            "sections is not one part. Never set it merely because this request starts a "
            "new four-page batch. Keep a theorem with its proof and an example with the statement it "
            "illustrates whenever the visible continuity supports that. Use `previous_page_structure` to judge the first "
            "page's continuity with the preceding batch. Explicitly report uncertainty instead of guessing. "
            "Do not add proof obligations, missing-proof markers, exercises, or mathematical claims that are not visibly "
            "present on the supplied page. Preserve exact notation and report uncertainty rather than repairing formulas."
        )
        if self._english_course(course_info):
            system_prompt = (
                "You are visually reading scanned pages from an English-learning reference, not merely reviewing OCR. Read every "
                "attached page directly and return ONLY JSON with key `pages`, exactly one item per page in input order. Preserve the "
                "existing schema: page_number, visible_heading, topic_path, starts_new_coherent_part, boundary_reason, "
                "continues_from_previous, continues_to_next, content_inventory, section_starts, "
                "continues_existing_section_at_page_start, and uncertainty. Identify real source sections and inventories such as "
                "grammar rules, examples, intentional errors/corrections, exercises, vocabulary or word formation, pronunciation, "
                "reading strategy, writing principle, tables and diagrams. OCR is navigation help only; decide text, headings and page "
                "structure from the image. Keep examples verbatim and report uncertainty rather than silently correcting them."
            )
        payload = {
            **course_info,
            "workflow": "visual_reference_page_structure_reading",
            "page_numbers": page_numbers,
            "pages": [
                {
                    "page_number": int(item.get("page_number") or 0),
                    "locator": str(item.get("locator") or ""),
                    "extraction_method": str(item.get("extraction_method") or ""),
                    "ocr_confidence": item.get("ocr_confidence"),
                    "ocr_error": str(item.get("ocr_error") or ""),
                    "ocr_navigation_hint": str(item.get("extracted_text") or ""),
                    "attachment_name": str(item.get("attachment_name") or ""),
                }
                for item in pages
            ],
        }
        answer = self._run(
            payload,
            self._attachments(pages),
            system_prompt,
            emit,
            stage="AI 参考资料扫描页视觉读取",
        )
        parsed = _parse_json_response(answer)
        rows = parsed.get("pages") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("参考资料视觉读取 Agent 没有返回 pages。")
        normalized = [dict(item) for item in rows if isinstance(item, dict)]
        returned_numbers = [int(item.get("page_number") or 0) for item in normalized]
        if returned_numbers != page_numbers:
            raise RuntimeError("参考资料视觉读取 Agent 没有按输入顺序逐页返回。")
        return normalized

    def plan_reference_material_parts(
        self,
        course_info: dict[str, Any],
        chunks: list[dict[str, Any]],
        emit: Callable[[str], None],
    ) -> list[dict[str, Any]]:
        """Group immutable extracted chunks without rewriting their contents."""
        if not chunks:
            return []
        outline_contract = _reference_outline_contract(
            str(course_info.get("outline_mode") or "") == "reference_section"
        )
        system_prompt = (
            "You perform the document-level partition of one imported mathematics-course reference. The mandatory "
            "granularity is exactly one reference-document SECTION per part. Never create a part for a numbered "
            "subsection such as 1.2.1 or 1.2.2; those headings remain content inside section 1.2. The output title "
            "must be the exact two-level source section number followed by its concise academic-English translation, "
            "for example `1.1 Filter Bases and Limits Along Them`. Preliminary material before the first numbered "
            "section belongs to that first section. This is a source-document partition pass, not a course-mapping "
            "pass: the course may have only a few recorded subsections today and many more in the future. Never infer "
            "future applicability and never map a source part to an episode or subsection in this pass. "
            "Return ONLY JSON with key `parts`. "
            "Each part must contain `title`, `chunk_ids`, `target_episode_numbers`, "
            "`target_subsection_titles`, and `mapping_reason`. Every supplied chunk_id must occur at least once, and the "
            "ordered sequence must contain every supplied chunk exactly once after adjacent shared-boundary duplicates "
            "are collapsed; never omit, rewrite, summarize, or quote chunk "
            "content. A PDF page is indivisible. When one page finishes one section and starts the next, repeat that page's "
            "chunk_id in both adjacent parts. Adjacent parts may share at most one complete PDF page, and the shared page "
            "must be the earlier part's last page and the later part's first page. If several explicit sections start on "
            "the same page, create separate adjacent parts and repeat that same page chunk for each section. Never use "
            "overlap for ordinary context or repeat any other page. "
            "Treat the complete ordered chunk list as one document. Never create a boundary merely because the caller "
            "used a processing batch. For scanned pages, `visual_structure` comes from direct inspection of the attached "
            "page image and is stronger boundary evidence than OCR; OCR remains useful only as a fallible search hint. "
            "Honor every genuine section transition marked by `starts_new_coherent_part`. A `visible_heading` "
            "or `topic_path` listing several numbered sections requires separate parts even though their PDF page is shared. "
            "Never merge several visible sections merely because the complete PDF or chapter is small. If the source has no explicit section "
            "labels, use the smallest coherent topical unit equivalent to one lecture section. Avoid fragments smaller than "
            "that unit and keep a statement with its proof, derivation, examples and immediate remarks. Use a specific "
            "mathematical title, not labels such as 'continued material', 'unreadable pages', or a raw page range. "
            "Both target arrays must always be empty. Mapping is performed later, one formal course subsection at a "
            "time, from the stable section catalog produced here. `mapping_reason` must explain only the source "
            "section boundary/title decision, never course applicability. "
            + self.MATHEMATICAL_TRAINING
            + outline_contract
        )
        if self._english_course(course_info):
            system_prompt = (
                "Partition one imported English-learning reference at exactly the source-document SECTION level. Return ONLY JSON "
                "with key `parts`; each part has `title`, `chunk_ids`, empty `target_episode_numbers`, empty "
                "`target_subsection_titles`, and `mapping_reason`. Preserve every supplied chunk exactly once after adjacent shared-page "
                "duplicates are collapsed. A page starting a new section may be shared only as the previous part's final and next part's "
                "first page. Never rewrite, summarize, omit, or pre-map content to future course units. Use exact source section numbers "
                "and concise academic-English titles when available. Visual structure outranks OCR for scans. Keep each rule with its "
                "conditions/examples and each exercise with its explanation when source continuity supports it. "
                + outline_contract
            )
        payload = {
            **course_info,
            "workflow": "lossless_reference_material_section_partition_only",
            "chunks": chunks,
        }
        answer = self._run(
            payload,
            [],
            system_prompt,
            emit,
            stage="AI 参考资料节级分拆（不预判未来小节）",
        )
        parsed = _parse_json_response(answer)
        rows = parsed.get("parts") if isinstance(parsed, dict) else None
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("参考资料分拆 Agent 没有返回 parts。")
        return [dict(item) for item in rows if isinstance(item, dict)]

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

    def curate_recording_window(
        self,
        course_info: dict[str, Any],
        window: dict[str, Any],
        board_frames: list[dict[str, Any]],
        timestamped_transcript: str,
        emit: Callable[[str], None],
    ) -> dict[str, Any]:
        """Conservatively condense one closed recording-time evidence window."""
        indexes = [int(item["index"]) for item in board_frames]
        if len(set(indexes)) != len(indexes):
            raise ValueError("Recording-time window candidate indexes are not unique.")
        system_prompt = (
            "You are one recording-time child agent for a mathematics lecture. Read the "
            "bounded transcript and every attached candidate frame once. Return ONLY one "
            "JSON object matching response_schema, including integer arrays `selected` "
            "and `rejected`, string `mathematical_content_markdown`, array "
            "`diagram_materials`, `evidence_status`, and `fallback_reason`. "
            "The arrays must partition every candidate index exactly once. Keep the clearest "
            "frames needed to preserve every distinct board state; reject only a frame whose "
            "mathematics is fully contained in another selected frame. Write chronological "
            "timestamped Markdown containing all definitions, hypotheses, formulas, theorem "
            "statements, proof steps, examples, exercises, and corrections supported by the "
            "transcript or images. Preserve notation and `【待补证明：...】` markers. Do not "
            "invent, prove omitted claims, build the course outline, or perform a second "
            "review. `diagram_materials` is only a list of possible mathematical relationships "
            "for later whole-lecture necessity review. Do not decide that a figure will be "
            "published, choose a drawing backend, or generate drawing source in this bounded "
            "window. Add a candidate only when a spatial, geometric, incidence, quotient, "
            "gluing, or multi-map relationship might be materially harder to understand from "
            "prose and displayed formulas alone. A drawable object, a board sketch, a textbook "
            "picture, or an attractive illustration is not by itself a candidate. Return an "
            "empty array when no relationship clears that threshold. Each retained candidate "
            "contains only its time, English title, and precise English mathematical "
            "description. The whole-lecture main Agent will later understand all content, "
            "choose include or omit, and only then generate source for an included figure. "
            "Never claim that a candidate was compiled or visually verified. Use `complete` when finished; otherwise "
            "state the exact unreadable item in `fallback_reason`. The later whole-lecture "
            "Agent receives and enforces the permanent drawing contract; this source-free "
            "recording window must not repeat that contract or perform drawing work."
        )
        if self._english_course(course_info):
            system_prompt = (
                "You are one recording-time evidence child agent for an English-language course. Read the bounded transcript and "
                "every attached candidate frame once. Return ONLY one JSON object matching response_schema. `selected` and "
                "`rejected` must partition every candidate index exactly once. Select the smallest frame set that preserves each "
                "distinct teaching state. In `mathematical_content_markdown` (a legacy field name), write chronological timestamped "
                "language-course evidence: English examples exactly as shown or spoken, deliberately wrong examples unchanged, "
                "corrections separately, grammar rules with conditions and exceptions, structural analysis, usage and collocations, "
                "word formation or etymology only when actually taught, pronunciation, exercises, reading strategies, and writing "
                "advice. The teacher may speak Chinese; faithfully preserve the explanatory content, but do not make final polished "
                "lecture notes and do not mechanically translate quoted examples. Never invent a rule, attribution, example, answer, "
                "or etymology. Do not use theorem/proof machinery or mathematical obligation markers. Return `diagram_materials` as "
                "an empty array. Use `complete` when the evidence is coherent, or `needs_review` with the exact unresolved item."
            )
        payload = {
            **course_info,
            "workflow": "recording_time_bounded_window_curation",
            "response_schema": {
                "selected": ["integer candidate index"],
                "rejected": ["integer candidate index"],
                "mathematical_content_markdown": "timestamped Markdown string",
                "diagram_materials": [
                    {
                        "time": "HH:MM:SS within this window",
                        "title": "specific mathematical title",
                        "description": (
                            "detailed semantic and visual acceptance specification"
                        ),
                    }
                ],
                "evidence_status": "complete | needs_review",
                "fallback_reason": "string",
            },
            "window": dict(window),
            "timestamped_audio_transcript": str(timestamped_transcript or ""),
            "candidate_frames": [
                {
                    "index": int(item["index"]),
                    "time": str(item.get("time") or ""),
                    "attachment_name": str(item["attachment_name"]),
                    "change_type": str(item.get("change_type") or ""),
                    "changed_area_ratio": float(item.get("changed_area_ratio") or 0.0),
                    "stable_seconds": float(item.get("stable_seconds") or 0.0),
                }
                for item in board_frames
            ],
        }
        answer = self._run(
            payload,
            self._attachments(board_frames),
            system_prompt,
            emit,
            stage="录制期窗口关键帧整理",
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict):
            raise RuntimeError("Recording-time window Agent returned a non-object response.")
        try:
            selected = {int(value) for value in parsed.get("selected") or []}
            rejected = {int(value) for value in parsed.get("rejected") or []}
        except (TypeError, ValueError) as error:
            raise RuntimeError("Recording-time window Agent returned invalid indexes.") from error
        expected = set(indexes)
        if selected & rejected or selected | rejected != expected:
            raise RuntimeError(
                "Recording-time window Agent did not partition every candidate exactly once."
            )
        if expected and not selected:
            raise RuntimeError(
                "Recording-time window Agent rejected every visual candidate; "
                "the conservative fallback must retain the full window."
            )
        markdown = str(parsed.get("mathematical_content_markdown") or "").strip()
        evidence_status = str(parsed.get("evidence_status") or "complete").strip()
        if evidence_status not in {"complete", "needs_review"}:
            evidence_status = "needs_review"
        fallback_reason = str(parsed.get("fallback_reason") or "").strip()
        diagram_materials = _normalize_window_diagram_materials(
            parsed.get("diagram_materials", []),
            session_id=str(window.get("session_id") or ""),
            window_index=int(window.get("window_index") or 0),
            start_video_time=float(window.get("start_video_time") or 0.0),
            end_video_time=float(window.get("end_video_time") or 0.0),
        )
        if evidence_status == "needs_review" and fallback_reason:
            review_marker = f"【待核对：{fallback_reason}】"
            if review_marker not in markdown:
                markdown = (markdown.rstrip() + "\n\n" + review_marker).strip()
        return {
            "selected_indexes": sorted(selected),
            "rejected_indexes": sorted(rejected),
            "mathematical_content_markdown": markdown,
            "diagram_materials": diagram_materials,
            "evidence_status": evidence_status,
            "fallback_reason": fallback_reason,
        }

    def review_recording_window(
        self,
        course_info: dict[str, Any],
        window: dict[str, Any],
        selected_frames: list[dict[str, Any]],
        timestamped_audio_transcript: str,
        locked_mathematical_content_markdown: str,
        uncertainty: str,
        emit: Callable[[str], None],
    ) -> dict[str, Any]:
        """Resolve one recording-time needs_review window while recording continues."""
        session_id = str(window.get("session_id") or "")
        window_index = int(window.get("window_index") or 0)
        payload = {
            **course_info,
            "workflow": "targeted_recording_window_mathematics_review",
            "schema_version": 1,
            "window": {
                "session_id": session_id,
                "window_index": window_index,
                "start_video_time": float(window.get("start_video_time") or 0.0),
                "end_video_time": float(window.get("end_video_time") or 0.0),
                "timestamped_audio_transcript": str(
                    timestamped_audio_transcript or ""
                ),
                "locked_mathematical_content_markdown": str(
                    locked_mathematical_content_markdown or ""
                ),
                "lead_agent_uncertainty": str(uncertainty or ""),
                "candidate_frames": [
                    {
                        "index": int(item.get("index") or 0),
                        "time": str(item.get("time") or ""),
                        "attachment_name": str(item.get("attachment_name") or ""),
                    }
                    for item in selected_frames
                ],
            },
            "response_schema": {
                "schema_version": 1,
                "session_id": "string",
                "window_index": "integer",
                "mathematical_content_markdown": "string",
                "evidence_status": "complete | source_error_corrected | unresolved",
                "source_error": "string; required only for source_error_corrected",
                "correction": "string; required only for source_error_corrected",
                "uncertainties": ["string"],
            },
        }
        prompt = (
            "You are a fail-closed mathematical evidence adjudicator working during "
            "lecture recording. Return ONLY one JSON object matching response_schema. "
            "Review only this bounded window. Compare every attached selected keyframe, "
            "the exact bounded transcript, the locked window note, and the stated "
            "uncertainty. Correct notation only when the evidence uniquely supports it. "
            "Preserve every definition, hypothesis, theorem, proof step, calculation, "
            "exercise, transition, correction, and exact `【待补证明：...】` marker. Do not "
            "summarize, complete an omitted proof, or invent mathematics. Return complete "
            "only when the window is consistently reconstructed. For a demonstrable source "
            "error with one unique minimal rigorous repair, return source_error_corrected, "
            "document both the source_error and correction, and label the correction in the "
            "Markdown. Otherwise return unresolved with a precise uncertainty."
        )
        if self._english_course(course_info):
            prompt = (
                "You are a fail-closed language-course evidence adjudicator working during recording. Return ONLY one JSON object "
                "matching response_schema. Review only this bounded window. Compare all selected frames, the transcript, the locked "
                "window note, and the stated uncertainty. Preserve English examples verbatim, including intentional errors; preserve "
                "teacher corrections, grammar conditions and exceptions, sentence analysis, usage, exercises, vocabulary and writing "
                "advice. Correct the evidence note only when the supplied sources uniquely support the correction. Never invent or "
                "normalize away a pedagogically relevant contrast, and never introduce theorem/proof machinery. Return complete only "
                "when the bounded evidence is consistently reconstructed; otherwise return unresolved with a precise uncertainty."
            )
        answer = self._run(
            payload,
            self._attachments(selected_frames),
            prompt,
            emit,
            stage="录制期窗口定点数学复核",
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict) or int(parsed.get("schema_version") or 0) != 1:
            raise RuntimeError("recording-time targeted review returned invalid JSON")
        if (
            str(parsed.get("session_id") or "") != session_id
            or int(parsed.get("window_index") or 0) != window_index
        ):
            raise RuntimeError("recording-time targeted review returned the wrong window")
        evidence_status = str(parsed.get("evidence_status") or "").strip()
        if evidence_status not in {"complete", "source_error_corrected"}:
            raise RuntimeError(
                "recording-time targeted review remained unresolved: "
                f"{parsed.get('uncertainties') or []}"
            )
        markdown = str(parsed.get("mathematical_content_markdown") or "").strip()
        if not markdown:
            raise RuntimeError("recording-time targeted review returned empty mathematics")
        obligations = set(
            re.findall(r"【待补证明：.*?】", locked_mathematical_content_markdown)
        )
        missing_obligations = sorted(value for value in obligations if value not in markdown)
        if missing_obligations:
            markdown += (
                "\n\n### Preserved proof obligations\n\n"
                + "\n\n".join(missing_obligations)
            )
        source_error = str(parsed.get("source_error") or "").strip()
        correction = str(parsed.get("correction") or "").strip()
        if evidence_status == "source_error_corrected":
            if not source_error or not correction:
                raise RuntimeError(
                    "recording-time targeted review omitted its source error correction"
                )
            if "Evidence correction" not in markdown:
                markdown += (
                    "\n\n- **Evidence correction — source error:** "
                    + source_error
                    + "\n\n- **Rigorous correction:** "
                    + correction
                )
        return {
            "mathematical_content_markdown": markdown,
            "evidence_status": "complete",
            "source_error_corrected": evidence_status == "source_error_corrected",
            "source_error": source_error,
            "correction": correction,
            "uncertainties": list(parsed.get("uncertainties") or []),
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
        mathematical_only_system_prompt = (
            system_prompt.partition(
                " Each recording window also contains diagram_materials"
            )[0].rstrip()
            + " No diagram candidates are assigned to this bounded text part; "
            "return diagram_decisions as an empty array."
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
            "reasoning_effort": self.REQUIRED_REASONING_EFFORT,
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
                and str(cached_response.get("synthesis_signature") or "")
                == synthesis_signature
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
                and str(stored_parts.get("synthesis_signature") or "")
                == synthesis_signature
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
                    "recording_sessions": [part_session],
                },
            }

        def system_prompt_for_part(part: dict[str, Any]) -> str:
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
                    if unmatched_targets is not None:
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
                        applied_block = apply_unscoped(block, anchored_rows)
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
                                    "successful mathematical reconstruction"
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
                ):
                    values = row.get(field) or []
                    if not isinstance(values, list):
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

    def transcribe_board(
        self,
        course_info: dict[str, Any],
        board_frames: list[dict[str, Any]],
        emit: Callable[[str], None],
        *,
        audio_transcript: str = "",
    ) -> tuple[str, str]:
        if not board_frames:
            return "# AI-transcribed board notes\n\nNo distinct unobscured board frames were captured.\n", ""
        system_prompt = (
            "You are the blackboard-transcription component of a mathematics-course evidence agent, not a lecturer. "
            "Read every supplied frame and return only timestamped Markdown, with one `## HH:MM:SS` heading for every materially new board state. "
            "Faithfully transcribe definitions, formulas, diagrams, arrows, conditions, annotations, calculations, and proof steps using Markdown and LaTeX. "
            "The payload also contains the complete timestamped audio transcript owned by this recording session. Cross-check the images and audio: preserve "
            "mathematics supported by either source, use the images as authority for written notation, use audio for spoken hypotheses and connective "
            "reasoning, and explicitly mark any unresolved conflict instead of silently choosing one source. "
            "Consolidate progressive or repeated states within the batch. Do not summarize, explain, fill missing proof steps, answer questions, or invent obscured content. "
            "The ordinary evidence path contains the curated keyframes and matching audio transcript and should be sufficient. "
            "Do not request interval screenshots merely for extra confidence. End the response with exactly "
            "`<!-- evidence-status:complete -->` when these sources support a continuous faithful reconstruction. Only when a specific "
            "formula, board transition, or time interval cannot be reconstructed because the supplied sources genuinely conflict or omit it, "
            "end with `<!-- evidence-status:needs-three-second-fallback reason=\"brief precise gap\" -->`. This fallback is expensive and exceptional. "
            "When a visible or spoken result is assigned as an exercise, left to the reader, or stated without a detailed proof, append "
            "`【待补证明：precise conclusion】` immediately after that result; never write the missing proof in this evidence transcription. "
            "Mark unreadable tokens as `[illegible]`. For every mathematical diagram, geometric sketch, commutative diagram, plot, or argument whose meaning depends on a visual configuration, append an exact marker of the form "
            "`【需要LaTeX绘图：complete description of the objects, labels, arrows, highlighted regions, incidences, and mathematical purpose】`. "
            "Use one marker per distinct required drawing, even when several diagrams occur in one frame. The description must be detailed enough to reconstruct and visually audit a single-protocol TikZ replacement without copying the screenshot. "
            "If a diagram cannot otherwise be faithfully represented as text, describe only its visible labelled structure, cite the attachment name inside that marker, and record the uncertainty. "
            + self.MATHEMATICAL_TRAINING
        )
        if self._english_course(course_info):
            system_prompt = (
                "You are the screen-and-audio transcription component of an English-course evidence agent, not a lecturer. Read "
                "every frame and return timestamped Markdown with a `## HH:MM:SS` heading for each materially new teaching state. "
                "Cross-check the matching transcript. Preserve English examples exactly, including deliberate errors; keep "
                "corrections separate. Capture teacher-supported grammar rules, conditions, exceptions, sentence structures, usage, "
                "collocations, vocabulary and word formation, pronunciation, exercises, reading strategies, and writing advice. "
                "Chinese explanations may be recorded faithfully as evidence. Consolidate repeated states; do not invent, polish, "
                "answer exercises, or turn evidence into final lecture notes. End with exactly "
                "`<!-- evidence-status:complete -->` when reconstruction is continuous; otherwise use the existing "
                "needs-three-second-fallback marker with a precise language-evidence gap."
            )
        transcript_rows: list[tuple[float, str]] = []
        for line in str(audio_transcript or "").splitlines():
            match = re.match(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*(.*)$", line.strip())
            if not match:
                continue
            seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
            transcript_rows.append((float(seconds), line.strip()))

        def frame_seconds(value: Any) -> float:
            match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})", str(value or "").strip())
            if not match:
                return 0.0
            return float(int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)))

        def process_batch(offset: int, batch: list[dict[str, Any]]) -> tuple[int, str, str]:
            batch_number = (offset // self.BOARD_TRANSCRIPTION_BATCH_SIZE) + 1
            times = [frame_seconds(item.get("time")) for item in batch]
            window_start = max(0.0, min(times or [0.0]) - 15.0)
            window_end = max(times or [0.0]) + 15.0
            transcript_excerpt = str(audio_transcript or "")[:220000]
            payload = {
                **course_info,
                "batch_number": batch_number,
                "audio_window": {
                    "start": str(
                        course_info.get("recording_session", {}).get(
                            "owned_start_video_time", window_start
                        )
                    ),
                    "end": str(
                        course_info.get("recording_session", {}).get(
                            "owned_end_video_time", window_end
                        )
                    ),
                },
                "timestamped_audio_transcript": transcript_excerpt,
                "frames": [
                    {
                        "index": int(item["index"]),
                        "time": str(item["time"]),
                        "attachment_name": str(item["attachment_name"]),
                        "evidence_kind": str(item.get("evidence_kind") or "board_frame"),
                    }
                    for item in batch
                ],
            }
            try:
                section = _strip_fence(
                    self._run(
                        payload,
                        self._attachments(batch),
                        system_prompt,
                        emit,
                        stage=f"AI 板书与音频交叉核对（批次 {batch_number}）",
                    ),
                    "markdown|md|text",
                )
                if not section:
                    raise RuntimeError("empty response")
                if not re.search(r"(?m)^##\s+\d{2}:\d{2}:\d{2}\b", section):
                    raise RuntimeError("missing timestamped board-state headings")
                return offset, section, ""
            except Exception as error:
                return offset, "", f"Batch {batch_number}: {error}"

        batches = [
            (offset, board_frames[offset : offset + self.BOARD_TRANSCRIPTION_BATCH_SIZE])
            for offset in range(0, len(board_frames), self.BOARD_TRANSCRIPTION_BATCH_SIZE)
        ]
        results: list[tuple[int, str, str]] = []
        if len(batches) == 1:
            results.append(process_batch(*batches[0]))
        else:
            workers = min(self.BOARD_BATCH_WORKERS, len(batches))
            emit(f"板书与音频交叉核对共 {len(batches)} 批，正在以 {workers} 个并发请求处理。")
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="board-evidence") as executor:
                futures = [executor.submit(process_batch, offset, batch) for offset, batch in batches]
                for future in as_completed(futures):
                    results.append(future.result())
        results.sort(key=lambda item: item[0])
        sections = [section for _offset, section, _error in results if section]
        errors = [error for _offset, _section, error in results if error]
        lines = [
            "# AI-transcribed board notes",
            "",
            f"- Vision model: {self.profile_label}",
            f"- Distinct unobscured frames processed: {len(board_frames)}",
            "",
            *sections,
        ]
        if errors:
            lines.extend(
                [
                    "",
                    "## Incomplete AI transcription",
                    "",
                    "These batches still require AI processing; inspect the original frames retained in local course storage and retry:",
                    *[f"- {message}" for message in errors],
                ]
            )
        return "\n".join(lines).rstrip() + "\n", "; ".join(errors)

    def curate_board_keyframes(
        self,
        course_info: dict[str, Any],
        board_frames: list[dict[str, Any]],
        emit: Callable[[str], None],
        *,
        indexed_frames: list[dict[str, Any]] | None = None,
    ) -> tuple[list[int], list[dict[str, Any]], list[list[int]], str]:
        """Select representatives inside one recording session only."""
        if not board_frames:
            return [], [], [], ""
        indexing_prompt = (
            "You are the mathematical-content indexing component of a recorded-course evidence agent. "
            "Return ONLY JSON with key `frames`. For every supplied frame, output an object with integer `index`, "
            "array `content_units`, number `completeness` from 0 to 1, and concise `clarity_note`. "
            "A content unit is one semantically distinct mathematical item: a definition clause, theorem statement, formula, "
            "proof step, quantified condition, labelled diagram component, or explicit annotation. Normalize equivalent notation "
            "to concise LaTeX so later comparison can recognize the same mathematics despite layout, colour, cursor position, "
            "scrolling, or additional surrounding text. Do not merge distinct proof steps and do not invent obscured content. "
            + self.MATHEMATICAL_TRAINING
        )
        if self._english_course(course_info):
            indexing_prompt = (
                "You are the teaching-content indexing component of a recorded English-course evidence agent. Return ONLY JSON "
                "with key `frames`. For every supplied frame, return integer `index`, array `content_units`, number "
                "`completeness` from 0 to 1, and concise `clarity_note`. A content unit is one distinct language-teaching item: an "
                "English example (preserved verbatim), an intentional error, a correction, a grammar rule or restriction, a sentence "
                "analysis step, usage or collocation, vocabulary or word-formation item, pronunciation cue, exercise, reading "
                "strategy, writing principle, or explicit teacher annotation. Normalize labels enough to detect repeated screen states, "
                "but never silently correct examples or invent obscured content."
            )
        valid_board_indexes = {int(item["index"]) for item in board_frames}
        indexed_by_index: dict[int, dict[str, Any]] = {
            int(item["index"]): dict(item)
            for item in (indexed_frames or [])
            if isinstance(item, dict)
            and int(item.get("index") or 0) in valid_board_indexes
            and isinstance(item.get("content_units"), list)
            and bool(item.get("content_units"))
        }
        errors: list[str] = []
        pending_batches: list[tuple[int, list[dict[str, Any]]]] = []
        for offset in range(0, len(board_frames), self.BOARD_BATCH_SIZE):
            batch = [
                item
                for item in board_frames[offset : offset + self.BOARD_BATCH_SIZE]
                if int(item["index"]) not in indexed_by_index
            ]
            if not batch:
                continue
            pending_batches.append((offset, batch))

        def process_index_batch(
            offset: int,
            batch: list[dict[str, Any]],
        ) -> tuple[int, dict[int, dict[str, Any]], str]:
            batch_number = (offset // self.BOARD_BATCH_SIZE) + 1
            local_indexed: dict[int, dict[str, Any]] = {}
            payload = {
                **course_info,
                "batch_number": batch_number,
                "frames": [
                    {
                        "index": int(item["index"]),
                        "time": str(item["time"]),
                        "attachment_name": str(item["attachment_name"]),
                    }
                    for item in batch
                ],
            }

            def collect_rows(answer: str, expected: list[dict[str, Any]]) -> set[int]:
                parsed = _parse_json_response(answer)
                rows = parsed.get("frames") if isinstance(parsed, dict) else None
                if not isinstance(rows, list):
                    raise RuntimeError("invalid board-content index JSON schema")
                expected_by_index = {int(item["index"]): item for item in expected}
                returned_indexes: set[int] = set()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    index = int(row.get("index") or 0)
                    units = [
                        str(unit).strip()
                        for unit in (row.get("content_units") or [])
                        if str(unit).strip()
                    ]
                    if index not in expected_by_index or not units:
                        continue
                    returned_indexes.add(index)
                    local_indexed[index] = {
                        "index": index,
                        "time": str(expected_by_index[index]["time"]),
                        "content_units": units,
                        "completeness": max(
                            0.0, min(1.0, float(row.get("completeness") or 0))
                        ),
                        "clarity_note": str(row.get("clarity_note") or ""),
                    }
                return returned_indexes

            try:
                answer = self._run(
                    payload,
                    self._attachments(batch),
                    indexing_prompt,
                    emit,
                    stage=f"AI 板书数学内容索引（批次 {batch_number}）",
                )
                batch_indexes = {int(item["index"]) for item in batch}
                returned_indexes = collect_rows(answer, batch)
                missing = sorted(batch_indexes - returned_indexes)
                if missing:
                    emit(
                        f"板书数学内容索引第 {batch_number} 批漏掉 {len(missing)} 张帧；"
                        "正在只重新上传缺失帧并补全索引。"
                    )
                    missing_set = set(missing)
                    missing_batch = [
                        item for item in batch if int(item["index"]) in missing_set
                    ]
                    repair_payload = {
                        **course_info,
                        "batch_number": batch_number,
                        "repair": "index_only_the_previously_omitted_frames",
                        "missing_frame_indexes": missing,
                        "frames": [
                            {
                                "index": int(item["index"]),
                                "time": str(item["time"]),
                                "attachment_name": str(item["attachment_name"]),
                            }
                            for item in missing_batch
                        ],
                    }
                    repair_prompt = (
                        indexing_prompt
                        + " The previous response omitted some supplied frames. This repair request contains only "
                        "those omitted frames. Return exactly one `frames` object for every supplied index, even when "
                        "its mathematics repeats another frame; do not omit, deduplicate, or renumber any frame."
                    )
                    repaired_answer = self._run(
                        repair_payload,
                        self._attachments(missing_batch),
                        repair_prompt,
                        emit,
                        stage=f"AI 板书数学内容索引补录（批次 {batch_number}）",
                    )
                    returned_indexes.update(collect_rows(repaired_answer, missing_batch))
                    missing = sorted(batch_indexes - returned_indexes)
                if missing:
                    raise RuntimeError(
                        "board-content index still omitted frame indexes after targeted repair: "
                        + ", ".join(str(value) for value in missing)
                    )
            except Exception as error:
                return offset, local_indexed, f"Batch {batch_number}: {error}"
            return offset, local_indexed, ""

        indexed_results: list[tuple[int, dict[int, dict[str, Any]], str]] = []
        if len(pending_batches) == 1:
            indexed_results.append(process_index_batch(*pending_batches[0]))
        elif pending_batches:
            workers = min(self.BOARD_BATCH_WORKERS, len(pending_batches))
            emit(
                f"录制批次关键帧数学索引共 {len(pending_batches)} 批，"
                f"正在以 {workers} 个并发请求处理。"
            )
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="board-keyframe-index"
            ) as executor:
                futures = [
                    executor.submit(process_index_batch, offset, batch)
                    for offset, batch in pending_batches
                ]
                for future in as_completed(futures):
                    indexed_results.append(future.result())
        indexed_results.sort(key=lambda item: item[0])
        for _offset, local_indexed, error in indexed_results:
            indexed_by_index.update(local_indexed)
            if error:
                errors.append(error)
        indexed = [indexed_by_index[index] for index in sorted(indexed_by_index)]
        if errors:
            return [], indexed, [], "; ".join(errors)

        selection_prompt = (
            "You are the recording-session mathematical-overlap deduplication component of a recorded-course evidence agent. "
            "The payload contains at most one bounded batch of AI-indexed mathematical content units. Return ONLY JSON "
            "with keys `selected`, `duplicate_groups`, and `summary`. Define the mathematical overlap ratio as the number of "
            "semantically equivalent mathematical content units shared by both frames divided by the number of units in their union "
            "(Jaccard intersection-over-union), never divided by the smaller frame. For adjacent frames on the same board/tablet, "
            "prefer a bridge with overlap at least 0.10 and strictly below 0.30. If overlap is at least 0.30, treat the pair as "
            "redundant and retain the clearer, more complete frame. If overlap is below 0.10, preserve both frames unless the supplied "
            "content proves a carrier or page transition. Group progressive board states, equivalent formulas, repeated theorem statements, "
            "and the same proof steps even when layout, colour, cursor, zoom, scrolling, or surrounding content differs. Preserve every "
            "candidate that contributes genuinely new mathematics. `selected` must contain objects with integer `index`, concise `reason`, "
            "and number `max_overlap_with_other_selected` from 0 to 1. `duplicate_groups` must be a JSON array of integer arrays such as "
            "`[[6,1,2],[9,7,8]]`; the retained representative is first and every rejected index occurs after exactly one representative. "
            "Do not return objects inside `duplicate_groups`. Deduplicate only the supplied batch and recording session. Never reject a "
            "boundary frame merely because another selection batch might contain a duplicate; preserving a boundary duplicate is safer than "
            "omitting mathematics. Decisions from earlier recording sessions are immutable. "
            + self.MATHEMATICAL_TRAINING
        )
        if self._english_course(course_info):
            selection_prompt = (
                "You are the recording-session teaching-state deduplication component of an English-course evidence agent. The "
                "payload contains AI-indexed language-teaching content units. Return ONLY JSON with `selected`, `duplicate_groups`, "
                "and `summary`. Use Jaccard intersection-over-union of semantically equivalent content units. For adjacent frames, "
                "prefer a bridge with overlap at least 0.10 and below 0.30; at 0.30 or above retain the clearer, more complete state. "
                "Below 0.10 preserve both unless the content proves a page or carrier transition. Preserve every frame that contributes "
                "a new example, correction, condition, exception, exercise step, pronunciation cue, reading point, or writing point. "
                "`selected` items contain integer `index`, concise `reason`, and `max_overlap_with_other_selected` from 0 to 1. "
                "`duplicate_groups` is an array of integer arrays with the retained representative first. Deduplicate only this "
                "recording session; earlier sessions are immutable."
            )

        def validate_selection(
            answer: str,
            expected_frames: list[dict[str, Any]],
        ) -> tuple[list[int], list[dict[str, Any]], list[list[int]]]:
            parsed = _parse_json_response(answer)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("selected"), list):
                raise RuntimeError("invalid board-keyframe selection JSON schema")
            valid_indexes = {int(item["index"]) for item in expected_frames}
            selected: list[dict[str, Any]] = []
            selected_indexes: set[int] = set()
            for item in parsed.get("selected") or []:
                if not isinstance(item, dict):
                    continue
                index = int(item.get("index") or 0)
                if index not in valid_indexes or index in selected_indexes:
                    continue
                selected_indexes.add(index)
                selected.append(
                    {
                        "candidate_index": index,
                        "reason": str(item.get("reason") or ""),
                        "max_overlap_with_other_selected": max(
                            0.0,
                            min(
                                1.0,
                                float(item.get("max_overlap_with_other_selected") or 0),
                            ),
                        ),
                    }
                )
            duplicate_groups: list[list[int]] = []
            rejected_covered: set[int] = set()
            for group in parsed.get("duplicate_groups") or []:
                raw_values = group.get("indices") if isinstance(group, dict) else group
                if not isinstance(raw_values, list):
                    continue
                values = [int(value) for value in raw_values if str(value).isdigit()]
                values = [value for value in values if value in valid_indexes]
                if len(values) >= 2 and values[0] in selected_indexes:
                    duplicate_groups.append(values)
                    rejected_covered.update(values[1:])
            rejected = valid_indexes - selected_indexes
            if not selected_indexes:
                raise RuntimeError("board-keyframe agent selected no representative frame")
            if rejected - rejected_covered:
                raise RuntimeError(
                    "board-keyframe duplicate groups omitted rejected frame indexes: "
                    + ", ".join(str(value) for value in sorted(rejected - rejected_covered))
                )
            if any(float(item["max_overlap_with_other_selected"]) >= 0.30 for item in selected):
                raise RuntimeError(
                    "board-keyframe agent retained frames whose reported mathematical overlap is at least 30%"
                )
            selected.sort(key=lambda item: int(item["candidate_index"]))
            return (
                [int(item["candidate_index"]) for item in selected],
                selected,
                duplicate_groups,
            )

        def process_selection_batch(
            offset: int,
            batch: list[dict[str, Any]],
        ) -> tuple[int, list[int], list[dict[str, Any]], list[list[int]], str]:
            batch_number = (offset // self.BOARD_BATCH_SIZE) + 1
            selection_payload = {
                **course_info,
                "selection_batch_number": batch_number,
                "selection_scope": "only_the_supplied_bounded_batch",
                "mathematical_overlap_definition": "Jaccard intersection over union of mathematical content units",
                "desired_adjacent_overlap": {"minimum": 0.10, "maximum_exclusive": 0.30},
                "indexed_frames": batch,
            }
            try:
                answer = self._run(
                    selection_payload,
                    [],
                    selection_prompt,
                    emit,
                    stage=f"AI 板书关键帧批次内去重（分片 {batch_number}）",
                )
                validation_error: Exception | None = None
                for correction_attempt in range(self.CURATION_CORRECTION_ATTEMPTS + 1):
                    try:
                        selected_indexes, selected, duplicate_groups = validate_selection(
                            answer, batch
                        )
                        return offset, selected_indexes, selected, duplicate_groups, ""
                    except Exception as error:
                        validation_error = error
                    if correction_attempt >= self.CURATION_CORRECTION_ATTEMPTS:
                        break
                    emit(
                        f"板书关键帧第 {batch_number} 个分片结果未通过完整性校验；"
                        "正在只修正分组 JSON，不重新上传或识别图片。"
                    )
                    correction_prompt = (
                        selection_prompt
                        + " The previous JSON response failed application validation. Correct the JSON only. "
                        "Do not change a selected representative unless necessary to satisfy the 0.30 overlap rule. "
                        "Return `duplicate_groups` strictly as arrays of integers. Every rejected index must appear "
                        "after exactly one retained representative; do not omit rejected indexes."
                    )
                    answer = self._run(
                        {
                            **selection_payload,
                            "previous_response": answer,
                            "validation_error": str(validation_error),
                            "correction_attempt": correction_attempt + 1,
                        },
                        [],
                        correction_prompt,
                        emit,
                        stage=(
                            f"AI 板书关键帧去重清单纠错（分片 {batch_number}，"
                            f"第 {correction_attempt + 1} 轮）"
                        ),
                    )
                raise validation_error or RuntimeError(
                    "board-keyframe selection validation failed"
                )
            except Exception as error:
                return offset, [], [], [], f"Selection batch {batch_number}: {error}"

        selection_batches = [
            (offset, indexed[offset : offset + self.BOARD_BATCH_SIZE])
            for offset in range(0, len(indexed), self.BOARD_BATCH_SIZE)
        ]
        selection_results: list[
            tuple[int, list[int], list[dict[str, Any]], list[list[int]], str]
        ] = []
        if len(selection_batches) == 1:
            selection_results.append(process_selection_batch(*selection_batches[0]))
        elif selection_batches:
            workers = min(self.BOARD_BATCH_WORKERS, len(selection_batches))
            emit(
                f"录制批次关键帧去重共 {len(selection_batches)} 个有界分片，"
                f"正在以 {workers} 个并发请求处理；分片边界不删除证据。"
            )
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="board-keyframe-selection"
            ) as executor:
                futures = [
                    executor.submit(process_selection_batch, offset, batch)
                    for offset, batch in selection_batches
                ]
                for future in as_completed(futures):
                    selection_results.append(future.result())
        selection_results.sort(key=lambda item: item[0])
        selected_indexes: list[int] = []
        selected: list[dict[str, Any]] = []
        duplicate_groups: list[list[int]] = []
        selection_errors: list[str] = []
        for _offset, batch_indexes, batch_selected, batch_groups, error in selection_results:
            selected_indexes.extend(batch_indexes)
            selected.extend(batch_selected)
            duplicate_groups.extend(batch_groups)
            if error:
                selection_errors.append(error)
        if selection_errors:
            return [], indexed, [], "; ".join(selection_errors)
        selected_indexes.sort()
        selected.sort(key=lambda item: int(item["candidate_index"]))
        return selected_indexes, selected, duplicate_groups, ""

    def audit_lecture_fast(
        self,
        *,
        latex_source: str,
        contract: dict[str, Any],
        atoms: list[dict[str, Any]],
        proof_obligations: list[dict[str, Any]],
        dependency_slice: dict[str, Any] | None = None,
        continuity_context: str = "",
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Run the single ordinary-path semantic critic.

        This critic verifies a locked draft; it is not permitted to rewrite it.
        Specialist escalation and targeted repair are orchestrated separately so
        this production fast path remains one bounded model call.
        """

        emit = emit or (lambda _message: None)
        compact_atoms = [
            {
                "atom_id": str(item.get("atom_id") or ""),
                "kind": str(item.get("kind") or ""),
                "canonical_name": str(item.get("canonical_name") or ""),
                "content": str(item.get("content") or ""),
                "hypotheses": list(item.get("hypotheses") or []),
                "required_subclaims": list(item.get("required_subclaims") or []),
                "scope_status": str(item.get("scope_status") or ""),
            }
            for item in atoms
        ]
        payload = {
            "contract": contract,
            "mathematical_atoms": compact_atoms,
            "proof_obligations": proof_obligations,
            "dependency_slice": dependency_slice or {"results": []},
            "adjacent_continuity_context": str(continuity_context or "")[-6000:],
            "locked_latex_draft": str(latex_source),
            "response_schema": {
                "complete": "boolean",
                "findings": [
                    {
                        "category": (
                            "coverage | evidence_scope | proof | promise | dependency | "
                            "hypothesis | continuity | notation"
                        ),
                        "severity": "warning | hard",
                        "message": "specific English diagnostic",
                        "atom_ids": ["atom-id"],
                        "obligation_ids": ["proof-id"],
                        "editable_start_line": "integer or null",
                        "editable_end_line": "integer or null",
                        "editable_hint": "smallest local correction required",
                    }
                ],
            },
        }
        system_prompt = (
            "You are the independent fast mathematical auditor for a recorded-course lecture-note pipeline. "
            "The draft is locked: audit it and NEVER rewrite it. Evidence and the supplied contract are the "
            "scope authority; current mathematical atoms retain authoritative BOARD_NOTES evidence, and accepted "
            "adjacent continuity retains definitions and conventions that already passed. Do not re-litigate an "
            "earlier accepted definition or reject an explicit current hypothesis merely because extraction left "
            "a narrower contract field empty. Reference-style knowledge may detect errors but may not authorize "
            "extra content. Check every required atom is substantively present (comments alone are not proof), no "
            "forbidden or unsupported atom is introduced, hypotheses and quantifiers do not drift, every "
            "proof establishes its exact claim and required subclaims, open promises are fulfilled, dependencies "
            "are legal at this position, and continuity language is truthful. Treat phrases such as clearly, "
            "well-defined, induced isomorphism, image is an ideal, iff, prime/maximal, and later/previous result "
            "as proof-risk signals. A hard mathematical or scope defect makes complete=false. Return ONLY one "
            "JSON object matching response_schema. Do not include repaired LaTeX."
        )
        answer = self._run(
            payload,
            [],
            system_prompt,
            emit,
            stage="Lecture fast semantic audit",
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict):
            raise RuntimeError("lecture fast auditor did not return a JSON object")
        findings = parsed.get("findings")
        if not isinstance(parsed.get("complete"), bool) or not isinstance(findings, list):
            raise RuntimeError("lecture fast auditor response does not match the required schema")
        allowed_categories = {
            "coverage", "evidence_scope", "proof", "promise", "dependency",
            "hypothesis", "continuity", "notation",
        }
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(findings):
            if not isinstance(item, dict):
                raise RuntimeError(f"lecture fast auditor finding {index + 1} is not an object")
            category = str(item.get("category") or "")
            severity = str(item.get("severity") or "")
            message = str(item.get("message") or "").strip()
            if category not in allowed_categories or severity not in {"warning", "hard"} or not message:
                raise RuntimeError(f"lecture fast auditor finding {index + 1} is invalid")
            normalized.append(
                {
                    "category": category,
                    "severity": severity,
                    "message": message,
                    "atom_ids": [str(value) for value in item.get("atom_ids") or []],
                    "obligation_ids": [str(value) for value in item.get("obligation_ids") or []],
                    "editable_start_line": item.get("editable_start_line"),
                    "editable_end_line": item.get("editable_end_line"),
                    "editable_hint": str(item.get("editable_hint") or ""),
                }
            )
        complete = bool(parsed["complete"])
        if any(item["severity"] == "hard" for item in normalized):
            complete = False
        return {"complete": complete, "findings": normalized}

    def audit_lecture_specialist(
        self,
        *,
        lane: str,
        latex_source: str,
        contract: dict[str, Any],
        atoms: list[dict[str, Any]],
        proof_obligations: list[dict[str, Any]],
        dependency_slice: dict[str, Any] | None = None,
        continuity_context: str = "",
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        allowed_lanes = {
            "proof": (
                "Check only claim-level proof validity, well-definedness, map properties, "
                "iff directions, hidden subclaims, and exact hypotheses."
            ),
            "coverage_evidence": (
                "Check only substantive required-atom coverage, evidence authorization, "
                "and leakage of continuity-only or unsupported mathematics."
            ),
            "dependency_hypothesis_continuity": (
                "Check only dependency chronology, hypothesis/quantifier drift, notation "
                "compatibility, open promises, and truthful continuity claims."
            ),
        }
        if lane not in allowed_lanes:
            raise ValueError(f"unknown lecture specialist lane: {lane}")
        emit = emit or (lambda _message: None)
        payload = {
            "lane": lane,
            "contract": contract,
            "mathematical_atoms": atoms,
            "proof_obligations": proof_obligations,
            "dependency_slice": dependency_slice or {"results": []},
            "accepted_continuity_context": str(continuity_context or "")[-8000:],
            "locked_latex_draft": latex_source,
            "response_schema": {
                "complete": "boolean",
                "findings": [
                    {
                        "category": (
                            "coverage | evidence_scope | proof | promise | dependency | "
                            "hypothesis | continuity | notation"
                        ),
                        "severity": "warning | hard",
                        "message": "specific English diagnostic",
                        "atom_ids": ["atom-id"],
                        "obligation_ids": ["proof-id"],
                        "editable_start_line": "integer or null",
                        "editable_end_line": "integer or null",
                        "editable_hint": "smallest local correction required",
                    }
                ],
            },
        }
        prompt = (
            "You are an independent specialist auditor for a locked mathematical lecture "
            "draft. Never rewrite the draft and never broaden evidence scope. Mathematical "
            "atoms include authoritative current BOARD_NOTES evidence. Accepted continuity "
            "contains read-only definitions and conventions that already passed earlier; do "
            "not re-litigate or reject their established scope. Suppress a proposed finding "
            "when explicit current evidence or accepted continuity already authorizes it. "
            + allowed_lanes[lane]
            + " A hard defect makes complete=false. Return ONLY JSON matching response_schema."
        )
        answer = self._run(
            payload,
            [],
            prompt,
            emit,
            stage=f"Lecture specialist audit: {lane}",
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("complete"), bool):
            raise RuntimeError(f"lecture specialist {lane} returned invalid JSON")
        findings = parsed.get("findings")
        if not isinstance(findings, list):
            raise RuntimeError(f"lecture specialist {lane} returned invalid findings")
        normalized: list[dict[str, Any]] = []
        allowed_categories = {
            "coverage", "evidence_scope", "proof", "promise", "dependency",
            "hypothesis", "continuity", "notation",
        }
        for index, item in enumerate(findings):
            if not isinstance(item, dict):
                raise RuntimeError(f"lecture specialist {lane} finding {index + 1} is invalid")
            category = str(item.get("category") or "")
            severity = str(item.get("severity") or "")
            message = str(item.get("message") or "").strip()
            if category not in allowed_categories or severity not in {"warning", "hard"} or not message:
                raise RuntimeError(f"lecture specialist {lane} finding {index + 1} is invalid")
            normalized.append(
                {
                    "category": category,
                    "severity": severity,
                    "message": message,
                    "atom_ids": [str(value) for value in item.get("atom_ids") or []],
                    "obligation_ids": [str(value) for value in item.get("obligation_ids") or []],
                    "editable_start_line": item.get("editable_start_line"),
                    "editable_end_line": item.get("editable_end_line"),
                    "editable_hint": str(item.get("editable_hint") or ""),
                }
            )
        complete = bool(parsed["complete"]) and not any(
            item["severity"] == "hard" for item in normalized
        )
        return {"complete": complete, "findings": normalized, "lane": lane}

    def repair_lecture_targeted(
        self,
        *,
        editable_start_line: int,
        editable_end_line: int,
        editable_text: str,
        minimal_context: str,
        findings: list[dict[str, Any]],
        contract: dict[str, Any],
        authorized_atoms: list[dict[str, Any]],
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Return one bounded replacement for a rejected lecture range."""

        emit = emit or (lambda _message: None)
        start_line = int(editable_start_line)
        end_line = int(editable_end_line)
        if start_line < 1 or end_line < start_line:
            raise ValueError("targeted lecture repair received an invalid line range")
        payload = {
            "editable_start_line": start_line,
            "editable_end_line": end_line,
            "editable_text": str(editable_text),
            "minimal_numbered_context": str(minimal_context),
            "hard_findings_for_this_exact_range": findings,
            "content_contract": contract,
            "authorized_mathematical_atoms": authorized_atoms,
            "response_schema": {
                "can_repair": "boolean",
                "editable_start_line": start_line,
                "editable_end_line": end_line,
                "replacement": "exact replacement text for only this range",
                "reason": "short English explanation",
            },
        }
        prompt = (
            "You are the specialized targeted-repair Agent for formal mathematics lecture "
            "LaTeX. Repair every supplied hard finding that refers to this one exact editable "
            "line range, and nothing else. The application, not you, owns the complete draft. "
            "Return only replacement text for the declared range; never return or rewrite the "
            "whole subsection. Treat the content contract and authorized atoms as a strict scope "
            "ceiling. Prefer deleting an unsupported assertion to inventing a proof or example. "
            "Correct hypotheses and quantifiers without silently strengthening or narrowing the "
            "authorized claim. Preserve any MPB-ATOM or proof-resolution comment inside the "
            "editable range truthfully. Do not add headings, input/include commands, references, "
            "style changes, or unrelated polishing. If the findings conflict or require evidence "
            "outside the supplied authority, set can_repair=false. Return ONLY one JSON object "
            "matching response_schema."
        )
        answer = self._run(
            payload,
            [],
            prompt,
            emit,
            stage="Lecture targeted mathematical repair",
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict):
            raise RuntimeError("lecture targeted repair did not return a JSON object")
        if not isinstance(parsed.get("can_repair"), bool):
            raise RuntimeError("lecture targeted repair omitted can_repair")
        returned_start = int(parsed.get("editable_start_line") or 0)
        returned_end = int(parsed.get("editable_end_line") or 0)
        if returned_start != start_line or returned_end != end_line:
            raise RuntimeError("lecture targeted repair changed its authorized line range")
        replacement = parsed.get("replacement")
        if not isinstance(replacement, str):
            raise RuntimeError("lecture targeted repair returned invalid replacement text")
        reason = str(parsed.get("reason") or "").strip()
        if not bool(parsed["can_repair"]):
            raise RuntimeError(
                "lecture targeted repair declined the authorized repair"
                + (f": {reason}" if reason else "")
            )
        if "```" in replacement:
            raise RuntimeError("lecture targeted repair returned a Markdown fence")
        if len(replacement) > 12000:
            raise RuntimeError("lecture targeted repair returned an oversized replacement")
        return {
            "editable_start_line": start_line,
            "editable_end_line": end_line,
            "replacement": replacement,
            "reason": reason,
        }

    def repair_lecture_targeted_bundle(
        self,
        *,
        rejected_source: str,
        repair_requests: list[dict[str, Any]],
        audit_findings: list[dict[str, Any]],
        contract: dict[str, Any],
        authorized_atoms: list[dict[str, Any]],
        board_evidence: str,
        transcript_evidence: str,
        lecture_errors: str,
        accepted_continuity: str,
        course_context: dict[str, Any],
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Adjudicate all hard findings and return one bounded repair patch set.

        This is one repair round and one model call.  It may reject false-positive
        findings, but it may never rewrite the complete subsection.
        """

        emit = emit or (lambda _message: None)
        payload = {
            "course_context": course_context,
            "content_contract": contract,
            "authorized_mathematical_atoms": authorized_atoms,
            "source_authority": {
                "board_notes": str(board_evidence)[-14000:],
                "transcript": str(transcript_evidence)[-14000:],
                "lecture_errors_and_corrections": str(lecture_errors)[-6000:],
                "accepted_prior_definitions_and_conventions": str(accepted_continuity)[-8000:],
            },
            "locked_rejected_draft": str(rejected_source),
            "hard_audit_findings": audit_findings,
            "editable_repair_clusters": repair_requests,
            "response_schema": {
                "finding_decisions": [
                    {
                        "finding_id": "exact supplied finding_id",
                        "status": "VALID | INVALID | AMBIGUOUS",
                        "repair_category": "one supplied repair category",
                        "reason": "short evidence-specific reason",
                        "evidence": "short exact authority locator or excerpt",
                    }
                ],
                "patches": [
                    {
                        "cluster_id": "exact supplied cluster_id",
                        "finding_ids": ["covered VALID finding IDs"],
                        "editable_start_line": "exact supplied integer",
                        "editable_end_line": "exact supplied integer",
                        "expected_old_hash": "exact supplied hash",
                        "replacement": "replacement for only this line range",
                    }
                ],
                "summary": "short repair summary",
            },
        }
        prompt = (
            "You are the Lecture Repair Agent: a constrained mathematical LaTeX patcher, "
            "not a writer and not an obedient copy of the auditor. First adjudicate EVERY "
            "hard finding as VALID, INVALID, or AMBIGUOUS. Raw relevant evidence and "
            "normalized BOARD_NOTES outrank the extracted contract; accepted prior "
            "definitions and course conventions outrank an auditor interpretation. General "
            "mathematical knowledge may detect falsehood but may not authorize unrecorded "
            "content. INVALID means NO PATCH. AMBIGUOUS means NO PATCH and the application "
            "will stop. For VALID findings, return the smallest correct patch inside exactly "
            "one supplied cluster range. Echo the exact old hash and exact line range. Do not "
            "change locked text, labels, numbering, notation, atom markers, passing proofs, "
            "paragraph order, headings, or style. Do not polish. Preserve an exercise as an "
            "exercise and provide its required solution when the source identifies it as an "
            "exercise. Prefer deleting an unsupported strengthening over inventing evidence. "
            "Use repair categories including REMOVE_UNSUPPORTED_CLAIM, "
            "RESTORE_SOURCE_HYPOTHESIS, REMOVE_UNAUTHORIZED_HYPOTHESIS, "
            "ADD_MISSING_PROOF_STEP, FIX_FORWARD_DEPENDENCY, FIX_HYPOTHESIS_DRIFT, "
            "FIX_QUANTIFIER, FIX_WELL_DEFINEDNESS, FIX_INJECTIVITY, FIX_SURJECTIVITY, "
            "FIX_IDEAL_CLOSURE, RESTORE_EXERCISE_STRUCTURE, ADD_REQUIRED_SOLUTION, "
            "CLOSE_PROMISE, RESTORE_MISSING_ATOM, REMOVE_SCOPE_LEAK, FIX_NOTATION, "
            "FIX_CONTINUITY, FIX_LATEX_STRUCTURE, or INVALID_AUDITOR_FINDING. Return ONLY "
            "one JSON object matching response_schema; never return a full replacement draft."
        )
        answer = self._run(
            payload,
            [],
            prompt,
            emit,
            stage="Lecture repair adjudication and minimal patch",
        )
        parsed = _parse_json_response(answer)
        if not isinstance(parsed, dict):
            raise RuntimeError("lecture repair Agent did not return a JSON object")
        decisions = parsed.get("finding_decisions")
        patches = parsed.get("patches")
        if not isinstance(decisions, list) or not isinstance(patches, list):
            raise RuntimeError("lecture repair Agent response omitted decisions or patches")
        finding_ids = {
            str(item.get("finding_id") or "") for item in audit_findings if isinstance(item, dict)
        }
        normalized_decisions: list[dict[str, Any]] = []
        seen_decisions: set[str] = set()
        for item in decisions:
            if not isinstance(item, dict):
                raise RuntimeError("lecture repair Agent returned an invalid finding decision")
            finding_id = str(item.get("finding_id") or "")
            status = str(item.get("status") or "").upper()
            if finding_id not in finding_ids or finding_id in seen_decisions:
                raise RuntimeError("lecture repair Agent returned an unknown or duplicate finding decision")
            if status not in {"VALID", "INVALID", "AMBIGUOUS"}:
                raise RuntimeError("lecture repair Agent returned an invalid adjudication status")
            seen_decisions.add(finding_id)
            normalized_decisions.append(
                {
                    "finding_id": finding_id,
                    "status": status,
                    "repair_category": str(item.get("repair_category") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                    "evidence": str(item.get("evidence") or "").strip(),
                }
            )
        if seen_decisions != finding_ids:
            raise RuntimeError("lecture repair Agent did not adjudicate every hard finding")

        requests_by_id = {
            str(item.get("cluster_id") or ""): item
            for item in repair_requests
            if isinstance(item, dict)
        }
        normalized_patches: list[dict[str, Any]] = []
        used_clusters: set[str] = set()
        for item in patches:
            if not isinstance(item, dict):
                raise RuntimeError("lecture repair Agent returned an invalid patch")
            cluster_id = str(item.get("cluster_id") or "")
            request = requests_by_id.get(cluster_id)
            if request is None or cluster_id in used_clusters:
                raise RuntimeError("lecture repair Agent returned an unknown or duplicate cluster")
            used_clusters.add(cluster_id)
            start_line = int(item.get("editable_start_line") or 0)
            end_line = int(item.get("editable_end_line") or 0)
            expected_hash = str(item.get("expected_old_hash") or "")
            if (
                start_line != int(request["editable_start_line"])
                or end_line != int(request["editable_end_line"])
                or expected_hash != str(request["expected_old_hash"])
            ):
                raise RuntimeError("lecture repair Agent changed a cluster's authorization")
            replacement = item.get("replacement")
            if not isinstance(replacement, str) or "```" in replacement:
                raise RuntimeError("lecture repair Agent returned invalid replacement text")
            covered = [str(value) for value in item.get("finding_ids") or []]
            if not covered or not set(covered).issubset(finding_ids):
                raise RuntimeError("lecture repair Agent patch has invalid finding coverage")
            normalized_patches.append(
                {
                    "cluster_id": cluster_id,
                    "finding_ids": covered,
                    "editable_start_line": start_line,
                    "editable_end_line": end_line,
                    "expected_old_hash": expected_hash,
                    "replacement": replacement,
                }
            )
        valid_ids = {
            item["finding_id"] for item in normalized_decisions if item["status"] == "VALID"
        }
        covered_ids = {
            finding_id for item in normalized_patches for finding_id in item["finding_ids"]
        }
        if covered_ids != valid_ids:
            raise RuntimeError("lecture repair Agent did not patch exactly the VALID findings")
        return {
            "finding_decisions": normalized_decisions,
            "patches": normalized_patches,
            "summary": str(parsed.get("summary") or "").strip(),
        }
