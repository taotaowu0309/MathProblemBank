"""The study page of one 广读 book: the passage on the left, explanations on the right.

The left column reads like a book: 读前导读 (author, background, the story so
far), the selected passage paragraph by paragraph, then 读后.  Words and
sentences the teacher explains are shaded; clicking one shows its
explanation.  The right column follows the paragraph being read: the
paragraph's explanation, the explanations anchored in it and the learner's
own notes on it; a second tab lists all notes.

Double-click a word to look it up.  When a selection settles (a mouse or
three-finger drag, shift-click, select-all), one offer appears that fits what
was selected: look up a word or phrase, or write a note on that phrase, that
sentence, those sentences, that paragraph, those paragraphs or the whole
passage.  Notes are stored by :class:`EnglishLearningService`
(``english_reading_notes``); this module only calls it.
"""

from __future__ import annotations

import html
import re
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QApplication,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.method_book_qt import ACCENT, _button, _scroll

SERIF = "'Charter', 'Georgia', 'Cambria', 'Times New Roman', 'Songti SC', 'SimSun', serif"
INK = "#1f2a33"
MUTED = "#6b7c8f"
PAPER = "#fbf8f1"
TEACHER = "#edf3f9"     # shading of words the teacher explains (light: the text must stay readable)
MINE = "#fbeec4"        # shading of the learner's notes
ACTIVE = "#f1d27a"      # the explanation or note being looked at
KIND_COLOURS = {"字义": "#2f6fb0", "文法": "#8a4fb5", "修辞": "#b5683a", "点评": "#3d8a5a", "要点": "#b03a48"}
SCOPE_NAMES = {"selection": "选中文字", "sentence": "一句", "paragraph": "段落", "article": "整篇"}

STUDY_STYLE = f"""
QTextBrowser#studyReader {{ background: {PAPER}; border: 1px solid #e7dfcf; border-radius: 16px; padding: 18px 30px; }}
QFrame#studyIntro {{ background: rgba(255,255,255,175); border: 1px solid rgba(255,255,255,210); border-radius: 14px; }}
QFrame#studySide {{ background: rgba(255,255,255,160); border: 1px solid rgba(255,255,255,206); border-radius: 18px; }}
QFrame#studyCard {{ background: white; border: 1px solid #e3e8ee; border-radius: 10px; }}
QFrame#studyCard[active="true"] {{ border: 2px solid #e2c36b; }}
QFrame#studyGist {{ background: transparent; border: 0; border-left: 3px solid {ACCENT}; border-radius: 0; }}
QFrame#studyMine {{ background: rgba(1,126,218,16); border: 0; border-left: 3px solid #e2c36b; border-radius: 0; }}
QFrame#studyMine[active="true"] {{ background: rgba(241,210,122,60); }}
QFrame#studyBar {{ background: #263746; border-radius: 10px; }}
QFrame#studyBar QPushButton {{ background: transparent; color: white; border: 0; padding: 4px 9px; border-radius: 6px; }}
QFrame#studyBar QPushButton:hover {{ background: rgba(255,255,255,40); }}
QLabel#studyKind {{ color: white; border-radius: 6px; padding: 0 6px; }}
"""

_SENTENCE_END = re.compile(r"[.!?](?:[\"”’)])?(?=\s|$)|[。！？](?:[\"”’）)])?")


def _center() -> Any:
    from shared.scripts import problem_bank_center_qt

    return problem_bank_center_qt


def inline(text: str) -> str:
    """The inline Markdown of the guides (bold, italic) and line breaks."""

    body = html.escape(str(text or ""), quote=False)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
    body = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", rf'<i style="font-family:{SERIF};">\1</i>', body)
    return body


def rich(text: str) -> str:
    """Paragraphs and '- ' lists of a guide text."""

    parts: list[str] = []
    for block in re.split(r"\n\s*\n", str(text or "")):
        lines = [line for line in block.splitlines() if line.strip()]
        if lines and all(line.lstrip().startswith("- ") for line in lines):
            parts.append("".join(f"<p style='margin:0 0 4px 0;'>· {inline(line.lstrip()[2:])}</p>" for line in lines))
        elif lines:
            parts.append(f"<p style='margin:0 0 8px 0;'>{'<br>'.join(inline(line) for line in lines)}</p>")
    return "".join(parts)


def sentence_range(text: str, position: int) -> tuple[int, int]:
    """The sentence of ``text`` that holds character ``position``."""

    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if position < end:
            while start < len(text) and text[start] == " ":
                start += 1
            return start, end
        start = end
    while start < len(text) and text[start] == " ":
        start += 1
    return start, len(text)


