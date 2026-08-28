from __future__ import annotations

import base64
import email.utils
import http.client
import json
import mimetypes
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from shared.scripts.ai_agent_config import ProviderProfile
from shared.scripts.ai_agent_operation_registry import FORMAL_WRITE_TOOL_NAMES


ProgressCallback = Callable[[str], None]
ToolCallback = Callable[[str, dict[str, Any]], dict[str, Any]]


def _message_images(message: dict[str, Any]) -> list[tuple[str, str]]:
    images: list[tuple[str, str]] = []
    for raw in message.get("attachments") or []:
        if not isinstance(raw, dict) or str(raw.get("kind") or "") != "image":
            continue
        path = Path(str(raw.get("path") or "")).expanduser()
        if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
            continue
        mime = str(raw.get("mime_type") or mimetypes.guess_type(path.name)[0] or "image/png")
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            continue
        images.append((mime, base64.b64encode(path.read_bytes()).decode("ascii")))
    return images


def _responses_message(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role") or "user")
    text = str(message.get("content") or "")
    images = _message_images(message) if role == "user" else []
    if not images:
        return {"role": role, "content": text}
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text or "请分析附件。"}]
    content.extend(
        {"type": "input_image", "image_url": f"data:{mime};base64,{data}"}
        for mime, data in images
    )
    return {"role": role, "content": content}


