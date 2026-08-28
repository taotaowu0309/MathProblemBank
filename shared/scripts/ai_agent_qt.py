from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QBuffer, QByteArray, QEasingCurve, QIODevice, QObject, QParallelAnimationGroup, QPoint, QPointF, QPropertyAnimation, QRunnable, QRect, QRectF, QSize, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont, QImage, QKeyEvent, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtPdf import QPdfDocument
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QLayout,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_config import (
    AiAgentSettingsStore,
    DEFAULT_MAX_TOOL_ROUNDS,
    PROVIDER_KINDS,
    REASONING_EFFORTS,
    ROUTING_STRATEGIES,
    TEXT_VERBOSITIES,
    ProviderProfile,
)
from shared.scripts.ai_agent_attachments import (
    MAX_ATTACHMENT_COUNT,
    store_files,
    store_image_bytes,
)
from shared.scripts.ai_agent_acceptance import (
    load_acceptance_suite,
    run_offline_acceptance,
    summarize_acceptance_results,
)
from shared.scripts.ai_agent_history import ConversationHistoryStore, ConversationViewStateStore
from shared.scripts.ai_agent_learner_profile import (
    clear_learner_profile,
    import_learner_profile,
    learner_profile_status,
)
from shared.scripts.ai_agent_math import MessageRenderResult, compile_message_svg
from shared.scripts.ai_agent_memory import FEEDBACK_ISSUES, LearningMemoryStore
from shared.scripts.ai_agent_reference_library import ReferenceLibraryStore
from shared.scripts.ai_agent_service import (
    DEFAULT_REASONING_PRESET,
    AgentRunResult,
    AiAgentService,
)
from shared.scripts.ai_agent_reliability import (
    BackgroundTaskStore,
    OperationJournal,
    ReliabilityPolicyStore,
    UsageLedger,
)


_ACCOUNT_USAGE_COMPAT: Any | None = None
if not APP_PATHS.public_release:
    try:
        from shared.scripts import ai_agent_account_usage as _ACCOUNT_USAGE_COMPAT
    except ImportError:
        _ACCOUNT_USAGE_COMPAT = None


AUTH_LABELS = {
    "Bearer Token": "bearer",
    "api-key Header": "api-key",
    "无需认证": "none",
}
AUTH_BY_VALUE = {value: label for label, value in AUTH_LABELS.items()}
ROOT_DIR = APP_PATHS.application_root
CACHE_DIR = APP_PATHS.cache_dir
MESSAGE_CONTENT_WIDTH_SCALE = 1.20
MESSAGE_HORIZONTAL_CHROME = 84
MIN_CHAT_COLUMN_WIDTH = 520
MIN_MESSAGE_CONTENT_WIDTH = 436
HISTORY_OVERLAY_BACKGROUND_ALPHA = 204
HISTORY_OVERLAY_ANIMATION_MS = 240


def expanded_chat_column_width(
    body_width: int,
    sidebar_width: int,
) -> int:
    """Keep the expanded message width stable while history floats above it."""

    available = max(1, int(body_width))
    sidebar = max(0, int(sidebar_width))
    legacy_column = max(MIN_CHAT_COLUMN_WIDTH, available - sidebar)
    legacy_content = max(MIN_MESSAGE_CONTENT_WIDTH, legacy_column - MESSAGE_HORIZONTAL_CHROME)
    expanded_content = round(legacy_content * MESSAGE_CONTENT_WIDTH_SCALE)
    desired_column = expanded_content + MESSAGE_HORIZONTAL_CHROME
    return max(MIN_CHAT_COLUMN_WIDTH, min(available, desired_column))


def _reveal_exported_txt_files(paths: list[Path]) -> None:
    resolved = [Path(path).resolve() for path in paths if Path(path).is_file()]
    if not resolved:
        raise FileNotFoundError("没有找到刚刚导出的 TXT 文件。")
    if os.name == "nt":
        # SHOpenFolderAndSelectItems supports selecting several child items in a
        # single Explorer window, unlike repeated `explorer /select` calls.
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.OleDLL("ole32")
        shell32.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        shell32.SHParseDisplayName.restype = ctypes.c_long
        shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
        shell32.ILFindLastID.restype = ctypes.c_void_p
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

        allocated: list[ctypes.c_void_p] = []
        folder_pidl = ctypes.c_void_p()
        try:
            result = shell32.SHParseDisplayName(str(resolved[0].parent), None, ctypes.byref(folder_pidl), 0, None)
            if result != 0 or not folder_pidl.value:
                raise OSError(f"Windows 无法定位导出目录（HRESULT 0x{result & 0xFFFFFFFF:08X}）。")
            allocated.append(folder_pidl)
            children = (ctypes.c_void_p * len(resolved))()
            for index, path in enumerate(resolved):
                item_pidl = ctypes.c_void_p()
                result = shell32.SHParseDisplayName(str(path), None, ctypes.byref(item_pidl), 0, None)
                if result != 0 or not item_pidl.value:
                    raise OSError(f"Windows 无法定位导出文件：{path.name}")
                allocated.append(item_pidl)
                children[index] = shell32.ILFindLastID(item_pidl)
            result = shell32.SHOpenFolderAndSelectItems(folder_pidl, len(resolved), children, 0)
            if result != 0:
                raise OSError(f"Windows 无法在资源管理器中选中文件（HRESULT 0x{result & 0xFFFFFFFF:08X}）。")
            return
        finally:
            for pidl in allocated:
                ole32.CoTaskMemFree(pidl)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(resolved[0])])
    else:
        subprocess.Popen(["xdg-open", str(resolved[0].parent)])


class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    canceled = Signal()


class _TaskCanceled(RuntimeError):
    pass


class _ProgressReporter:
    def __init__(self, signals: _WorkerSignals, cancel_event: threading.Event) -> None:
        self.signals = signals
        self.cancel_event = cancel_event
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        if self.cancel_event.is_set():
            raise _TaskCanceled()
        try:
            self.signals.progress.emit(str(message))
        except RuntimeError:
            raise _TaskCanceled()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def register_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self.cancel_event.is_set():
                callback()
                return lambda: None
            self._callbacks.append(callback)

        def unregister() -> None:
            with self._lock:
                try:
                    self._callbacks.remove(callback)
                except ValueError:
                    pass

        return unregister

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass


class _Worker(QRunnable):
    def __init__(self, task: Callable[[Callable[[str], None]], Any]) -> None:
        super().__init__()
        self.task = task
        self.signals = _WorkerSignals()
        self._cancel_event = threading.Event()
        self.reporter = _ProgressReporter(self.signals, self._cancel_event)

    def cancel(self) -> None:
        self.reporter.cancel()

    def _safe_emit(self, signal: Signal, *arguments: Any) -> None:
        try:
            signal.emit(*arguments)
        except RuntimeError:
            # The owning panel can close while a local XeLaTeX or HTTP step is
            # still unwinding.  Qt has already deleted the receiver in that
            # case, so there is nothing useful left to notify.
            return

    @Slot()
    def run(self) -> None:
        try:
            result = self.task(self.reporter)
            if self._cancel_event.is_set():
                self._safe_emit(self.signals.canceled)
            else:
                self._safe_emit(self.signals.finished, result)
        except _TaskCanceled:
            self._safe_emit(self.signals.canceled)
        except Exception as error:
            if self._cancel_event.is_set():
                self._safe_emit(self.signals.canceled)
            else:
                self._safe_emit(self.signals.failed, str(error))


class _MutationApprovalBroker(QObject):
    requested = Signal(object)

    def request(self, preview: dict[str, Any]) -> bool:
        event = threading.Event()
        payload = {"preview": dict(preview), "approved": False, "event": event}
        self.requested.emit(payload)
        event.wait()
        return bool(payload.get("approved"))


class WrappingFlowLayout(QLayout):
    """Lay out compact action buttons without increasing the container width."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[Any] = []
        self._spacing = max(0, int(spacing))
        self.setContentsMargins(0, 2, 0, 0)

    def addItem(self, item: Any) -> None:  # noqa: N802 - Qt virtual method
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Any | None:  # noqa: N802 - Qt virtual method
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Any | None:  # noqa: N802 - Qt virtual method
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802 - Qt virtual method
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt virtual method
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt virtual method
        return self._arrange(QRect(0, 0, max(0, width), 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt virtual method
        super().setGeometry(rect)
        self._arrange(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt virtual method
        width = max((item.minimumSize().width() for item in self._items), default=0)
        height = max((item.minimumSize().height() for item in self._items), default=0)
        left, top, right, bottom = self.getContentsMargins()
        return QSize(width + left + right, height + top + bottom)

    def _arrange(self, rect: QRect, *, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        usable = rect.adjusted(left, top, -right, -bottom)
        x = usable.x()
        y = usable.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            item_width = min(hint.width(), max(1, usable.width()))
            next_x = x + item_width
            if line_height and next_x > usable.right() + 1:
                x = usable.x()
                y += line_height + self._spacing
                next_x = x + item_width
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(x, y, item_width, hint.height()))
            x = next_x + self._spacing
            line_height = max(line_height, hint.height())
        return max(0, y + line_height + bottom - rect.y())


_ATTACHMENT_TYPE_STYLES: dict[str, tuple[str, str, str]] = {
    ".pdf": ("PDF", "PDF 文档", "#d93025"),
    ".doc": ("W", "Word 文档", "#2b579a"),
    ".docx": ("W", "Word 文档", "#2b579a"),
    ".odt": ("W", "文字文档", "#2b579a"),
    ".xls": ("XLS", "Excel 表格", "#217346"),
    ".xlsx": ("XLS", "Excel 表格", "#217346"),
    ".ods": ("XLS", "电子表格", "#217346"),
    ".csv": ("CSV", "数据表格", "#217346"),
    ".tsv": ("TSV", "数据表格", "#217346"),
    ".ppt": ("PPT", "PowerPoint", "#d24726"),
    ".pptx": ("PPT", "PowerPoint", "#d24726"),
    ".odp": ("PPT", "演示文稿", "#d24726"),
    ".zip": ("ZIP", "压缩文件", "#7b5aa6"),
    ".7z": ("7Z", "压缩文件", "#7b5aa6"),
    ".rar": ("RAR", "压缩文件", "#7b5aa6"),
    ".txt": ("TXT", "文本文件", "#64748b"),
    ".md": ("MD", "Markdown", "#64748b"),
}
_CODE_SUFFIXES = {
    ".bib", ".c", ".cc", ".cls", ".cpp", ".cs", ".css", ".go", ".h", ".hpp",
    ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".lua", ".m",
    ".php", ".ps1", ".py", ".r", ".rb", ".rs", ".sh", ".sql", ".sty", ".swift",
    ".tex", ".toml", ".ts", ".tsx", ".xml", ".yaml", ".yml",
}


def _format_attachment_size(raw_size: Any) -> str:
    try:
        size = max(0, int(raw_size or 0))
    except (TypeError, ValueError):
        size = 0
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _attachment_type_style(attachment: dict[str, Any]) -> tuple[str, str, str]:
    path = Path(str(attachment.get("path") or attachment.get("name") or ""))
    suffix = path.suffix.casefold()
    if str(attachment.get("kind") or "") == "image":
        return "IMG", "图片", "#7c3aed"
    if suffix in _ATTACHMENT_TYPE_STYLES:
        return _ATTACHMENT_TYPE_STYLES[suffix]
    if suffix in _CODE_SUFFIXES:
        label = "LaTeX 源码" if suffix in {".tex", ".bib", ".sty", ".cls"} else f"{suffix[1:].upper()} 代码"
        return "</>", label, "#5f6368"
    extension = suffix[1:].upper()
    return (extension[:4] or "FILE", f"{extension or '通用'} 文件", "#667085")


class AttachmentCard(QFrame):
    """Chat-style attachment card shared by the composer and message history."""

    def __init__(
        self,
        attachment: dict[str, Any],
        *,
        removable: bool = False,
        remove_callback: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.attachment = dict(attachment)
        self.path = Path(str(attachment.get("path") or ""))
        self.setObjectName("aiAttachmentCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor if self.path.is_file() else Qt.CursorShape.ArrowCursor)
        self.setFixedSize(236, 78)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 7, 7)
        layout.setSpacing(9)

        if str(attachment.get("kind") or "") == "image" and self.path.is_file():
            visual = QLabel()
            visual.setObjectName("aiAttachmentThumbnail")
            visual.setAlignment(Qt.AlignmentFlag.AlignCenter)
            visual.setFixedSize(64, 64)
            pixmap = QPixmap(str(self.path))
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    visual.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = max(0, (scaled.width() - visual.width()) // 2)
                y = max(0, (scaled.height() - visual.height()) // 2)
                visual.setPixmap(scaled.copy(x, y, visual.width(), visual.height()))
            layout.addWidget(visual)
        else:
            badge_text, _type_name, badge_color = _attachment_type_style(attachment)
            visual = QLabel(badge_text)
            visual.setObjectName("aiAttachmentTypeBadge")
            visual.setAlignment(Qt.AlignmentFlag.AlignCenter)
            visual.setFixedSize(48, 52)
            visual.setStyleSheet(
                f"background: {badge_color}; color: white; border: 0; border-radius: 8px; font-weight: 700;"
            )
            _set_font(visual, 8, QFont.Weight.Bold)
            layout.addWidget(visual)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 2, 0, 2)
        text_column.setSpacing(2)
        name_text = str(attachment.get("name") or self.path.name or "附件")
        self.name_label = QLabel()
        self.name_label.setObjectName("aiAttachmentName")
        _set_font(self.name_label, 9, QFont.Weight.DemiBold)
        self.name_label.setText(self.name_label.fontMetrics().elidedText(name_text, Qt.TextElideMode.ElideMiddle, 130))
        self.name_label.setMaximumWidth(132)
        badge_text, type_name, _badge_color = _attachment_type_style(attachment)
        detail_text = type_name
        size_text = _format_attachment_size(attachment.get("size"))
        if size_text != "0 B":
            detail_text += f" · {size_text}"
        detail = QLabel(detail_text)
        detail.setObjectName("aiAttachmentDetail")
        _set_font(detail, 8)
        text_column.addStretch(1)
        text_column.addWidget(self.name_label)
        text_column.addWidget(detail)
        text_column.addStretch(1)
        layout.addLayout(text_column, 1)

        if removable:
            remove = QPushButton("×")
            remove.setObjectName("aiAttachmentRemove")
            remove.setFixedSize(22, 22)
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            if remove_callback is not None:
                remove.clicked.connect(remove_callback)
            layout.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton and self.path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.resolve())))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class OperationPreviewDialog(QDialog):
    def __init__(self, preview: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(str(preview.get("title") or "确认 AI 操作"))
        self.resize(900, 700)
        layout = QVBoxLayout(self)
        title = QLabel(str(preview.get("title") or "确认 AI 操作"))
        _set_font(title, 14, QFont.Weight.DemiBold)
        layout.addWidget(title)
        note = QLabel("以下是模型准备执行的实际操作。只有点击“确认执行”后才会写文件或生成 PDF。")
        note.setWordWrap(True)
        layout.addWidget(note)
        targets = QTextEdit()
        targets.setReadOnly(True)
        targets.setMaximumHeight(110)
        targets.setPlainText("\n".join(str(item) for item in preview.get("targets") or []) or "未提供目标路径")
        layout.addWidget(QLabel("目标"))
        layout.addWidget(targets)
        diff = QTextEdit()
        diff.setReadOnly(True)
        diff.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        diff.setFont(QFont("Cascadia Mono", 9))
        diff.setPlainText(str(preview.get("diff") or json.dumps(preview.get("arguments") or {}, ensure_ascii=False, indent=2)))
        layout.addWidget(QLabel("变更预览"))
        layout.addWidget(diff, 1)
        row = QHBoxLayout()
        reject = QPushButton("拒绝")
        approve = QPushButton("确认执行")
        reject.clicked.connect(self.reject)
        approve.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(reject)
        row.addWidget(approve)
        layout.addLayout(row)


def _set_font(widget: QWidget, size: int, weight: QFont.Weight = QFont.Weight.Normal) -> None:
    font = QFont("Microsoft YaHei UI")
    font.setPointSize(size)
    font.setWeight(weight)
    widget.setFont(font)


class ChatInputTextEdit(QTextEdit):
    send_requested = Signal()
    attachments_added = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._height_limit_provider: Callable[[], int] | None = None
        self.document().contentsChanged.connect(self.update_content_height)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def set_height_limit_provider(self, provider: Callable[[], int]) -> None:
        self._height_limit_provider = provider
        self.update_content_height()

    def update_content_height(self) -> None:
        limit = max(90, int(self._height_limit_provider() if self._height_limit_provider else 260))
        document_height = int(self.document().documentLayout().documentSize().height())
        target = max(60, min(limit, document_height + 18))
        self.setFixedHeight(target)
        policy = (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if document_height + 18 > limit
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(policy)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        QTimer.singleShot(0, self.update_content_height)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            event.accept()
            self.send_requested.emit()
            return
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not (
            event.modifiers()
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
        ):
            before = self.textCursor()
            before_state = (before.position(), before.anchor())
            super().keyPressEvent(event)
            after = self.textCursor()
            if (after.position(), after.anchor()) != before_state:
                return

            operation = (
                QTextCursor.MoveOperation.Start
                if event.key() == Qt.Key.Key_Up
                else QTextCursor.MoveOperation.End
            )
            mode = (
                QTextCursor.MoveMode.KeepAnchor
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                else QTextCursor.MoveMode.MoveAnchor
            )
            after.movePosition(operation, mode)
            self.setTextCursor(after)
            event.accept()
            return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, source) -> bool:  # type: ignore[no-untyped-def]
        if source.hasUrls() or source.hasImage():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # type: ignore[no-untyped-def]
        local_paths = [url.toLocalFile() for url in source.urls() if url.isLocalFile()]
        if local_paths:
            self.attachments_added.emit(local_paths)
            return
        if source.hasImage():
            image = source.imageData()
            if not isinstance(image, QImage):
                image = QApplication.clipboard().image()
            if not image.isNull():
                data = QByteArray()
                buffer = QBuffer(data)
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                image.save(buffer, "PNG")
                buffer.close()
                try:
                    attachment = store_image_bytes(bytes(data), name="粘贴的图片.png")
                except (OSError, ValueError) as error:
                    QMessageBox.warning(self, "无法粘贴图片", str(error))
                    return
                self.attachments_added.emit([attachment])
                return
        super().insertFromMimeData(source)


class SendArrowButton(QPushButton):
    """Circular send button with a DPI-aware, optically centered arrow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setAccessibleName("发送消息")
        self.setToolTip("发送消息")

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        unit = min(self.width(), self.height()) / 34.0
        center_x = self.width() / 2.0
        top = self.height() / 2.0 - 10.0 * unit
        bottom = self.height() / 2.0 + 10.0 * unit
        head_span = 6.5 * unit
        pen = QPen(Qt.GlobalColor.white, 2.5 * unit)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(center_x, bottom), QPointF(center_x, top))
        painter.drawLine(QPointF(center_x, top), QPointF(center_x - head_span, top + head_span))
        painter.drawLine(QPointF(center_x, top), QPointF(center_x + head_span, top + head_span))


class LatexMessageView(QWidget):
    """A responsive SVG surface used inside a chat bubble."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.renderer = QSvgRenderer(self)
        self._natural_size = QSize(720, 130)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_svg(self, svg_path: str | Path) -> bool:
        loaded = self.renderer.load(str(svg_path))
        if not loaded:
            return False
        size = self.renderer.defaultSize()
        if size.width() > 0 and size.height() > 0:
            scale = 1.4
            self._natural_size = QSize(max(1, int(size.width() * scale)), max(1, int(size.height() * scale)))
            self.setMaximumWidth(self._natural_size.width())
        self.updateGeometry()
        self.update()
        return True

    def is_valid(self) -> bool:
        return self.renderer.isValid()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        scale = min(1.0, max(1, width) / max(1, self._natural_size.width()))
        return max(24, min(160000, int(self._natural_size.height() * scale + 2)))

    def sizeHint(self):  # type: ignore[no-untyped-def]
        return QSize(self._natural_size)

    def paintEvent(self, _event) -> None:  # type: ignore[no-untyped-def]
        if not self.renderer.isValid():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.renderer.render(painter, QRectF(self.rect()))


class SelectableMessageSource(QTextEdit):
    """Read-only source layer used when the user wants exact copyable text."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aiMessageSource")
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setPlainText(text)
        _set_font(self, 11)
        QTimer.singleShot(0, self.update_content_height)

    def set_source_text(self, text: str) -> None:
        if self.toPlainText() != text:
            self.setPlainText(text)
        QTimer.singleShot(0, self.update_content_height)

    def update_content_height(self) -> None:
        width = max(1, self.viewport().width())
        self.document().setTextWidth(width)
        height = int(self.document().documentLayout().documentSize().height()) + 12
        self.setFixedHeight(max(34, min(160000, height)))

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        QTimer.singleShot(0, self.update_content_height)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        QTimer.singleShot(0, self.update_content_height)

def split_collapsible_code(text: str) -> tuple[str, list[dict[str, str]]]:
    blocks: list[dict[str, str]] = []

    def code_language(line: str) -> str:
        stripped = line.strip()
        if re.match(
            r"^\\(?:documentclass|usepackage|begin|end|addplot|addlegendentry|draw|node|path|"
            r"pgfplotsset|usetikzlibrary|setmainfont|setCJKmainfont|definecolor|caption|label)\b",
            stripped,
        ):
            return "latex"
        if re.match(r"^(?:from\s+\S+\s+import|import\s+\S+|def\s+\w+|class\s+\w+|async\s+def|"
                    r"if\s+.+:|elif\s+.+:|else:|for\s+.+:|while\s+.+:|try:|except\b|return\b)", stripped):
            return "python"
        if re.match(r"^(?:const|let|var|function|export|import)\b|^(?:async\s+)?\w+\s*\([^)]*\)\s*=>", stripped):
            return "javascript"
        if re.match(r"^(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH)\b", stripped, re.IGNORECASE):
            return "sql"
        if re.match(r"^(?:git|pip|python|python3|xelatex|latexmk|npm|pnpm|curl|wget|docker)\s+", stripped):
            return "shell"
        if re.match(r'^"[^"\n]+"\s*:', stripped):
            return "json"
        if re.match(r"^[A-Za-z_][\w.-]*\s*[:=]\s*[^=]", stripped):
            return "code"
        return ""

    def extract_unfenced_code(segment: str) -> str:
        lines = segment.splitlines(keepends=True)
        output: list[str] = []
        index = 0
        while index < len(lines):
            language = code_language(lines[index])
            if not language:
                output.append(lines[index])
                index += 1
                continue
            start = index
            languages: list[str] = []
            code_lines = 0
            last_content = index
            while index < len(lines):
                current = lines[index]
                detected = code_language(current)
                stripped = current.strip()
                continuation = bool(
                    not stripped
                    or detected
                    or re.match(r"^[\[\]{}(),;]|^[%#]|^//", stripped)
                    or (len(current) - len(current.lstrip()) >= 2 and not re.search(r"[。！？]$", stripped))
                )
                if not continuation:
                    break
                if detected:
                    languages.append(detected)
                    code_lines += 1
                if stripped:
                    last_content = index
                index += 1
            raw = "".join(lines[start : last_content + 1]).strip("\r\n")
            if code_lines >= 3 and len(raw) >= 80:
                selected = max(set(languages), key=languages.count) if languages else language
                blocks.append({"language": selected, "code": raw})
                block_index = len(blocks) - 1
                output.append(f"\n<!--AI_CODE_BLOCK:{block_index}-->\n")
                index = last_content + 1
            else:
                output.extend(lines[start:index])
        return "".join(output)

    def replace(match: re.Match[str]) -> str:
        language = str(match.group(1) or "code").strip() or "code"
        code = str(match.group(2) or "").rstrip()
        blocks.append({"language": language, "code": code})
        block_index = len(blocks) - 1
        return f"\n\n<!--AI_CODE_BLOCK:{block_index}-->\n\n"

    original = str(text or "")
    display_parts: list[str] = []
    cursor = 0
    for match in re.finditer(r"```([^\n`]*)\n(.*?)```", original, flags=re.DOTALL):
        display_parts.append(extract_unfenced_code(original[cursor : match.start()]))
        display_parts.append(replace(match))
        cursor = match.end()
    display_parts.append(extract_unfenced_code(original[cursor:]))
    display = "".join(display_parts)
    return display.strip(), blocks


@dataclass(slots=True)
class StructuredMessagePart:
    kind: str
    text: str = ""
    language: str = ""


@dataclass(slots=True)
class MessageReference:
    kind: str
    label: str
    target: str
    subject_name: str = ""
    project_ref: str = ""
    line: int = 0


def problem_reference_from_evidence(source: dict[str, Any]) -> MessageReference | None:
    if str(source.get("kind") or "") != "problem":
        return None
    target = str(source.get("target") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2,8}-P\d{4,}", target):
        return None
    subject = str(source.get("location") or "").strip()
    return MessageReference("problem", f"打开题目 {target}", target, subject_name=subject)


