from __future__ import annotations

import contextlib
import datetime as dt
import faulthandler
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import traceback
from typing import Any, Callable, Iterator
import uuid

from shared.scripts.application_paths import APP_PATHS


UTC = dt.timezone.utc
DEFAULT_HEARTBEAT_SECONDS = 10
SESSION_RETENTION_DAYS = 30
APPLICATION_RETENTION_DAYS = 30
CRASH_RETENTION_DAYS = 90
PIPELINE_RETENTION_DAYS = 60
RING_BUFFER_SIZE = 500


def _utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def _timestamp(value: dt.datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="milliseconds")


def _safe_component(value: object, fallback: str = "unknown") -> str:
    text = "".join(
        character if character.isalnum() or character in "-_." else "-"
        for character in str(value or "").strip()
    ).strip("-.")
    return text[:96] or fallback


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        stream.flush()


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(text.rstrip("\n") + "\n")
        stream.flush()


def _default_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class PipelineRunLogger:
    def __init__(
        self,
        diagnostics: "CrashDiagnostics",
        *,
        pipeline: str,
        project_id: str,
        project_name: str,
        operation: str,
    ) -> None:
        self.diagnostics = diagnostics
        self.pipeline = _safe_component(pipeline, "pipeline")
        self.project_id = _safe_component(project_id, "project")
        self.project_name = str(project_name or "")
        self.operation = str(operation or pipeline)
        self.run_id = (
            f"run-{_utc_now().astimezone().strftime('%Y%m%d-%H%M%S')}-"
            f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        self.directory = (
            diagnostics.pipeline_dir / self.pipeline / self.project_id / self.run_id
        )
        self.directory.mkdir(parents=True, exist_ok=False)
        self.jsonl_path = self.directory / "events.jsonl"
        self.log_path = self.directory / "run.log"
        self.status_path = self.directory / "run.json"
        self.started_at = _timestamp()
        self._lock = threading.RLock()
        self._status = "running"
        self._last_stage = "starting"
        self._write_status()
        self.emit(
            f"流水线开始：{self.operation}",
            event="pipeline_started",
            stage="starting",
        )

    def _write_status(self, **extra: Any) -> None:
        payload = {
            "schema_version": 1,
            "run_id": self.run_id,
            "session_id": self.diagnostics.session_id,
            "status": self._status,
            "pipeline": self.pipeline,
            "operation": self.operation,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "stage": self._last_stage,
            "started_at": self.started_at,
            "updated_at": _timestamp(),
            "events_jsonl": str(self.jsonl_path),
            "human_log": str(self.log_path),
        }
        payload.update(extra)
        _atomic_json(self.status_path, payload)

    @staticmethod
    def infer_stage(message: str) -> str:
        lowered = message.casefold()
        if "开始处理块" in message or "checkpoint" in lowered or "检查点" in message:
            return "mineru_chunks"
        if "合并" in message or "merged" in lowered:
            return "mineru_merge"
        if "章级" in message or "节级" in message or "截图" in message:
            return "mineru_postprocess"
        if "项目目录" in message or "写后回读" in message:
            return "project_write_complete"
        if "刷新" in message or "callback" in lowered:
            return "ui_refresh"
        return "running"

    def emit(
        self,
        message: str,
        *,
        level: str = "INFO",
        event: str = "pipeline_progress",
        stage: str = "",
        exception: str = "",
    ) -> None:
        clean = str(message or "").rstrip()
        if not clean:
            return
        with self._lock:
            self._last_stage = stage or self.infer_stage(clean)
            payload = self.diagnostics.event_payload(
                level=level,
                logger=f"pipeline.{self.pipeline}",
                event=event,
                message=clean,
                project_id=self.project_id,
                operation=self.operation,
                stage=self._last_stage,
                exception=exception,
                run_id=self.run_id,
            )
            _append_jsonl(self.jsonl_path, payload)
            local_time = dt.datetime.fromisoformat(payload["timestamp"]).astimezone()
            _append_text(
                self.log_path,
                f"[{local_time:%Y-%m-%d %H:%M:%S}] "
                f"[{level}] [{self._last_stage}] {clean}",
            )
            self._write_status()
            context_values = {
                "current_operation": self.operation,
                "active_project_id": self.project_id,
                "active_project_name": self.project_name,
                "active_pipeline": self.pipeline,
                "active_stage": self._last_stage,
            }
            if level.upper() not in {"ERROR", "CRITICAL"}:
                context_values["last_successful_step"] = event
            self.diagnostics.update_context(emit_event=False, **context_values)
            self.diagnostics.log_event(**{
                key: payload[key]
                for key in ("level", "logger", "event", "message", "project_id", "operation", "stage")
            })

    def finish(self, message: str = "流水线完成。") -> None:
        with self._lock:
            if self._status != "running":
                return
            self.emit(message, event="pipeline_finished", stage="completed")
            self._status = "completed"
            self._last_stage = "completed"
            self._write_status(finished_at=_timestamp())

    def fail(self, message: str, traceback_text: str = "") -> None:
        with self._lock:
            if self._status != "running":
                return
            self.emit(
                message,
                level="ERROR",
                event="pipeline_failed",
                stage="failed",
                exception=traceback_text,
            )
            self._status = "failed"
            self._last_stage = "failed"
            self._write_status(finished_at=_timestamp(), exception=traceback_text)


class CrashDiagnostics:
    def __init__(
        self,
        root: str | Path,
        *,
        process_alive_probe: Callable[[int], bool] | None = None,
        windows_event_lookup: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.crash_dir = self.root / "crash"
        self.application_dir = self.root / "application"
        self.pipeline_dir = self.root / "pipelines"
        self.session_dir = self.root / "sessions"
        self._process_alive_probe = process_alive_probe or _default_process_alive
        self._windows_event_lookup_enabled = windows_event_lookup
        self._lock = threading.RLock()
        self._ring: list[dict[str, Any]] = []
        self._context: dict[str, Any] = {}
        self._workers: dict[str, dict[str, Any]] = {}
        self._session: dict[str, Any] = {}
        self._session_path: Path | None = None
        self._session_events_path: Path | None = None
        self._application_log_path: Path | None = None
        self._native_stream: Any = None
        self._started = False
        self._clean = False
        self._original_sys_hook: Any = None
        self._original_thread_hook: Any = None
        self._original_unraisable_hook: Any = None
        self._original_qt_handler: Any = None
        self._qt_handler_installed = False

    @property
    def session_id(self) -> str:
        return str(self._session.get("session_id") or "")

    @property
    def is_started(self) -> bool:
        return self._started

    def _runtime_metadata(self) -> dict[str, Any]:
        try:
            pyside_version = importlib.metadata.version("PySide6")
        except importlib.metadata.PackageNotFoundError:
            pyside_version = ""
        git_commit = ""
        git_dirty: bool | None = None
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=no"],
                cwd=APP_PATHS.application_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            git_dirty = bool(completed.stdout.strip()) if completed.returncode == 0 else None
            revision = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=APP_PATHS.application_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if revision.returncode == 0:
                git_commit = revision.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return {
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "executable": sys.executable,
            "python_version": sys.version,
            "pyside_version": pyside_version,
            "qt_version": "",
            "windows_version": platform.platform(),
            "cwd": os.getcwd(),
            "argv": list(sys.argv),
            "repository_root": str(APP_PATHS.application_root),
            "git_commit": git_commit,
            "git_dirty": git_dirty,
        }

    def start_session(self) -> list[Path]:
        with self._lock:
            if self._started:
                return []
            for directory in (
                self.root,
                self.crash_dir,
                self.application_dir,
                self.pipeline_dir,
                self.session_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            recoveries = self._recover_unclean_sessions()
            now = _utc_now()
            session_id = (
                f"{now.astimezone():%Y%m%d-%H%M%S}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            self._session_path = self.session_dir / f"session-{session_id}.json"
            self._session_events_path = self.session_dir / f"session-{session_id}.jsonl"
            self._application_log_path = (
                self.application_dir / f"application-{now.astimezone():%Y%m%d}.jsonl"
            )
            self._session = {
                "schema_version": 1,
                "session_id": session_id,
                "status": "running",
                "started_at": _timestamp(now),
                "last_heartbeat_at": _timestamp(now),
                "last_event_at": _timestamp(now),
                "clean_shutdown_at": "",
                "exit_code": None,
                "context": {},
                "active_workers": [],
                "runtime": self._runtime_metadata(),
                "paths": {
                    "session": str(self._session_path),
                    "events": str(self._session_events_path),
                    "application_log": str(self._application_log_path),
                    "diagnostics_root": str(self.root),
                },
            }
            self._started = True
            self._clean = False
            _atomic_json(self._session_path, self._session)
            native_path = self.crash_dir / f"native-{session_id}.log"
            self._native_stream = native_path.open("a", encoding="utf-8")
            faulthandler.enable(file=self._native_stream, all_threads=True)
            self.log_event(
                level="INFO",
                logger="application",
                event="application_started",
                message="题库管理中心会话已启动。",
            )
            self._apply_retention()
            return recoveries

    def _recover_unclean_sessions(self) -> list[Path]:
        recovered: list[Path] = []
        for marker in sorted(self.session_dir.glob("session-*.json")):
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if str(previous.get("status") or "") != "running":
                continue
            pid = int((previous.get("runtime") or {}).get("pid") or 0)
            if self._process_alive_probe(pid):
                continue
            detected_at = _utc_now()
            previous["status"] = "unclean_exit_detected"
            previous["unclean_exit_detected_at"] = _timestamp(detected_at)
            _atomic_json(marker, previous)
            recovered.append(self._write_unclean_report(previous, detected_at))
        return recovered

    def _windows_events(self, previous: dict[str, Any]) -> dict[str, Any]:
        if os.name != "nt" or not self._windows_event_lookup_enabled:
            return {"status": "unavailable", "reason": "Windows event lookup disabled or non-Windows"}
        heartbeat = str(previous.get("last_heartbeat_at") or previous.get("started_at") or "")
        try:
            center = dt.datetime.fromisoformat(heartbeat).astimezone()
        except ValueError:
            center = dt.datetime.now().astimezone()
        start = (center - dt.timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S")
        end = (center + dt.timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S")
        script = (
            "$events=Get-WinEvent -FilterHashtable @{LogName='Application';"
            f"StartTime=[datetime]'{start}';EndTime=[datetime]'{end}';Id=1000,1001,1002}} "
            "-ErrorAction SilentlyContinue|Select-Object TimeCreated,Id,ProviderName,Message;"
            "$events|ConvertTo-Json -Depth 3 -Compress"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {"status": "unavailable", "reason": str(error)}
        if completed.returncode != 0:
            return {"status": "unavailable", "reason": completed.stderr.strip()}
        try:
            events = json.loads(completed.stdout) if completed.stdout.strip() else []
        except ValueError:
            events = []
        return {"status": "ok", "events": events if isinstance(events, list) else [events]}

    def _tail_events(self, path_text: object, limit: int = 200) -> list[dict[str, Any]]:
        path = Path(str(path_text or ""))
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def _write_unclean_report(
        self, previous: dict[str, Any], detected_at: dt.datetime
    ) -> Path:
        session_id = str(previous.get("session_id") or "unknown")
        report_id = f"unclean-{detected_at.astimezone():%Y%m%d-%H%M%S}-{_safe_component(session_id)}"
        last_heartbeat = str(previous.get("last_heartbeat_at") or "")
        try:
            age = (detected_at - dt.datetime.fromisoformat(last_heartbeat).astimezone(UTC)).total_seconds()
        except ValueError:
            age = None
        events_path = (previous.get("paths") or {}).get("events")
        last_events = self._tail_events(events_path)
        report = {
            "schema_version": 1,
            "report_type": "unclean_exit",
            "report_id": report_id,
            "detected_at": _timestamp(detected_at),
            "previous_session": previous,
            "seconds_since_last_heartbeat": age,
            "last_events": last_events,
            "windows_event_lookup": self._windows_events(previous),
            "explanation": "上一会话保留 running 状态且对应 PID 已不存在；没有观察到 clean_shutdown。",
        }
        json_path = self.crash_dir / f"{report_id}.json"
        log_path = self.crash_dir / f"{report_id}.log"
        _atomic_json(json_path, report)
        readable = [
            "MathProblemBank unclean exit recovery report",
            f"Detected: {report['detected_at']}",
            f"Previous session: {session_id}",
            f"Last heartbeat: {last_heartbeat}",
            f"Seconds since heartbeat: {age}",
            f"Context: {json.dumps(previous.get('context') or {}, ensure_ascii=False)}",
            "",
            "--- Last events before exit ---",
        ]
        readable.extend(
            f"{event.get('timestamp')} [{event.get('level')}] "
            f"{event.get('event')}: {event.get('message')}"
            for event in last_events
        )
        log_path.write_text("\n".join(readable) + "\n", encoding="utf-8")
        return json_path

    def event_payload(
        self,
        *,
        level: str,
        logger: str,
        event: str,
        message: str,
        project_id: str = "",
        operation: str = "",
        stage: str = "",
        exception: str = "",
        run_id: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        thread = threading.current_thread()
        context = dict(self._context)
        payload: dict[str, Any] = {
            "timestamp": _timestamp(),
            "level": str(level or "INFO").upper(),
            "logger": str(logger or "application"),
            "event": str(event or "event"),
            "session_id": self.session_id,
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "thread_name": thread.name,
            "project_id": project_id or str(context.get("active_project_id") or ""),
            "operation": operation or str(context.get("current_operation") or ""),
            "stage": stage or str(context.get("active_stage") or ""),
            "message": str(message or ""),
            "exception": str(exception or ""),
        }
        if run_id:
            payload["run_id"] = run_id
        payload.update(extra)
        return payload

    def log_event(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            payload = self.event_payload(**fields)
            if not self._started or self._session_events_path is None:
                return payload
            _append_jsonl(self._session_events_path, payload)
            if self._application_log_path is not None:
                _append_jsonl(self._application_log_path, payload)
            self._ring.append(payload)
            if len(self._ring) > RING_BUFFER_SIZE:
                del self._ring[: len(self._ring) - RING_BUFFER_SIZE]
            self._session["last_event_at"] = payload["timestamp"]
            return payload

    def update_context(self, *, emit_event: bool = True, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                if value in (None, ""):
                    self._context.pop(key, None)
                else:
                    self._context[key] = value
            if self._started and emit_event:
                self.log_event(
                    level="DEBUG",
                    logger="runtime_context",
                    event="context_updated",
                    message="运行上下文已更新。",
                )

    @contextlib.contextmanager
    def operation_context(self, **values: Any) -> Iterator[None]:
        with self._lock:
            previous = dict(self._context)
        self.update_context(**values)
        try:
            yield
        finally:
            with self._lock:
                self._context = previous

    def register_worker(self, worker_id: str, **context: Any) -> None:
        with self._lock:
            self._workers[str(worker_id)] = {
                "worker_id": str(worker_id),
                "registered_at": _timestamp(),
                **context,
            }
            self.log_event(
                level="INFO",
                logger="worker",
                event="worker_started",
                message=f"后台任务已登记：{worker_id}",
                project_id=str(context.get("project_id") or ""),
                operation=str(context.get("operation") or ""),
                stage=str(context.get("stage") or ""),
            )

    def unregister_worker(self, worker_id: str, *, status: str = "finished") -> None:
        with self._lock:
            context = self._workers.pop(str(worker_id), {})
            self.log_event(
                level="INFO" if status == "finished" else "ERROR",
                logger="worker",
                event=f"worker_{status}",
                message=f"后台任务已结束：{worker_id}（{status}）",
                project_id=str(context.get("project_id") or ""),
                operation=str(context.get("operation") or ""),
                stage=str(context.get("stage") or ""),
            )

    def heartbeat(self, **context: Any) -> None:
        with self._lock:
            if not self._started or self._clean or self._session_path is None:
                return
            if context:
                self.update_context(**context)
            now = _timestamp()
            self._session["last_heartbeat_at"] = now
            self._session["last_event_at"] = self._session.get("last_event_at") or now
            self._session["context"] = dict(self._context)
            self._session["active_workers"] = list(self._workers.values())
            _atomic_json(self._session_path, self._session)

    def install_exception_hooks(self) -> None:
        if self._original_sys_hook is not None:
            return
        self._original_sys_hook = sys.excepthook
        self._original_thread_hook = threading.excepthook
        self._original_unraisable_hook = getattr(sys, "unraisablehook", None)

        def system_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
            self.record_exception(
                exc_type,
                exc,
                tb,
                kind="python_main_thread_unhandled",
                fatal=True,
            )
            if self._original_sys_hook is not None:
                self._original_sys_hook(exc_type, exc, tb)

        def thread_hook(args: Any) -> None:
            self.record_exception(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                kind="python_thread_unhandled",
                fatal=False,
                thread_name=str(getattr(args.thread, "name", "")),
                thread_id=getattr(args.thread, "ident", None),
            )
            if self._original_thread_hook is not None:
                self._original_thread_hook(args)

        def unraisable_hook(args: Any) -> None:
            self.record_exception(
                type(args.exc_value),
                args.exc_value,
                args.exc_traceback,
                kind="python_unraisable",
                fatal=False,
                unraisable_object=repr(args.object),
            )
            if self._original_unraisable_hook is not None:
                self._original_unraisable_hook(args)

        sys.excepthook = system_hook
        threading.excepthook = thread_hook
        if hasattr(sys, "unraisablehook"):
            sys.unraisablehook = unraisable_hook

    def install_asyncio_exception_handler(self, loop: Any) -> None:
        previous = loop.get_exception_handler()

        def handler(active_loop: Any, context: dict[str, Any]) -> None:
            exception = context.get("exception")
            if isinstance(exception, BaseException):
                self.record_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                    kind="asyncio_unhandled",
                    fatal=False,
                )
            else:
                self.log_event(
                    level="ERROR",
                    logger="asyncio",
                    event="asyncio_unhandled",
                    message=str(context.get("message") or context),
                )
            if previous is not None:
                previous(active_loop, context)
            else:
                active_loop.default_exception_handler(context)

        loop.set_exception_handler(handler)

    def restore_exception_hooks(self) -> None:
        if self._original_sys_hook is None:
            return
        sys.excepthook = self._original_sys_hook
        if self._original_thread_hook is not None:
            threading.excepthook = self._original_thread_hook
        if self._original_unraisable_hook is not None and hasattr(sys, "unraisablehook"):
            sys.unraisablehook = self._original_unraisable_hook
        self._original_sys_hook = None
        self._original_thread_hook = None
        self._original_unraisable_hook = None

    def install_qt_message_handler(self) -> bool:
        if self._qt_handler_installed:
            return True
        try:
            from PySide6 import QtCore
        except ImportError:
            return False

        message_names = {
            int(QtCore.QtMsgType.QtDebugMsg): "debug",
            int(QtCore.QtMsgType.QtInfoMsg): "info",
            int(QtCore.QtMsgType.QtWarningMsg): "warning",
            int(QtCore.QtMsgType.QtCriticalMsg): "critical",
            int(QtCore.QtMsgType.QtFatalMsg): "fatal",
        }

        def handler(message_type: Any, context: Any, message: str) -> None:
            kind = message_names.get(int(message_type), str(int(message_type)))
            level = {
                "debug": "DEBUG",
                "info": "INFO",
                "warning": "WARNING",
                "critical": "ERROR",
                "fatal": "CRITICAL",
            }.get(kind, "INFO")
            self.log_event(
                level=level,
                logger="qt",
                event=f"qt_{kind}",
                message=str(message),
                qt_file=str(getattr(context, "file", "") or ""),
                qt_line=int(getattr(context, "line", 0) or 0),
                qt_function=str(getattr(context, "function", "") or ""),
                qt_category=str(getattr(context, "category", "") or ""),
            )

        self._original_qt_handler = QtCore.qInstallMessageHandler(handler)
        self._qt_handler_installed = True
        try:
            self._session["runtime"]["qt_version"] = str(QtCore.qVersion())
            if self._session_path is not None:
                _atomic_json(self._session_path, self._session)
        except (KeyError, OSError):
            pass
        return True

    def restore_qt_message_handler(self) -> None:
        if not self._qt_handler_installed:
            return
        from PySide6 import QtCore

        QtCore.qInstallMessageHandler(self._original_qt_handler)
        self._original_qt_handler = None
        self._qt_handler_installed = False

    def record_exception(
        self,
        exc_type: type[BaseException],
        exc: BaseException,
        tb: Any,
        *,
        kind: str,
        fatal: bool,
        thread_name: str = "",
        thread_id: int | None = None,
        **extra: Any,
    ) -> tuple[Path, Path] | None:
        with self._lock:
            if not self._started:
                return None
            traceback_text = "".join(traceback.format_exception(exc_type, exc, tb))
            now = _utc_now()
            report_id = (
                f"crash-{now.astimezone():%Y%m%d-%H%M%S}-{os.getpid()}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            payload = {
                "schema_version": 1,
                "report_type": kind,
                "report_id": report_id,
                "timestamp": _timestamp(now),
                "fatal": fatal,
                "session_id": self.session_id,
                "pid": os.getpid(),
                "thread_id": thread_id if thread_id is not None else threading.get_ident(),
                "thread_name": thread_name or threading.current_thread().name,
                "exception_type": getattr(exc_type, "__name__", str(exc_type)),
                "message": str(exc),
                "traceback": traceback_text,
                "context": dict(self._context),
                "active_workers": list(self._workers.values()),
                "last_events": list(self._ring),
                **extra,
            }
            json_path = self.crash_dir / f"{report_id}.json"
            log_path = self.crash_dir / f"{report_id}.log"
            _atomic_json(json_path, payload)
            readable = [
                f"MathProblemBank diagnostic: {kind}",
                f"Timestamp: {payload['timestamp']}",
                f"Session: {self.session_id}",
                f"PID/thread: {os.getpid()} / {payload['thread_name']} ({payload['thread_id']})",
                f"Context: {json.dumps(payload['context'], ensure_ascii=False)}",
                "",
                "--- Traceback ---",
                traceback_text.rstrip(),
                "",
                "--- Last events before exception ---",
            ]
            readable.extend(
                f"{event.get('timestamp')} [{event.get('level')}] "
                f"{event.get('event')}: {event.get('message')}"
                for event in payload["last_events"]
            )
            log_path.write_text("\n".join(readable) + "\n", encoding="utf-8")
            self.log_event(
                level="CRITICAL" if fatal else "ERROR",
                logger="crash_diagnostics",
                event=kind,
                message=str(exc),
                exception=traceback_text,
            )
            if fatal and self._session_path is not None:
                self._session["status"] = "crash_reported"
                self._session["crash_report"] = str(json_path)
                _atomic_json(self._session_path, self._session)
            return json_path, log_path

    def start_pipeline_run(
        self,
        *,
        pipeline: str,
        project_id: str,
        project_name: str = "",
        operation: str = "",
    ) -> PipelineRunLogger:
        if not self._started:
            self.start_session()
        return PipelineRunLogger(
            self,
            pipeline=pipeline,
            project_id=project_id,
            project_name=project_name,
            operation=operation or pipeline,
        )

    def mark_clean_shutdown(self, exit_code: int = 0) -> None:
        with self._lock:
            if not self._started or self._clean or self._session_path is None:
                return
            now = _utc_now()
            try:
                started = dt.datetime.fromisoformat(str(self._session["started_at"])).astimezone(UTC)
                uptime = (now - started).total_seconds()
            except (KeyError, ValueError):
                uptime = None
            self.log_event(
                level="INFO",
                logger="application",
                event="clean_shutdown",
                message=f"题库管理中心正常退出，exit_code={exit_code}。",
            )
            self._session["status"] = "clean_shutdown"
            self._session["clean_shutdown_at"] = _timestamp(now)
            self._session["exit_code"] = int(exit_code)
            self._session["uptime_seconds"] = uptime
            self._session["context"] = dict(self._context)
            self._session["active_workers"] = list(self._workers.values())
            _atomic_json(self._session_path, self._session)
            self._clean = True
            if self._native_stream is not None:
                self._native_stream.flush()
                faulthandler.disable()
                self._native_stream.close()
                self._native_stream = None

    def _apply_retention(self) -> None:
        now = _utc_now().timestamp()
        policies = (
            (self.application_dir, APPLICATION_RETENTION_DAYS),
            (self.session_dir, SESSION_RETENTION_DAYS),
            (self.crash_dir, CRASH_RETENTION_DAYS),
            (self.pipeline_dir, PIPELINE_RETENTION_DAYS),
        )
        for directory, days in policies:
            cutoff = now - days * 86400
            try:
                candidates = list(directory.rglob("*"))
            except OSError:
                continue
            for path in candidates:
                if not path.is_file() or path == self._session_path:
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue


_GLOBAL_DIAGNOSTICS = CrashDiagnostics(APP_PATHS.user_data_root / "logs")


def get_crash_diagnostics() -> CrashDiagnostics:
    return _GLOBAL_DIAGNOSTICS


__all__ = [
    "CrashDiagnostics",
    "PipelineRunLogger",
    "DEFAULT_HEARTBEAT_SECONDS",
    "get_crash_diagnostics",
]
