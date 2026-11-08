"""The 广读材料 page of the English workspace.

Its reference material is 旋元佑「广读学英文」, the teacher's 23-episode video
series, as 《旋元佑英文阅读》 is the reference of the 阅读方法 page.  Three tabs:

* 旋老师书单 — the books of the series as covers, filtered by the teacher's
  level, kind and public-domain status; a book opens its guide: the video's
  chapters (each opens the video at that time), the selected passage with the
  teacher's marks and remarks, the lecture record, the verified transcript,
  the episode's key points and vocabulary.
* 我的书架 — the learner's own reading files (import and the PDF reader).
* 广读方法 — episode 1 (what extensive reading is) and the appendix
  《广读学英文》 of the Ray Du books.

Data comes from :class:`EnglishLearningService` (``extensive_reading_guides``);
this module writes nothing but the remembered tab and book.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPixmap, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.method_book_qt import ACCENT, VIEW_STYLE, _button, _scroll
from shared.scripts.xuan_video_guides import SERIES_TITLE, video_link

PAGE_NAME = "广读材料"
SERIF = "'Charter', 'Georgia', 'Cambria', 'Times New Roman', 'Songti SC', 'SimSun', serif"
INK = "#263746"
MUTED = "#6b7c8f"

PAGE_STYLE = VIEW_STYLE + f"""
QPushButton#extensiveTab {{
    background: transparent; border: 0; border-radius: 10px; padding: 0 18px; color: #41566d; font-weight: 600;
}}
QPushButton#extensiveTab:hover {{ background: rgba(91, 141, 205, 32); }}
QPushButton#extensiveTab:checked {{ background: #5686d8; color: white; }}
QPushButton#extensiveChip {{
    background: rgba(255, 255, 255, 150); border: 1px solid rgba(255, 255, 255, 200); border-radius: 13px;
    padding: 0 12px; color: #41566d;
}}
QPushButton#extensiveChip:hover {{ border-color: rgba(1, 126, 218, 120); }}
QPushButton#extensiveChip:checked {{ background: rgba(1, 126, 218, 40); border-color: rgba(1, 126, 218, 150); color: {ACCENT}; }}
QFrame#extensivePanel, QFrame#extensiveBookCard {{
    background: rgba(255, 255, 255, 168); border: 1px solid rgba(255, 255, 255, 206); border-radius: 18px;
}}
QFrame#extensiveBookCard:hover {{ border: 1px solid rgba(1, 126, 218, 150); }}
QFrame#extensiveSheet {{
    background: #fbf8f1; border: 1px solid #e7dfcf; border-radius: 14px;
}}
QFrame#extensiveRemark {{
    background: rgba(1, 126, 218, 20); border: 0; border-left: 3px solid {ACCENT}; border-radius: 0;
}}
QTextBrowser#extensiveText {{
    background: rgba(255, 255, 255, 168); border: 1px solid rgba(255, 255, 255, 206); border-radius: 14px;
    padding: 10px 14px; color: {INK};
}}
QListWidget#extensiveChapters {{
    background: transparent; border: 0; outline: 0; color: {INK};
}}
QListWidget#extensiveChapters::item {{ padding: 6px 4px; border-radius: 6px; }}
QListWidget#extensiveChapters::item:hover {{ background: rgba(1, 126, 218, 18); }}
QListWidget#extensiveChapters::item:selected {{ background: rgba(1, 126, 218, 34); color: {ACCENT}; }}
QLabel#extensiveLevel {{ border-radius: 8px; padding: 1px 8px; }}
"""

LEVEL_COLOURS = {"初级": ("#2e7d4f", "rgba(46,158,90,36)"), "中低级": ("#5f7a12", "rgba(150,170,40,40)"),
                 "中级": ("#9a6100", "rgba(239,159,39,46)")}
FILTERS = (
    ("all", "全部"), ("初级", "初级"), ("中级", "中级"), ("童书", "童书"),
    ("fiction", "小说"), ("非小说", "非小说"), ("pd", "可免费读全书"),
)
# A bare mm:ss not already part of a link, a longer time or a number.
TIME = re.compile(r"(?<![0-9=(\[:])([0-9]{1,2}:[0-9]{2})(?![0-9:])")


def _center() -> Any:
    from shared.scripts import problem_bank_center_qt

    return problem_bank_center_qt


def attach_extensive_word_lookup(widget: QWidget, owner: Any, *, note_reader: QTextBrowser | None = None) -> None:
    """Connect every reading surface to the control center's existing WordLookup."""

    getter = getattr(owner, "_english_question_word_lookup", None)
    lookup = getter() if callable(getter) else None
    if lookup is None:
        return
    lookup.attach(widget, selectable_labels=True)
    for browser in widget.findChildren(QTextBrowser):
        lookup.attach_text_browser(browser, lookup_on_selection=browser is not note_reader)
    for cover in widget.findChildren(BookCover):
        if not cover.property("wordLookupBound"):
            cover.lookup_requested.connect(lookup.show)
            cover.setProperty("wordLookupBound", True)


