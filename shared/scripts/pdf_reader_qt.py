"""Qt widgets of the PDF reader: search bar, search highlights, reference preview,
thumbnails and the "continue reading" banner.

``QtPdfPreviewWindow`` in ``problem_bank_center_qt`` composes these widgets; the
non-Qt logic lives in ``pdf_reader_tools``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.reading_help_qt import (
    ACCENT,
    ACCENT_SOFT,
    AMBER_SOFT,
    FAINT,
    INK,
    LINE,
    MUTED,
    SURFACE,
    SURFACE_ALT,
    tool_button,
)

SIDEBAR_STYLE = f"""
QTabWidget#readerSidebar::pane {{ border: none; border-right: 1px solid {LINE}; background: {SURFACE_ALT}; }}
QTabWidget#readerSidebar QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 6px 12px; border: none; border-bottom: 2px solid transparent;
}}
QTabWidget#readerSidebar QTabBar::tab:selected {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
QListWidget#readerThumbnails, QListWidget#readerSearchResults {{
    background: {SURFACE_ALT}; border: none; color: {INK}; outline: none;
}}
QListWidget#readerThumbnails::item {{ border-radius: 6px; padding: 4px; color: {MUTED}; }}
QListWidget#readerThumbnails::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QListWidget#readerSearchResults::item {{ border-bottom: 1px solid {LINE}; padding: 6px 4px; }}
QListWidget#readerSearchResults::item:selected {{ background: {ACCENT_SOFT}; color: {INK}; }}
"""


# -- search bar ------------------------------------------------------------------------


class SearchBar(QFrame):
    """Ctrl+F bar under the toolbar: query, options, "3 / 17" and previous / next."""

    query_changed = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("readerSearchBar")
        self.setStyleSheet(
            f"QFrame#readerSearchBar {{ background: {SURFACE}; border: none; border-bottom: 1px solid {LINE}; }}"
            f"QLineEdit {{ border: 1px solid {LINE}; border-radius: 6px; padding: 4px 8px; background: {SURFACE_ALT}; color: {INK}; }}"
            f"QLineEdit:focus {{ border: 1px solid {ACCENT}; background: {SURFACE}; }}"
            f"QCheckBox {{ color: {MUTED}; }} QLabel#searchCount {{ color: {MUTED}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.setSpacing(6)
        self.input = QLineEdit()
        self.input.setPlaceholderText("在本文档中搜索（回车下一处，Shift+回车上一处）")
        self.input.setClearButtonEnabled(True)
        self.input.setMinimumWidth(320)
        self.case_box = QCheckBox("区分大小写")
        self.word_box = QCheckBox("整词")
        self.word_box.setToolTip("只匹配完整的词：搜 ring 不会匹配 string")
        self.count_label = QLabel("")
        self.count_label.setObjectName("searchCount")
        self.count_label.setMinimumWidth(96)
        self.previous_button = tool_button("‹", "上一处（Shift+回车）")
        self.next_button = tool_button("›", "下一处（回车）")
        self.close_button = tool_button("×", "关闭搜索（Esc）")
        layout.addWidget(self.input, 1)
        layout.addWidget(self.case_box)
        layout.addWidget(self.word_box)
        layout.addSpacing(6)
        layout.addWidget(self.count_label)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.close_button)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(260)
        self._debounce.timeout.connect(self.query_changed.emit)
        self.input.textChanged.connect(lambda _text: self._debounce.start())
        self.case_box.toggled.connect(lambda _on: self.query_changed.emit())
        self.word_box.toggled.connect(lambda _on: self.query_changed.emit())
        self.previous_button.clicked.connect(self.previous_requested.emit)
        self.next_button.clicked.connect(self.next_requested.emit)
        self.close_button.clicked.connect(self.closed.emit)
        self.input.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched: Any, event: QEvent) -> bool:  # type: ignore[override]
        if watched is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._debounce.isActive():
                    self._debounce.stop()
                    self.query_changed.emit()
                elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.previous_requested.emit()
                else:
                    self.next_requested.emit()
                return True
            if key == Qt.Key.Key_Escape:
                self.closed.emit()
                return True
        return super().eventFilter(watched, event)

    @property
    def query(self) -> str:
        return self.input.text().strip()

    def options(self) -> dict[str, bool]:
        return {"case_sensitive": self.case_box.isChecked(), "whole_word": self.word_box.isChecked()}

    def set_query(self, text: str) -> None:
        self.input.blockSignals(True)
        self.input.setText(text)
        self.input.blockSignals(False)

    def set_count(self, current: int, total: int, *, searching: str = "") -> None:
        if searching:
            self.count_label.setText(searching)
        elif not self.query:
            self.count_label.setText("")
        elif total == 0:
            self.count_label.setText("没有找到")
        else:
            self.count_label.setText(f"{current + 1 if current >= 0 else 0} / {total}")
        self.previous_button.setEnabled(total > 0)
        self.next_button.setEnabled(total > 0)

    def open(self) -> None:
        self.show()
        self.input.setFocus()
        self.input.selectAll()


class SearchOverlay(QWidget):
    """All search hits on screen in yellow, the current one in orange."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.rects: list[tuple[QRectF, bool]] = []
        self.hide()

    def set_rects(self, rects: list[tuple[QRectF, bool]]) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.rects = rects
        self.setVisible(bool(rects))
        self.update()

    def paintEvent(self, _event: QEvent) -> None:  # type: ignore[override]
        if not self.rects:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for rect, current in self.rects:
            painter.setPen(QPen(QColor(230, 120, 0), 1.6) if current else Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 140, 0, 110) if current else QColor(255, 214, 0, 95))
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 2, 2)