def paragraph_html(text: str, anchors: list[dict[str, Any]], notes: list[dict[str, Any]], active: tuple[str, int] | None,
                   index: int = 0) -> str:
    """The paragraph with the teacher's anchors (links ``a:i``), the learner's
    notes (links ``n:id``) and the active one shaded, run by run."""

    count = len(text)
    owner: list[tuple[str, int] | None] = [None] * count
    for number, anchor in enumerate(anchors):
        for position in range(anchor["start"], min(count, anchor["end"])):
            owner[position] = ("a", number)
    for note in notes:
        if note["start"] < 0:
            continue
        for position in range(max(0, note["start"]), min(count, note["end"])):
            owner[position] = ("n", int(note["id"]))
    # The one being looked at is drawn on top of everything else.
    if active is not None:
        span = (anchors[active[1]]["start"], anchors[active[1]]["end"]) if active[0] == "a" and active[1] < len(anchors) else next(
            ((note["start"], note["end"]) for note in notes if active[0] == "n" and int(note["id"]) == active[1]), None)
        if span:
            for position in range(max(0, span[0]), min(count, span[1])):
                owner[position] = active
    parts: list[str] = []
    position = 0
    while position < count:
        tag = owner[position]
        end = position
        while end < count and owner[end] == tag:
            end += 1
        # A line break inside a paragraph (dialogue) stays one character: <br>
        # becomes U+2028 in the document, so offsets still match the text.
        chunk = html.escape(text[position:end], quote=False).replace("\n", "<br>")
        if tag is None:
            parts.append(chunk)
        else:
            colour = ACTIVE if active == tag else (TEACHER if tag[0] == "a" else MINE)
            href = f"a:{index}:{tag[1]}" if tag[0] == "a" else f"n:{tag[1]}"
            parts.append(f"<a href='{href}' style='color:{INK}; text-decoration:none; "
                         f"background-color:{colour};'>{chunk}</a>")
        position = end
    # SERIF holds single quotes, so the style attribute uses double quotes.
    return (f'<p style="font-family:{SERIF}; font-size:15.5pt; line-height:175%; margin:14px 0 0 0;">'
            f"{''.join(parts)}</p>")


class NoteDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, quote: str, body: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 340)
        layout = QVBoxLayout(self)
        if quote:
            shown = QLabel(html.escape(quote if len(quote) < 400 else quote[:400] + "…"))
            shown.setWordWrap(True)
            shown.setStyleSheet(f"font-family:{SERIF}; color:{MUTED}; background:{PAPER}; padding:8px; border-radius:8px;")
            layout.addWidget(shown)
        self.editor = QPlainTextEdit(body)
        self.editor.setPlaceholderText("写下你的理解、疑问、联想……")
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.editor.setFocus()

    def text(self) -> str:
        return self.editor.toPlainText().strip()


class ClickFrame(QFrame):
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._click_timer.timeout.connect(self.clicked.emit)
        self._press_at: QPoint | None = None
        self._after_double_click = False

    def enable_text_lookup(self) -> None:
        for label in self.findChildren(QLabel):
            if label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse:
                label.installEventFilter(self)

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._press_at = event.position().toPoint()
            self._click_timer.stop()
        elif kind == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            self._after_double_click = True
            self._click_timer.stop()
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            start, self._press_at = self._press_at, None
            if self._after_double_click:
                self._after_double_click = False
            elif start is not None and (event.position().toPoint() - start).manhattanLength() <= 6:
                QTimer.singleShot(0, lambda label=watched: self._text_clicked(label))
        return False

    def _text_clicked(self, label: QLabel) -> None:
        from shiboken6 import isValid

        if isValid(label) and not label.hasSelectedText() and not label.property("lookupDismissed"):
            self._click_timer.start(QApplication.doubleClickInterval())

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            child = self.childAt(event.position().toPoint())
            if isinstance(child, QLabel) and child.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse:
                # QLabel's release can propagate here.  Its filter already distinguishes a click from lookup.
                event.accept()
                return
            self._click_timer.start(QApplication.doubleClickInterval())
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self._click_timer.stop()
        super().mouseDoubleClickEvent(event)


