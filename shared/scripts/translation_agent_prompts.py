from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.translation_project_service import UPSTREAM_SKILL_REVISION


SKILL_ROOT = APP_PATHS.application_root / "shared" / "skills"
UPSTREAM_SKILL_PATH = SKILL_ROOT / "vendor" / "mathtranslations" / "SKILL.md"
SOURCE_REPAIR_ADAPTER_PATH = SKILL_ROOT / "mathtranslations_source_repair" / "SKILL.md"
ACCEPTED_REPAIRS_PATH = (
    SKILL_ROOT
    / "mathtranslations_source_repair"
    / "references"
    / "accepted_repairs.json"
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SecondLayerPromptBundle:
    stable_prefix: str
    task_prompt: str
    upstream_revision: str
    stable_prefix_sha256: str


def load_stable_second_layer_prefix() -> tuple[str, str]:
    """Load the pinned upstream Skill and the higher-priority local adapter."""

    upstream = UPSTREAM_SKILL_PATH.read_text(encoding="utf-8")
    adapter = SOURCE_REPAIR_ADAPTER_PATH.read_text(encoding="utf-8")
    accepted_repairs = ACCEPTED_REPAIRS_PATH.read_text(encoding="utf-8")
    prefix = (
        "[UPSTREAM MATHTRANSLATIONS SKILL — FIDELITY REFERENCE]\n"
        + upstream.strip()
        + "\n\n[MATHPROBLEMBANK SECOND-LAYER ADAPTER — HIGHER PRIORITY]\n"
        + adapter.strip()
        + "\n\n[USER-ACCEPTED SOURCE-REPAIR MEMORY — APPLY GENERALIZED RULES]\n"
        + accepted_repairs.strip()
        + "\n"
    )
    return prefix, _sha256_text(prefix)


def build_second_layer_prompt(
    project: dict[str, Any],
    chapter: dict[str, Any],
    section: dict[str, Any],
    mineru_markdown: str,
    image_manifest: dict[str, Any],
    reference_candidates: list[dict[str, Any]] | None = None,
    deterministic_reference_mentions: list[dict[str, Any]] | None = None,
    chunk: dict[str, Any] | None = None,
) -> SecondLayerPromptBundle:
    """Build one section or contiguous section-chunk repair request."""

    stable_prefix, prefix_sha256 = load_stable_second_layer_prefix()
    locked = dict(project.get("second_layer_skill") or {})
    revision = str(locked.get("upstream_revision") or "")
    if revision != UPSTREAM_SKILL_REVISION:
        raise RuntimeError(
            "译本项目锁定的 MathTranslations revision 与当前 vendor 资源不一致；"
            "必须显式升级项目，不能在处理中静默漂移。"
        )
    upstream_sha256 = _sha256_text(UPSTREAM_SKILL_PATH.read_text(encoding="utf-8"))
    adapter_sha256 = _sha256_text(SOURCE_REPAIR_ADAPTER_PATH.read_text(encoding="utf-8"))
    accepted_repairs_sha256 = _sha256_text(
        ACCEPTED_REPAIRS_PATH.read_text(encoding="utf-8")
    )
    if (
        str(locked.get("upstream_sha256") or "") != upstream_sha256
        or str(locked.get("adapter_sha256") or "") != adapter_sha256
        or str(locked.get("accepted_repairs_sha256") or "")
        != accepted_repairs_sha256
    ):
        raise RuntimeError(
            "译本项目锁定的第二层 Skill/Adapter 文件哈希与当前资源不一致；"
            "必须显式升级项目，不能在处理中静默改变校正规则。"
        )
    figures = [
        item
        for item in image_manifest.get("figures") or []
        if str(item.get("figure_id") or "") in set(section.get("figure_ids") or [])
    ]
    chunk_record = dict(chunk or {})
    task = {
        "mode": (
            "SECOND_LAYER_SOURCE_REPAIR_CHUNK"
            if chunk_record
            else "SECOND_LAYER_SOURCE_REPAIR"
        ),
        "project": {
            "code": project.get("code"),
            "title": project.get("title"),
            "author": project.get("author"),
        },
        "chapter": {
            "number": chapter.get("number"),
            "title": chapter.get("title"),
            "page_start": chapter.get("page_start"),
            "page_end": chapter.get("page_end"),
        },
        "section": {
            "id": section.get("id"),
            "number": section.get("number"),
            "title": section.get("title"),
            "page_start": section.get("page_start"),
            "page_end": section.get("page_end"),
        },
        "figures": figures,
        "allowed_reference_labels": list(reference_candidates or []),
        "deterministic_minimum_reference_mentions": list(
            deterministic_reference_mentions or []
        ),
        "mineru_markdown": str(mineru_markdown),
        "output_contract": {
            "format": "json_object",
            "required": [
                "corrected_markdown",
                "report_markdown",
                "reference_catalog",
            ],
            "corrected_markdown": (
                "complete source-language Markdown for this section; preserve the language "
                "printed in the book and never translate"
            ),
            "report_markdown": "short repair and figure-audit report with page locators",
            "reference_catalog": {
                "authorship": (
                    "you, the second-layer agent, must explicitly author this catalog"
                ),
                "labels": (
                    "ASCII English labels only; choose exact labels only from "
                    "allowed_reference_labels"
                ),
                "definitions": (
                    "every numbered chapter/section/subsection and every theorem, lemma, "
                    "proposition, corollary, definition, example, exercise, remark, "
                    "equation, figure and table defined in this target"
                ),
                "references": (
                    "every explicit prose occurrence referring to any listed object, "
                    "including forward references; each record must contain kind, number, "
                    "label and the exact source_text substring from corrected_markdown"
                ),
                "shape": {
                    "definitions": [
                        {
                            "kind": "section",
                            "number": "1.1",
                            "label": "sec:1.1",
                            "source_text": "Section 1.1",
                            "title": "Notation",
                            "target_id": str(section.get("id") or ""),
                        }
                    ],
                    "references": [
                        {
                            "kind": "theorem",
                            "number": "5.4.4",
                            "label": "thm:5.4.4",
                            "source_text": "Theorem 5.4.4",
                            "occurrences": 1,
                            "target_id": str(section.get("id") or ""),
                        }
                    ],
                },
            },
        },
    }
    if chunk_record:
        task["chunk"] = {
            "index": int(chunk_record.get("index") or 1),
            "count": int(chunk_record.get("count") or 1),
            "is_first": bool(chunk_record.get("is_first")),
            "is_last": bool(chunk_record.get("is_last")),
            "page_numbers": [
                int(value) for value in chunk_record.get("page_numbers") or []
            ],
        }
    scope_instruction = (
        "Repair exactly this one contiguous chunk of the complete section in the "
        "following JSON task. Preserve every character-bearing paragraph in this chunk. "
        "Only the first chunk may retain the section heading; never add a continuation "
        "heading, transition, batch note, or content from another chunk. "
        if chunk_record
        else "Repair exactly the one complete section in the following JSON task. "
    )
    task_prompt = (
        scope_instruction
        +
        "The attached full chapter PDF, or exact rendered pages from it, is the source of truth; "
        "inspect only the task's page range. "
        "Attached PNG files are program-generated source crops. Do not translate, write final LaTeX, "
        "redraw figures, change the source language, or launch a verifier. "
        "An English source must remain fully English; any other source must remain in its "
        "original language. Identify every definition and every backward or forward "
        "cross-reference, author the English-label reference_catalog, and never omit a "
        "reference merely because its target is in a later section. Return only the "
        "required JSON object.\n\n"
        + json.dumps(task, ensure_ascii=False, indent=2)
    )
    return SecondLayerPromptBundle(
        stable_prefix=stable_prefix,
        task_prompt=task_prompt,
        upstream_revision=revision,
        stable_prefix_sha256=prefix_sha256,
    )


__all__ = [
    "SecondLayerPromptBundle",
    "ACCEPTED_REPAIRS_PATH",
    "build_second_layer_prompt",
    "load_stable_second_layer_prefix",
]