def _chat_message(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role") or "user")
    text = str(message.get("content") or "")
    images = _message_images(message) if role == "user" else []
    if not images:
        return {"role": role, "content": text}
    content: list[dict[str, Any]] = [{"type": "text", "text": text or "请分析附件。"}]
    content.extend(
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
        for mime, data in images
    )
    return {"role": role, "content": content}


def _anthropic_message(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role") or "user")
    text = str(message.get("content") or "")
    images = _message_images(message) if role == "user" else []
    if not images:
        return {"role": role, "content": text}
    content: list[dict[str, Any]] = []
    content.extend(
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
        for mime, data in images
    )
    content.append({"type": "text", "text": text or "请分析附件。"})
    return {"role": role, "content": content}


def _gemini_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(message.get("content") or "")
    parts: list[dict[str, Any]] = [{"text": text or "请分析附件。"}]
    if str(message.get("role") or "") == "user":
        parts.extend(
            {"inlineData": {"mimeType": mime, "data": data}}
            for mime, data in _message_images(message)
        )
    return parts

TOOL_BUDGET_FINALIZATION_PROMPT = (
    "\n\n工具调用预算已经用完。不得再请求或假装调用任何工具。"
    "请只根据已经返回的工具结果给出最终答复：明确区分已经成功完成、失败后已回滚和仍未完成的事项；"
    "不得把尚未执行的写入或编译声称为成功。"
)
# Live registry-backed set: a future formal OperationSpec is automatically
# hidden until authorization and receives duplicate-mutation protection.
MUTATING_TOOLS = FORMAL_WRITE_TOOL_NAMES


def _tool_visual_evidence(result: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(result.get("ok")):
        return []
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    raw_items = data.get("visual_evidence") if isinstance(data, dict) else []
    attachments: list[dict[str, Any]] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict) or str(raw.get("kind") or "") != "image":
            continue
        path = Path(str(raw.get("path") or "")).expanduser()
        if (
            not path.is_file()
            or path.stat().st_size > 50 * 1024 * 1024
            or path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        ):
            continue
        attachments.append(
            {
                **raw,
                "kind": "image",
                "path": str(path.resolve()),
                "mime_type": str(
                    raw.get("mime_type")
                    or mimetypes.guess_type(path.name)[0]
                    or "image/png"
                ),
            }
        )
        if len(attachments) >= 4:
            break
    return attachments


def _tool_visual_prompt(attachments: list[dict[str, Any]]) -> str:
    diagrams = [item for item in attachments if item.get("diagram_id")]
    if diagrams:
        evidence = [
            {
                "diagram_id": str(item.get("diagram_id") or ""),
                "backend": str(item.get("backend") or ""),
                "source_sha256": str(item.get("source_sha256") or ""),
            }
            for item in diagrams
        ]
        return (
            "<tool_generated_diagram_visual_evidence>\n"
            "These images are the actual renderings returned by the mathematical "
            "diagram tool. Inspect object/arrow correctness, labels, crossings, "
            "clipping, scale, whitespace, and reading order before accepting the "
            "diagram or revising its source and calling the renderer again.\n"
            + json.dumps(evidence, ensure_ascii=False, indent=2)
            + "\n</tool_generated_diagram_visual_evidence>"
        )
    pages = [
        {
            "book_code": str(item.get("book_code") or ""),
            "page_number": int(item.get("page_number") or 0),
            "source_path": str(item.get("source_path") or ""),
            "sha256": str(item.get("sha256") or ""),
        }
        for item in attachments
    ]
    return (
        "<tool_generated_textbook_visual_evidence>\n"
        "以下图片是工具刚从选定教材的精确页面本地渲染出的视觉证据。"
        "请直接查看公式、上下标、特殊符号和图形；不得只依赖 OCR，也不得推断未附加的页面。\n"
        + json.dumps(pages, ensure_ascii=False, indent=2)
        + "\n</tool_generated_textbook_visual_evidence>"
    )


def _explicit_requested_tool(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> str:
    """Return an available tool that the latest user explicitly asked to call."""

    latest = next(
        (str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"),
        "",
    )
    if not latest:
        return ""
    for tool in sorted(tools, key=lambda item: len(str(item.get("name") or "")), reverse=True):
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        escaped = re.escape(name)
        if re.search(rf"(?:不要|禁止|无需|不必|别).{{0,40}}{escaped}", latest, re.I | re.S):
            continue
        if re.search(
            rf"(?:明确|必须|务必|直接|请)?\s*(?:调用|使用).{{0,80}}{escaped}\b|"
            rf"\b{escaped}\b.{{0,20}}工具",
            latest,
            re.I | re.S,
        ):
            return name
    return ""


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class ToolTrace:
    name: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderResult:
    answer: str
    tool_traces: list[ToolTrace] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    route: str = ""
    reasoning_effort: str = ""
    reasoning_mode: str = ""
    text_verbosity: str = ""
    response_model: str = ""
    response_id: str = ""
    response_status: str = ""
    reasoning_context: str = ""
    fallback_reason: str = ""


def _join_endpoint(base_url: str, suffix: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    normalized_suffix = "/" + suffix.strip("/")
    if base.lower().endswith(normalized_suffix.lower()):
        return base
    return base + normalized_suffix


def _concrete_reasoning_effort(value: str) -> str:
    """Resolve the UI-only adaptive preset before sending provider payloads."""

    normalized = str(value or "auto").casefold()
    return "medium" if normalized == "adaptive" else normalized


def _tool_budget_fallback_answer(traces: list[ToolTrace]) -> str:
    succeeded = sum(1 for trace in traces if trace.ok)
    failed = len(traces) - succeeded
    details = "、".join(
        f"{trace.name}（{'成功' if trace.ok else '失败'}）" for trace in traces[-6:]
    ) or "没有工具实际执行"
    return (
        "本次复杂任务已停止继续调用工具，并保留此前的实际结果。"
        f"共执行 {len(traces)} 次工具调用，其中成功 {succeeded} 次、失败 {failed} 次；"
        f"最近的执行记录为：{details}。"
        "尚未由工具明确返回成功的写入或编译不能视为已经完成。"
    )


def _auth_headers(profile: ProviderProfile, api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "MathProblemBank-AIAgent/1.0"}
    if profile.auth_mode == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif profile.auth_mode == "api-key" and api_key:
        headers["api-key"] = api_key
    return headers


def _redact(text: str, secrets: list[str]) -> str:
    clean = str(text or "")
    for secret in secrets:
        if secret:
            clean = clean.replace(secret, "***")
    return clean


def _register_cancel(progress: ProgressCallback | None, callback: Callable[[], None]) -> Callable[[], None]:
    register = getattr(progress, "register_cancel", None)
    if callable(register):
        return register(callback)
    return lambda: None


def _raise_if_cancelled(progress: ProgressCallback | None) -> None:
    checker = getattr(progress, "is_cancelled", None)
    if callable(checker) and checker():
        raise RuntimeError("请求已取消。")


def _json_request(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
    secrets: list[str] | None = None,
    progress: ProgressCallback | None = None,
    max_retries: int = 1,
) -> dict[str, Any]:
    if payload.get("stream") is True:
        raise ProviderError("题库管理中心禁止模型 API 使用流式传输。")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
    raw = ""
    retry_limit = max(0, int(max_retries))

    def retry_delay(error: Exception, retry_index: int) -> float:
        headers = getattr(error, "headers", None)
        if headers is not None:
            retry_after_ms = str(headers.get("retry-after-ms", "") or "").strip()
            if retry_after_ms:
                try:
                    seconds = float(retry_after_ms) / 1000.0
                    if 0.0 <= seconds <= 60.0:
                        return seconds
                except ValueError:
                    pass
            retry_after = str(headers.get("retry-after", "") or "").strip()
            if retry_after:
                try:
                    seconds = float(retry_after)
                except ValueError:
                    try:
                        parsed = email.utils.parsedate_to_datetime(retry_after)
                        seconds = parsed.timestamp() - time.time()
                    except (TypeError, ValueError, OverflowError):
                        seconds = -1.0
                if 0.0 <= seconds <= 60.0:
                    return seconds
        base = min(0.5 * (2**retry_index), 8.0)
        return base * (1.0 - 0.25 * random.random())

    def wait_before_retry(error: Exception, attempt: int) -> None:
        delay = retry_delay(error, attempt)
        if progress:
            try:
                progress(
                    "兼容 API 请求发生可重试的连接/网关错误；"
                    f"{delay:.1f} 秒后进行第 {attempt + 2}/{retry_limit + 1} 次连接尝试。"
                )
            except (OSError, UnicodeError):
                pass
        _raise_if_cancelled(progress)
        time.sleep(delay)
        _raise_if_cancelled(progress)

    for attempt in range(retry_limit + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                unregister = _register_cancel(progress, getattr(response, "close", lambda: None))
                try:
                    raw = response.read().decode("utf-8", errors="replace")
                finally:
                    unregister()
            _raise_if_cancelled(progress)
            break
        except urllib.error.HTTPError as error:
            retryable_status = error.code in {408, 409, 429} or error.code >= 500
            if attempt < retry_limit and retryable_status:
                wait_before_retry(error, attempt)
                continue
            raw = error.read().decode("utf-8", errors="replace")
            message = raw
            try:
                parsed = json.loads(raw)
                detail = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
                if isinstance(detail, dict):
                    message = str(detail.get("message") or detail.get("detail") or detail)
                else:
                    message = str(detail)
            except json.JSONDecodeError:
                pass
            raise ProviderError(
                _redact(f"API 请求失败（HTTP {error.code}）：{message[:1800]}", secrets or []),
                status_code=error.code,
            ) from None
        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            http.client.BadStatusLine,
            ConnectionAbortedError,
            ConnectionResetError,
            TimeoutError,
            ssl.SSLError,
        ) as error:
            if attempt < retry_limit:
                wait_before_retry(error, attempt)
                continue
            reason = getattr(error, "reason", error)
            if isinstance(error, TimeoutError):
                message = (
                    f"模型 API 请求超过 {timeout} 秒时限仍未完成。"
                    if retry_limit == 0
                    else f"模型 API 请求超过 {timeout} 秒时限，自动重试后仍未成功。"
                )
                raise ProviderError(message) from None
            message = (
                f"模型 API 连接中断：{reason}"
                if retry_limit == 0
                else f"模型 API 连接中断，自动重试后仍未成功：{reason}"
            )
            raise ProviderError(_redact(message, secrets or [])) from None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ProviderError(f"模型 API 返回了无法解析的 JSON：{raw[:1000]}") from None
    if not isinstance(parsed, dict):
        raise ProviderError("模型 API 返回的顶层数据不是 JSON 对象。")
    return parsed


def _json_get_request(
    url: str,
    headers: dict[str, str],
    timeout: int,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(url=url, headers=headers, method="GET")
    raw = ""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                raw = response.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as error:
            if attempt == 0 and error.code in {502, 503, 504}:
                time.sleep(1.2)
                continue
            raw = error.read().decode("utf-8", errors="replace")
            message = raw
            try:
                parsed_error = json.loads(raw)
                detail = parsed_error.get("error", parsed_error) if isinstance(parsed_error, dict) else parsed_error
                message = str(detail.get("message") or detail) if isinstance(detail, dict) else str(detail)
            except json.JSONDecodeError:
                pass
            raise ProviderError(
                _redact(f"获取模型列表失败（HTTP {error.code}）：{message[:1800]}", secrets or [])
            ) from None
        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            http.client.BadStatusLine,
            ConnectionAbortedError,
            ConnectionResetError,
            TimeoutError,
            ssl.SSLError,
        ) as error:
            if attempt == 0:
                time.sleep(1.2)
                continue
            reason = getattr(error, "reason", error)
            if isinstance(error, TimeoutError):
                raise ProviderError("获取模型列表超时，自动重试后仍未成功。") from None
            raise ProviderError(_redact(f"模型列表连接中断，自动重试后仍未成功：{reason}", secrets or [])) from None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ProviderError(f"模型列表 API 返回了无法解析的 JSON：{raw[:1000]}") from None
    if not isinstance(parsed, dict):
        raise ProviderError("模型列表 API 返回的顶层数据不是 JSON 对象。")
    return parsed


def list_available_models(profile: ProviderProfile, api_key: str) -> list[str]:
    if profile.provider_kind not in {"openai_compatible", "openai_responses"}:
        raise ProviderError("当前 API 协议没有统一的模型列表接口，请手动填写模型名称。")
    data = _json_get_request(
        _join_endpoint(profile.base_url, "models"),
        _auth_headers(profile, api_key),
        profile.timeout_seconds,
        [api_key],
    )
    rows = data.get("data")
    if not isinstance(rows, list):
        raise ProviderError("模型列表 API 返回的数据缺少 data 数组。")
    model_ids = sorted(
        {
            str(item.get("id") or "").strip()
            for item in rows
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        },
        key=str.casefold,
    )
    if not model_ids:
        raise ProviderError("模型列表为空；请检查令牌分组和模型限制。")
    return model_ids


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError as error:
        raise ProviderError(f"模型生成了无效的工具参数 JSON：{error}") from None
    if not isinstance(parsed, dict):
        raise ProviderError("模型生成的工具参数必须是 JSON 对象。")
    return parsed


def _trace_summary(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict) and "verified" in data:
        label = "Lean 内核已接受" if data.get("verified") else "Lean 内核未接受"
        relative = str(data.get("relative_path") or "")
        duration = int(data.get("duration_ms") or 0)
        return " · ".join(item for item in (label, relative, f"{duration} ms" if duration else "") if item)
    if not result.get("ok"):
        return str(result.get("error") or "工具执行失败")[:500]
    if isinstance(data, dict):
        if "exit_code" in data and data.get("command"):
            return (
                f"退出码 {data.get('exit_code')} · {str(data.get('command') or '')[:240]}"
                + (f" · {int(data.get('duration_ms') or 0)} ms" if data.get("duration_ms") else "")
            )
        if data.get("transaction_verified") and data.get("changed_files"):
            return f"事务回读核验通过 · {len(data.get('changed_files') or [])} 个路径"
        if data.get("verification") in {
            "public_arxiv_pdf_downloaded_and_parsed",
            "crossref_metadata_retrieved",
        }:
            paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
            return f"论文已读取 · {paper.get('title') or paper.get('paper_id') or 'paper'}"
        if data.get("engine") or data.get("status"):
            label = str(data.get("engine") or "dual")
            operation = str(data.get("operation") or data.get("status") or "compute")
            duration = int(data.get("duration_ms") or 0)
            return f"{label} · {operation}" + (f" · {duration} ms" if duration else "")
        count_keys = [key for key in data if key.endswith("_count")]
        counts = ", ".join(f"{key}={data[key]}" for key in count_keys[:4])
        return counts or "工具已返回数据"
    return "工具已返回数据"


def _trace_evidence(name: str, result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    if name == "lean_check" and isinstance(data, dict):
        return {
            key: data[key]
            for key in (
                "verified", "verification", "exit_code", "path", "relative_path",
                "project_root", "duration_ms", "diagnostics", "source_validation",
                "contains_sorry", "contains_admit", "contains_axiom",
            )
            if key in data
        }
    if not result.get("ok") and name != "run_workspace_command":
        return {}
    if not isinstance(data, dict):
        return {}
    if name in {"symbolic_math", "numerical_math", "verify_formula", "find_counterexample", "mathematica_compute", "mathematica_plot", "dual_verify_math", "plot_math_function"}:
        return {"compute": data}
    allowed = (
        "changed",
        "relative_path",
        "backup_directory",
        "project_pdf_path",
        "project_pdf_duration_seconds",
        "validation",
        "project_code",
        "changed_files",
        "changed_file_count",
        "transaction_verified",
        "tex_path",
        "pdf_path",
        "pdf_size_bytes",
        "visual_validation",
        "math_validation",
        "path",
        "url",
        "subject_name",
        "problem_code",
        "title",
        "page_start",
        "page_end",
        "project_ref",
        "verified",
        "code_changed",
        "before_hashes",
        "after_hashes",
        "diff",
        "file_operations",
        "command",
        "working_directory",
        "exit_code",
        "stdout",
        "stderr",
        "duration_ms",
        "log_path",
        "database_path",
        "backup_path",
        "before_sha256",
        "after_sha256",
        "integrity_check",
        "foreign_key_violations",
        "schema_added",
        "schema_removed",
    )
    evidence = {key: data[key] for key in allowed if key in data and data[key] not in (None, "")}
    for key in ("content", "text", "excerpt", "problem_statement", "solution_excerpt"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            evidence["source_excerpt"] = value.strip()[:1200]
            break
    if name == "get_problem_evidence_batch" and isinstance(data.get("problems"), list):
        evidence["problem_refs"] = [
            str(item.get("problem_code") or "")
            for item in data["problems"]
            if isinstance(item, dict) and item.get("problem_code")
        ][:8]
        evidence["source_excerpt"] = "\n\n".join(
            str(item.get("problem_statement") or item.get("summary") or "")[:400]
            for item in data["problems"][:4]
            if isinstance(item, dict)
        )[:1200]
    if name in {"search_problems", "resolve_problem_reference"} and isinstance(data.get("results"), list):
        matched_problems = [item for item in data["results"] if isinstance(item, dict) and item.get("problem_code")]
        evidence["problem_refs"] = [str(item["problem_code"]) for item in matched_problems[:12]]
        if matched_problems and not evidence.get("subject_name"):
            evidence["subject_name"] = str(matched_problems[0].get("subject_name") or "")
        evidence["source_excerpt"] = "\n\n".join(
            str(item.get("summary_tex") or item.get("title") or "")[:400]
            for item in matched_problems[:4]
        )[:1200]
    if name in {"semantic_search", "web_search"} and isinstance(data.get("results"), list):
        compact_sources: list[dict[str, Any]] = []
        for item in data["results"][:20]:
            if not isinstance(item, dict):
                continue
            compact_sources.append(
                {
                    key: item.get(key)
                    for key in (
                        "kind",
                        "title",
                        "path",
                        "url",
                        "subject_name",
                        "problem_ref",
                        "page_start",
                        "page_end",
                        "snippet",
                    )
                    if item.get(key) not in (None, "", 0)
                }
            )
        if compact_sources:
            evidence["sources"] = compact_sources
    if name == "discover_public_math_resources" and isinstance(data.get("verified_resources"), list):
        evidence["sources"] = [
            {
                key: item.get(key)
                for key in ("title", "url", "domain", "resource_type", "page_count", "excerpt")
                if item.get(key) not in (None, "", 0)
            }
            for item in data["verified_resources"][:6]
            if isinstance(item, dict) and item.get("url") and item.get("verified_open")
        ]
    if name == "search_math_papers" and isinstance(data.get("papers"), list):
        evidence["sources"] = [
            {
                key: item.get(key)
                for key in (
                    "paper_id", "title", "authors", "published", "arxiv_id", "doi",
                    "venue", "abstract_url", "landing_url", "pdf_url", "full_text_status",
                )
                if item.get(key) not in (None, "", [], {})
            }
            for item in data["papers"][:10]
            if isinstance(item, dict)
        ]
    if name == "read_math_paper":
        paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
        urls = [str(url) for url in data.get("source_urls") or [] if str(url).startswith(("http://", "https://"))]
        evidence.update(
            {
                "title": str(paper.get("title") or ""),
                "paper_id": str(paper.get("paper_id") or ""),
                "arxiv_id": str(paper.get("arxiv_id") or ""),
                "doi": str(paper.get("doi") or ""),
                "pdf_path": str(data.get("pdf_path") or ""),
                "page_count": data.get("page_count"),
                "page_start": data.get("page_start"),
                "page_end": data.get("page_end"),
                "verification": str(data.get("verification") or ""),
                "source_urls": urls,
                "sources": [{"title": str(paper.get("title") or url), "url": url} for url in urls],
            }
        )
        if isinstance(data.get("content"), str) and data["content"].strip():
            evidence["source_excerpt"] = data["content"].strip()[:3000]
    return evidence


def _tool_call_signature(name: str, arguments: dict[str, Any]) -> str:
    return name + "\n" + json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tool_loop_should_stop(traces: list[ToolTrace]) -> bool:
    if any(trace.name in {"discover_public_math_resources", "read_math_paper"} and trace.ok for trace in traces):
        return True
    if traces and traces[-1].summary.startswith("已阻止重复工具调用"):
        return True
    return len(traces) >= 3 and all(not trace.ok for trace in traces[-3:])


def _tool_loop_stop_message(traces: list[ToolTrace]) -> str:
    if any(trace.name == "read_math_paper" and trace.ok for trace in traces):
        return "相关论文的摘要或公开正文已经读取，正在整理带标识与页码的回答..."
    if any(trace.name == "discover_public_math_resources" and trace.ok for trace in traces):
        return "公开数学资料已经完成检索和打开核验，正在整理资料清单..."
    return "检测到重复调用或连续失败，已停止新工具并保留现有结果..."


def _execute_and_trace(
    name: str,
    arguments: dict[str, Any],
    execute_tool: ToolCallback,
    traces: list[ToolTrace],
    progress: ProgressCallback,
) -> dict[str, Any]:
    signature = _tool_call_signature(name, arguments)
    identical = [
        trace
        for trace in traces
        if _tool_call_signature(trace.name, trace.arguments) == signature
    ]
    preview_successes = [trace for trace in traces if trace.name == name and trace.ok]
    preview_passed = any(
        bool((trace.evidence.get("visual_validation") or {}).get("passed"))
        for trace in preview_successes
        if isinstance(trace.evidence, dict)
    )
    duplicate_mutation = name in MUTATING_TOOLS and any(trace.ok for trace in identical)
    duplicate_preview = name == "render_math_figure_preview" and (
        preview_passed or len(preview_successes) >= 2
    )
    excessive_repeat = len(identical) >= 2
    if duplicate_mutation or duplicate_preview or excessive_repeat:
        reason = (
            "已阻止重复工具调用：同一写入已经成功执行，不能再次提交。"
            if duplicate_mutation
            else "已阻止重复工具调用：临时图形已经通过检查，或已完成一次视觉修正，应报告现有结果。"
            if duplicate_preview
            else "已阻止重复工具调用：相同名称和参数已经执行两次。"
        )
        traces.append(ToolTrace(name=name, arguments=arguments, ok=False, summary=reason))
        return {"ok": False, "error": reason}
    if name == "render_math_figure_preview":
        attempt = len(preview_successes) + 1
        progress("正在编译并检查数学图形…" if attempt == 1 else "正在按视觉检查结果进行唯一一次修正…")
    else:
        progress(f"正在调用项目工具：{name}")
    result = execute_tool(name, arguments)
    traces.append(
        ToolTrace(
            name=name,
            arguments=arguments,
            ok=bool(result.get("ok")),
            summary=_trace_summary(result),
            evidence=_trace_evidence(name, result),
        )
    )
    return result


class BaseProvider:
    def __init__(self, profile: ProviderProfile, api_key: str) -> None:
        self.profile = profile
        self.api_key = api_key

    def run_turn(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        execute_tool: ToolCallback,
        progress: ProgressCallback,
    ) -> ProviderResult:
        raise NotImplementedError


class OpenAIResponsesProvider(BaseProvider):
    def run_turn(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        execute_tool: ToolCallback,
        progress: ProgressCallback,
    ) -> ProviderResult:
        endpoint = _join_endpoint(self.profile.base_url, "responses")
        headers = _auth_headers(self.profile, self.api_key)
        input_items: list[dict[str, Any]] = [
            _responses_message(item)
            for item in messages
            if item.get("role") in {"user", "assistant"}
        ]
        response_tools = [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
                "strict": False,
            }
            for tool in tools
        ]
        explicitly_requested_tool = _explicit_requested_tool(messages, tools)
        traces: list[ToolTrace] = []
        last_usage: dict[str, Any] = {}
        last_response_model = ""
        last_response_id = ""
        last_response_status = ""
        last_reasoning_context = ""
        tool_rounds = 0
        for round_index in range(self.profile.max_tool_rounds + 1):
            progress("正在请求模型..." if round_index == 0 else "模型正在分析工具结果...")
            payload: dict[str, Any] = {
                "model": self.profile.model,
                "instructions": system_prompt
                + (TOOL_BUDGET_FINALIZATION_PROMPT if tool_rounds >= self.profile.max_tool_rounds else ""),
                "input": input_items,
                "max_output_tokens": self.profile.max_output_tokens,
                "store": False,
                "stream": False,
            }
            supports_gpt_56_controls = self.profile.model.casefold().startswith("gpt-5.6")
            reasoning: dict[str, Any] = {}
            if supports_gpt_56_controls:
                reasoning["mode"] = "standard"
            concrete_effort = _concrete_reasoning_effort(
                self.profile.reasoning_effort
            )
            if concrete_effort != "auto":
                reasoning["effort"] = concrete_effort
            if reasoning:
                payload["reasoning"] = reasoning
            if supports_gpt_56_controls and self.profile.text_verbosity != "auto":
                payload["text"] = {"verbosity": self.profile.text_verbosity}
            if response_tools:
                payload["tools"] = response_tools
                payload["tool_choice"] = (
                    {"type": "function", "name": explicitly_requested_tool}
                    if explicitly_requested_tool and round_index == 0
                    else "auto"
                )
                payload["include"] = ["reasoning.encrypted_content"]
            data = _json_request(
                endpoint,
                payload,
                headers,
                self.profile.timeout_seconds,
                [self.api_key],
                progress,
                max_retries=int(
                    getattr(self.profile, "transport_retries", 0) or 0
                ),
            )
            last_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            last_response_model = str(data.get("model") or "")
            last_response_id = str(data.get("id") or "")
            last_response_status = str(data.get("status") or "")
            response_reasoning = data.get("reasoning") if isinstance(data.get("reasoning"), dict) else {}
            last_reasoning_context = str(response_reasoning.get("context") or "")
            output = data.get("output") if isinstance(data.get("output"), list) else []
            function_calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
            if function_calls:
                if tool_rounds >= self.profile.max_tool_rounds:
                    return ProviderResult(
                        answer=_tool_budget_fallback_answer(traces),
                        tool_traces=traces,
                        usage=last_usage,
                        route="responses",
                        reasoning_effort=self.profile.reasoning_effort,
                        reasoning_mode="standard" if supports_gpt_56_controls else "",
                        text_verbosity=self.profile.text_verbosity,
                        response_model=last_response_model,
                        response_id=last_response_id,
                        response_status=last_response_status,
                        reasoning_context=last_reasoning_context,
                    )
                input_items.extend(item for item in output if isinstance(item, dict))
                round_visual_attachments: list[dict[str, Any]] = []
                for call in function_calls:
                    name = str(call.get("name") or "")
                    arguments = _parse_arguments(call.get("arguments"))
                    result = _execute_and_trace(name, arguments, execute_tool, traces, progress)
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": str(call.get("call_id") or call.get("id") or ""),
                            "output": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    visual_attachments = _tool_visual_evidence(result)
                    if visual_attachments:
                        round_visual_attachments.extend(visual_attachments)
                if round_visual_attachments:
                    round_visual_attachments = round_visual_attachments[:4]
                    input_items.append(
                        _responses_message(
                            {
                                "role": "user",
                                "content": _tool_visual_prompt(round_visual_attachments),
                                "attachments": round_visual_attachments,
                            }
                        )
                    )
                tool_rounds += 1
                if _tool_loop_should_stop(traces):
                    tool_rounds = self.profile.max_tool_rounds
                    response_tools = []
                    progress(_tool_loop_stop_message(traces))
                elif tool_rounds >= self.profile.max_tool_rounds:
                    response_tools = []
                    progress("工具预算已用完，正在保留现有结果并生成最终说明...")
                continue
            answer = str(data.get("output_text") or "").strip()
            if not answer:
                text_parts: list[str] = []
                for item in output:
                    if not isinstance(item, dict) or item.get("type") != "message":
                        continue
                    for block in item.get("content") or []:
                        if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                            text_parts.append(str(block.get("text") or ""))
                answer = "\n".join(part for part in text_parts if part.strip()).strip()
            if not answer:
                raise ProviderError("模型没有返回可显示的文本回答。")
            return ProviderResult(
                answer=answer,
                tool_traces=traces,
                usage=last_usage,
                route="responses",
                reasoning_effort=self.profile.reasoning_effort,
                reasoning_mode="standard" if supports_gpt_56_controls else "",
                text_verbosity=self.profile.text_verbosity,
                response_model=last_response_model,
                response_id=last_response_id,
                response_status=last_response_status,
                reasoning_context=last_reasoning_context,
            )
        raise ProviderError("模型工具循环未能结束。")


class OpenAICompatibleProvider(BaseProvider):
    def run_turn(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        execute_tool: ToolCallback,
        progress: ProgressCallback,
    ) -> ProviderResult:
        endpoint = _join_endpoint(self.profile.base_url, "chat/completions")
        headers = _auth_headers(self.profile, self.api_key)
        chat_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        chat_messages.extend(
            _chat_message(item)
            for item in messages
            if item.get("role") in {"user", "assistant"}
        )
        chat_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in tools
        ]
        explicitly_requested_tool = _explicit_requested_tool(messages, tools)
        traces: list[ToolTrace] = []
        last_usage: dict[str, Any] = {}
        tool_rounds = 0
        for round_index in range(self.profile.max_tool_rounds + 1):
            progress("正在请求模型..." if round_index == 0 else "模型正在分析工具结果...")
            payload: dict[str, Any] = {
                "model": self.profile.model,
                "messages": chat_messages,
                "max_tokens": self.profile.max_output_tokens,
                "stream": False,
            }
            effective_effort = ""
            if self.profile.model.casefold().startswith("gpt-5.6"):
                effective_effort = (
                    "none"
                    if chat_tools
                    else _concrete_reasoning_effort(self.profile.reasoning_effort)
                )
                if effective_effort != "auto":
                    payload["reasoning_effort"] = effective_effort
            if chat_tools:
                payload["tools"] = chat_tools
                payload["tool_choice"] = (
                    {"type": "function", "function": {"name": explicitly_requested_tool}}
                    if explicitly_requested_tool and round_index == 0
                    else "auto"
                )
            data = _json_request(
                endpoint, payload, headers, self.profile.timeout_seconds, [self.api_key], progress
            )
            last_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ProviderError("兼容 API 返回的数据缺少 choices。")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise ProviderError("兼容 API 返回的数据缺少 assistant message。")
            tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
            if tool_calls:
                if tool_rounds >= self.profile.max_tool_rounds:
                    return ProviderResult(
                        answer=_tool_budget_fallback_answer(traces),
                        tool_traces=traces,
                        usage=last_usage,
                        route="chat_completions",
                        reasoning_effort=effective_effort,
                        response_model=str(data.get("model") or ""),
                        response_id=str(data.get("id") or ""),
                        response_status=str(choices[0].get("finish_reason") or ""),
                    )
                chat_messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                round_visual_attachments: list[dict[str, Any]] = []
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = str(function.get("name") or "")
                    arguments = _parse_arguments(function.get("arguments"))
                    result = _execute_and_trace(name, arguments, execute_tool, traces, progress)
                    chat_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id") or ""),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    visual_attachments = _tool_visual_evidence(result)
                    if visual_attachments:
                        round_visual_attachments.extend(visual_attachments)
                if round_visual_attachments:
                    round_visual_attachments = round_visual_attachments[:4]
                    chat_messages.append(
                        _chat_message(
                            {
                                "role": "user",
                                "content": _tool_visual_prompt(round_visual_attachments),
                                "attachments": round_visual_attachments,
                            }
                        )
                    )
                tool_rounds += 1
                if _tool_loop_should_stop(traces):
                    tool_rounds = self.profile.max_tool_rounds
                    chat_tools = []
                    chat_messages.append({"role": "system", "content": TOOL_BUDGET_FINALIZATION_PROMPT.strip()})
                    progress(_tool_loop_stop_message(traces))
                elif tool_rounds >= self.profile.max_tool_rounds:
                    chat_tools = []
                    chat_messages.append({"role": "system", "content": TOOL_BUDGET_FINALIZATION_PROMPT.strip()})
                    progress("工具预算已用完，正在保留现有结果并生成最终说明...")
                continue
            content = message.get("content")
            if isinstance(content, list):
                answer = "\n".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
                ).strip()
            else:
                answer = str(content or "").strip()
            if not answer:
                raise ProviderError("模型没有返回可显示的文本回答。")
            return ProviderResult(
                answer=answer,
                tool_traces=traces,
                usage=last_usage,
                route="chat_completions",
                reasoning_effort=effective_effort,
                response_model=str(data.get("model") or ""),
                response_id=str(data.get("id") or ""),
                response_status=str(choices[0].get("finish_reason") or ""),
            )
        raise ProviderError("模型工具循环未能结束。")


class QualityFirstOpenAIProvider(BaseProvider):
    """Prefer Responses, with a narrow compatibility fallback for relays.

    Fallback is allowed only when the Responses endpoint itself rejects the
    request and no local tool has run. This prevents a compatibility retry from
    repeating a TeX edit or another state-changing tool.
    """

    _ENDPOINT_COMPATIBILITY_STATUS_CODES = {404, 405, 415, 501}
    def run_turn(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        execute_tool: ToolCallback,
        progress: ProgressCallback,
    ) -> ProviderResult:
        tool_started = False

        def tracked_execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            nonlocal tool_started
            tool_started = True
            return execute_tool(name, arguments)

        effort_label = self.profile.reasoning_effort if self.profile.reasoning_effort != "auto" else "供应商默认"
        progress(f"质量优先：正在使用 Responses API（standard / 推理强度 {effort_label}）...")
        responses_profile = replace(self.profile, provider_kind="openai_responses")
        try:
            return OpenAIResponsesProvider(responses_profile, self.api_key).run_turn(
                messages,
                system_prompt,
                tools,
                tracked_execute_tool,
                progress,
            )
        except ProviderError as error:
            if tool_started or error.status_code not in self._ENDPOINT_COMPATIBILITY_STATUS_CODES:
                raise
            progress("当前中转站未接受 Responses 请求，正在兼容降级到 Chat Completions...")
            chat_profile = replace(self.profile, provider_kind="openai_compatible")
            result = OpenAICompatibleProvider(chat_profile, self.api_key).run_turn(
                messages,
                system_prompt,
                tools,
                execute_tool,
                progress,
            )
            result.route = "chat_completions_fallback"
            result.fallback_reason = str(error)
            return result


class AnthropicProvider(BaseProvider):
    def run_turn(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        execute_tool: ToolCallback,
        progress: ProgressCallback,
    ) -> ProviderResult:
        endpoint = _join_endpoint(self.profile.base_url, "messages")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MathProblemBank-AIAgent/1.0",
            "anthropic-version": "2023-06-01",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        anthropic_messages: list[dict[str, Any]] = [
            _anthropic_message(item)
            for item in messages
            if item.get("role") in {"user", "assistant"}
        ]
        anthropic_tools = [
            {"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
            for tool in tools
        ]
        traces: list[ToolTrace] = []
        last_usage: dict[str, Any] = {}
        tool_rounds = 0
        for round_index in range(self.profile.max_tool_rounds + 1):
            progress("正在请求模型..." if round_index == 0 else "模型正在分析工具结果...")
            payload: dict[str, Any] = {
                "model": self.profile.model,
                "system": system_prompt
                + (TOOL_BUDGET_FINALIZATION_PROMPT if tool_rounds >= self.profile.max_tool_rounds else ""),
                "messages": anthropic_messages,
                "max_tokens": self.profile.max_output_tokens,
                "stream": False,
            }
            if anthropic_tools:
                payload["tools"] = anthropic_tools
                payload["tool_choice"] = {"type": "auto"}
            data = _json_request(
                endpoint, payload, headers, self.profile.timeout_seconds, [self.api_key], progress
            )
            last_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            content = data.get("content") if isinstance(data.get("content"), list) else []
            tool_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
            if tool_blocks:
                if tool_rounds >= self.profile.max_tool_rounds:
                    return ProviderResult(
                        answer=_tool_budget_fallback_answer(traces),
                        tool_traces=traces,
                        usage=last_usage,
                    )
                anthropic_messages.append({"role": "assistant", "content": content})
                result_blocks = []
                for block in tool_blocks:
                    name = str(block.get("name") or "")
                    arguments = _parse_arguments(block.get("input"))
                    result = _execute_and_trace(name, arguments, execute_tool, traces, progress)
                    visual_attachments = _tool_visual_evidence(result)
                    result_content: str | list[dict[str, Any]] = json.dumps(
                        result, ensure_ascii=False
                    )
                    if visual_attachments:
                        result_content = [
                            {"type": "text", "text": result_content},
                            {"type": "text", "text": _tool_visual_prompt(visual_attachments)},
                            *[
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": mime,
                                        "data": data,
                                    },
                                }
                                for mime, data in _message_images(
                                    {"attachments": visual_attachments}
                                )
                            ],
                        ]
                    result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": str(block.get("id") or ""),
                            "content": result_content,
                            "is_error": not bool(result.get("ok")),
                        }
                    )
                anthropic_messages.append({"role": "user", "content": result_blocks})
                tool_rounds += 1
                if _tool_loop_should_stop(traces):
                    tool_rounds = self.profile.max_tool_rounds
                    anthropic_tools = []
                    progress(_tool_loop_stop_message(traces))
                elif tool_rounds >= self.profile.max_tool_rounds:
                    anthropic_tools = []
                    progress("工具预算已用完，正在保留现有结果并生成最终说明...")
                continue
            answer = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if not answer:
                raise ProviderError("模型没有返回可显示的文本回答。")
            return ProviderResult(answer=answer, tool_traces=traces, usage=last_usage)
        raise ProviderError("模型工具循环未能结束。")


def _gemini_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _gemini_schema(item)
            for key, item in value.items()
            if key not in {"additionalProperties", "$schema"}
        }
    if isinstance(value, list):
        return [_gemini_schema(item) for item in value]
    return value