def _state_path() -> Path:
    from shared.scripts.application_paths import APP_PATHS

    return APP_PATHS.cache_dir / "extensive_reading_state.json"


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    # A per-machine convenience (last tab and book); failure is harmless.
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _seconds(text: str) -> int:
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return total


def inline_html(markdown: str) -> str:
    """Inline Markdown of the records (bold, italic, links, times) as rich text."""

    text = html.escape(markdown, quote=False)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    return TIME.sub(lambda m: f'<a href="t:{_seconds(m.group(1))}">{m.group(1)}</a>', text)


def block_html(markdown: str) -> str:
    """Paragraphs, bullet lists and quotes of a record section."""

    out: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        bullet = re.match(r"^(\s*)[-*] (.*)", line)
        if bullet:
            if not in_list:
                out.append("<ul style='margin-top:2px; margin-bottom:6px; -qt-list-indent:1;'>")
                in_list = True
            out.append(f"<li style='margin-bottom:5px;'>{inline_html(bullet.group(2))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line.strip():
            continue
        if line.startswith(">"):
            out.append(f"<p style='color:{MUTED};'>{inline_html(line.lstrip('> '))}</p>")
        elif line.startswith("|"):
            continue
        else:
            out.append(f"<p>{inline_html(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def marked_terms(passage: str, notes: str) -> list[str]:
    """English words and phrases a remark names that occur in the passage
    (``在 worth、making happy 下画线`` → worth, making happy)."""

    terms = []
    for piece in re.findall(r"[A-Za-z][A-Za-z'’\- ]*[A-Za-z!?]|[A-Za-z]{3,}", notes):
        for candidate in (piece, *piece.split()):
            candidate = candidate.strip()
            if len(candidate) < 3:
                continue
            if re.search(rf"(?<![A-Za-z]){re.escape(candidate)}(?![A-Za-z])", passage, re.IGNORECASE):
                terms.append(candidate)
                break
    return sorted(set(terms), key=len, reverse=True)


def passage_html(passage: str, terms: list[str]) -> str:
    body = html.escape(passage, quote=False)
    for term in terms:
        pattern = re.compile(rf"(?<![A-Za-z>]){re.escape(html.escape(term, quote=False))}(?![A-Za-z<])", re.IGNORECASE)
        body = pattern.sub(lambda m: f"<span style='background-color:#d9e8f6; border-bottom:2px solid {ACCENT};'>{m.group(0)}</span>",
                           body, count=1)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n(?=[A-Z“\"])", body) if part.strip()]
    return "".join(
        f"<p style='margin:0 0 10px 0; line-height:150%;'>{part.replace(chr(10), ' ')}</p>" for part in paragraphs
    )


class BookCover(QWidget):
    """A plain typographic cover: the English title on the book's colour."""

    lookup_requested = Signal(str, QPoint)

    def __init__(self, book: dict[str, Any], code: str, size: QSize = QSize(132, 186)) -> None:
        super().__init__()
        self.book = book
        self.code = code
        self.setFixedSize(size)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        painter.fillPath(path, QColor(self.book.get("color") or "#5F5E5A"))
        # A thin spine line, like a cloth binding.
        painter.fillRect(QRectF(rect.left() + 9, rect.top(), 1.2, rect.height()), QColor(255, 255, 255, 60))
        painter.setPen(QColor(255, 255, 255, 235))
        title_font = QFont("Georgia")
        title_font.setPointSizeF(max(6.5, self.width() / 11.0))
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        text_rect = rect.adjusted(18, 18, -10, -40)
        painter.drawText(text_rect, int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                         str(self.book.get("en") or ""))
        small = QFont("Georgia")
        small.setPointSizeF(max(5.5, self.width() / 16.0))
        painter.setFont(small)
        painter.setPen(QColor(255, 255, 255, 190))
        painter.drawText(rect.adjusted(18, 0, -10, -12),
                         int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom),
                         str(self.book.get("author") or ""))
        painter.drawText(rect.adjusted(0, 6, -8, 0), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop), self.code)
        painter.end()

    def _word_at(self, position: QPoint) -> str:
        """Hit-test the existing painted title/author without replacing the cover artwork."""

        from shared.scripts.vocabulary_review_qt import lookup_selection

        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        for text, is_title in ((self.book.get("en"), True), (self.book.get("author"), False)):
            font = QFont("Georgia")
            font.setPointSizeF(max(6.5, self.width() / 11.0) if is_title else max(5.5, self.width() / 16.0))
            if is_title:
                font.setWeight(QFont.Weight.DemiBold)
            area = rect.adjusted(18, 18, -10, -40) if is_title else rect.adjusted(18, 0, -10, -12)
            document = QTextDocument()
            document.setDocumentMargin(0)
            document.setDefaultFont(font)
            document.setPlainText(str(text or ""))
            document.setTextWidth(area.width())
            if not is_title:
                area.setTop(area.bottom() - document.size().height())
            if not area.contains(position):
                continue
            offset = document.documentLayout().hitTest(QPointF(position) - area.topLeft(), Qt.HitTestAccuracy.ExactHit)
            if offset >= 0:
                cursor = QTextCursor(document)
                cursor.setPosition(offset)
                cursor.select(QTextCursor.SelectionType.WordUnderCursor)
                word = lookup_selection(cursor.selectedText())
                if word:
                    return word
        return ""

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            word = self._word_at(event.position().toPoint())
            if word:
                self.lookup_requested.emit(word, event.globalPosition().toPoint())
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


