"""Shared frame of the English workspace's book method pages (文法, 阅读方法, 写作方法).

The left column lists a book's sections with the learner's progress; the
middle shows one section; the right column is the textbook-notes panel of the
PDF reader, linked to the book's bookmarks, so a note written here is the same
note the reader shows.  A subclass supplies the service calls, fills the
outline tree and renders a section; everything else — the progress bar, the
read toggle, opening the book and the notes panel — lives here.

All data comes from :class:`EnglishLearningService`; this module owns no
database logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

ACCENT = "#017EDA"
READ_GREEN = "#2e9e5a"

# Glass panels like the rest of the control center: a translucent outline
# panel on the left, transparent scroll areas so the cards float on the
# background, and the bank's accent for the current section.
VIEW_STYLE = f"""
QWidget#readingMethodView, QStackedWidget#readingMethodStack, QWidget#readingMethodHost,
QScrollArea#readingMethodScroll, QScrollArea#readingMethodScroll > QWidget > QWidget {{
    background: transparent; border: 0;
}}
QFrame#readingMethodNav {{
    background: rgba(255, 255, 255, 150); border: 1px solid rgba(255, 255, 255, 196); border-radius: 18px;
}}
QFrame#readingMethodHeader {{
    background: rgba(255, 255, 255, 168); border: 1px solid rgba(255, 255, 255, 206); border-radius: 18px;
}}
QTreeWidget#readingMethodOutline {{
    background: transparent; border: 0; outline: 0; color: #263746; show-decoration-selected: 0;
}}
QTreeWidget#readingMethodOutline::item {{
    padding: 5px 4px; border: 0;
}}
QTreeWidget#readingMethodOutline::item:hover {{ background: rgba(1, 126, 218, 18); }}
QTreeWidget#readingMethodOutline::item:selected {{ background: rgba(1, 126, 218, 34); color: {ACCENT}; }}
QTreeWidget#readingMethodOutline::branch,
QTreeWidget#readingMethodOutline::branch:selected,
QTreeWidget#readingMethodOutline::branch:hover {{ background: transparent; border: 0; image: none; }}
QWidget#readingMethodNotes {{
    background: rgba(255, 255, 255, 150); border: 1px solid rgba(255, 255, 255, 196); border-radius: 18px;
}}
QWidget#readingMethodNotes QListWidget {{
    background: rgba(255, 255, 255, 170); border: 1px solid rgba(255, 255, 255, 210); border-radius: 10px;
    padding: 4px; outline: 0;
}}
QWidget#readingMethodNotes QListWidget::item {{ padding: 4px 6px; border-radius: 6px; }}
QWidget#readingMethodNotes QListWidget::item:selected {{ background: rgba(1, 126, 218, 34); color: {ACCENT}; }}
QWidget#readingMethodNotes QPdfView, QWidget#readingMethodNotes QPdfView > QWidget {{
    background: rgba(240, 244, 248, 210); border: 0; border-radius: 10px;
}}
QProgressBar#readingMethodProgress {{
    background: rgba(1, 126, 218, 26); border: 0; border-radius: 3px; max-height: 6px;
}}
QProgressBar#readingMethodProgress::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QLabel#readingMethodBreadcrumb {{ color: #6b7c8f; }}
QLabel#readingMethodExampleTag {{
    background: {ACCENT}; color: white; border-radius: 10px; padding: 3px 14px;
}}
QPushButton#secondaryButton[read="true"] {{
    color: {READ_GREEN}; border-color: rgba(46, 158, 90, 140); background: rgba(46, 158, 90, 26);
}}
"""


def _center() -> Any:
    # The control center imports this module lazily; import it the same way.
    from shared.scripts import problem_bank_center_qt

    return problem_bank_center_qt


def _scroll(host: QWidget) -> QScrollArea:
    host.setObjectName("readingMethodHost")
    scroll = QScrollArea()
    scroll.setObjectName("readingMethodScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(host)
    return scroll


def _release_widget(widget: QWidget) -> None:
    # deleteLater alone leaves the widget on screen until the event loop.
    # A 范例 label that has not been given a layout rect yet keeps Qt's default
    # 640×480 size, so its accent fill paints the solid blue block.
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


def _clear(layout: QLayout, *, sweep_host: bool = True) -> None:
    """Remove widgets and nested layouts.

    Example rows are ``addLayout`` rows, not widgets. ``takeAt`` on that row
    drops the layout item and leaves the 范例 label and 分屏练习 button as
    children of the page, which is the overlap after switching sections.
    """

    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        nested = item.layout()
        if widget is not None:
            _release_widget(widget)
        elif nested is not None:
            _clear(nested, sweep_host=False)
    if not sweep_host:
        return
    host = layout.parentWidget()
    if host is None:
        return
    for child in list(host.children()):
        if isinstance(child, QWidget):
            _release_widget(child)
    host.update()


def _button(text: str, slot: Callable[[], None], tooltip: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("secondaryButton")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedHeight(32)
    if tooltip:
        button.setToolTip(tooltip)
    button.clicked.connect(slot)
    return button


class MethodBookView(QWidget):
    """Outline | section | notes.  Also the "reader" the notes panel follows:
    it reports the current section's page of the book PDF."""

    BOOK_TITLE = ""
    NAV_TITLE = "目录"
    # Attribute on the control center that remembers the open section across rebuilds.
    OWNER_SECTION_ATTRIBUTE = ""

    def __init__(self, owner: Any) -> None:
        super().__init__()
        self.owner = owner
        self.service = owner.english_service
        self.outline_data: dict[str, Any] = {}
        self.section: dict[str, Any] | None = None
        self.section_key = str(getattr(owner, self.OWNER_SECTION_ATTRIBUTE, "") or "")
        self.pdf_path: Path | None = None
        self.notes_panel: Any = None
        center = _center()
        self.center = center

        self.setObjectName("readingMethodView")
        self.setStyleSheet(VIEW_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.notes_button = _button("笔记", self.toggle_notes, "显示或收起右侧的教材笔记（链接到原书书签，与 PDF 阅读器中的笔记相同）")
        self.notes_button.setCheckable(True)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(14)
        self.splitter.setStyleSheet("QSplitter::handle { background: transparent; }"
                                    "QSplitter::handle:hover { background: rgba(1,126,218,30); border-radius: 4px; }")
        # Left: the book's outline with the reading progress.
        nav = QFrame()
        nav.setObjectName("readingMethodNav")
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(14, 16, 10, 12)
        nav_layout.setSpacing(6)
        nav_title = QLabel(self.NAV_TITLE)
        center.set_font(nav_title, 13, QFont.Weight.DemiBold)
        nav_layout.addWidget(nav_title)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("pageNote")
        self.progress_label.setWordWrap(True)
        center.set_font(self.progress_label, 10)
        nav_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("readingMethodProgress")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        nav_layout.addWidget(self.progress_bar)
        nav_layout.addSpacing(6)
        self.tree = QTreeWidget()
        self.tree.setObjectName("readingMethodOutline")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setRootIsDecorated(False)
        self.tree.setItemsExpandable(False)
        self.tree.setIndentation(16)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.header().resizeSection(1, 26)
        center.set_font(self.tree, 10)
        self.tree.currentItemChanged.connect(lambda current, _previous: self._open_item(current))
        nav_layout.addWidget(self.tree, 1)
        nav.setMinimumWidth(250)
        nav.setMaximumWidth(380)
        self.splitter.addWidget(nav)

        self.stack = QStackedWidget()
        self.stack.setObjectName("readingMethodStack")
        # Page 0: the section.
        section_host = QWidget()
        self.section_layout = QVBoxLayout(section_host)
        self.section_layout.setContentsMargins(0, 0, 8, 12)
        self.section_layout.setSpacing(14)
        self.section_scroll = _scroll(section_host)
        self.stack.addWidget(self.section_scroll)
        self._add_pages()
        self.splitter.addWidget(self.stack)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([270, 1000])
        layout.addWidget(self.splitter, 1)
        self.setMinimumHeight(620)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # -- what a book page supplies -------------------------------------------------

    def _add_pages(self) -> None:
        """Further pages of the stack after the section (page 0)."""

    def _outline(self) -> dict[str, Any]:
        raise NotImplementedError

    def _section(self, key: str) -> dict[str, Any]:
        raise NotImplementedError

    def _set_read(self, key: str, read: bool) -> dict[str, Any]:
        raise NotImplementedError

    def _fill_tree(self) -> None:
        raise NotImplementedError

    def _render_section(self) -> None:
        raise NotImplementedError

    # -- the notes panel's "reader" protocol -------------------------------------

    def current_page(self) -> int:
        page = int((self.section or {}).get("source_page") or 1)
        return max(0, page - 1)

    def windowTitle(self) -> str:  # noqa: N802 - the PDF reader's API
        return str((self.outline_data or {}).get("book_title") or self.BOOK_TITLE)

    def jump_to_page(self, page_index: int) -> None:
        self.open_book(int(page_index) + 1)

    def notes_panel_visibility_changed(self) -> None:
        panel = self.notes_panel
        self.notes_button.setChecked(bool(panel is not None and panel.is_shown()))

    # -- loading ----------------------------------------------------------------

    def load(self) -> None:
        try:
            self.outline_data = self._outline()
        except Exception as error:
            self.progress_label.setText(f"读取《{self.BOOK_TITLE}》失败：{error}")
            return
        pdf = str(self.outline_data.get("pdf_path") or "")
        self.pdf_path = Path(pdf) if pdf else None
        self._fill_tree()
        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        elif self.section is None and self.tree.currentItem() is not None and self.section_key:
            # _fill_tree() restores the previous selection while signals are
            # blocked. On a rebuilt page that means currentItemChanged does
            # not fire, leaving the section pane empty on Windows. Explicitly
            # load the restored section after the tree has been populated.
            self.open_section(self.section_key)

    def refresh_outline(self) -> None:
        """Re-read the progress (after an answer or marking a section read)."""
        try:
            self.outline_data = self._outline()
        except Exception:
            return
        self._fill_tree()

    def _select_tree_item(self, key: str) -> None:
        iterator = [self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        while iterator:
            item = iterator.pop(0)
            if item.data(0, Qt.ItemDataRole.UserRole) == key:
                if self.tree.currentItem() is not item:
                    self.tree.blockSignals(True)
                    self.tree.setCurrentItem(item)
                    self.tree.blockSignals(False)
                return
            iterator.extend(item.child(index) for index in range(item.childCount()))

    def _open_item(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        key = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if key:
            self.open_section(key)

    def open_section(self, key: str) -> None:
        try:
            self.section = self._section(key)
        except Exception as error:
            QMessageBox.warning(self, f"读取《{self.BOOK_TITLE}》失败", str(error))
            return
        self.section_key = key
        setattr(self.owner, self.OWNER_SECTION_ATTRIBUTE, key)
        self._select_tree_item(key)
        self.stack.setCurrentIndex(0)
        self._render_section()
        panel = self.notes_panel
        if panel is not None and panel.is_shown():
            panel.refresh(force=True)

    def _read_button(self) -> QPushButton:
        read = bool((self.section or {}).get("read"))
        self.read_button = _button("已读 ✓" if read else "标为已读", self.toggle_read,
                                   "记下这一节已经读完（学习进度）；再点一次取消")
        self.read_button.setObjectName("secondaryButton")
        self.read_button.setProperty("read", read)
        return self.read_button

    def _step_row(self, section: dict[str, Any]) -> QHBoxLayout:
        """← 上一节 / 下一节 → at the foot of a section, in book order."""
        row = QHBoxLayout()
        row.setContentsMargins(4, 0, 4, 0)
        previous_key, next_key = str(section.get("previous_key") or ""), str(section.get("next_key") or "")
        if previous_key:
            row.addWidget(_button("← 上一节", lambda: self.open_section(previous_key)))
        row.addStretch(1)
        if next_key:
            row.addWidget(_button("下一节 →", lambda: self.open_section(next_key)))
        return row

    # -- actions ----------------------------------------------------------------

    def toggle_read(self) -> None:
        section = self.section or {}
        if not section:
            return
        try:
            result = self._set_read(section["key"], not bool(section.get("read")))
            if not result["readback_verified"]:
                raise RuntimeError("学习进度写入后回读不一致。")
        except Exception as error:
            QMessageBox.warning(self, "记录进度失败", str(error))
            return
        section["read"] = bool(result["read"])
        self.read_button.setText("已读 ✓" if section["read"] else "标为已读")
        self.read_button.setProperty("read", section["read"])
        self.read_button.style().unpolish(self.read_button)
        self.read_button.style().polish(self.read_button)
        self.refresh_outline()

    def open_book(self, page: int | None = None) -> None:
        pdf = self.pdf_path
        if pdf is None or not pdf.is_file():
            QMessageBox.information(self, "原书不在本机", f"没有找到《{self.BOOK_TITLE}》的 PDF。")
            return
        target = int(page or (self.section or {}).get("source_page") or 1)
        try:
            preview = self.owner.pdf_preview_for_path(pdf)
            preview.show_pdf_location(pdf, page_index=max(0, target - 1), anchor_y=0.0, title=self.windowTitle())
        except Exception as error:
            QMessageBox.critical(self, "内部阅读器打开失败", str(error))

    def toggle_notes(self) -> None:
        if self.pdf_path is None:
            self.notes_button.setChecked(False)
            QMessageBox.information(
                self, "原书不在本机", f"笔记链接到《{self.BOOK_TITLE}》PDF 的书签，本机没有找到这本书的 PDF。",
            )
            return
        panel = self.notes_panel
        if panel is None:
            from shared.scripts.textbook_notes_qt import TextbookNotesPanel

            panel = TextbookNotesPanel(self, self.splitter)
            panel.setObjectName("readingMethodNotes")
            panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            panel.layout().setContentsMargins(14, 14, 14, 14)
            panel.setMinimumWidth(380)
            # The note PDF sits on a light page background instead of Qt's dark grey.
            palette = panel.note_view.palette()
            for role in (QPalette.ColorRole.Dark, QPalette.ColorRole.Window, QPalette.ColorRole.Base):
                palette.setColor(role, QColor("#eef2f6"))
            panel.note_view.setPalette(palette)
            panel.note_view.viewport().setPalette(palette)
            self.notes_panel = panel
            self.splitter.addWidget(panel)
            panel.hide()
        if panel.is_floating():
            panel.dock(show=False)
        shown = not panel.isVisible()
        panel.setVisible(shown)
        if shown:
            sizes = self.splitter.sizes()
            total = sum(sizes) or self.width()
            self.splitter.setSizes([sizes[0] or 270, max(360, total - (sizes[0] or 270) - 420), 420])
            panel.refresh(force=True)
        self.notes_button.setChecked(shown)


__all__ = [
    "ACCENT",
    "MethodBookView",
    "READ_GREEN",
    "VIEW_STYLE",
]