class SearchResultList(QListWidget):
    """Sidebar list of hits with their surrounding text."""

    result_activated = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("readerSearchResults")
        self.setWordWrap(True)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.itemClicked.connect(lambda item: self.result_activated.emit(int(item.data(Qt.ItemDataRole.UserRole))))

    def set_hits(self, hits: list[dict[str, Any]], page_names: Any) -> None:
        self.clear()
        for index, hit in enumerate(hits[:2000]):
            item = QListWidgetItem(f"{page_names(int(hit['page']))}\n{hit.get('context') or ''}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.addItem(item)
        if len(hits) > 2000:
            extra = QListWidgetItem(f"……另有 {len(hits) - 2000} 处未列出，请用回车逐处查看")
            extra.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(extra)

    def select_index(self, index: int) -> None:
        if 0 <= index < self.count():
            self.blockSignals(True)
            self.setCurrentRow(index)
            self.blockSignals(False)
            self.scrollToItem(self.item(index))


# -- thumbnails -----------------------------------------------------------------------------


class ThumbnailList(QListWidget):
    """Page thumbnails rendered lazily for the rows on screen."""

    page_activated = Signal(int)
    WIDTH = 132

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("readerThumbnails")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.TopToBottom)
        self.setWrapping(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSpacing(6)
        self.setIconSize(QSize(self.WIDTH, int(self.WIDTH * 1.42)))
        self.document: Any = None
        self._rendered: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._render_visible)
        self.verticalScrollBar().valueChanged.connect(lambda _value: self._timer.start())
        self.itemClicked.connect(lambda item: self.page_activated.emit(int(item.data(Qt.ItemDataRole.UserRole))))

    def set_document(self, document: Any, names: list[str]) -> None:
        self.clear()
        self.document = document
        self._rendered = set()
        placeholder = QPixmap(self.iconSize())
        placeholder.fill(QColor(SURFACE))
        for index, name in enumerate(names):
            item = QListWidgetItem(placeholder, name)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.addItem(item)
        self._timer.start()

    def showEvent(self, event: Any) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._timer.start()

    def resizeEvent(self, event: Any) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._timer.start()

    def _render_visible(self) -> None:
        if self.document is None or not self.isVisible() or self.count() == 0:
            return
        viewport = self.viewport().rect()
        rendered = 0
        ratio = self.devicePixelRatioF()
        for row in range(self.count()):
            item = self.item(row)
            rect = self.visualItemRect(item)
            if rect.bottom() < viewport.top() - 300:
                continue
            if rect.top() > viewport.bottom() + 300:
                break
            if row in self._rendered:
                continue
            try:
                size = self.document.pagePointSize(row)
                width = self.WIDTH
                height = max(1, int(width * float(size.height()) / max(1.0, float(size.width()))))
                image: QImage = self.document.render(row, QSize(int(width * ratio), int(height * ratio)))
            except RuntimeError:
                return
            if image.isNull():
                continue
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(ratio)
            framed = QPixmap(QSize(int(width * ratio) + 2, int(height * ratio) + 2))
            framed.setDevicePixelRatio(ratio)
            framed.fill(QColor(LINE))
            painter = QPainter(framed)
            painter.drawPixmap(1, 1, pixmap)
            painter.end()
            item.setIcon(framed)
            self._rendered.add(row)
            rendered += 1
            if rendered >= 6:
                self._timer.start()
                return

    def set_current_page(self, page: int) -> None:
        if 0 <= page < self.count():
            self.blockSignals(True)
            self.setCurrentRow(page)
            self.blockSignals(False)
            if self.isVisible():
                self.scrollToItem(self.item(page), QListWidget.ScrollHint.EnsureVisible)