class StudyView(QWidget):
    """读前导读 → 原文 → 读后 | 讲解 / 我的笔记 for one book."""

    def __init__(self, owner: Any, back: Callable[[], None]) -> None:
        super().__init__()
        self.owner = owner
        self.service = owner.english_service
        self.center = _center()
        self.study: dict[str, Any] = {}
        self.notes: list[dict[str, Any]] = []
        self.active: tuple[str, int] | None = None
        self.groups: list[QWidget] = []
        self.cards: dict[tuple[str, int], QFrame] = {}
        self._following = True
        self._bar: QFrame | None = None
        self.setStyleSheet(STUDY_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        header = QFrame()
        header.setObjectName("readingMethodHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(16, 10, 16, 10)
        header_row.setSpacing(14)
        self.back_button = _button("← 书单", back)
        header_row.addWidget(self.back_button)
        self.cover_slot = QHBoxLayout()
        header_row.addLayout(self.cover_slot)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet(f"font-family:{SERIF};")
        self.center.set_font(self.title, 17, QFont.Weight.DemiBold)
        self.meta = QLabel()
        self.meta.setObjectName("pageNote")
        self.center.set_font(self.meta, 10)
        titles.addWidget(self.title)
        titles.addWidget(self.meta)
        header_row.addLayout(titles, 1)
        hint = QLabel("双击单词查词 · 选中文字可记笔记")
        hint.setObjectName("pageNote")
        self.center.set_font(hint, 9.5)
        header_row.addWidget(hint)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(14)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        # One text surface for 读前、原文、读后, so a selection can run across
        # sentences and paragraphs (a mouse or three-finger drag).
        self.reader = QTextBrowser()
        self.reader.setObjectName("studyReader")
        self.reader.setOpenLinks(False)
        self._anchor_timer = QTimer(self)
        self._anchor_timer.setSingleShot(True)
        self._anchor_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._anchor_timer.timeout.connect(self._activate_pending_anchor)
        self._pending_anchor = ""
        self.reader.anchorClicked.connect(lambda url: self._queue_anchor(url.toString()))
        self.reader.selectionChanged.connect(self._selection_changed)
        self.reader.verticalScrollBar().valueChanged.connect(lambda _value: self._follow())
        self.reader.viewport().installEventFilter(self)
        self.center.set_font(self.reader, 11)
        self._selection_timer = QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.setInterval(260)
        self._selection_timer.timeout.connect(self._selection_settled)
        self._double_clicked = False
        self.block_positions: list[int] = []
        splitter.addWidget(self.reader)

        side = QFrame()
        side.setObjectName("studySide")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(12, 12, 8, 12)
        side_layout.setSpacing(8)
        tabs = QHBoxLayout()
        tabs.setSpacing(5)
        self.tab_explain = self._tab("讲解", lambda: self._show_side(0))
        self.tab_notes = self._tab("我的笔记", lambda: self._show_side(1))
        tabs.addWidget(self.tab_explain)
        tabs.addWidget(self.tab_notes)
        tabs.addStretch(1)
        side_layout.addLayout(tabs)
        explain_host = QWidget()
        self.explain_layout = QVBoxLayout(explain_host)
        self.explain_layout.setContentsMargins(0, 0, 6, 0)
        self.explain_layout.setSpacing(10)
        self.explain_scroll = _scroll(explain_host)
        notes_host = QWidget()
        self.notes_layout = QVBoxLayout(notes_host)
        self.notes_layout.setContentsMargins(0, 0, 6, 0)
        self.notes_layout.setSpacing(10)
        self.notes_scroll = _scroll(notes_host)
        side_layout.addWidget(self.explain_scroll, 1)
        side_layout.addWidget(self.notes_scroll, 1)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([780, 520])
        layout.addWidget(splitter, 1)
        self._show_side(0)

    # -- building ---------------------------------------------------------------------

    def _tab(self, text: str, slot: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("extensiveChip")
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.setFixedHeight(28)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def _show_side(self, index: int) -> None:
        (self.tab_explain if index == 0 else self.tab_notes).setChecked(True)
        self.explain_scroll.setVisible(index == 0)
        self.notes_scroll.setVisible(index == 1)

    def open(self, episode: int | str) -> None:
        from shared.scripts.extensive_reading_qt import BookCover
        from shared.scripts.xuan_video_guides import study_stem

        self._anchor_timer.stop()
        self._pending_anchor = ""
        self.study = self.service.extensive_reading_study(episode)
        book = self.study["book"]
        while self.cover_slot.count():
            item = self.cover_slot.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.cover_slot.addWidget(BookCover(book, self.study.get("cover_code", study_stem(episode)), QSize(52, 74)))
        self.title.setText(f"{book['en']}　{book['zh']}")
        self.meta.setText(" · ".join(filter(None, [
            book.get("author"), book.get("year"), book.get("kind"), book.get("level"),
            "公版，可免费读全书" if book.get("public_domain") else "", self.study.get("passage_title"),
        ])))
        self.active = None
        self._load_notes()
        self.block_positions = []
        self.reader.verticalScrollBar().setValue(0)
        self._render()
        self._build_explanations()
        self._build_notes_tab()
        self.reader.verticalScrollBar().setValue(0)
        self.explain_scroll.verticalScrollBar().setValue(0)

    def _load_notes(self) -> None:
        try:
            self.notes = self.service.reading_notes(self.study["key"])
        except Exception as error:  # a broken database must not hide the text
            self.notes = []
            QMessageBox.warning(self, "读取笔记失败", str(error))
        self.tab_notes.setText(f"我的笔记 {len(self.notes)}" if self.notes else "我的笔记")

    @staticmethod
    def _clear(layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _section_html(self, title: str, entries: list[dict[str, Any]]) -> str:
        parts = [f"<h2 style='font-size:13pt; margin:4px 0 8px 0;'>{html.escape(title)}</h2>"]
        for entry in entries:
            parts.append(f"<p style='margin:10px 0 3px 0; color:{MUTED};'><b>{html.escape(entry.get('title', ''))}</b></p>")
            parts.append(rich(entry.get("text", "")))
        return "".join(parts)

    def _note_span(self, note: dict[str, Any], index: int) -> tuple[int, int] | None:
        """The part of paragraph ``index`` a note covers (notes may run over paragraphs)."""

        first, last = int(note["paragraph"]), int(note.get("paragraph_end", -1))
        last = first if last < first else last
        if note["scope"] == "article" or not first <= index <= last:
            return None
        length = len(self.study["paragraphs"][index]["text"])
        start = int(note["start"]) if index == first else 0
        end = int(note["end"]) if index == last and int(note["end"]) >= 0 else length
        return max(0, start), min(length, end)

    def _paragraph_notes(self, index: int) -> list[dict[str, Any]]:
        """Notes shown with paragraph ``index``: those that start in it."""
        return [note for note in self.notes if note["scope"] != "article" and int(note["paragraph"]) == index]

    def _render(self) -> None:
        """Rebuild the reading surface (shading follows notes and the active item)."""

        position = self.reader.verticalScrollBar().value()
        parts = ["<html><body style='color:%s;'>" % INK]
        if self.study.get("before"):
            parts.append(self._section_html("读前导读", self.study["before"]))
            parts.append("<hr style='color:#e7dfcf;'>")
        parts.append(f'<p style="font-family:{SERIF}; font-size:16pt; font-weight:600; margin:14px 0 4px 0;">'
                     f"{html.escape(self.study['book']['en'])}"
                     f"<span style='color:{MUTED}; font-size:10.5pt; font-weight:400;'>　{html.escape(self.study.get('passage_title', ''))}</span></p>")
        for index, paragraph in enumerate(self.study["paragraphs"]):
            if self.study.get("show_paragraph_titles"):
                heading = str(paragraph.get("title") or "")
                parts.append(f"<h3 style='font-size:12pt; margin:18px 0 4px 0;'>{html.escape(heading)}</h3>")
            mine = []
            for note in self.notes:
                span = self._note_span(note, index)
                if span and note["scope"] != "paragraph":
                    mine.append({"id": note["id"], "start": span[0], "end": span[1]})
            parts.append(paragraph_html(paragraph["text"], paragraph["notes"], mine, self._active_in(index), index))
        if self.study.get("after"):
            parts.append("<hr style='color:#e7dfcf;'>")
            parts.append(self._section_html("读后", self.study["after"]))
        parts.append(f"<p style='color:{MUTED}; font-size:9pt; margin-top:16px;'>{html.escape(self.study.get('source', ''))}</p>")
        parts.append("</body></html>")
        # Rebuilding resets the scroll bar and then restores it; neither jump is
        # the reader scrolling, so the right column must not follow them.
        following, self._following = self._following, False
        self.reader.blockSignals(True)
        self.reader.setHtml("".join(parts))
        self.reader.blockSignals(False)
        self._locate_paragraphs()
        self.reader.verticalScrollBar().setValue(position)
        self._following = following

    def _locate_paragraphs(self) -> None:
        """Document position of each passage paragraph (block text equals the paragraph)."""

        texts = [paragraph["text"] for paragraph in self.study["paragraphs"]]
        found: dict[int, int] = {}
        block = self.reader.document().begin()
        while block.isValid():
            text = block.text().replace("\u2028", "\n")
            if text in texts and texts.index(text) not in found:
                found[texts.index(text)] = block.position()
            block = block.next()
        self.block_positions = [found.get(index, -1) for index in range(len(texts))]

    def _active_in(self, index: int) -> tuple[str, int] | None:
        if not self.active:
            return None
        kind, value = self.active
        if kind == "a":
            paragraph, anchor = divmod(value, 1000)
            return ("a", anchor) if paragraph == index else None
        note = next((note for note in self.notes if note["id"] == value), None)
        return ("n", value) if note is not None and self._note_span(note, index) else None

    def _card(self, active_key: tuple[str, int], parent_layout: Any) -> ClickFrame:
        card = ClickFrame()
        card.setObjectName("studyCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.clicked.connect(lambda key=active_key: self._activate_key(key, from_side=True))
        self.cards[active_key] = card
        parent_layout.addWidget(card)
        return card

    def _build_explanations(self) -> None:
        self._clear(self.explain_layout)
        self.groups = []
        self.cards = {}
        article = [note for note in self.notes if note["scope"] == "article"]
        if article:
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.setSpacing(6)
            title = QLabel("整篇笔记")
            self.center.set_font(title, 10.5, QFont.Weight.DemiBold)
            box_layout.addWidget(title)
            for note in article:
                box_layout.addWidget(self._mine_frame(note))
            self.explain_layout.addWidget(box)
        for index, paragraph in enumerate(self.study["paragraphs"]):
            group = QWidget()
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(0, 4, 0, 10)
            group_layout.setSpacing(8)
            gist = QFrame()
            gist.setObjectName("studyGist")
            gist.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            gist_layout = QVBoxLayout(gist)
            gist_layout.setContentsMargins(12, 2, 4, 2)
            gist_layout.setSpacing(3)
            title = QLabel(f"<span style='color:{MUTED};'>第 {index + 1} 段</span>　<b>{html.escape(paragraph.get('title', ''))}</b>")
            title.setTextFormat(Qt.TextFormat.RichText)
            self.center.set_font(title, 11)
            gist_layout.addWidget(title)
            if paragraph.get("explain"):
                body = QLabel(rich(paragraph["explain"]))
                body.setWordWrap(True)
                body.setTextFormat(Qt.TextFormat.RichText)
                body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self.center.set_font(body, 10.5)
                gist_layout.addWidget(body)
            group_layout.addWidget(gist)
            for number, note in enumerate(paragraph["notes"]):
                card = self._card(("a", index * 1000 + number), group_layout)
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(12, 9, 12, 10)
                card_layout.setSpacing(4)
                top = QHBoxLayout()
                top.setSpacing(8)
                kind = QLabel(note.get("kind", ""))
                kind.setObjectName("studyKind")
                kind.setStyleSheet(f"background:{KIND_COLOURS.get(note.get('kind'), MUTED)};")
                self.center.set_font(kind, 8.5, QFont.Weight.DemiBold)
                top.addWidget(kind, 0, Qt.AlignmentFlag.AlignTop)
                anchor = QLabel(html.escape(note["anchor"], quote=False))
                anchor.setTextFormat(Qt.TextFormat.RichText)
                anchor.setWordWrap(True)
                anchor.setStyleSheet(f"font-family:{SERIF}; color:{INK};")
                self.center.set_font(anchor, 11.5, QFont.Weight.DemiBold)
                top.addWidget(anchor, 1)
                card_layout.addLayout(top)
                body = QLabel(rich(note.get("text", "")))
                body.setWordWrap(True)
                body.setTextFormat(Qt.TextFormat.RichText)
                self.center.set_font(body, 10.5)
                card_layout.addWidget(body)
            for note in self._paragraph_notes(index):
                group_layout.addWidget(self._mine_frame(note))
            self.groups.append(group)
            self.explain_layout.addWidget(group)
        self.explain_layout.addStretch(1)
        self._attach_lookup()

    def _mine_frame(self, note: dict[str, Any], with_quote: bool = False) -> ClickFrame:
        frame = ClickFrame()
        frame.setObjectName("studyMine")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame.setCursor(Qt.CursorShape.PointingHandCursor)
        frame.clicked.connect(lambda key=("n", int(note["id"])): self._activate_key(key, from_side=True))
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 6, 8, 8)
        layout.setSpacing(3)
        top = QHBoxLayout()
        where = SCOPE_NAMES.get(note["scope"], "")
        first, last = int(note["paragraph"]), int(note.get("paragraph_end", -1))
        if first >= 0:
            span = f"第 {first + 1}–{last + 1} 段" if last > first else f"第 {first + 1} 段"
            where = f"{span} · {where}"
        label = QLabel(f"我的笔记 · {where}")
        label.setObjectName("pageNote")
        self.center.set_font(label, 9)
        top.addWidget(label, 1)
        for text, slot in (("编辑", lambda n=note: self.edit_note(n)), ("删除", lambda n=note: self.delete_note(n))):
            button = QPushButton(text)
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(f"color:{ACCENT}; border:0; padding:0 4px;")
            button.clicked.connect(slot)
            self.center.set_font(button, 9)
            top.addWidget(button)
        layout.addLayout(top)
        if with_quote and note.get("quote"):
            quote = QLabel(html.escape(note["quote"] if len(note["quote"]) < 160 else note["quote"][:160] + "…"))
            quote.setWordWrap(True)
            quote.setStyleSheet(f"font-family:{SERIF}; color:{MUTED};")
            self.center.set_font(quote, 10.5)
            layout.addWidget(quote)
        body = QLabel(html.escape(note["body"]).replace("\n", "<br>"))
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        self.center.set_font(body, 10.5)
        layout.addWidget(body)
        if not with_quote:  # the explanation tab's frame is the one to scroll to
            self.cards[("n", int(note["id"]))] = frame
        return frame

    def _build_notes_tab(self) -> None:
        self._clear(self.notes_layout)
        if not self.notes:
            empty = QLabel("还没有笔记。在原文里选中一句、一段或几个词，点“笔记”就能记下你的想法。")
            empty.setWordWrap(True)
            empty.setObjectName("pageNote")
            self.center.set_font(empty, 10.5)
            self.notes_layout.addWidget(empty)
        else:
            for note in sorted(self.notes, key=lambda n: (n["paragraph"], n["start"], n["id"])):
                self.notes_layout.addWidget(self._mine_frame(note, with_quote=True))
        self.notes_layout.addStretch(1)
        self._attach_lookup()

    def _attach_lookup(self) -> None:
        from shared.scripts.extensive_reading_qt import attach_extensive_word_lookup

        attach_extensive_word_lookup(self, self.owner, note_reader=self.reader)
        for frame in self.findChildren(ClickFrame):
            frame.enable_text_lookup()

    # -- following and highlighting -------------------------------------------------------

    def _paragraph_top(self, index: int) -> float:
        position = self.block_positions[index] if 0 <= index < len(self.block_positions) else -1
        if position < 0:
            return 0.0
        block = self.reader.document().findBlock(position)
        return self.reader.document().documentLayout().blockBoundingRect(block).top()

    def _follow(self) -> None:
        """Keep the explanation of the paragraph being read at the top of the right column."""

        if not self._following or not self.block_positions or not self.groups or self.explain_scroll.isHidden():
            return
        mark = self.reader.verticalScrollBar().value() + self.reader.viewport().height() * 0.3
        current = 0
        for index in range(len(self.block_positions)):
            if self._paragraph_top(index) <= mark:
                current = index
        self.explain_scroll.verticalScrollBar().setValue(max(0, self.groups[current].pos().y() - 4))

    def _queue_anchor(self, href: str) -> None:
        if self._double_clicked:
            return
        self._pending_anchor = href
        self._anchor_timer.start(QApplication.doubleClickInterval())

    def _activate_pending_anchor(self) -> None:
        href, self._pending_anchor = self._pending_anchor, ""
        if href:
            self._activate(href)

    def _activate(self, href: str) -> None:
        kind, _, value = href.partition(":")
        if kind == "a":
            paragraph, _, number = value.partition(":")
            self._activate_key(("a", int(paragraph) * 1000 + int(number)), from_side=False)
        elif kind == "n":
            self._activate_key(("n", int(value)), from_side=False)

    def _activate_key(self, key: tuple[str, int], *, from_side: bool) -> None:
        previous = self.active
        self.active = key
        for old in {previous, key} - {None}:
            card = self.cards.get(old)
            if card is not None:
                card.setProperty("active", old == key)
                card.style().unpolish(card)
                card.style().polish(card)
        side = self.explain_scroll.verticalScrollBar().value()
        self._render()
        paragraph = self._paragraph_of(key)
        if from_side and paragraph is not None:
            # The card just clicked stays where it is; only the passage moves.
            self._following = False
            self.reader.verticalScrollBar().setValue(int(max(0, self._paragraph_top(paragraph) - 60)))
            self.explain_scroll.verticalScrollBar().setValue(side)
            QTimer.singleShot(0, lambda: setattr(self, "_following", True))
        elif not from_side:
            self._show_side(0)
            card = self.cards.get(key)
            if card is not None:
                self.explain_scroll.ensureWidgetVisible(card, 0, 60)

    def _paragraph_of(self, key: tuple[str, int]) -> int | None:
        if key[0] == "a":
            return key[1] // 1000
        note = next((note for note in self.notes if note["id"] == key[1]), None)
        return int(note["paragraph"]) if note and int(note["paragraph"]) >= 0 else None

    # -- what the learner selects -----------------------------------------------------------

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        if watched is self.reader.viewport():
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress:
                self._anchor_timer.stop()
                self._pending_anchor = ""
                self._double_clicked = False
                self._hide_bar()
                lookup = self._lookup()
                if lookup is not None:
                    lookup.close()
            elif kind == QEvent.Type.MouseButtonDblClick:
                # A double-click looks the word up; it is not a selection to annotate.
                self._anchor_timer.stop()
                self._pending_anchor = ""
                self._double_clicked = True
        return False

    def _lookup(self) -> Any:
        getter = getattr(self.owner, "_english_question_word_lookup", None)
        return getter() if callable(getter) else None

    def _selection_changed(self) -> None:
        self._hide_bar()
        self._selection_timer.start()

    def _selection_settled(self) -> None:
        # Wait until the drag (mouse or three-finger) has ended.
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            self._selection_timer.start()
            return
        if self._double_clicked:
            self._double_clicked = False
            return
        cursor = self.reader.textCursor()
        if not cursor.hasSelection():
            return
        offer = self.selection_offer(cursor.selectionStart(), cursor.selectionEnd(), cursor.selectedText())
        if offer is not None:
            self._show_bar(offer, cursor)

    def _locate(self, position: int) -> tuple[int, int]:
        """(paragraph, offset) of a document position, clamped into the passage."""

        last = -1
        for index, start in enumerate(self.block_positions):
            if start < 0:
                continue
            length = len(self.study["paragraphs"][index]["text"])
            if position < start:
                return (index, 0) if last < 0 else (last, len(self.study["paragraphs"][last]["text"]))
            if position <= start + length:
                return index, position - start
            last = index
        return (last, len(self.study["paragraphs"][last]["text"])) if last >= 0 else (-1, -1)

    def _in_passage(self, start: int, end: int) -> bool:
        first = next((p for p in self.block_positions if p >= 0), -1)
        if first < 0:
            return False
        last_index = len(self.block_positions) - 1
        last = self.block_positions[last_index] + len(self.study["paragraphs"][last_index]["text"])
        return end > first and start < last

    def selection_offer(self, start: int, end: int, selected: str) -> dict[str, Any] | None:
        """What to offer for a selection: look a word up, or a note on exactly
        what was selected — a phrase, a sentence, sentences, a paragraph,
        paragraphs or the whole passage."""

        from shared.scripts.vocabulary_review_qt import lookup_selection

        selected = selected.replace("\u2029", "\n").replace("\u2028", "\n").strip()
        word = lookup_selection(selected)
        is_word = bool(word) and len(word.split()) <= 3 and "\n" not in selected
        if not self._in_passage(start, end):
            return {"lookup": word} if is_word else None
        (first, a), (last, b) = self._locate(start), self._locate(end)
        paragraphs = self.study["paragraphs"]
        text = paragraphs[first]["text"]
        while a < len(text) and text[a] == " ":
            a += 1
        offer: dict[str, Any] = {"lookup": word if is_word else "", "paragraph": first, "start": a,
                                 "paragraph_end": last, "end": b}
        if first != last:
            total = sum(len(p["text"]) for p in paragraphs)
            covered = (len(text) - a) + b + sum(len(paragraphs[i]["text"]) for i in range(first + 1, last))
            whole_ends = a <= 2 and b >= len(paragraphs[last]["text"]) - 2
            if covered >= total * 0.9:
                offer.update(scope="article", label="为整篇记笔记")
            elif whole_ends:
                offer.update(scope="paragraph", label=f"为这 {last - first + 1} 段记笔记")
            else:
                offer.update(scope="selection", label="为选中的这几段文字记笔记")
            return offer
        if b - a >= len(text) - 3:
            offer.update(scope="paragraph", label="为这一段记笔记", start=0, end=len(text))
            return offer
        first_sentence, last_sentence = sentence_range(text, a), sentence_range(text, max(a, b - 1))
        at_sentence_edges = abs(a - first_sentence[0]) <= 1 and abs(b - last_sentence[1]) <= 2
        if at_sentence_edges and first_sentence == last_sentence:
            offer.update(scope="sentence", label="为这一句记笔记", start=first_sentence[0], end=first_sentence[1])
        elif at_sentence_edges:
            offer.update(scope="selection", label="为这几句记笔记")
        else:
            offer.update(scope="selection", label="为这个词记笔记" if is_word and len(word.split()) == 1
                         else ("为这个短语记笔记" if is_word else "为选中的文字记笔记"))
        return offer

    def _show_bar(self, offer: dict[str, Any], cursor: Any) -> None:
        self._hide_bar()
        bar = QFrame(self)
        bar.setObjectName("studyBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(2)
        end_cursor = self.reader.textCursor()
        end_cursor.setPosition(cursor.selectionEnd())
        rect = self.reader.cursorRect(end_cursor)
        anchor = self.reader.viewport().mapToGlobal(rect.bottomLeft())
        if offer.get("lookup"):
            word = offer["lookup"]
            row.addWidget(self._bar_button("查词", lambda: self._bar_lookup(word, anchor)))
        if offer.get("scope"):
            row.addWidget(self._bar_button(offer["label"], lambda: self.add_note(offer)))
        if not row.count():
            bar.deleteLater()
            return
        bar.adjustSize()
        # Just under the end of the selection, so it covers nothing selected.
        local = self.mapFromGlobal(anchor) + QPoint(-bar.width() // 2, 8)
        if local.y() + bar.height() > self.height() - 4:
            start_cursor = self.reader.textCursor()
            start_cursor.setPosition(cursor.selectionStart())
            top = self.reader.viewport().mapToGlobal(self.reader.cursorRect(start_cursor).topLeft())
            local = self.mapFromGlobal(top) + QPoint(-bar.width() // 2, -bar.height() - 8)
        local.setX(max(4, min(self.width() - bar.width() - 4, local.x())))
        local.setY(max(4, min(self.height() - bar.height() - 4, local.y())))
        bar.move(local)
        bar.show()
        bar.raise_()
        self._bar = bar

    def _bar_button(self, text: str, slot: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.center.set_font(button, 9.5, QFont.Weight.DemiBold)
        button.clicked.connect(lambda: (self._hide_bar(), slot()))
        return button

    def _bar_lookup(self, word: str, position: QPoint) -> None:
        lookup = self._lookup()
        if lookup is not None:
            lookup.show(word, position)

    def _hide_bar(self) -> None:
        if self._bar is not None:
            self._bar.hide()
            self._bar.deleteLater()
            self._bar = None

    def _quote(self, offer: dict[str, Any]) -> str:
        if offer["scope"] == "article":
            return self.study["book"]["en"]
        paragraphs = self.study["paragraphs"]
        first, last = offer["paragraph"], offer["paragraph_end"]
        if first == last:
            return paragraphs[first]["text"][offer["start"]:offer["end"]]
        pieces = [paragraphs[first]["text"][offer["start"]:]]
        pieces += [paragraphs[i]["text"] for i in range(first + 1, last)]
        pieces.append(paragraphs[last]["text"][:offer["end"]])
        return "\n".join(pieces)

    def add_note(self, offer: dict[str, Any]) -> None:
        quote = self._quote(offer)
        dialog = NoteDialog(self, offer["label"].replace("为", "笔记 · ", 1).replace("记笔记", ""), quote)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.text():
            return
        try:
            result = self.service.save_reading_note(
                self.study["key"], dialog.text(), scope=offer["scope"], paragraph=offer.get("paragraph", -1),
                start=offer.get("start", -1), end=offer.get("end", -1), quote=quote,
                paragraph_end=offer.get("paragraph_end"),
            )
            if not result["readback_verified"]:
                raise RuntimeError("笔记写入后回读不一致。")
        except Exception as error:
            QMessageBox.warning(self, "保存笔记失败", str(error))
            return
        self._after_change(("n", int(result["note"]["id"])))

    def edit_note(self, note: dict[str, Any]) -> None:
        dialog = NoteDialog(self, f"编辑笔记 · {SCOPE_NAMES.get(note['scope'], '')}", note.get("quote", ""), note["body"])
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.text():
            return
        try:
            result = self.service.save_reading_note(self.study["key"], dialog.text(), note_id=int(note["id"]))
            if not result["readback_verified"]:
                raise RuntimeError("笔记写入后回读不一致。")
        except Exception as error:
            QMessageBox.warning(self, "保存笔记失败", str(error))
            return
        self._after_change(("n", int(note["id"])))

    def delete_note(self, note: dict[str, Any]) -> None:
        answer = QMessageBox.question(self, "删除笔记", "确定删除这条笔记吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.service.delete_reading_note(int(note["id"]))
            if not result["readback_verified"]:
                raise RuntimeError("删除后回读仍能找到这条笔记。")
        except Exception as error:
            QMessageBox.warning(self, "删除笔记失败", str(error))
            return
        self._after_change(None)

    def _after_change(self, key: tuple[str, int] | None) -> None:
        notes_tab = self.notes_scroll.isVisible()
        self._hide_bar()
        self._load_notes()
        self.active = key
        self._render()
        self._build_explanations()
        self._build_notes_tab()
        self._show_side(1 if notes_tab else 0)
        if key is not None and key in self.cards:
            card = self.cards[key]
            card.setProperty("active", True)
            card.style().unpolish(card)
            card.style().polish(card)
            QTimer.singleShot(0, lambda: self.explain_scroll.ensureWidgetVisible(card, 0, 60))


__all__ = ["StudyView", "paragraph_html", "sentence_range"]
