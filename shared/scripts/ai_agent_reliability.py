from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
POLICY_PATH = APP_PATHS.settings_dir / "ai_agent_reliability_policy.json"
TASKS_PATH = APP_PATHS.cache_dir / "ai_agent_tasks.json"
JOURNAL_PATH = APP_PATHS.cache_dir / "ai_agent_operation_journal.json"
USAGE_LEDGER_PATH = APP_PATHS.cache_dir / "ai_agent_usage_ledger.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


@dataclass(slots=True)
class ReliabilityPolicy:
    confirm_mutations: bool = True
    # Kept for backward-compatible loading of existing policy files. Ordinary
    # requests no longer show a confirmation dialog; hard limits below remain
    # the actual spending guard.
    show_preflight: bool = False
    single_request_limit: float = 0.50
    daily_limit: float = 3.00
    currency: str = "CNY"
    auto_notify: bool = True


class ReliabilityPolicyStore:
    def __init__(self, path: Path = POLICY_PATH) -> None:
        self.path = Path(path)
        raw = _read_json(self.path, {})
        allowed = set(ReliabilityPolicy.__dataclass_fields__)
        values = {key: value for key, value in raw.items() if key in allowed} if isinstance(raw, dict) else {}
        try:
            self.policy = ReliabilityPolicy(**values)
        except (TypeError, ValueError):
            self.policy = ReliabilityPolicy()
        # Local mutations must always require an operation preview and explicit
        # user confirmation.  Keep the persisted field only for compatibility
        # with older policy files; a stored false value must never bypass it.
        self.policy.confirm_mutations = True

    def save(self) -> None:
        if self.policy.single_request_limit < 0 or self.policy.daily_limit < 0:
            raise ValueError("费用上限不能为负数。")
        self.policy.confirm_mutations = True
        _atomic_json(self.path, asdict(self.policy))


@dataclass(slots=True)
class TaskRecord:
    id: str
    conversation_id: str
    profile_id: str
    question: str
    context: dict[str, Any]
    state: str = "queued"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    error: str = ""
    run_id: str = ""


class BackgroundTaskStore:
    def __init__(self, path: Path = TASKS_PATH) -> None:
        self.path = Path(path)
        self.records: list[TaskRecord] = []
        self._load()

    def _load(self) -> None:
        raw = _read_json(self.path, {})
        rows = raw.get("tasks", []) if isinstance(raw, dict) else []
        allowed = set(TaskRecord.__dataclass_fields__)
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                self.records.append(TaskRecord(**{key: value for key, value in item.items() if key in allowed}))
            except (TypeError, ValueError):
                continue
        # A process cannot still own a task after a fresh application start.
        for record in self.records:
            if record.state in {"queued", "running"}:
                record.state = "interrupted"
                record.error = "程序在任务完成前退出；可在任务窗口中重试。"
                record.updated_at = _now()
        self.records = self.records[-200:]
        if any(item.state == "interrupted" for item in self.records):
            self._save()

    def _save(self) -> None:
        _atomic_json(self.path, {"version": 1, "tasks": [asdict(item) for item in self.records[-200:]]})

    def create(self, conversation_id: str, profile_id: str, question: str, context: dict[str, Any]) -> TaskRecord:
        record = TaskRecord(uuid.uuid4().hex, conversation_id, profile_id, question, dict(context))
        self.records.append(record)
        self._save()
        return record

    def update(self, task_id: str, state: str, *, error: str = "", run_id: str = "") -> TaskRecord | None:
        record = next((item for item in self.records if item.id == task_id), None)
        if record is None:
            return None
        record.state = str(state)
        record.error = str(error)
        record.run_id = str(run_id or record.run_id)
        record.updated_at = _now()
        self._save()
        return record

    def retryable(self) -> list[TaskRecord]:
        return [item for item in reversed(self.records) if item.state in {"failed", "interrupted", "canceled"}]


@dataclass(slots=True)
class OperationRecord:
    id: str
    task_id: str
    tool_name: str
    preview: dict[str, Any]
    state: str = "awaiting_confirmation"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    result: dict[str, Any] = field(default_factory=dict)