class BookCard(QFrame):
    clicked = Signal(int)

    def __init__(self, row: dict[str, Any], studied: bool = False) -> None:
        super().__init__()
        self.episode = int(row["episode"])
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._click_timer.timeout.connect(lambda: self.clicked.emit(self.episode))
        self._after_double_click = False
        self.setObjectName("extensiveBookCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(164)
        book = row["book"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(7)
        cover = BookCover(book, row["code"])
        cover.installEventFilter(self)
        layout.addWidget(cover, 0, Qt.AlignmentFlag.AlignHCenter)
        center = _center()
        title = QLabel(book.get("zh") or row["title"])
        title.setWordWrap(True)
        center.set_font(title, 11, QFont.Weight.DemiBold)
        layout.addWidget(title)
        meta = QHBoxLayout()
        meta.setSpacing(6)
        level = str(book.get("level") or "")
        if level:
            badge = QLabel(level)
            badge.setObjectName("extensiveLevel")
            colour, fill = LEVEL_COLOURS.get(level, (MUTED, "rgba(0,0,0,20)"))
            badge.setStyleSheet(f"color:{colour}; background:{fill};")
            center.set_font(badge, 8.5, QFont.Weight.DemiBold)
            meta.addWidget(badge)
        kind = QLabel(" · ".join(filter(None, [str(book.get("kind") or ""), "公版" if book.get("public_domain") else "",
                                                "学习版" if studied else ""])))
        kind.setObjectName("pageNote")
        center.set_font(kind, 9)
        meta.addWidget(kind, 1)
        layout.addLayout(meta)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            if self._after_double_click:
                self._after_double_click = False
            else:
                self._click_timer.start(QApplication.doubleClickInterval())
        super().mouseReleaseEvent(event)

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        if isinstance(watched, BookCover) and event.type() == QEvent.Type.MouseButtonDblClick:
            self._click_timer.stop()
            self._after_double_click = True
        return super().eventFilter(watched, event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self._click_timer.stop()
        self._after_double_click = True
        super().mouseDoubleClickEvent(event)


class ExtensiveReadingView(QWidget):
    """书单 | 我的书架 | 广读方法, plus one book's guide (stack page 3)."""

    TABS = (("books", "旋老师书单"), ("shelf", "我的书架"), ("method", "广读方法"))

    def __init__(self, owner: Any) -> None:
        super().__init__()
        self.owner = owner
        self.service = owner.english_service
        self.guides = self.service.extensive_reading_guides()
        self.state = _load_state()
        self.filter = str(self.state.get("filter") or "all")
        self.episode: dict[str, Any] | None = None
        self.catalog: list[dict[str, Any]] = []
        self.cards: list[BookCard] = []
        self.study_episodes: set[int] = set()
        self._grid_columns = 0
        self.center = _center()

        self.setObjectName("readingMethodView")
        self.setStyleSheet(PAGE_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        tab_panel = QFrame()
        tab_panel.setObjectName("extensivePanel")
        tab_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        tab_row = QHBoxLayout(tab_panel)
        tab_row.setContentsMargins(10, 8, 10, 8)
        tab_row.setSpacing(5)
        self.tab_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for key, label in self.TABS:
            button = QPushButton(label)
            button.setObjectName("extensiveTab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(36)
            button.clicked.connect(lambda _checked=False, selected=key: self.show_tab(selected))
            group.addButton(button)
            self.tab_buttons[key] = button
            tab_row.addWidget(button)
        tab_row.addStretch(1)
        source = QLabel(f"讲解依据：{SERIES_TITLE}（旋元佑老师的广读书单与讲解）")
        source.setObjectName("pageNote")
        self.center.set_font(source, 9.5)
        tab_row.addWidget(source)
        layout.addWidget(tab_panel)

        self.stack = QStackedWidget()
        self.stack.setObjectName("readingMethodStack")
        self.stack.addWidget(self._build_books_page())      # 0
        self.stack.addWidget(self._build_shelf_page())      # 1
        self.stack.addWidget(self._build_method_page())     # 2
        self.stack.addWidget(self._build_guide_page())      # 3
        from shared.scripts.extensive_study_qt import StudyView

        self._study_back = "books"  # a method lecture returns to 广读方法
        self.study_view = StudyView(owner, lambda: self.show_tab(self._study_back))
        self.stack.addWidget(self.study_view)                # 4
        layout.addWidget(self.stack, 1)
        self.setMinimumHeight(640)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # -- tabs ---------------------------------------------------------------------

    def load(self) -> None:
        if not self.guides.available():
            self.books_note.setText(
                f"没有找到视频记录：{self.guides.folder}。这份资料随学科数据同步，请确认 Syncthing 已同步完成。"
            )
        else:
            self.catalog = self.guides.catalog()
            self.study_episodes = set(self.guides.study_episodes())
            self._fill_books()
        tab = str(self.state.get("tab") or "books")
        episode = int(self.state.get("episode") or 0)
        if tab == "guide" and episode:
            self.open_guide(episode)
        elif tab == "lecture" and self.state.get("lecture"):
            self.open_lecture(str(self.state["lecture"]))
        else:
            self.show_tab(tab if tab in dict(self.TABS) else "books")
        attach_extensive_word_lookup(self, self.owner, note_reader=self.study_view.reader)

    def show_tab(self, key: str) -> None:
        index = {"books": 0, "shelf": 1, "method": 2}.get(key, 0)
        self.tab_buttons[key if key in self.tab_buttons else "books"].setChecked(True)
        if key == "method":
            self._fill_method()
        self.stack.setCurrentIndex(index)
        self._remember(tab=key)

    def _remember(self, **values: Any) -> None:
        self.state.update(values)
        _save_state(self.state)

    # -- 旋老师书单 ----------------------------------------------------------------

    def _build_books_page(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(12)
        self.continue_card = QFrame()
        self.continue_card.setObjectName("extensivePanel")
        self.continue_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.continue_layout = QHBoxLayout(self.continue_card)
        self.continue_layout.setContentsMargins(18, 14, 18, 14)
        self.continue_layout.setSpacing(16)
        layout.addWidget(self.continue_card)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.filter_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for key, label in FILTERS:
            button = QPushButton(label)
            button.setObjectName("extensiveChip")
            button.setCheckable(True)
            button.setFixedHeight(28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setChecked(key == self.filter)
            button.clicked.connect(lambda _checked=False, selected=key: self.set_filter(selected))
            group.addButton(button)
            self.filter_buttons[key] = button
            chips.addWidget(button)
        chips.addStretch(1)
        self.books_note = QLabel()
        self.books_note.setObjectName("pageNote")
        self.center.set_font(self.books_note, 9.5)
        chips.addWidget(self.books_note)
        layout.addLayout(chips)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        layout.addWidget(self.grid_host)
        layout.addStretch(1)
        self.books_scroll = _scroll(host)
        # Re-flow the cards whenever the visible width changes (the first
        # layout happens before the page has its final size).
        self.books_scroll.viewport().installEventFilter(self)
        return self.books_scroll

    def _matches(self, row: dict[str, Any]) -> bool:
        book = row["book"]
        if row["episode"] == 1:
            return self.filter == "all"
        if self.filter == "all":
            return True
        if self.filter == "fiction":
            return book["kind"] in {"小说", "短篇小说"}
        if self.filter == "pd":
            return bool(book["public_domain"])
        if self.filter in {"初级", "中级"}:
            # The teacher's 中低级 sits between the two and shows under both.
            return book["level"] in {self.filter, "中低级"}
        return book["kind"] == self.filter

    def set_filter(self, key: str) -> None:
        self.filter = key
        self._remember(filter=key)
        self._fill_books()

    def _fill_books(self) -> None:
        for card in self.cards:
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        rows = [row for row in self.catalog if self._matches(row)]
        for row in rows:
            card = BookCard(row, studied=row["episode"] in self.study_episodes)
            card.clicked.connect(self.open_guide)
            self.cards.append(card)
        self._grid_columns = 0
        self._layout_grid()
        books = sum(1 for row in rows if row["episode"] != 1)
        self.books_note.setText(f"{books} 本书" if self.filter != "all" else f"导论 1 集 · 书 {books} 本")
        self._fill_continue()
        attach_extensive_word_lookup(self, self.owner, note_reader=self.study_view.reader)

    def _layout_grid(self) -> None:
        width = max(1, self.books_scroll.viewport().width() - 8)
        columns = max(2, width // 178)
        if columns == self._grid_columns and all(self.grid.indexOf(card) >= 0 for card in self.cards):
            return
        self._grid_columns = columns
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        for column in range(columns + 1):
            self.grid.setColumnStretch(column, 0)
        self.grid.setColumnStretch(columns, 1)

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.Resize and watched is self.books_scroll.viewport() and self.cards:
            self._layout_grid()
        return super().eventFilter(watched, event)

    def _fill_continue(self) -> None:
        layout = self.continue_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        episode = int(self.state.get("episode") or 0)
        row = next((row for row in self.catalog if row["episode"] == episode), None)
        if row is None:
            text = QLabel(
                "广读的作法：大量、快速、持续、不求甚解。挑一本你有兴趣、不太难的书——"
                "先看旋老师的导读，再读原文；也可以先读“广读方法”中的导论。"
            )
            text.setWordWrap(True)
            self.center.set_font(text, 10.5)
            layout.addWidget(text, 1)
            return
        is_introduction = episode == 1
        layout.addWidget(BookCover(row["book"], "" if is_introduction else row["code"], QSize(64, 90)))
        # Retire the labels together when this slot is replaced on the next book.
        column_host = QWidget()
        column = QVBoxLayout(column_host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        hint = QLabel("继续阅读" if is_introduction else "继续看导读")
        hint.setObjectName("pageNote")
        self.center.set_font(hint, 9)
        title = QLabel(f"{row['book']['en']}　{row['book']['zh']}")
        title.setStyleSheet(f"font-family: {SERIF};")
        title.setWordWrap(True)
        self.center.set_font(title, 14, QFont.Weight.DemiBold)
        meta = QLabel("广读方法导论" if is_introduction else
                     f"{row['code']} · 视频 {row['duration']} · 讲义 {row['sheet_count']} 页 · 词汇 {row['vocabulary_count']} 条")
        meta.setObjectName("pageNote")
        meta.setWordWrap(True)
        self.center.set_font(meta, 9.5)
        column.addWidget(hint)
        column.addWidget(title)
        column.addWidget(meta)
        layout.addWidget(column_host, 1)
        layout.addWidget(_button("继续 →", lambda: self.open_guide(episode)), 0, Qt.AlignmentFlag.AlignVCenter)
        attach_extensive_word_lookup(self, self.owner, note_reader=self.study_view.reader)

    # -- 我的书架 -------------------------------------------------------------------

    def _build_shelf_page(self) -> QWidget:
        host = QWidget()
        layout = self.owner._build_english_extensive_reading_page(show_heading=False)
        host.setLayout(layout)
        return _scroll(host)

    # -- 广读方法 -------------------------------------------------------------------

    def _build_method_page(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(12)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(_button("阅读广读导论", lambda: self.open_guide(1)))
        for key, label in (("WS01", "讲座一：英文学习方法论"), ("WS02", "讲座二：广读的实际作法")):
            row.addWidget(_button(label, lambda _checked=False, key=key: self.open_lecture(key)))
        layout.addLayout(row)
        self.method_text = self._text_browser()
        self.method_text.setMinimumHeight(600)
        layout.addWidget(self.method_text, 1)
        self._method_filled = False
        return _scroll(host)

    def _fill_method(self) -> None:
        if self._method_filled:
            return
        parts = [
            "<table width='100%' cellpadding='12' cellspacing='0' bgcolor='#fbf8f1' style='border:1px solid #e7dfcf;'><tr><td>"
            "<p style='margin:0;'><b>试读一页，判断这本书适不适合你广读</b></p>"
            "<p style='margin:6px 0 0 0;'>翻开第一页：<b>不查单字、不暂停、不回头</b>，一气呵成读完。然后问自己两件事——"
            "<b>大意看得懂吗？</b>有几个单字不认识、几个句子看不懂都没关系，看得出在讲什么就行；"
            "<b>觉得好看吗？</b>不查单字、不求甚解地读，仍然想往下看，就是适合你的广读材料。</p>"
            "<p style='margin:6px 0 0 0; color:#6b7c8f;'>出自《旋元佑英文阅读》等五本书的附录《广读学英文》。</p></td></tr></table>",
        ]
        try:
            first = self.guides.episode(1)
            parts.append("<h2>广读方法导论</h2>")
            parts.append(block_html(first["summary"]))
            if first["sheets"]:
                parts.append("<p style='color:#6b7c8f;'>导论提纲：</p>")
                lines = [inline_html(line) for line in first["sheets"][0]["passage"].splitlines() if line.strip()]
                parts.append(f"<blockquote>{'<br>'.join(lines)}</blockquote>")
        except (OSError, ValueError) as error:
            parts.append(f"<p>第 1 集视频记录读取失败：{html.escape(str(error))}</p>")
        if self.guides.lectures():
            parts.append("<h2>旋老师的两讲：如何真正学好英文</h2>")
            parts.append(
                "<p>旋氏英文课程里的方法论讲座（右上角按钮打开学习版）。"
                "<b>讲座一</b>讲英语教学法的沿革——文法翻译法、句型练习法、沟通法、阅读法——"
                "以及为什么在没人说英文的环境里，要靠阅读、尤其是广读；"
                "<b>讲座二</b>讲广读的实际作法：每分钟 250 字的速度目标，生字先拆再猜，"
                "听说靠广读打底、发音流利重于标准，平时不做句型分析、难句和关键句才停下来。</p>")
        try:
            parts.append("<h2>附录　广读学英文</h2>")
            parts.append(self.service.extensive_reading_appendix_html())
        except (OSError, ValueError) as error:
            parts.append(f"<p>附录读取失败（需要本机的《旋元佑英文阅读》原书工程）：{html.escape(str(error))}</p>")
        self.method_text.setHtml(self._document("".join(parts)))
        self._method_filled = True

    # -- one book's guide ------------------------------------------------------------

    def _text_browser(self) -> QTextBrowser:
        browser = QTextBrowser()
        browser.setObjectName("extensiveText")
        browser.setOpenLinks(False)
        browser.anchorClicked.connect(self._open_anchor)
        self.center.set_font(browser, 11)
        return browser

    def _document(self, body: str) -> str:
        return (f"<html><body style='color:{INK}; line-height:160%;'>"
                f"<style>h2 {{ font-size: 15pt; margin-top: 18px; }} h3 {{ font-size: 12.5pt; margin-top: 12px; }}"
                f" a {{ color: {ACCENT}; text-decoration: none; }}"
                f" td {{ padding: 4px 8px; border-bottom: 1px solid #e3e8ee; }}</style>{body}</body></html>")

    def _build_guide_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("readingMethodHost")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        header = QFrame()
        header.setObjectName("readingMethodHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(14)
        header_layout.addWidget(_button("← 书单", lambda: self.show_tab("books")), 0, Qt.AlignmentFlag.AlignTop)
        self.guide_cover_slot = QHBoxLayout()
        header_layout.addLayout(self.guide_cover_slot)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        self.guide_title = QLabel()
        self.guide_title.setStyleSheet(f"font-family: {SERIF};")
        self.guide_title.setWordWrap(True)
        self.center.set_font(self.guide_title, 17, QFont.Weight.DemiBold)
        self.guide_meta = QLabel()
        self.guide_meta.setObjectName("pageNote")
        self.guide_meta.setWordWrap(True)
        self.center.set_font(self.guide_meta, 10)
        self.guide_book = QLabel()
        self.guide_book.setTextFormat(Qt.TextFormat.RichText)
        self.guide_book.setWordWrap(True)
        self.center.set_font(self.guide_book, 10)
        titles.addWidget(self.guide_title)
        titles.addWidget(self.guide_meta)
        titles.addWidget(self.guide_book)
        header_layout.addLayout(titles, 1)
        self.video_button = _button("▶ 看视频", lambda: self._open_video(0), "用浏览器打开 YouTube 上的这一集")
        header_layout.addWidget(self.video_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(14)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        nav = QFrame()
        nav.setObjectName("readingMethodNav")
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(14, 14, 10, 12)
        nav_layout.setSpacing(6)
        nav_title = QLabel("视频章节")
        self.center.set_font(nav_title, 12, QFont.Weight.DemiBold)
        nav_layout.addWidget(nav_title)
        nav_hint = QLabel("点一下，从这里看视频")
        nav_hint.setObjectName("pageNote")
        self.center.set_font(nav_hint, 9)
        nav_layout.addWidget(nav_hint)
        self.chapter_list = QListWidget()
        self.chapter_list.setObjectName("extensiveChapters")
        self.chapter_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chapter_list.setWordWrap(True)
        self.center.set_font(self.chapter_list, 10)
        self.chapter_list.itemClicked.connect(lambda item: self._open_video(int(item.data(Qt.ItemDataRole.UserRole))))
        nav_layout.addWidget(self.chapter_list, 1)
        nav.setMinimumWidth(200)
        nav.setMaximumWidth(300)
        splitter.addWidget(nav)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(8)
        view_row = QHBoxLayout()
        view_row.setSpacing(5)
        self.view_buttons: dict[int, QPushButton] = {}
        group = QButtonGroup(self)
        for index, label in enumerate(("精选片段", "讲解与画面", "逐句文字稿")):
            button = QPushButton(label)
            button.setObjectName("extensiveChip")
            button.setCheckable(True)
            button.setFixedHeight(28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, selected=index: self._show_view(selected))
            group.addButton(button)
            self.view_buttons[index] = button
            view_row.addWidget(button)
        view_row.addStretch(1)
        middle_layout.addLayout(view_row)
        self.views = QStackedWidget()
        self.views.setObjectName("readingMethodStack")
        sheets_host = QWidget()
        self.sheets_layout = QVBoxLayout(sheets_host)
        self.sheets_layout.setContentsMargins(0, 0, 8, 12)
        self.sheets_layout.setSpacing(14)
        self.sheets_scroll = _scroll(sheets_host)
        self.views.addWidget(self.sheets_scroll)
        self.lecture_text = self._text_browser()
        self.views.addWidget(self.lecture_text)
        self.transcript_text = self._text_browser()
        self.views.addWidget(self.transcript_text)
        middle_layout.addWidget(self.views, 1)
        splitter.addWidget(middle)

        self.side_text = self._text_browser()
        self.side_text.setMinimumWidth(260)
        self.side_text.setMaximumWidth(420)
        splitter.addWidget(self.side_text)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([230, 760, 320])
        layout.addWidget(splitter, 1)
        return page

    def open_lecture(self, key: str) -> None:
        """One of the teacher's 旋氏英文 method lectures, in the study view."""

        try:
            self.study_view.open(key)
        except (OSError, ValueError) as error:
            self.show_tab("method")
            self.method_text.append(f"<p>读取讲座 {html.escape(key)} 失败：{html.escape(str(error))}</p>")
            return
        self._study_back = "method"
        self.study_view.back_button.setText("← 广读方法")
        self.stack.setCurrentIndex(4)
        self._remember(tab="lecture", lecture=key)

    def open_guide(self, number: int) -> None:
        """A book with a study guide opens it; the others (still being written)
        open the record of the teacher's lesson."""

        if int(number) in self.study_episodes:
            try:
                self.study_view.open(int(number))
            except (OSError, ValueError) as error:
                self.books_note.setText(f"读取第 {number} 集的学习版失败：{error}")
                return
            self._study_back = "books"
            self.study_view.back_button.setText("← 书单")
            self.stack.setCurrentIndex(4)
            self._remember(tab="guide", episode=int(number))
            self._fill_continue()
            return
        try:
            episode = self.guides.episode(int(number))
        except (OSError, ValueError) as error:
            self.books_note.setText(f"读取第 {number} 集失败：{error}")
            return
        self.episode = episode
        book = episode["book"]
        for button in self.tab_buttons.values():
            button.setAutoExclusive(False)
            button.setChecked(False)
            button.setAutoExclusive(True)
        while self.guide_cover_slot.count():
            item = self.guide_cover_slot.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.guide_cover_slot.addWidget(BookCover(book, episode["code"], QSize(64, 90)))
        self.guide_title.setText(f"{book['en']}　{book['zh']}" if book.get("en") else episode["title"])
        self.guide_meta.setText(" · ".join(filter(None, [
            book.get("author"), book.get("year"), book.get("kind"),
            book.get("level") or "老师未说难度", "公版，可免费读全书" if book.get("public_domain") else "",
            f"{episode['code']} 视频 {episode['duration']}",
        ])))
        self.guide_book.setText(inline_html(episode["book_line"]))
        self.chapter_list.clear()
        for chapter in episode["chapters"]:
            item = QListWidgetItem(f"{chapter['time']}　{chapter['label']}")
            item.setData(Qt.ItemDataRole.UserRole, chapter["start"])
            self.chapter_list.addItem(item)
        self._fill_sheets(episode)
        self.lecture_text.setHtml(self._document(block_html(episode["windows"])))
        self.transcript_text.setHtml(self._document(self._transcript_html(episode)))
        vocabulary = "".join(
            f"<tr><td style='font-family:{SERIF};'><b>{inline_html(row['term'])}</b></td><td>{inline_html(row['meaning'])}</td></tr>"
            for row in episode["vocabulary"]
        )
        self.side_text.setHtml(self._document(
            "<h3 style='margin-top:0;'>本集要点</h3>" + block_html(episode["summary"])
            + f"<h3>词汇与表达（{len(episode['vocabulary'])}）</h3>"
            + f"<table cellspacing='0' width='100%'>{vocabulary}</table>"
        ))
        self._show_view(int(self.state.get("view") or 0) if self.state.get("episode") == episode["episode"] else 0)
        self.stack.setCurrentIndex(3)
        self._remember(tab="guide", episode=episode["episode"])
        self._fill_continue()
        attach_extensive_word_lookup(self, self.owner, note_reader=self.study_view.reader)

    def _transcript_html(self, episode: dict[str, Any]) -> str:
        parts = ["<p style='color:#6b7c8f;'>众文图书的字幕（繁体转简体，措辞未改），与语音逐字交叉核对；"
                 "时间可跳转到视频。<b>粗体</b>是字幕里以黄字强调的词。</p>"]
        for section in episode["transcript"]:
            parts.append(f"<h3>{html.escape(section['label'])}</h3>")
            for paragraph in section["paragraphs"]:
                parts.append(f"<p><a href='t:{paragraph['start']}'>{paragraph['time']}</a>　{inline_html(paragraph['text'])}</p>")
        return "".join(parts)

    def _fill_sheets(self, episode: dict[str, Any]) -> None:
        layout = self.sheets_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        lookup = self.owner._english_question_word_lookup() if hasattr(self.owner, "_english_question_word_lookup") else None
        for sheet in episode["sheets"]:
            card = QFrame()
            card.setObjectName("extensiveSheet")
            card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(24, 18, 24, 18)
            card_layout.setSpacing(10)
            head = QHBoxLayout()
            label = QLabel(f"讲义 {sheet['number']}")
            label.setStyleSheet(f"color:{MUTED};")
            self.center.set_font(label, 9.5, QFont.Weight.DemiBold)
            head.addWidget(label)
            head.addStretch(1)
            head.addWidget(_button(f"▶ {sheet['time']} 听这段", lambda start=sheet["start"]: self._open_video(start)))
            if sheet.get("image"):
                head.addWidget(_button("原讲义截图", lambda path=sheet["image"], n=sheet["number"]: self._show_image(path, n)))
            card_layout.addLayout(head)
            passage = QLabel()
            passage.setTextFormat(Qt.TextFormat.RichText)
            passage.setWordWrap(True)
            passage.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            passage.setStyleSheet(f"font-family: {SERIF}; color: #1f2a33;")
            self.center.set_font(passage, 14.5)
            passage.setText(passage_html(sheet["passage"], marked_terms(sheet["passage"], sheet["notes"])))
            card_layout.addWidget(passage)
            if sheet["notes"]:
                remark = QFrame()
                remark.setObjectName("extensiveRemark")
                remark.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
                remark_layout = QHBoxLayout(remark)
                remark_layout.setContentsMargins(12, 8, 12, 8)
                remark_text = re.sub(r"^(批注与核对|批注)[：:]\s*", "", sheet["notes"])
                note = QLabel(f"✎ 旋老师的标记　{inline_html(remark_text)}")
                note.setWordWrap(True)
                note.setTextFormat(Qt.TextFormat.RichText)
                note.setOpenExternalLinks(False)
                note.linkActivated.connect(lambda href: self._open_anchor(QUrl(href)))
                self.center.set_font(note, 10)
                remark_layout.addWidget(note)
                card_layout.addWidget(remark)
            layout.addWidget(card)
            if lookup is not None:
                lookup.attach(passage)
        tip = QLabel("划线的词是旋老师在讲义上标出的地方；双击英文单词可以查词。完整讲解在“讲解与画面”。")
        tip.setObjectName("pageNote")
        tip.setWordWrap(True)
        self.center.set_font(tip, 9.5)
        layout.addWidget(tip)
        layout.addStretch(1)

    def _show_view(self, index: int) -> None:
        self.view_buttons[index].setChecked(True)
        self.views.setCurrentIndex(index)
        self._remember(view=index)

    # -- links -------------------------------------------------------------------------

    def _open_anchor(self, url: QUrl) -> None:
        text = url.toString()
        if text.startswith("t:"):
            self._open_video(int(text[2:] or 0))
        elif text.startswith("http"):
            QDesktopServices.openUrl(url)

    def _open_video(self, start: int) -> None:
        episode = self.episode
        if not episode or not episode.get("video_url"):
            return
        QDesktopServices.openUrl(QUrl(video_link(episode["video_url"], start)))

    def _show_image(self, path: str, number: int) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{(self.episode or {}).get('code', '')} 讲义 {number}（最终批注状态）")
        layout = QVBoxLayout(dialog)
        image = QLabel()
        screen = self.screen().availableGeometry() if self.screen() else None
        limit = QSize(int(screen.width() * 0.6), int(screen.height() * 0.8)) if screen else QSize(900, 900)
        image.setPixmap(pixmap.scaled(limit, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(image)
        dialog.exec()


def build_extensive_reading_page(owner: Any) -> QVBoxLayout:
    """The 广读材料 page (left navigation of the English workspace)."""

    layout = QVBoxLayout()
    layout.setContentsMargins(24, 22, 24, 28)
    layout.setSpacing(12)
    layout.addLayout(owner._english_page_heading(
        PAGE_NAME,
        "旋元佑老师推荐的广读书单：左边读原文，右边是老师的讲解；双击查词，选中文字记笔记。"
        "广读的作法：大量、快速、持续、不求甚解。",
    ))
    if owner.english_service is None:
        layout.addWidget(QLabel("英语学习服务不可用。"))
        return layout
    view = ExtensiveReadingView(owner)
    owner.extensive_reading_view = view
    view.load()
    layout.addWidget(view, 1)
    return layout


__all__ = ["ExtensiveReadingView", "PAGE_NAME", "build_extensive_reading_page", "marked_terms", "passage_html"]
