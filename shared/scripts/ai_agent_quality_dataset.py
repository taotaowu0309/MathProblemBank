from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
TRAINING_ROOT = ROOT_DIR / "shared" / "templates" / "ai_agent_training"
CURATED_PAIRS_PATH = TRAINING_ROOT / "math_quality_pairs.json"
GENERATED_PAIRS_PATH = APP_PATHS.cache_dir / "ai_agent_quality_pairs.json"
RUBRIC_PATH = TRAINING_ROOT / "math_quality_rubric.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _tokens(text: str) -> set[str]:
    value = str(text or "").casefold()
    result = set(re.findall(r"[a-z][a-z0-9_-]{1,}|\\[a-z]+", value))
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", value):
        result.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return result


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    return len(a & b) / max(1, len(a | b))


@dataclass(slots=True)
class QualityPair:
    id: str
    created_at: str
    updated_at: str
    task_kind: str
    prompt: str
    preferred_answer: str
    rejected_answer: str
    preference_reason: str
    context: dict[str, Any]
    source: str
    status: str = "active"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QualityPair":
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or raw.get("created_at") or _now()),
            task_kind=str(raw.get("task_kind") or "math_explanation"),
            prompt=str(raw.get("prompt") or "")[:12000],
            preferred_answer=str(raw.get("preferred_answer") or "")[:50000],
            rejected_answer=str(raw.get("rejected_answer") or "")[:50000],
            preference_reason=str(raw.get("preference_reason") or "")[:4000],
            context=dict(raw.get("context") or {}),
            source=str(raw.get("source") or "manual"),
            status="archived" if raw.get("status") == "archived" else "active",
        )