def extract_message_references(text: str) -> list[MessageReference]:
    """Extract only references the answer explicitly recommends opening."""
    source = str(text or "")
    references: list[MessageReference] = []
    seen: set[tuple[str, str, int]] = set()

    def add(reference: MessageReference) -> None:
        key = (reference.kind, reference.target.casefold(), int(reference.line or 0))
        if not reference.target or key in seen:
            return
        seen.add(key)
        references.append(reference)

    def has_jump_intent(match: re.Match[str]) -> bool:
        context = source[max(0, match.start() - 120) : min(len(source), match.end() + 60)]
        if re.search(r"不(?:必|需要|用|建议).{0,8}(?:打开|跳转|查看|参见|阅读)", context):
            return False
        return any(
            token in context
            for token in ("打开", "跳转", "查看", "参见", "进一步阅读", "补充阅读", "阅读全文")
        )

    for match in re.finditer(r"https?://[^\s<>\])，。；]+", source, flags=re.IGNORECASE):
        url = match.group(0).rstrip(".,;:!?'")
        add(MessageReference("url", "打开网页", url))

    for match in re.finditer(r"\[跳转题目[：:]\s*([^/\]\n]+?)\s*/\s*([A-Z]{2,8}-P\d{4,})\s*\]", source):
        subject, code = match.group(1).strip(), match.group(2).upper()
        add(MessageReference("problem", f"打开题目 {code}", code, subject_name=subject))
    for match in re.finditer(r"\[本地题目[：:]\s*([^/\]\n]+?)\s*/\s*([A-Z]{2,8}-P\d{4,})\s*\]", source):
        if has_jump_intent(match):
            subject, code = match.group(1).strip(), match.group(2).upper()
            add(MessageReference("problem", f"打开题目 {code}", code, subject_name=subject))

    project_pattern = re.compile(
        r"\[跳转文件[：:]\s*([^/\]\n]+?)\s*/\s*([^/\]\n]+?)\s*/\s*([^\]\n]+?)\s*\]"
    )
    for match in project_pattern.finditer(source):
        subject, project, relative = (item.strip() for item in match.groups())
        line_match = re.search(r":(\d+)$", relative)
        line = int(line_match.group(1)) if line_match else 0
        if line_match:
            relative = relative[: line_match.start()]
        add(
            MessageReference(
                "project_file",
                f"打开文件 {Path(relative).name}" + (f":{line}" if line else ""),
                relative,
                subject_name=subject,
                project_ref=project,
                line=line,
            )
        )

    legacy_project_pattern = re.compile(
        r"\[本地文件[：:]\s*([^/\]\n]+?)\s*/\s*([^/\]\n]+?)\s*/\s*([^\]\n]+?)\s*\]"
    )
    for match in legacy_project_pattern.finditer(source):
        if not has_jump_intent(match):
            continue
        subject, project, relative = (item.strip() for item in match.groups())
        line_match = re.search(r":(\d+)$", relative)
        line = int(line_match.group(1)) if line_match else 0
        if line_match:
            relative = relative[: line_match.start()]
        add(MessageReference("project_file", f"打开文件 {Path(relative).name}", relative, subject, project, line))

    path_pattern = re.compile(
        r"(?<![\w])([A-Za-z]:[\\/][^\n\r<>|\"?*]+?\.(?:tex|txt|md|json|bib|sty|cls|pdf|docx|csv|py))(?::(\d+))?",
        flags=re.IGNORECASE,
    )
    for match in path_pattern.finditer(source):
        if not has_jump_intent(match):
            continue
        target = match.group(1).strip().rstrip(".,;，。；")
        line = int(match.group(2) or 0)
        add(
            MessageReference(
                "file",
                f"打开文件 {Path(target).name}" + (f":{line}" if line else ""),
                target,
                line=line,
            )
        )
    return references[:10]


def parse_structured_message(text: str, *, extract_code: bool) -> list[StructuredMessagePart]:
    source = str(text or "")
    if not extract_code:
        return [StructuredMessagePart("text", source)] if source.strip() else []
    display, blocks = split_collapsible_code(source)
    parts: list[StructuredMessagePart] = []
    cursor = 0
    seen: set[int] = set()
    for match in re.finditer(r"<!--AI_CODE_BLOCK:(\d+)-->", display):
        prefix = display[cursor : match.start()]
        if prefix.strip():
            parts.append(StructuredMessagePart("text", prefix.strip()))
        index = int(match.group(1))
        if 0 <= index < len(blocks):
            block = blocks[index]
            parts.append(
                StructuredMessagePart(
                    "code",
                    str(block.get("code") or ""),
                    str(block.get("language") or "code"),
                )
            )
            seen.add(index)
        cursor = match.end()
    suffix = display[cursor:]
    if suffix.strip():
        parts.append(StructuredMessagePart("text", suffix.strip()))
    for index, block in enumerate(blocks):
        if index not in seen:
            parts.append(
                StructuredMessagePart(
                    "code",
                    str(block.get("code") or ""),
                    str(block.get("language") or "code"),
                )
            )
    return parts


class CollapsibleSourcePanel(QFrame):
    def __init__(self, language: str, code: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.code = str(code or "")
        self.setObjectName("aiCollapsedSource")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)
        row = QHBoxLayout()
        self.toggle_button = QPushButton(
            f"展开 {str(language or 'code')} 源代码（{len(self.code.splitlines())} 行）"
        )
        self.toggle_button.setObjectName("aiCollapsedSourceToggle")
        self.copy_button = QPushButton("复制代码")
        self.copy_button.setObjectName("aiCollapsedSourceCopy")
        self.toggle_button.clicked.connect(self._toggle)
        self.copy_button.clicked.connect(self._copy)
        row.addWidget(self.toggle_button)
        row.addStretch(1)
        row.addWidget(self.copy_button)
        layout.addLayout(row)
        self.editor = QTextEdit()
        self.editor.setObjectName("aiCollapsedSourceEditor")
        self.editor.setReadOnly(True)
        self.editor.setAcceptRichText(False)
        self.editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.editor.setPlainText(self.code)
        self.editor.setMaximumHeight(420)
        self.editor.hide()
        layout.addWidget(self.editor)

    def _toggle(self) -> None:
        visible = not self.editor.isVisible()
        self.editor.setVisible(visible)
        self.toggle_button.setText(
            ("收起" if visible else "展开")
            + f"源代码（{len(self.code.splitlines())} 行）"
        )
        self.updateGeometry()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.code)
        self.copy_button.setText("已复制")
        QTimer.singleShot(1200, lambda: self.copy_button.setText("复制代码"))


class InlineFigurePreview(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aiFigurePreview")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self._pdf_path = Path()
        self._image = None
        self._pdf_buffer = QBuffer(self)
        self._document = QPdfDocument(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("生成图预览")
        title.setObjectName("aiFigurePreviewTitle")
        _set_font(title, 9, QFont.Weight.DemiBold)
        self.open_button = QPushButton("打开 PDF")
        self.open_button.setObjectName("aiFigurePreviewOpen")
        self.open_button.clicked.connect(self._open_pdf)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.open_button)
        layout.addLayout(head)
        self.image_label = QLabel()
        self.image_label.setObjectName("aiFigurePreviewImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # QLabel reports the pixmap's native width as its size hint.  Ignoring
        # that horizontal hint keeps a large rendered PDF from widening the
        # chat host before the preview has been laid out and scaled.
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.image_label.setMinimumWidth(0)
        layout.addWidget(self.image_label)

    def clear_preview(self) -> None:
        self._document.close()
        self._pdf_buffer.close()
        self._pdf_path = Path()
        self._image = None
        self.image_label.clear()
        self.hide()

    def set_preview(self, pdf_path: str, visual_validation: dict[str, Any] | None = None) -> bool:
        target = Path(str(pdf_path or ""))
        if not target.is_file() or target.suffix.casefold() != ".pdf":
            self.clear_preview()
            return False
        self._document.close()
        self._pdf_buffer.close()
        try:
            pdf_bytes = target.read_bytes()
        except OSError:
            self.clear_preview()
            return False
        if len(pdf_bytes) > 50 * 1024 * 1024:
            self.clear_preview()
            return False
        self._pdf_buffer.setData(QByteArray(pdf_bytes))
        self._pdf_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._document.load(self._pdf_buffer)
        if self._document.error() != QPdfDocument.Error.None_ or self._document.pageCount() < 1:
            self.clear_preview()
            return False
        page_size = self._document.pagePointSize(0)
        render_width = 1224
        render_height = max(1, round(render_width * page_size.height() / max(1.0, page_size.width())))
        image = self._document.render(0, QSize(render_width, render_height))
        if image.isNull():
            self.clear_preview()
            return False
        validation = dict(visual_validation or {})
        bbox = validation.get("content_bbox")
        source_width = int(validation.get("width") or 0)
        source_height = int(validation.get("height") or 0)
        if (
            isinstance(bbox, list)
            and len(bbox) == 4
            and source_width > 0
            and source_height > 0
        ):
            sx = image.width() / source_width
            sy = image.height() / source_height
            padding = 22
            left = max(0, round(float(bbox[0]) * sx) - padding)
            top = max(0, round(float(bbox[1]) * sy) - padding)
            right = min(image.width(), round(float(bbox[2]) * sx) + padding)
            bottom = min(image.height(), round(float(bbox[3]) * sy) + padding)
            crop = QRect(left, top, max(1, right - left), max(1, bottom - top)).intersected(image.rect())
            if crop.width() > 20 and crop.height() > 20:
                image = image.copy(crop)
        self._pdf_path = target.resolve()
        self._image = image
        self.show()
        # MessageBubble inserts this widget into its layout after set_preview
        # returns.  Defer scaling so self.width() is the real bubble width.
        QTimer.singleShot(0, self._update_pixmap)
        return True

    def _update_pixmap(self) -> None:
        if self._image is None or self._image.isNull():
            return
        available = self.contentsRect().width() - 24
        if available <= 0:
            return
        target_width = max(1, min(int(available), self._image.width()))
        pixmap = QPixmap.fromImage(self._image).scaledToWidth(
            target_width,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(pixmap)
        self.image_label.setMinimumHeight(pixmap.height())

    def _open_pdf(self) -> None:
        if self._pdf_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._pdf_path)))

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_pixmap)


class MessageTextSegment(QWidget):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = str(text or "")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.raw = QLabel(self.text)
        self.raw.setObjectName("aiMessageRaw")
        self.raw.setWordWrap(True)
        self.raw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.raw.setMinimumWidth(0)
        self.raw.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        _set_font(self.raw, 11)
        self.svg = LatexMessageView()
        self.svg.hide()
        layout.addWidget(self.raw)
        layout.addWidget(self.svg)

    def set_svg_result(self, result: MessageRenderResult) -> bool:
        if not self.svg.set_svg(result.svg_path):
            self.show_raw()
            return False
        self.raw.hide()
        self.svg.show()
        return True

    def show_raw(self) -> None:
        self.svg.hide()
        self.raw.show()


