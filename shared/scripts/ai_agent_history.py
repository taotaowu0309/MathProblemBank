from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_attachments import ATTACHMENT_ROOT, attachment_manifest
from shared.scripts.ai_agent_quality import normalize_math_terminology


ROOT_DIR = APP_PATHS.application_root
HISTORY_PATH = APP_PATHS.cache_dir / "ai_agent_history.json"
HISTORY_ASSET_ROOT = APP_PATHS.cache_dir / "ai_agent_history_assets"
HISTORY_VIEW_STATE_PATH = APP_PATHS.cache_dir / "ai_agent_history_view_state.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def conversation_title(text: str) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return (compact[:24] + "…") if len(compact) > 24 else (compact or "新对话")


def safe_filename(text: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text or "")).strip(" .")
    value = re.sub(r"\s+", " ", value)[:72].strip(" .") or "对话"
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


def sanitize_assistant_text(text: str) -> str:
    """Remove terminal-encoding debris without touching normal mathematical question marks."""

    cleaned: list[str] = []
    for raw_line in str(text or "").splitlines():
        compact = re.sub(r"\s+", "", raw_line)
        question_count = compact.count("?")
        if question_count >= 8 and question_count / max(1, len(compact)) >= 0.35:
            continue
        line = re.sub(r"^[?*#\s]{2,}PDF\?\s*", "PDF：", raw_line, flags=re.IGNORECASE)
        cleaned.append(line)
    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    result = normalize_math_terminology(result)
    return result


def sanitize_history_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(raw or {})
    traces: list[dict[str, Any]] = []
    for item in metadata.get("tool_traces") or []:
        if not isinstance(item, dict):
            continue
        trace = dict(item)
        summary = sanitize_assistant_text(str(trace.get("summary") or ""))
        if not summary:
            summary = "工具已返回数据" if trace.get("ok") else "工具执行失败"
        trace["summary"] = summary
        traces.append(trace)
    if traces:
        metadata["tool_traces"] = traces
    if isinstance(metadata.get("runs"), list):
        metadata["runs"] = [
            sanitize_history_metadata(item) if isinstance(item, dict) else item
            for item in metadata["runs"]
        ]
    note = sanitize_assistant_text(str(metadata.get("local_recovery_note") or ""))
    if not note and metadata.get("final_preview_path") and metadata.get("visual_passed"):
        note = "最终图形已由本地 XeLaTeX 编译并通过视觉检查；没有写入正式项目。"
    if note:
        metadata["local_recovery_note"] = note
    elif "local_recovery_note" in metadata:
        metadata.pop("local_recovery_note", None)
    return metadata


def _persist_artifact_file(conversation_id: str, source: str) -> str:
    target = Path(str(source or "")).expanduser()
    suffix = target.suffix.casefold()
    if not target.is_file() or suffix not in {".pdf", ".png", ".svg", ".json", ".wl"}:
        return str(source or "")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(conversation_id or ""))[:80] or "conversation"
    destination_dir = (HISTORY_ASSET_ROOT / safe_id).resolve()
    try:
        if destination_dir == target.resolve().parent:
            return str(target.resolve())
    except OSError:
        return str(source or "")
    if target.stat().st_size > 50 * 1024 * 1024:
        return str(source or "")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()[:20]
    destination = destination_dir / f"figure_{digest}{suffix}"
    destination_dir.mkdir(parents=True, exist_ok=True)
    if not destination.is_file() or destination.stat().st_size != target.stat().st_size:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(target, temporary)
        os.replace(temporary, destination)
    return str(destination)


def _persist_preview_pdf(conversation_id: str, source: str) -> str:
    target = Path(str(source or ""))
    if target.suffix.casefold() != ".pdf":
        return str(source or "")
    return _persist_artifact_file(conversation_id, source)