# -- reference preview ------------------------------------------------------------------------


class ReferencePreview(QFrame):
    """Floating picture of the place a reference points to."""

    MAX_WIDTH = 600

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("referencePreview")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            f"QFrame#referencePreview {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px; }}"
            f"QLabel#previewTitle {{ color: {INK}; font-weight: 600; }} QLabel#previewHint {{ color: {FAINT}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        self.title = QLabel("")
        self.title.setObjectName("previewTitle")
        self.image = QLabel("")
        self.image.setStyleSheet("background: white;")
        self.hint = QLabel("单击跳转 · 跳转后用“‹ 返回”回来")
        self.hint.setObjectName("previewHint")
        layout.addWidget(self.title)
        layout.addWidget(self.image)
        layout.addWidget(self.hint)
        self.key: Any = None
        self.hide()

    def show_preview(self, key: Any, title: str, pixmap: QPixmap, anchor: QRectF, *, linked: bool) -> None:
        self.key = key
        self.title.setText(title)
        where = "" if linked else "（按编号在文档中找到的出处）"
        self.hint.setText(f"单击跳转{where} · Shift+单击钉住 · Ctrl/⌘+单击在分屏打开")
        self.image.setPixmap(pixmap)
        self.adjustSize()
        parent = self.parentWidget().rect()
        x = int(min(max(8.0, anchor.left()), max(8.0, parent.width() - self.width() - 8.0)))
        below = anchor.bottom() + 8
        if below + self.height() <= parent.height() - 8 or anchor.top() - self.height() - 8 < 8:
            y = int(min(below, max(8.0, parent.height() - self.height() - 8.0)))
        else:
            y = int(anchor.top() - self.height() - 8)
        self.move(x, y)
        self.show()
        self.raise_()

    def dismiss(self) -> None:
        self.key = None
        self.hide()


# -- continue reading banner ------------------------------------------------------------------


