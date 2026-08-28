from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shared.scripts.application_paths import APP_PATHS

DEFAULT_RUNTIME_ROOT = APP_PATHS.runtime_root / "mineru-rag"
PARSER_SCHEMA_VERSION = 2
REFERENCE_BLOCK_PAGES = 32
CHECKPOINT_MANIFEST_NAME = "MPB_MINERU_CHECKPOINT.json"
REFERENCE_CHUNK_CHARACTERS = 6000
_ANSI_CONTROL_SEQUENCE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _runtime_root() -> Path:
    configured = str(os.environ.get("MPB_MINERU_RUNTIME_ROOT") or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_RUNTIME_ROOT


def _mineru_executable() -> Path | None:
    configured = str(os.environ.get("MPB_MINERU_EXECUTABLE") or "").strip()
    candidates = [
        Path(configured).expanduser() if configured else Path(),
        _runtime_root() / ".venv" / "Scripts" / "mineru.exe",
    ]
    discovered = shutil.which("mineru")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if str(candidate) not in {"", "."} and candidate.is_file():
            return candidate.resolve()
    return None


def _command_version(executable: Path) -> str:
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = " ".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if str(part or "").strip()
    )
    return output[:200] or "unknown"


def reference_pipeline_status() -> dict[str, Any]:
    executable = _mineru_executable()
    python_path = _runtime_root() / ".venv" / "Scripts" / "python.exe"
    raganything_available = False
    mineru_version = ""
    accelerator = {
        "torch_version": "",
        "cuda_build": "",
        "cuda_available": False,
        "device_name": "",
        "device_memory_bytes": 0,
    }
    if python_path.is_file():
        try:
            completed = subprocess.run(
                [
                    str(python_path),
                    "-c",
                    (
                        "import importlib.metadata,importlib.util,json;"
                        "v=importlib.metadata.version('torch');"
                        "r={'raganything_available':importlib.util.find_spec('raganything') is not None,"
                        "'torch_version':v,'mineru_version':importlib.metadata.version('mineru')};"
                        "print(json.dumps(r))"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=(0x08000000 if os.name == "nt" else 0),
            )
            payload = json.loads(completed.stdout.strip()) if completed.returncode == 0 else {}
            raganything_available = bool(payload.get("raganything_available"))
            accelerator["torch_version"] = str(payload.get("torch_version") or "")
            mineru_version = str(payload.get("mineru_version") or "")
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            pass
    torch_cuda = re.search(r"\+cu(\d+)", str(accelerator["torch_version"]))
    if torch_cuda:
        digits = torch_cuda.group(1)
        accelerator["cuda_build"] = (
            f"{digits[:-1]}.{digits[-1]}" if len(digits) >= 2 else digits
        )
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                completed = subprocess.run(
                    [
                        nvidia_smi,
                        "--query-gpu=name,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    creationflags=(0x08000000 if os.name == "nt" else 0),
                )
                first_line = completed.stdout.strip().splitlines()[0]
                device_name, memory_mib = [
                    value.strip() for value in first_line.rsplit(",", 1)
                ]
                accelerator["device_name"] = device_name
                accelerator["device_memory_bytes"] = int(memory_mib) * 1024 * 1024
                accelerator["cuda_available"] = completed.returncode == 0
            except (IndexError, OSError, ValueError, subprocess.SubprocessError):
                pass
    return {
        "available": executable is not None,
        "mineru_executable": str(executable or ""),
        "mineru_version": (
            f"mineru, version {mineru_version}"
            if mineru_version
            else (_command_version(executable) if executable else "")
        ),
        "runtime_root": str(_runtime_root()),
        "raganything_available": raganything_available,
        **accelerator,
        "invocation_mode": "application_managed_cli",
    }


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            creationflags=0x08000000,
            timeout=30,
            check=False,
        )
    else:
        process.kill()


def _process_environment(timeout_seconds: float) -> dict[str, str]:
    environment = os.environ.copy()
    environment.setdefault("MINERU_MODEL_SOURCE", "huggingface")
    # MinerU's CLI has its own result wait limit.  It used to expire after one
    # hour even when MathProblemBank's outer timeout was six hours.
    environment.setdefault(
        "MINERU_TASK_RESULT_TIMEOUT_SECONDS",
        str(max(21600, int(timeout_seconds))),
    )
    environment.setdefault("MINERU_TASK_RESULT_DOWNLOAD_TIMEOUT_SECONDS", "1800")
    # MinerU 3 starts a short-lived FastAPI service on loopback.  Windows proxy
    # auto-discovery can otherwise send the client's health check to the user's
    # HTTP proxy and turn a healthy local service into a misleading 502.
    for variable in ("NO_PROXY", "no_proxy"):
        existing = str(environment.get(variable) or "").strip()
        entries = [item.strip() for item in existing.split(",") if item.strip()]
        for loopback in ("127.0.0.1", "localhost", "::1"):
            if loopback not in entries:
                entries.append(loopback)
        environment[variable] = ",".join(entries)
    return environment


def _stream_process(
    command: list[str],
    *,
    cwd: Path,
    emit: Callable[[str], None],
    timeout_seconds: float,
) -> None:
    creationflags = 0x08000000 if os.name == "nt" else 0
    started = time.monotonic()
    environment = _process_environment(timeout_seconds)
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        env=environment,
    )
    recent: list[str] = []
    assert process.stdout is not None
    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            for raw_line in process.stdout:
                output_queue.put(raw_line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(
        target=read_output,
        name=f"mineru-output-{process.pid}",
        daemon=True,
    )
    reader.start()
    try:
        while True:
            if time.monotonic() - started > timeout_seconds:
                _terminate_process_tree(process)
                raise TimeoutError(
                    f"MinerU 在 {int(timeout_seconds)} 秒时限内没有完成。"
                )
            try:
                raw_line = output_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if raw_line is None:
                break
            line = raw_line.strip()
            if line:
                line = _ANSI_CONTROL_SEQUENCE.sub("", line)
                recent.append(line)
                recent = recent[-20:]
                message = f"MinerU：{line}"
                try:
                    emit(message)
                except UnicodeError:
                    # Some Windows console callbacks still use GBK.  A model
                    # download progress line must never abort the parse job.
                    emit(message.encode("ascii", errors="replace").decode("ascii"))
        return_code = process.wait()
    except BaseException:
        _terminate_process_tree(process)
        raise
    finally:
        process.stdout.close()
        reader.join(timeout=2)
    if return_code != 0:
        detail = "\n".join(recent[-8:])
        raise RuntimeError(
            f"MinerU 解析失败，退出码 {return_code}。"
            + (f"\n{detail}" if detail else "")
        )


def _pdf_page_count(source_path: Path) -> int:
    try:
        import fitz  # type: ignore

        with fitz.open(source_path) as document:
            page_count = int(document.page_count)
    except Exception as error:
        raise RuntimeError(f"无法读取参考教材 PDF 页数：{source_path}") from error
    if page_count <= 0:
        raise RuntimeError("参考教材 PDF 没有可解析页面。")
    return page_count


def _page_blocks(page_count: int) -> list[tuple[int, int, int]]:
    """Return (one-based block index, zero-based inclusive start/end)."""
    return [
        (index + 1, start, min(page_count - 1, start + REFERENCE_BLOCK_PAGES - 1))
        for index, start in enumerate(range(0, page_count, REFERENCE_BLOCK_PAGES))
    ]


def _find_content_list(output_dir: Path) -> Path:
    candidates = sorted(
        path
        for path in output_dir.rglob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    )
    if not candidates:
        raise RuntimeError("MinerU 已结束，但没有生成 content_list.json。")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _configuration() -> dict[str, Any]:
    return {
        "method": str(os.environ.get("MPB_MINERU_METHOD") or "auto"),
        "backend": str(os.environ.get("MPB_MINERU_BACKEND") or "pipeline"),
        "effort": str(os.environ.get("MPB_MINERU_EFFORT") or "high"),
        "formula": True,
        "table": True,
        "image_analysis": False,
        "model_source": str(
            os.environ.get("MINERU_MODEL_SOURCE") or "huggingface"
        ),
        "context_window_blocks": 3,
        "raganything_context_mode": "chunk",
        "page_block_size": REFERENCE_BLOCK_PAGES,
    }


def _parse_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"MinerU content_list 无法读取：{path}") from error
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RuntimeError("MinerU content_list 顶层必须是对象数组。")
    return [dict(item) for item in payload]


def _mineru_command(
    executable: Path,
    source_path: Path,
    output_dir: Path,
    configuration: dict[str, Any],
    *,
    start_page: int,
    end_page: int,
) -> list[str]:
    command = [
        str(executable),
        "-p",
        str(source_path),
        "-o",
        str(output_dir),
        "-m",
        str(configuration["method"]),
        "-b",
        str(configuration["backend"]),
        "-f",
        "true",
        "-t",
        "true",
        "-s",
        str(start_page),
        "-e",
        str(end_page),
    ]
    if str(configuration["backend"]).startswith("hybrid"):
        command.extend(
            [
                "--effort",
                str(configuration["effort"]),
                "--image-analysis",
                "true",
            ]
        )
    return command


def _checkpoint_directory(
    run_dir: Path,
    block_index: int,
    start_page: int,
    end_page: int,
) -> Path:
    return (
        run_dir
        / "blocks"
        / (
            f"block_{block_index:04d}_pages_"
            f"{start_page + 1:04d}_{end_page + 1:04d}"
        )
    )


def _checkpoint_assets(
    content_list_path: Path,
    items: list[dict[str, Any]],
) -> list[dict[str, str]]:
    assets: dict[str, dict[str, str]] = {}
    for item in items:
        raw = str(item.get("img_path") or "").strip()
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = content_list_path.parent / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise RuntimeError(f"MinerU 图表资源不存在：{candidate}")
        key = str(candidate)
        assets[key] = {
            "path": key,
            "sha256": _sha256_file(candidate),
        }
    return [assets[key] for key in sorted(assets)]


def _page_index_mode(
    items: list[dict[str, Any]],
    *,
    start_page: int,
    end_page: int,
) -> str:
    page_indices = [int(item.get("page_idx") or 0) for item in items]
    if not page_indices:
        raise RuntimeError(
            f"MinerU 第 {start_page + 1}-{end_page + 1} 页没有生成任何内容块。"
        )
    block_page_count = end_page - start_page + 1
    if start_page > 0 and all(start_page <= value <= end_page for value in page_indices):
        return "source_absolute"
    if all(0 <= value < block_page_count for value in page_indices):
        return "block_relative"
    raise RuntimeError(
        "MinerU 分块页码越界："
        f"期望块内 0-{block_page_count - 1} 或原书 {start_page}-{end_page}。"
    )


def _validate_checkpoint(
    checkpoint_dir: Path,
    *,
    fingerprint: str,
    block_index: int,
    start_page: int,
    end_page: int,
) -> tuple[dict[str, Any], Path, list[dict[str, Any]]] | None:
    manifest_path = checkpoint_dir / CHECKPOINT_MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        content_list_path = Path(str(manifest.get("content_list_path") or ""))
        if not content_list_path.is_absolute():
            content_list_path = checkpoint_dir / content_list_path
        if not (
            manifest.get("complete") is True
            and str(manifest.get("fingerprint") or "") == fingerprint
            and int(manifest.get("block_index") or 0) == block_index
            and int(manifest.get("start_page", -1)) == start_page
            and int(manifest.get("end_page", -1)) == end_page
            and content_list_path.is_file()
            and _sha256_file(content_list_path)
            == str(manifest.get("content_list_sha256") or "")
        ):
            return None
        items = _parse_json_list(content_list_path)
        mode = _page_index_mode(
            items,
            start_page=start_page,
            end_page=end_page,
        )
        if str(manifest.get("page_index_mode") or "") != mode:
            return None
        actual_assets = _checkpoint_assets(content_list_path, items)
        expected_assets = manifest.get("assets") or []
        if actual_assets != expected_assets:
            return None
        return manifest, content_list_path, items
    except (OSError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
        return None


def _write_checkpoint(
    *,
    executable: Path,
    source_path: Path,
    run_dir: Path,
    configuration: dict[str, Any],
    fingerprint: str,
    block_index: int,
    block_count: int,
    start_page: int,
    end_page: int,
    emit: Callable[[str], None],
    timeout_seconds: float,
) -> tuple[dict[str, Any], Path, list[dict[str, Any]], bool]:
    checkpoint_dir = _checkpoint_directory(
        run_dir, block_index, start_page, end_page
    )
    cached = _validate_checkpoint(
        checkpoint_dir,
        fingerprint=fingerprint,
        block_index=block_index,
        start_page=start_page,
        end_page=end_page,
    )
    if cached is not None:
        manifest, content_list_path, items = cached
        emit(
            f"MinerU 已校验并跳过完成块 {block_index}/{block_count}："
            f"原书第 {start_page + 1}-{end_page + 1} 页。"
        )
        return manifest, content_list_path, items, True

    attempt_dir = checkpoint_dir / "attempts" / f"attempt-{uuid.uuid4().hex}"
    output_dir = attempt_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=False)
    emit(
        f"MinerU 开始处理块 {block_index}/{block_count}："
        f"原书第 {start_page + 1}-{end_page + 1} 页（每块最多 32 页）。"
    )
    _stream_process(
        _mineru_command(
            executable,
            source_path,
            output_dir,
            configuration,
            start_page=start_page,
            end_page=end_page,
        ),
        cwd=run_dir,
        emit=emit,
        timeout_seconds=timeout_seconds,
    )
    content_list_path = _find_content_list(output_dir).resolve()
    items = _parse_json_list(content_list_path)
    page_index_mode = _page_index_mode(
        items,
        start_page=start_page,
        end_page=end_page,
    )
    assets = _checkpoint_assets(content_list_path, items)
    manifest = {
        "checkpoint_schema_version": 1,
        "fingerprint": fingerprint,
        "complete": True,
        "block_index": block_index,
        "block_count": block_count,
        "start_page": start_page,
        "end_page": end_page,
        "page_count": end_page - start_page + 1,
        "page_index_mode": page_index_mode,
        "content_list_path": str(content_list_path),
        "content_list_sha256": _sha256_file(content_list_path),
        "source_block_count": len(items),
        "assets": assets,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # This is the commit point.  A failed/partial attempt has no complete
    # checkpoint manifest and will be redone on the next invocation.
    _atomic_json(checkpoint_dir / CHECKPOINT_MANIFEST_NAME, manifest)
    emit(
        f"MinerU 块 {block_index}/{block_count} 已校验并写入检查点："
        f"原书第 {start_page + 1}-{end_page + 1} 页。"
    )
    return manifest, content_list_path, items, False


def _merge_checkpoints(
    run_dir: Path,
    checkpoints: list[tuple[dict[str, Any], Path, list[dict[str, Any]]]],
) -> Path:
    merged: list[dict[str, Any]] = []
    for manifest, content_list_path, items in checkpoints:
        start_page = int(manifest["start_page"])
        mode = str(manifest["page_index_mode"])
        for item in items:
            normalized = dict(item)
            raw_page_index = int(normalized.get("page_idx") or 0)
            normalized["page_idx"] = (
                raw_page_index + start_page
                if mode == "block_relative"
                else raw_page_index
            )
            raw_asset = str(normalized.get("img_path") or "").strip()
            if raw_asset:
                asset_path = Path(raw_asset)
                if not asset_path.is_absolute():
                    asset_path = content_list_path.parent / asset_path
                normalized["img_path"] = str(asset_path.resolve())
            normalized["mpb_source_block"] = int(manifest["block_index"])
            merged.append(normalized)
    if not merged:
        raise RuntimeError("MinerU 所有分块合并后没有内容。")
    merged_path = run_dir / "MPB_MERGED_content_list.json"
    _atomic_json(merged_path, merged)
    return merged_path.resolve()


def _raganything_contexts(
    content_list_path: Path,
    cache_path: Path,
    emit: Callable[[str], None],
) -> dict[int, str]:
    """Use RAG-Anything's deterministic ContextExtractor without an LLM call."""
    python_path = _runtime_root() / ".venv" / "Scripts" / "python.exe"
    if not python_path.is_file():
        return {}
    content_sha256 = _sha256_file(content_list_path)
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and str(cached.get("content_list_sha256") or "") == content_sha256
                and isinstance(cached.get("contexts"), dict)
            ):
                return {
                    int(key): str(value)
                    for key, value in cached["contexts"].items()
                    if str(value).strip()
                }
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    script = r"""
import json, sys
from raganything.modalprocessors import ContextConfig, ContextExtractor
source_path, output_path = sys.argv[1], sys.argv[2]
content = json.load(open(source_path, 'r', encoding='utf-8'))
extractor = ContextExtractor(ContextConfig(
    context_window=3,
    context_mode='chunk',
    max_context_tokens=3000,
    include_headers=True,
    include_captions=True,
    filter_content_types=['text', 'image', 'chart', 'table'],
))
contexts = {}
for index, item in enumerate(content):
    if str(item.get('type') or '').lower() in {'image', 'chart', 'table'}:
        contexts[str(index)] = extractor.extract_context(
            content, {'page_idx': int(item.get('page_idx') or 0), 'index': index}, 'minerU'
        )
json.dump({'contexts': contexts}, open(output_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
"""
    temporary_output = cache_path.with_suffix(cache_path.suffix + ".tmp")
    try:
        completed = subprocess.run(
            [
                str(python_path),
                "-c",
                script,
                str(content_list_path),
                str(temporary_output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
        if completed.returncode != 0 or not temporary_output.is_file():
            emit(
                "RAG-Anything 上下文提取不可用，已保留确定性的相邻块上下文："
                + str(completed.stderr or completed.stdout or "unknown error")[-500:]
            )
            temporary_output.unlink(missing_ok=True)
            return {}
        payload = json.loads(temporary_output.read_text(encoding="utf-8"))
        contexts = {
            int(key): str(value)
            for key, value in dict(payload.get("contexts") or {}).items()
            if str(value).strip()
        }
        _atomic_json(
            cache_path,
            {
                "schema_version": 1,
                "content_list_sha256": content_sha256,
                "configuration": {
                    "context_window": 3,
                    "context_mode": "chunk",
                    "include_headers": True,
                    "include_captions": True,
                    "max_context_tokens": 3000,
                },
                "contexts": {str(key): value for key, value in contexts.items()},
            },
        )
        emit(
            f"RAG-Anything 已为 {len(contexts)} 个图表块绑定标题、图注和邻近数学上下文；"
            "此步骤不调用远程模型。"
        )
        return contexts
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
        temporary_output.unlink(missing_ok=True)
        emit(f"RAG-Anything 上下文提取失败，已使用确定性降级：{error}")
        return {}


def _list_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _block_text(item: dict[str, Any]) -> str:
    kind = str(item.get("type") or "").casefold()
    if kind in {"text", "equation"}:
        return str(item.get("text") or "").strip()
    if kind in {"image", "chart"}:
        return " ".join(
            value
            for value in (
                _list_text(item.get("image_caption")),
                _list_text(item.get("chart_caption")),
                str(item.get("content") or "").strip(),
            )
            if value
        )
    if kind == "table":
        return " ".join(
            value
            for value in (
                _list_text(item.get("table_caption")),
                str(item.get("table_body") or "").strip(),
                _list_text(item.get("table_footnote")),
            )
            if value
        )
    if kind == "code":
        return str(item.get("code_body") or "").strip()
    if kind == "list":
        return "\n".join(str(value) for value in item.get("list_items") or [])
    return str(item.get("text") or item.get("content") or "").strip()


def _asset_path(content_list_path: Path, item: dict[str, Any]) -> Path | None:
    raw = str(item.get("img_path") or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = content_list_path.parent / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    return candidate if candidate.is_file() else None


def _figure_id(
    source_sha256: str,
    page_number: int,
    block_index: int,
    item: dict[str, Any],
    asset: Path | None,
) -> str:
    seed = json.dumps(
        {
            "source": source_sha256,
            "page": page_number,
            "block": block_index,
            "type": item.get("type"),
            "bbox": item.get("bbox"),
            "asset": _sha256_file(asset) if asset else "",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "reference-figure-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _render_block(item: dict[str, Any], figure: dict[str, Any] | None) -> str:
    kind = str(item.get("type") or "").casefold()
    if kind == "text":
        text = str(item.get("text") or "").strip()
        level = int(item.get("text_level") or 0)
        return ("#" * min(6, max(1, level)) + " " + text) if level else text
    if kind == "equation":
        return str(item.get("text") or "").strip()
    if kind == "table":
        caption = _list_text(item.get("table_caption")) or "Table"
        body = str(item.get("table_body") or "").strip()
        footnote = _list_text(item.get("table_footnote"))
        pieces = [f"**{caption}**", body]
        if footnote:
            pieces.append(f"Table footnote: {footnote}")
        if figure and figure.get("asset_path"):
            pieces.append(
                f"![{caption}](mineru-asset://{figure['figure_id']})"
            )
        return "\n\n".join(piece for piece in pieces if piece)
    if kind in {"image", "chart"} and figure:
        caption = str(figure.get("caption") or "Mathematical figure")
        description = str(figure.get("mineru_description") or "").strip()
        pieces = [f"![{caption}](mineru-asset://{figure['figure_id']})"]
        if description:
            pieces.append(f"MinerU visual description: {description}")
        pieces.append(
            f"Reference figure obligation: `{figure['figure_id']}`; "
            "the source crop, caption, coordinates and surrounding context are in the part figure manifest."
        )
        return "\n\n".join(pieces)
    if kind == "code":
        return "```\n" + str(item.get("code_body") or "").strip() + "\n```"
    if kind == "list":
        return "\n".join(f"- {value}" for value in item.get("list_items") or [])
    return _block_text(item)


def _chunks_from_content_list(
    content_list_path: Path,
    *,
    source_sha256: str,
    raganything_contexts: dict[int, str] | None = None,
) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    items = _parse_json_list(content_list_path)
    page_count = max((int(item.get("page_idx") or 0) for item in items), default=-1) + 1
    heading_stack: dict[int, str] = {}
    block_records: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    raganything_contexts = dict(raganything_contexts or {})
    for block_index, item in enumerate(items, start=1):
        page_number = int(item.get("page_idx") or 0) + 1
        kind = str(item.get("type") or "").casefold()
        text = _block_text(item)
        if kind == "text" and int(item.get("text_level") or 0) > 0:
            level = int(item.get("text_level") or 0)
            heading_stack[level] = text
            for stale in [value for value in heading_stack if value > level]:
                heading_stack.pop(stale, None)
        figure: dict[str, Any] | None = None
        if kind in {"image", "chart", "table"} and str(item.get("sub_type") or "") != "seal":
            asset = _asset_path(content_list_path, item)
            figure = {
                "figure_id": _figure_id(
                    source_sha256, page_number, block_index, item, asset
                ),
                "kind": kind,
                "page_number": page_number,
                "bbox": list(item.get("bbox") or []),
                "caption": (
                    _list_text(item.get("image_caption"))
                    or _list_text(item.get("chart_caption"))
                    or _list_text(item.get("table_caption"))
                ),
                "footnote": (
                    _list_text(item.get("image_footnote"))
                    or _list_text(item.get("chart_footnote"))
                    or _list_text(item.get("table_footnote"))
                ),
                "mineru_description": str(item.get("content") or "").strip(),
                "source_asset_path": str(asset or ""),
                "source_asset_sha256": _sha256_file(asset) if asset else "",
                "heading_path": [heading_stack[key] for key in sorted(heading_stack)],
                "requires_web_visual_review": kind in {"image", "chart"},
                "candidate_latex_kind": (
                    "tikz_or_tikz-cd" if kind in {"image", "chart"} else "latex_table"
                ),
                "raganything_context": str(
                    raganything_contexts.get(block_index - 1) or ""
                ),
            }
            figures.append(figure)
        block_records.append(
            {
                "block_id": f"B{block_index:06d}",
                "page_number": page_number,
                "kind": kind,
                "text": text,
                "rendered": _render_block(item, figure),
                "figure": figure,
                "is_heading": kind == "text" and int(item.get("text_level") or 0) > 0,
            }
        )

    for index, record in enumerate(block_records):
        figure = record.get("figure")
        if not isinstance(figure, dict):
            continue
        before = [
            value["text"]
            for value in block_records[max(0, index - 3) : index]
            if str(value.get("text") or "").strip()
        ]
        after = [
            value["text"]
            for value in block_records[index + 1 : index + 4]
            if str(value.get("text") or "").strip()
        ]
        figure["context_before"] = before
        figure["context_after"] = after
        figure["context_source"] = "mineru_content_list_chunk_window"

    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_page = 0
    current_length = 0

    def flush() -> None:
        nonlocal current, current_page, current_length
        if not current:
            return
        block_ids = [str(item["block_id"]) for item in current]
        content = "\n\n".join(
            str(item.get("rendered") or "").strip()
            for item in current
            if str(item.get("rendered") or "").strip()
        ).strip()
        if content:
            chunks.append(
                {
                    "locator": (
                        f"PDF page {current_page} (MinerU blocks "
                        f"{block_ids[0]}-{block_ids[-1]})"
                    ),
                    "content": content,
                    "page_number": current_page,
                    "extraction_method": "mineru",
                    "ocr_confidence": None,
                    "ocr_error": "",
                    "mineru_block_ids": block_ids,
                    "multimodal_elements": [
                        dict(item["figure"])
                        for item in current
                        if isinstance(item.get("figure"), dict)
                    ],
                    "detected_section_start": any(
                        bool(item.get("is_heading")) for item in current
                    ),
                }
            )
        current = []
        current_page = 0
        current_length = 0

    for record in block_records:
        rendered = str(record.get("rendered") or "").strip()
        if not rendered:
            continue
        page_number = int(record["page_number"])
        projected = current_length + len(rendered) + (2 if current else 0)
        if current and (
            page_number != current_page or projected > REFERENCE_CHUNK_CHARACTERS
        ):
            flush()
        current_page = page_number
        current.append(record)
        current_length += len(rendered) + (2 if len(current) > 1 else 0)
    flush()
    if not chunks:
        raise RuntimeError("MinerU content_list 没有可写入的正文、公式或图表块。")
    coverage = {
        "source_block_count": len(items),
        "persisted_block_count": len(
            {block_id for chunk in chunks for block_id in chunk["mineru_block_ids"]}
        ),
        "figure_count": sum(1 for item in figures if item["kind"] in {"image", "chart"}),
        "table_count": sum(1 for item in figures if item["kind"] == "table"),
        "equation_count": sum(1 for item in block_records if item["kind"] == "equation"),
    }
    if coverage["persisted_block_count"] != coverage["source_block_count"]:
        missing = coverage["source_block_count"] - coverage["persisted_block_count"]
        # Auxiliary headers/footers can be empty, but non-empty blocks must be preserved.
        nonempty = sum(
            1 for item in block_records if str(item.get("rendered") or "").strip()
        )
        if coverage["persisted_block_count"] != nonempty:
            raise RuntimeError(f"MinerU 内容块覆盖校验失败，缺少 {missing} 个块。")
        coverage["empty_auxiliary_block_count"] = missing
    return page_count, chunks, coverage


@dataclass(frozen=True)
class MinerUExtraction:
    page_count: int
    chunks: list[dict[str, Any]]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class MinerUCachedPageLayout:
    """Read-only page layout recovered from a completed MinerU cache."""

    page_index: int
    page_width: float
    page_height: float
    blocks: list[dict[str, Any]]
    content_list_path: str


def load_cached_page_layout(
    source_path: Path,
    page_index: int,
    *,
    cache_roots: list[Path] | None = None,
) -> MinerUCachedPageLayout | None:
    """Load text/equation boxes without launching MinerU or changing its cache.

    MinerU content-list boxes use a normalized 0..1000 page coordinate space.
    The returned boxes are converted to PDF point coordinates so the PDF viewer
    can safely compare them with PyMuPDF word/character boxes.
    """

    source_path = Path(source_path).resolve()
    if not source_path.is_file() or source_path.suffix.casefold() != ".pdf":
        return None
    target_page = int(page_index)
    if target_page < 0:
        return None
    source_sha256 = _sha256_file(source_path)
    roots = [Path(value).resolve() for value in (cache_roots or [])]
    reference_root = source_path.parent.parent
    sibling_cache = reference_root / "mineru_cache"
    if sibling_cache.is_dir() and sibling_cache not in roots:
        roots.append(sibling_cache)

    manifests: list[Path] = []
    for root in roots:
        source_cache = root / source_sha256
        if source_cache.is_dir():
            manifests.extend(source_cache.glob("*/MPB_MINERU_MANIFEST.json"))
            manifests.extend(source_cache.glob("*/pages/*/MPB_MINERU_PAGE_MANIFEST.json"))
    manifests.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)

    try:
        import fitz  # type: ignore

        with fitz.open(source_path) as document:
            if not 0 <= target_page < int(document.page_count):
                return None
            rect = document.load_page(target_page).rect
            page_width = float(rect.width)
            page_height = float(rect.height)
    except Exception:
        return None

    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not (
                manifest.get("complete") is True
                and str(manifest.get("source_sha256") or "") == source_sha256
            ):
                continue
            content_list_path = Path(str(manifest.get("content_list_path") or ""))
            if not content_list_path.is_absolute():
                content_list_path = manifest_path.parent / content_list_path
            if not (
                content_list_path.is_file()
                and _sha256_file(content_list_path)
                == str(manifest.get("content_list_sha256") or "")
            ):
                continue
            page_items = [
                dict(item)
                for item in _parse_json_list(content_list_path)
                if int(item.get("page_idx") or 0) == target_page
            ]
            blocks: list[dict[str, Any]] = []
            for item in page_items:
                bbox = item.get("bbox") or []
                if len(bbox) < 4:
                    continue
                x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
                if x1 <= x0 or y1 <= y0:
                    continue
                blocks.append(
                    {
                        "kind": str(item.get("type") or "").casefold(),
                        "text": _block_text(item),
                        "x0": page_width * x0 / 1000.0,
                        "y0": page_height * y0 / 1000.0,
                        "x1": page_width * x1 / 1000.0,
                        "y1": page_height * y1 / 1000.0,
                    }
                )
            if blocks:
                return MinerUCachedPageLayout(
                    page_index=target_page,
                    page_width=page_width,
                    page_height=page_height,
                    blocks=blocks,
                    content_list_path=str(content_list_path.resolve()),
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
            continue
    return None


def extract_pdf_page_layout_with_mineru(
    source_path: Path,
    page_index: int,
    cache_root: Path,
    emit: Callable[[str], None] | None = None,
) -> MinerUCachedPageLayout:
    """Extract one displayed PDF page and persist a reusable layout cache."""

    emit = emit or (lambda _message: None)
    source_path = Path(source_path).resolve()
    target_page = int(page_index)
    cached = load_cached_page_layout(source_path, target_page, cache_roots=[cache_root])
    if cached is not None:
        emit(f"已复用 PDF 第 {target_page + 1} 页的 MinerU 选词校验缓存。")
        return cached
    executable = _mineru_executable()
    if executable is None:
        raise FileNotFoundError("MinerU 运行时尚未安装，当前页继续使用 PDF 原生文本层。")
    page_count = _pdf_page_count(source_path)
    if not 0 <= target_page < page_count:
        raise ValueError("MinerU 当前页索引越界。")
    configuration = _configuration()
    status = reference_pipeline_status()
    source_sha256 = _sha256_file(source_path)
    fingerprint_payload = {
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "mineru_version": status["mineru_version"],
        "configuration": configuration,
        "scope": "pdf_vocabulary_page",
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    run_dir = Path(cache_root).resolve() / source_sha256 / fingerprint
    page_dir = run_dir / "pages" / f"page_{target_page + 1:04d}"
    manifest_path = page_dir / "MPB_MINERU_PAGE_MANIFEST.json"
    page_dir.mkdir(parents=True, exist_ok=True)
    timeout_seconds = float(os.environ.get("MPB_MINERU_PAGE_TIMEOUT_SECONDS") or 900)
    emit(f"MinerU 正在后台校验 PDF 第 {target_page + 1} 页的正文与公式区域。")
    checkpoint, content_list_path, items, reused = _write_checkpoint(
        executable=executable,
        source_path=source_path,
        run_dir=page_dir,
        configuration=configuration,
        fingerprint=fingerprint,
        block_index=1,
        block_count=1,
        start_page=target_page,
        end_page=target_page,
        emit=emit,
        timeout_seconds=timeout_seconds,
    )
    mode = str(checkpoint.get("page_index_mode") or "")
    normalized: list[dict[str, Any]] = []
    for item in items:
        value = dict(item)
        if mode == "block_relative":
            value["page_idx"] = int(value.get("page_idx") or 0) + target_page
        normalized.append(value)
    merged_path = page_dir / "MPB_PAGE_content_list.json"
    _atomic_json(merged_path, normalized)
    manifest = {
        **fingerprint_payload,
        "fingerprint": fingerprint,
        "complete": True,
        "page_index": target_page,
        "page_count": page_count,
        "content_list_path": str(merged_path.resolve()),
        "content_list_sha256": _sha256_file(merged_path),
        "cache_reused": bool(reused),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _atomic_json(manifest_path, manifest)
    layout = load_cached_page_layout(source_path, target_page, cache_roots=[cache_root])
    if layout is None:
        raise RuntimeError("MinerU 当前页解析完成，但坐标缓存回读失败。")
    return layout


def extract_pdf_with_mineru(
    source_path: Path,
    cache_root: Path,
    emit: Callable[[str], None] | None = None,
) -> MinerUExtraction:
    emit = emit or (lambda _message: None)
    source_path = source_path.resolve()
    executable = _mineru_executable()
    if executable is None:
        raise FileNotFoundError(
            "题库专用开源 MinerU 运行时尚未安装。请等待安装完成后重试；"
            "无需打开 MinerU 桌面软件。"
        )
    status = reference_pipeline_status()
    configuration = _configuration()
    source_sha256 = _sha256_file(source_path)
    source_page_count = _pdf_page_count(source_path)
    fingerprint_payload = {
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "mineru_version": status["mineru_version"],
        "configuration": configuration,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    run_dir = cache_root.resolve() / source_sha256 / fingerprint
    manifest_path = run_dir / "MPB_MINERU_MANIFEST.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            content_list_path = Path(str(manifest.get("content_list_path") or ""))
            if (
                manifest.get("complete") is True
                and str(manifest.get("fingerprint") or "") == fingerprint
                and content_list_path.is_file()
                and _sha256_file(content_list_path)
                == str(manifest.get("content_list_sha256") or "")
                and int(manifest.get("page_count") or 0) == source_page_count
            ):
                completed_checkpoints = []
                for block_index, start_page, end_page in _page_blocks(source_page_count):
                    checkpoint = _validate_checkpoint(
                        _checkpoint_directory(
                            run_dir, block_index, start_page, end_page
                        ),
                        fingerprint=fingerprint,
                        block_index=block_index,
                        start_page=start_page,
                        end_page=end_page,
                    )
                    if checkpoint is None:
                        completed_checkpoints = []
                        break
                    completed_checkpoints.append(checkpoint)
                if len(completed_checkpoints) != len(_page_blocks(source_page_count)):
                    raise ValueError("MinerU 最终清单对应的分块检查点不完整。")
                rag_contexts = _raganything_contexts(
                    content_list_path,
                    run_dir / "MPB_RAGANYTHING_CONTEXTS.json",
                    emit,
                )
                _parsed_page_count, chunks, coverage = _chunks_from_content_list(
                    content_list_path,
                    source_sha256=source_sha256,
                    raganything_contexts=rag_contexts,
                )
                emit("已复用哈希和解析配置完全一致的 MinerU 多模态缓存。")
                return MinerUExtraction(
                    page_count=source_page_count,
                    chunks=chunks,
                    manifest={**manifest, "coverage": coverage, "cache_reused": True},
                )
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    emit(
        "题库正在后台启动开源 MinerU；将提取正文、LaTeX 公式、表格和数学图，"
        "无需打开 MinerU 桌面软件。教材固定每 32 页一块并逐块保存检查点。"
    )
    timeout_seconds = float(os.environ.get("MPB_MINERU_TIMEOUT_SECONDS") or 21600)
    page_blocks = _page_blocks(source_page_count)
    checkpoint_payloads: list[
        tuple[dict[str, Any], Path, list[dict[str, Any]]]
    ] = []
    reused_checkpoint_count = 0
    for block_index, start_page, end_page in page_blocks:
        checkpoint, checkpoint_content, checkpoint_items, reused = _write_checkpoint(
            executable=executable,
            source_path=source_path,
            run_dir=run_dir,
            configuration=configuration,
            fingerprint=fingerprint,
            block_index=block_index,
            block_count=len(page_blocks),
            start_page=start_page,
            end_page=end_page,
            emit=emit,
            timeout_seconds=timeout_seconds,
        )
        reused_checkpoint_count += int(reused)
        checkpoint_payloads.append(
            (checkpoint, checkpoint_content, checkpoint_items)
        )
    content_list_path = _merge_checkpoints(run_dir, checkpoint_payloads)
    rag_contexts = _raganything_contexts(
        content_list_path,
        run_dir / "MPB_RAGANYTHING_CONTEXTS.json",
        emit,
    )
    _parsed_page_count, chunks, coverage = _chunks_from_content_list(
        content_list_path,
        source_sha256=source_sha256,
        raganything_contexts=rag_contexts,
    )
    manifest = {
        **fingerprint_payload,
        "fingerprint": fingerprint,
        "complete": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "page_count": source_page_count,
        "page_block_size": REFERENCE_BLOCK_PAGES,
        "block_count": len(page_blocks),
        "reused_checkpoint_count": reused_checkpoint_count,
        "checkpoints": [
            {
                "block_index": int(checkpoint["block_index"]),
                "start_page": int(checkpoint["start_page"]),
                "end_page": int(checkpoint["end_page"]),
                "manifest_path": str(
                    _checkpoint_directory(
                        run_dir,
                        int(checkpoint["block_index"]),
                        int(checkpoint["start_page"]),
                        int(checkpoint["end_page"]),
                    )
                    / CHECKPOINT_MANIFEST_NAME
                ),
                "content_list_sha256": str(checkpoint["content_list_sha256"]),
            }
            for checkpoint, _path, _items in checkpoint_payloads
        ],
        "content_list_path": str(content_list_path),
        "content_list_sha256": _sha256_file(content_list_path),
        "blocks_root": str(run_dir / "blocks"),
        "coverage": coverage,
        "cache_reused": False,
        "raganything_available": bool(status["raganything_available"]),
        "raganything_context_count": len(rag_contexts),
        "context_contract": {
            "mode": "chunk",
            "window": 3,
            "include_headers": True,
            "include_captions": True,
            "selection_policy": "complete_mapped_section_not_top_k",
        },
    }
    _atomic_json(manifest_path, manifest)
    return MinerUExtraction(
        page_count=source_page_count,
        chunks=chunks,
        manifest=manifest,
    )
