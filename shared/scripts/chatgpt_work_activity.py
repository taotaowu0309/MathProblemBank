"""Activity log of ChatGPT Work's command-line calls.

Every ``chatgpt_work`` command appends a start line when it begins and a result
line when it ends: how long it ran, what was asked, whether it succeeded and a
short result summary.  A start line with no result line is a command that is
still running or was killed; the log alone cannot tell which, so it keeps the
process id for checking on that machine.  The log lives in ``work_activity/<machine>/<date>.jsonl`` in the project folder, so
it travels with the subject data through Syncthing (one file per machine, no
sync conflicts) and stays out of Git.  The gap between one call's end and the
next call's start is the time Work spent reading and writing on its own.

Logging never changes a command's result: any failure here is swallowed.
"""

from __future__ import annotations

import json
import os
import re
import socket
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS

PROJECT_ROOT = APP_PATHS.user_data_root
ACTIVITY_DIR_NAME = "work_activity"
MAX_STRING = 400
MAX_ITEMS = 20
MAX_PROGRESS_LINES = 40


def activity_root(project_root: Path | None = None) -> Path:
    return (project_root or PROJECT_ROOT) / ACTIVITY_DIR_NAME


def machine_name() -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", socket.gethostname() or "unknown").strip("._")
    return name or "unknown"


def summarize(value: Any, depth: int = 0) -> Any:
    """Shrink a value for the log: short strings, few items, shallow nesting."""
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + f"…(+{len(value) - MAX_STRING} chars)"
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 3:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict) or (hasattr(value, "keys") and hasattr(value, "__getitem__")):
        keys = list(value.keys())
        out = {str(key): summarize(value[key], depth + 1) for key in keys[:MAX_ITEMS]}
        if len(keys) > MAX_ITEMS:
            out["…"] = f"+{len(keys) - MAX_ITEMS} keys"
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [summarize(item, depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append(f"…+{len(items) - MAX_ITEMS} items")
        return out
    return summarize(str(value), depth)


def request_summary(argv: list[str]) -> Any:
    """For request-file commands, log the request itself, not just its path."""
    for item in argv:
        if item.lower().endswith(".json"):
            try:
                return summarize(json.loads(Path(item).read_text(encoding="utf-8-sig")))
            except Exception:  # noqa: BLE001 - the command reports its own error
                return None
    return None


def _append(record: dict[str, Any], started: datetime, project_root: Path | None) -> Path:
    # Both lines of a call go in the file of its start day, so they stay together.
    path = activity_root(project_root) / record["machine"] / f"{started:%Y-%m-%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


def record_start(argv: list[str], started: datetime, *,
                 project_root: Path | None = None) -> str:
    """Append a start line; return the id that ties it to the result line."""
    try:
        started = started.astimezone()
        call_id = uuid.uuid4().hex[:12]
        record = {
            "event": "start",
            "call_id": call_id,
            "started": started.isoformat(timespec="seconds"),
            "machine": machine_name(),
            "pid": os.getpid(),
            "command": " ".join(argv[:2]),
            "argv": [summarize(item) for item in argv],
        }
        request = request_summary(argv)
        if request is not None:
            record["request"] = request
        _append(record, started, project_root)
        return call_id
    except Exception:  # noqa: BLE001 - logging must never break a Work command
        return ""


def record_call(argv: list[str], started: datetime, finished: datetime, ok: bool, *,
                result: Any = None, error: str = "", progress: list[str] | None = None,
                call_id: str = "", project_root: Path | None = None) -> Path | None:
    """Append one call to today's log; return the log path, or None on failure."""
    try:
        started = started.astimezone()
        record = {
            "started": started.isoformat(timespec="seconds"),
            "finished": finished.astimezone().isoformat(timespec="seconds"),
            "duration_s": round((finished - started).total_seconds(), 2),
            "machine": machine_name(),
            "command": " ".join(argv[:2]),
            "argv": [summarize(item) for item in argv],
            "ok": ok,
        }
        if call_id:
            record["call_id"] = call_id
        request = request_summary(argv)
        if request is not None:
            record["request"] = request
        if ok:
            record["result"] = summarize(result)
        else:
            record["error"] = summarize(error)
        if progress:
            record["progress"] = [summarize(line) for line in progress[-MAX_PROGRESS_LINES:]]
        return _append(record, started, project_root)
    except Exception:  # noqa: BLE001 - logging must never break a Work command
        return None


def read_timeline(date: str | None = None, machine: str | None = None, *,
                  project_root: Path | None = None) -> dict[str, Any]:
    """Calls of one day in time order, with Work's own time between calls.

    ``unfinished`` lists commands that have a start line but no result line.
    """
    day = date or f"{datetime.now():%Y-%m-%d}"
    root = activity_root(project_root)
    files = sorted(root.glob(f"{machine or '*'}/{day}.jsonl")) if root.is_dir() else []
    rows: list[dict[str, Any]] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a line cut by an interrupted write or an in-flight sync
    calls = [row for row in rows if row.get("event") != "start"]
    ended = {call.get("call_id") for call in calls}
    unfinished = sorted((row for row in rows if row.get("event") == "start" and row.get("call_id") not in ended),
                        key=lambda row: row.get("started", ""))
    calls.sort(key=lambda row: (row.get("machine", ""), row.get("started", "")))
    previous: dict[str, datetime] = {}
    command_time = work_time = 0.0
    for call in calls:
        start = datetime.fromisoformat(call["started"])
        end = start + timedelta(seconds=float(call.get("duration_s") or 0))
        gap = previous.get(call.get("machine", ""))
        call["gap_before_s"] = round((start - gap).total_seconds(), 1) if gap else None
        previous[call.get("machine", "")] = end
        command_time += float(call.get("duration_s") or 0)
        if call["gap_before_s"]:
            work_time += max(call["gap_before_s"], 0.0)
    return {
        "date": day,
        "files": [str(path) for path in files],
        "calls": len(calls),
        "failed_calls": sum(1 for call in calls if not call.get("ok")),
        "unfinished": unfinished,
        "command_time_s": round(command_time, 1),
        "time_between_calls_s": round(work_time, 1),
        "timeline": calls,
    }
