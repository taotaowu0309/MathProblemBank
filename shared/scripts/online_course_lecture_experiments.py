from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.scripts.online_course_lecture_quality import content_hash


EXPERIMENT_SCHEMA_VERSION = 1
OFFLINE_GUARD_ENV = "MPB_LECTURE_EVAL_OFFLINE"


class ExperimentMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_case_recall: float = Field(ge=0, le=1)
    false_accept_rate: float = Field(ge=0, le=1)
    false_reject_rate: float = Field(ge=0, le=1)
    live_added_strong_model_calls: int = Field(ge=0)
    ordinary_audit_model_calls: int = Field(ge=0)
    three_hour_package_seconds: float = Field(ge=0)
    audit_p95_seconds: float = Field(ge=0)
    cache_hit_rate: float = Field(ge=0, le=1)


class ExperimentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = EXPERIMENT_SCHEMA_VERSION
    experiment_id: str
    created_at: str
    candidate_id: str
    optimizer: Literal["manual", "dspy", "gepa", "inspect"]
    editable_surfaces: list[str]
    corpus_hash: str = Field(min_length=64, max_length=64)
    split_manifest_hash: str = Field(min_length=64, max_length=64)
    evaluator_version: str
    metrics: ExperimentMetrics
    eligible_for_promotion: bool
    rejection_reasons: list[str]

    @model_validator(mode="after")
    def validate_eligibility(self) -> "ExperimentRecord":
        reasons = promotion_rejection_reasons(self.metrics)
        if self.eligible_for_promotion != (not reasons):
            raise ValueError("promotion eligibility does not match frozen gates")
        if self.rejection_reasons != reasons:
            raise ValueError("promotion rejection reasons do not match frozen gates")
        return self


def require_offline_mode() -> None:
    if os.environ.get(OFFLINE_GUARD_ENV) != "1":
        raise RuntimeError(
            f"Lecture optimizer/eval code is offline-only; set {OFFLINE_GUARD_ENV}=1 "
            "in an isolated evaluation process."
        )


def optional_tool_availability() -> dict[str, bool]:
    return {
        "inspect": importlib.util.find_spec("inspect_ai") is not None,
        "dspy": importlib.util.find_spec("dspy") is not None,
        "gepa": importlib.util.find_spec("gepa") is not None,
    }


def promotion_rejection_reasons(metrics: ExperimentMetrics) -> list[str]:
    reasons: list[str] = []
    if metrics.hard_case_recall < 1.0:
        reasons.append("hard_case_recall_below_1.0")
    if metrics.false_accept_rate > 0:
        reasons.append("false_accept_rate_above_0")
    if metrics.false_reject_rate > 0.10:
        reasons.append("false_reject_rate_above_0.10")
    if metrics.live_added_strong_model_calls != 0:
        reasons.append("live_path_added_strong_model_calls")
    if metrics.ordinary_audit_model_calls > 1:
        reasons.append("ordinary_path_uses_more_than_one_critic")
    if metrics.three_hour_package_seconds > 300:
        reasons.append("three_hour_package_exceeds_300_seconds")
    return reasons


def build_experiment_record(
    *,
    candidate_id: str,
    optimizer: Literal["manual", "dspy", "gepa", "inspect"],
    editable_surfaces: list[str],
    corpus_path: Path,
    split_manifest_path: Path,
    evaluator_version: str,
    metrics: ExperimentMetrics,
) -> ExperimentRecord:
    require_offline_mode()
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    split_manifest = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    reasons = promotion_rejection_reasons(metrics)
    identity = {
        "candidate_id": candidate_id,
        "optimizer": optimizer,
        "editable_surfaces": editable_surfaces,
        "corpus_hash": content_hash(corpus),
        "split_manifest_hash": content_hash(split_manifest),
        "evaluator_version": evaluator_version,
        "metrics": metrics.model_dump(mode="json"),
    }
    return ExperimentRecord(
        experiment_id="lecture-exp-" + content_hash(identity)[:20],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        candidate_id=candidate_id,
        optimizer=optimizer,
        editable_surfaces=editable_surfaces,
        corpus_hash=identity["corpus_hash"],
        split_manifest_hash=identity["split_manifest_hash"],
        evaluator_version=evaluator_version,
        metrics=metrics,
        eligible_for_promotion=not reasons,
        rejection_reasons=reasons,
    )


def append_experiment_log(path: Path, record: ExperimentRecord) -> None:
    require_offline_mode()
    existing: list[dict[str, object]] = []
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = list(payload.get("experiments") or [])
    if any(item.get("experiment_id") == record.experiment_id for item in existing):
        return
    existing.append(record.model_dump(mode="json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"schema_version": EXPERIMENT_SCHEMA_VERSION, "experiments": existing},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)