def persist_preview_assets(conversation_id: str, raw: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    metadata = sanitize_history_metadata(raw)
    changed = False
    path_map: dict[str, str] = {}

    def update_details(details: dict[str, Any]) -> None:
        nonlocal changed
        traces = details.get("tool_traces")
        if isinstance(traces, list):
            for trace in traces:
                if not isinstance(trace, dict) or not trace.get("ok"):
                    continue
                evidence = trace.get("evidence")
                if not isinstance(evidence, dict):
                    continue
                if trace.get("name") == "render_math_figure_preview":
                    original = str(evidence.get("pdf_path") or "")
                    persisted = _persist_preview_pdf(conversation_id, original)
                    if persisted and persisted != original:
                        evidence["pdf_path"] = persisted
                        path_map[original] = persisted
                        validation = evidence.get("visual_validation")
                        if isinstance(validation, dict) and str(validation.get("path") or "") == original:
                            validation["path"] = persisted
                        changed = True
                compute = evidence.get("compute")
                if not isinstance(compute, dict):
                    continue
                for artifact in compute.get("artifacts") or []:
                    if not isinstance(artifact, dict):
                        continue
                    original = str(artifact.get("absolute_path") or "")
                    persisted = _persist_artifact_file(conversation_id, original)
                    if persisted and persisted != original:
                        artifact["absolute_path"] = persisted
                        try:
                            artifact["relative_path"] = Path(persisted).resolve().relative_to(ROOT_DIR).as_posix()
                        except (OSError, ValueError):
                            artifact["relative_path"] = persisted
                        path_map[original] = persisted
                        changed = True
                for key in ("pdf_path", "png_path", "svg_path", "source_path", "metadata_path"):
                    original = str(compute.get(key) or "")
                    persisted = path_map.get(original) or _persist_artifact_file(conversation_id, original)
                    if persisted and persisted != original:
                        compute[key] = persisted
                        path_map[original] = persisted
                        changed = True
                validation = compute.get("visual_validation")
                if isinstance(validation, dict):
                    original_path = str(validation.get("path") or "")
                    if original_path in path_map:
                        validation["path"] = path_map[original_path]
                        changed = True
        direct = str(details.get("final_preview_path") or "")
        if direct:
            persisted = path_map.get(direct) or _persist_preview_pdf(conversation_id, direct)
            if persisted != direct:
                details["final_preview_path"] = persisted
                changed = True
        for run in details.get("runs") or []:
            if isinstance(run, dict):
                update_details(run)

    update_details(metadata)
    return metadata, changed


@dataclass(slots=True)
class ConversationRecord:
    id: str
    title: str
    created_at: str
    updated_at: str
    profile_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConversationRecord":
        messages = []
        for item in raw.get("messages", []):
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            content = (
                sanitize_assistant_text(str(item.get("content") or ""))
                if item.get("role") == "assistant"
                else str(item.get("content") or "")
            )
            attachments = attachment_manifest(item.get("attachments") or [])
            if not content.strip() and not attachments:
                continue
            message: dict[str, Any] = {
                "role": str(item.get("role") or ""),
                "content": content,
            }
            if attachments:
                message["attachments"] = attachments
            messages.append(message)
        created = str(raw.get("created_at") or _now())
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex),
            title=conversation_title(str(raw.get("title") or (messages[0]["content"] if messages else "新对话"))),
            created_at=created,
            updated_at=str(raw.get("updated_at") or created),
            profile_id=str(raw.get("profile_id") or ""),
            messages=messages,
            metadata=sanitize_history_metadata(dict(raw.get("metadata") or {})),
        )


