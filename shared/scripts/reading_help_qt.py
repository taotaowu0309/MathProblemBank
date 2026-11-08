"""Qt widgets of the PDF reader: reading-help panel, mark overlay, selection bar, speech bar.

``QtPdfPreviewWindow`` in ``problem_bank_center_qt`` composes these widgets;
data goes through ``reading_help_service.ReadingHelpService`` only.
"""

from __future__ import annotations

import html
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# -- palette -----------------------------------------------------------------

INK = "#1F2933"
MUTED = "#616E7C"
FAINT = "#9AA5B1"
LINE = "#E4E7EB"
CANVAS = "#F0F2F5"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#F7F8FA"
ACCENT = "#2F6FDB"
ACCENT_SOFT = "#E8F0FD"
AMBER = "#B7791F"
AMBER_SOFT = "#FDF3E1"
GREEN = "#2F855A"
RED = "#C53030"

READER_STYLE = f"""
QWidget#pdfReader {{ background: {CANVAS}; }}
QFrame#readerToolbar {{ background: {SURFACE}; border: none; border-bottom: 1px solid {LINE}; }}
QToolButton#readerTool {{
    border: none; border-radius: 6px; padding: 5px 9px; color: {INK}; background: transparent;
}}
QToolButton#readerTool:hover {{ background: {SURFACE_ALT}; }}
QToolButton#readerTool:pressed {{ background: {LINE}; }}
QToolButton#readerTool:checked {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QToolButton#readerTool:disabled {{ color: {FAINT}; }}
QLabel#readerTitle {{ color: {INK}; }}
QLabel#readerMeta {{ color: {MUTED}; }}
QFrame#readerSeparator {{ background: {LINE}; }}
QTreeWidget#readerOutline {{
    background: {SURFACE_ALT}; border: none; border-right: 1px solid {LINE}; padding: 6px 2px; color: {INK};
}}
QTreeWidget#readerOutline::item {{ padding: 4px 2px; border-radius: 4px; }}
QTreeWidget#readerOutline::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QTreeWidget#readerOutline::item:hover {{ background: {LINE}; }}
QPdfView, QAbstractScrollArea#readerPdfView {{ background: {CANVAS}; border: none; }}
"""


def tool_button(text: str, tooltip: str = "", *, checkable: bool = False) -> QToolButton:
    button = QToolButton()
    button.setObjectName("readerTool")
    button.setText(text)
    button.setToolTip(tooltip or text)
    button.setCheckable(checkable)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    font = button.font()
    font.setPointSizeF(max(10.0, font.pointSizeF()))
    button.setFont(font)
    return button


def toolbar_separator() -> QFrame:
    line = QFrame()
    line.setObjectName("readerSeparator")
    line.setFixedSize(1, 20)
    return line


# -- overlay of stored marks -----------------------------------------------------


class MarkOverlay(QWidget):
    """Draws stored "I don't understand" marks under the live selection."""

    COLORS = {
        "pending": (QColor(245, 176, 65, 70), QColor(214, 140, 30, 0)),
        "reexplain": (QColor(245, 176, 65, 70), QColor(214, 140, 30, 0)),
        "explained": (QColor(66, 133, 244, 46), QColor(47, 111, 219, 0)),
    }

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.items: list[tuple[int, str, list[QRectF]]] = []
        self.active_id: int | None = None
        self.hide()

    def set_items(self, items: list[tuple[int, str, list[QRectF]]], active_id: int | None) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.items = items
        self.active_id = active_id
        self.setVisible(bool(items))
        self.update()

    def paintEvent(self, _event: QEvent) -> None:  # type: ignore[override]
        if not self.items:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for mark_id, status, rects in self.items:
            fill, _edge = self.COLORS.get(status, self.COLORS["pending"])
            active = mark_id == self.active_id
            # Resting marks are only underlined; the selected one also gets a light box.
            if active:
                painter.setBrush(fill)
                painter.setPen(Qt.PenStyle.NoPen)
                for rect in rects:
                    painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 2.5, 2.5)
            # A thin underline keeps marks visible on dense pages.
            underline = QColor(214, 140, 30) if status != "explained" else QColor(47, 111, 219)
            underline.setAlpha(230 if active else 150)
            painter.setPen(QPen(underline, 2.0 if active else 1.3))
            for rect in rects:
                y = rect.bottom() + 1.0
                painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))