class MathQualityDataset:
    def __init__(
        self,
        curated_path: Path = CURATED_PAIRS_PATH,
        generated_path: Path = GENERATED_PAIRS_PATH,
        rubric_path: Path = RUBRIC_PATH,
    ) -> None:
        self.curated_path = Path(curated_path)
        self.generated_path = Path(generated_path)
        self.rubric_path = Path(rubric_path)

    @staticmethod
    def _read_list(path: Path) -> list[dict[str, Any]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def all(self, *, include_archived: bool = False) -> list[QualityPair]:
        records: dict[str, QualityPair] = {}
        for raw in self._read_list(self.curated_path) + self._read_list(self.generated_path):
            pair = QualityPair.from_dict(raw)
            records[pair.id] = pair
        values = sorted(records.values(), key=lambda item: item.updated_at, reverse=True)
        return values if include_archived else [item for item in values if item.status == "active"]

    def rubric(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.rubric_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return raw if isinstance(raw, dict) else {}

    def _save_generated(self, pairs: Iterable[QualityPair]) -> None:
        values = list(pairs)[-300:]
        self.generated_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.generated_path.with_suffix(self.generated_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps([asdict(item) for item in values], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.generated_path)

    def add(self, pair: QualityPair) -> QualityPair:
        generated = [QualityPair.from_dict(item) for item in self._read_list(self.generated_path)]
        generated = [item for item in generated if item.id != pair.id]
        pair.updated_at = _now()
        generated.append(pair)
        self._save_generated(generated)
        return pair

    def create(
        self,
        *,
        task_kind: str,
        prompt: str,
        preferred_answer: str,
        rejected_answer: str,
        preference_reason: str,
        context: dict[str, Any] | None = None,
        source: str = "manual",
    ) -> QualityPair:
        if not str(prompt).strip() or not str(preferred_answer).strip() or not str(rejected_answer).strip():
            raise ValueError("成对质量样例必须同时包含问题、优选回答和反例回答。")
        now = _now()
        return self.add(
            QualityPair(
                uuid.uuid4().hex,
                now,
                now,
                str(task_kind or "math_explanation"),
                str(prompt),
                str(preferred_answer),
                str(rejected_answer),
                str(preference_reason),
                dict(context or {}),
                str(source or "manual"),
            )
        )

    def update(
        self,
        pair_id: str,
        *,
        task_kind: str,
        prompt: str,
        preferred_answer: str,
        rejected_answer: str,
        preference_reason: str,
    ) -> QualityPair:
        existing = next((item for item in self.all(include_archived=True) if item.id == str(pair_id)), None)
        if existing is None:
            raise ValueError("没有找到这条数学质量样例。")
        if not str(prompt).strip() or not str(preferred_answer).strip() or not str(rejected_answer).strip():
            raise ValueError("成对质量样例必须同时包含问题、优选回答和反例回答。")
        existing.task_kind = str(task_kind or existing.task_kind)
        existing.prompt = str(prompt)
        existing.preferred_answer = str(preferred_answer)
        existing.rejected_answer = str(rejected_answer)
        existing.preference_reason = str(preference_reason)
        existing.source = "manual_override"
        return self.add(existing)

    def delete(self, pair_ids: list[str]) -> int:
        selected = {str(item) for item in pair_ids}
        generated = [QualityPair.from_dict(item) for item in self._read_list(self.generated_path)]
        before = len(generated)
        self._save_generated(item for item in generated if item.id not in selected)
        return before - len([item for item in generated if item.id not in selected])

    def derive_from_feedback(self, feedback: Iterable[Any]) -> dict[str, int]:
        helpful = [item for item in feedback if getattr(item, "rating", "") == "helpful" and getattr(item, "answer_excerpt", "")]
        rejected = [item for item in feedback if getattr(item, "rating", "") == "improve" and getattr(item, "answer_excerpt", "")]
        existing_keys = {
            (item.prompt.strip(), item.preferred_answer.strip(), item.rejected_answer.strip())
            for item in self.all(include_archived=True)
        }
        created = 0
        for bad in rejected:
            candidates = [
                good
                for good in helpful
                if _similarity(getattr(bad, "question", ""), getattr(good, "question", "")) >= 0.35
                and all(
                    not getattr(bad, "context", {}).get(key)
                    or not getattr(good, "context", {}).get(key)
                    or getattr(bad, "context", {}).get(key) == getattr(good, "context", {}).get(key)
                    for key in ("subject_name", "project_ref")
                )
            ]
            if not candidates:
                continue
            good = max(candidates, key=lambda item: _similarity(bad.question, item.question))
            key = (bad.question.strip(), good.answer_excerpt.strip(), bad.answer_excerpt.strip())
            if key in existing_keys:
                continue
            reasons = [str(issue) for issue in getattr(bad, "issues", [])]
            if getattr(bad, "note", ""):
                reasons.append(str(bad.note))
            self.create(
                task_kind="math_explanation",
                prompt=bad.question,
                preferred_answer=good.answer_excerpt,
                rejected_answer=bad.answer_excerpt,
                preference_reason="；".join(reasons) or "用户更偏好另一条相关回答。",
                context=dict(getattr(bad, "context", {}) or {}),
                source="paired_user_feedback",
            )
            existing_keys.add(key)
            created += 1
        return {"created_pairs": created, "total_pairs": len(self.all())}

    def relevant_examples(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        current = dict(context or {})
        ranked: list[tuple[float, QualityPair]] = []
        for pair in self.all():
            score = _similarity(prompt, pair.prompt)
            for key in ("subject_name", "project_ref"):
                if current.get(key) and pair.context.get(key) == current.get(key):
                    score += 0.25
            if score > 0:
                ranked.append((score, pair))
        ranked.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [
            {
                "task_kind": pair.task_kind,
                "prompt": pair.prompt,
                "preferred_answer": pair.preferred_answer,
                "rejected_answer": pair.rejected_answer,
                "preference_reason": pair.preference_reason,
                "comparison_scope": str(pair.context.get("comparison_scope") or "answer_quality"),
                "instruction": (
                    "只学习优选回答的解释顺序、连接深度、段落密度和前提处理方式，"
                    "同时避免反例暴露的表达问题；样例中的数学对象与结论不得迁移到当前问题，"
                    "当前本地题目和教材始终具有更高事实优先级。"
                ),
            }
            for _score, pair in ranked[: max(0, min(int(limit), 3))]
        ]