def render_conversation_txt(record: ConversationRecord) -> str:
    lines = [
        f"标题：{record.title}",
        f"创建时间：{record.created_at}",
        f"更新时间：{record.updated_at}",
        "",
    ]
    for message in record.messages:
        label = "你" if message["role"] == "user" else "AI"
        lines.extend((f"===== {label} =====", message["content"].rstrip(), ""))
        attachments = message.get("attachments") or []
        if attachments:
            lines.append("附件：" + "、".join(str(item.get("name") or "附件") for item in attachments))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class ConversationHistoryStore:
    def __init__(self, path: Path = HISTORY_PATH) -> None:
        self.path = Path(path)
        self.records: list[ConversationRecord] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, dict):
            raw = raw.get("conversations", [])
        if not isinstance(raw, list):
            raw = []
        self.records = [ConversationRecord.from_dict(item) for item in raw if isinstance(item, dict)]
        self.records.sort(key=lambda item: item.updated_at, reverse=True)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "conversations": [asdict(item) for item in self.records]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def all(self) -> list[ConversationRecord]:
        return list(self.records)

    def reload(self) -> None:
        """Reload externally added conversations without recreating the panel."""
        self._load()

    def get(self, conversation_id: str) -> ConversationRecord | None:
        return next((item for item in self.records if item.id == conversation_id), None)

    def materialize_preview_assets(self, conversation_id: str) -> ConversationRecord | None:
        record = self.get(conversation_id)
        if record is None:
            return None
        metadata, changed = persist_preview_assets(record.id, record.metadata)
        if changed:
            record.metadata = metadata
            self._save()
        return record

    def upsert(
        self,
        conversation_id: str | None,
        messages: list[dict[str, Any]],
        profile_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ConversationRecord:
        normalized: list[dict[str, Any]] = []
        for item in messages:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            attachments = attachment_manifest(item.get("attachments") or [])
            if role not in {"user", "assistant"} or (not content.strip() and not attachments):
                continue
            message: dict[str, Any] = {"role": role, "content": content}
            if attachments:
                message["attachments"] = attachments
            normalized.append(message)
        if not normalized:
            raise ValueError("空对话不能写入历史记录。")
        record = self.get(str(conversation_id or ""))
        timestamp = _now()
        if record is None:
            record = ConversationRecord(
                id=str(conversation_id or uuid.uuid4().hex),
                title=conversation_title(normalized[0]["content"]),
                created_at=timestamp,
                updated_at=timestamp,
            )
            self.records.append(record)
        record.messages = [dict(item) for item in normalized]
        record.profile_id = str(profile_id or record.profile_id)
        if metadata is not None:
            record.metadata = dict(metadata)
        record.updated_at = timestamp
        self.records.sort(key=lambda item: item.updated_at, reverse=True)
        self._save()
        return record

    def delete(self, conversation_ids: list[str]) -> int:
        selected = {str(conversation_id) for conversation_id in conversation_ids if str(conversation_id)}
        if not selected:
            raise ValueError("请至少选择一个需要删除的对话。")
        attachment_paths = {
            str(attachment.get("path") or "")
            for record in self.records
            if record.id in selected
            for message in record.messages
            for attachment in message.get("attachments") or []
            if isinstance(attachment, dict) and attachment.get("path")
        }
        before = len(self.records)
        self.records = [record for record in self.records if record.id not in selected]
        deleted = before - len(self.records)
        if deleted:
            self._save()
            for conversation_id in selected:
                safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)[:80] or "conversation"
                asset_dir = (HISTORY_ASSET_ROOT / safe_id).resolve()
                if asset_dir.parent == HISTORY_ASSET_ROOT.resolve() and asset_dir.is_dir():
                    shutil.rmtree(asset_dir)
            retained_paths = {
                str(attachment.get("path") or "")
                for record in self.records
                for message in record.messages
                for attachment in message.get("attachments") or []
                if isinstance(attachment, dict) and attachment.get("path")
            }
            attachment_root = ATTACHMENT_ROOT.resolve()
            for raw_path in attachment_paths - retained_paths:
                target = Path(raw_path).resolve()
                if target.parent == attachment_root:
                    target.unlink(missing_ok=True)
        return deleted

    def export_txt(self, conversation_ids: list[str], destination: Path) -> list[Path]:
        selected_ids = set(conversation_ids)
        selected = [record for record in self.records if record.id in selected_ids]
        if not selected:
            raise ValueError("请至少选择一个需要导出的对话。")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        exported: list[Path] = []
        used_names: set[str] = set()
        for record in selected:
            stamp = re.sub(r"\D", "", record.created_at[:16]) or "conversation"
            stem = safe_filename(f"{stamp}_{record.title}_{record.id[:6]}")
            candidate = stem
            suffix = 2
            while candidate.casefold() in used_names or (destination / f"{candidate}.txt").exists():
                candidate = f"{stem}_{suffix}"
                suffix += 1
            used_names.add(candidate.casefold())
            path = destination / f"{candidate}.txt"
            path.write_text(render_conversation_txt(record), encoding="utf-8-sig")
            exported.append(path)
        return exported


class ConversationViewStateStore:
    """Persist per-conversation reading positions without changing history order."""

    def __init__(self, path: Path = HISTORY_VIEW_STATE_PATH) -> None:
        self.path = Path(path)
        self.positions: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        positions = raw.get("positions", {}) if isinstance(raw, dict) else {}
        if not isinstance(positions, dict):
            positions = {}
        self.positions = {
            str(key): {
                "value": max(0, int(value.get("value") or 0)),
                "maximum": max(0, int(value.get("maximum") or 0)),
            }
            for key, value in positions.items()
            if str(key) and isinstance(value, dict)
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "positions": self.positions}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def remember(self, conversation_id: str, value: int, maximum: int) -> None:
        key = str(conversation_id or "")
        if not key:
            return
        self.positions[key] = {
            "value": max(0, int(value)),
            "maximum": max(0, int(maximum)),
        }
        self._save()

    def position(self, conversation_id: str, current_maximum: int) -> int:
        stored = self.positions.get(str(conversation_id or ""))
        if not stored:
            return 0
        old_value = max(0, int(stored.get("value") or 0))
        old_maximum = max(0, int(stored.get("maximum") or 0))
        selected_maximum = max(0, int(current_maximum))
        if old_maximum <= 0:
            return min(old_value, selected_maximum)
        ratio = min(1.0, old_value / old_maximum)
        return min(selected_maximum, max(0, round(selected_maximum * ratio)))

    def delete(self, conversation_ids: list[str]) -> int:
        selected = {str(item) for item in conversation_ids if str(item)}
        before = len(self.positions)
        self.positions = {key: value for key, value in self.positions.items() if key not in selected}
        deleted = before - len(self.positions)
        if deleted:
            self._save()
        return deleted
