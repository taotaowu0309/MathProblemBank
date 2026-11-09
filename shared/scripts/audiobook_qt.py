"""The 有声书 page of the English workspace: listen to a book while reading it.

Left: the books, the chapters of the open book (heard ✓, ready, or not
prepared yet) and the preparation of the book (download, align and
translate, chapter by chapter, in the background).  Right: the chapter's
text with the sentence being read highlighted, the translation bar, and the
player: previous / play / next sentence, repeat one sentence, speed and
position.

A click selects a sentence (no playback): its translation shows in the bar,
and play (or Space) starts from it.  A double-click looks a word up, so the
two never get in each other's way.  The translation bar opens and closes
with 译文 (or T) and follows the sentence being read.

Data comes from :class:`AudiobookService`; this module writes only through it.
"""

from __future__ import annotations

import bisect
import threading
from typing import Any

from PySide6.QtCore import QEvent, QObject, QThreadPool, QTimer, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.audiobook_service import AudiobookService
from shared.scripts.method_book_qt import ACCENT, VIEW_STYLE
from shared.scripts.vocabulary_review_qt import _Task, _font

PAGE_NAME = "有声书"
INK = "#263746"
MUTED = "#8A929C"
CURRENT = "#FFF0BF"
SELECTED = "#DCEBFA"
SERIF = "Georgia, 'Times New Roman', 'Songti SC', serif"
SPEEDS = (0.75, 0.85, 1.0, 1.15, 1.3)

PAGE_STYLE = VIEW_STYLE + f"""
QListWidget#audiobookList {{ background: transparent; border: 0; outline: 0; color: {INK}; }}
QListWidget#audiobookList::item {{ padding: 8px 6px; border-radius: 8px; }}
QListWidget#audiobookList::item:hover {{ background: rgba(1, 126, 218, 18); }}
QListWidget#audiobookList::item:selected {{ background: rgba(1, 126, 218, 34); color: {ACCENT}; }}
QTextBrowser#audiobookText {{
    font-family: "Georgia", "Times New Roman", "Songti SC"; font-size: 15.5pt;
    background: rgba(255, 255, 255, 215); border: 1px solid rgba(255, 255, 255, 230); border-radius: 16px;
    padding: 18px 26px; color: {INK};
}}
QFrame#audiobookTranslation {{
    background: rgba(255, 252, 240, 235); border: 1px solid rgba(225, 205, 150, 160); border-radius: 14px;
}}
QFrame#audiobookPlayer {{
    background: rgba(255, 255, 255, 190); border: 1px solid rgba(255, 255, 255, 220); border-radius: 16px;
}}
QPushButton#audiobookPlay {{
    background: {ACCENT}; color: white; border: 0; border-radius: 22px; font-size: 18px;
}}
QPushButton#audiobookPlay:disabled {{ background: #B7C4CE; }}
QPushButton#audiobookControl, QPushButton#audiobookMode {{
    background: rgba(255, 255, 255, 170); border: 1px solid rgba(1, 126, 218, 60); border-radius: 10px;
    padding: 0 12px; color: {INK};
}}
QPushButton#audiobookControl:hover, QPushButton#audiobookMode:hover {{ border-color: {ACCENT}; }}
QPushButton#audiobookControl:checked, QPushButton#audiobookMode:checked {{
    background: rgba(1, 126, 218, 40); border-color: {ACCENT}; color: {ACCENT};
}}
QPushButton#audiobookPrepare {{
    background: {ACCENT}; color: white; border: 0; border-radius: 10px; padding: 0 14px;
}}
QPushButton#audiobookPrepare:disabled {{ background: #B7C4CE; }}
"""


HINT = "空格 播放/暂停（从选中的句子开始）· ← → 上一句/下一句 · R 单句循环 · T 译文 · 单击选句 · 双击单词查词"


