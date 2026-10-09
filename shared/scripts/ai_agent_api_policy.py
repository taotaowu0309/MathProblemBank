"""API request plumbing; provider/account quotas are the only quota authority."""
from __future__ import annotations

from contextlib import closing
import contextvars
import functools
import re
import sqlite3
import uuid
from pathlib import Path

from shared.scripts.application_paths import APP_PATHS

LOW_LIMIT = 6000
HIGH_LIMIT = 12000
TASK_LIMIT = 48000
LEDGER_PATH = APP_PATHS.settings_dir / "api_budget.sqlite3"
_task = contextvars.ContextVar("api_budget_task", default=None)
_repair = contextvars.ContextVar("api_budget_repair", default=False)


def budgeted_task(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        token = None if _task.get() is not None else _task.set(uuid.uuid4().hex)
        stage = str(kwargs.get("stage", "")).casefold()
        repair_token = _repair.set(_repair.get() or kwargs.get("reasoning_preset") in {"deep", "max"} or stage in {
            "lecture targeted mathematical repair", "lecture repair adjudication and minimal patch"
        })
        try:
            return function(*args, **kwargs)
        finally:
            _repair.reset(repair_token)
            if token is not None:
                _task.reset(token)
    return wrapped


def submit_with_budget(executor, function, *args, **kwargs):
    context = contextvars.copy_context()
    return executor.submit(context.run, function, *args, **kwargs)


def complex_request(payload):
    """Only short user instructions can opt in, never a long source document."""
    messages = payload.get("input", payload.get("messages", []))
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        text = str(content)
        return len(text) <= 2000 and bool(re.search(
            r"复杂证明|严格证明|完整证明|证明.*充要|Zorn|well-defined|prove.*if and only if|"
            r"rigorous proof|targeted repair|局部修复|高强度思考", text, re.I))
    return False


def reserve(payload, *, path=None):
    # Local quota enforcement is disabled.  The account/provider owns quota;
    # this ledger remains observational only and must never truncate or reject
    # a request based on accumulated usage.
    field = "max_output_tokens" if "max_output_tokens" in payload else "max_tokens"
    result = dict(payload)
    requested = int(result.get(field) or 0)
    if requested > 0:
        result[field] = requested
    return result, (Path(path or LEDGER_PATH), "unlimited"), "high"


def settle(ticket, data):
    if ticket[1] == "unlimited":
        return
    path, request_id = ticket
    usage = data.get("usage") or {}
    tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if not isinstance(tokens, int) or tokens < 0:
        return  # Unknown usage retains the reservation, including interrupted requests.
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("UPDATE requests SET tokens=?,status=? WHERE id=?", (tokens, str(data.get("status", "received")), request_id))