class OperationJournal:
    def __init__(self, path: Path = JOURNAL_PATH) -> None:
        self.path = Path(path)
        raw = _read_json(self.path, {})
        self.records: list[OperationRecord] = []
        allowed = set(OperationRecord.__dataclass_fields__)
        for item in raw.get("operations", []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            try:
                self.records.append(OperationRecord(**{key: value for key, value in item.items() if key in allowed}))
            except (TypeError, ValueError):
                continue
        for record in self.records:
            if record.state in {"awaiting_confirmation", "executing"}:
                record.state = "interrupted"
                record.updated_at = _now()
        self.records = self.records[-300:]
        self._save()

    def _save(self) -> None:
        _atomic_json(self.path, {"version": 1, "operations": [asdict(item) for item in self.records[-300:]]})

    def begin(self, task_id: str, tool_name: str, preview: dict[str, Any]) -> OperationRecord:
        record = OperationRecord(uuid.uuid4().hex, task_id, tool_name, dict(preview))
        self.records.append(record)
        self._save()
        return record

    def finish(self, operation_id: str, state: str, result: dict[str, Any] | None = None) -> None:
        record = next((item for item in self.records if item.id == operation_id), None)
        if record is None:
            return
        record.state = str(state)
        record.result = dict(result or {})
        record.updated_at = _now()
        self._save()

    def unresolved(self) -> list[OperationRecord]:
        return [item for item in reversed(self.records) if item.state in {"interrupted", "failed"}]

    def undoable(self) -> list[OperationRecord]:
        return [
            item
            for item in reversed(self.records)
            if item.state in {"completed", "interrupted", "failed"}
            and bool(item.preview.get("recovery"))
        ]

    def rollback(self, operation_id: str) -> dict[str, Any]:
        record = next((item for item in self.records if item.id == operation_id), None)
        if record is None:
            raise ValueError("没有找到该操作记录。")
        recovery = record.preview.get("recovery") or []
        if not recovery:
            raise ValueError("该操作没有可恢复的正文快照。PDF 生成操作可直接重新执行。")
        restored: list[str] = []
        for item in recovery:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            target = Path(str(item["path"])).resolve()
            if item.get("existed_before"):
                temporary = target.with_suffix(target.suffix + ".ai-recovery.tmp")
                temporary.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(str(item.get("original_text") or ""), encoding="utf-8")
                os.replace(temporary, target)
            else:
                target.unlink(missing_ok=True)
            restored.append(str(target))
        if not restored:
            raise ValueError("操作记录中没有可恢复的文件。")
        previous_state = record.state
        self.finish(operation_id, "rolled_back", {"restored_files": restored})
        return {
            "restored_files": restored,
            "count": len(restored),
            "operation_id": record.id,
            "tool_name": record.tool_name,
            "arguments": dict(record.preview.get("arguments") or {}),
            "previous_state": previous_state,
        }


class UsageLedger:
    def __init__(self, path: Path = USAGE_LEDGER_PATH) -> None:
        self.path = Path(path)

    def _rows(self) -> list[dict[str, Any]]:
        raw = _read_json(self.path, {})
        return [dict(item) for item in raw.get("entries", []) if isinstance(item, dict)] if isinstance(raw, dict) else []

    def record(self, task_id: str, estimated_amount: float | None, actual_amount: float | None = None) -> None:
        rows = self._rows()
        rows.append(
            {
                "task_id": str(task_id),
                "created_at": _now(),
                "estimated_amount": estimated_amount,
                "actual_amount": actual_amount,
            }
        )
        _atomic_json(self.path, {"version": 1, "entries": rows[-1000:]})

    def today_total(self) -> float:
        prefix = date.today().isoformat()
        total = 0.0
        for row in self._rows():
            if not str(row.get("created_at") or "").startswith(prefix):
                continue
            value = row.get("actual_amount")
            if not isinstance(value, (int, float)):
                value = row.get("estimated_amount")
            if isinstance(value, (int, float)):
                total += float(value)
        return round(total, 6)

    def update_actual(self, task_id: str, actual_amount: float) -> bool:
        rows = self._rows()
        changed = False
        for row in reversed(rows):
            if str(row.get("task_id") or "") == str(task_id):
                row["actual_amount"] = float(actual_amount)
                changed = True
                break
        if changed:
            _atomic_json(self.path, {"version": 1, "entries": rows[-1000:]})
        return changed