class GeminiProvider(BaseProvider):
    def run_turn(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        execute_tool: ToolCallback,
        progress: ProgressCallback,
    ) -> ProviderResult:
        base = str(self.profile.base_url or "").strip().rstrip("/")
        model_name = urllib.parse.quote(self.profile.model, safe="-_.")
        endpoint = f"{base}/models/{model_name}:generateContent"
        if self.api_key:
            endpoint += "?" + urllib.parse.urlencode({"key": self.api_key})
        headers = {"Content-Type": "application/json", "User-Agent": "MathProblemBank-AIAgent/1.0"}
        contents: list[dict[str, Any]] = [
            {
                "role": "model" if item["role"] == "assistant" else "user",
                "parts": _gemini_parts(item),
            }
            for item in messages
            if item.get("role") in {"user", "assistant"}
        ]
        gemini_tools = [
            {
                "functionDeclarations": [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": _gemini_schema(tool["parameters"]),
                    }
                    for tool in tools
                ]
            }
        ] if tools else []
        traces: list[ToolTrace] = []
        last_usage: dict[str, Any] = {}
        tool_rounds = 0
        for round_index in range(self.profile.max_tool_rounds + 1):
            progress("正在请求模型..." if round_index == 0 else "模型正在分析工具结果...")
            payload: dict[str, Any] = {
                "systemInstruction": {
                    "parts": [
                        {
                            "text": system_prompt
                            + (
                                TOOL_BUDGET_FINALIZATION_PROMPT
                                if tool_rounds >= self.profile.max_tool_rounds
                                else ""
                            )
                        }
                    ]
                },
                "contents": contents,
                "generationConfig": {"maxOutputTokens": self.profile.max_output_tokens},
            }
            if gemini_tools:
                payload["tools"] = gemini_tools
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
            data = _json_request(
                endpoint, payload, headers, self.profile.timeout_seconds, [self.api_key], progress
            )
            last_usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
            candidates = data.get("candidates")
            if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
                prompt_feedback = data.get("promptFeedback")
                raise ProviderError(f"Gemini 没有返回候选回答：{prompt_feedback or data}")
            content = candidates[0].get("content")
            if not isinstance(content, dict):
                raise ProviderError("Gemini 返回的数据缺少 content。")
            parts = content.get("parts") if isinstance(content.get("parts"), list) else []
            function_parts = [part for part in parts if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)]
            if function_parts:
                if tool_rounds >= self.profile.max_tool_rounds:
                    return ProviderResult(
                        answer=_tool_budget_fallback_answer(traces),
                        tool_traces=traces,
                        usage=last_usage,
                    )
                contents.append({"role": "model", "parts": parts})
                response_parts = []
                for part in function_parts:
                    call = part["functionCall"]
                    name = str(call.get("name") or "")
                    arguments = _parse_arguments(call.get("args"))
                    result = _execute_and_trace(name, arguments, execute_tool, traces, progress)
                    response_parts.append(
                        {
                            "functionResponse": {
                                "name": name,
                                "response": result,
                            }
                        }
                    )
                    visual_attachments = _tool_visual_evidence(result)
                    if visual_attachments:
                        response_parts.append(
                            {"text": _tool_visual_prompt(visual_attachments)}
                        )
                        response_parts.extend(
                            {
                                "inlineData": {"mimeType": mime, "data": data}
                            }
                            for mime, data in _message_images(
                                {"attachments": visual_attachments}
                            )
                        )
                contents.append({"role": "user", "parts": response_parts})
                tool_rounds += 1
                if _tool_loop_should_stop(traces):
                    tool_rounds = self.profile.max_tool_rounds
                    gemini_tools = []
                    progress(_tool_loop_stop_message(traces))
                elif tool_rounds >= self.profile.max_tool_rounds:
                    gemini_tools = []
                    progress("工具预算已用完，正在保留现有结果并生成最终说明...")
                continue
            answer = "\n".join(
                str(part.get("text") or "")
                for part in parts
                if isinstance(part, dict) and "text" in part
            ).strip()
            if not answer:
                raise ProviderError("模型没有返回可显示的文本回答。")
            return ProviderResult(answer=answer, tool_traces=traces, usage=last_usage)
        raise ProviderError("模型工具循环未能结束。")


def create_provider(profile: ProviderProfile, api_key: str) -> BaseProvider:
    if (
        profile.routing_strategy == "quality_first"
        and profile.provider_kind in {"openai_responses", "openai_compatible"}
    ):
        return QualityFirstOpenAIProvider(profile, api_key)
    if profile.provider_kind == "openai_responses":
        return OpenAIResponsesProvider(profile, api_key)
    if profile.provider_kind == "openai_compatible":
        return OpenAICompatibleProvider(profile, api_key)
    if profile.provider_kind == "anthropic":
        return AnthropicProvider(profile, api_key)
    if profile.provider_kind == "gemini":
        return GeminiProvider(profile, api_key)
    raise ValueError(f"不支持的 API 协议：{profile.provider_kind}")