# -- floating bar beside a selection -------------------------------------------------


class SelectionActionBar(QFrame):
    """Small floating toolbar shown after a text selection."""

    mark_requested = Signal()
    sentence_requested = Signal()
    paragraph_requested = Signal()
    copy_requested = Signal()
    pin_requested = Signal()
    image_requested = Signal()
    latex_requested = Signal()
    lookup_requested = Signal()
    table_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("selectionBar")
        self.setStyleSheet(
            f"QFrame#selectionBar {{ background: {INK}; border-radius: 8px; }}"
            "QToolButton { color: #F5F7FA; border: none; padding: 6px 10px; border-radius: 6px; background: transparent; }"
            "QToolButton:hover { background: #3E4C59; }"
            f"QToolButton#primary {{ color: #FFD58A; }}"
            "QFrame#barLine { background: #52606D; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(0)
        self.mark_button = self._button("标记不懂", self.mark_requested, primary=True)
        self.text_line = self._line()
        self.sentence_button = self._button("扩到整句", self.sentence_requested)
        self.paragraph_button = self._button("扩到整段", self.paragraph_requested)
        self.copy_line = self._line()
        self.copy_button = self._button("复制", self.copy_requested)
        self.lookup_button = self._button("查词", self.lookup_requested)
        self.lookup_button.setToolTip("查这个词或短语：词汇库 → 学科术语表 → 本地词典")
        self.table_button = self._button("复制表格", self.table_requested)
        self.table_button.setToolTip("把框里的表格复制成可以直接粘进 Excel 的表格文字")
        self.latex_button = self._button("复制 LaTeX", self.latex_requested)
        self.latex_button.setToolTip("复制这里公式的 LaTeX 源码（讲义取自 TeX 源文件，教材取自 MinerU 识别结果）")
        self.image_button = self._button("截图", self.image_requested)
        self.image_button.setToolTip("把这块区域复制为图片")
        self.pin_button = self._button("钉住", self.pin_requested)
        self.pin_button.setToolTip("把这块区域放进置顶小窗，往后读时一直看得到")
        for widget in (
            self.mark_button, self.text_line, self.sentence_button, self.paragraph_button, self.copy_line,
            self.copy_button, self.lookup_button, self.latex_button, self.table_button, self.image_button, self.pin_button,
        ):
            layout.addWidget(widget)
        self.hide()

    def set_region_mode(self, region: bool) -> None:
        """A dragged rectangle (Alt+drag) has no text to mark: offer only picture and formula actions."""

        for widget in (self.mark_button, self.text_line, self.sentence_button, self.paragraph_button, self.copy_line, self.copy_button):
            widget.setVisible(not region)
        self.table_button.setVisible(region)

    def set_lookup_visible(self, visible: bool) -> None:
        self.lookup_button.setVisible(visible)

    def _button(self, text: str, signal: Any, *, primary: bool = False) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        if primary:
            button.setObjectName("primary")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(signal.emit)
        return button

    @staticmethod
    def _line() -> QFrame:
        line = QFrame()
        line.setObjectName("barLine")
        line.setFixedSize(1, 18)
        return line

    def place_near(self, anchor: QRectF, viewport: QWidget) -> None:
        self.adjustSize()
        width, height = self.width(), self.height()
        x = anchor.left()
        y = anchor.bottom() + 8
        if y + height > viewport.height() - 8:
            y = anchor.top() - height - 8
        x = max(8.0, min(x, viewport.width() - width - 8.0))
        y = max(8.0, min(y, viewport.height() - height - 8.0))
        self.move(int(x), int(y))
        self.show()
        self.raise_()


# -- speech bar ------------------------------------------------------------------------


class SpeechBar(QFrame):
    """Play/pause, stop and speed for PDF read-aloud, shown while speech is active."""

    SPEEDS = ("0.75×", "0.9×", "1.0×", "1.15×", "1.3×")

    def __init__(self, owner: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.owner = owner
        self.setObjectName("speechBar")
        self.setStyleSheet(
            f"QFrame#speechBar {{ background: {SURFACE}; border: none; border-top: 1px solid {LINE}; }}"
            f"QLabel {{ color: {MUTED}; }}"
            f"QLabel#speechState {{ color: {GREEN}; background: #E6F4EA; border-radius: 9px; padding: 2px 9px; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(8)
        title = QLabel("朗读")
        title.setStyleSheet(f"color: {INK}; font-weight: 600;")
        layout.addWidget(title)
        self.state_label = QLabel("声音已开启")
        self.state_label.setObjectName("speechState")
        layout.addWidget(self.state_label)
        self.play_button = tool_button("暂停", "暂停 / 继续朗读")
        self.stop_button = tool_button("停止", "停止朗读")
        layout.addWidget(self.play_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(QLabel("速度"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(self.SPEEDS)
        self.speed_combo.setCurrentIndex(2)
        layout.addWidget(self.speed_combo)
        self.status_label = QLabel("")
        layout.addWidget(self.status_label, 1)
        self.hide_button = tool_button("收起", "收起朗读控制条")
        layout.addWidget(self.hide_button)
        self.play_button.clicked.connect(lambda: self._call("toggle_pdf_speech"))
        self.stop_button.clicked.connect(lambda: self._call("stop_pdf_speech"))
        self.hide_button.clicked.connect(self.hide)
        self.speed_combo.activated.connect(self._speed_selected)
        self.hide()

    def _call(self, name: str) -> None:
        callback = getattr(self.owner, name, None)
        if callable(callback):
            callback()
        else:
            self.set_status("当前窗口未提供播放控制。")

    def _speed_selected(self, _index: int) -> None:
        try:
            speed = float(self.speed_combo.currentText().replace("×", ""))
        except ValueError:
            return
        callback = getattr(self.owner, "set_pdf_speech_speed", None)
        if callable(callback):
            callback(speed)
        self.set_status(f"倍速 {speed:g}×（下一句起生效）")

    def set_status(self, message: str) -> None:
        text = str(message or "")
        self.status_label.setText(text)
        if "暂停" in text:
            self._state("已暂停", AMBER, AMBER_SOFT)
            self.play_button.setText("继续")
        elif any(token in text for token in ("停止", "未播放", "失败", "完成")):
            self._state("声音已开启", GREEN, "#E6F4EA")
            self.play_button.setText("播放")
        elif "正在" in text or "继续" in text or "开始" in text:
            self._state("正在朗读", ACCENT, ACCENT_SOFT)
            self.play_button.setText("暂停")
            self.show()

    def _state(self, text: str, color: str, background: str) -> None:
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color}; background: {background}; border-radius: 9px; padding: 2px 9px;")


# -- explanation rendering --------------------------------------------------------------


STATUS_TEXT = {"pending": "待讲解", "reexplain": "待重新讲解", "explained": "已讲解"}
SCOPE_TEXT = {"selection": "选段", "sentence": "句子", "paragraph": "段落"}


def _escape(text: str) -> str:
    return html.escape(str(text or "")).replace("\n", "<br>")


def explanation_html(mark: dict[str, Any]) -> str:
    """Rich-text explanation for one mark, or its waiting state."""

    status = str(mark.get("status") or "pending")
    color = ACCENT if status == "explained" else AMBER
    background = ACCENT_SOFT if status == "explained" else AMBER_SOFT
    chips = (
        f"<span style='color:{color};background:{background};'>&nbsp;{STATUS_TEXT.get(status, status)}&nbsp;</span>"
        f"&nbsp;&nbsp;<span style='color:{MUTED};'>{SCOPE_TEXT.get(str(mark.get('scope')), '')} · 第 {int(mark.get('page_number') or 1)} 页</span>"
    )
    if mark.get("located") == "lost":
        chips += f"&nbsp;&nbsp;<span style='color:{RED};'>PDF 改动后找不到原位置</span>"
    parts = [f"<p style='margin:0 0 10px 0;'>{chips}</p>"]
    parts.append(
        f"<p style='margin:0 0 14px 0;color:{INK};font-family:Georgia,\"Times New Roman\",serif;font-size:15px;line-height:150%;'>"
        f"{_escape(mark.get('text'))}</p>"
    )
    if mark.get("question"):
        parts.append(f"<p style='margin:0 0 12px 0;color:{MUTED};'>你的提问：{_escape(mark['question'])}</p>")
    explanation = mark.get("explanation")
    if not explanation:
        parts.append(
            f"<p style='color:{MUTED};line-height:160%;'>还没有讲解。对 Work 说："
            f"<br><b style='color:{INK};'>讲解《{_escape(mark.get('document_title') or '这份讲义')}》中标记为不懂的句子</b>"
            "<br>讲解导入后，这里会显示中文意思、句子主干、各成分和词组。</p>"
        )
        return "".join(parts)
    if status == "reexplain":
        parts.append(f"<p style='color:{AMBER};'>已请求重新讲解；下面是上一版讲解。</p>")
    if explanation.get("answer"):
        parts.append(_section("回答你的提问", _escape(explanation["answer"])))
    if explanation.get("gist"):
        parts.append(_section("这一段在说什么", _escape(explanation["gist"])))
    sentences = explanation.get("sentences") or []
    for index, sentence in enumerate(sentences, start=1):
        block = []
        if len(sentences) > 1:
            block.append(
                f"<p style='margin:16px 0 4px 0;color:{FAINT};font-size:12px;'>第 {index} 句</p>"
                f"<p style='margin:0 0 8px 0;color:{INK};font-family:Georgia,serif;'>{_escape(sentence.get('text'))}</p>"
            )
        block.append(
            f"<p style='margin:0 0 10px 0;color:{INK};font-size:15px;line-height:160%;'>{_escape(sentence.get('translation'))}</p>"
        )
        block.append(
            f"<p style='margin:0 0 4px 0;color:{FAINT};font-size:12px;'>句子主干</p>"
            f"<p style='margin:0 0 10px 0;color:{INK};'>{_escape(sentence.get('skeleton'))}</p>"
        )
        structures = sentence.get("structures") or []
        if structures:
            rows = "".join(
                "<tr>"
                f"<td style='padding:3px 8px 3px 0;color:{INK};font-family:Georgia,serif;vertical-align:top;'>{_escape(part.get('part'))}</td>"
                f"<td style='padding:3px 8px 3px 0;vertical-align:top;white-space:nowrap;'><span style='color:#5B3CC4;background:#EFEAFD;'>&nbsp;{_escape(part.get('role'))}&nbsp;</span></td>"
                f"<td style='padding:3px 0;color:{MUTED};vertical-align:top;'>{_escape(part.get('explanation'))}</td>"
                "</tr>"
                for part in structures
            )
            block.append(
                f"<p style='margin:0 0 4px 0;color:{FAINT};font-size:12px;'>结构拆解</p>"
                f"<table cellspacing='0' cellpadding='0' style='margin:0 0 10px 0;'>{rows}</table>"
            )
        phrases = sentence.get("phrases") or []
        if phrases:
            items = "".join(
                f"<p style='margin:0 0 5px 0;'><span style='color:{INK};font-family:Georgia,serif;'>{_escape(item.get('term'))}</span>"
                f"&nbsp;&nbsp;<span style='color:{INK};'>{_escape(item.get('meaning'))}</span>"
                + (f"<br><span style='color:{MUTED};font-size:12px;'>{_escape(item.get('note'))}</span>" if item.get("note") else "")
                + "</p>"
                for item in phrases
            )
            block.append(f"<p style='margin:0 0 4px 0;color:{FAINT};font-size:12px;'>词和短语</p>{items}")
        if sentence.get("math_note"):
            block.append(
                f"<p style='margin:8px 0 4px 0;color:{FAINT};font-size:12px;'>数学上在说什么</p>"
                f"<p style='margin:0 0 6px 0;color:{MUTED};'>{_escape(sentence['math_note'])}</p>"
            )
        parts.append("".join(block))
    return "".join(parts)


def _section(title: str, body: str) -> str:
    return (
        f"<p style='margin:6px 0 4px 0;color:{FAINT};font-size:12px;'>{html.escape(title)}</p>"
        f"<p style='margin:0 0 12px 0;color:{INK};line-height:160%;'>{body}</p>"
    )


# -- right-hand panel --------------------------------------------------------------------


class ReadingHelpPanel(QWidget):
    """"中文辅助" panel: the selected mark's explanation plus every mark of this PDF."""

    mark_activated = Signal(int)
    reexplain_requested = Signal(int)
    delete_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("readingHelpPanel")
        self.setMinimumWidth(300)
        self.setStyleSheet(
            f"QWidget#readingHelpPanel {{ background: {SURFACE}; border-left: 1px solid {LINE}; }}"
            f"QLabel#panelTitle {{ color: {INK}; }}"
            f"QLabel#panelHint {{ color: {MUTED}; }}"
            f"QTextBrowser {{ border: none; background: {SURFACE}; color: {INK}; }}"
            f"QListWidget {{ border: none; background: {SURFACE_ALT}; color: {INK}; outline: 0; }}"
            f"QListWidget::item {{ padding: 7px 8px; border-bottom: 1px solid {LINE}; }}"
            f"QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {INK}; }}"
            f"QPushButton#filter {{ border: 1px solid {LINE}; border-radius: 11px; padding: 2px 10px; color: {MUTED}; background: {SURFACE}; }}"
            f"QPushButton#filter:checked {{ border-color: {ACCENT}; color: {ACCENT}; background: {ACCENT_SOFT}; }}"
            f"QPushButton#panelAction {{ border: 1px solid {LINE}; border-radius: 6px; padding: 5px 10px; color: {INK}; background: {SURFACE}; }}"
            f"QPushButton#panelAction:hover {{ background: {SURFACE_ALT}; }}"
            f"QPushButton#panelDanger {{ border: none; padding: 5px 6px; color: {RED}; background: transparent; }}"
        )
        self.document_title = ""
        self.marks: list[dict[str, Any]] = []
        self.current_id: int | None = None
        self.filter = "all"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QVBoxLayout()
        header.setContentsMargins(16, 12, 16, 8)
        header.setSpacing(4)
        title = QLabel("中文辅助")
        title.setObjectName("panelTitle")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        header.addWidget(title)
        hint = QLabel("双击单词查词；拖选句子或段落后点“标记不懂”。")
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        header.addWidget(hint)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(16, 4, 12, 8)
        detail_layout.setSpacing(8)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setHtml(self._empty_html())
        detail_layout.addWidget(self.browser, 1)
        actions = QHBoxLayout()
        self.jump_button = QPushButton("回到原文")
        self.reexplain_button = QPushButton("还是不懂")
        self.delete_button = QPushButton("删除标记")
        for button in (self.jump_button, self.reexplain_button):
            button.setObjectName("panelAction")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        self.delete_button.setObjectName("panelDanger")
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        actions.addWidget(self.delete_button)
        detail_layout.addLayout(actions)
        splitter.addWidget(detail)

        list_box = QWidget()
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(0)
        self.count_label = QLabel("本文档的标记")
        self.count_label.setObjectName("panelHint")
        self.count_label.setContentsMargins(16, 10, 16, 0)
        list_layout.addWidget(self.count_label)
        filters = QHBoxLayout()
        filters.setContentsMargins(16, 6, 16, 8)
        filters.setSpacing(6)
        group = QButtonGroup(self)
        for key, text in (("all", "全部"), ("waiting", "待讲解"), ("explained", "已讲解")):
            button = QPushButton(text)
            button.setObjectName("filter")
            button.setCheckable(True)
            button.setChecked(key == "all")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, value=key: self._set_filter(value))
            group.addButton(button)
            filters.addWidget(button)
        filters.addStretch(1)
        list_layout.addLayout(filters)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.list.itemClicked.connect(self._on_item_clicked)
        list_layout.addWidget(self.list, 1)
        splitter.addWidget(list_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.jump_button.clicked.connect(lambda: self.current_id is not None and self.mark_activated.emit(self.current_id))
        self.reexplain_button.clicked.connect(lambda: self.current_id is not None and self.reexplain_requested.emit(self.current_id))
        self.delete_button.clicked.connect(lambda: self.current_id is not None and self.delete_requested.emit(self.current_id))
        self._update_actions()

    @staticmethod
    def _empty_html() -> str:
        return (
            f"<p style='color:{MUTED};line-height:170%;'>点一处<span style='color:{AMBER};'>黄色</span>或"
            f"<span style='color:{ACCENT};'>蓝色</span>标记查看讲解。</p>"
            f"<p style='color:{MUTED};line-height:170%;'>黄色：已标记，等待 Work 讲解。<br>蓝色：讲解已导入，永久保存在本地。</p>"
        )

    def set_document(self, title: str, marks: list[dict[str, Any]]) -> None:
        self.document_title = title
        self.marks = marks
        if self.current_id is not None and not any(int(mark["id"]) == self.current_id for mark in marks):
            self.current_id = None
        self._refill()
        self.show_mark(self.current_id)

    def _set_filter(self, value: str) -> None:
        self.filter = value
        self._refill()

    def _visible(self, mark: dict[str, Any]) -> bool:
        if self.filter == "waiting":
            return mark["status"] in ("pending", "reexplain")
        if self.filter == "explained":
            return mark["status"] == "explained"
        return True

    def _refill(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        waiting = sum(1 for mark in self.marks if mark["status"] in ("pending", "reexplain"))
        explained = sum(1 for mark in self.marks if mark["status"] == "explained")
        self.count_label.setText(f"本文档 {len(self.marks)} 处 · 待讲解 {waiting} · 已讲解 {explained}")
        for mark in self.marks:
            if not self._visible(mark):
                continue
            dot = "●"
            text = str(mark["text"])
            snippet = text if len(text) <= 90 else text[:88] + "…"
            lost = "  · 位置失效" if mark.get("located") == "lost" else ""
            item = QListWidgetItem(f"{dot} 第 {mark['page_number']} 页 · {STATUS_TEXT.get(mark['status'], '')}{lost}\n{snippet}")
            item.setData(Qt.ItemDataRole.UserRole, int(mark["id"]))
            item.setForeground(QColor(INK))
            item.setToolTip(text)
            self.list.addItem(item)
            if int(mark["id"]) == self.current_id:
                item.setSelected(True)
        self.list.blockSignals(False)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        mark_id = item.data(Qt.ItemDataRole.UserRole)
        if mark_id is not None:
            self.mark_activated.emit(int(mark_id))

    def show_mark(self, mark_id: int | None) -> None:
        self.current_id = mark_id
        mark = next((item for item in self.marks if int(item["id"]) == mark_id), None) if mark_id is not None else None
        if mark is None:
            self.current_id = None
            self.browser.setHtml(self._empty_html())
        else:
            self.browser.setHtml(explanation_html({**mark, "document_title": self.document_title}))
            for index in range(self.list.count()):
                item = self.list.item(index)
                item.setSelected(item.data(Qt.ItemDataRole.UserRole) == mark_id)
        self._update_actions()

    def _update_actions(self) -> None:
        mark = next((item for item in self.marks if int(item["id"]) == self.current_id), None)
        for button in (self.jump_button, self.delete_button):
            button.setVisible(mark is not None)
        self.reexplain_button.setVisible(mark is not None and mark["status"] == "explained")


def word_lookup_card_style() -> str:
    return (
        f"QFrame#pdfVocabularyPopup {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 10px; }}"
        "QLabel { background: transparent; }"
    )


def source_chip(text: str, kind: str) -> str:
    colors = {
        "vocabulary": (GREEN, "#E6F4EA"),
        "dictionary": (ACCENT, ACCENT_SOFT),
        "glossary": ("#6B46C1", "#F1EBFB"),
        "none": (MUTED, SURFACE_ALT),
    }
    color, background = colors.get(kind, colors["none"])
    return f"color: {color}; background: {background}; border-radius: 7px; padding: 1px 7px;"


Callback = Callable[..., Any]