def _clock(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


class _SentenceClicks(QObject):
    """A plain click on the text selects its sentence; a double-click or a drag is left alone."""

    def __init__(self, view: "AudiobookView") -> None:
        super().__init__(view)
        self.view = view
        self.press: Any = None
        self.double = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.press = event.position().toPoint()
        elif kind == QEvent.Type.MouseButtonDblClick:
            self.double = True  # the word lookup's gesture
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            start, self.press = self.press, None
            if self.double:
                self.double = False
            elif start is not None and (event.position().toPoint() - start).manhattanLength() <= 6:
                position = self.view.reader.cursorForPosition(event.position().toPoint()).position()
                number = self.view.sentence_at(position)
                if number >= 0:
                    self.view.select_sentence(number)
        return False


class AudiobookView(QWidget):
    """Books and chapters | the chapter's text and the player."""

    def __init__(self, owner: Any, service: AudiobookService | None = None) -> None:
        super().__init__()
        self.owner = owner
        self.service = service or getattr(owner, "audiobook_service", None) or AudiobookService()
        owner.audiobook_service = self.service
        self.book_key = ""
        self.chapter: dict[str, Any] | None = None
        self.starts: list[int] = []
        self.ranges: list[tuple[int, int]] = []
        self.current = -1
        self.repeat_sentence = -1
        self.selected = -1  # a sentence clicked but not yet played from
        self._player: Any = None
        self._output: Any = None
        self._pending_start: int | None = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.setObjectName("readingMethodView")
        self.setStyleSheet(PAGE_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(14)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")

        # Left: books, chapters, preparation.
        nav = QFrame()
        nav.setObjectName("readingMethodNav")
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(14, 14, 10, 12)
        nav_layout.setSpacing(8)
        books_title = QLabel("书")
        _font(books_title, 14, QFont.Weight.DemiBold)
        nav_layout.addWidget(books_title)
        self.book_list = QListWidget()
        self.book_list.setObjectName("audiobookList")
        self.book_list.setWordWrap(True)
        self.book_list.setMaximumHeight(190)  # the chapters below get the rest
        _font(self.book_list, 12)
        self.book_list.currentItemChanged.connect(lambda item, _old: self.open_book(item.data(Qt.ItemDataRole.UserRole)) if item else None)
        nav_layout.addWidget(self.book_list)
        chapters_title = QLabel("章节")
        _font(chapters_title, 14, QFont.Weight.DemiBold)
        nav_layout.addWidget(chapters_title)
        self.chapter_list = QListWidget()
        self.chapter_list.setObjectName("audiobookList")
        _font(self.chapter_list, 12)
        self.chapter_list.itemClicked.connect(lambda item: self.open_chapter(int(item.data(Qt.ItemDataRole.UserRole))))
        nav_layout.addWidget(self.chapter_list, 1)
        prepare_row = QHBoxLayout()
        self.prepare_button = QPushButton("准备这本书")
        self.prepare_button.setObjectName("audiobookPrepare")
        self.prepare_button.setFixedHeight(34)
        self.prepare_button.setToolTip("下载 LibriVox 录音和古腾堡原文，用 Groq Whisper 把每一句对上录音时间；逐章进行，准备好的章节马上能听")
        _font(self.prepare_button, 11.5, QFont.Weight.DemiBold)
        self.prepare_button.clicked.connect(self.prepare_book)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("audiobookControl")
        self.stop_button.setFixedHeight(34)
        _font(self.stop_button, 11)
        self.stop_button.clicked.connect(self.stop_preparation)
        prepare_row.addWidget(self.prepare_button, 1)
        prepare_row.addWidget(self.stop_button)
        nav_layout.addLayout(prepare_row)
        self.status_label = QLabel("")
        self.status_label.setObjectName("pageNote")
        self.status_label.setWordWrap(True)
        _font(self.status_label, 10.5)
        nav_layout.addWidget(self.status_label)
        nav.setMinimumWidth(300)
        nav.setMaximumWidth(420)
        splitter.addWidget(nav)

        # Right: header, text, player.
        stage = QWidget()
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(10)
        header = QFrame()
        header.setObjectName("readingMethodHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(18, 10, 14, 10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_label = QLabel("有声书")
        self.title_label.setStyleSheet(f"font-family:{SERIF};")
        _font(self.title_label, 18, QFont.Weight.DemiBold)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("pageNote")
        self.meta_label.setWordWrap(True)
        _font(self.meta_label, 10.5)
        titles.addWidget(self.title_label)
        titles.addWidget(self.meta_label)
        header_row.addLayout(titles, 1)
        self.translation_button = QPushButton("译文")
        self.translation_button.setObjectName("audiobookMode")
        self.translation_button.setCheckable(True)
        self.translation_button.setFixedHeight(32)
        self.translation_button.setToolTip("打开或关闭当前句的中文译文（T）")
        _font(self.translation_button, 11.5, QFont.Weight.DemiBold)
        self.translation_button.toggled.connect(self._show_translation_bar)
        header_row.addWidget(self.translation_button)
        stage_layout.addWidget(header)

        self.reader = QTextBrowser()
        self.reader.setObjectName("audiobookText")
        self.reader.setOpenLinks(False)
        self.reader.setOpenExternalLinks(False)
        self._clicks = _SentenceClicks(self)
        self.reader.viewport().installEventFilter(self._clicks)
        stage_layout.addWidget(self.reader, 1)

        # The translation of the selected sentence, or of the one being read.
        self.translation_bar = QFrame()
        self.translation_bar.setObjectName("audiobookTranslation")
        self.translation_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar = QHBoxLayout(self.translation_bar)
        bar.setContentsMargins(16, 10, 12, 10)
        bar.setSpacing(12)
        self.translation_number = QLabel("")
        self.translation_number.setObjectName("pageNote")
        self.translation_number.setAlignment(Qt.AlignmentFlag.AlignTop)
        _font(self.translation_number, 10.5)
        self.translation_label = QLabel("")
        self.translation_label.setWordWrap(True)
        self.translation_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.translation_label.setStyleSheet(f"color:{INK};")
        _font(self.translation_label, 14)
        bar.addWidget(self.translation_number, 0, Qt.AlignmentFlag.AlignTop)
        bar.addWidget(self.translation_label, 1)
        self.translation_bar.setVisible(False)
        stage_layout.addWidget(self.translation_bar)

        player = QFrame()
        player.setObjectName("audiobookPlayer")
        player.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        player_layout = QVBoxLayout(player)
        player_layout.setContentsMargins(16, 10, 16, 10)
        player_layout.setSpacing(6)
        seek_row = QHBoxLayout()
        self.position_label = QLabel("0:00")
        self.duration_label = QLabel("0:00")
        for label in (self.position_label, self.duration_label):
            label.setObjectName("pageNote")
            _font(label, 10.5)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderReleased.connect(lambda: self._seek(self.slider.value()))
        seek_row.addWidget(self.position_label)
        seek_row.addWidget(self.slider, 1)
        seek_row.addWidget(self.duration_label)
        player_layout.addLayout(seek_row)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.previous_button = self._control("⏮ 上一句", "←", self.previous_sentence)
        self.play_button = QPushButton("▶")
        self.play_button.setObjectName("audiobookPlay")
        self.play_button.setFixedSize(44, 44)
        self.play_button.setToolTip("播放 / 暂停（空格）")
        self.play_button.clicked.connect(self.toggle_play)
        self.next_button = self._control("下一句 ⏭", "→", self.next_sentence)
        self.repeat_button = self._control("🔁 单句循环", "R", self.toggle_repeat)
        self.repeat_button.setCheckable(True)
        self.speed_combo = QComboBox()
        for speed in SPEEDS:
            self.speed_combo.addItem(f"{speed:g}×", speed)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.setToolTip("语速")
        _font(self.speed_combo, 11)
        self.speed_combo.currentIndexChanged.connect(lambda _index: self._apply_speed())
        controls.addWidget(self.previous_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addSpacing(10)
        controls.addWidget(self.repeat_button)
        controls.addStretch(1)
        speed_label = QLabel("语速")
        speed_label.setObjectName("pageNote")
        _font(speed_label, 10.5)
        controls.addWidget(speed_label)
        controls.addWidget(self.speed_combo)
        player_layout.addLayout(controls)
        self.hint_label = QLabel(HINT)
        self.hint_label.setObjectName("pageNote")
        _font(self.hint_label, 10)
        player_layout.addWidget(self.hint_label)
        stage_layout.addWidget(player)
        splitter.addWidget(stage)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 1000])
        layout.addWidget(splitter, 1)
        self.setMinimumHeight(700)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        QShortcut(QKeySequence("Space"), self, activated=self.toggle_play)
        QShortcut(QKeySequence("Left"), self, activated=self.previous_sentence)
        QShortcut(QKeySequence("Right"), self, activated=self.next_sentence)
        QShortcut(QKeySequence("R"), self, activated=lambda: self.repeat_button.click())
        QShortcut(QKeySequence("T"), self, activated=lambda: self.translation_button.toggle())
        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._follow)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(2500)
        self._save_timer.timeout.connect(self.save_progress)

        self._set_player_enabled(False)
        self.refresh()
        self._attach_word_lookup()
        self.translation_button.setChecked(bool(getattr(owner, "audiobook_show_translation", False)))

    def _control(self, text: str, key: str, slot: Any) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("audiobookControl")
        button.setFixedHeight(34)
        button.setToolTip(f"快捷键 {key}")
        _font(button, 11)
        button.clicked.connect(slot)
        return button

    def _attach_word_lookup(self) -> None:
        getter = getattr(self.owner, "_english_question_word_lookup", None)
        lookup = getter() if callable(getter) else None
        if lookup is not None:
            lookup.attach_text_browser(self.reader, lookup_on_selection=False)

    # -- books and chapters -------------------------------------------------------

    def refresh(self) -> None:
        """Re-read what is downloaded and aligned; the open chapter and playback stay."""

        try:
            rows = self.service.catalog()
        except Exception as error:
            self.status_label.setText(f"有声书数据读取失败：{error}")
            return
        remembered = self.book_key or str(getattr(self.owner, "audiobook_book_key", "") or "")
        self.book_list.blockSignals(True)
        self.book_list.clear()
        selected = None
        for row in rows:
            hours = row["seconds"] / 3600
            length = f"{hours:.1f} 小时" if hours >= 1 else f"{round(row['seconds'] / 60)} 分钟"
            ready = f"已准备 {row['aligned']}/{row['chapter_count']} 章" if row["aligned"] else "未准备"
            item = QListWidgetItem(f"{row['title_zh']}  {row['title']}\n{row['level']} · {length} · {ready}")
            item.setData(Qt.ItemDataRole.UserRole, row["key"])
            item.setToolTip(f"{row['author']}（{row['year']}）\n{row['note']}\n录音：{row['reader']}")
            self.book_list.addItem(item)
            if selected is None or row["key"] == remembered:
                selected = item
        self.rows = {row["key"]: row for row in rows}
        self.book_list.blockSignals(False)
        if selected is not None:
            self.book_list.setCurrentItem(selected)
            if selected.data(Qt.ItemDataRole.UserRole) == self.book_key:
                self._fill_chapters()  # the same book: update the chapter marks only
        self._update_preparation_controls()

    def open_book(self, key: str) -> None:
        if not key or key == self.book_key:
            return
        self.save_progress()
        self.stop_playback()
        self.book_key = key
        self.owner.audiobook_book_key = key
        row = self.rows[key]
        self.title_label.setText(f"{row['title_zh']}  {row['title']}")
        self.meta_label.setText(f"{row['author']}（{row['year']}）· {row['level']} · {row['reader']}\n{row['note']}")
        self._fill_chapters()
        progress = row.get("progress") or {}
        chapter = int(progress.get("chapter") or 0)
        try:
            self.service.chapter_paragraphs(key)
        except FileNotFoundError:
            self.chapter = None
            self.reader.setHtml(
                f"<p style='color:{MUTED}; font-size:15px;'>这本书还没有准备。点左下角“准备这本书”：下载录音和原文，"
                "并把每一句对上录音时间（逐章进行，第一章好了就能先听）。</p>"
            )
            self._set_player_enabled(False)
            return
        self.open_chapter(chapter, sentence=int(progress.get("sentence") or 0))

    def _fill_chapters(self) -> None:
        if not self.book_key:
            return
        row = self.rows.get(self.book_key) or {}
        heard = set((row.get("progress") or {}).get("heard_chapters") or [])
        spec = self.service.catalog_data[self.book_key]
        current = self.chapter["index"] if self.chapter else -1
        self.chapter_list.clear()
        for index, chapter in enumerate(spec["chapters"]):
            if self.service.alignment_path(self.book_key, index).is_file():
                mark = "✓ 已听" if index in heard else "可听"
            elif self.service.audio_path(self.book_key, index).is_file():
                mark = "未对齐"
            else:
                mark = "未准备"
            minutes = round(int(chapter.get("seconds") or 0) / 60)
            item = QListWidgetItem(f"{index + 1:>2}. {chapter['title']}\n      {minutes} 分钟 · {mark}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            if mark == "未准备":
                item.setForeground(QColor(MUTED))
            self.chapter_list.addItem(item)
            if index == current:
                self.chapter_list.setCurrentItem(item)

    def open_chapter(self, index: int, *, sentence: int = 0) -> None:
        if not self.book_key:
            return
        self.save_progress()
        self.stop_playback()
        try:
            self.chapter = self.service.chapter(self.book_key, index)
        except Exception as error:
            self.status_label.setText(str(error))
            return
        alignment = self.chapter.get("alignment") or {}
        self.starts = [int(item["start_ms"]) for item in alignment.get("sentences", [])]
        self.current = -1
        self.repeat_sentence = -1
        self.repeat_button.setChecked(False)
        self.selected = -1
        self._render()
        self._fill_chapters()
        audio = self.chapter["audio_path"]
        self._set_player_enabled(bool(audio))
        self.previous_button.setEnabled(bool(self.starts))
        self.next_button.setEnabled(bool(self.starts))
        self.repeat_button.setEnabled(bool(self.starts))
        if not audio:
            self.hint_label.setText("这一章还没有准备：点左下角“准备这本书”。原文可以先读。")
        elif not self.starts:
            self.hint_label.setText("这一章还没对齐，只能整章播放，不能逐句高亮。")
        else:
            self.hint_label.setText(HINT)
        if audio:
            player = self._media()
            if player is not None:
                player.setSource(QUrl.fromLocalFile(audio))
                self._apply_speed()
                if self.starts and 0 < sentence < len(self.starts):
                    self._pending_start = self.starts[sentence]
                    self._set_current(sentence, scroll=True)
        self._update_translation()

    # -- text ---------------------------------------------------------------------

    def _render(self) -> None:
        """The chapter as one document; the character range of every sentence is kept."""

        if self.chapter is None:
            return
        document = QTextDocument(self.reader)
        # The widget's font is the source: Qt copies it onto the document on every FontChange.
        document.setDefaultFont(self.reader.font())
        cursor = QTextCursor(document)
        block = QTextBlockFormat()
        block.setBottomMargin(14)
        block.setLineHeight(150, 1)  # proportional line height
        heading = QTextCharFormat()
        heading.setFontPointSize(20)
        heading.setFontWeight(QFont.Weight.DemiBold)
        heading.setForeground(QColor(INK))
        cursor.setBlockFormat(block)
        cursor.insertText(f"{self.chapter['index'] + 1}. {self.chapter['title']}", heading)
        self.ranges = []
        space = QTextCharFormat()
        for paragraph in self.chapter["paragraphs"]:
            cursor.insertBlock(block)
            for sentence in paragraph:
                style = QTextCharFormat()
                style.setForeground(QColor(INK))
                start = cursor.position()
                cursor.insertText(sentence, style)
                self.ranges.append((start, cursor.position()))
                cursor.insertText(" ", space)
        self.reader.setDocument(document)
        self._highlight(scroll=False)

    def _highlight(self, *, scroll: bool) -> None:
        selections = []
        if 0 <= self.selected < len(self.ranges) and self.selected != self.current:
            start, end = self.ranges[self.selected]
            chosen = QTextEdit.ExtraSelection()
            chosen.cursor = QTextCursor(self.reader.document())
            chosen.cursor.setPosition(start)
            chosen.cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            chosen.format.setBackground(QColor(SELECTED))
            selections.append(chosen)
        if 0 <= self.current < len(self.ranges):
            start, end = self.ranges[self.current]
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(self.reader.document())
            selection.cursor.setPosition(start)
            selection.cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.format.setBackground(QColor(CURRENT))
            selections.append(selection)
            if scroll:
                cursor = QTextCursor(self.reader.document())
                cursor.setPosition(end)
                self.reader.setTextCursor(cursor)
                self.reader.ensureCursorVisible()
        self.reader.setExtraSelections(selections)

    def _set_current(self, number: int, *, scroll: bool = True) -> None:
        if number == self.current:
            return
        self.current = number
        if number == self.selected:
            self.selected = -1  # reached: the selection has done its job
        self._highlight(scroll=scroll)
        self._update_translation()
        self._save_timer.start()

    def sentence_at(self, position: int) -> int:
        """The sentence holding character ``position`` of the document, or -1 (a heading, a gap)."""

        starts = [start for start, _end in self.ranges]
        number = bisect.bisect_right(starts, position) - 1
        if 0 <= number < len(self.ranges) and position <= self.ranges[number][1]:
            return number
        return -1

    def select_sentence(self, number: int) -> None:
        """Mark a sentence: its translation shows, and play starts from it.  Nothing plays yet."""

        if not 0 <= number < len(self.ranges):
            return
        self.selected = -1 if number == self.current else number
        self._highlight(scroll=False)
        self._update_translation()

    # -- translation ---------------------------------------------------------------

    def _show_translation_bar(self, shown: bool) -> None:
        self.owner.audiobook_show_translation = bool(shown)
        self.translation_bar.setVisible(bool(shown))
        self._update_translation()

    def _update_translation(self) -> None:
        if not self.translation_bar.isVisibleTo(self) or self.chapter is None:
            return
        number = self.selected if self.selected >= 0 else max(0, self.current)
        translations = self.chapter.get("translations")
        if not translations:
            self.translation_number.setText("")
            self.translation_label.setText("本章还没有译文，可以让 Work 逐句翻译后写入。")
            return
        if not 0 <= number < len(translations):
            return
        self.translation_number.setText(f"第 {number + 1} 句" + ("（选中）" if number == self.selected else ""))
        self.translation_label.setText(translations[number])

    # -- playback -------------------------------------------------------------------

    def _media(self) -> Any:
        if self._player is None:
            try:
                from PySide6.QtMultimedia import QMediaPlayer
            except ImportError:
                self.hint_label.setText("本机的 Qt 缺少多媒体组件，无法播放。")
                return None
            # The outputs live in a holder made before the player: a widget deletes its
            # children in creation order, and an output deleted after its player calls
            # into the deleted player (a crash when the page is closed).
            self._outputs = QObject(self)
            self._player = QMediaPlayer(self)
            self._use_default_output()
            # Speakers, headphones or a display chosen in System Settings while the page is open.
            from PySide6.QtMultimedia import QMediaDevices

            self._devices = QMediaDevices(self)
            self._devices.audioOutputsChanged.connect(self._use_default_output)
            self._player.durationChanged.connect(self._duration_changed)
            self._player.mediaStatusChanged.connect(self._media_status)
            self._player.playbackStateChanged.connect(lambda _state: self._update_play_button())
            self._player.errorOccurred.connect(lambda *_: self.hint_label.setText(f"播放失败：{self._player.errorString()}"))
        return self._player

    def _use_default_output(self) -> None:
        """A fresh audio output on the system's current default device.

        An output kept from earlier can point at a device id CoreAudio no
        longer resolves ("Unable to find most recent CoreAudio AudioDeviceID");
        the player then runs silently.  Resolving the device just before
        playing, and again when the devices change, follows what the system
        plays on now.
        """

        from PySide6.QtMultimedia import QAudioOutput, QMediaDevices

        if self._player is None:
            return
        device = QMediaDevices.defaultAudioOutput()
        old = self._output
        self._output = QAudioOutput(device, self._outputs) if not device.isNull() else QAudioOutput(self._outputs)
        self._output.setVolume(1.0)
        self._player.setAudioOutput(self._output)  # the old output is detached first
        if old is not None:
            old.deleteLater()

    def _set_player_enabled(self, enabled: bool) -> None:
        for widget in (self.play_button, self.slider, self.speed_combo):
            widget.setEnabled(enabled)
        if not enabled:
            for widget in (self.previous_button, self.next_button, self.repeat_button):
                widget.setEnabled(False)
            self.position_label.setText("0:00")
            self.duration_label.setText("0:00")

    def _duration_changed(self, duration: int) -> None:
        self.slider.setRange(0, max(0, int(duration)))
        self.duration_label.setText(_clock(duration))

    def _media_status(self, status: Any) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) \
                and self._pending_start is not None:
            # A sentence chosen while the recording was loading; seeking earlier is ignored.
            pending, self._pending_start = self._pending_start, None
            self._player.setPosition(pending)
            self._follow()
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.chapter is not None:
            self._tick.stop()
            self.service.save_progress(self.book_key, chapter=self.chapter["index"],
                                       sentence=max(0, self.current), finished_chapter=True)
            self.refresh()
            self.hint_label.setText("这一章听完了。点左边的下一章继续。")

    def _apply_speed(self) -> None:
        if self._player is not None:
            self._player.setPlaybackRate(float(self.speed_combo.currentData() or 1.0))

    def _update_play_button(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        playing = self._player is not None and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText("⏸" if playing else "▶")

    def toggle_play(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        player = self._player
        if player is None or not self.play_button.isEnabled():
            return
        if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            player.pause()
            self._tick.stop()
            self.save_progress()
        else:
            position = int(player.position())
            if 0 <= self.selected < len(self.starts):
                # Play from the sentence clicked last.
                position = self.starts[self.selected]
                if self.repeat_sentence >= 0:
                    self.repeat_sentence = self.selected
                self._set_current(self.selected)
            self._use_default_output()  # a new output starts the stream again; keep the place
            if position > 0:
                self._seek(position)
            player.play()
            self._tick.start()

    def stop_playback(self) -> None:
        self._tick.stop()
        if self._player is not None:
            self._player.stop()
        self._update_play_button() if self._player is not None else None

    def _seek(self, position: int) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._player is None:
            return
        if self._player.mediaStatus() not in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                                              QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.EndOfMedia):
            self._pending_start = max(0, int(position))  # applied once the recording has loaded
            return
        self._player.setPosition(max(0, int(position)))
        self._follow()

    def _sentence_at(self, position: int) -> int:
        return max(0, bisect.bisect_right(self.starts, position) - 1) if self.starts else -1

    def _follow(self) -> None:
        """Every 100 ms: the position, the sentence being read, and the repeated sentence."""

        player = self._player
        if player is None:
            return
        position = int(player.position())
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self.position_label.setText(_clock(position))
        if not self.starts:
            return
        if 0 <= self.repeat_sentence < len(self.starts):
            end = self.chapter["alignment"]["sentences"][self.repeat_sentence]["end_ms"]
            if position >= end - 60 or position < self.starts[self.repeat_sentence] - 500:
                player.setPosition(self.starts[self.repeat_sentence])
                return
        self._set_current(self._sentence_at(position))

    def _go_to_sentence(self, number: int) -> None:
        if not self.starts:
            return
        number = max(0, min(len(self.starts) - 1, number))
        self.selected = -1
        if self.repeat_sentence >= 0:
            self.repeat_sentence = number
        self._seek(self.starts[number])
        self._set_current(number)

    def previous_sentence(self) -> None:
        if self.starts:
            self._go_to_sentence(max(0, self.current) - 1 if self.current > 0 else 0)

    def next_sentence(self) -> None:
        if self.starts:
            self._go_to_sentence(self.current + 1)

    def toggle_repeat(self) -> None:
        if not self.starts:
            self.repeat_button.setChecked(False)
            return
        self.repeat_sentence = max(0, self.current) if self.repeat_button.isChecked() else -1

    def save_progress(self) -> None:
        if self.book_key and self.chapter is not None:
            try:
                self.service.save_progress(self.book_key, chapter=self.chapter["index"], sentence=max(0, self.current))
            except Exception:
                pass  # progress is a convenience; listening goes on

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        # Leaving the page pauses the book where it was.
        if self._player is not None:
            self._player.pause()
            self._tick.stop()
        self.save_progress()
        super().hideEvent(event)

    # -- preparation -------------------------------------------------------------

    def _update_preparation_controls(self) -> None:
        running = getattr(self.owner, "_audiobook_task", None) is not None
        row = self.rows.get(self.book_key) or {}
        complete = bool(row) and row.get("aligned") == row.get("chapter_count")
        self.prepare_button.setEnabled(not running and bool(self.book_key) and not complete)
        self.prepare_button.setText("正在准备…" if running else "已全部准备" if complete
                                    else "继续准备" if row.get("aligned") else "准备这本书")
        self.stop_button.setVisible(running)
        text = str(getattr(self.owner, "_audiobook_status", "") or "")
        if text:
            self.status_label.setText(text)

    def prepare_book(self) -> None:
        if not self.book_key or getattr(self.owner, "_audiobook_task", None) is not None:
            return
        key = self.book_key
        stop = threading.Event()
        task = _Task(lambda: None)
        owner = self.owner

        def run() -> Any:
            return self.service.prepare(key, progress=task.signals.progress.emit, stop=stop)

        task.function = run

        def show(text: str) -> None:
            owner._audiobook_status = text
            view = getattr(owner, "audiobook_view", None)
            if view is not None and isValid(view):
                view.status_label.setText(text)
                view.refresh()  # a chapter that became ready is marked at once

        def finished(result: Any) -> None:
            owner._audiobook_task = None
            owner._audiobook_stop = None
            if isinstance(result, dict) and result.get("error"):
                text = f"准备失败：{result['error']}"
            else:
                done = len(result.get("aligned_chapters") or [])
                text = result.get("paused") or f"《{result['title']}》已准备 {done}/{result['chapter_count']} 章。"
                warnings = [f"第 {item['chapter'] + 1} 章：{item['warning']}" for item in result.get("prepared", []) if item.get("warning")]
                if warnings:
                    text += "\n" + "\n".join(warnings)
            show(text)
            view = getattr(owner, "audiobook_view", None)
            if view is not None and isValid(view) and view.book_key == key:
                if view.chapter is None:
                    view.book_key = ""
                    view.open_book(key)  # the text arrived with the first chapter
                elif not view.chapter.get("alignment"):
                    view.open_chapter(view.chapter["index"])  # the open chapter just became ready

        task.signals.progress.connect(show)
        task.signals.finished.connect(finished)
        owner._audiobook_task = task
        owner._audiobook_stop = stop
        owner._audiobook_status = "开始准备…"
        self._update_preparation_controls()
        QThreadPool.globalInstance().start(task)
        if self.chapter is None:
            QTimer.singleShot(4000, lambda: self._open_when_text_ready(key))

    def _open_when_text_ready(self, key: str) -> None:
        """The first chapter's text arrives within seconds; show it while the audio downloads."""

        if not isValid(self) or self.book_key != key or self.chapter is not None:
            return
        if self.service.source_path(key).is_file():
            self.book_key = ""
            self.open_book(key)
        elif getattr(self.owner, "_audiobook_task", None) is not None:
            QTimer.singleShot(3000, lambda: self._open_when_text_ready(key))

    def stop_preparation(self) -> None:
        stop = getattr(self.owner, "_audiobook_stop", None)
        if stop is not None:
            stop.set()
            self.status_label.setText("这一章完成后停止…")


def build_audiobook_page(owner: Any) -> QVBoxLayout:
    """The 有声书 page (left navigation of the English workspace)."""

    layout = QVBoxLayout()
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(12)
    note = (("先读懂，再用听力巩固：选读得懂的书的有声书，把阅读能力延伸到听力。"
             if APP_PATHS.public_release else
             "旋元佑的方法：先用广读打好阅读基础，再听读得懂的书的有声书，把能力延伸到听力。") + (
            "这里的书配的是 LibriVox 公版真人朗读和古腾堡原文，播放时高亮正在读的句子；每一句都有中文译文，按“译文”打开。"))
    heading = getattr(owner, "_english_page_heading", None)
    if callable(heading):
        layout.addLayout(heading(PAGE_NAME, note))
    else:
        title = QLabel(PAGE_NAME)
        title.setObjectName("pageTitle")
        _font(title, 20, QFont.Weight.DemiBold)
        layout.addWidget(title)
        layout.addWidget(QLabel(note))
    try:
        view = AudiobookView(owner)
    except Exception as error:
        failure = QLabel(f"有声书页面打开失败：{error}")
        failure.setWordWrap(True)
        layout.addWidget(failure)
        layout.addStretch(1)
        return layout
    owner.audiobook_view = view
    layout.addWidget(view, 1)
    return layout


__all__ = ["AudiobookView", "PAGE_NAME", "build_audiobook_page"]