class MessageBubble(QFrame):
    feedback_requested = Signal(str)
    reference_requested = Signal(object)
    rewrite_requested = Signal()
    regenerate_requested = Signal()

    def __init__(self, role: str, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self.message_text = text
        self.display_text = text
        self.source_panels: list[CollapsibleSourcePanel] = []
        self.text_segments: list[MessageTextSegment] = []
        self.pending_render_segments = 0
        self.failed_render_segments = 0
        self.render_generation = 0
        self.render_width_px = 0
        self.render_requested_width_px = 0
        self.copy_mode = False
        self.setObjectName("aiMessageBubble")
        self.setProperty("role", role)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

        layout = QVBoxLayout(self)
        self.body_layout = layout
        layout.setContentsMargins(18, 14, 18, 13)
        layout.setSpacing(7)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.author = QLabel("AI" if role == "assistant" else "你")
        self.author.setObjectName("aiMessageAuthor")
        _set_font(self.author, 9, QFont.Weight.DemiBold)
        self.status = QLabel("")
        self.status.setObjectName("aiMessageStatus")
        _set_font(self.status, 8)
        self.copy_mode_button = QPushButton("选择复制")
        self.copy_mode_button.setObjectName("aiMessageCopyMode")
        self.copy_mode_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_mode_button.setMinimumHeight(24)
        self.copy_mode_button.clicked.connect(lambda: self.set_copy_mode(not self.copy_mode))
        self.copy_mode_button.hide()
        self.copy_source_button = QPushButton("一键复制")
        self.copy_source_button.setObjectName("aiMessageCopyMode")
        self.copy_source_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_source_button.setMinimumHeight(24)
        self.copy_source_button.clicked.connect(self._copy_source_text)
        self.copy_source_button.hide()
        self.helpful_button = QPushButton("有帮助")
        self.helpful_button.setObjectName("aiMessageFeedback")
        self.helpful_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.helpful_button.setMinimumHeight(24)
        self.helpful_button.clicked.connect(lambda: self.feedback_requested.emit("helpful"))
        self.helpful_button.hide()
        self.improve_button = QPushButton("没帮助")
        self.improve_button.setObjectName("aiMessageFeedback")
        self.improve_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.improve_button.setMinimumHeight(24)
        self.improve_button.clicked.connect(lambda: self.feedback_requested.emit("improve"))
        self.improve_button.hide()
        self.rewrite_button = QPushButton("局部改写")
        self.rewrite_button.setObjectName("aiMessageFeedback")
        self.rewrite_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rewrite_button.setMinimumHeight(24)
        self.rewrite_button.clicked.connect(self.rewrite_requested.emit)
        self.rewrite_button.hide()
        self.regenerate_button = QPushButton("重新生成回答")
        self.regenerate_button.setObjectName("aiMessageFeedback")
        self.regenerate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.regenerate_button.setMinimumHeight(24)
        self.regenerate_button.clicked.connect(self.regenerate_requested.emit)
        self.regenerate_button.hide()
        self.inline_references: list[MessageReference] = []
        head.addWidget(self.author)
        head.addStretch(1)
        head.addWidget(self.status)
        head.addWidget(self.regenerate_button)
        head.addWidget(self.rewrite_button)
        head.addWidget(self.helpful_button)
        head.addWidget(self.improve_button)
        head.addWidget(self.copy_source_button)
        head.addWidget(self.copy_mode_button)
        layout.addLayout(head)

        self.source = SelectableMessageSource(text)
        self.source.hide()
        self.figure_preview = InlineFigurePreview()
        self.figure_preview.hide()
        self.attachments_host = QWidget()
        self.attachments_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.attachments_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.attachments_layout = WrappingFlowLayout(self.attachments_host, spacing=8)
        self.attachments_layout.setContentsMargins(0, 0, 0, 2)
        self.attachments_host.hide()
        self.segment_host = QWidget()
        self.segment_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.segment_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.segment_host.setMinimumWidth(0)
        self.segment_layout = QVBoxLayout(self.segment_host)
        self.segment_layout.setContentsMargins(0, 0, 0, 0)
        self.segment_layout.setSpacing(8)
        self.reference_host = QWidget()
        self.reference_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.reference_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.reference_layout = WrappingFlowLayout(self.reference_host, spacing=6)
        self.reference_host.hide()
        layout.addWidget(self.attachments_host)
        layout.addWidget(self.segment_host)
        layout.addWidget(self.reference_host)
        layout.addWidget(self.source)
        self.raw = QLabel()
        self.svg = LatexMessageView()
        if text:
            self.set_text_for_compilation(text)
        else:
            self.segment_host.hide()

    def set_copy_mode(self, enabled: bool) -> None:
        if not self.message_text:
            return
        self.copy_mode = bool(enabled)
        if self.copy_mode:
            self.source.set_source_text(self.message_text)
            self.segment_host.hide()
            self.source.show()
            self.copy_mode_button.setText("返回排版")
            self.copy_source_button.setText("一键复制")
            self.copy_source_button.show()
            self.status.setText("复制模式 · 公式保留 LaTeX")
            self.status.show()
            self.source.setFocus(Qt.FocusReason.MouseFocusReason)
        else:
            self.source.hide()
            self.copy_mode_button.setText("选择复制")
            self.copy_source_button.hide()
            self.segment_host.show()
            if self.pending_render_segments <= 0:
                self.status.hide()
        self.updateGeometry()

    def _copy_source_text(self) -> None:
        cursor = self.source.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n").strip()
        copied = selected or self.message_text
        if not copied:
            return
        QApplication.clipboard().setText(copied)
        self.copy_source_button.setText("已复制所选" if selected else "已复制全文")
        QTimer.singleShot(
            1200,
            lambda: self.copy_source_button.setText("一键复制") if self.copy_mode else None,
        )

    def set_waiting(self, text: str) -> None:
        self.copy_mode = False
        self.segment_host.hide()
        self.source.hide()
        self.copy_mode_button.hide()
        self.copy_source_button.hide()
        self.helpful_button.hide()
        self.improve_button.hide()
        self.rewrite_button.hide()
        self.regenerate_button.hide()
        self.figure_preview.clear_preview()
        self._clear_structured_parts()
        self.status.setText(text)
        self.status.show()

    def set_text_for_compilation(self, text: str) -> None:
        self.message_text = text
        parts = parse_structured_message(text, extract_code=self.role == "assistant")
        self.display_text = "\n\n".join(part.text for part in parts if part.kind == "text")
        self.copy_mode = False
        self._clear_structured_parts()
        for part in parts:
            if part.kind == "code":
                panel = CollapsibleSourcePanel(part.language or "code", part.text)
                self.segment_layout.addWidget(panel)
                self.source_panels.append(panel)
                continue
            segment = MessageTextSegment(part.text)
            self.segment_layout.addWidget(segment)
            self.text_segments.append(segment)
        if self.text_segments:
            self.raw = self.text_segments[0].raw
            self.svg = self.text_segments[0].svg
        self.segment_host.show()
        self.source.set_source_text(text)
        self.source.hide()
        self.copy_mode_button.hide()
        self.copy_source_button.hide()
        self.inline_references = extract_message_references(text) if self.role == "assistant" else []
        self._refresh_references()
        self.status.setText("正在排版…")
        self.status.show()
        self.pending_render_segments = len(self.text_segments)
        self.failed_render_segments = 0
        if not self.text_segments:
            self.copy_mode_button.show()
            self.status.hide()

    def _clear_structured_parts(self) -> None:
        while self.segment_layout.count():
            item = self.segment_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                if widget is self.figure_preview:
                    self.figure_preview.hide()
                    self.figure_preview.setParent(self)
                else:
                    widget.deleteLater()
        self.source_panels.clear()
        self.text_segments.clear()
        self.pending_render_segments = 0
        self.failed_render_segments = 0

    def _set_references(self, references: list[MessageReference]) -> None:
        while self.reference_layout.count():
            item = self.reference_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for reference in references:
            button = QPushButton(reference.label)
            button.setObjectName("aiMessageReference")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.clicked.connect(
                lambda _checked=False, current=reference: self.reference_requested.emit(current)
            )
            self.reference_layout.addWidget(button)
        self.reference_host.setVisible(bool(references))

    def _refresh_references(self) -> None:
        merged: list[MessageReference] = []
        seen: set[tuple[str, str, int]] = set()
        for reference in self.inline_references:
            key = (reference.kind, reference.target.casefold(), int(reference.line or 0))
            if key in seen:
                continue
            seen.add(key)
            merged.append(reference)
        self._set_references(merged[:10])

    def set_figure_preview(
        self,
        pdf_path: str,
        visual_validation: dict[str, Any] | None = None,
    ) -> bool:
        if not self.figure_preview.set_preview(pdf_path, visual_validation):
            return False
        if self.segment_layout.indexOf(self.figure_preview) < 0:
            insertion_index = self.segment_layout.count()
            for index in range(self.segment_layout.count() - 1, -1, -1):
                if isinstance(self.segment_layout.itemAt(index).widget(), CollapsibleSourcePanel):
                    insertion_index = index + 1
                    break
            self.segment_layout.insertWidget(insertion_index, self.figure_preview)
        self.figure_preview.updateGeometry()
        QTimer.singleShot(0, self.figure_preview._update_pixmap)
        return True

    def set_svg_result(
        self,
        result: MessageRenderResult,
        segment: MessageTextSegment | None = None,
    ) -> None:
        target = segment or (self.text_segments[0] if self.text_segments else None)
        if target is None or not target.set_svg_result(result):
            self.failed_render_segments += 1
        self.copy_mode_button.show()
        if self.copy_mode:
            self.segment_host.hide()
            self.source.show()
        self.updateGeometry()

    def finish_segment_render(self, *, failed: bool = False, message: str = "") -> None:
        if failed:
            self.failed_render_segments += 1
        self.pending_render_segments = max(0, self.pending_render_segments - 1)
        if self.pending_render_segments > 0:
            self.status.setText(f"正在排版…还剩 {self.pending_render_segments} 段")
            self.status.show()
            return
        self.copy_mode_button.show()
        if self.failed_render_segments:
            self.status.setText("部分段落排版失败，已保留原文")
            self.status.show()
        else:
            self.status.hide()

    def set_render_error(self, message: str) -> None:
        for segment in self.text_segments:
            segment.show_raw()
        self.segment_host.show()
        self.source.hide()
        self.copy_mode_button.hide()
        self.copy_source_button.hide()
        self.status.setText("LaTeX 排版失败，已保留原文")
        self.status.show()

    def set_request_error(self, message: str) -> None:
        self.set_text_for_compilation("这次 API 请求没有成功。\n" + message)
        for segment in self.text_segments:
            segment.show_raw()
        self.segment_host.show()
        self.source.hide()
        self.copy_mode_button.hide()
        self.copy_source_button.hide()
        self.helpful_button.hide()
        self.improve_button.hide()
        self.status.setText("请求失败")
        self.status.show()

    def set_feedback_available(self, rating: str = "") -> None:
        if self.role != "assistant" or not self.message_text.strip():
            return
        self.helpful_button.show()
        self.improve_button.show()
        self.helpful_button.setText("已标记有帮助" if rating == "helpful" else "有帮助")
        self.improve_button.setText("已标记没帮助" if rating == "improve" else "没帮助")
        self.helpful_button.setProperty("selected", rating == "helpful")
        self.improve_button.setProperty("selected", rating == "improve")
        for button in (self.helpful_button, self.improve_button):
            button.style().unpolish(button)
            button.style().polish(button)

    def set_rewrite_available(self, available: bool = True) -> None:
        self.rewrite_button.setVisible(self.role == "assistant" and bool(available) and bool(self.message_text.strip()))

    def set_regenerate_available(self, available: bool = True) -> None:
        self.regenerate_button.setVisible(
            self.role == "user" and bool(available) and bool(self.message_text.strip())
        )

    def set_attachments(self, attachments: list[dict[str, Any]]) -> None:
        while self.attachments_layout.count():
            item = self.attachments_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        valid = [dict(item) for item in attachments if isinstance(item, dict)]
        for attachment in valid:
            self.attachments_layout.addWidget(AttachmentCard(attachment))
        self.attachments_host.setVisible(bool(valid))


class AnswerFeedbackDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("改进这次回答")
        self.setModal(True)
        self.resize(520, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = QLabel("这次回答主要哪里不合适？")
        _set_font(title, 12, QFont.Weight.DemiBold)
        layout.addWidget(title)
        hint = QLabel("反馈只保存在本机；以后遇到相关问题时，助手会优先遵守这些改进要求。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.issue_checks: dict[str, QCheckBox] = {}
        for issue, label in FEEDBACK_ISSUES.items():
            check = QCheckBox(label)
            self.issue_checks[issue] = check
            layout.addWidget(check)
        layout.addWidget(QLabel("补充说明（可选）"))
        self.note_edit = QTextEdit()
        self.note_edit.setPlaceholderText("例如：请严格沿着原证明讲清第三段，不要改用同伦或覆盖空间。")
        self.note_edit.setMaximumHeight(120)
        layout.addWidget(self.note_edit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        save = QPushButton("保存反馈")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._accept_feedback)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _accept_feedback(self) -> None:
        if not self.selected_issues() and not self.note_edit.toPlainText().strip():
            QMessageBox.information(self, "还没有填写", "请选择至少一项，或写下具体的改进要求。")
            return
        self.accept()

    def selected_issues(self) -> list[str]:
        return [issue for issue, check in self.issue_checks.items() if check.isChecked()]


class LocalRewriteDialog(QDialog):
    def __init__(self, excerpt: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("局部改写")
        self.resize(620, 460)
        layout = QVBoxLayout(self)
        title = QLabel("只处理选中的这一段；原回答会保留不变")
        _set_font(title, 12, QFont.Weight.DemiBold)
        layout.addWidget(title)
        preview = QTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText(excerpt)
        preview.setMaximumHeight(150)
        layout.addWidget(preview)
        layout.addWidget(QLabel("希望怎样处理？"))
        self.action = QComboBox()
        self.action.addItems(("讲得更细，补足连接步骤", "用更基础的语言解释", "简化并删除无关内容", "严格沿当前教材重写", "检查并纠正数学错误"))
        layout.addWidget(self.action)
        self.note = QTextEdit()
        self.note.setPlaceholderText("可选：指出你具体没看懂的句子，或说明希望保留哪些公式。")
        self.note.setMaximumHeight(110)
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        submit = QPushButton("生成局部版本")
        cancel.clicked.connect(self.reject)
        submit.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(submit)
        layout.addLayout(buttons)

    def instruction(self) -> str:
        note = self.note.toPlainText().strip()
        return self.action.currentText() + (("；补充要求：" + note) if note else "")


class ReferenceLibraryDialog(QDialog):
    def __init__(
        self,
        store: ReferenceLibraryStore,
        parent: QWidget | None = None,
        *,
        discipline: str = "math",
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.discipline = "physics" if str(discipline).casefold() == "physics" else "math"
        subject_label = "物理" if self.discipline == "physics" else "数学"
        self.rebuild_requested = False
        self.setWindowTitle(f"{subject_label}资料库")
        self.resize(780, 520)
        layout = QVBoxLayout(self)
        title = QLabel(f"本地{subject_label}资料库")
        _set_font(title, 14, QFont.Weight.DemiBold)
        layout.addWidget(title)
        note = QLabel(
            "这里的 Markdown、PDF、Word 和文本资料只在本机建立索引。回答前先免费检索，"
            "只把少量高相关片段发送给 API；当前题库、项目和绑定教材始终优先。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.root_list = QListWidget()
        self.root_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.root_list.itemChanged.connect(self._toggle_root)
        layout.addWidget(self.root_list, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        add = QPushButton("添加目录")
        remove = QPushButton("移除所选")
        rebuild = QPushButton("保存并重建索引")
        close = QPushButton("关闭")
        add.clicked.connect(self._add_root)
        remove.clicked.connect(self._remove_root)
        rebuild.clicked.connect(self._request_rebuild)
        close.clicked.connect(self.accept)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        row.addWidget(rebuild)
        row.addWidget(close)
        layout.addLayout(row)
        self._refresh()

    def _refresh(self) -> None:
        self.root_list.blockSignals(True)
        self.root_list.clear()
        for root in self.store.roots:
            item = QListWidgetItem(f"{root.name}\n{root.path}")
            item.setData(Qt.ItemDataRole.UserRole, root.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if root.enabled else Qt.CheckState.Unchecked)
            self.root_list.addItem(item)
        self.root_list.blockSignals(False)
        status = self.store.status()
        suffixes = "，".join(
            f"{suffix or '无扩展名'} {count}"
            for suffix, count in sorted((status.get("suffix_counts") or {}).items())
        )
        self.status_label.setText(
            f"已启用 {status['enabled_root_count']} 个目录，可检索 {status['file_count']} 个文件"
            + (f"（{suffixes}）" if suffixes else "")
            + "。目录变化会在下一次数学检索前自动更新索引。"
        )

    def _toggle_root(self, item: QListWidgetItem) -> None:
        self.store.set_enabled(
            str(item.data(Qt.ItemDataRole.UserRole) or ""),
            item.checkState() == Qt.CheckState.Checked,
        )
        self._refresh()

    def _add_root(self) -> None:
        subject_label = "物理" if self.discipline == "physics" else "数学"
        path = QFileDialog.getExistingDirectory(self, f"选择{subject_label}资料目录")
        if not path:
            return
        try:
            self.store.add(path)
        except ValueError as error:
            QMessageBox.warning(self, "无法添加资料目录", str(error))
            return
        self._refresh()

    def _remove_root(self) -> None:
        item = self.root_list.currentItem()
        if item is None:
            return
        if QMessageBox.question(self, "移除资料目录", "只移除索引配置，不会删除磁盘上的任何文件。继续吗？") != QMessageBox.StandardButton.Yes:
            return
        self.store.remove(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        self._refresh()

    def _request_rebuild(self) -> None:
        self.store.save()
        self.rebuild_requested = True
        self.accept()


class LearningMemoryDialog(QDialog):
    def __init__(
        self,
        service: AiAgentService,
        parent: QWidget | None = None,
        *,
        discipline: str = "math",
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.store = service.memory_store
        self.discipline = "physics" if str(discipline).casefold() == "physics" else "math"
        self.setWindowTitle("物理学习记忆" if self.discipline == "physics" else "数学学习记忆")
        self.setModal(True)
        self.resize(820, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        tabs = QTabWidget()
        tabs.addTab(self._profile_tab(), "长期学习画像")
        tabs.addTab(self._explicit_memory_tab(), "明确记忆")
        tabs.addTab(self._signals_tab(), "概念与学习状态")
        tabs.addTab(self._feedback_tab(), "回答反馈")
        if self.discipline == "math":
            tabs.addTab(self._quality_pairs_tab(), "数学质量样例")
            tabs.addTab(self._acceptance_tab(), "模型验收")
        layout.addWidget(tabs, 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        row = QHBoxLayout()
        if self.discipline == "math":
            acceptance_button = QPushButton("运行本地系统验收")
            acceptance_button.clicked.connect(self._run_acceptance)
            row.addWidget(acceptance_button)
        row.addStretch(1)
        row.addWidget(close_button)
        layout.addLayout(row)
        self._refresh_explicit_memories()
        self._refresh_signals()
        self._refresh_feedback()
        if self.discipline == "math":
            self._refresh_quality_pairs()
            self._refresh_acceptance_results()

    def _run_acceptance(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            report = run_offline_acceptance(self.service.repository)
        except Exception as error:
            QMessageBox.critical(self, "验收失败", str(error))
            return
        finally:
            QApplication.restoreOverrideCursor()
        lines = [
            f"本地验收：{report['passed_count']} / {report['case_count']} 项通过",
            "付费 API 调用：0",
            "",
        ]
        lines.extend(
            ("通过  " if case.get("passed") else "失败  ") + str(case.get("id") or "")
            for case in report.get("cases", [])
        )
        lines.append("\n数学回答的最终质量仍需使用系统验收套件中的真实问题进行模型评分。")
        QMessageBox.information(self, "系统验收结果", "\n".join(lines))

    def _profile_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(
            f"{('物理' if self.discipline == 'physics' else '数学')}学习画像默认留空，"
            "仅在用户显式导入后用于后续回答。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.profile_status_label = QLabel()
        self.profile_status_label.setWordWrap(True)
        layout.addWidget(self.profile_status_label)
        self.profile_edit = QTextEdit()
        self.profile_edit.setReadOnly(True)
        layout.addWidget(self.profile_edit, 1)
        import_button = QPushButton("导入学习画像")
        clear_button = QPushButton("清空学习画像")
        import_button.clicked.connect(self._import_profile)
        clear_button.clicked.connect(self._clear_profile)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(clear_button)
        row.addWidget(import_button)
        layout.addLayout(row)
        self._refresh_profile()
        return page

    def _explicit_memory_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        self.explicit_memory_list = QListWidget()
        self.explicit_memory_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.explicit_memory_list.currentItemChanged.connect(self._explicit_memory_selected)
        layout.addWidget(self.explicit_memory_list, 2)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        hint = QLabel(
            "这里保存你明确要求 AI 长期记住的偏好和背景。聊天中使用“请记住：……”也会自动加入。"
        )
        hint.setWordWrap(True)
        editor_layout.addWidget(hint)
        self.explicit_memory_edit = QTextEdit()
        self.explicit_memory_edit.setPlaceholderText(
            "例如：场论推导要先声明度规号差，并始终检查量纲。"
            if self.discipline == "physics"
            else "例如：数学证明要沿教材原有顺序解释，不要拆成很多小标题。"
        )
        editor_layout.addWidget(self.explicit_memory_edit, 1)
        button_row = QHBoxLayout()
        new_button = QPushButton("新建")
        save_button = QPushButton("保存")
        delete_button = QPushButton("删除")
        new_button.clicked.connect(self._new_explicit_memory)
        save_button.clicked.connect(self._save_explicit_memory)
        delete_button.clicked.connect(self._delete_explicit_memory)
        button_row.addWidget(new_button)
        button_row.addWidget(delete_button)
        button_row.addStretch(1)
        button_row.addWidget(save_button)
        editor_layout.addLayout(button_row)
        layout.addWidget(editor, 3)
        return page

    def _signals_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        self.signal_list = QListWidget()
        self.signal_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.signal_list.currentItemChanged.connect(self._signal_selected)
        layout.addWidget(self.signal_list, 2)
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.addWidget(QLabel(f"AI 应记住的{('物理' if self.discipline == 'physics' else '数学')}学习状态"))
        self.signal_state = QComboBox()
        self.signal_state.addItem("需要补足或重新解释", "needs_explanation")
        self.signal_state.addItem("已经掌握", "understood")
        editor_layout.addWidget(self.signal_state)
        self.signal_edit = QTextEdit()
        self.signal_edit.setPlaceholderText(
            "例如：我还不熟悉规范固定对自由度计数的作用。"
            if self.discipline == "physics"
            else "例如：我还不熟悉商拓扑中饱和开集的作用。"
        )
        editor_layout.addWidget(self.signal_edit, 1)
        button_row = QHBoxLayout()
        new_button = QPushButton("新建")
        save_button = QPushButton("保存")
        delete_button = QPushButton("删除")
        new_button.clicked.connect(self._new_signal)
        save_button.clicked.connect(self._save_signal)
        delete_button.clicked.connect(self._delete_signal)
        button_row.addWidget(new_button)
        button_row.addWidget(delete_button)
        button_row.addStretch(1)
        button_row.addWidget(save_button)
        editor_layout.addLayout(button_row)
        layout.addWidget(editor, 3)
        return page

    def _feedback_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        self.feedback_list = QListWidget()
        self.feedback_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.feedback_list.currentItemChanged.connect(self._feedback_selected)
        layout.addWidget(self.feedback_list, 2)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.addWidget(QLabel("本地保存的评价与认可样例"))
        self.feedback_detail = QTextEdit()
        self.feedback_detail.setReadOnly(True)
        detail_layout.addWidget(self.feedback_detail, 1)
        delete_button = QPushButton("删除所选反馈")
        delete_button.clicked.connect(self._delete_feedback)
        detail_layout.addWidget(delete_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(detail, 3)
        return page

    def _quality_pairs_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        self.quality_pair_list = QListWidget()
        self.quality_pair_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.quality_pair_list.currentItemChanged.connect(self._quality_pair_selected)
        layout.addWidget(self.quality_pair_list, 2)
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        hint = QLabel(
            "每条样例同时保存同一问题的优选回答与反例回答。它们只用于约束讲解风格、深度和资料边界，不会修改模型权重。"
        )
        hint.setWordWrap(True)
        editor_layout.addWidget(hint)
        self.quality_kind = QComboBox()
        for label, value in (
            ("定义与概念", "definition"),
            ("证明解释", "math_explanation"),
            ("相关题检索", "problem_search"),
            ("数学绘图", "drawing_or_visualization"),
            ("项目操作", "project_edit"),
        ):
            self.quality_kind.addItem(label, value)
        editor_layout.addWidget(self.quality_kind)
        self.quality_prompt = QTextEdit()
        self.quality_prompt.setPlaceholderText("测试问题")
        self.quality_prompt.setMaximumHeight(90)
        editor_layout.addWidget(self.quality_prompt)
        self.quality_preferred = QTextEdit()
        self.quality_preferred.setPlaceholderText("你认可的优选回答")
        editor_layout.addWidget(self.quality_preferred, 1)
        self.quality_rejected = QTextEdit()
        self.quality_rejected.setPlaceholderText("失败回答或不希望再出现的写法")
        editor_layout.addWidget(self.quality_rejected, 1)
        self.quality_reason = QTextEdit()
        self.quality_reason.setPlaceholderText("为什么优选前者、反例具体错在哪里")
        self.quality_reason.setMaximumHeight(80)
        editor_layout.addWidget(self.quality_reason)
        buttons = QHBoxLayout()
        new_button = QPushButton("新建")
        save_button = QPushButton("保存成对样例")
        delete_button = QPushButton("删除所选")
        new_button.clicked.connect(self._new_quality_pair)
        save_button.clicked.connect(self._save_quality_pair)
        delete_button.clicked.connect(self._delete_quality_pairs)
        buttons.addWidget(new_button)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        buttons.addWidget(save_button)
        editor_layout.addLayout(buttons)
        layout.addWidget(editor, 3)
        return page

    def _acceptance_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("固定验收问题（复制到正常聊天中发送）"))
        self.acceptance_case_list = QListWidget()
        for case in load_acceptance_suite():
            item = QListWidgetItem(f"[{case.get('category', '')}] {case.get('id', '')}")
            item.setData(Qt.ItemDataRole.UserRole, dict(case))
            self.acceptance_case_list.addItem(item)
        self.acceptance_case_list.currentItemChanged.connect(self._acceptance_case_selected)
        left_layout.addWidget(self.acceptance_case_list, 2)
        self.acceptance_prompt = QTextEdit()
        self.acceptance_prompt.setReadOnly(True)
        left_layout.addWidget(self.acceptance_prompt, 2)
        copy_button = QPushButton("复制所选验收问题")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.acceptance_prompt.toPlainText())
        )
        left_layout.addWidget(copy_button)
        layout.addWidget(left, 2)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("真实模型验收记录"))
        self.acceptance_comparison = QTextEdit()
        self.acceptance_comparison.setReadOnly(True)
        self.acceptance_comparison.setMaximumHeight(150)
        self.acceptance_comparison.setPlaceholderText("同一固定问题运行两次后，这里会比较模型、路由、推理档位、评分、耗时和费用。")
        right_layout.addWidget(self.acceptance_comparison)
        self.acceptance_result_list = QListWidget()
        self.acceptance_result_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.acceptance_result_list.currentItemChanged.connect(self._acceptance_result_selected)
        right_layout.addWidget(self.acceptance_result_list, 2)
        self.acceptance_result_detail = QTextEdit()
        self.acceptance_result_detail.setReadOnly(True)
        right_layout.addWidget(self.acceptance_result_detail, 2)
        review_row = QHBoxLayout()
        self.acceptance_score = QLineEdit()
        self.acceptance_score.setPlaceholderText("0-100")
        self.acceptance_score.setMaximumWidth(90)
        self.acceptance_passed = QComboBox()
        self.acceptance_passed.addItem("通过", True)
        self.acceptance_passed.addItem("未通过", False)
        review_row.addWidget(QLabel("人工分数"))
        review_row.addWidget(self.acceptance_score)
        review_row.addWidget(self.acceptance_passed)
        right_layout.addLayout(review_row)
        self.acceptance_note = QTextEdit()
        self.acceptance_note.setPlaceholderText("记录数学错误、讲解深度或资料忠实度问题")
        self.acceptance_note.setMaximumHeight(80)
        right_layout.addWidget(self.acceptance_note)
        action_row = QHBoxLayout()
        delete_button = QPushButton("删除所选记录")
        save_button = QPushButton("保存人工评分")
        delete_button.clicked.connect(self._delete_acceptance_results)
        save_button.clicked.connect(self._save_acceptance_review)
        action_row.addWidget(delete_button)
        action_row.addStretch(1)
        action_row.addWidget(save_button)
        right_layout.addLayout(action_row)
        layout.addWidget(right, 3)
        return page

    def _refresh_profile(self) -> None:
        try:
            status = learner_profile_status(self.discipline)
        except (OSError, ValueError) as error:
            self.profile_status_label.setText(f"读取失败：{error}")
            self.profile_edit.clear()
            return
        labels = {
            "user": "已导入",
            "empty": "已清空",
            "legacy": "正在使用现有本地画像；重新导入或清空后将由用户配置接管",
            "none": "未导入",
        }
        self.profile_status_label.setText(
            f"状态：{labels.get(str(status['source']), '未导入')}\n"
            f"用户配置路径：{status['user_path']}"
        )
        self.profile_edit.setPlainText(str(status["content"]))

    def _import_profile(self) -> None:
        source, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "导入学习画像",
            "",
            "文本文件 (*.txt *.md)",
        )
        if not source:
            return
        try:
            import_learner_profile(Path(source), self.discipline)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "导入失败", str(error))
            return
        self._refresh_profile()
        QMessageBox.information(self, "已导入", "学习画像已导入，下一条消息开始生效。")

    def _clear_profile(self) -> None:
        if QMessageBox.question(
            self,
            "清空学习画像",
            "确定清空当前学习画像吗？清空后不会再向模型注入画像内容。",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            clear_learner_profile(self.discipline)
        except OSError as error:
            QMessageBox.critical(self, "清空失败", str(error))
            return
        self._refresh_profile()
        QMessageBox.information(self, "已清空", "学习画像已清空。")

    def _refresh_explicit_memories(self) -> None:
        self.store.reload()
        self.explicit_memory_list.clear()
        for memory in self.store.all_explicit_memories():
            item = QListWidgetItem(memory.statement[:100])
            item.setData(Qt.ItemDataRole.UserRole, memory.id)
            item.setToolTip(f"保存时间：{memory.created_at}\n{memory.statement}")
            self.explicit_memory_list.addItem(item)

    def _explicit_memory_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        memory_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        memory = next((item for item in self.store.explicit_memories if item.id == memory_id), None)
        if memory is not None:
            self.explicit_memory_edit.setPlainText(memory.statement)

    def _new_explicit_memory(self) -> None:
        self.explicit_memory_list.clearSelection()
        self.explicit_memory_list.setCurrentItem(None)
        self.explicit_memory_edit.clear()
        self.explicit_memory_edit.setFocus()

    def _save_explicit_memory(self) -> None:
        statement = self.explicit_memory_edit.toPlainText().strip()
        current = self.explicit_memory_list.currentItem()
        try:
            if current is None:
                self.store.add_explicit_memory(statement)
            else:
                self.store.update_explicit_memory(
                    str(current.data(Qt.ItemDataRole.UserRole) or ""), statement
                )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self._new_explicit_memory()
        self._refresh_explicit_memories()

    def _delete_explicit_memory(self) -> None:
        current = self.explicit_memory_list.currentItem()
        if current is None:
            return
        self.store.delete_explicit_memories([str(current.data(Qt.ItemDataRole.UserRole) or "")])
        self._new_explicit_memory()
        self._refresh_explicit_memories()

    def _refresh_signals(self) -> None:
        self.store.reload()
        self.signal_list.clear()
        for signal in self.store.all_learning_signals():
            state = "需补足" if signal.state == "needs_explanation" else "已掌握"
            item = QListWidgetItem(f"[{state}] {signal.statement[:80]}")
            item.setData(Qt.ItemDataRole.UserRole, signal.id)
            self.signal_list.addItem(item)

    def _signal_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        signal_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        signal = next((item for item in self.store.learning_signals if item.id == signal_id), None)
        if signal is None:
            return
        self.signal_edit.setPlainText(signal.statement)
        index = self.signal_state.findData(signal.state)
        self.signal_state.setCurrentIndex(max(0, index))

    def _new_signal(self) -> None:
        self.signal_list.clearSelection()
        self.signal_list.setCurrentItem(None)
        self.signal_edit.clear()
        self.signal_state.setCurrentIndex(0)
        self.signal_edit.setFocus()

    def _save_signal(self) -> None:
        statement = self.signal_edit.toPlainText().strip()
        state = str(self.signal_state.currentData() or "needs_explanation")
        current = self.signal_list.currentItem()
        try:
            if current is None:
                self.store.add_learning_signal(statement, state)
            else:
                self.store.update_learning_signal(
                    str(current.data(Qt.ItemDataRole.UserRole) or ""), statement, state
                )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self._refresh_signals()

    def _delete_signal(self) -> None:
        current = self.signal_list.currentItem()
        if current is None:
            return
        self.store.delete_learning_signals([str(current.data(Qt.ItemDataRole.UserRole) or "")])
        self.signal_edit.clear()
        self._refresh_signals()

    def _refresh_feedback(self) -> None:
        self.store.reload()
        self.feedback_list.clear()
        for record in self.store.all_feedback():
            rating = "有帮助" if record.rating == "helpful" else "需改进"
            item = QListWidgetItem(f"[{rating}] {record.question[:75] or '未命名问题'}")
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            self.feedback_list.addItem(item)

    def _feedback_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.feedback_detail.clear()
            return
        record_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        record = next((item for item in self.store.feedback if item.id == record_id), None)
        if record is None:
            return
        issue_text = "、".join(FEEDBACK_ISSUES.get(item, item) for item in record.issues) or "无"
        self.feedback_detail.setPlainText(
            f"评价：{'有帮助' if record.rating == 'helpful' else '需改进'}\n"
            f"时间：{record.created_at}\n"
            f"问题：{record.question}\n"
            f"原因：{issue_text}\n"
            f"补充说明：{record.note or '无'}\n\n"
            f"认可回答片段：\n{record.answer_excerpt or '未保存'}"
        )

    def _delete_feedback(self) -> None:
        selected = [
            str(item.data(Qt.ItemDataRole.UserRole) or "") for item in self.feedback_list.selectedItems()
        ]
        if not selected:
            return
        self.store.delete_feedback(selected)
        self.feedback_detail.clear()
        self._refresh_feedback()

    def _refresh_quality_pairs(self) -> None:
        self.quality_pair_list.clear()
        for pair in self.service.quality_dataset.all():
            item = QListWidgetItem(f"[{pair.task_kind}] {pair.prompt[:70]}")
            item.setData(Qt.ItemDataRole.UserRole, pair.id)
            self.quality_pair_list.addItem(item)

    def _quality_pair_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        pair_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        pair = next((item for item in self.service.quality_dataset.all() if item.id == pair_id), None)
        if pair is None:
            return
        index = self.quality_kind.findData(pair.task_kind)
        self.quality_kind.setCurrentIndex(max(0, index))
        self.quality_prompt.setPlainText(pair.prompt)
        self.quality_preferred.setPlainText(pair.preferred_answer)
        self.quality_rejected.setPlainText(pair.rejected_answer)
        self.quality_reason.setPlainText(pair.preference_reason)

    def _new_quality_pair(self) -> None:
        self.quality_pair_list.clearSelection()
        self.quality_pair_list.setCurrentItem(None)
        self.quality_prompt.clear()
        self.quality_preferred.clear()
        self.quality_rejected.clear()
        self.quality_reason.clear()

    def _save_quality_pair(self) -> None:
        try:
            current = self.quality_pair_list.currentItem()
            payload = {
                "task_kind": str(self.quality_kind.currentData() or "math_explanation"),
                "prompt": self.quality_prompt.toPlainText(),
                "preferred_answer": self.quality_preferred.toPlainText(),
                "rejected_answer": self.quality_rejected.toPlainText(),
                "preference_reason": self.quality_reason.toPlainText(),
            }
            if current is None:
                self.service.quality_dataset.create(
                    **payload,
                    source="manual_memory_dialog",
                )
            else:
                self.service.quality_dataset.update(
                    str(current.data(Qt.ItemDataRole.UserRole) or ""), **payload
                )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "无法保存", str(error))
            return
        self._new_quality_pair()
        self._refresh_quality_pairs()

    def _delete_quality_pairs(self) -> None:
        selected = [
            str(item.data(Qt.ItemDataRole.UserRole) or "")
            for item in self.quality_pair_list.selectedItems()
        ]
        if not selected:
            return
        self.service.quality_dataset.delete(selected)
        self._new_quality_pair()
        self._refresh_quality_pairs()

    def _acceptance_case_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        case = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        if not isinstance(case, dict):
            self.acceptance_prompt.clear()
            return
        self.acceptance_prompt.setPlainText(str(case.get("prompt") or ""))

    def _refresh_acceptance_results(self) -> None:
        records = self.service.acceptance_store.all()
        self.acceptance_result_list.clear()
        for record in reversed(records):
            status = str(record.get("manual_status") or "pending_review")
            item = QListWidgetItem(
                f"[{status}] {record.get('category', '')} / {record.get('profile_name', '')} / {record.get('created_at', '')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, str(record.get("id") or ""))
            self.acceptance_result_list.addItem(item)
        lines: list[str] = []
        for summary in summarize_acceptance_results(records):
            score = "待评分" if summary["average_score"] is None else f"{summary['average_score']:.1f} 分"
            pass_rate = (
                "待评分"
                if summary["pass_rate"] is None
                else f"{float(summary['pass_rate']) * 100:.0f}% 通过"
            )
            lines.append(
                f"{summary['case_id']} · {summary['profile_name']} · "
                f"{summary['route']} / {summary['reasoning_effort'] or '-'} · "
                f"{summary['run_count']} 次 · {score} · {pass_rate} · "
                f"平均 {summary['average_elapsed_seconds']:.1f} 秒 / "
                f"{summary['average_total_tokens']:,} tokens / "
                f"¥{summary['average_estimated_cost']:.4f}"
            )
        self.acceptance_comparison.setPlainText("\n".join(lines) if lines else "尚无真实模型验收记录。")

    def _acceptance_result_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self.acceptance_result_detail.clear()
            return
        record_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        record = next(
            (item for item in self.service.acceptance_store.all() if item.get("id") == record_id),
            None,
        )
        if record is None:
            return
        self.acceptance_result_detail.setPlainText(
            f"案例：{record.get('case_id', '')}\n"
            f"模型：{record.get('profile_name', '')}\n"
            f"路由：{record.get('route', '')}\n"
            f"状态：{record.get('manual_status', '')}\n"
            f"运行指标：{json.dumps(record.get('run_metrics') or {}, ensure_ascii=False)}\n\n"
            f"问题：\n{record.get('prompt', '')}\n\n"
            f"回答：\n{record.get('answer', '')}"
        )
        score = record.get("manual_score")
        self.acceptance_score.setText("" if score is None else str(score))
        index = self.acceptance_passed.findData(record.get("manual_status") == "passed")
        self.acceptance_passed.setCurrentIndex(max(0, index))
        self.acceptance_note.setPlainText(str(record.get("manual_note") or ""))

    def _save_acceptance_review(self) -> None:
        current = self.acceptance_result_list.currentItem()
        if current is None:
            return
        try:
            score = int(self.acceptance_score.text().strip())
            self.service.acceptance_store.update_manual_review(
                str(current.data(Qt.ItemDataRole.UserRole) or ""),
                score=score,
                passed=bool(self.acceptance_passed.currentData()),
                note=self.acceptance_note.toPlainText(),
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "无法保存验收评分", str(error))
            return
        self._refresh_acceptance_results()

    def _delete_acceptance_results(self) -> None:
        selected = [
            str(item.data(Qt.ItemDataRole.UserRole) or "")
            for item in self.acceptance_result_list.selectedItems()
        ]
        if not selected:
            return
        self.service.acceptance_store.delete(selected)
        self.acceptance_result_detail.clear()
        self._refresh_acceptance_results()


class TieredPricingDialog(QDialog):
    """Edit provider pricing that changes at a context-length threshold."""

    def __init__(self, values: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("分档计费设置")
        self.resize(560, 620)
        layout = QVBoxLayout(self)
        note = QLabel(
            "价格单位均为每 100 万 tokens。短上下文价格仍在主配置窗口中编辑；"
            "这里补充缓存写入价格和长上下文档位。阈值为 0 时禁用自动分档。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.edits: dict[str, QLineEdit] = {}
        labels = {
            "pricing_plan_name": "计费方案",
            "cached_write_price_per_million": "短上下文缓存写入",
            "long_context_threshold_tokens": "长上下文阈值（tokens）",
            "long_input_price_per_million": "长上下文输入",
            "long_cached_input_price_per_million": "长上下文缓存读取",
            "long_cached_write_price_per_million": "长上下文缓存写入",
            "long_output_price_per_million": "长上下文输出",
        }
        for key, label in labels.items():
            edit = QLineEdit(str(values.get(key, "")))
            self.edits[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        save = QPushButton("采用")
        save.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def values(self) -> dict[str, Any]:
        return {
            "pricing_plan_name": self.edits["pricing_plan_name"].text().strip(),
            "cached_write_price_per_million": float(
                self.edits["cached_write_price_per_million"].text().strip() or 0
            ),
            "long_context_threshold_tokens": int(
                self.edits["long_context_threshold_tokens"].text().strip() or 0
            ),
            "long_input_price_per_million": float(
                self.edits["long_input_price_per_million"].text().strip() or 0
            ),
            "long_cached_input_price_per_million": float(
                self.edits["long_cached_input_price_per_million"].text().strip() or 0
            ),
            "long_cached_write_price_per_million": float(
                self.edits["long_cached_write_price_per_million"].text().strip() or 0
            ),
            "long_output_price_per_million": float(
                self.edits["long_output_price_per_million"].text().strip() or 0
            ),
        }

    def accept(self) -> None:
        try:
            values = self.values()
            if values["long_context_threshold_tokens"] < 0 or any(
                float(values[key]) < 0
                for key in values
                if key not in {"pricing_plan_name", "long_context_threshold_tokens"}
            ):
                raise ValueError("价格和上下文阈值不能为负数。")
        except ValueError as error:
            QMessageBox.critical(self, "分档计费设置无效", str(error))
            return
        super().accept()


class ModelSettingsDialog(QDialog):
    profiles_changed = Signal(str)

    def __init__(
        self,
        settings_store: AiAgentSettingsStore,
        service: AiAgentService,
        thread_pool: QThreadPool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings_store = settings_store
        self.service = service
        self.thread_pool = thread_pool
        self._workers: set[_Worker] = set()
        self.setWindowTitle("模型与 API")
        self.setModal(True)
        self.resize(720, 840)
        self._build_ui()
        self._refresh_profile_combo()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background: #f7faff; color: #18212f; }
            QLabel { color: #26364a; }
            QLineEdit, QComboBox { background: white; border: 1px solid #cbd8e8; border-radius: 8px; padding: 7px 9px; }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #75aaf0; }
            QPushButton { background: white; border: 1px solid #b9cbe2; border-radius: 8px; padding: 7px 14px; }
            QPushButton:hover { background: #eef6ff; border-color: #7caef0; }
            QPushButton#primary { background: #1976d2; color: white; border-color: #1976d2; }
            QPushButton#danger { color: #b42318; }
            QLabel#dialogStatus { color: #66809f; }
            """
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 20)
        outer.setSpacing(13)
        title = QLabel("模型与 API")
        _set_font(title, 18, QFont.Weight.DemiBold)
        note = QLabel("这里的设置只控制内置数学顾问。API Key 使用 Windows DPAPI 加密，不进入 Git。")
        note.setWordWrap(True)
        _set_font(note, 9)
        outer.addWidget(title)
        outer.addWidget(note)

        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumHeight(38)
        self.profile_combo.currentIndexChanged.connect(self._profile_selected)
        outer.addWidget(self.profile_combo)

        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.profile_name_edit = QLineEdit()
        self.provider_kind_combo = QComboBox()
        self.provider_kind_combo.addItems(list(PROVIDER_KINDS.values()))
        self.reasoning_combo = QComboBox()
        self.reasoning_combo.addItems(list(REASONING_EFFORTS.values()))
        self.text_verbosity_combo = QComboBox()
        self.text_verbosity_combo.addItems(list(TEXT_VERBOSITIES.values()))
        self.routing_combo = QComboBox()
        self.routing_combo.addItems(list(ROUTING_STRATEGIES.values()))
        self.base_url_edit = QLineEdit()
        self.model_edit = QComboBox()
        self.model_edit.setEditable(True)
        self.model_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.fetch_models_button = QPushButton("获取模型列表")
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_env_edit = QLineEdit()
        self.auth_combo = QComboBox()
        self.auth_combo.addItems(list(AUTH_LABELS))
        self.max_tokens_edit = QLineEdit("6000")
        self.max_rounds_edit = QLineEdit(str(DEFAULT_MAX_TOOL_ROUNDS))
        self.timeout_edit = QLineEdit("180")
        self.input_price_edit = QLineEdit("0")
        self.cached_price_edit = QLineEdit("0")
        self.output_price_edit = QLineEdit("0")
        self.currency_edit = QLineEdit("CNY")
        self.tiered_pricing_values: dict[str, Any] = {
            "pricing_plan_name": "",
            "cached_write_price_per_million": 0.0,
            "long_context_threshold_tokens": 0,
            "long_input_price_per_million": 0.0,
            "long_cached_input_price_per_million": 0.0,
            "long_cached_write_price_per_million": 0.0,
            "long_output_price_per_million": 0.0,
        }
        self.tiered_pricing_button = QPushButton("分档计费设置")
        self.tiered_pricing_button.clicked.connect(self.open_tiered_pricing)
        self.tiered_pricing_summary = QLabel("未启用长上下文分档")
        self.tiered_pricing_summary.setWordWrap(True)
        self.api_key_edit.setPlaceholderText("留空则使用已保存密钥或环境变量")
        self.base_url_edit.setPlaceholderText("例如 https://api.example.com/v1")
        if self.model_edit.lineEdit() is not None:
            self.model_edit.lineEdit().setPlaceholderText("供应商提供的准确模型名称")
        for widget in (
            self.profile_name_edit,
            self.provider_kind_combo,
            self.reasoning_combo,
            self.text_verbosity_combo,
            self.routing_combo,
            self.base_url_edit,
            self.model_edit,
            self.api_key_edit,
            self.api_key_env_edit,
            self.auth_combo,
            self.max_tokens_edit,
            self.max_rounds_edit,
            self.timeout_edit,
            self.input_price_edit,
            self.cached_price_edit,
            self.output_price_edit,
            self.currency_edit,
        ):
            widget.setMinimumHeight(36)
        form.addRow("配置名称", self.profile_name_edit)
        form.addRow("API 协议", self.provider_kind_combo)
        form.addRow("推理强度", self.reasoning_combo)
        form.addRow("回答详略", self.text_verbosity_combo)
        form.addRow("请求路由", self.routing_combo)
        form.addRow("Base URL", self.base_url_edit)
        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(8)
        model_layout.addWidget(self.model_edit, 1)
        model_layout.addWidget(self.fetch_models_button)
        form.addRow("模型", model_row)
        form.addRow("API Key", self.api_key_edit)
        form.addRow("Key 环境变量", self.api_key_env_edit)
        form.addRow("认证", self.auth_combo)
        form.addRow("最大输出", self.max_tokens_edit)
        form.addRow("工具轮数", self.max_rounds_edit)
        form.addRow("超时（秒）", self.timeout_edit)
        form.addRow("输入单价/百万 token", self.input_price_edit)
        form.addRow("缓存输入单价/百万", self.cached_price_edit)
        form.addRow("输出单价/百万 token", self.output_price_edit)
        form.addRow("价格币种", self.currency_edit)
        pricing_row = QWidget()
        pricing_layout = QHBoxLayout(pricing_row)
        pricing_layout.setContentsMargins(0, 0, 0, 0)
        pricing_layout.setSpacing(8)
        pricing_layout.addWidget(self.tiered_pricing_button)
        pricing_layout.addWidget(self.tiered_pricing_summary, 1)
        form.addRow("动态价格", pricing_row)
        outer.addLayout(form)

        self.tools_checkbox = QCheckBox("允许模型调用题库、联网和受控 TeX 工具")
        self.tools_checkbox.setChecked(True)
        self.tools_checkbox.setToolTip("默认只读；仅在你明确要求时允许修改学习项目内的 TeX，并自动备份和编译回滚。")
        self.reasoning_combo.setToolTip(
            "推荐使用“自动按任务”：数学解释、推导和证明默认 high，极难长证明才升级到 xhigh。"
        )
        self.text_verbosity_combo.setToolTip("自动模式下数学回答使用 high，其他任务使用 medium。")
        self.routing_combo.setToolTip("质量优先会先调用 Responses；只有中转站明确不兼容且尚未执行任何本地工具时才降级。")
        self.max_rounds_edit.setToolTip("复杂的读取、TeX 写入和编译任务需要更多轮次；默认 24，允许范围 1–64。")
        outer.addWidget(self.tools_checkbox)

        row = QHBoxLayout()
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("primary")
        self.copy_button = QPushButton("复制新建")
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("danger")
        self.test_button = QPushButton("测试连接")
        self.clear_key_button = QPushButton("清除已保存 Key")
        for button in (self.save_button, self.copy_button, self.delete_button, self.test_button, self.clear_key_button):
            button.setMinimumHeight(36)
        self.save_button.clicked.connect(self.save_current_profile)
        self.copy_button.clicked.connect(self.copy_profile)
        self.delete_button.clicked.connect(self.delete_current_profile)
        self.test_button.clicked.connect(self.test_connection)
        self.fetch_models_button.clicked.connect(self.fetch_models)
        self.clear_key_button.clicked.connect(self.clear_saved_key)
        row.addWidget(self.save_button)
        row.addWidget(self.copy_button)
        row.addWidget(self.delete_button)
        row.addStretch(1)
        outer.addLayout(row)
        key_row = QHBoxLayout()
        key_row.addWidget(self.test_button)
        key_row.addWidget(self.clear_key_button)
        key_row.addStretch(1)
        outer.addLayout(key_row)
        self.status_label = QLabel("")
        self.status_label.setObjectName("dialogStatus")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        outer.addStretch(1)

    def _refresh_tiered_pricing_summary(self) -> None:
        threshold = int(self.tiered_pricing_values.get("long_context_threshold_tokens") or 0)
        plan = str(self.tiered_pricing_values.get("pricing_plan_name") or "").strip()
        if threshold <= 0:
            text = "未启用长上下文分档"
        else:
            text = f"{plan + '；' if plan else ''}>{threshold:,} tokens 自动使用长上下文价格"
        self.tiered_pricing_summary.setText(text)

    def open_tiered_pricing(self) -> None:
        dialog = TieredPricingDialog(self.tiered_pricing_values, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.tiered_pricing_values = dialog.values()
        self._refresh_tiered_pricing_summary()

    def _refresh_profile_combo(self, selected_id: str = "") -> None:
        target = selected_id or self.settings_store.active_profile_id
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.settings_store.profiles:
            self.profile_combo.addItem(profile.name, profile.id)
        index = self.profile_combo.findData(target)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.blockSignals(False)
        self._profile_selected(self.profile_combo.currentIndex())

    def _profile_selected(self, index: int) -> None:
        profile = self.settings_store.profile(str(self.profile_combo.itemData(index) or ""))
        if profile is None:
            return
        self.settings_store.set_active(profile.id)
        self.profile_name_edit.setText(profile.name)
        self.provider_kind_combo.setCurrentText(PROVIDER_KINDS.get(profile.provider_kind, profile.provider_kind))
        self.reasoning_combo.setCurrentText(
            REASONING_EFFORTS.get(profile.reasoning_effort, profile.reasoning_effort)
        )
        self.text_verbosity_combo.setCurrentText(
            TEXT_VERBOSITIES.get(profile.text_verbosity, profile.text_verbosity)
        )
        self.routing_combo.setCurrentText(
            ROUTING_STRATEGIES.get(profile.routing_strategy, profile.routing_strategy)
        )
        self.base_url_edit.setText(profile.base_url)
        self.model_edit.setEditText(profile.model)
        self.api_key_edit.clear()
        self.api_key_env_edit.setText(profile.api_key_env)
        self.auth_combo.setCurrentText(AUTH_BY_VALUE.get(profile.auth_mode, "Bearer Token"))
        self.tools_checkbox.setChecked(profile.supports_tools)
        self.max_tokens_edit.setText(str(profile.max_output_tokens))
        self.max_rounds_edit.setText(str(profile.max_tool_rounds))
        self.timeout_edit.setText(str(profile.timeout_seconds))
        self.input_price_edit.setText(str(profile.input_price_per_million))
        self.cached_price_edit.setText(str(profile.cached_input_price_per_million))
        self.output_price_edit.setText(str(profile.output_price_per_million))
        self.currency_edit.setText(str(profile.price_currency or "CNY"))
        self.tiered_pricing_values = {
            "pricing_plan_name": str(profile.pricing_plan_name or ""),
            "cached_write_price_per_million": float(profile.cached_write_price_per_million or 0),
            "long_context_threshold_tokens": int(profile.long_context_threshold_tokens or 0),
            "long_input_price_per_million": float(profile.long_input_price_per_million or 0),
            "long_cached_input_price_per_million": float(
                profile.long_cached_input_price_per_million or 0
            ),
            "long_cached_write_price_per_million": float(
                profile.long_cached_write_price_per_million or 0
            ),
            "long_output_price_per_million": float(profile.long_output_price_per_million or 0),
        }
        self._refresh_tiered_pricing_summary()
        placeholder = "已加密保存；留空保持不变" if self.settings_store.has_saved_api_key(profile.id) else "留空则使用环境变量"
        self.api_key_edit.setPlaceholderText(placeholder)

    def _form_profile(self) -> ProviderProfile:
        profile_id = str(self.profile_combo.currentData() or "") or uuid.uuid4().hex
        provider_label = self.provider_kind_combo.currentText()
        provider_kind = next((kind for kind, label in PROVIDER_KINDS.items() if label == provider_label), "openai_compatible")
        reasoning_label = self.reasoning_combo.currentText()
        reasoning_effort = next(
            (value for value, label in REASONING_EFFORTS.items() if label == reasoning_label),
            "auto",
        )
        verbosity_label = self.text_verbosity_combo.currentText()
        text_verbosity = next(
            (value for value, label in TEXT_VERBOSITIES.items() if label == verbosity_label),
            "auto",
        )
        routing_label = self.routing_combo.currentText()
        routing_strategy = next(
            (value for value, label in ROUTING_STRATEGIES.items() if label == routing_label),
            "fixed",
        )
        profile = ProviderProfile(
            id=profile_id,
            name=self.profile_name_edit.text().strip(),
            provider_kind=provider_kind,
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.currentText().strip(),
            api_key_env=self.api_key_env_edit.text().strip(),
            auth_mode=AUTH_LABELS.get(self.auth_combo.currentText(), "bearer"),
            supports_tools=self.tools_checkbox.isChecked(),
            requires_api_key=self.auth_combo.currentText() != "无需认证",
            max_output_tokens=int(self.max_tokens_edit.text().strip()),
            max_tool_rounds=int(self.max_rounds_edit.text().strip()),
            timeout_seconds=int(self.timeout_edit.text().strip()),
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            routing_strategy=routing_strategy,
            input_price_per_million=float(self.input_price_edit.text().strip() or 0),
            cached_input_price_per_million=float(self.cached_price_edit.text().strip() or 0),
            cached_write_price_per_million=float(
                self.tiered_pricing_values.get("cached_write_price_per_million") or 0
            ),
            output_price_per_million=float(self.output_price_edit.text().strip() or 0),
            long_context_threshold_tokens=int(
                self.tiered_pricing_values.get("long_context_threshold_tokens") or 0
            ),
            long_input_price_per_million=float(
                self.tiered_pricing_values.get("long_input_price_per_million") or 0
            ),
            long_cached_input_price_per_million=float(
                self.tiered_pricing_values.get("long_cached_input_price_per_million") or 0
            ),
            long_cached_write_price_per_million=float(
                self.tiered_pricing_values.get("long_cached_write_price_per_million") or 0
            ),
            long_output_price_per_million=float(
                self.tiered_pricing_values.get("long_output_price_per_million") or 0
            ),
            price_currency=self.currency_edit.text().strip() or "CNY",
            pricing_plan_name=str(self.tiered_pricing_values.get("pricing_plan_name") or ""),
        )
        profile.validate(require_model=False)
        return profile

    def save_current_profile(self) -> ProviderProfile | None:
        try:
            profile = self._form_profile()
            self.settings_store.upsert_profile(profile, self.api_key_edit.text().strip() or None)
            self.api_key_edit.clear()
            self._refresh_profile_combo(profile.id)
            self.status_label.setText(f"已保存：{profile.name}")
            self.profiles_changed.emit(profile.id)
            return profile
        except (ValueError, OSError, RuntimeError) as error:
            QMessageBox.critical(self, "模型配置无效", str(error))
            return None

    def copy_profile(self) -> None:
        try:
            current = self._form_profile()
        except (ValueError, TypeError) as error:
            QMessageBox.critical(self, "无法复制配置", str(error))
            return
        copied = replace(current, id=uuid.uuid4().hex, name=(current.name or "模型配置") + " 副本")
        self.settings_store.upsert_profile(copied)
        self._refresh_profile_combo(copied.id)
        self.profiles_changed.emit(copied.id)

    def delete_current_profile(self) -> None:
        profile = self.settings_store.profile(str(self.profile_combo.currentData() or ""))
        if profile is None:
            return
        if QMessageBox.question(self, "删除模型配置", f"确定删除“{profile.name}”吗？") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.settings_store.delete_profile(profile.id)
            self._refresh_profile_combo()
            self.profiles_changed.emit(self.settings_store.active_profile_id)
        except ValueError as error:
            QMessageBox.warning(self, "不能删除", str(error))

    def clear_saved_key(self) -> None:
        self.settings_store.delete_api_key(str(self.profile_combo.currentData() or ""))
        self.api_key_edit.clear()
        self.api_key_edit.setPlaceholderText("留空则使用环境变量")
        self.status_label.setText("已清除该配置中加密保存的 API Key。")

    def _set_testing(self, testing: bool) -> None:
        for widget in (
            self.save_button,
            self.copy_button,
            self.delete_button,
            self.test_button,
            self.fetch_models_button,
            self.clear_key_button,
        ):
            widget.setEnabled(not testing)
        self.test_button.setText("正在连接…" if testing else "测试连接")

    def fetch_models(self) -> None:
        try:
            profile = self._form_profile()
        except (ValueError, TypeError) as error:
            QMessageBox.warning(self, "无法获取模型列表", str(error))
            return
        entered_key = self.api_key_edit.text().strip() or None
        self._set_testing(True)
        self.status_label.setText("正在从 API 获取可用模型列表…")
        worker = _Worker(lambda emit: self.service.list_models(profile, entered_key, emit))
        worker.setAutoDelete(False)
        self._workers.add(worker)
        worker.signals.progress.connect(self.status_label.setText)

        def finished(models: Any) -> None:
            model_names = [str(model).strip() for model in models if str(model).strip()]
            current_model = self.model_edit.currentText().strip()
            self.model_edit.clear()
            self.model_edit.addItems(model_names)
            preferred_model = current_model if current_model in model_names else "gpt-5.6-sol"
            if preferred_model not in model_names and model_names:
                preferred_model = model_names[0]
            self.model_edit.setEditText(preferred_model)
            self._set_testing(False)
            self.status_label.setText(f"已获取 {len(model_names)} 个可用模型；请选择后保存配置。")
            self._workers.discard(worker)

        def failed(message: str) -> None:
            self._set_testing(False)
            self.status_label.setText("获取模型列表失败：" + message)
            self._workers.discard(worker)
            QMessageBox.critical(self, "获取模型列表失败", message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.thread_pool.start(worker)

    def test_connection(self) -> None:
        profile = self.save_current_profile()
        if profile is None:
            return
        try:
            profile.validate(require_model=True)
        except ValueError as error:
            QMessageBox.warning(self, "无法测试", str(error))
            return
        self._set_testing(True)
        self.status_label.setText("正在连接模型 API…")
        worker = _Worker(lambda emit: self.service.test_profile(profile, None, emit))
        worker.setAutoDelete(False)
        self._workers.add(worker)
        worker.signals.progress.connect(self.status_label.setText)

        def finished(answer: Any) -> None:
            self._set_testing(False)
            self.status_label.setText(f"连接成功，模型返回：{str(answer).strip()}")
            self._workers.discard(worker)
            QMessageBox.information(self, "连接成功", "模型 API 已成功响应。")

        def failed(message: str) -> None:
            self._set_testing(False)
            self.status_label.setText("连接失败：" + message)
            self._workers.discard(worker)
            QMessageBox.critical(self, "API 连接测试失败", message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.thread_pool.start(worker)


class ConversationExportDialog(QDialog):
    def __init__(
        self,
        history_store: ConversationHistoryStore,
        current_conversation_id: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.history_store = history_store
        self.current_conversation_id = str(current_conversation_id or "")
        self.setWindowTitle("导出 AI 对话")
        self.resize(720, 610)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background: #f7faff; color: #111111; }
            QListWidget { background: white; border: 1px solid #c8d7e8; border-radius: 10px; padding: 6px; }
            QListWidget::item { color: #111111; padding: 9px 6px; border-bottom: 1px solid #edf1f6; }
            QListWidget::item:hover { background: #f0f7fb; color: #111111; }
            QListWidget::item:selected { background: #e5f1f4; color: #111111; }
            QPushButton { background: white; border: 1px solid #b9cbe2; border-radius: 8px; padding: 7px 14px; }
            QPushButton:hover { background: #eef6ff; }
            QPushButton#primary { background: #1976d2; color: white; border-color: #1976d2; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = QLabel("导出对话为 TXT")
        _set_font(title, 17, QFont.Weight.DemiBold)
        note = QLabel("每个选中的对话生成一个独立 TXT 文件；你和 AI 的消息及其中的 LaTeX 代码都会按原文导出。")
        note.setWordWrap(True)
        _set_font(note, 9)
        layout.addWidget(title)
        layout.addWidget(note)

        controls = QHBoxLayout()
        self.select_all = QCheckBox("全选")
        current_button = QPushButton("仅选择当前对话")
        clear_button = QPushButton("取消全选")
        self.select_all.toggled.connect(self._toggle_all)
        current_button.clicked.connect(self._select_current)
        clear_button.clicked.connect(lambda: self._set_all(False))
        controls.addWidget(self.select_all)
        controls.addWidget(current_button)
        controls.addWidget(clear_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.list_widget = QListWidget()
        self.list_widget.setWordWrap(True)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for record in self.history_store.all():
            stamp = record.updated_at.replace("T", " ")[:16]
            item = QListWidgetItem(f"{record.title}\n{stamp}  ·  {len(record.messages)} 条消息")
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = record.id == self.current_conversation_id
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, 1)

        actions = QHBoxLayout()
        self.count_label = QLabel("")
        export_button = QPushButton("选择目录并导出")
        export_button.setObjectName("primary")
        close_button = QPushButton("关闭")
        export_button.clicked.connect(self._export)
        close_button.clicked.connect(self.reject)
        self.list_widget.itemPressed.connect(self._item_pressed)
        self.list_widget.itemClicked.connect(self._item_clicked)
        self.list_widget.itemChanged.connect(self._selection_changed)
        actions.addWidget(self.count_label)
        actions.addStretch(1)
        actions.addWidget(close_button)
        actions.addWidget(export_button)
        layout.addLayout(actions)
        self._selection_changed()

    def _item_pressed(self, item: QListWidgetItem) -> None:
        self._pressed_check_state = item.checkState()

    def _item_clicked(self, item: QListWidgetItem) -> None:
        previous = getattr(self, "_pressed_check_state", item.checkState())
        if item.checkState() == previous:
            item.setCheckState(
                Qt.CheckState.Unchecked
                if previous == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )

    def _checked_ids(self) -> list[str]:
        return [
            str(self.list_widget.item(index).data(Qt.ItemDataRole.UserRole) or "")
            for index in range(self.list_widget.count())
            if self.list_widget.item(index).checkState() == Qt.CheckState.Checked
        ]

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self.list_widget.count()):
            self.list_widget.item(index).setCheckState(state)

    def _toggle_all(self, checked: bool) -> None:
        self._set_all(checked)

    def _select_current(self) -> None:
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(Qt.ItemDataRole.UserRole) or "") == self.current_conversation_id
                else Qt.CheckState.Unchecked
            )

    def _selection_changed(self, _item: QListWidgetItem | None = None) -> None:
        selected = len(self._checked_ids())
        self.count_label.setText(f"已选择 {selected} 个对话")
        self.select_all.blockSignals(True)
        self.select_all.setChecked(self.list_widget.count() > 0 and selected == self.list_widget.count())
        self.select_all.blockSignals(False)

    def _export(self) -> None:
        selected = self._checked_ids()
        if not selected:
            QMessageBox.information(self, "尚未选择", "请至少选择一个需要导出的对话。")
            return
        destination = QFileDialog.getExistingDirectory(self, "选择 TXT 导出目录")
        if not destination:
            return
        try:
            paths = self.history_store.export_txt(selected, Path(destination))
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "导出失败", str(error))
            return
        self.accept()
        try:
            _reveal_exported_txt_files(paths)
        except (OSError, FileNotFoundError) as error:
            if os.name == "nt":
                try:
                    subprocess.Popen(["explorer", "/select,", str(paths[0])])
                    return
                except OSError:
                    pass
            QMessageBox.warning(
                self.parentWidget(),
                "TXT 已导出，但无法自动定位",
                f"已导出 {len(paths)} 个文件到：\n{destination}\n\n{error}",
            )


class ConversationDeleteDialog(QDialog):
    def __init__(
        self,
        history_store: ConversationHistoryStore,
        current_conversation_id: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.history_store = history_store
        self.current_conversation_id = str(current_conversation_id or "")
        self.deleted_ids: list[str] = []
        self.setWindowTitle("删除历史对话")
        self.resize(700, 590)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background: #f7faff; color: #111111; }
            QListWidget { background: white; border: 1px solid #c8d7e8; border-radius: 10px; padding: 6px; }
            QListWidget::item { color: #111111; padding: 9px 6px; border-bottom: 1px solid #edf1f6; }
            QListWidget::item:hover { background: #f0f7fb; color: #111111; }
            QListWidget::item:selected { background: #e5f1f4; color: #111111; }
            QPushButton { background: white; border: 1px solid #b9cbe2; border-radius: 8px; padding: 7px 14px; }
            QPushButton:hover { background: #eef6ff; }
            QPushButton#danger { background: #c9362b; color: white; border-color: #c9362b; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = QLabel("删除历史对话")
        _set_font(title, 17, QFont.Weight.DemiBold)
        note = QLabel("可以删除当前、多个或全部对话。删除后无法恢复，导出的 TXT 文件不会受到影响。")
        note.setWordWrap(True)
        _set_font(note, 9)
        layout.addWidget(title)
        layout.addWidget(note)

        controls = QHBoxLayout()
        self.select_all = QCheckBox("全选")
        current_button = QPushButton("仅选择当前对话")
        clear_button = QPushButton("取消全选")
        self.select_all.toggled.connect(self._toggle_all)
        current_button.clicked.connect(self._select_current)
        clear_button.clicked.connect(lambda: self._set_all(False))
        controls.addWidget(self.select_all)
        controls.addWidget(current_button)
        controls.addWidget(clear_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.list_widget = QListWidget()
        self.list_widget.setWordWrap(True)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for record in self.history_store.all():
            stamp = record.updated_at.replace("T", " ")[:16]
            item = QListWidgetItem(f"{record.title}\n{stamp}  ·  {len(record.messages)} 条消息")
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if record.id == self.current_conversation_id else Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, 1)

        actions = QHBoxLayout()
        self.count_label = QLabel("")
        delete_button = QPushButton("永久删除所选对话")
        delete_button.setObjectName("danger")
        close_button = QPushButton("取消")
        delete_button.clicked.connect(self._delete)
        close_button.clicked.connect(self.reject)
        self.list_widget.itemPressed.connect(self._item_pressed)
        self.list_widget.itemClicked.connect(self._item_clicked)
        self.list_widget.itemChanged.connect(self._selection_changed)
        actions.addWidget(self.count_label)
        actions.addStretch(1)
        actions.addWidget(close_button)
        actions.addWidget(delete_button)
        layout.addLayout(actions)
        self._selection_changed()

    def _item_pressed(self, item: QListWidgetItem) -> None:
        self._pressed_check_state = item.checkState()

    def _item_clicked(self, item: QListWidgetItem) -> None:
        previous = getattr(self, "_pressed_check_state", item.checkState())
        if item.checkState() == previous:
            item.setCheckState(
                Qt.CheckState.Unchecked
                if previous == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )

    def _checked_ids(self) -> list[str]:
        return [
            str(self.list_widget.item(index).data(Qt.ItemDataRole.UserRole) or "")
            for index in range(self.list_widget.count())
            if self.list_widget.item(index).checkState() == Qt.CheckState.Checked
        ]

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self.list_widget.count()):
            self.list_widget.item(index).setCheckState(state)

    def _toggle_all(self, checked: bool) -> None:
        self._set_all(checked)

    def _select_current(self) -> None:
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(Qt.ItemDataRole.UserRole) or "") == self.current_conversation_id
                else Qt.CheckState.Unchecked
            )

    def _selection_changed(self, _item: QListWidgetItem | None = None) -> None:
        selected = len(self._checked_ids())
        self.count_label.setText(f"已选择 {selected} 个对话")
        self.select_all.blockSignals(True)
        self.select_all.setChecked(self.list_widget.count() > 0 and selected == self.list_widget.count())
        self.select_all.blockSignals(False)

    def _delete(self) -> None:
        selected = self._checked_ids()
        if not selected:
            QMessageBox.information(self, "尚未选择", "请至少选择一个需要删除的对话。")
            return
        answer = QMessageBox.question(
            self,
            "确认永久删除",
            f"确定永久删除所选的 {len(selected)} 个历史对话吗？\n\n此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = self.history_store.delete(selected)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "删除失败", str(error))
            return
        if deleted <= 0:
            QMessageBox.information(self, "没有删除", "所选历史对话已经不存在。")
            return
        self.deleted_ids = selected
        self.accept()


class ReliabilityCenterDialog(QDialog):
    def __init__(
        self,
        policy_store: ReliabilityPolicyStore,
        task_store: BackgroundTaskStore,
        journal: OperationJournal,
        ledger: UsageLedger,
        parent: QWidget | None = None,
        post_rollback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.policy_store = policy_store
        self.task_store = task_store
        self.journal = journal
        self.ledger = ledger
        self.post_rollback = post_rollback
        self.retry_task_id = ""
        self.setWindowTitle("后台任务、费用与恢复")
        self.resize(900, 700)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        task_tab = QWidget()
        task_layout = QVBoxLayout(task_tab)
        task_layout.addWidget(QLabel("任务在切换页面后继续运行；程序异常退出时，运行中任务会标记为“已中断”，不会悄悄重复扣费。"))
        self.task_list = QListWidget()
        for task in reversed(task_store.records):
            item = QListWidgetItem(f"[{task.state}] {task.question[:90]}  ·  {task.updated_at}")
            item.setData(Qt.ItemDataRole.UserRole, task.id)
            item.setToolTip(task.error or task.id)
            self.task_list.addItem(item)
        task_layout.addWidget(self.task_list, 1)
        retry = QPushButton("重试所选失败/中断任务")
        retry.clicked.connect(self._retry)
        task_layout.addWidget(retry)
        tabs.addTab(task_tab, "后台任务")

        budget_tab = QWidget()
        budget_layout = QFormLayout(budget_tab)
        policy = policy_store.policy
        self.confirm_mutations = QCheckBox("每次具体写入前显示文件和差异（固定开启）")
        self.confirm_mutations.setChecked(True)
        self.confirm_mutations.setEnabled(False)
        self.confirm_mutations.setToolTip("读取本机资料无需确认；创建、修改、编译或重建本地内容必须逐次确认。")
        self.auto_notify = QCheckBox("任务完成时显示系统通知")
        self.auto_notify.setChecked(policy.auto_notify)
        self.single_limit = QDoubleSpinBox()
        self.single_limit.setRange(0, 10000)
        self.single_limit.setDecimals(3)
        self.single_limit.setValue(policy.single_request_limit)
        self.single_limit.setPrefix("¥")
        self.daily_limit = QDoubleSpinBox()
        self.daily_limit.setRange(0, 100000)
        self.daily_limit.setDecimals(3)
        self.daily_limit.setValue(policy.daily_limit)
        self.daily_limit.setPrefix("¥")
        budget_layout.addRow(self.confirm_mutations)
        budget_layout.addRow(self.auto_notify)
        budget_layout.addRow("单次硬上限（0 表示不限制）", self.single_limit)
        budget_layout.addRow("每日硬上限（0 表示不限制）", self.daily_limit)
        budget_layout.addRow("今日已记录", QLabel(f"¥{ledger.today_total():.6f}"))
        tabs.addTab(budget_tab, "费用与确认")

        recovery_tab = QWidget()
        recovery_layout = QVBoxLayout(recovery_tab)
        recovery_layout.addWidget(QLabel("这里显示可撤销的成功操作和未正常结束的操作。撤销会恢复操作前的正文快照；项目 PDF 会在后台自动刷新。"))
        self.operation_list = QListWidget()
        for operation in journal.undoable():
            targets = "、".join(str(item) for item in operation.preview.get("targets") or [])
            item = QListWidgetItem(f"[{operation.state}] {operation.tool_name} · {targets}")
            item.setData(Qt.ItemDataRole.UserRole, operation.id)
            self.operation_list.addItem(item)
        recovery_layout.addWidget(self.operation_list, 1)
        rollback = QPushButton("撤销所选操作并恢复正文")
        rollback.clicked.connect(self._rollback)
        recovery_layout.addWidget(rollback)
        tabs.addTab(recovery_tab, "操作撤销与恢复")

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        layout.addLayout(row)

    def _retry(self) -> None:
        item = self.task_list.currentItem()
        if item is None:
            QMessageBox.information(self, "未选择任务", "请选择一条失败、取消或中断的任务。")
            return
        task_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        task = next((record for record in self.task_store.records if record.id == task_id), None)
        if task is None or task.state not in {"failed", "canceled", "interrupted"}:
            QMessageBox.information(self, "不能重试", "只有失败、取消或中断的任务可以重试。")
            return
        self.retry_task_id = task_id
        self._save()

    def _rollback(self) -> None:
        item = self.operation_list.currentItem()
        if item is None:
            QMessageBox.information(self, "未选择操作", "请选择一条需要恢复的操作。")
            return
        operation_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if QMessageBox.question(self, "确认撤销", "撤销将以操作前的本地快照替换相关正文文件；项目操作随后会重新生成正式 PDF。继续吗？") != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.journal.rollback(operation_id)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "恢复失败", str(error))
            return
        if self.post_rollback is not None:
            self.post_rollback(dict(result))
        QMessageBox.information(self, "撤销完成", f"已恢复 {result['count']} 个文件。若属于项目 TeX 操作，正式 PDF 正在后台刷新。")
        self.operation_list.takeItem(self.operation_list.currentRow())

    def _save(self) -> None:
        policy = self.policy_store.policy
        policy.confirm_mutations = True
        policy.show_preflight = False
        policy.auto_notify = self.auto_notify.isChecked()
        policy.single_request_limit = float(self.single_limit.value())
        policy.daily_limit = float(self.daily_limit.value())
        self.policy_store.save()
        self.accept()


@dataclass(slots=True)
class PendingTurn:
    question: str
    profile_id: str
    assistant_bubble: MessageBubble | None
    conversation_id: str = ""
    conversation_messages: list[dict[str, Any]] = field(default_factory=list)
    conversation_run_details: dict[str, Any] = field(default_factory=dict)
    account_snapshot: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    preflight: dict[str, Any] = field(default_factory=dict)
    mode: str = "answer"
    reasoning_preset: str = DEFAULT_REASONING_PRESET
    compute_mode: str = "auto"
    attachments: list[dict[str, Any]] = field(default_factory=list)
    user_bubble: MessageBubble | None = None
    user_message_index: int = -1
    regenerate_had_answer: bool = False
    rewrite_question: str = ""
    rewrite_answer: str = ""
    rewrite_excerpt: str = ""
    rewrite_instruction: str = ""


def regeneration_context(
    messages: list[dict[str, Any]], user_index: int
) -> tuple[list[dict[str, Any]], bool]:
    """Return the immutable conversation prefix used to regenerate the latest answer."""

    index = int(user_index)
    if not 0 <= index < len(messages) or messages[index].get("role") != "user":
        raise ValueError("没有找到需要重新生成回答的用户问题。")
    latest_user_index = next(
        (position for position in range(len(messages) - 1, -1, -1) if messages[position].get("role") == "user"),
        -1,
    )
    if index != latest_user_index:
        raise ValueError("目前只能重新生成当前对话中最新一条用户问题的回答。")
    prefix = [dict(item) for item in messages[: index + 1]]
    had_answer = any(item.get("role") == "assistant" for item in messages[index + 1 :])
    return prefix, had_answer


def figure_preview_from_metadata(metadata: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    details = dict(metadata or {})
    candidates: list[tuple[str, dict[str, Any], bool]] = []
    for trace in details.get("tool_traces") or []:
        if not isinstance(trace, dict) or not trace.get("ok"):
            continue
        evidence = dict(trace.get("evidence") or {})
        if trace.get("name") == "render_math_figure_preview":
            path = str(evidence.get("pdf_path") or "")
            validation = dict(evidence.get("visual_validation") or {})
        elif trace.get("name") in {"plot_math_function", "mathematica_plot"}:
            compute = dict(evidence.get("compute") or {})
            path = str(compute.get("pdf_path") or "")
            if not path:
                path = next(
                    (
                        str(item.get("absolute_path") or "")
                        for item in compute.get("artifacts") or []
                        if isinstance(item, dict) and item.get("format") == "pdf"
                    ),
                    "",
                )
            validation = dict(compute.get("visual_validation") or {})
        else:
            continue
        if path:
            candidates.append((path, validation, bool(validation.get("passed"))))
    passed = [candidate for candidate in candidates if candidate[2]]
    if passed:
        path, validation, _passed = passed[-1]
        return path, validation
    direct = str(details.get("final_preview_path") or "")
    if direct:
        matching = next(
            (candidate for candidate in reversed(candidates) if Path(candidate[0]) == Path(direct)),
            None,
        )
        return direct, dict(matching[1] if matching else {})
    if candidates:
        path, validation, _passed = candidates[-1]
        return path, validation
    return "", {}


class AccountUsageDialog(QDialog):
    """Display provider account metrics from an isolated web login session."""

    def __init__(
        self,
        monitor: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.monitor = monitor
        self.setWindowTitle("Provider 余额与用量")
        self.resize(900, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("Provider 账户用量")
        _set_font(title, 15, QFont.Weight.DemiBold)
        layout.addWidget(title)
        note = QLabel(
            "数据直接来自当前 Provider 网页控制台的只读接口。首次使用需要在下方完成一次网页登录；"
            "登录 Cookie 只保存在题库管理中心的独立本地资料目录，不会交给 AI，也不会读取 Chrome Cookie。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        metrics_row = QHBoxLayout()
        self.metric_labels: dict[str, QLabel] = {}
        cards = (
            ("remaining_balance", "剩余额度"),
            ("last_24h_usage", "近 24 小时消耗"),
            ("total_usage", "累计消耗"),
            ("request_count", "请求数"),
            ("runway_days", "预计可用"),
        )
        for key, label_text in cards:
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card_layout = QVBoxLayout(card)
            label = QLabel(label_text)
            value = QLabel("—")
            _set_font(value, 16, QFont.Weight.DemiBold)
            card_layout.addWidget(label)
            card_layout.addWidget(value)
            metrics_row.addWidget(card, 1)
            self.metric_labels[key] = value
        layout.addLayout(metrics_row)

        self.status_label = QLabel("尚未同步")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.refresh_button = QPushButton("立即刷新")
        self.login_button = QPushButton("网页登录")
        self.login_button.setCheckable(True)
        dashboard_button = QPushButton("打开控制台页面")
        close_button = QPushButton("关闭")
        self.refresh_button.clicked.connect(lambda: self.monitor.refresh())
        self.login_button.toggled.connect(self._toggle_login_view)
        dashboard_button.clicked.connect(self._show_dashboard)
        close_button.clicked.connect(self.accept)
        button_row.addWidget(self.refresh_button)
        button_row.addWidget(self.login_button)
        button_row.addWidget(dashboard_button)
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.web_view = QWebEngineView()
        self.web_view.setPage(self.monitor.page)
        self.web_view.setMinimumHeight(420)
        self.web_view.hide()
        layout.addWidget(self.web_view, 1)

        self.monitor.snapshot_updated.connect(self._show_snapshot)
        self.monitor.refresh_failed.connect(self._show_error)
        self.monitor.login_required.connect(self._show_login_required)
        self.monitor.busy_changed.connect(self._busy_changed)
        snapshot = self.monitor.cached_snapshot()
        if snapshot:
            self._show_snapshot(snapshot)
        else:
            self.status_label.setText("尚无同步数据。点击“网页登录”完成一次登录，然后点击“立即刷新”。")

    def _format_money(self, snapshot: dict[str, Any], key: str) -> str:
        value = snapshot.get(key)
        if not isinstance(value, (int, float)):
            return "—"
        return f"{snapshot.get('currency_symbol') or '¥'}{float(value):.4f}".rstrip("0").rstrip(".")

    def _show_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.metric_labels["remaining_balance"].setText(self._format_money(snapshot, "remaining_balance"))
        self.metric_labels["last_24h_usage"].setText(self._format_money(snapshot, "last_24h_usage"))
        self.metric_labels["total_usage"].setText(self._format_money(snapshot, "total_usage"))
        self.metric_labels["request_count"].setText(str(int(snapshot.get("request_count") or 0)))
        runway = snapshot.get("runway_days")
        self.metric_labels["runway_days"].setText(
            f"约 {float(runway):.1f} 天" if isinstance(runway, (int, float)) else "—"
        )
        self.status_label.setText(
            f"实时接口验证成功 · {snapshot.get('updated_at') or '未知时间'} · "
            f"数据源：{snapshot.get('source') or 'Provider 登录控制台'}"
        )

    def _show_error(self, message: str) -> None:
        self.status_label.setText("刷新失败：" + str(message))

    def _show_login_required(self, message: str) -> None:
        self.status_label.setText("需要网页登录：" + str(message))
        self.login_button.setChecked(True)

    def _busy_changed(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        self.refresh_button.setText("正在刷新…" if busy else "立即刷新")

    def _toggle_login_view(self, visible: bool) -> None:
        self.web_view.setVisible(bool(visible))
        self.login_button.setText("隐藏网页登录" if visible else "网页登录")
        if visible:
            self.monitor.open_dashboard()

    def _show_dashboard(self) -> None:
        self.login_button.setChecked(True)
        self.monitor.open_dashboard()


class RunDetailsDialog(QDialog):
    def __init__(self, metadata: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 运行详情")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        title = QLabel("本轮路由、上下文、用量与工具记录")
        _set_font(title, 13, QFont.Weight.DemiBold)
        layout.addWidget(title)
        view = QTextEdit()
        view.setReadOnly(True)
        latest = dict(metadata.get("latest_run") or metadata) if metadata else {}
        if not latest:
            text = "当前对话还没有已完成的 AI 回答。"
        else:
            usage = dict(latest.get("usage") or {})
            input_details = dict(usage.get("input_tokens_details") or {})
            output_details = dict(usage.get("output_tokens_details") or {})
            cost = dict(latest.get("cost_estimate") or {})
            billable = dict(cost.get("billable_tokens") or {})
            amount = cost.get("estimated_amount")
            cost_text = (
                f"{amount:.6f} {cost.get('currency', 'CNY')}"
                if isinstance(amount, (int, float))
                else "未配置 token 单价"
            )
            lines = [
                f"模型：{latest.get('profile_name') or '-'}",
                f"服务端模型：{latest.get('response_model') or '-'}",
                f"实际路由：{latest.get('route') or '-'}",
                f"推理模式：{latest.get('actual_reasoning_mode') or '-'}（请求：{latest.get('requested_reasoning_mode') or '-'}）",
                f"推理强度：{latest.get('actual_reasoning_effort') or '-'}（请求：{latest.get('requested_reasoning_effort') or '-'}）",
                f"回答详略：{latest.get('actual_text_verbosity') or '-'}",
                f"数学回答方式：{latest.get('math_response_mode') or '-'}",
                f"响应状态：{latest.get('response_status') or '-'}",
                f"耗时：{latest.get('elapsed_seconds', 0)} 秒",
                "",
                "Token 用量",
                f"  输入：{usage.get('input_tokens', usage.get('prompt_tokens', 0))}",
                f"  缓存输入：{input_details.get('cached_tokens', 0)}",
                f"  缓存写入：{billable.get('cached_write', 0)}",
                f"  输出：{usage.get('output_tokens', usage.get('completion_tokens', 0))}",
                f"  推理：{output_details.get('reasoning_tokens', 0)}",
                f"  总计：{usage.get('total_tokens', 0)}",
                f"估算费用：{cost_text}",
                "",
                f"工具调用：{len(latest.get('tool_traces') or [])} 次",
                f"对话累计 token：{metadata.get('cumulative_total_tokens', usage.get('total_tokens', 0))}",
                f"对话轮数：{metadata.get('run_count', 1)}",
            ]
            traces = list(latest.get("tool_traces") or [])
            if traces:
                lines.extend(("", "使用的工具"))
                lines.extend(
                    f"  {'成功' if trace.get('ok') else '失败'} · {trace.get('name') or '-'}"
                    for trace in traces
                    if isinstance(trace, dict)
                )
                compute_records = [
                    dict((trace.get("evidence") or {}).get("compute") or {})
                    for trace in traces
                    if isinstance(trace, dict)
                    and isinstance(trace.get("evidence"), dict)
                    and isinstance((trace.get("evidence") or {}).get("compute"), dict)
                ]
                if compute_records:
                    lines.extend(("", "计算记录"))
                    for index, record in enumerate(compute_records, 1):
                        lines.extend(
                            (
                                f"[{index}] 引擎：{record.get('engine') or 'dual'}",
                                f"    operation：{record.get('operation') or record.get('status') or '-'}",
                                f"    输入：{json.dumps(record.get('input') or {}, ensure_ascii=False)}",
                                f"    假设：{json.dumps(record.get('assumptions') or [], ensure_ascii=False)}",
                                f"    原始输出：{str(record.get('raw_output') or '')[:4000] or '-'}",
                                f"    格式化结果：{str(record.get('formatted_output') or '')[:4000] or '-'}",
                                f"    条件：{json.dumps(record.get('conditions') or [], ensure_ascii=False)}",
                                f"    警告：{json.dumps((record.get('metadata') or {}).get('warnings') or record.get('warnings') or [], ensure_ascii=False)}",
                                f"    产物：{json.dumps([item.get('absolute_path') for item in record.get('artifacts') or [] if isinstance(item, dict)], ensure_ascii=False)}",
                                f"    耗时：{int(record.get('duration_ms') or 0)} ms",
                                f"    错误：{record.get('error') or '-'}",
                                f"    重启：{bool((record.get('metadata') or {}).get('restarted'))}",
                                f"    核验状态：{record.get('status') or '-'}",
                            )
                        )
            verification = dict(latest.get("execution_verification") or {})
            if verification:
                all_verified = bool(verification.get("all_verified"))
                lines.extend(
                    (
                        "",
                        "本地操作完成门",
                        f"  结论：{'通过' if all_verified else '未通过'}",
                        f"  要求真实执行：{bool(verification.get('execution_required'))}",
                        f"  缺少写入执行：{bool(verification.get('missing_execution'))}",
                        f"  摘要：{verification.get('summary') or '-'}",
                    )
                )
                operations = [
                    dict(item) for item in verification.get("operations") or [] if isinstance(item, dict)
                ]
                for index, operation in enumerate(operations, 1):
                    changed_files = [str(item) for item in operation.get("changed_files") or []]
                    lines.extend(
                        (
                            f"[{index}] {operation.get('tool') or '-'} · {operation.get('status') or '-'}",
                            f"    说明：{operation.get('summary') or '-'}",
                            f"    改动文件：{json.dumps(changed_files, ensure_ascii=False)}",
                            f"    备份：{operation.get('backup_directory') or '-'}",
                            f"    命令：{operation.get('command') or '-'}",
                            f"    退出码：{operation.get('exit_code') if operation.get('exit_code') is not None else '-'}",
                            f"    命令日志：{operation.get('log_path') or '-'}",
                            f"    数据库：{operation.get('database_path') or '-'}",
                            f"    完整性：{operation.get('integrity_check') or '-'}",
                            f"    代码验证：{'需要且已有' if operation.get('code_verification_required') and operation.get('code_verification_present') else '需要但缺少' if operation.get('code_verification_required') else '不要求'}",
                        )
                    )
                    before_hashes = dict(operation.get("before_hashes") or {})
                    after_hashes = dict(operation.get("after_hashes") or {})
                    if before_hashes or after_hashes:
                        lines.append(
                            "    哈希证据："
                            + json.dumps({"before": before_hashes, "after": after_hashes}, ensure_ascii=False)[:6000]
                        )
                    diff = str(operation.get("diff") or "").strip()
                    if diff:
                        lines.extend(("    补丁摘要：", diff[:12000]))
            text = "\n".join(lines)
        view.setPlainText(text)
        layout.addWidget(view, 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close_button)
        layout.addLayout(row)


class ConversationContextDialog(QDialog):
    def __init__(
        self,
        summary: str,
        reference_state: dict[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("对话摘要与指代")
        self.resize(720, 620)
        layout = QVBoxLayout(self)
        note = QLabel("这里的内容会随下一轮请求发送。可以修改或删除 AI 对长对话的理解，不会改动原始历史消息。")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel("可编辑对话摘要"))
        self.summary_edit = QTextEdit()
        self.summary_edit.setPlainText(summary)
        layout.addWidget(self.summary_edit, 1)
        form = QFormLayout()
        self.reference_edits: dict[str, QLineEdit] = {}
        labels = {
            "current_topic": "当前主题",
            "current_problem": "当前题目",
            "previous_problem": "上一道题",
            "current_definition": "当前定义",
            "current_step_excerpt": "刚才那一步",
        }
        for key, label in labels.items():
            edit = QLineEdit(str(reference_state.get(key) or ""))
            self.reference_edits[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear)
        save_button = QPushButton("保存")
        save_button.clicked.connect(self.accept)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(clear_button)
        buttons.addStretch(1)
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)
        layout.addLayout(buttons)

    def _clear(self) -> None:
        self.summary_edit.clear()
        for edit in self.reference_edits.values():
            edit.clear()

    def values(self) -> tuple[str, dict[str, str]]:
        return (
            self.summary_edit.toPlainText().strip(),
            {key: edit.text().strip() for key, edit in self.reference_edits.items() if edit.text().strip()},
        )


class AiAgentPanel(QWidget):
    """A persistent ChatGPT-like subject learning panel."""

    def __init__(
        self,
        current_context: Callable[[], dict[str, Any]],
        palette_provider: Callable[[], tuple[str, str, str]] | None = None,
        reference_handler: Callable[[MessageReference], None] | None = None,
        parent: QWidget | None = None,
        *,
        discipline: str = "math",
    ) -> None:
        super().__init__(parent)
        self.discipline = "physics" if str(discipline).casefold() == "physics" else "math"
        self.subject_label = "物理" if self.discipline == "physics" else "数学"
        self.current_context_provider = current_context
        self.palette_provider = palette_provider
        self.reference_handler = reference_handler
        self.settings_store = AiAgentSettingsStore()
        if self.discipline == "physics":
            self.history_store = ConversationHistoryStore(CACHE_DIR / "ai_physics_agent_history.json")
            self.view_state_store = ConversationViewStateStore(CACHE_DIR / "ai_physics_agent_history_view_state.json")
            self.reference_library_store = ReferenceLibraryStore(
                CACHE_DIR / "ai_physics_reference_library.json",
                auto_add_default=False,
            )
            memory_store = LearningMemoryStore(CACHE_DIR / "ai_physics_agent_memory.json")
        else:
            self.history_store = ConversationHistoryStore()
            self.view_state_store = ConversationViewStateStore()
            self.reference_library_store = ReferenceLibraryStore()
            memory_store = LearningMemoryStore()
        self.service = AiAgentService(
            self.settings_store,
            memory_store=memory_store,
            discipline=self.discipline,
        )
        self.policy_store = ReliabilityPolicyStore()
        self.task_store = BackgroundTaskStore()
        self.operation_journal = self.service.tool_executor.operation_journal
        self.usage_ledger = UsageLedger()
        self.approval_broker = _MutationApprovalBroker(self)
        self.approval_broker.requested.connect(self._show_operation_approval)
        self._tray_icon: QSystemTrayIcon | None = None
        self.account_usage_monitor: Any | None = None
        if _ACCOUNT_USAGE_COMPAT is not None:
            monitor_type = getattr(
                _ACCOUNT_USAGE_COMPAT,
                "ProviderAccountUsageMonitor",
                None,
            )
            if monitor_type is not None:
                self.account_usage_monitor = monitor_type(self)
                self.account_usage_monitor.snapshot_updated.connect(
                    self._account_usage_updated
                )
                self.account_usage_monitor.login_required.connect(
                    self._account_login_required
                )
                self.account_usage_monitor.refresh_failed.connect(
                    self._account_usage_refresh_failed
                )
        self.thread_pool = QThreadPool.globalInstance()
        self._workers: set[_Worker] = set()
        self._active_agent_worker: _Worker | None = None
        self.messages: list[dict[str, Any]] = []
        self.pending_attachments: list[dict[str, Any]] = []
        self.active_conversation_id: str | None = None
        self.pending_turns: list[PendingTurn] = []
        self.current_turn: PendingTurn | None = None
        self.busy = False
        self.current_run_details: dict[str, Any] = {}
        self._pending_cost_reconciliations: list[dict[str, Any]] = []
        self._message_bubbles: list[MessageBubble] = []
        self._suppress_auto_scroll = False
        self._restore_scroll_conversation_id: str | None = None
        self._restore_scroll_attempts = 0
        self._history_sidebar_width = 292
        self._history_overlay_target_visible = False
        self._history_overlay_animating = False
        self._chat_column_target_width = 0
        self._message_reflow_timer = QTimer(self)
        self._message_reflow_timer.setSingleShot(True)
        self._message_reflow_timer.setInterval(260)
        self._message_reflow_timer.timeout.connect(self._reflow_message_bubbles)
        self._build_ui()
        self._refresh_tasks_button()
        self._refresh_profile_combo()
        self.refresh_context()
        self._account_refresh_timer: QTimer | None = None
        if self.account_usage_monitor is not None:
            self._account_refresh_timer = QTimer(self)
            self._account_refresh_timer.setInterval(5 * 60 * 1000)
            self._account_refresh_timer.timeout.connect(
                self._refresh_account_usage_if_supported_provider
            )
            self._account_refresh_timer.start()
            cached_usage = self.account_usage_monitor.cached_snapshot()
            if cached_usage:
                self._account_usage_updated(cached_usage)
            QTimer.singleShot(1500, self._refresh_account_usage_if_supported_provider)

    def _build_ui(self) -> None:
        self.setObjectName("aiChatShell")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.refresh_theme()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        toolbar = QFrame()
        toolbar.setObjectName("aiToolbar")
        toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(18, 12, 18, 12)
        toolbar_layout.setSpacing(9)
        title = QLabel(f"AI {self.subject_label}顾问")
        title.setObjectName("aiTitle")
        _set_font(title, 13, QFont.Weight.DemiBold)
        self.context_label = QLabel("")
        self.context_label.setObjectName("aiContext")
        _set_font(self.context_label, 8)
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("aiProfileCombo")
        self.profile_combo.setMinimumHeight(34)
        self.profile_combo.currentIndexChanged.connect(self._profile_selected)
        new_chat_button = QPushButton("新对话")
        self.history_button = QPushButton("历史对话")
        self.history_button.setCheckable(True)
        self.memory_button = QPushButton("记忆")
        self.account_usage_button: QPushButton | None = None
        if self.account_usage_monitor is not None:
            self.account_usage_button = QPushButton("余额与用量")
        self.more_button = QPushButton("更多")
        more_menu = QMenu(self.more_button)
        export_action = more_menu.addAction("导出对话")
        details_action = more_menu.addAction("运行详情")
        self.tasks_action = more_menu.addAction("任务与预算")
        reference_library_action = more_menu.addAction(f"{self.subject_label}资料库")
        more_menu.addSeparator()
        settings_action = more_menu.addAction("模型与 API")
        self.more_button.setMenu(more_menu)
        toolbar_buttons = [
            self.history_button,
            self.memory_button,
            new_chat_button,
            self.more_button,
        ]
        if self.account_usage_button is not None:
            toolbar_buttons.insert(2, self.account_usage_button)
        for button in toolbar_buttons:
            button.setObjectName("aiToolbarButton")
            button.setMinimumHeight(34)
        self.history_button.toggled.connect(self.toggle_history_sidebar)
        self.memory_button.clicked.connect(self.open_learning_memory)
        export_action.triggered.connect(self.open_export_dialog)
        details_action.triggered.connect(self.open_run_details)
        if self.account_usage_button is not None:
            self.account_usage_button.clicked.connect(self.open_account_usage)
        self.tasks_action.triggered.connect(self.open_reliability_center)
        reference_library_action.triggered.connect(self.open_reference_library)
        settings_action.triggered.connect(self.open_model_settings)
        new_chat_button.clicked.connect(self.clear_conversation)
        toolbar_layout.addWidget(title)
        toolbar_layout.addWidget(self.context_label, 1)
        toolbar_layout.addWidget(self.profile_combo)
        toolbar_layout.addWidget(self.history_button)
        toolbar_layout.addWidget(self.memory_button)
        if self.account_usage_button is not None:
            toolbar_layout.addWidget(self.account_usage_button)
        toolbar_layout.addWidget(new_chat_button)
        toolbar_layout.addWidget(self.more_button)
        outer.addWidget(toolbar)

        self.body = QWidget()
        self.body.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.history_sidebar = QFrame(self.body)
        self.history_sidebar.setObjectName("aiHistorySidebar")
        self.history_sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.history_sidebar.setFixedWidth(self._history_sidebar_width)
        sidebar_layout = QVBoxLayout(self.history_sidebar)
        sidebar_layout.setContentsMargins(12, 14, 12, 14)
        sidebar_layout.setSpacing(9)
        sidebar_head = QHBoxLayout()
        sidebar_title = QLabel("历史对话")
        sidebar_title.setObjectName("aiHistoryTitle")
        _set_font(sidebar_title, 11, QFont.Weight.DemiBold)
        sidebar_new = QPushButton("＋")
        sidebar_new.setObjectName("aiHistoryNew")
        sidebar_new.setFixedSize(30, 30)
        sidebar_new.setToolTip("新对话")
        sidebar_new.clicked.connect(self.clear_conversation)
        sidebar_delete = QPushButton("删除")
        sidebar_delete.setObjectName("aiHistoryDelete")
        sidebar_delete.setFixedHeight(30)
        sidebar_delete.setToolTip("删除一个、多个或全部历史对话")
        sidebar_delete.clicked.connect(self.open_delete_dialog)
        sidebar_head.addWidget(sidebar_title)
        sidebar_head.addStretch(1)
        sidebar_head.addWidget(sidebar_delete)
        sidebar_head.addWidget(sidebar_new)
        sidebar_layout.addLayout(sidebar_head)
        self.history_list = QListWidget()
        self.history_list.setObjectName("aiHistoryList")
        self.history_list.setWordWrap(True)
        self.history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_list.itemClicked.connect(self._history_item_clicked)
        sidebar_layout.addWidget(self.history_list, 1)
        self._history_overlay_opacity = QGraphicsOpacityEffect(self.history_sidebar)
        self._history_overlay_opacity.setOpacity(0.0)
        self.history_sidebar.setGraphicsEffect(self._history_overlay_opacity)
        self._history_overlay_animation = QParallelAnimationGroup(self)
        self._history_slide_animation = QPropertyAnimation(self.history_sidebar, b"pos", self)
        self._history_fade_animation = QPropertyAnimation(
            self._history_overlay_opacity,
            b"opacity",
            self,
        )
        for animation in (self._history_slide_animation, self._history_fade_animation):
            animation.setDuration(HISTORY_OVERLAY_ANIMATION_MS)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._history_overlay_animation.addAnimation(animation)
        self._history_overlay_animation.finished.connect(
            self._finish_history_overlay_animation
        )
        self.history_sidebar.hide()

        self.chat_stage = QWidget()
        self.chat_stage.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        stage_layout = QHBoxLayout(self.chat_stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)
        stage_layout.addStretch(1)

        self.chat_column = QWidget()
        self.chat_column.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.chat_column.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        chat_layout = QVBoxLayout(self.chat_column)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("aiConversationScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.conversation_host = QWidget()
        self.conversation_host.setObjectName("aiConversationHost")
        self.conversation_host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.conversation_host.setMinimumWidth(0)
        self.conversation_layout = QVBoxLayout(self.conversation_host)
        self.conversation_layout.setContentsMargins(24, 24, 24, 18)
        self.conversation_layout.setSpacing(16)
        empty_text = (
            "可以询问物理概念、推导、计算、量纲、近似与实验含义，搜索相关题目或论文，或绘制物理图形。"
            if self.discipline == "physics"
            else "可以询问定义、定理、具体数学问题，搜索相关题目，或要求绘制数学图形。"
        )
        self.empty_hint = QLabel(empty_text + "\n消息中的 Markdown 与 LaTeX 会在发送后自动排版。")
        self.empty_hint.setObjectName("aiEmptyHint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _set_font(self.empty_hint, 12)
        self._message_area_empty = True
        self.conversation_layout.addStretch(1)
        self.conversation_layout.addWidget(self.empty_hint)
        self.conversation_layout.addStretch(1)
        self.scroll.setWidget(self.conversation_host)
        chat_layout.addWidget(self.scroll, 1)

        composer_wrap = QWidget()
        composer_wrap.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        wrap_layout = QVBoxLayout(composer_wrap)
        wrap_layout.setContentsMargins(22, 8, 22, 18)
        composer = QFrame()
        composer.setObjectName("aiComposer")
        composer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(12, 8, 10, 8)
        composer_layout.setSpacing(2)
        self.attachment_bar = QWidget()
        self.attachment_bar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.attachment_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.attachment_bar_layout = WrappingFlowLayout(self.attachment_bar, spacing=7)
        self.attachment_bar_layout.setContentsMargins(0, 0, 0, 5)
        self.attachment_bar.hide()
        composer_layout.addWidget(self.attachment_bar)
        input_row = QHBoxLayout()
        self.attach_button = QPushButton("＋")
        self.attach_button.setObjectName("aiAttachButton")
        self.attach_button.setFixedSize(34, 34)
        self.attach_button.clicked.connect(self.import_attachments)
        self.thinking_combo = QComboBox()
        self.thinking_combo.setObjectName("aiThinkingCombo")
        self.thinking_combo.setMinimumHeight(34)
        self.thinking_combo.setMinimumWidth(104)
        self.thinking_combo.addItem(f"标准{self.subject_label}", "auto")
        self.thinking_combo.addItem("深度思考", "deep")
        self.thinking_combo.addItem("最大思考", "max")
        default_thinking_index = self.thinking_combo.findData(DEFAULT_REASONING_PRESET)
        if default_thinking_index >= 0:
            self.thinking_combo.setCurrentIndex(default_thinking_index)
        self.thinking_combo.setToolTip(
            f"标准{self.subject_label}默认 standard + high；深度思考使用 standard + xhigh；最大思考使用 standard + max。"
        )
        self.compute_combo = QComboBox()
        self.compute_combo.setObjectName("aiComputeCombo")
        self.compute_combo.setMinimumHeight(34)
        self.compute_combo.setMinimumWidth(96)
        self.compute_combo.addItem("计算自动", "auto")
        self.compute_combo.addItem("计算关闭", "off")
        self.compute_combo.addItem("Python", "python")
        self.compute_combo.addItem("Mathematica", "mathematica")
        self.compute_combo.addItem("双重核验", "dual")
        self.compute_combo.setToolTip("只决定本轮向模型提供哪些受控计算工具，不会自动执行工具；证明和概念题的自动模式通常不暴露计算工具。")
        self.question_edit = ChatInputTextEdit()
        self.question_edit.setObjectName("aiComposerEdit")
        self.question_edit.setAcceptRichText(False)
        self.question_edit.setPlaceholderText(
            r"例如：推导 Klein--Gordon 方程并检查量纲，或用 Mathematica 绘制波包演化"
            if self.discipline == "physics"
            else r"例如：紧算子的定义是什么？搜索与 Arzelà--Ascoli 定理相关的题，或画出 $y=x^2$ 的图像"
        )
        self.question_edit.set_height_limit_provider(lambda: max(90, int(self.height() * 0.4)))
        _set_font(self.question_edit, 11)
        self.question_edit.send_requested.connect(self.send_question)
        self.question_edit.attachments_added.connect(self._add_attachments)
        self.send_button = SendArrowButton()
        self.send_button.setObjectName("aiSendButton")
        self.send_button.setFixedSize(34, 34)
        self.send_button.clicked.connect(self.send_question)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setObjectName("aiCancelButton")
        self.cancel_button.setFixedHeight(34)
        self.cancel_button.setToolTip("停止当前回答。已经发送给 API 的部分仍可能产生用量；取消会在当前网络或编译步骤返回后生效。")
        self.cancel_button.clicked.connect(self.cancel_current_turn)
        self.cancel_button.hide()
        input_row.addWidget(self.attach_button, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(self.thinking_combo, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(self.compute_combo, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(self.question_edit, 1)
        input_row.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        composer_layout.addLayout(input_row)
        hint = QLabel("Enter 发送并排版 · Shift+Enter 换行 · 可粘贴图片/文件或点击 ＋ 批量导入 · AI 回复期间仍可继续发送")
        hint.setObjectName("aiComposerHint")
        _set_font(hint, 8)
        composer_layout.addWidget(hint)
        wrap_layout.addWidget(composer)
        chat_layout.addWidget(composer_wrap)
        stage_layout.addWidget(self.chat_column)
        stage_layout.addStretch(1)
        body_layout.addWidget(self.chat_stage, 1)
        outer.addWidget(self.body, 1)
        self._refresh_history_list()
        QTimer.singleShot(0, lambda: self._sync_chat_column_width(reflow=False))
        QTimer.singleShot(0, self._position_history_overlay)

    def refresh_theme(self) -> None:
        accent = "#4f9bd7"
        contrast = "#d3b384"
        if self.palette_provider is not None:
            try:
                palette = self.palette_provider()
                accent = str(palette[0] or accent)
                contrast = str(palette[2] or contrast)
            except Exception:
                pass
        from PySide6.QtGui import QColor

        color = QColor(accent)
        ar, ag, ab = color.red(), color.green(), color.blue()
        contrast_color = QColor(contrast)
        cr, cg, cb = contrast_color.red(), contrast_color.green(), contrast_color.blue()
        style = """
            QWidget#aiChatShell {
                background: rgba(255, 255, 255, 242);
                border: 1px solid rgba(@R@, @G@, @B@, 205);
                border-radius: 20px;
                color: #111111;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
            }
            QFrame#aiToolbar { background: rgba(255, 255, 255, 224); border: 0; border-bottom: 1px solid #d9e4f1; }
            QLabel#aiTitle { color: #111111; }
            QLabel#aiContext { color: #6b7f99; }
            QComboBox#aiProfileCombo { background: rgba(255,255,255,245); border: 1px solid #c5d5e8; border-radius: 9px; padding: 6px 10px; min-width: 145px; }
            QComboBox#aiThinkingCombo { background: #eef4fb; border: 1px solid #c3d3e6; border-radius: 9px; padding: 5px 8px; color: #29415d; }
            QPushButton#aiToolbarButton { background: rgba(255,255,255,240); border: 1px solid #bdcfe5; border-radius: 9px; padding: 6px 12px; color: #29415d; }
            QPushButton#aiToolbarButton:hover { background: #edf6ff; border-color: #79aef0; }
            QPushButton#aiToolbarButton:checked { background: rgba(@R@,@G@,@B@,75); border-color: rgba(@R@,@G@,@B@,190); }
            QFrame#aiHistorySidebar { background: rgba(248,251,255,@HISTORY_ALPHA@); border: 0; border-right: 1px solid rgba(@R@,@G@,@B@,120); border-top-right-radius: 14px; border-bottom-right-radius: 14px; }
            QLabel#aiHistoryTitle { color: #111111; }
            QPushButton#aiHistoryNew { background: white; border: 1px solid #c3d3e6; border-radius: 8px; color: #111111; }
            QPushButton#aiHistoryDelete { background: white; border: 1px solid #e2aaa6; border-radius: 8px; padding: 4px 9px; color: #a9231b; }
            QPushButton#aiHistoryDelete:hover { background: #fff0ef; border-color: #cf6259; }
            QListWidget#aiHistoryList { background: transparent; border: 0; outline: 0; color: #111111; }
            QListWidget#aiHistoryList::item { background: transparent; border: 0; border-radius: 9px; padding: 9px 7px; margin: 2px 0; }
            QListWidget#aiHistoryList::item:hover { background: rgba(@R@,@G@,@B@,38); }
            QListWidget#aiHistoryList::item:selected { background: rgba(@R@,@G@,@B@,72); color: #111111; }
            QScrollArea#aiConversationScroll, QWidget#aiConversationHost { background: transparent; border: 0; }
            QLabel#aiEmptyHint { color: #7890ad; }
            QFrame#aiMessageBubble { border-radius: 16px; }
            QFrame#aiMessageBubble[role="assistant"] { background: #ffffff; border: 1px solid #d4deea; }
            QFrame#aiMessageBubble[role="user"] { background: rgba(@R@,@G@,@B@,120); border: 1px solid rgba(@R@,@G@,@B@,205); }
            QLabel#aiMessageAuthor { color: #2e4663; }
            QLabel#aiMessageStatus { color: #7890ad; }
            QLabel#aiMessageRaw { color: #111111; font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; }
            QPushButton#aiMessageCopyMode { background: transparent; border: 0; color: #617a99; padding: 2px 5px; }
            QPushButton#aiMessageCopyMode:hover { color: #245f9e; text-decoration: underline; }
            QPushButton#aiMessageFeedback { background: transparent; border: 0; color: #617a99; padding: 2px 5px; }
            QPushButton#aiMessageFeedback:hover { color: #245f9e; text-decoration: underline; }
            QPushButton#aiMessageFeedback[selected="true"] { color: #176b45; font-weight: 600; }
            QFrame#aiAttachmentCard { background: rgba(247,249,252,248); border: 1px solid #d4dce7; border-radius: 11px; }
            QFrame#aiAttachmentCard:hover { background: #f1f5f9; border-color: #b8c6d8; }
            QLabel#aiAttachmentThumbnail { background: #e8edf3; border: 0; border-radius: 8px; }
            QLabel#aiAttachmentName { color: #172536; border: 0; }
            QLabel#aiAttachmentDetail { color: #6b7b8f; border: 0; }
            QPushButton#aiMessageReference { background: #f4f8fd; border: 1px solid #c5d7eb; border-radius: 9px; color: #245f9e; padding: 4px 9px; }
            QPushButton#aiMessageReference:hover { background: #e7f2ff; border-color: #78aee9; text-decoration: underline; }
            QTextEdit#aiMessageSource { background: transparent; border: 0; color: #111111; padding: 0; selection-background-color: rgba(@R@,@G@,@B@,92); font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; }
            QFrame#aiFigurePreview { background: #fbfcfe; border: 1px solid #ccd8e6; border-radius: 12px; }
            QLabel#aiFigurePreviewTitle { color: #2e4663; border: 0; }
            QLabel#aiFigurePreviewImage { background: white; border: 0; }
            QPushButton#aiFigurePreviewOpen { background: white; border: 1px solid #bdcfe5; border-radius: 7px; padding: 4px 9px; color: #29415d; }
            QPushButton#aiFigurePreviewOpen:hover { background: #edf6ff; border-color: #79aef0; }
            QFrame#aiCollapsedSource { background: #f7f9fc; border: 1px solid #d3deea; border-radius: 10px; }
            QPushButton#aiCollapsedSourceToggle, QPushButton#aiCollapsedSourceCopy { background: transparent; border: 0; color: #315f91; padding: 4px 6px; }
            QPushButton#aiCollapsedSourceToggle:hover, QPushButton#aiCollapsedSourceCopy:hover { color: #174f88; text-decoration: underline; }
            QTextEdit#aiCollapsedSourceEditor { background: #ffffff; border: 1px solid #d8e1ec; border-radius: 7px; padding: 8px; color: #172536; font-family: "Cascadia Mono", Consolas, monospace; }
            QFrame#aiComposer { background: rgba(255,255,255,250); border: 1px solid #b8cce5; border-radius: 18px; }
            QPushButton#aiAttachButton { background: #eef4fb; border: 1px solid #c3d3e6; border-radius: 17px; color: #29415d; font-size: 20px; }
            QPushButton#aiAttachButton:hover { background: #e2effd; border-color: #79aef0; }
            QPushButton#aiAttachmentRemove { background: rgba(255,255,255,220); border: 1px solid #d5dde7; border-radius: 11px; color: #687b91; font-size: 16px; }
            QPushButton#aiAttachmentRemove:hover { color: #b3261e; }
            QPushButton#aiAttachmentClear { background: transparent; border: 1px solid #d5dde7; border-radius: 9px; color: #667085; padding: 6px 10px; }
            QPushButton#aiAttachmentClear:hover { background: #fff1f0; border-color: #e1aaa5; color: #b3261e; }
            QTextEdit#aiComposerEdit { background: transparent; border: 0; color: #111111; padding: 6px; selection-background-color: rgba(@R@,@G@,@B@,80); }
            QPushButton#aiSendButton { background: rgb(@R@,@G@,@B@); color: white; border: 0; border-radius: 17px; font-weight: 700; }
            QPushButton#aiSendButton:hover { background: rgba(@R@,@G@,@B@,220); }
            QPushButton#aiCancelButton { background: #fff4f1; color: #a84232; border: 1px solid #e3b4aa; border-radius: 12px; padding: 4px 11px; }
            QPushButton#aiCancelButton:hover { background: #ffe7e1; border-color: #d99080; }
            QLabel#aiComposerHint { color: #8295ad; }
            """
        self.setStyleSheet(
            style.replace("@R@", str(ar))
            .replace("@G@", str(ag))
            .replace("@B@", str(ab))
            .replace("@CR@", str(cr))
            .replace("@CG@", str(cg))
            .replace("@CB@", str(cb))
            .replace("@HISTORY_ALPHA@", str(HISTORY_OVERLAY_BACKGROUND_ALPHA))
        )

    def refresh_context(self) -> None:
        self.refresh_theme()
        context = dict(self.current_context_provider() or {})
        subject = str(context.get("subject_name") or "全部学科")
        project = str(context.get("project_ref") or "")
        short = subject + (f" · {project}" if project else "")
        self.context_label.setText(short)
        self.context_label.setToolTip("当前定位：" + json.dumps(context, ensure_ascii=False, indent=2) + "\n检索范围仍可扩展到全部题库。")

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        if hasattr(self, "question_edit"):
            QTimer.singleShot(0, self.question_edit.update_content_height)
        if hasattr(self, "chat_column"):
            QTimer.singleShot(0, lambda: self._sync_chat_column_width(reflow=True))
        if hasattr(self, "history_sidebar"):
            QTimer.singleShot(0, self._position_history_overlay)

    def _refresh_profile_combo(self, selected_id: str = "") -> None:
        target = selected_id or self.settings_store.active_profile_id
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.settings_store.profiles:
            self.profile_combo.addItem(profile.name, profile.id)
        index = self.profile_combo.findData(target)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.blockSignals(False)
        self._profile_selected(self.profile_combo.currentIndex())

    def _profile_selected(self, index: int) -> None:
        profile_id = str(self.profile_combo.itemData(index) or "")
        if profile_id:
            self.settings_store.set_active(profile_id)

    def open_model_settings(self) -> None:
        dialog = ModelSettingsDialog(self.settings_store, self.service, self.thread_pool, self)
        dialog.profiles_changed.connect(self._refresh_profile_combo)
        dialog.exec()
        self._refresh_profile_combo()

    def open_learning_memory(self) -> None:
        LearningMemoryDialog(self.service, self, discipline=self.discipline).exec()

    def open_run_details(self) -> None:
        RunDetailsDialog(self.current_run_details, self).exec()

    def open_reliability_center(self) -> None:
        dialog = ReliabilityCenterDialog(
            self.policy_store,
            self.task_store,
            self.operation_journal,
            self.usage_ledger,
            self,
            post_rollback=self._post_operation_rollback,
        )
        dialog.exec()
        if dialog.retry_task_id:
            task = next((item for item in self.task_store.records if item.id == dialog.retry_task_id), None)
            if task is not None:
                self.question_edit.setPlainText(task.question)
                self.question_edit.setFocus()
                QTimer.singleShot(0, self.send_question)
        self._refresh_tasks_button()

    def _post_operation_rollback(self, result: dict[str, Any]) -> None:
        if str(result.get("tool_name") or "") not in {"edit_project_tex", "insert_tikz_figure"}:
            self._notify_task("操作已撤销", f"已恢复 {int(result.get('count') or 0)} 个文件。")
            return
        arguments = dict(result.get("arguments") or {})
        subject = str(arguments.get("subject_name") or "")
        project = str(arguments.get("project_ref") or "")
        if not subject or not project:
            self._notify_task("操作已撤销", "正文已恢复；缺少项目标识，未自动刷新 PDF。")
            return
        self._run_background(
            lambda emit: self.service.tool_executor.tex_editor.build_project_pdf(subject, project),
            lambda _value: self._notify_task("撤销已完成", "正文和正式项目 PDF 都已恢复到撤销后的状态。"),
            lambda message: QMessageBox.warning(self, "PDF 刷新失败", "正文已恢复，但正式项目 PDF 刷新失败：\n" + str(message)),
        )

    def _refresh_tasks_button(self) -> None:
        self.tasks_action.setText("任务与预算")
        self.more_button.setText("更多")

    def _show_operation_approval(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        try:
            dialog = OperationPreviewDialog(dict(payload.get("preview") or {}), self)
            payload["approved"] = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            event = payload.get("event")
            if hasattr(event, "set"):
                event.set()

    def _notify_task(self, title: str, message: str) -> None:
        if not self.policy_store.policy.auto_notify:
            return
        QApplication.alert(self.window(), 2500)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray_icon is None:
            self._tray_icon = QSystemTrayIcon(self.window().windowIcon(), self)
            self._tray_icon.show()
        self._tray_icon.showMessage(title, message[:240], QSystemTrayIcon.MessageIcon.Information, 5000)

    def _queue_local_rewrite(self, bubble: MessageBubble, assistant_index: int) -> None:
        if not 0 <= assistant_index < len(self.messages):
            return
        if not bubble.copy_mode:
            bubble.set_copy_mode(True)
            bubble.status.setText("请先框选要处理的段落，再点击“局部改写”")
            bubble.status.show()
            return
        excerpt = bubble.source.textCursor().selectedText().replace("\u2029", "\n").strip()
        if not excerpt:
            QMessageBox.information(self, "尚未选择内容", "请在原始文本中框选一段，再点击“局部改写”。")
            return
        dialog = LocalRewriteDialog(excerpt, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        instruction = dialog.instruction()
        source_answer = str(self.messages[assistant_index].get("content") or "")
        source_question = self._question_before_assistant(assistant_index)
        profile_id = str(self.profile_combo.currentData() or "")
        visible_prompt = f"局部改写：{instruction}\n\n> {excerpt[:1200]}"
        context = dict(self.current_context_provider() or {})
        try:
            preflight = self.service.preflight(
                profile_id,
                [{"role": "user", "content": visible_prompt + "\n\n" + source_question + "\n\n" + excerpt}],
                context,
            )
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "局部改写预检失败", str(error))
            return
        amount = (preflight.get("cost_estimate") or {}).get("estimated_amount")
        amount_value = float(amount) if isinstance(amount, (int, float)) else 0.0
        policy = self.policy_store.policy
        if (policy.single_request_limit > 0 and amount_value > policy.single_request_limit) or (
            policy.daily_limit > 0 and self.usage_ledger.today_total() + amount_value > policy.daily_limit
        ):
            QMessageBox.warning(self, "局部改写超过费用上限", f"保守预估 ¥{amount_value:.4f}，当前费用上限不允许发送。")
            return
        bubble.set_copy_mode(False)
        self._compile_bubble(self._add_bubble("user", visible_prompt), visible_prompt)
        assistant_bubble = self._add_bubble("assistant")
        task = self.task_store.create(str(self.active_conversation_id or ""), profile_id, visible_prompt, context)
        self.pending_turns.append(
            PendingTurn(
                visible_prompt,
                profile_id,
                assistant_bubble,
                conversation_id=str(self.active_conversation_id or ""),
                task_id=task.id,
                context=context,
                preflight=preflight,
                mode="rewrite",
                rewrite_question=source_question,
                rewrite_answer=source_answer,
                rewrite_excerpt=excerpt,
                rewrite_instruction=instruction,
            )
        )
        if self.busy:
            assistant_bubble.set_waiting(f"已排队局部改写 · 前面还有 {len(self.pending_turns) - 1} 条")
        else:
            self._start_next_turn()

    def open_reference_library(self) -> None:
        dialog = ReferenceLibraryDialog(self.reference_library_store, self, discipline=self.discipline)
        dialog.exec()
        if not dialog.rebuild_requested:
            return
        self._notify_task(f"{self.subject_label}资料库", "正在后台重建本地索引；可以继续浏览和输入消息。")
        self._run_background(
            lambda emit: self.service.tool_executor.semantic_index.ensure_current(force=True),
            lambda result: self._notify_task(
                f"{self.subject_label}资料库索引完成",
                f"已索引 {int((result or {}).get('document_count') or 0)} 个资料片段。",
            ),
            lambda message: QMessageBox.warning(self, "资料库索引失败", str(message)),
        )

    def open_account_usage(self) -> None:
        if self.account_usage_monitor is None:
            return
        dialog = AccountUsageDialog(self.account_usage_monitor, self)
        QTimer.singleShot(0, self._refresh_account_usage_if_supported_provider)
        dialog.exec()

    def _refresh_account_usage_if_supported_provider(self) -> None:
        if self.account_usage_monitor is None or _ACCOUNT_USAGE_COMPAT is None:
            return
        try:
            profile = self.settings_store.active_profile()
        except (KeyError, ValueError):
            return
        supports_profile = getattr(
            _ACCOUNT_USAGE_COMPAT,
            "supports_provider_profile",
            None,
        )
        if supports_profile is None or not supports_profile(profile):
            return
        self.account_usage_monitor.refresh()

    def _account_usage_updated(self, snapshot: dict[str, Any]) -> None:
        if self.account_usage_button is None:
            return
        symbol = str(snapshot.get("currency_symbol") or "¥")
        remaining = snapshot.get("remaining_balance")
        if isinstance(remaining, (int, float)):
            self.account_usage_button.setText(f"余额 {symbol}{float(remaining):.2f}")
        self.account_usage_button.setToolTip(
            "\n".join(
                (
                    f"剩余额度：{symbol}{float(snapshot.get('remaining_balance') or 0):.4f}",
                    f"近 24 小时消耗：{symbol}{float(snapshot.get('last_24h_usage') or 0):.4f}",
                    f"累计消耗：{symbol}{float(snapshot.get('total_usage') or 0):.4f}",
                    f"请求数：{int(snapshot.get('request_count') or 0)}",
                    f"更新时间：{snapshot.get('updated_at') or '未知'}",
                )
            )
        )
        self._reconcile_pending_costs(snapshot)

    def _reconcile_pending_costs(self, snapshot: dict[str, Any]) -> None:
        if not self._pending_cost_reconciliations:
            return
        remaining: list[dict[str, Any]] = []
        self.history_store.reload()
        details_by_conversation: dict[str, dict[str, Any]] = {}
        records_by_conversation: dict[str, Any] = {}
        changed_conversations: set[str] = set()

        def conversation_details(conversation_id: str) -> dict[str, Any]:
            selected_id = str(conversation_id or self.active_conversation_id or "")
            if selected_id in details_by_conversation:
                return details_by_conversation[selected_id]
            if selected_id == self.active_conversation_id:
                details = dict(self.current_run_details)
            else:
                record = self.history_store.get(selected_id)
                records_by_conversation[selected_id] = record
                details = dict(record.metadata or {}) if record is not None else {}
            details_by_conversation[selected_id] = details
            return details

        groups: dict[tuple[int, float, str], list[dict[str, Any]]] = {}
        for pending in self._pending_cost_reconciliations:
            before = dict(pending.get("before") or {})
            key = (
                int(before.get("request_count") or 0),
                float(before.get("total_usage") or 0),
                str(before.get("updated_at") or ""),
            )
            groups.setdefault(key, []).append(pending)
        for pending_group in groups.values():
            before = dict(pending_group[0].get("before") or {})
            reconcile_usage = (
                getattr(_ACCOUNT_USAGE_COMPAT, "reconcile_provider_usage", None)
                if _ACCOUNT_USAGE_COMPAT is not None
                else None
            )
            window = reconcile_usage(before, snapshot, None) if reconcile_usage else {}
            if not window:
                remaining.extend(pending_group)
                continue
            total_actual = float(window.get("actual_charge") or 0)
            estimates = [
                max(0.0, float(item.get("estimated_amount") or 0)) for item in pending_group
            ]
            total_estimate = sum(estimates)
            for index, pending in enumerate(pending_group):
                estimate = estimates[index]
                if len(pending_group) == 1:
                    actual = total_actual
                    attribution = "exact_turn_window"
                elif total_estimate > 0:
                    actual = total_actual * estimate / total_estimate
                    attribution = "proportional_multi_turn_window"
                else:
                    actual = total_actual / len(pending_group)
                    attribution = "equal_multi_turn_window"
                reconciliation = {
                    **window,
                    "actual_charge": round(actual, 6),
                    "estimated_charge": estimate,
                    "difference": round(actual - estimate, 6),
                    "attribution": attribution,
                    "note": (
                        str(window.get("note") or "")
                        + (
                            f" 同一同步窗口内完成了 {len(pending_group)} 条回答，"
                            "实际总扣费按各回答的本地估算占比分摊。"
                            if len(pending_group) > 1
                            else ""
                        )
                    ).strip(),
                }
                run_id = str(pending.get("run_id") or "")
                task_id = str(pending.get("task_id") or "")
                conversation_id = str(pending.get("conversation_id") or self.active_conversation_id or "")
                if task_id:
                    self.usage_ledger.update_actual(task_id, float(reconciliation.get("actual_charge") or 0))
                details = conversation_details(conversation_id)
                runs = [dict(item) for item in details.get("runs") or [] if isinstance(item, dict)]
                for run in runs:
                    if str(run.get("run_id") or "") == run_id:
                        run["account_reconciliation"] = reconciliation
                        latest = dict(runs[-1]) if runs else {}
                        details_by_conversation[conversation_id] = {
                            **details,
                            **latest,
                            "latest_run": latest,
                            "runs": runs,
                        }
                        changed_conversations.add(conversation_id)
                        break
        self._pending_cost_reconciliations = remaining
        for conversation_id in changed_conversations:
            details = details_by_conversation[conversation_id]
            if conversation_id == self.active_conversation_id:
                self.current_run_details = dict(details)
                self._persist_history(str(self.profile_combo.currentData() or ""))
                continue
            record = records_by_conversation.get(conversation_id) or self.history_store.get(conversation_id)
            if record is not None:
                self.history_store.upsert(
                    record.id,
                    [dict(item) for item in record.messages],
                    record.profile_id,
                    details,
                )
        if changed_conversations:
            self._refresh_history_list()

    def _account_login_required(self, _message: str) -> None:
        if self.account_usage_button is None or self.account_usage_monitor is None:
            return
        if not self.account_usage_monitor.cached_snapshot():
            self.account_usage_button.setText("余额与用量 · 需登录")
        self.account_usage_button.setToolTip("点击后在独立的 Provider 网页会话中完成一次登录。")

    def _account_usage_refresh_failed(self, message: str) -> None:
        if self.account_usage_button is None or self.account_usage_monitor is None:
            return
        cached = self.account_usage_monitor.cached_snapshot()
        if not cached:
            self.account_usage_button.setText("余额与用量 · 刷新失败")
        previous = "\n".join(
            line
            for line in self.account_usage_button.toolTip().splitlines()
            if not line.startswith("自动刷新失败：")
        ).strip()
        failure = "自动刷新失败：" + str(message)
        self.account_usage_button.setToolTip((previous + "\n" + failure).strip())

    def toggle_history_sidebar(self, visible: bool) -> None:
        target_visible = bool(visible)
        self._history_overlay_animation.stop()
        self._history_overlay_target_visible = target_visible
        self._history_overlay_animating = True
        self._position_history_overlay()
        self.history_sidebar.show()
        self.history_sidebar.raise_()
        self.history_button.setText("隐藏历史" if visible else "历史对话")
        if visible:
            self._refresh_history_list()
        self._history_slide_animation.setStartValue(self.history_sidebar.pos())
        self._history_slide_animation.setEndValue(
            QPoint(0 if target_visible else -self._history_sidebar_width, 0)
        )
        self._history_fade_animation.setStartValue(
            self._history_overlay_opacity.opacity()
        )
        self._history_fade_animation.setEndValue(1.0 if target_visible else 0.0)
        self._history_overlay_animation.start()

    def _position_history_overlay(self) -> None:
        if not hasattr(self, "history_sidebar") or not hasattr(self, "body"):
            return
        height = max(1, self.body.height())
        self.history_sidebar.resize(self._history_sidebar_width, height)
        if not self._history_overlay_animating:
            x = 0 if self._history_overlay_target_visible else -self._history_sidebar_width
            self.history_sidebar.move(x, 0)
        self.history_sidebar.raise_()

    def _finish_history_overlay_animation(self) -> None:
        self._history_overlay_animating = False
        self._position_history_overlay()
        if self._history_overlay_target_visible:
            self._history_overlay_opacity.setOpacity(1.0)
            self.history_sidebar.show()
            self.history_sidebar.raise_()
        else:
            self._history_overlay_opacity.setOpacity(0.0)
            self.history_sidebar.hide()

    def _sync_chat_column_width(self, *, reflow: bool) -> None:
        if not hasattr(self, "body") or not hasattr(self, "chat_column"):
            return
        body_width = max(self.body.width(), self.width())
        target = expanded_chat_column_width(
            body_width,
            self._history_sidebar_width,
        )
        previous = self._chat_column_target_width
        self._chat_column_target_width = target
        if self.chat_column.width() != target:
            self.chat_column.setFixedWidth(target)
        if reflow and previous > 0 and abs(previous - target) >= 36:
            self._message_reflow_timer.start()

    def _refresh_history_list(self) -> None:
        if not hasattr(self, "history_list"):
            return
        self.history_store.reload()
        self.history_list.blockSignals(True)
        self.history_list.clear()
        active_item: QListWidgetItem | None = None
        for record in self.history_store.all():
            stamp = record.updated_at.replace("T", " ")[:16]
            item = QListWidgetItem(f"{record.title}\n{stamp}")
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            usage = dict(record.metadata.get("usage") or {})
            total_tokens = usage.get("total_tokens") or usage.get("total_token_count") or usage.get("totalTokenCount")
            usage_line = f"\n总 token：{total_tokens}" if total_tokens is not None else ""
            cumulative = record.metadata.get("cumulative_total_tokens")
            cumulative_line = f"\n对话累计 token：{cumulative}" if cumulative is not None else ""
            item.setToolTip(
                f"{len(record.messages)} 条消息\n创建于 {record.created_at.replace('T', ' ')[:19]}"
                f"{usage_line}{cumulative_line}"
            )
            self.history_list.addItem(item)
            if record.id == self.active_conversation_id:
                active_item = item
        if active_item is not None:
            self.history_list.setCurrentItem(active_item)
        self.history_list.blockSignals(False)

    def _persist_history(self, profile_id: str = "") -> None:
        if not self.messages:
            return
        try:
            record = self.history_store.upsert(
                self.active_conversation_id,
                self.messages,
                profile_id,
                self.current_run_details,
            )
        except (OSError, ValueError):
            return
        self.active_conversation_id = record.id
        self.current_run_details = dict(record.metadata or {})
        self._refresh_history_list()

    def _turn_conversation_state(
        self,
        turn: PendingTurn,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conversation_id = str(turn.conversation_id or "")
        if conversation_id and conversation_id == self.active_conversation_id:
            return [dict(item) for item in self.messages], dict(self.current_run_details)
        if conversation_id:
            self.history_store.reload()
            record = self.history_store.get(conversation_id)
            if record is not None:
                return [dict(item) for item in record.messages], dict(record.metadata or {})
        return [dict(item) for item in self.messages], dict(self.current_run_details)

    def _persist_turn_conversation(
        self,
        turn: PendingTurn,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> None:
        record = self.history_store.upsert(
            turn.conversation_id or None,
            messages,
            turn.profile_id,
            metadata,
        )
        turn.conversation_id = record.id
        turn.conversation_messages = [dict(item) for item in record.messages]
        turn.conversation_run_details = dict(record.metadata or {})
        if self.active_conversation_id in {None, "", record.id}:
            self.active_conversation_id = record.id
            self.messages = [dict(item) for item in record.messages]
            self.current_run_details = dict(record.metadata or {})
        self._refresh_history_list()

    def _turn_bubble(self, turn: PendingTurn, *, create_if_visible: bool = False) -> MessageBubble | None:
        bubble = turn.assistant_bubble
        if bubble is not None and bubble in self._message_bubbles:
            return bubble
        turn.assistant_bubble = None
        if create_if_visible and turn.conversation_id == self.active_conversation_id:
            bubble = self._add_bubble("assistant")
            turn.assistant_bubble = bubble
            return bubble
        return None

    def _set_turn_waiting(self, turn: PendingTurn, message: str) -> None:
        bubble = self._turn_bubble(turn)
        if bubble is not None:
            bubble.set_waiting(str(message))

    def _history_item_clicked(self, item: QListWidgetItem) -> None:
        self.load_conversation(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def load_conversation(self, conversation_id: str) -> None:
        record = self.history_store.materialize_preview_assets(conversation_id)
        if record is None:
            return
        self._remember_current_scroll_position()
        for turn in [self.current_turn, *self.pending_turns]:
            if turn is not None:
                turn.assistant_bubble = None
                turn.user_bubble = None
        self._suppress_auto_scroll = True
        self._restore_scroll_conversation_id = record.id
        self._restore_scroll_attempts = 0
        self._reset_conversation_view()
        self.active_conversation_id = record.id
        self.current_run_details = dict(record.metadata or {})
        self.messages = [dict(message) for message in record.messages]
        if record.profile_id:
            index = self.profile_combo.findData(record.profile_id)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        runs = [item for item in self.current_run_details.get("runs") or [] if isinstance(item, dict)]
        assistant_run_index = 0
        assistant_count = sum(message.get("role") == "assistant" for message in self.messages)
        latest_user_index = next(
            (
                index
                for index in range(len(self.messages) - 1, -1, -1)
                if self.messages[index].get("role") == "user"
            ),
            -1,
        )
        for message_index, message in enumerate(self.messages):
            bubble = self._add_bubble(message["role"], message["content"])
            bubble.set_attachments(list(message.get("attachments") or []))
            bubble.author.setText("你" if message["role"] == "user" else "AI")
            self._compile_bubble(bubble, message["content"])
            if message["role"] == "assistant":
                preview_metadata = (
                    runs[assistant_run_index]
                    if assistant_run_index < len(runs)
                    else self.current_run_details
                    if assistant_count == 1 or assistant_run_index == assistant_count - 1
                    else {}
                )
                preview_path, visual_validation = figure_preview_from_metadata(preview_metadata)
                if preview_path:
                    bubble.set_figure_preview(preview_path, visual_validation)
                bubble.set_rewrite_available(True)
                bubble.rewrite_requested.connect(
                    lambda current=bubble, index=message_index: self._queue_local_rewrite(current, index)
                )
                self._enable_feedback(bubble, message_index)
                assistant_run_index += 1
            elif message_index == latest_user_index:
                self._enable_regenerate(bubble, message_index)
        if self.current_turn is not None and self.current_turn.conversation_id == record.id:
            waiting = self._add_bubble("assistant")
            waiting.set_waiting("后台任务仍在运行…")
            self.current_turn.assistant_bubble = waiting
        self._suppress_auto_scroll = False
        self._refresh_history_list()
        self._schedule_history_scroll_restore(40)

    def open_export_dialog(self) -> None:
        self._persist_history(str(self.profile_combo.currentData() or ""))
        if not self.history_store.all():
            QMessageBox.information(self, "没有可导出的对话", "完成至少一次对话后才能导出 TXT。")
            return
        ConversationExportDialog(self.history_store, self.active_conversation_id, self).exec()

    def open_delete_dialog(self) -> None:
        if self.busy or self.pending_turns:
            QMessageBox.information(self, "请求进行中", "请等待当前及已排队的模型请求结束后再删除历史对话。")
            return
        dialog = ConversationDeleteDialog(self.history_store, self.active_conversation_id, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.view_state_store.delete(dialog.deleted_ids)
        if self.active_conversation_id in set(dialog.deleted_ids):
            self.messages.clear()
            self.active_conversation_id = None
            self.current_run_details = {}
            self._reset_conversation_view()
        self._refresh_history_list()

    def _prepare_message_area(self) -> None:
        if not self._message_area_empty:
            return
        self.empty_hint.hide()
        self._message_area_empty = False
        for index in reversed(range(self.conversation_layout.count())):
            item = self.conversation_layout.itemAt(index)
            if item.spacerItem() is not None:
                self.conversation_layout.takeAt(index)
        self.conversation_layout.addStretch(1)

    def _add_bubble(self, role: str, text: str = "") -> MessageBubble:
        self._prepare_message_area()
        bubble = MessageBubble(role, text)
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(bubble, 1)
        self.conversation_layout.insertWidget(max(0, self.conversation_layout.count() - 1), row)
        self._message_bubbles.append(bubble)
        bubble.reference_requested.connect(self._open_message_reference)
        self._scroll_to_bottom()
        return bubble

    def _open_message_reference(self, reference: object) -> None:
        if not isinstance(reference, MessageReference):
            return
        if reference.kind == "url":
            QDesktopServices.openUrl(QUrl(reference.target))
            return
        if reference.kind == "project_file":
            try:
                project_dir, _project = self.service.repository._project_directory(
                    reference.subject_name, reference.project_ref
                )
                target = (Path(project_dir) / reference.target).resolve()
                if Path(project_dir).resolve() not in target.parents or not target.is_file():
                    raise FileNotFoundError(reference.target)
                reference = replace(reference, kind="file", target=str(target))
            except (OSError, ValueError, KeyError) as error:
                QMessageBox.warning(self, "无法打开本地引用", str(error))
                return
        if reference.kind == "file":
            target = Path(reference.target)
            if not target.is_file():
                QMessageBox.warning(self, "文件不存在", str(target))
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.resolve())))
            return
        if self.reference_handler is not None:
            self.reference_handler(reference)
            return
        QMessageBox.information(self, "本地题目引用", f"题号：{reference.target}")

    def _question_before_assistant(self, assistant_index: int) -> str:
        for index in range(min(int(assistant_index) - 1, len(self.messages) - 1), -1, -1):
            message = self.messages[index]
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    def _enable_regenerate(self, bubble: MessageBubble, user_index: int) -> None:
        if bubble.role != "user":
            return
        if not bool(bubble.property("regenerateConnected")):
            bubble.regenerate_requested.connect(
                lambda current=bubble, index=int(user_index): self._queue_regenerate_answer(
                    current, index
                )
            )
            bubble.setProperty("regenerateConnected", True)
        bubble.set_regenerate_available(True)

    def _restore_turn_regenerate_action(self, turn: PendingTurn) -> None:
        bubble = turn.user_bubble
        if (
            self.pending_turns
            or bubble is None
            or bubble not in self._message_bubbles
            or turn.conversation_id != self.active_conversation_id
            or (turn.mode == "regenerate" and turn.regenerate_had_answer)
        ):
            return
        self._enable_regenerate(bubble, turn.user_message_index)

    def _assistant_bubble_after(self, user_bubble: MessageBubble) -> MessageBubble | None:
        try:
            index = self._message_bubbles.index(user_bubble)
        except ValueError:
            return None
        if index + 1 < len(self._message_bubbles):
            candidate = self._message_bubbles[index + 1]
            if candidate.role == "assistant":
                return candidate
        return None

    def _queue_regenerate_answer(self, user_bubble: MessageBubble, user_index: int) -> None:
        if self.busy or self.pending_turns:
            QMessageBox.information(self, "请求进行中", "请等待当前及已排队的回答结束后再重新生成。")
            return
        try:
            messages, had_answer = regeneration_context(self.messages, user_index)
        except ValueError as error:
            QMessageBox.information(self, "无法重新生成", str(error))
            return
        profile_id = str(self.profile_combo.currentData() or "")
        profile = self.settings_store.profile(profile_id)
        if profile is None:
            QMessageBox.warning(self, "缺少模型配置", "请先在顶部“模型与 API”中选择配置。")
            return
        try:
            profile.validate(require_model=True)
        except ValueError as error:
            QMessageBox.warning(self, "模型配置不完整", str(error))
            return
        reasoning_preset = str(self.thinking_combo.currentData() or DEFAULT_REASONING_PRESET)
        compute_mode = str(self.compute_combo.currentData() or "auto")
        context = dict(self.current_context_provider() or {})
        try:
            preflight = self.service.preflight(
                profile_id,
                messages,
                context,
                reasoning_preset,
                compute_mode,
            )
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "重新生成预检失败", str(error))
            return
        amount = (preflight.get("cost_estimate") or {}).get("estimated_amount")
        amount_value = float(amount) if isinstance(amount, (int, float)) else 0.0
        policy = self.policy_store.policy
        if policy.single_request_limit > 0 and amount_value > policy.single_request_limit:
            QMessageBox.warning(
                self,
                "超过单次费用上限",
                f"本次保守预估为 ¥{amount_value:.4f}，超过单次硬上限 ¥{policy.single_request_limit:.4f}。",
            )
            return
        if policy.daily_limit > 0 and self.usage_ledger.today_total() + amount_value > policy.daily_limit:
            QMessageBox.warning(
                self,
                "超过每日费用上限",
                f"本次保守预估为 ¥{amount_value:.4f}，加上今日已记录费用后超过每日硬上限。",
            )
            return
        question_message = messages[-1]
        question = str(question_message.get("content") or "")
        attachments = [dict(item) for item in question_message.get("attachments") or []]
        assistant_bubble = self._assistant_bubble_after(user_bubble) or self._add_bubble("assistant")
        assistant_bubble.author.setText(profile.name)
        user_bubble.set_regenerate_available(False)
        task = self.task_store.create(
            str(self.active_conversation_id or ""), profile.id, question, context
        )
        self.pending_turns.append(
            PendingTurn(
                question,
                profile.id,
                assistant_bubble,
                conversation_id=str(self.active_conversation_id or ""),
                task_id=task.id,
                context=context,
                preflight=preflight,
                mode="regenerate",
                reasoning_preset=reasoning_preset,
                compute_mode=compute_mode,
                attachments=attachments,
                user_bubble=user_bubble,
                user_message_index=int(user_index),
                regenerate_had_answer=had_answer,
            )
        )
        self._start_next_turn()

    def _enable_feedback(self, bubble: MessageBubble, assistant_index: int) -> None:
        if bubble.role != "assistant":
            return
        existing = self.service.memory_store.feedback_for(
            str(self.active_conversation_id or ""), int(assistant_index)
        )
        current_answer = (
            str(self.messages[assistant_index].get("content") or "")[:8000]
            if 0 <= assistant_index < len(self.messages)
            else ""
        )
        if existing is not None and existing.answer_excerpt != current_answer:
            existing = None
        bubble.set_feedback_available(existing.rating if existing else "")
        bubble.feedback_requested.connect(
            lambda rating, current=bubble, index=int(assistant_index): self._record_answer_feedback(
                current, index, str(rating)
            )
        )

    def _record_answer_feedback(self, bubble: MessageBubble, assistant_index: int, rating: str) -> None:
        if not self.active_conversation_id:
            self._persist_history(str(self.profile_combo.currentData() or ""))
        if not self.active_conversation_id:
            QMessageBox.warning(self, "无法保存反馈", "当前对话尚未保存。")
            return
        record = self.service.memory_store.record_feedback(
            self.active_conversation_id,
            assistant_index,
            rating,
            issues=[],
            note="",
            question=self._question_before_assistant(assistant_index),
            answer=(
                str(self.messages[assistant_index].get("content") or "")
                if 0 <= assistant_index < len(self.messages)
                else ""
            ),
            context=dict(self.current_context_provider() or {}),
        )
        self.service.quality_dataset.derive_from_feedback(self.service.memory_store.feedback)
        bubble.set_feedback_available(record.rating)

    def _remember_current_scroll_position(self) -> None:
        if not self.active_conversation_id or not hasattr(self, "scroll"):
            return
        bar = self.scroll.verticalScrollBar()
        try:
            self.view_state_store.remember(self.active_conversation_id, bar.value(), bar.maximum())
        except OSError:
            pass

    def _schedule_history_scroll_restore(self, delay_ms: int = 0) -> None:
        conversation_id = self._restore_scroll_conversation_id
        if not conversation_id:
            return
        QTimer.singleShot(
            max(0, int(delay_ms)),
            lambda expected=conversation_id: self._restore_history_scroll_position(expected),
        )

    def _restore_history_scroll_position(self, conversation_id: str) -> None:
        if (
            conversation_id != self._restore_scroll_conversation_id
            or conversation_id != self.active_conversation_id
        ):
            return
        bar = self.scroll.verticalScrollBar()
        bar.setValue(self.view_state_store.position(conversation_id, bar.maximum()))
        self._restore_scroll_attempts += 1
        ready = all(
            not bubble.message_text.strip()
            or (bubble.render_requested_width_px > 0 and bubble.pending_render_segments <= 0)
            for bubble in self._message_bubbles
        )
        if ready or self._restore_scroll_attempts >= 80:
            self._restore_scroll_conversation_id = None
            return
        self._schedule_history_scroll_restore(120)

    def _scroll_to_bottom(self) -> None:
        if self._suppress_auto_scroll or self._restore_scroll_conversation_id:
            return
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def hideEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._remember_current_scroll_position()
        super().hideEvent(event)

    def _run_background(
        self,
        task: Callable[[Callable[[str], None]], Any],
        success: Callable[[Any], None],
        failure: Callable[[str], None],
        progress: Callable[[str], None] | None = None,
        canceled: Callable[[], None] | None = None,
    ) -> _Worker:
        worker = _Worker(task)
        worker.setAutoDelete(False)
        self._workers.add(worker)

        def done(result: Any) -> None:
            try:
                success(result)
            finally:
                self._workers.discard(worker)

        def failed(message: str) -> None:
            try:
                failure(message)
            finally:
                self._workers.discard(worker)

        def was_canceled() -> None:
            try:
                if canceled is not None:
                    canceled()
            finally:
                self._workers.discard(worker)

        if progress is not None:
            worker.signals.progress.connect(progress)
        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.canceled.connect(was_canceled)
        self.thread_pool.start(worker)
        return worker

    def _compile_bubble(self, bubble: MessageBubble, text: str) -> None:
        bubble.set_text_for_compilation(text)
        QTimer.singleShot(0, lambda current=bubble, content=text: self._compile_bubble_after_layout(current, content))

    def _target_message_width(self) -> int:
        chat_width = self._chat_column_target_width or self.scroll.viewport().width()
        if chat_width < 400:
            chat_width = expanded_chat_column_width(
                self.width(),
                self._history_sidebar_width,
            )
        return max(MIN_MESSAGE_CONTENT_WIDTH, chat_width - MESSAGE_HORIZONTAL_CHROME)

    def _compile_bubble_after_layout(self, bubble: MessageBubble, text: str) -> None:
        if bubble not in self._message_bubbles or not text.strip():
            return
        segments = list(bubble.text_segments)
        if not segments:
            bubble.render_width_px = self._target_message_width()
            bubble.render_requested_width_px = bubble.render_width_px
            return
        available_text_width = self._target_message_width()
        if (
            bubble.render_width_px > 0
            and abs(bubble.render_width_px - available_text_width) < 36
        ):
            return
        bubble.render_generation += 1
        generation = bubble.render_generation
        bubble.render_requested_width_px = available_text_width
        bubble.pending_render_segments = len(segments)
        bubble.failed_render_segments = 0
        width_mm = max(150.0, min(900.0, available_text_width * 25.4 / (72.27 * 1.4)))
        for segment in segments:
            self._run_background(
                lambda emit, content=segment.text: compile_message_svg(content, emit, width_mm=width_mm),
                lambda result, current=segment: self._message_rendered(
                    bubble, current, result, generation, available_text_width
                ),
                lambda message, current=segment: self._message_render_failed(
                    bubble, current, message, generation, available_text_width
                ),
                lambda message: bubble.status.setText(str(message)),
            )

    def _message_rendered(
        self,
        bubble: MessageBubble,
        segment: MessageTextSegment,
        result: Any,
        generation: int,
        rendered_width: int,
    ) -> None:
        if bubble not in self._message_bubbles or generation != bubble.render_generation:
            return
        if isinstance(result, MessageRenderResult):
            bubble.set_svg_result(result, segment)
            bubble.finish_segment_render()
            if bubble.pending_render_segments <= 0:
                bubble.render_width_px = rendered_width
                bubble.render_requested_width_px = rendered_width
            if self._restore_scroll_conversation_id:
                self._schedule_history_scroll_restore()
            else:
                self._scroll_to_bottom()
        else:
            self._message_render_failed(
                bubble,
                segment,
                "编译器返回了无法识别的结果。",
                generation,
                rendered_width,
            )

    def _message_render_failed(
        self,
        bubble: MessageBubble,
        segment: MessageTextSegment,
        message: str,
        generation: int,
        rendered_width: int,
    ) -> None:
        if bubble not in self._message_bubbles or generation != bubble.render_generation:
            return
        segment.show_raw()
        bubble.finish_segment_render(failed=True, message=message)
        if bubble.pending_render_segments <= 0:
            bubble.render_width_px = rendered_width
            bubble.render_requested_width_px = rendered_width
        if self._restore_scroll_conversation_id:
            self._schedule_history_scroll_restore()

    def _reflow_message_bubbles(self) -> None:
        target_width = self._target_message_width()
        for bubble in tuple(self._message_bubbles):
            if not bubble.message_text.strip():
                continue
            current_width = bubble.render_width_px or bubble.render_requested_width_px
            if current_width > 0 and abs(current_width - target_width) < 36:
                continue
            self._compile_bubble_after_layout(bubble, bubble.message_text)

    def _reset_conversation_view(self) -> None:
        self._message_bubbles.clear()
        while self.conversation_layout.count():
            item = self.conversation_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        empty_text = (
            "可以询问物理概念、推导、计算、量纲、近似与实验含义，搜索相关题目或论文，或绘制物理图形。"
            if self.discipline == "physics"
            else "可以询问定义、定理、具体数学问题，搜索相关题目，或要求绘制数学图形。"
        )
        self.empty_hint = QLabel(empty_text + "\n消息中的 Markdown 与 LaTeX 会在发送后自动排版。")
        self.empty_hint.setObjectName("aiEmptyHint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _set_font(self.empty_hint, 12)
        self._message_area_empty = True
        self.conversation_layout.addStretch(1)
        self.conversation_layout.addWidget(self.empty_hint)
        self.conversation_layout.addStretch(1)
        self.empty_hint.show()

    def clear_conversation(self) -> None:
        if self.busy or self.pending_turns:
            QMessageBox.information(self, "请求进行中", "请等待当前及已排队的模型请求结束后再新建对话。")
            return
        self._remember_current_scroll_position()
        self._restore_scroll_conversation_id = None
        self._clear_pending_attachments()
        self.messages.clear()
        self.active_conversation_id = None
        self.current_run_details = {}
        self._reset_conversation_view()
        self._refresh_history_list()

    def import_attachments(self) -> None:
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "导入图片或文件",
            "",
            "所有支持的附件 (*.png *.jpg *.jpeg *.webp *.gif *.pdf *.doc *.docx *.odt *.xls *.xlsx *.ods *.csv *.tsv *.ppt *.pptx *.odp *.tex *.bib *.sty *.cls *.md *.txt *.json *.yaml *.yml *.toml *.py *.js *.ts *.tsx *.jsx *.java *.c *.cpp *.h *.hpp *.cs *.go *.rs *.rb *.php *.swift *.kt *.m *.r *.sh *.ps1 *.sql *.html *.css *.xml *.zip *.7z *.rar);;所有文件 (*.*)",
        )
        if paths:
            self._add_attachments(paths)

    def _add_attachments(self, payload: object) -> None:
        try:
            values = list(payload) if isinstance(payload, (list, tuple)) else []
            if not values:
                return
            if all(isinstance(item, dict) for item in values):
                additions = [dict(item) for item in values]
            else:
                additions = store_files(str(item) for item in values)
            existing = {str(item.get("path") or "").casefold() for item in self.pending_attachments}
            for item in additions:
                key = str(item.get("path") or "").casefold()
                if key and key not in existing:
                    self.pending_attachments.append(item)
                    existing.add(key)
            if len(self.pending_attachments) > MAX_ATTACHMENT_COUNT:
                overflow = self.pending_attachments[MAX_ATTACHMENT_COUNT:]
                self.pending_attachments = self.pending_attachments[:MAX_ATTACHMENT_COUNT]
                for item in overflow:
                    Path(str(item.get("path") or "")).unlink(missing_ok=True)
                raise ValueError(f"一次最多添加 {MAX_ATTACHMENT_COUNT} 个附件。")
            self._refresh_attachment_bar()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "无法添加附件", str(error))

    def _remove_attachment(self, attachment_id: str) -> None:
        removed = next((item for item in self.pending_attachments if str(item.get("id") or "") == attachment_id), None)
        self.pending_attachments = [
            item for item in self.pending_attachments if str(item.get("id") or "") != attachment_id
        ]
        if removed is not None:
            Path(str(removed.get("path") or "")).unlink(missing_ok=True)
        self._refresh_attachment_bar()

    def _clear_pending_attachments(self) -> None:
        for attachment in self.pending_attachments:
            Path(str(attachment.get("path") or "")).unlink(missing_ok=True)
        self.pending_attachments.clear()
        self._refresh_attachment_bar()

    def _refresh_attachment_bar(self) -> None:
        while self.attachment_bar_layout.count():
            item = self.attachment_bar_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for attachment in self.pending_attachments:
            attachment_id = str(attachment.get("id") or "")
            self.attachment_bar_layout.addWidget(
                AttachmentCard(
                    attachment,
                    removable=True,
                    remove_callback=lambda _checked=False, current=attachment_id: self._remove_attachment(current),
                )
            )
        if len(self.pending_attachments) > 1:
            clear = QPushButton("清空附件")
            clear.setObjectName("aiAttachmentClear")
            clear.setCursor(Qt.CursorShape.PointingHandCursor)
            clear.clicked.connect(self._clear_pending_attachments)
            self.attachment_bar_layout.addWidget(clear)
        self.attachment_bar.setVisible(bool(self.pending_attachments))

    def send_question(self) -> None:
        question = self.question_edit.toPlainText().strip()
        attachments = [dict(item) for item in self.pending_attachments]
        if not question and attachments:
            question = "请分析我附上的内容。"
        if not question:
            return
        profile_id = str(self.profile_combo.currentData() or "")
        reasoning_preset = str(self.thinking_combo.currentData() or DEFAULT_REASONING_PRESET)
        compute_mode = str(self.compute_combo.currentData() or "auto")
        profile = self.settings_store.profile(profile_id)
        if profile is None:
            QMessageBox.warning(self, "缺少模型配置", "请先在顶部“模型与 API”中选择配置。")
            return
        try:
            profile.validate(require_model=True)
        except ValueError as error:
            QMessageBox.warning(self, "模型配置不完整", str(error))
            return

        context = dict(self.current_context_provider() or {})
        try:
            preflight = self.service.preflight(
                profile_id,
                [*self.messages, {"role": "user", "content": question, "attachments": attachments}],
                context,
                reasoning_preset,
                compute_mode,
            )
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "请求预检失败", str(error))
            return
        amount = (preflight.get("cost_estimate") or {}).get("estimated_amount")
        amount_value = float(amount) if isinstance(amount, (int, float)) else 0.0
        policy = self.policy_store.policy
        if policy.single_request_limit > 0 and amount_value > policy.single_request_limit:
            QMessageBox.warning(
                self,
                "超过单次费用上限",
                f"本次保守预估为 ¥{amount_value:.4f}，超过单次硬上限 ¥{policy.single_request_limit:.4f}。\n"
                "请在“任务与预算”中调整上限，或缩短问题与上下文。",
            )
            return
        today_total = self.usage_ledger.today_total()
        if policy.daily_limit > 0 and today_total + amount_value > policy.daily_limit:
            QMessageBox.warning(
                self,
                "超过每日费用上限",
                f"今日已记录 ¥{today_total:.4f}，本次预估 ¥{amount_value:.4f}，"
                f"合计超过每日硬上限 ¥{policy.daily_limit:.4f}。",
            )
            return
        self.question_edit.clear()
        default_thinking_index = self.thinking_combo.findData(DEFAULT_REASONING_PRESET)
        if default_thinking_index >= 0:
            self.thinking_combo.setCurrentIndex(default_thinking_index)
        self.compute_combo.setCurrentIndex(0)
        self.pending_attachments.clear()
        self._refresh_attachment_bar()
        for existing_bubble in self._message_bubbles:
            existing_bubble.set_regenerate_available(False)
        user_bubble = self._add_bubble("user", question)
        user_bubble.set_attachments(attachments)
        self._compile_bubble(user_bubble, question)
        assistant_bubble = self._add_bubble("assistant")
        assistant_bubble.author.setText(profile.name)
        task = self.task_store.create(str(self.active_conversation_id or ""), profile.id, question, context)
        pending = PendingTurn(
            question,
            profile.id,
            assistant_bubble,
            conversation_id=str(self.active_conversation_id or ""),
            task_id=task.id,
            context=context,
            preflight=preflight,
            reasoning_preset=reasoning_preset,
            compute_mode=compute_mode,
            attachments=attachments,
            user_bubble=user_bubble,
        )
        if self.busy:
            assistant_bubble.set_waiting(f"已排队 · 前面还有 {len(self.pending_turns) + 1} 条")
            self.pending_turns.append(pending)
        else:
            self.pending_turns.append(pending)
            self._start_next_turn()
        self.question_edit.setFocus()

    def _start_next_turn(self) -> None:
        if self.busy or not self.pending_turns:
            return
        turn = self.pending_turns.pop(0)
        self.task_store.update(turn.task_id, "running")
        turn.account_snapshot = (
            self.account_usage_monitor.cached_snapshot()
            if self.account_usage_monitor is not None
            else {}
        )
        self.current_turn = turn
        self.busy = True
        self.cancel_button.show()
        bubble = self._turn_bubble(turn, create_if_visible=True)
        if bubble is not None:
            waiting = {
                "deep": "正在深度思考…",
                "max": "正在进行最大思考…",
            }.get(turn.reasoning_preset, "正在思考…")
            bubble.set_waiting(waiting)
        conversation_messages, conversation_details = self._turn_conversation_state(turn)
        if turn.mode == "regenerate":
            try:
                conversation_messages, turn.regenerate_had_answer = regeneration_context(
                    conversation_messages, turn.user_message_index
                )
            except ValueError as error:
                self._answer_failed(str(error), turn_override=turn)
                return
        else:
            user_message: dict[str, Any] = {"role": "user", "content": turn.question}
            if turn.attachments:
                user_message["attachments"] = [dict(item) for item in turn.attachments]
            conversation_messages.append(user_message)
            turn.user_message_index = len(conversation_messages) - 1
        turn.conversation_messages = [dict(item) for item in conversation_messages]
        turn.conversation_run_details = dict(conversation_details)
        if turn.mode != "regenerate":
            self._persist_turn_conversation(turn, conversation_messages, conversation_details)
        context = dict(turn.context or self.current_context_provider() or {})
        context["conversation_summary"] = (
            ""
            if turn.mode == "regenerate"
            else str(turn.conversation_run_details.get("conversation_summary") or "")
        )
        context["conversation_reference_state"] = (
            {}
            if turn.mode == "regenerate"
            else dict(turn.conversation_run_details.get("reference_state") or {})
        )
        messages = [dict(item) for item in turn.conversation_messages]
        if turn.mode == "rewrite":
            task_callable = lambda emit: self.service.rewrite_answer_excerpt(
                turn.profile_id,
                turn.rewrite_question,
                turn.rewrite_answer,
                turn.rewrite_excerpt,
                turn.rewrite_instruction,
                context,
                emit,
            )
        else:
            task_callable = lambda emit: self.service.run(
                turn.profile_id,
                messages,
                context,
                emit,
                compile_math=False,
                mutation_approval=self.approval_broker.request,
                task_id=turn.task_id,
                reasoning_preset=turn.reasoning_preset,
                compute_mode=turn.compute_mode,
            )
        self._active_agent_worker = self._run_background(
            task_callable,
            self._answer_finished,
            self._answer_failed,
            lambda message, current=turn: self._set_turn_waiting(current, str(message)),
            self._answer_canceled,
        )

    def cancel_current_turn(self) -> None:
        worker = self._active_agent_worker
        if not self.busy or worker is None:
            return
        worker.cancel()
        self.cancel_button.setEnabled(False)
        if self.current_turn is not None:
            self._set_turn_waiting(self.current_turn, "正在停止当前回答…")

    def _answer_canceled(self) -> None:
        turn = self.current_turn
        self.current_turn = None
        self._active_agent_worker = None
        self.busy = False
        self.cancel_button.hide()
        self.cancel_button.setEnabled(True)
        if turn is not None:
            self.task_store.update(turn.task_id, "canceled", error="用户停止了当前回答。")
            self._refresh_tasks_button()
            cancellation = "已停止当前回答。已经发送给 API 的部分可能仍会计入用量。"
            if turn.mode == "regenerate" and turn.regenerate_had_answer:
                if turn.conversation_id == self.active_conversation_id:
                    self.load_conversation(turn.conversation_id)
            else:
                messages = [dict(item) for item in turn.conversation_messages]
                messages.append({"role": "assistant", "content": cancellation})
                self._persist_turn_conversation(turn, messages, turn.conversation_run_details)
                bubble = self._turn_bubble(turn, create_if_visible=True)
                if bubble is not None:
                    bubble.author.setText("AI")
                    self._compile_bubble(bubble, cancellation)
            self._restore_turn_regenerate_action(turn)
        self._start_next_turn()

    def _answer_finished(self, result: Any) -> None:
        turn = self.current_turn
        self.current_turn = None
        self._active_agent_worker = None
        self.busy = False
        self.cancel_button.hide()
        self.cancel_button.setEnabled(True)
        if turn is None:
            return
        if not isinstance(result, AgentRunResult):
            self._answer_failed("模型返回了无法识别的结果。", turn_override=turn)
            return
        conversation_messages = [dict(item) for item in turn.conversation_messages]
        conversation_messages.append({"role": "assistant", "content": result.answer})
        base_details = dict(turn.conversation_run_details)
        run_id = uuid.uuid4().hex
        run_details = {
            "run_id": run_id,
            "profile_name": result.profile_name,
            "route": result.route,
            "requested_reasoning_effort": result.requested_reasoning_effort,
            "actual_reasoning_effort": result.reasoning_effort,
            "requested_reasoning_mode": result.requested_reasoning_mode,
            "actual_reasoning_mode": result.reasoning_mode,
            "requested_text_verbosity": result.requested_text_verbosity,
            "actual_text_verbosity": result.text_verbosity,
            "reasoning_route_reason": result.reasoning_route_reason,
            "reasoning_preset": turn.reasoning_preset,
            "compute_mode": result.compute_mode or turn.compute_mode,
            "math_response_mode": result.math_response_mode,
            "response_model": result.response_model,
            "response_id": result.response_id,
            "response_status": result.response_status,
            "reasoning_context": result.reasoning_context,
            "task_kind": result.task_kind,
            "selected_tools": result.selected_tools,
            "context_budget": result.context_budget,
            "usage": result.usage,
            "tool_traces": result.tool_traces,
            "plan_report": result.plan_report,
            "quality_report": result.quality_report,
            "execution_verification": result.execution_verification,
            "fallback_reason": result.fallback_reason,
            "elapsed_seconds": result.elapsed_seconds,
            "cost_estimate": result.cost_estimate,
            "conversation_summary": result.conversation_summary
            or str(base_details.get("conversation_summary") or ""),
            "reference_state": result.reference_state
            or dict(base_details.get("reference_state") or {}),
            "task_id": turn.task_id,
            "preflight": turn.preflight,
        }
        previous_runs = base_details.get("runs") if isinstance(base_details, dict) else []
        runs = [dict(item) for item in previous_runs or [] if isinstance(item, dict)]
        if turn.mode == "regenerate":
            prior_assistant_count = sum(
                message.get("role") == "assistant" for message in turn.conversation_messages
            )
            runs = runs[:prior_assistant_count]
        runs.append(run_details)
        runs = runs[-50:]
        cumulative_total = 0
        for item in runs:
            usage = dict(item.get("usage") or {})
            value = usage.get("total_tokens") or usage.get("total_token_count") or usage.get("totalTokenCount") or 0
            try:
                cumulative_total += int(value)
            except (TypeError, ValueError):
                pass
        updated_details = {
            **run_details,
            "runs": runs,
            "run_count": len(runs),
            "cumulative_total_tokens": cumulative_total,
        }
        estimated_amount = (
            result.cost_estimate.get("estimated_amount") if result.cost_estimate else None
        )
        self.usage_ledger.record(turn.task_id, estimated_amount)
        self.task_store.update(turn.task_id, "completed", run_id=run_id)
        self._refresh_tasks_button()
        if turn.account_snapshot:
            self._pending_cost_reconciliations.append(
                {
                    "run_id": run_id,
                    "before": dict(turn.account_snapshot),
                    "estimated_amount": estimated_amount,
                    "task_id": turn.task_id,
                    "conversation_id": turn.conversation_id,
                }
            )
        self._persist_turn_conversation(turn, conversation_messages, updated_details)
        persisted_record = (
            self.history_store.materialize_preview_assets(turn.conversation_id)
            if turn.conversation_id
            else None
        )
        if persisted_record is not None:
            turn.conversation_run_details = dict(persisted_record.metadata or updated_details)
            if self.active_conversation_id == turn.conversation_id:
                self.current_run_details = dict(turn.conversation_run_details)
        if self.account_usage_monitor is not None:
            QTimer.singleShot(800, self._refresh_account_usage_if_supported_provider)
        assistant_index = len(conversation_messages) - 1
        persisted_runs = turn.conversation_run_details.get("runs") or []
        preview_metadata = (
            persisted_runs[-1]
            if persisted_runs and isinstance(persisted_runs[-1], dict)
            else {"tool_traces": result.tool_traces}
        )
        bubble = self._turn_bubble(turn, create_if_visible=True)
        if turn.mode == "regenerate" and turn.conversation_id == self.active_conversation_id:
            self.load_conversation(turn.conversation_id)
            bubble = None
        elif bubble is not None:
            bubble.author.setText(result.profile_name or "AI")
            self._compile_bubble(bubble, result.answer)
            bubble.set_rewrite_available(True)
            bubble.rewrite_requested.connect(
                lambda current=bubble, index=assistant_index: self._queue_local_rewrite(current, index)
            )
            preview_path, visual_validation = figure_preview_from_metadata(preview_metadata)
            if preview_path:
                bubble.set_figure_preview(preview_path, visual_validation)
            self._enable_feedback(bubble, assistant_index)
        self._restore_turn_regenerate_action(turn)
        self._notify_task("AI 任务已完成", turn.question)
        self._start_next_turn()

    def _answer_failed(self, message: str, turn_override: PendingTurn | None = None) -> None:
        turn = turn_override or self.current_turn
        self.current_turn = None
        self._active_agent_worker = None
        self.busy = False
        self.cancel_button.hide()
        self.cancel_button.setEnabled(True)
        if turn is not None:
            self.task_store.update(turn.task_id, "failed", error=message)
            self._refresh_tasks_button()
            if (
                turn.mode == "regenerate"
                and turn.regenerate_had_answer
                and turn.conversation_id == self.active_conversation_id
            ):
                self.load_conversation(turn.conversation_id)
            else:
                bubble = self._turn_bubble(turn, create_if_visible=True)
                if bubble is not None:
                    bubble.set_request_error(message)
            self._restore_turn_regenerate_action(turn)
            self._notify_task("AI 任务失败", message)
        self._start_next_turn()