class ResumeBanner(QFrame):
    """"上次读到第 37 页 · 继续阅读" shown when a PDF opens somewhere else."""

    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resumeBanner")
        self.setStyleSheet(
            f"QFrame#resumeBanner {{ background: {AMBER_SOFT}; border: none; border-bottom: 1px solid {LINE}; }}"
            f"QLabel {{ color: {INK}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 4, 10, 4)
        layout.setSpacing(8)
        self.label = QLabel("")
        self.resume_button = tool_button("继续阅读", "跳到上次离开的位置")
        self.close_button = tool_button("×", "不用了")
        layout.addWidget(self.label, 1)
        layout.addWidget(self.resume_button)
        layout.addWidget(self.close_button)
        self.resume_button.clicked.connect(self._resume)
        self.close_button.clicked.connect(self.hide)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(20000)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    def offer(self, text: str) -> None:
        self.label.setText(text)
        self.show()
        self._hide_timer.start()

    def _resume(self) -> None:
        self.hide()
        self.resume_requested.emit()


# -- 定理 list ----------------------------------------------------------------------------------


class StatementList(QWidget):
    """Sidebar list of the document's theorems, definitions, examples... in reading order."""

    statement_activated = Signal(int, float)
    GROUPS = ("全部", "定理类", "定义", "例子", "注记", "习题", "图表", "公式")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QComboBox

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.setSpacing(6)
        self.filter_box = QComboBox()
        self.filter_box.addItems(self.GROUPS)
        self.filter_box.setCurrentIndex(1)
        self.list = QListWidget()
        self.list.setObjectName("readerSearchResults")
        self.list.setWordWrap(True)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.status = QLabel("正在整理本文档的定理、定义……")
        self.status.setStyleSheet(f"color: {MUTED}; padding: 2px;")
        self.status.setWordWrap(True)
        layout.addWidget(self.filter_box)
        layout.addWidget(self.status)
        layout.addWidget(self.list, 1)
        self._items: list[Any] = []
        self._page_names: Any = str
        self.filter_box.currentIndexChanged.connect(lambda _index: self._fill())
        self.list.itemClicked.connect(self._activate)

    def set_statements(self, items: list[Any] | None, page_names: Any) -> None:
        self._items = list(items or [])
        self._page_names = page_names
        if items is None:
            self.status.setText("正在整理本文档的定理、定义……")
            self.status.show()
            self.list.clear()
            return
        self._fill()

    def _fill(self) -> None:
        from shared.scripts.pdf_reader_tools import STATEMENT_GROUPS

        group = self.filter_box.currentText()
        self.list.clear()
        shown = [item for item in self._items if group == "全部" or STATEMENT_GROUPS.get(item.kind) == group]
        for target in shown[:3000]:
            body = target.text
            label = target.label
            if body.lower().startswith(label.lower()):
                body = body[len(label):].lstrip(" .:")
            row = QListWidgetItem(f"{label}　{self._page_names(target.page)}\n{body}".rstrip())
            row.setData(Qt.ItemDataRole.UserRole, (int(target.page), float(target.y)))
            self.list.addItem(row)
        if not self._items:
            self.status.setText("没有识别到带编号的定理、定义（扫描版要先识别文字）。")
            self.status.show()
        elif not shown:
            self.status.setText(f"本文档没有“{group}”。")
            self.status.show()
        else:
            self.status.setText(f"共 {len(shown)} 条，按出现顺序排列；点击跳转。")
            self.status.show()

    def _activate(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.statement_activated.emit(int(data[0]), float(data[1]))


# -- pinned clip --------------------------------------------------------------------------------


class PinnedClip(QWidget):
    """A small always-on-top window holding a region of the PDF (a definition, a formula)."""

    jump_requested = Signal(int, float)

    def __init__(self, pixmap: QPixmap, title: str, page_index: int, y: float, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(f"background: {SURFACE};")
        self._pixmap = pixmap
        self._page_index = int(page_index)
        self._y = float(y)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(3)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(80, 40)
        self.image.setSizePolicy(self.image.sizePolicy().Policy.Ignored, self.image.sizePolicy().Policy.Ignored)
        hint = QLabel("双击回到原文 · 拖动边框缩放")
        hint.setStyleSheet(f"color: {FAINT};")
        layout.addWidget(self.image, 1)
        layout.addWidget(hint)
        ratio = pixmap.devicePixelRatio() or 1.0
        width = min(720, int(pixmap.width() / ratio))
        height = int(width * pixmap.height() / max(1, pixmap.width()))
        self.resize(width + 12, height + 30)

    def resizeEvent(self, event: Any) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        ratio = self._pixmap.devicePixelRatio() or 1.0
        target = self.image.size() * ratio
        scaled = self._pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self.image.setPixmap(scaled)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # type: ignore[override]
        self.jump_requested.emit(self._page_index, self._y)


# -- copy LaTeX dialog ---------------------------------------------------------------------------


class LatexCopyDialog(QWidget):
    """Choose among the LaTeX candidates of a formula, edit, and copy."""

    def __init__(self, result: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Dialog)
        from PySide6.QtWidgets import QApplication, QPlainTextEdit

        self.setWindowTitle("复制 LaTeX")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        source = QLabel(result.source_note)
        source.setWordWrap(True)
        source.setStyleSheet(f"color: {MUTED};")
        self.list = QListWidget()
        self.list.setObjectName("readerSearchResults")
        self.list.setWordWrap(True)
        for candidate in result.candidates:
            item = QListWidgetItem(f"{candidate.label}\n{candidate.latex}")
            item.setData(Qt.ItemDataRole.UserRole, candidate.latex)
            self.list.addItem(item)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("选中上面一项，可以在这里修改后再复制")
        buttons = QHBoxLayout()
        self.copy_button = tool_button("复制", "复制下面框里的 LaTeX")
        self.close_button = tool_button("关闭", "关闭")
        buttons.addStretch(1)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.close_button)
        layout.addWidget(source)
        layout.addWidget(self.list, 2)
        layout.addWidget(self.editor, 1)
        layout.addLayout(buttons)
        self.list.currentItemChanged.connect(
            lambda item, _previous: self.editor.setPlainText(str(item.data(Qt.ItemDataRole.UserRole)) if item else "")
        )

        def copy() -> None:
            QApplication.clipboard().setText(self.editor.toPlainText())
            self.copy_button.setText("已复制")

        self.copy_button.clicked.connect(copy)
        self.close_button.clicked.connect(self.close)
        if self.list.count():
            self.list.setCurrentRow(0)
