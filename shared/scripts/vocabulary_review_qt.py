"""Qt surfaces of the vocabulary review system (背单词).

* ``build_vocabulary_review_page`` - the "背单词" page of the English workspace:
  today's queue in one line and the book's sections as study units.
* ``VocabularyStudyWindow`` - one card at a time in one fixed layout, four
  review modes and four grades.
* ``add_reader_vocabulary_button`` / ``open_study_for_reader`` - the reader's
  "背单词" button, which opens the section being read (or, in any other
  material, the words looked up in it).
* ``WordLookup`` - the card's double-click/drag word lookup for any other
  surface (the question bank's cards): the same rule, popup and service.

All data goes through ``VocabularyReviewService`` and ``VocabularyManager``.
Book audio is the original MP3 played with QtMultimedia; words without book
audio use the project's existing ``EnglishSpeechService`` TTS.
"""

from __future__ import annotations

import html
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from shiboken6 import isValid
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.application_paths import APP_PATHS, resolve_stored_path
from shared.scripts.vocabulary_manager import REVIEW_GRADES, schedule_review
from shared.scripts.vocabulary_review_service import (
    VOCABULARY_UNIT_KEY,
    VOCABULARY_UNIT_TITLE,
    RAYDU_BOOK_TITLE,
    REVIEW_MODES,
    VocabularyReviewService,
    grade_typed_answer,
)

INK = "#1D232B"
MUTED = "#8A929C"
SOFT = "#5E6772"
ACCENT = "#2F5D7C"
HAIRLINE = "#E5E7EA"
PAPER = "#FFFFFF"
CANVAS = "#F5F6F7"
GRADE_LABELS = {"again": "重来", "hard": "困难", "good": "良好", "easy": "简单"}
MODE_CHOICES = [("mixed", "混合"), *REVIEW_MODES.items()]
SERIF = "Georgia, 'Times New Roman', serif"
SANS = (
    "'PingFang SC', 'Helvetica Neue', sans-serif" if sys.platform == "darwin"
    else "'Segoe UI', 'Microsoft YaHei UI', sans-serif"
)


def review_service_for(owner: Any) -> VocabularyReviewService:
    """One review service per control center, built from its existing services."""

    service = getattr(owner, "_vocabulary_review_service", None)
    if service is None:
        manager = getattr(getattr(owner, "service", None), "vocabulary_manager", None)
        service = VocabularyReviewService(
            manager,
            english_service=getattr(owner, "english_service", None),
            dictionary_service=getattr(owner, "english_dictionary_service", None),
        )
        owner._vocabulary_review_service = service
    return service


def _font(widget: QWidget, size: float, weight: QFont.Weight = QFont.Weight.Normal) -> None:
    font = widget.font()
    font.setFamilies(
        ["PingFang SC", "Helvetica Neue"] if sys.platform == "darwin"
        else ["Segoe UI Variable", "Microsoft YaHei UI", "Microsoft YaHei", "Arial"]
    )
    font.setPointSizeF(size)
    font.setWeight(weight)
    widget.setFont(font)


def _interval_text(due_at: str, now: datetime) -> str:
    try:
        due = datetime.strptime(due_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""
    minutes = max(1, round((due - now).total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    if minutes < 60 * 20:
        return f"{round(minutes / 60)} 小时"
    days = max(1, round(minutes / 1440))
    if days < 60:
        return f"{days} 天"
    return f"{round(days / 30)} 个月"


class _TaskSignals(QObject):
    finished = Signal(object)
    progress = Signal(object)


class _Task(QRunnable):
    """Run ``function`` on the thread pool and deliver its result on the UI thread."""

    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = _TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as error:
            result = {"error": str(error)}
        try:
            self.signals.finished.emit(result)
        except RuntimeError:
            pass  # the window closed while the task ran


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

class AudioPlayer(QObject):
    """Book MP3 through QtMultimedia, otherwise the existing English TTS."""

    status_changed = Signal(str)

    def __init__(self, speech_service: Any = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.speech_service = speech_service
        self._player: Any = None
        self._output: Any = None
        self._pending: list[str] = []
        self._generation = 0
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._timed_out)

    def _timed_out(self) -> None:
        self.stop()
        self.status_changed.emit("朗读失败：播放超时，请检查网络或语音服务。")

    def _report_status(self, generation: int, message: str) -> None:
        if generation == self._generation and isValid(self):
            try:
                self.status_changed.emit(message)
            except RuntimeError:
                pass

    def _media(self) -> Any:
        if self._player is None:
            try:
                from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            except ImportError:
                return None
            self._player = QMediaPlayer(self)
            self._output = QAudioOutput(self)
            self._output.setVolume(1.0)
            self._player.setAudioOutput(self._output)
            self._player.mediaStatusChanged.connect(self._on_media_status)
            self._player.errorOccurred.connect(lambda *_: self._media_error())
        return self._player

    def _on_media_status(self, status: Any) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._pending:
                self._player.setSource(QUrl.fromLocalFile(self._pending.pop(0)))
                self._player.play()
            else:
                self._timeout.stop()

    def _media_error(self) -> None:
        detail = self._player.errorString()
        self.stop()
        self.status_changed.emit(f"朗读失败：{detail or '录音无法播放'}")

    def play_sequence(self, items: list[tuple[str, str]]) -> str:
        """Play ``(audio_path, text)`` items one after another (the word, then its examples).

        Book MP3s are chained on the media player; if any item lacks book audio the
        whole sequence is read by TTS instead, so the order is never mixed up.
        """

        items = [(path, text) for path, text in items if path or text]
        if not items:
            return ""
        if all(path and Path(path).is_file() for path, _text in items) and self._media() is not None:
            self.play(items[0][0])
            self._pending = [path for path, _text in items[1:]]
            return "book"
        return self.play("", "\n\n".join(text for _path, text in items if text), mode="paragraph")

    def stop(self) -> None:
        self._generation += 1
        self._timeout.stop()
        self._pending = []
        if self._player is not None:
            self._player.stop()
        if self.speech_service is not None:
            try:
                self.speech_service.stop()
            except Exception:
                pass

    def play(self, audio_path: str = "", text: str = "", *, mode: str = "word") -> str:
        """Play ``audio_path`` when it exists, else speak ``text``; returns the backend used."""

        self.stop()
        self._pending = []
        if audio_path and Path(audio_path).is_file():
            player = self._media()
            if player is not None:
                player.setSource(QUrl.fromLocalFile(str(audio_path)))
                player.play()
                self._timeout.start(300_000)
                return "book"
        if text and self.speech_service is not None:
            try:
                generation = self._generation
                self.speech_service.play(text, mode=mode, on_status=lambda message: self._report_status(generation, message))
                return "tts"
            except Exception as error:
                self.status_changed.emit(f"朗读失败：{error}")
                return ""
        self.status_changed.emit("朗读失败：没有可用的录音或语音组件。")
        return ""


# ---------------------------------------------------------------------------
# Card rendering (one layout for every card)
# ---------------------------------------------------------------------------

CARD_CSS = f"""
body {{ color:{INK}; font-family:{SANS}; }}
a {{ color:{ACCENT}; text-decoration:none; }}
.head {{ font-family:{SERIF}; font-size:40px; color:{INK}; }}
.ipa {{ color:{MUTED}; font-size:15px; }}
.pos {{ color:{MUTED}; font-size:12px; }}
.sense {{ font-size:17px; }}
.morph {{ font-family:{SERIF}; font-size:17px; color:{ACCENT}; }}
.meaning {{ color:{SOFT}; font-size:13px; }}
.label {{ color:{ACCENT}; font-size:12px; font-weight:600; }}
.derived {{ font-size:14px; }}
.dterm {{ font-family:{SERIF}; font-size:16px; color:{INK}; }}
.ddef {{ font-size:14px; color:{INK}; }}
.dmore {{ font-size:14px; color:{SOFT}; }}
.en {{ font-family:{SERIF}; font-size:16px; color:{INK}; }}
.zh {{ color:{SOFT}; font-size:13px; }}
.num {{ color:{MUTED}; font-size:12px; }}
.source {{ color:{MUTED}; font-size:12px; }}
.crumb {{ color:{MUTED}; font-size:12px; }}
.prompt {{ font-family:{SERIF}; font-size:22px; color:{INK}; }}
.hint {{ color:{SOFT}; font-size:14px; }}
.ok {{ color:#2E7D5B; font-size:14px; }}
.bad {{ color:#B24A4A; font-size:14px; }}
"""
POS_CHIP = "#EAF1F6"
NEW_CHIP = ("#2E7D5B", "#E6F3EC")
REVIEW_CHIP = (ACCENT, POS_CHIP)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _play_link(target: str) -> str:
    return f'<a href="play:{_esc(target)}">▶</a>'


def _rule() -> str:
    return f'<table width="100%" cellspacing="0" cellpadding="0"><tr><td style="border-top:1px solid {HAIRLINE};"></td></tr></table>'


def _chip(text: Any, color: str = SOFT, background: str = POS_CHIP) -> str:
    if not str(text or "").strip():
        return ""
    return f'<span style="background-color:{background}; color:{color}; font-size:12px;">&nbsp;{_esc(text)}&nbsp;</span>'


def _section(label: str, body: str) -> str:
    """One labelled block of the card's back: the label in a narrow left column."""

    return (
        '<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:16px;"><tr>'
        f'<td width="54" valign="top" style="padding-top:3px;"><span class="label">{_esc(label)}</span></td>'
        f'<td valign="top">{body}</td></tr></table>'
    )


def _derived_rows(card: dict[str, Any]) -> str:
    """The book's 衍生 words, then the 派生 words the local dictionaries add (grey meanings)."""

    rows = []
    items = [*card.get("derived", []), *card.get("derived_more", [])]
    for index, item in enumerate(items):
        meaning_class = "dmore" if item.get("source") == "dictionary" else "ddef"
        rows.append(
            f'<tr><td valign="top" style="padding:2px 12px 4px 0;"><a href="say:{index}"><span class="dterm">{_esc(item.get("term"))}</span></a></td>'
            f'<td valign="top" style="padding:4px 8px 4px 0;">{_chip(item.get("pos"))}</td>'
            f'<td valign="top" style="padding:3px 0 4px 0;"><span class="{meaning_class}">{_esc(item.get("definition"))}</span></td></tr>'
        )
    if not rows:
        return ""
    table = f'<table cellspacing="0" cellpadding="0">{"".join(rows)}</table>'
    if card.get("derived_more"):
        table += '<p class="source" style="margin-top:2px;">灰色释义由本地词典补充（ECDICT · Etymological WordNet）</p>'
    return table


def derived_items(card: dict[str, Any]) -> list[dict[str, Any]]:
    """The 派生 rows in the order :func:`card_html` numbers its ``say:`` links."""

    return [*card.get("derived", []), *card.get("derived_more", [])]


def card_html(card: dict[str, Any], question: dict[str, Any] | None = None, *,
              reveal: bool = True, result: dict[str, Any] | None = None) -> str:
    """The card as HTML: front (per mode) or the full back, always in the same order."""

    parts = [f"<html><head><style>{CARD_CSS}</style></head><body>"]
    mode = (question or {}).get("mode", "recognize")
    if not reveal:
        parts.append('<p style="margin-top:48px;"></p>')
        if mode == "recognize":
            parts.append(f'<p align="center"><span class="head">{_esc(card["term"])}</span></p>')
            if card.get("pronunciation"):
                parts.append(f'<p align="center" class="ipa">{_esc(card["pronunciation"])}&nbsp;&nbsp;{_play_link("word")}</p>')
            else:
                parts.append(f'<p align="center">{_play_link("word")}</p>')
            parts.append('<p align="center" class="hint" style="margin-top:36px;">想一想它的意思，再按空格看答案</p>')
        elif mode == "dictation_word":
            parts.append(f'<p align="center">{_chip(REVIEW_MODES[mode], ACCENT)}</p>')
            parts.append(f'<p align="center" class="prompt" style="margin-top:22px;">{_play_link("question")}&nbsp; 听发音，拼写这个词</p>')
        elif mode == "dictation_cloze":
            parts.append(f'<p>{_chip(REVIEW_MODES[mode], ACCENT)}</p>')
            parts.append(f'<p class="prompt" style="margin-top:18px;">{_esc(question.get("prompt"))}&nbsp;&nbsp;{_play_link("question")}</p>')
            if question.get("hint"):
                parts.append(f'<p class="hint">{_esc(question["hint"])}</p>')
        else:  # spell
            parts.append(f'<p>{_chip(REVIEW_MODES["spell"], ACCENT)}</p>')
            for sense in card.get("senses", []):
                parts.append(f'<p class="sense" style="margin-top:12px;">{_chip(sense.get("pos"))}&nbsp; {_esc(sense.get("definition"))}</p>')
            for morph in card.get("morph", [])[:1]:
                parts.append(f'<p class="meaning" style="margin-top:12px;">字根提示：{_esc(morph.get("meanings"))}</p>')
        parts.append("</body></html>")
        return "".join(parts)

    if result is not None:
        if result.get("correct"):
            parts.append('<p class="ok">✓ 正确</p>')
        else:
            parts.append(
                f'<p class="bad">✗ 你的答案 <b>{_esc(result.get("answer") or "（空）")}</b>'
                f' · 正确 <b>{_esc(result.get("expected"))}</b></p>'
            )
    state = str((card.get("review") or {}).get("state") or "new")
    status = _chip("新词", *NEW_CHIP) if state == "new" or mode == "learn" else _chip("复习", *REVIEW_CHIP)
    crumb = f'&nbsp;&nbsp;<span class="crumb">{_esc(card.get("unit_path"))}</span>' if card.get("unit_path") else ""
    parts.append(f"<p>{status}{crumb}</p>")
    parts.append(f'<p style="margin-top:6px;"><span class="head">{_esc(card["term"])}</span>&nbsp;&nbsp;{_play_link("word")}</p>')
    if card.get("pronunciation"):
        parts.append(f'<p class="ipa">{_esc(card["pronunciation"])}</p>')
    for sense in card.get("senses", []):
        parts.append(f'<p class="sense" style="margin-top:8px;">{_chip(sense.get("pos"))}&nbsp; {_esc(sense.get("definition"))}</p>')
    if card.get("morph"):
        body = "".join(
            f'<span class="morph">{_esc(morph.get("segments"))}</span>'
            f'&nbsp;&nbsp;&nbsp;<span class="meaning">{_esc(morph.get("meanings"))}</span><br/>'
            for morph in card["morph"]
        )
        parts.append(_section("构词", body))
    if card.get("etymology_zh"):
        body = f'<span class="meaning">{_esc(card["etymology_zh"])}</span>'
        if card.get("etymology_note"):
            body += f'<br/><span class="source">{_esc(card["etymology_note"])}</span>'
        parts.append(_section("字源", body))
    derived = _derived_rows(card)
    if derived:
        parts.append(_section("派生", derived))
    if card.get("synonyms"):
        parts.append(_section("同义", f'<span class="derived">{_esc(", ".join(card["synonyms"]))}</span>'))
    if card.get("examples"):
        rows = []
        for index, example in enumerate(card["examples"]):
            rows.append(
                f'<p style="margin-top:{0 if index == 0 else 10}px;"><span class="num">{index + 1}&nbsp; </span>'
                f'<span class="en">{_esc(example.get("en"))}</span>&nbsp; {_play_link(f"example:{index}")}<br/>'
                f'<span class="num">&nbsp;&nbsp;&nbsp; </span><span class="zh">{_esc(example.get("zh"))}</span>'
                + (f'<br/><span class="num">&nbsp;&nbsp;&nbsp; {_esc(example["attribution"])}</span>' if example.get("attribution") else "")
                + "</p>"
            )
        parts.append(_section("例句", "".join(rows)))
    parts.append('<p style="margin-top:14px;"></p>')
    parts.append(_rule())
    sources = []
    if card.get("source_kind") == "book" and card.get("unit_path"):
        # The merged books are one book: the word's unit in the one directory and its page.
        location = (card.get("locations") or [{}])[0]
        page = f' · 第 {location["page"]} 页' if location.get("page") else ""
        sources.append(f'{_esc(card["unit_path"])}{page}&nbsp;&nbsp;<a href="open:0">在阅读器中打开</a>')
    locations = [] if sources else card.get("locations", []) if card.get("source_kind") == "book" else card.get("locations", [])[:3]
    for index, location in enumerate(locations):
        if card.get("source_kind") == "book":
            band = f' › {_esc(location.get("band"))}' if location.get("band") else ""
            page = f' · 第 {location["page"]} 页' if location.get("page") else ""
            # The GRE path names its book already ("《旋元佑GRE字汇通》Section 1 … › a-: without").
            path = str(location.get("path") or "")
            title = "" if path.startswith("《") else f'《{_esc(card.get("book_title") or RAYDU_BOOK_TITLE)}》 '
            band = "" if location.get("deck", card.get("deck")) == "gre" else band
            sources.append(f'{title}{_esc(path)}{band}{page}'
                           f'&nbsp;&nbsp;<a href="open:{index}">在阅读器中打开</a>')
        else:
            page = f' 第 {location["page"]} 页' if location.get("page") else ""
            sources.append(f'查于《{_esc(location.get("material") or "阅读材料")}》{page}'
                           f'&nbsp;&nbsp;<a href="open:{index}">回到原文</a>')
    if card.get("source_kind") == "vocabulary":
        sources.insert(0, "收藏于词汇库")
    if sources:
        parts.append(f'<p class="source">{"<br/>".join(sources)}</p>')
    parts.append("</body></html>")
    return "".join(parts)


def message_html(title: str, lines: list[str], *, figures: list[tuple[str, str]] | None = None) -> str:
    """A centred message on the card: the welcome and the end of a round."""

    parts = [f"<html><head><style>{CARD_CSS}</style></head><body>", '<p style="margin-top:56px;"></p>']
    parts.append(f'<p align="center"><span class="head" style="font-size:30px;">{_esc(title)}</span></p>')
    if figures:
        cells = "".join(
            f'<td align="center" style="padding:0 22px;"><span style="font-family:{SERIF}; font-size:34px; color:{INK};">{_esc(value)}</span>'
            f'<br/><span class="num">{_esc(label)}</span></td>'
            for value, label in figures
        )
        parts.append(f'<table align="center" cellspacing="0" cellpadding="0" style="margin-top:26px;"><tr>{cells}</tr></table>')
    for line in lines:
        parts.append(f'<p align="center" class="hint" style="margin-top:14px;">{line}</p>')
    parts.append("</body></html>")
    return "".join(parts)


def lookup_selection(selected: str) -> str:
    """The selected text worth looking up: English letters, at most a short phrase."""

    text = str(selected or "").replace(" ", " ").strip()
    if text and any(char.isascii() and char.isalpha() for char in text) and len(text) <= 80:
        return text
    return ""


class CardView(QTextBrowser):
    """The card; double-clicking a word or selecting a phrase asks for its meaning."""

    lookup_requested = Signal(str, QPoint)
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(34)
        self.setStyleSheet(f"QTextBrowser {{ background:{PAPER}; border:1px solid {HAIRLINE}; border-radius:18px; }}")
        self._press_position: QPoint | None = None

    def _emit_selection(self, global_position: QPoint) -> None:
        text = lookup_selection(self.textCursor().selectedText())
        if text:
            self.lookup_requested.emit(text, global_position)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self._press_position = event.position().toPoint()
        self.dismissed.emit()  # a click on the card closes an open lookup
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        super().mouseDoubleClickEvent(event)
        self._emit_selection(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        super().mouseReleaseEvent(event)
        start = self._press_position
        if start is not None and (event.position().toPoint() - start).manhattanLength() > 6:
            self._emit_selection(event.globalPosition().toPoint())  # a dragged word or phrase


# ---------------------------------------------------------------------------
# Study panel (embedded in the 背单词 page) and its stand-alone window
# ---------------------------------------------------------------------------

GRADE_COLORS = {"again": "#B9534B", "hard": "#B9822C", "good": "#2E7D5B", "easy": "#2F6496"}
GRADE_TINTS = {"again": "#FBEFEE", "hard": "#FBF4E8", "good": "#EAF5EF", "easy": "#EAF1F8"}

STUDY_CSS = f"""
QWidget#studyRoot {{ background:{CANVAS}; }}
QLabel {{ color:{INK}; background:transparent; }}
QLabel#muted {{ color:{MUTED}; }}
QLineEdit {{ background:{PAPER}; border:1px solid {HAIRLINE}; border-radius:12px;
             padding:10px 14px; color:{INK}; }}
QLineEdit:focus {{ border-color:{ACCENT}; }}
QComboBox {{ background:{PAPER}; border:1px solid {HAIRLINE}; border-radius:8px; padding:4px 10px; color:{INK}; }}
QPushButton {{ background:{PAPER}; border:1px solid {HAIRLINE}; border-radius:12px;
               padding:9px 16px; color:{INK}; }}
QPushButton:hover {{ border-color:{ACCENT}; }}
QPushButton#primary {{ background:{ACCENT}; border-color:{ACCENT}; color:white; padding:11px 16px; }}
QPushButton#primary:hover {{ background:#264D67; }}
QProgressBar#studyProgress {{ background:rgba(47, 93, 124, 28); border:0; border-radius:3px; }}
QProgressBar#studyProgress::chunk {{ background:{ACCENT}; border-radius:3px; }}
""" + "".join(
    f"""
QPushButton#grade_{grade} {{ background:{GRADE_TINTS[grade]}; border:1px solid {GRADE_TINTS[grade]};
                              color:{color}; border-radius:12px; padding:8px 6px; }}
QPushButton#grade_{grade}:hover {{ background:{color}; color:white; }}
QPushButton#grade_{grade}[suggested="true"] {{ border:2px solid {color}; }}
"""
    for grade, color in GRADE_COLORS.items()
)


class VocabularyStudyPanel(QWidget):
    """Review one queue (today's, one unit's, or explicit cards) card by card.

    The 背单词 page holds one panel beside its directory and calls :meth:`start`
    for each unit; :class:`VocabularyStudyWindow` is the same panel on its own.
    """

    reviewed = Signal()
    next_unit_requested = Signal()

    def __init__(
        self,
        owner: Any,
        *,
        unit_key: str = "",
        title: str = "今日复习",
        card_keys: list[str] | None = None,
        autoload: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.owner = owner
        self.service = review_service_for(owner)
        self.unit_key = unit_key
        self.explicit_keys = card_keys
        self.audio = AudioPlayer(getattr(getattr(owner, "english_service", None), "speech_service", None), self)
        self.audio.status_changed.connect(self._audio_status)
        dictionary = getattr(self.service, "dictionary_service", None)
        for warm in ("warm_lookup_caches_async", "warm_optional_sources_async"):
            if callable(getattr(dictionary, warm, None)):
                getattr(dictionary, warm)()  # the first word lookup then answers at once
        self.queue: list[str] = []
        self.card: dict[str, Any] | None = None
        self.question: dict[str, Any] | None = None
        self.revealed = False
        self.result: dict[str, Any] | None = None
        self.started = 0.0
        self.done_count = 0
        self.grade_counts = {grade: 0 for grade in REVIEW_GRADES}
        self.next_unit_title = ""  # set by the page: the section after this one
        self._audio_pending = ""  # card whose 词汇库 audio is being prepared
        self._replay_when_ready = False
        self._tasks: list[_Task] = []
        self._closed = False
        self.setStyleSheet(STUDY_CSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.crumb_label = QLabel("")
        self.crumb_label.setObjectName("muted")
        _font(self.crumb_label, 11)
        self.title_label = QLabel(title)
        _font(self.title_label, 18, QFont.Weight.DemiBold)
        self.title_label.setWordWrap(True)
        titles.addWidget(self.crumb_label)
        titles.addWidget(self.title_label)
        self.mode_combo = QComboBox()
        for value, label in MODE_CHOICES:
            self.mode_combo.addItem(label, value)
        self.mode_combo.setToolTip("复习方式：混合按熟练度自动选择")
        _font(self.mode_combo, 12)
        self.mode_combo.currentIndexChanged.connect(lambda _index: self._show_current(rechoose=True))
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("muted")
        _font(self.progress_label, 12)
        header.addLayout(titles, 1)
        header.addWidget(self.progress_label, 0, Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.mode_combo, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(header)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("studyProgress")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        layout.addWidget(self.progress_bar)

        self.view = CardView()
        self.view.anchorClicked.connect(self._on_link)
        self.view.lookup_requested.connect(self.show_lookup)
        self.view.dismissed.connect(self.close_lookup)
        self.lookup_popup: LookupPopup | None = None
        layout.addWidget(self.view, 1)

        self.answer_edit = QLineEdit()
        self.answer_edit.setPlaceholderText("输入拼写后按回车")
        _font(self.answer_edit, 15)
        self.answer_edit.returnPressed.connect(self.reveal)
        layout.addWidget(self.answer_edit)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.reveal_button = QPushButton("显示答案")
        self.reveal_button.setObjectName("primary")
        self.reveal_button.setMinimumHeight(48)
        _font(self.reveal_button, 13, QFont.Weight.DemiBold)
        self.reveal_button.clicked.connect(self.reveal)
        actions.addWidget(self.reveal_button, 1)
        self.grade_buttons: dict[str, QPushButton] = {}
        for number, grade in enumerate(REVIEW_GRADES, start=1):
            button = QPushButton(GRADE_LABELS[grade])
            button.setObjectName(f"grade_{grade}")
            button.setMinimumHeight(48)
            _font(button, 12, QFont.Weight.DemiBold)
            button.setToolTip(f"快捷键 {number}")
            button.clicked.connect(lambda _checked=False, value=grade: self.grade(value))
            actions.addWidget(button, 1)
            self.grade_buttons[grade] = button
        # The end of a round: go on with today's queue or the next section.
        self.continue_button = QPushButton("继续今日复习")
        self.continue_button.setObjectName("primary")
        self.continue_button.setMinimumHeight(48)
        _font(self.continue_button, 13, QFont.Weight.DemiBold)
        self.continue_button.clicked.connect(lambda: self.start(title="今日复习"))
        self.next_button = QPushButton("下一节")
        self.next_button.setMinimumHeight(48)
        _font(self.next_button, 12, QFont.Weight.DemiBold)
        self.next_button.clicked.connect(self.next_unit_requested.emit)
        actions.addWidget(self.continue_button, 1)
        actions.addWidget(self.next_button, 1)
        self.continue_button.hide()
        self.next_button.hide()
        layout.addLayout(actions)
        self.hint_label = QLabel("空格 显示答案 · 1–4 评分 · Ctrl+R 重播")
        self.hint_label.setObjectName("muted")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _font(self.hint_label, 11)
        layout.addWidget(self.hint_label)

        for number, grade in enumerate(REVIEW_GRADES, start=1):
            QShortcut(QKeySequence(str(number)), self, activated=lambda value=grade: self._shortcut_grade(value))
        QShortcut(QKeySequence("Space"), self, activated=self._shortcut_reveal)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.replay)
        if autoload:
            self.load_queue()

    # -- queue -----------------------------------------------------------

    def start(self, *, unit_key: str = "", title: str = "今日复习", card_keys: list[str] | None = None,
              crumb: str = "") -> None:
        """Begin a new round: today's queue, one unit's, or explicit cards."""

        self.close_lookup()
        self.audio.stop()
        self.unit_key = unit_key
        self.explicit_keys = card_keys
        self.done_count = 0
        self.grade_counts = {grade: 0 for grade in REVIEW_GRADES}
        self.title_label.setText(title)
        self.crumb_label.setText(crumb)
        self.load_queue()

    def load_queue(self) -> None:
        if self.explicit_keys is not None:
            self.queue = list(self.explicit_keys)
        else:
            queue = self.service.review_queue(unit_key=self.unit_key)
            self.queue = [row["card_key"] for row in queue["cards"]]
        self._next()

    def _next(self) -> None:
        self.close_lookup()
        self.audio.stop()
        if not self.queue:
            self.card = None
            self._show_finished()
            return
        key = self.queue.pop(0)
        try:
            self.card = self.service.card(key)
        except Exception as error:
            QMessageBox.warning(self, "卡片读取失败", f"{key}\n{error}")
            self._next()
            return
        self._prepare_audio()
        self._show_current(rechoose=True)

    def _prepare_audio(self) -> None:
        """A 词汇库 card that is not ready yet gets its examples, checked 字源 and audio off the UI thread."""

        card = self.card
        self._audio_pending = ""
        self._replay_when_ready = False
        if card is None or card.get("source_kind") == "book" or self.service.card_is_ready(card):
            return
        key = card["card_key"]
        self._audio_pending = key
        self.hint_label.setText("正在准备这张卡片的音频…")
        task = _Task(lambda: self.service.prepare_library_card(key))
        task.signals.finished.connect(lambda result, key=key, task=task: self._card_prepared(key, result, task))
        self._tasks.append(task)
        QThreadPool.globalInstance().start(task)

    def _card_prepared(self, key: str, result: Any, task: _Task) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        if self._closed or not isValid(self) or not isValid(self.view):
            return
        if self.card is None or self.card.get("card_key") != key:
            return
        self._audio_pending = ""
        prepared = result.get("card") if isinstance(result, dict) else None
        if isinstance(prepared, dict):
            prepared["review"] = self.card.get("review")
            self.card = prepared
            if self.question is not None:
                if self.question.get("mode") == "dictation_cloze":
                    match = next((item for item in self.card.get("examples", []) if item.get("en") == self.question.get("speak")), None)
                    self.question["audio_path"] = (match or {}).get("audio_path", "")
                else:
                    self.question["audio_path"] = self.card.get("audio_path", "")
            self.view.setHtml(card_html(self.card, self.question, reveal=self.revealed, result=self.result))
        mode = (self.question or {}).get("mode")
        self.hint_label.setText(str(result.get("error")) if isinstance(result, dict) and result.get("error") else (
            "学习新词：读完卡片后评分 · 1–4 评分 · Ctrl+R 重播" if mode == "learn"
            else "空格 显示答案 · 1–4 评分 · Ctrl+R 重播"
        ))
        if self._replay_when_ready:
            self._replay_when_ready = False
            self.replay()

    def _audio_status(self, message: str) -> None:
        if not self._closed and isValid(self):
            self.hint_label.setText(message)

    def _set_buttons(self, *, reveal: bool = False, grades: bool = False, finished: bool = False) -> None:
        self.reveal_button.setVisible(reveal)
        for button in self.grade_buttons.values():
            button.setVisible(grades)
        if not finished:
            self.continue_button.setVisible(False)
            self.next_button.setVisible(False)

    def _show_current(self, *, rechoose: bool = False) -> None:
        if self.card is None:
            return
        if rechoose:
            mode = self.service.choose_mode(self.card, str(self.mode_combo.currentData() or "mixed"))
            self.question = self.service.question_for(self.card, mode)
        self.revealed = False
        self.result = None
        self.started = time.monotonic()
        typing = self.question["mode"] not in {"recognize", "learn"}
        self.answer_edit.setVisible(typing)
        self.answer_edit.setEnabled(True)
        self.answer_edit.clear()
        self._set_buttons(reveal=True)
        self._update_progress()
        if self.question["mode"] == "learn":
            # Learning a new word: the whole card at once, then the word and every example aloud.
            self._show_back()
            self.replay()
            return
        self.hint_label.setText("空格 显示答案 · 1–4 评分 · Ctrl+R 重播")
        self.view.setHtml(card_html(self.card, self.question, reveal=False))
        if typing:
            self.answer_edit.setFocus()
        else:
            self.reveal_button.setFocus()
        if self.question["mode"] != "spell":
            self.replay()

    def _update_progress(self) -> None:
        remaining = len(self.queue) + (1 if self.card else 0)
        self.progress_label.setText(f"已完成 {self.done_count} · 剩余 {remaining}")
        self.progress_bar.setRange(0, max(1, self.done_count + remaining))
        self.progress_bar.setValue(self.done_count if remaining else max(1, self.done_count + remaining))

    def show_welcome(self, summary: dict[str, Any]) -> None:
        """Before a round: today's figures and one button."""

        self.card = None
        self.queue = []
        self.title_label.setText("今日复习")
        self.crumb_label.setText("")
        self.answer_edit.setVisible(False)
        self._set_buttons(finished=True)
        due, new = int(summary.get("due") or 0), int(summary.get("new") or 0)
        streak = int(summary.get("streak") or 0)
        figures = [(str(due), "待复习"), (str(new), "新词"), (str(summary.get("reviewed_today") or 0), "今天已复习")]
        if due or new:
            lines = [f"大约 {max(1, round((due * 8 + new * 25) / 60))} 分钟。按空格或点下面的按钮开始。"]
        else:
            lines = ["今天的任务都完成了。可以在左边的目录里挑一节继续学。"]
        if streak:
            lines.insert(0, f"已连续背词 {streak} 天")
        self.view.setHtml(message_html("准备好了吗" if due or new else "今天完成了", lines, figures=figures))
        self.continue_button.setText("开始今日复习")
        self.continue_button.setVisible(bool(due or new))
        self.next_button.setVisible(False)
        self.progress_label.setText("")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.hint_label.setText("点左边目录里的一节，背这一节的单词")

    def _show_finished(self) -> None:
        self.answer_edit.setVisible(False)
        self._set_buttons(finished=True)
        try:
            summary = self.service.today_summary()
        except Exception:
            summary = {}
        more = int(summary.get("due") or 0) + int(summary.get("new") or 0)
        figures = [(str(self.done_count), "本轮"), *(
            (str(self.grade_counts[grade]), GRADE_LABELS[grade]) for grade in REVIEW_GRADES if self.grade_counts[grade]
        )]
        lines = []
        if summary.get("streak"):
            lines.append(f"已连续背词 {summary['streak']} 天 · 今天共复习 {summary.get('reviewed_today', 0)} 张")
        lines.append("到期的卡片会按间隔重复自动安排。" if self.done_count else "这一组暂时没有要背的卡片。")
        self.view.setHtml(message_html("本轮完成" if self.done_count else "没有待背的卡片", lines, figures=figures))
        self.continue_button.setText(f"继续今日复习（{more}）")
        self.continue_button.setVisible(more > 0)
        self.next_button.setText(f"下一节：{self.next_unit_title}" if self.next_unit_title else "下一节")
        self.next_button.setVisible(bool(self.next_unit_title))  # only the page names a next section
        self._update_progress()
        self.hint_label.setText("")

    # -- answering ---------------------------------------------------------

    def _shortcut_reveal(self) -> None:
        if self.answer_edit.hasFocus():
            return
        if self.card is None:
            if self.continue_button.isVisible():
                self.continue_button.click()
            return
        self.reveal()

    def _shortcut_grade(self, grade: str) -> None:
        if self.revealed and not self.answer_edit.hasFocus():
            self.grade(grade)

    def reveal(self) -> None:
        if self.card is None or self.question is None:
            return
        if self.revealed:
            self.grade(self._suggested_grade())
            return
        if self.question["mode"] not in {"recognize", "learn"}:
            answer = self.answer_edit.text()
            check = grade_typed_answer(answer, self.question["answer"])
            self.result = {**check, "answer": answer}
            self.answer_edit.setEnabled(False)
        self._show_back()
        if self.question["mode"] == "spell":
            self.replay()

    def _show_back(self) -> None:
        self.revealed = True
        self.view.setHtml(card_html(self.card, self.question, reveal=True, result=self.result))
        self.reveal_button.setVisible(False)
        now = datetime.now()
        review = self.card.get("review") or {"state": "new"}
        suggested = self._suggested_grade()
        for grade, button in self.grade_buttons.items():
            due = schedule_review(review, grade, now)["due_at"]
            button.setText(f"{GRADE_LABELS[grade]}\n{_interval_text(due, now)}")
            button.setProperty("suggested", grade == suggested and self.result is not None)
            button.style().unpolish(button)
            button.style().polish(button)
            button.setVisible(True)
        self.grade_buttons[suggested].setFocus()
        self.hint_label.setText(
            "学习新词：读完卡片后评分 · 1–4 评分 · Ctrl+R 重播" if self.question["mode"] == "learn"
            else "空格 显示答案 · 1–4 评分 · Ctrl+R 重播"
        )

    def _suggested_grade(self) -> str:
        return str((self.result or {}).get("grade") or "good")

    def grade(self, grade: str) -> None:
        if self.card is None or not self.revealed:
            return
        try:
            outcome = self.service.record_review(
                self.card["card_key"], grade,
                mode=self.question["mode"] if self.question else "recognize",
                answer=(self.result or {}).get("answer", ""),
                correct=(self.result or {}).get("correct"),
                elapsed_ms=int((time.monotonic() - self.started) * 1000),
            )
        except Exception as error:
            QMessageBox.critical(self, "复习记录未保存", str(error))
            return
        self.done_count += 1
        self.grade_counts[grade] += 1
        card = outcome["card"]
        # Cards still in their short learning steps come back in this session.
        if card["state"] in {"learning", "relearning"} and card.get("due_at"):
            due = datetime.strptime(card["due_at"], "%Y-%m-%d %H:%M:%S")
            if (due - datetime.now()).total_seconds() <= 20 * 60:
                self.queue.append(self.card["card_key"])
        self.reviewed.emit()
        self._next()

    # -- audio and links -------------------------------------------------------

    def show_lookup(self, text: str, position: QPoint) -> None:
        """Show the popup at once ("正在查询…"), then fill it once the local lookup returns."""

        self.close_lookup()
        popup = LookupPopup(self, text, self)
        self.lookup_popup = popup
        popup.place(self.mapFromGlobal(position))
        popup.show()

        def fill() -> None:
            try:
                result = self.service.lookup_word(text)
            except Exception as error:
                result = {"selected": text, "error": str(error)}
            if self.lookup_popup is popup:
                popup.show_result(result)

        QTimer.singleShot(30, fill)

    def close_lookup(self) -> None:
        if self.lookup_popup is not None:
            try:
                self.lookup_popup.close()
                self.lookup_popup.deleteLater()
            except RuntimeError:
                pass
            self.lookup_popup = None

    def replay(self) -> None:
        if self.card is None:
            return
        if self._audio_pending == self.card.get("card_key"):
            self._replay_when_ready = True  # play once the cached audio is ready
            return
        question = self.question or {}
        if question.get("mode") == "learn":
            self.audio.play_sequence(
                [(self.card.get("audio_path", ""), self.card["term"])]
                + [(example.get("audio_path", ""), example.get("en", "")) for example in self.card.get("examples", [])]
            )
        elif not self.revealed and question.get("mode") in {"dictation_cloze"}:
            self.audio.play(question.get("audio_path", ""), question.get("speak", ""), mode="sentence")
        else:
            self.audio.play(self.card.get("audio_path", ""), self.card["term"], mode="word")

    def _on_link(self, url: QUrl) -> None:
        target = url.toString()
        if self.card is None:
            return
        if target == "play:word":
            self.audio.play(self.card.get("audio_path", ""), self.card["term"], mode="word")
        elif target == "play:question":
            self.replay()
        elif target.startswith("play:example:"):
            index = int(target.rsplit(":", 1)[1])
            example = self.card["examples"][index]
            self.audio.play(example.get("audio_path", ""), example.get("en", ""), mode="sentence")
        elif target.startswith("say:"):
            items = derived_items(self.card)
            index = int(target.split(":", 1)[1])
            if 0 <= index < len(items):
                self.audio.play("", str(items[index].get("term") or ""), mode="word")
        elif target.startswith("open:"):
            open_card_location(self.owner, self.card, int(target.split(":", 1)[1]))

    def shutdown(self) -> None:
        self._closed = True
        self.close_lookup()
        self.audio.stop()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        # Leaving the page (or closing the window) silences the card.
        self.close_lookup()
        self.audio.stop()
        super().hideEvent(event)


class VocabularyStudyWindow(VocabularyStudyPanel):
    """The study panel as a window of its own (kept for callers outside the 背单词 page)."""

    def __init__(
        self,
        owner: Any,
        *,
        unit_key: str = "",
        title: str = "今日复习",
        card_keys: list[str] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(owner, unit_key=unit_key, title=title, card_keys=card_keys, autoload=False)
        self.on_closed = on_closed
        self.setWindowTitle(f"背单词 · {title}")
        self.resize(780, 780)
        self.setObjectName("studyRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.layout().setContentsMargins(28, 22, 28, 22)
        self.load_queue()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        windows = getattr(self.owner, "_vocabulary_study_windows", [])
        if self in windows:
            windows.remove(self)
        if callable(self.on_closed):
            try:
                self.on_closed()
            except Exception:
                pass
        super().closeEvent(event)


class LookupPopup(QFrame):
    """Meaning of a word selected on a card, floating inside ``window`` like the reader's popup.

    ``source`` supplies ``service`` (VocabularyReviewService), ``audio`` and
    ``owner``: the study window itself, or a :class:`WordLookup`.
    """

    closed = Signal()

    def __init__(self, window: QWidget, selected: str, source: Any) -> None:
        super().__init__(window)
        self.window_ = window
        self.source = source
        self.result: dict[str, Any] = {"selected": selected}
        self.setObjectName("lookupPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QFrame#lookupPopup {{ background:{PAPER}; border:1px solid #C9CED6; border-radius:10px; }}"
            f"QLabel {{ color:{INK}; background:transparent; }}"
            f"QScrollArea {{ background:transparent; border:none; }}"
            f"QPushButton {{ background:{PAPER}; border:1px solid {HAIRLINE}; border-radius:8px; padding:5px 12px; }}"
            f"QPushButton:hover {{ border-color:{ACCENT}; }}"
            f"QPushButton#closeButton {{ border:none; color:{MUTED}; font-size:18px; padding:0 4px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 10, 12)
        layout.setSpacing(6)
        self.close_button = QPushButton("×", self)
        self.close_button.setObjectName("closeButton")
        self.close_button.setFixedSize(26, 26)
        self.close_button.clicked.connect(self.close)
        self.body = QLabel(f'<span style="color:{MUTED};">正在查询 {_esc(selected)} …</span>')
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.body)
        layout.addWidget(scroll, 1)
        row = QHBoxLayout()
        self.play_button = QPushButton("▶ 发音")
        self.play_button.clicked.connect(self._play)
        row.addWidget(self.play_button)
        row.addStretch(1)
        self.add_button = QPushButton("收藏到词汇库")
        self.add_button.clicked.connect(self._add)
        row.addWidget(self.add_button)
        layout.addLayout(row)
        self.audio_status = QLabel("")
        self.audio_status.setWordWrap(True)
        self.audio_status.setVisible(False)  # an empty row would take the meaning's height
        layout.addWidget(self.audio_status)
        self.source.audio.status_changed.connect(self._show_audio_status)
        self.play_button.setEnabled(False)
        self.add_button.setEnabled(False)
        self._anchor = QPoint(0, 0)

    def show_result(self, result: dict[str, Any]) -> None:
        self.result = result
        self.body.setText(lookup_html(result))
        ok = not result.get("error")
        self.play_button.setEnabled(ok)
        # Book words are collected with their book card; other words are saved to the 词汇库.
        book = bool(result.get("book"))
        collectable = ok and bool(book or result.get("in_library") or result.get("chinese") or result.get("english"))
        self.add_button.setVisible(collectable)
        if result.get("in_library"):
            self.add_button.setText("已收藏（原书卡片）" if book else "已在词汇库")
        else:
            self.add_button.setText("收藏（原书卡片）" if book else "收藏到词汇库")
        self.add_button.setEnabled(collectable and not result.get("in_library"))
        self.place(self._anchor)  # fit the height to the result

    def place(self, anchor: QPoint) -> None:
        """Size to the window and sit beside ``anchor`` (window coordinates) without leaving it."""

        self._anchor = QPoint(anchor)
        width = min(480, max(300, self.window_.width() - 48))
        content = self.body.heightForWidth(width - 46)
        layout = self.layout()
        margins = layout.contentsMargins()
        chrome = margins.top() + margins.bottom() + layout.spacing() + self.play_button.sizeHint().height()
        if not self.audio_status.isHidden():
            chrome += layout.spacing() + self.audio_status.heightForWidth(width - 28)
        height = min(int(self.window_.height() * 0.82), max(120, content + chrome + 6))
        self.setFixedSize(width, height)
        x = min(max(16, anchor.x() + 8), self.window_.width() - width - 16)
        y = anchor.y() + 18
        if y + height > self.window_.height() - 16:
            y = max(16, anchor.y() - height - 12)
        self.move(x, y)
        self.raise_()

    def _show_audio_status(self, message: str) -> None:
        self.audio_status.setText(message)
        self.audio_status.setVisible(bool(message))
        self.place(self._anchor)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.close_button.move(self.width() - 34, 8)
        self.close_button.raise_()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.closed.emit()
        super().closeEvent(event)

    def _play(self) -> None:
        book = self.result.get("book") or {}
        path = ""
        if book:
            card = self.source.service.card_index()["cards"].get(book["card_key"], {})
            candidate = self.source.service.deck_audio_dir(card.get("deck", "raydu")) / f"{card.get('audio_key', '')}.mp3"
            if book["term"].casefold() == str(self.result.get("lemma")) and candidate.is_file():
                path = str(candidate)
        self.source.audio.play(path, str(self.result.get("lemma") or self.result.get("selected")), mode="word")

    def _add(self) -> None:
        try:
            self.source.service.collect_terms([str(self.result.get("lemma") or self.result.get("selected"))])
        except Exception as error:
            # The modal deactivates the window, which dismisses and deletes this popup; a
            # dialog parented here would be destroyed inside its own event loop.
            window = self.window_
            self.close()
            QMessageBox.critical(window, "收藏失败", str(error))
            return
        self.add_button.setText("已收藏（原书卡片）" if self.result.get("book") else "已收藏到词汇库")
        self.add_button.setEnabled(False)
        start_library_preparation(self.source.owner)  # the new card is built in the background


class _LookupDismissFilter(QObject):
    """Observe outside presses without consuming the button, scrollbar or selection action."""

    def __init__(self, lookup: Any) -> None:
        super().__init__(lookup)
        self.lookup = lookup

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        popup = self.lookup.popup
        if popup is None or not isValid(popup):
            return False
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
            if watched is not popup and not popup.isAncestorOf(watched):
                self.lookup._dismissed_widget = id(watched)
                self.lookup.close()
        elif event.type() == QEvent.Type.ApplicationDeactivate or (
                event.type() == QEvent.Type.WindowDeactivate and watched is self.lookup.host.window()):
            self.lookup.close()
        return False


class WordLookup(QObject):
    """Double-click (or drag over) English in any selectable label to look it up.

    The same rule, popup and lookup service as the study card, for other
    surfaces such as the question bank.  The popup floats inside ``host``;
    a press anywhere outside it closes it immediately.
    """

    def __init__(self, host: QWidget, owner: Any) -> None:
        super().__init__(host)
        self.host = host
        self.owner = owner
        self._service: VocabularyReviewService | None = None
        self._audio: AudioPlayer | None = None
        self.popup: LookupPopup | None = None
        self._press: dict[int, QPoint] = {}
        self._double: dict[int, bool] = {}
        self._gesture = 0
        self._dismissed_widget: int | None = None
        self._dismiss_filter = _LookupDismissFilter(self)
        self._lookup_tasks: set[_Task] = set()

    @property
    def service(self) -> VocabularyReviewService:
        if self._service is None:
            self._service = review_service_for(self.owner)
            dictionary = getattr(self._service, "dictionary_service", None)
            for warm in ("warm_lookup_caches_async", "warm_optional_sources_async"):
                if callable(getattr(dictionary, warm, None)):
                    getattr(dictionary, warm)()
        return self._service

    @property
    def audio(self) -> AudioPlayer:
        if self._audio is None:
            self._audio = AudioPlayer(getattr(getattr(self.owner, "english_service", None), "speech_service", None), self)
        return self._audio

    def attach(self, widget: QWidget, *, selectable_labels: bool = False) -> None:
        """Watch every selectable rich-text label inside ``widget``."""

        labels = [widget] if isinstance(widget, QLabel) else widget.findChildren(QLabel)
        for label in labels:
            if selectable_labels:
                document = QTextDocument()
                document.setHtml(label.text())
                if any(char.isascii() and char.isalpha() for char in document.toPlainText()):
                    label.setTextInteractionFlags(label.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse)
            if label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse:
                label.installEventFilter(self)

    def attach_text_browser(self, browser: QTextBrowser, *, lookup_on_selection: bool = True) -> None:
        """Use the same dictionary popup on a reading surface; note readers keep their drag toolbar."""

        browser.setProperty("wordLookupOnSelection", lookup_on_selection)
        browser.viewport().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            self._gesture += 1
        browser = watched.parent()
        if isinstance(browser, QTextBrowser) and watched is browser.viewport():
            if kind == QEvent.Type.MouseButtonPress:
                self._dismissed_widget = None
                self._press[id(watched)] = event.position().toPoint()
                self.close()
            elif kind == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                self._press.pop(id(watched), None)
                # Capture the word at the pointer before a highlighted link can rebuild the document.
                cursor = browser.cursorForPosition(event.position().toPoint())
                cursor.select(QTextCursor.SelectionType.WordUnderCursor)
                text = lookup_selection(cursor.selectedText())
                position = event.globalPosition().toPoint()
                if text:
                    gesture = self._gesture
                    QTimer.singleShot(0, lambda word=text, at=position:
                                      self.show(word, at) if isValid(browser) and gesture == self._gesture else None)
            elif kind == QEvent.Type.MouseButtonRelease:
                start = self._press.pop(id(watched), None)
                if browser.property("wordLookupOnSelection") and start is not None \
                        and (event.position().toPoint() - start).manhattanLength() > 6:
                    position = event.globalPosition().toPoint()
                    QTimer.singleShot(0, lambda view=browser, at=position: self._lookup_browser_selection(view, at))
            return False
        if isinstance(watched, QLabel):
            if kind == QEvent.Type.MouseButtonPress:
                self._press[id(watched)] = event.position().toPoint()
                # A click on the text closes an open lookup.  The label remembers
                # that this press only dismissed it (an answer option then does
                # not answer).
                watched.setProperty("lookupDismissed", self.popup is not None or self._dismissed_widget == id(watched))
                self._dismissed_widget = None
                self.close()
            elif kind == QEvent.Type.MouseButtonDblClick:
                self._press.pop(id(watched), None)
                self._double[id(watched)] = True
                position = event.globalPosition().toPoint()
                # The label selects the word while handling the event; read it afterwards.
                self._queue_label_lookup(watched, position)
            elif kind == QEvent.Type.MouseButtonRelease:
                start = self._press.pop(id(watched), None)
                if self._double.pop(id(watched), False):
                    return False  # the end of a double-click, already looked up
                if watched.property("lookupDismissed") and start is not None \
                        and (event.position().toPoint() - start).manhattanLength() <= 6:
                    return False  # dismissing an old selection must not reopen its lookup
                # Whatever made the selection (a mouse drag, a trackpad
                # three-finger drag, shift-click), selected English is looked up.
                position = event.globalPosition().toPoint()
                self._queue_label_lookup(watched, position)
        return False

    def _queue_label_lookup(self, label: QLabel, position: QPoint) -> None:
        gesture = self._gesture
        QTimer.singleShot(0, lambda: self._lookup_selection(label, position) if gesture == self._gesture else None)

    def _lookup_browser_selection(self, browser: QTextBrowser, position: QPoint) -> None:
        if isValid(browser):
            text = lookup_selection(browser.textCursor().selectedText())
            if text:
                self.show(text, position)

    def _lookup_selection(self, label: QLabel, position: QPoint) -> None:
        try:
            if not label.hasSelectedText():
                return
            text = lookup_selection(label.selectedText())
        except RuntimeError:
            return  # the label was rebuilt meanwhile
        if text:
            self.show(text, position)

    def show(self, text: str, position: QPoint) -> None:
        """Show at once; run the dictionary off the UI thread and ignore dismissed results."""

        self.close()
        self._dismissed_widget = None
        popup = LookupPopup(self.host, text, self)
        self.popup = popup
        popup.closed.connect(lambda: self._popup_closed(popup))
        popup.place(self.host.mapFromGlobal(position))
        popup.show()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._dismiss_filter)

        def begin_lookup() -> None:
            if self.popup is not popup:
                return
            try:
                service = self.service
            except Exception as error:
                popup.show_result({"selected": text, "error": str(error)})
                return
            task = _Task(lambda: service.lookup_word(text))
            self._lookup_tasks.add(task)

            def fill(result: dict[str, Any]) -> None:
                self._lookup_tasks.discard(task)
                if self.popup is not popup or not isValid(popup):
                    return
                popup.show_result(result)

            task.signals.finished.connect(fill)
            QThreadPool.globalInstance().start(task)

        QTimer.singleShot(30, begin_lookup)

    def close(self) -> None:
        self._gesture += 1
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._dismiss_filter)
        popup, self.popup = self.popup, None
        if popup is not None:
            try:
                popup.close()
                popup.deleteLater()
            except RuntimeError:
                pass

    def _popup_closed(self, popup: LookupPopup) -> None:
        if self.popup is popup:
            self.popup = None
            self._gesture += 1
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self._dismiss_filter)
            popup.deleteLater()


def lookup_html(result: dict[str, Any]) -> str:
    """Popup text: word, the book's senses, local Chinese senses, word parts and origin, WordNet."""

    if result.get("error"):
        return f'<span style="color:{SOFT};">{_esc(result["error"])}</span>'
    selected = str(result.get("selected") or "")
    lemma = str(result.get("lemma") or selected)
    # Keep the reader's own capitalisation ("SARS") unless the word was reduced to a base form.
    head = _esc(selected if lemma == selected.casefold() else lemma)
    parts = [f'<span style="font-family:{SERIF}; font-size:22px;">{head}</span>']
    if result.get("pronunciation"):
        parts.append(f'&nbsp;&nbsp;<span style="color:{MUTED};">{_esc(result["pronunciation"])}</span>')
    if result.get("base_form"):
        parts.append(f'<br/><span style="color:{MUTED}; font-size:12px;">原形 {_esc(result["base_form"])}</span>')

    def section(title: str) -> str:
        return f'<br/><br/><span style="color:{MUTED}; font-size:12px;">{_esc(title)}</span>'

    def senses(items: list[dict[str, Any]]) -> str:
        return "".join(
            f'<br/><span style="color:{MUTED}; font-size:12px;">{_esc(item.get("pos"))}</span>&nbsp; {_esc(item.get("definition"))}'
            if item.get("pos") else f'<br/>{_esc(item.get("definition"))}'
            for item in items
        )

    book = result.get("book")
    if book:
        where = f' · {_esc(book["path"])}' if book.get("path") else ""
        title = f'《{_esc(book.get("book_title") or RAYDU_BOOK_TITLE)}》 ' if book.get("book_title") else ""
        parts.append(f'<br/><br/><span style="color:{ACCENT}; font-size:12px;">{title}{_esc(book["term"])}{where}</span>')
        parts.append(senses(book.get("senses", [])))
        if book.get("derived"):
            parts.append(senses(book["derived"]))
    if result.get("chinese"):
        parts.append(section(f"中文释义 · {result.get('chinese_source') or '本地词典'}"))
        parts.append(senses(result["chinese"]))
    morphology = result.get("morphology")
    if morphology:
        parts.append(section("构词"))
        parts.append(f'<br/><span style="font-family:{SERIF}; font-size:16px; color:{ACCENT};">{_esc(morphology["segments"])}</span>')
        for part in morphology["parts"]:
            if not part.get("meaning"):
                continue
            explained = any("\u4e00" <= char <= "\u9fff" for char in str(part.get("note") or ""))
            note = f'<span style="color:{MUTED};">（{_esc(part["note"])}）</span>' if explained and part["note"] != "独立英语词" else ""
            parts.append(f'<br/><b>{_esc(part["segment"])}</b>&nbsp; {_esc(part["meaning"])}{note}')
    if result.get("etymology_zh"):
        parts.append(section("字源"))
        parts.append(f'<br/><span style="color:{SOFT};">{_esc(result["etymology_zh"])}</span>')
    if result.get("word_family"):
        parts.append(f'<br/><span style="color:{MUTED}; font-size:12px;">{_esc(result["word_family"])}</span>')
    if result.get("english"):
        parts.append(section("WordNet"))
        parts.append(f'<span style="color:{SOFT};">{senses(result["english"][:2])}</span>')
    if not (book or result.get("chinese") or result.get("english") or morphology):
        parts.append(f'<br/><br/><span style="color:{SOFT};">本地词典没有收录这个词。</span>')
    return "".join(parts)


def open_study_window(owner: Any, **kwargs: Any) -> VocabularyStudyWindow:
    window = VocabularyStudyWindow(owner, **kwargs)
    windows = getattr(owner, "_vocabulary_study_windows", None)
    if not isinstance(windows, list):
        windows = []
        owner._vocabulary_study_windows = windows
    windows.append(window)
    window.show()
    window.raise_()
    window.activateWindow()
    return window


def open_review_unit(owner: Any, unit_key: str, title: str) -> None:
    """Study one unit on the 背单词 page of the control center (a window only without one)."""

    view = getattr(owner, "vocabulary_review_view", None)
    if view is not None and isValid(view) and getattr(owner, "current_page", "") == "背单词":
        view.start_unit(unit_key)
    elif callable(getattr(owner, "show_page", None)) and str(getattr(owner, "workspace", "")) == "english":
        owner.vocabulary_review_unit_key = unit_key
        owner._vocabulary_review_autostart = unit_key
        owner.show_page("背单词")
    else:
        open_study_window(owner, unit_key=unit_key, title=title, on_closed=lambda: _refresh_page(owner))
        return
    window = owner.window() if isinstance(owner, QWidget) else None
    if window is not None:
        window.showNormal() if window.isMinimized() else None
        window.raise_()
        window.activateWindow()


def open_card_location(owner: Any, card: dict[str, Any], index: int = 0) -> None:
    """Open the book page of a book card, or the reading position of a looked-up word."""

    locations = card.get("locations") or []
    if not 0 <= index < len(locations):
        return
    location = locations[index]
    page = int(location.get("page") or 1)
    service = review_service_for(owner)
    english = getattr(owner, "english_service", None)
    if card.get("source_kind") == "book":
        # A merged card lists both books; each location opens its own PDF.
        path = service.deck_pdf_path(location.get("deck") or card.get("deck", "raydu")) or service.book_pdf_path()
        anchor = 0.0
    else:
        path = resolve_stored_path(str(location.get("source_path") or ""))
        anchor = float(location.get("anchor_y") or 0.0)
    material = english.material_for_pdf(path) if english is not None else None
    if material is not None and hasattr(owner, "open_english_material"):
        owner.open_english_material(int(material["id"]), page_number=page, anchor_y=anchor)
        return
    if not Path(path).is_file():
        QMessageBox.information(None, "找不到原文", f"没有找到 PDF：{path}")
        return
    preview = owner.pdf_preview_for_path(Path(path))
    preview.show_pdf_location(Path(path), page_index=page - 1, anchor_y=anchor, title=Path(path).stem)


# ---------------------------------------------------------------------------
# Reader entry
# ---------------------------------------------------------------------------

def open_study_for_reader(owner: Any, pdf_path: Path | str | None, page_number: int) -> None:
    """From the reader: the vocabulary book opens its current section, other materials the 词汇库."""

    try:
        service = review_service_for(owner)
        deck = service.book_deck_for_pdf(pdf_path)
        if deck is not None:
            unit = service.unit_for_page(int(page_number), deck)
            if unit is None:
                QMessageBox.information(None, "背单词", "当前页不在字汇正文的任何小节内。")
                return
            open_review_unit(owner, unit["unit_key"], unit["path"])
            return
        if not service.library_card_rows():
            QMessageBox.information(None, "背单词", "词汇库里还没有收藏的词。阅读时选词后点“收藏到词汇库”即可。")
            return
        open_review_unit(owner, VOCABULARY_UNIT_KEY, VOCABULARY_UNIT_TITLE)
    except Exception as error:
        QMessageBox.critical(None, "背单词打开失败", str(error))


def add_reader_vocabulary_button(reader: Any, toolbar: QHBoxLayout) -> None:
    """Add the "背单词" button to the macOS Qt reader in the English workspace."""

    owner = getattr(reader, "owner", None)
    if str(getattr(owner, "workspace", "")) != "english":
        return
    button = QPushButton("背单词")
    button.setToolTip("字汇书（英文字汇、GRE字汇通）：背当前小节的单词；其他材料：背词汇库里收藏的词")
    button.setFixedHeight(32)
    _font(button, 11, QFont.Weight.DemiBold)
    button.clicked.connect(
        lambda: open_study_for_reader(owner, getattr(reader, "pdf_path", None), int(reader.current_page()) + 1)
    )
    toolbar.addWidget(button)
    reader.vocabulary_review_button = button


# ---------------------------------------------------------------------------
# "背单词" page
# ---------------------------------------------------------------------------

def start_library_preparation(owner: Any) -> None:
    """Prepare every 词汇库 card that is not ready (examples, AI 字源 check, audio) in the background."""

    if getattr(owner, "_vocabulary_preparation_running", False):
        return
    service = review_service_for(owner)
    owner._vocabulary_preparation_running = True
    previous = str(getattr(owner, "_vocabulary_preparation_text", "") or "")
    task = _Task(lambda: None)

    def run() -> Any:
        return service.prepare_library_cards(
            progress=lambda index, total, key: task.signals.progress.emit(
                f"正在准备词汇卡片 {index} / {total}：{key.split(':', 1)[1]}（音频）"
            )
        )

    task.function = run

    def show(text: str) -> None:
        owner._vocabulary_preparation_text = text
        label = getattr(owner, "_vocabulary_preparation_label", None)
        try:
            if label is not None:
                label.setText(text)
        except RuntimeError:
            owner._vocabulary_preparation_label = None  # the page was rebuilt

    def finished(result: Any) -> None:
        owner._vocabulary_preparation_running = False
        owner._vocabulary_preparation_task = None
        if isinstance(result, dict) and result.get("total"):
            failed = [item for item in result.get("prepared", []) if item.get("error")]
            text = f"词汇卡片已准备 {result['ready']} / {result['total']} 张"
            if failed:
                text += f"；{len(failed)} 张有问题（{failed[0]['error'][:60]}），下次启动时重试"
            show(text)
        else:
            show(previous if previous.startswith("词汇卡片已准备") else "")  # nothing was pending
        if isinstance(result, dict) and result.get("ready"):
            _refresh_page(owner)  # only when something became ready, so an offline machine cannot loop

    task.signals.progress.connect(show)
    task.signals.finished.connect(finished)
    owner._vocabulary_preparation_task = task
    QThreadPool.globalInstance().start(task)


def _example_library_row(owner: Any, service: VocabularyReviewService) -> QHBoxLayout:
    """One line: the Tatoeba example library for 词汇库 words, and its recording licence switch."""

    row = QHBoxLayout()
    row.setSpacing(10)
    label = QLabel()
    label.setObjectName("pageNote")
    label.setWordWrap(True)
    _font(label, 11)
    button = QPushButton("下载例句库（约 8 MB）")
    button.setObjectName("secondaryButton")
    button.setFixedHeight(32)
    _font(button, 11)

    def show_status() -> dict[str, Any]:
        try:
            status = service.example_library_status()
        except Exception as error:
            status = {"error": str(error)}
        downloaded = all(item.get("downloaded") for item in status.get("sources", {}).values()) if status.get("sources") else False
        status["downloaded"] = downloaded
        button.setVisible(not (status.get("error") or status.get("index_ready") or downloaded))
        button.setEnabled(True)
        button.setText("下载例句库（约 8 MB）")
        if status.get("error"):
            label.setText(f"例句库状态读取失败：{status['error']}")
        elif status.get("index_ready"):
            label.setText(f"例句库：Tatoeba 中英例句 {status.get('sentences', 0)} 条，词汇库单词自动配例句、翻译和音频。")
        elif downloaded:
            label.setText("正在建立例句索引…")
        else:
            label.setText("例句库未下载：词汇库里的书外单词暂时没有例句。")
        return status

    status = show_status()
    downloaded = status["downloaded"]

    def prepare() -> None:
        button.setEnabled(False)
        button.setText("正在准备…")
        task = _Task(service.prepare_example_library)

        def finished(result: Any) -> None:
            if isinstance(result, dict) and result.get("error"):
                QMessageBox.critical(None, "例句库准备失败", str(result["error"]))
            if isValid(label):
                show_status()  # in place: a round on the page goes on

        task.signals.finished.connect(finished)
        owner._vocabulary_example_task = task  # keep the signals alive until it finishes
        QThreadPool.globalInstance().start(task)

    button.clicked.connect(prepare)
    if downloaded and not status.get("index_ready") and not status.get("error"):
        prepare()  # the files are here; index them in the background (about a second)
    switch = QCheckBox("例句也使用 CC BY-NC-ND 真人录音（仅限个人学习）")
    _font(switch, 11)
    switch.setChecked(bool(status.get("example_audio_noncommercial")))
    switch.setToolTip("Tatoeba 大部分真人录音是 CC BY-NC-ND 授权：允许非商业使用、原样播放并署名。关闭时只用 CC BY / CC BY-SA / CC0 录音，其余用 Edge TTS。")

    def toggle(checked: bool) -> None:
        try:
            service.set_example_audio_noncommercial(checked)
        except Exception as error:
            QMessageBox.critical(None, "设置未保存", str(error))

    switch.toggled.connect(toggle)
    row.addWidget(label, 1)
    row.addWidget(switch)
    row.addWidget(button)
    return row


def _refresh_page(owner: Any) -> None:
    """Update the 背单词 page in place, so a round in progress is not interrupted."""

    view = getattr(owner, "vocabulary_review_view", None)
    if view is not None and isValid(view):
        view.refresh()
        return
    if getattr(owner, "current_page", "") == "背单词" and hasattr(owner, "show_page"):
        try:
            owner.show_page("背单词")
        except Exception:
            pass


REVIEW_PAGE_CSS = f"""
QWidget#vocabReviewView {{ background:transparent; }}
QFrame#vocabNav, QFrame#vocabStage {{
    background:rgba(255, 255, 255, 150); border:1px solid rgba(255, 255, 255, 196); border-radius:18px;
}}
QFrame#vocabToday {{ background:{PAPER}; border:1px solid {HAIRLINE}; border-radius:14px; }}
QLabel {{ background:transparent; }}
QLabel#vocabFigure {{ color:{INK}; }}
QLabel#vocabFigureLabel, QLabel#vocabNavNote {{ color:{MUTED}; }}
QLabel#vocabStreak {{ color:#B9822C; }}
QTreeWidget#vocabOutline {{
    background:transparent; border:0; outline:0; color:#263746; show-decoration-selected:0;
}}
QTreeWidget#vocabOutline::item {{ padding:7px 2px; border:0; }}
QTreeWidget#vocabOutline::item:hover {{ background:rgba(47, 93, 124, 18); }}
QTreeWidget#vocabOutline::item:selected {{ background:rgba(47, 93, 124, 36); color:{ACCENT}; }}
QTreeWidget#vocabOutline::branch,
QTreeWidget#vocabOutline::branch:selected,
QTreeWidget#vocabOutline::branch:hover {{ background:transparent; border:0; image:none; }}
QProgressBar#vocabTodayProgress {{ background:rgba(47, 93, 124, 28); border:0; border-radius:3px; }}
QProgressBar#vocabTodayProgress::chunk {{ background:#2E7D5B; border-radius:3px; }}
QPushButton#vocabStart {{ background:{ACCENT}; border:1px solid {ACCENT}; border-radius:10px; color:white; padding:7px 12px; }}
QPushButton#vocabStart:hover {{ background:#264D67; }}
QPushButton#vocabStart:disabled {{ background:#B7C4CE; border-color:#B7C4CE; }}
"""


class VocabularyReviewView(QWidget):
    """The 背单词 page: today and the directory on the left, the card on the right."""

    def __init__(self, owner: Any) -> None:
        super().__init__()
        self.owner = owner
        self.service = review_service_for(owner)
        self.units: list[dict[str, Any]] = []
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self.refresh)
        self.setObjectName("vocabReviewView")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(REVIEW_PAGE_CSS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(14)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }"
                               "QSplitter::handle:hover { background: rgba(47,93,124,30); border-radius: 4px; }")

        # Left: today, then the directory with each section's progress.
        nav = QFrame()
        nav.setObjectName("vocabNav")
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(14, 14, 10, 12)
        nav_layout.setSpacing(8)
        today = QFrame()
        today.setObjectName("vocabToday")
        today.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        today_layout = QVBoxLayout(today)
        today_layout.setContentsMargins(14, 12, 14, 12)
        today_layout.setSpacing(8)
        top = QHBoxLayout()
        heading = QLabel("今天")
        _font(heading, 14, QFont.Weight.DemiBold)
        self.streak_label = QLabel("")
        self.streak_label.setObjectName("vocabStreak")
        _font(self.streak_label, 11, QFont.Weight.DemiBold)
        top.addWidget(heading)
        top.addStretch(1)
        top.addWidget(self.streak_label)
        today_layout.addLayout(top)
        figures = QHBoxLayout()
        figures.setSpacing(4)
        self.figure_labels: dict[str, QLabel] = {}
        for key, label in (("due", "待复习"), ("new", "新词"), ("reviewed_today", "已复习")):
            column = QVBoxLayout()
            column.setSpacing(0)
            value = QLabel("0")
            value.setObjectName("vocabFigure")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _font(value, 22, QFont.Weight.DemiBold)
            caption = QLabel(label)
            caption.setObjectName("vocabFigureLabel")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _font(caption, 10.5)
            column.addWidget(value)
            column.addWidget(caption)
            figures.addLayout(column, 1)
            self.figure_labels[key] = value
        today_layout.addLayout(figures)
        self.today_progress = QProgressBar()
        self.today_progress.setObjectName("vocabTodayProgress")
        self.today_progress.setTextVisible(False)
        self.today_progress.setFixedHeight(6)
        today_layout.addWidget(self.today_progress)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.start_button = QPushButton("开始今日复习")
        self.start_button.setObjectName("vocabStart")
        _font(self.start_button, 12, QFont.Weight.DemiBold)
        self.start_button.clicked.connect(self.start_today)
        per_day_label = QLabel("每天新词")
        per_day_label.setObjectName("vocabNavNote")
        _font(per_day_label, 10.5)
        self.per_day = QSpinBox()
        self.per_day.setRange(0, 200)
        self.per_day.setFixedWidth(62)
        _font(self.per_day, 11)
        self.per_day.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Space and 1–4 stay with the card
        self.per_day.editingFinished.connect(self._save_per_day)
        row.addWidget(self.start_button, 1)
        row.addWidget(per_day_label)
        row.addWidget(self.per_day)
        today_layout.addLayout(row)
        nav_layout.addWidget(today)
        directory = QLabel("目录")
        _font(directory, 14, QFont.Weight.DemiBold)
        nav_layout.addSpacing(4)
        nav_layout.addWidget(directory)
        self.tree = QTreeWidget()
        self.tree.setObjectName("vocabOutline")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(3)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(True)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree_header = self.tree.header()
        tree_header.setStretchLastSection(False)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        tree_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        tree_header.resizeSection(1, 34)
        tree_header.resizeSection(2, 76)
        _font(self.tree, 12)
        self.tree.itemClicked.connect(lambda item, _column: self._open_item(item))
        nav_layout.addWidget(self.tree, 1)
        self.nav_note = QLabel("点一节就在右边背；数字是到期张数，右边是已学 / 总数。")
        self.nav_note.setObjectName("vocabNavNote")
        self.nav_note.setWordWrap(True)
        _font(self.nav_note, 10)
        nav_layout.addWidget(self.nav_note)
        nav.setMinimumWidth(300)
        nav.setMaximumWidth(400)
        splitter.addWidget(nav)

        # Right: the card.
        stage = QFrame()
        stage.setObjectName("vocabStage")
        stage.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(22, 16, 22, 14)
        self.panel = VocabularyStudyPanel(owner, autoload=False)
        self.panel.reviewed.connect(self._refresh_timer.start)
        self.panel.next_unit_requested.connect(self.open_next_unit)
        stage_layout.addWidget(self.panel)
        splitter.addWidget(stage)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 1000])
        layout.addWidget(splitter, 1)
        self.setMinimumHeight(700)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.refresh()
        self.panel.show_welcome(self.summary)

    # -- data -------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read today's figures and the directory's progress; the card is left alone."""

        try:
            self.summary = self.service.today_summary()
            self.units = self.service.list_units()
        except Exception as error:
            self.summary = {}
            self.units = []
            self.nav_note.setText(f"背单词数据读取失败：{error}")
        summary = self.summary
        for key, label in self.figure_labels.items():
            label.setText(str(summary.get(key, 0)))
        streak = int(summary.get("streak") or 0)
        self.streak_label.setText(f"🔥 连续 {streak} 天" if streak else "")
        reviewed = int(summary.get("reviewed_today") or 0)
        left = int(summary.get("due") or 0) + int(summary.get("new") or 0)
        self.today_progress.setRange(0, max(1, reviewed + left))
        self.today_progress.setValue(reviewed if left or reviewed else 1)
        self.start_button.setEnabled(bool(left))
        self.start_button.setText("开始今日复习" if left else "今天完成了 ✓")
        self.per_day.blockSignals(True)
        self.per_day.setValue(int(summary.get("new_limit") or 0))
        self.per_day.blockSignals(False)
        self._fill_tree()
        self.panel.next_unit_title = (self._next_unit() or {}).get("title", "")

    def _fill_tree(self) -> None:
        current = getattr(self.owner, "vocabulary_review_unit_key", None)
        expanded = {
            self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).isExpanded()
        }
        first_fill = self.tree.topLevelItemCount() == 0
        self.tree.clear()
        parts: dict[str, QTreeWidgetItem] = {}
        current_item: QTreeWidgetItem | None = None
        for unit in self.units:
            if unit["unit_key"] == VOCABULARY_UNIT_KEY:
                parent, label = None, unit["title"]
            else:
                part, _, rest = str(unit["path"]).partition(" › ")
                if not rest:
                    part, rest = "其他", unit["title"]
                label = rest
                parent = parts.get(part)
                if parent is None:
                    parent = QTreeWidgetItem([part, "", ""])
                    font = parent.font(0)
                    font.setWeight(QFont.Weight.DemiBold)
                    parent.setFont(0, font)
                    parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    parts[part] = parent
                    self.tree.addTopLevelItem(parent)
            item = QTreeWidgetItem([label, str(unit["due"] or ""), self._progress_text(unit["learned"], unit["total"])])
            item.setData(0, Qt.ItemDataRole.UserRole, unit["unit_key"])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, unit["path"])
            page = f"第 {unit['page_start']} 页 · " if unit.get("page_start") else ""
            bands = "  ".join(unit.get("bands", []))
            item.setToolTip(0, f"{unit['path']}\n{page}{bands}".strip())
            item.setForeground(1, QColor(ACCENT))
            item.setForeground(2, QColor("#2E7D5B" if unit["total"] and unit["learned"] >= unit["total"] else MUTED))
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if parent is None:
                self.tree.insertTopLevelItem(0, item)
            else:
                parent.addChild(item)
            if unit["unit_key"] == current:
                current_item = item
        for name, parent in parts.items():
            learned = sum(unit["learned"] for unit in self.units if str(unit["path"]).startswith(f"{name} › "))
            total = sum(unit["total"] for unit in self.units if str(unit["path"]).startswith(f"{name} › "))
            parent.setText(2, self._progress_text(learned, total))
            parent.setForeground(2, QColor(MUTED))
            parent.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            parent.setToolTip(0, name)
            parent.setExpanded(name in expanded)
        if current_item is not None:
            if current_item.parent() is not None:
                current_item.parent().setExpanded(True)
            self.tree.setCurrentItem(current_item)
            if first_fill:
                QTimer.singleShot(0, lambda item=current_item: self.tree.scrollToItem(item) if isValid(self.tree) else None)
        elif first_fill and self.tree.topLevelItemCount() > 1:
            self.tree.topLevelItem(1).setExpanded(True)

    @staticmethod
    def _progress_text(learned: int, total: int) -> str:
        return "✓" if total and learned >= total else f"{learned}/{total}"

    def _save_per_day(self) -> None:
        try:
            self.service.manager.set_review_new_per_day(self.per_day.value())
        except Exception as error:
            QMessageBox.critical(None, "设置未保存", str(error))
            return
        self.refresh()

    # -- studying ---------------------------------------------------------

    def start_today(self) -> None:
        self.owner.vocabulary_review_unit_key = None
        self.tree.clearSelection()
        self.panel.start(title="今日复习")

    def start_unit(self, unit_key: str) -> None:
        unit = next((unit for unit in self.units if unit["unit_key"] == unit_key), None)
        if unit is None:
            return
        self.owner.vocabulary_review_unit_key = unit_key
        part, _, rest = str(unit["path"]).partition(" › ")
        self.panel.next_unit_title = (self._next_unit() or {}).get("title", "")
        self.panel.start(unit_key=unit_key, title=rest or unit["title"], crumb=part if rest else "")
        self._fill_tree()

    def _open_item(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key:
            item.setExpanded(not item.isExpanded())
            return
        self.start_unit(str(key))

    def _next_unit(self) -> dict[str, Any] | None:
        """The first section after the current one that still has words to learn."""

        current = getattr(self.owner, "vocabulary_review_unit_key", None)
        keys = [unit["unit_key"] for unit in self.units]
        if current not in keys:
            return None
        for unit in self.units[keys.index(current) + 1:]:
            if unit["unit_key"] != VOCABULARY_UNIT_KEY and unit["learned"] < unit["total"]:
                return unit
        return None

    def open_next_unit(self) -> None:
        unit = self._next_unit()
        if unit is not None:
            self.start_unit(unit["unit_key"])


def build_vocabulary_review_page(owner: Any) -> QVBoxLayout:
    """The 背单词 page: heading, the directory and card side by side, the example library."""

    layout = QVBoxLayout()
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(12)
    title = QLabel("背单词")
    title.setObjectName("pageTitle")
    _font(title, 20, QFont.Weight.DemiBold)
    layout.addWidget(title)
    try:
        unified = bool(review_service_for(owner).card_index().get("unified"))
    except Exception:
        unified = False  # reported below, where the units are read
    note_text = ("按字汇书的章节逐节背词（同一字首字根的词在同一节，每个词只出现一次）；收藏到词汇库的词在“词汇库”单元里背。"
                 if unified else f"按《{RAYDU_BOOK_TITLE}》的章节逐节背词；收藏到词汇库的词在“词汇库”单元里背。")
    if APP_PATHS.public_release:
        note_text = "背词汇库里收藏的单词；到期复习与新词按记忆曲线安排。"
    elif sys.platform == "darwin":
        note_text += " Mac 上的复习记录不会同步到 Windows，日常背词请在 Windows 上进行。"
    note = QLabel(note_text)
    note.setObjectName("pageNote")
    note.setWordWrap(True)
    _font(note, 11)
    layout.addWidget(note)

    service = review_service_for(owner)
    try:
        service.today_summary()
    except Exception as error:
        failure = QLabel(f"背单词数据读取失败：{error}")
        failure.setWordWrap(True)
        layout.addWidget(failure)
        layout.addStretch(1)
        return layout

    view = VocabularyReviewView(owner)
    owner.vocabulary_review_view = view
    layout.addWidget(view, 1)
    autostart = getattr(owner, "_vocabulary_review_autostart", None)
    if autostart:
        owner._vocabulary_review_autostart = None
        QTimer.singleShot(0, lambda: view.start_unit(str(autostart)) if isValid(view) else None)

    footer = _example_library_row(owner, service)
    rebuild = QPushButton("重建卡片索引")
    rebuild.setObjectName("secondaryButton")
    rebuild.setFixedHeight(32)
    _font(rebuild, 11)

    def rebuild_index() -> None:
        try:
            result = service.rebuild_card_index()
        except Exception as error:
            QMessageBox.critical(None, "重建失败", str(error))
            return
        missing_audio = int(result["cards_without_audio"]) + int(result.get("gre_cards_without_audio") or 0)
        QMessageBox.information(
            None, "卡片索引已重建",
            f"原书词条 {result['entries']} 条，合并为 {result['cards']} 张卡片、{result['units']} 个单元。\n"
            f"缺页码 {result['cards_without_page']} 张，缺原书音频 {missing_audio} 张（用语音合成朗读）。",
        )
        _refresh_page(owner)

    rebuild.clicked.connect(rebuild_index)
    footer.addWidget(rebuild)
    layout.addLayout(footer)
    preparation = QLabel(getattr(owner, "_vocabulary_preparation_text", "") or "")
    preparation.setObjectName("pageNote")
    _font(preparation, 11)
    owner._vocabulary_preparation_label = preparation
    layout.addWidget(preparation)
    if service.examples.sources_present():
        start_library_preparation(owner)
    return layout


# ---------------------------------------------------------------------------
# 词汇库 page (English workspace): one line per word, the card unfolds below it
# ---------------------------------------------------------------------------

LIBRARY_CSS = f"""
QFrame#vocabRow {{ background: {PAPER}; border: none; border-bottom: 1px solid {HAIRLINE}; }}
QFrame#vocabRow[hovered="true"] {{ background: #F7F9FA; }}
QFrame#vocabRow[expanded="true"] {{ background: #F7F9FA; }}
QLabel#vocabTerm {{ color: {INK}; }}
QLabel#vocabIpa {{ color: {MUTED}; }}
QLabel#vocabChinese {{ color: {INK}; }}
QLabel#vocabEnglish {{ color: {SOFT}; }}
QLabel#vocabTag {{ color: {MUTED}; border: 1px solid {HAIRLINE}; border-radius: 9px; padding: 1px 8px; }}
QLabel#vocabTag[familiar="true"] {{ color: #2E7D5B; border-color: #BFDCCD; }}
QToolButton#vocabPlay {{ border: 1px solid {HAIRLINE}; border-radius: 14px; background: {PAPER}; color: {ACCENT}; }}
QToolButton#vocabPlay:hover {{ border-color: {ACCENT}; }}
QLabel#vocabChevron {{ color: {MUTED}; }}
QFrame#vocabDetail {{ background: #F7F9FA; border: none; border-bottom: 1px solid {HAIRLINE}; }}
"""


class _ElidedLabel(QLabel):
    """One-line label that elides with "…" instead of widening the row."""

    def __init__(self, text: str, object_name: str) -> None:
        super().__init__()
        self.full_text = text
        self.setObjectName(object_name)
        self.setToolTip(text)
        from PySide6.QtWidgets import QSizePolicy

        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        metrics = self.fontMetrics()
        self.setText(metrics.elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(10, self.width())))


class VocabularyRow(QFrame):
    """A collapsed word line; clicking it unfolds the word's full card with an animation."""

    toggled = Signal(object)

    def __init__(self, item: dict[str, Any], page: "VocabularyLibraryPage") -> None:
        super().__init__()
        from PySide6.QtWidgets import QToolButton

        self.item = item
        self.page = page
        self.setObjectName("vocabRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 18, 10)
        row.setSpacing(12)
        self.check = QCheckBox()
        self.check.setToolTip("勾选后可批量设为熟悉、不熟悉或移出词汇库")
        row.addWidget(self.check)
        play = QToolButton()
        play.setObjectName("vocabPlay")
        play.setText("▶")
        play.setFixedSize(28, 28)
        play.setToolTip("播放单词发音")
        play.clicked.connect(lambda: page.play_word(self.item))
        row.addWidget(play)
        term = QLabel(item["term"])
        term.setObjectName("vocabTerm")
        font = term.font()
        font.setFamilies(["Georgia", "Times New Roman"])
        font.setPointSizeF(15)
        term.setFont(font)
        term.setMinimumWidth(150)
        row.addWidget(term)
        ipa = QLabel(item.get("pronunciation") or "")
        ipa.setObjectName("vocabIpa")
        _font(ipa, 10)
        ipa.setMinimumWidth(120)
        row.addWidget(ipa)
        chinese = _ElidedLabel(item.get("chinese") or "", "vocabChinese")
        _font(chinese, 11)
        row.addWidget(chinese, 3)
        english = _ElidedLabel(item.get("english") or "", "vocabEnglish")
        _font(english, 10)
        row.addWidget(english, 2)
        familiar = item.get("familiarity") == "familiar"
        tags = ([item["state"]] if item.get("state") else []) + (["熟悉"] if familiar else [])
        for text in tags:
            tag = QLabel(text)
            tag.setObjectName("vocabTag")
            tag.setProperty("familiar", "true" if text == "熟悉" else "false")
            _font(tag, 9)
            row.addWidget(tag)
        self.chevron = QLabel("›")
        self.chevron.setObjectName("vocabChevron")
        _font(self.chevron, 14)
        row.addWidget(self.chevron)
        self.detail: QFrame | None = None
        self.view: CardView | None = None
        self.animation: Any = None
        self.expanded = False

    def event(self, event) -> bool:  # type: ignore[override]
        from PySide6.QtCore import QEvent

        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave):
            self.setProperty("hovered", "true" if event.type() == QEvent.Type.HoverEnter else "false")
            self.style().unpolish(self)
            self.style().polish(self)
        return super().event(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggled.emit(self)
        super().mouseReleaseEvent(event)

    def build_detail(self) -> QFrame:
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        detail = QFrame()
        detail.setObjectName("vocabDetail")
        detail.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(56, 6, 24, 16)
        self.view = CardView()
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.anchorClicked.connect(lambda url: self.page.card_link(self, url))
        layout.addWidget(self.view)
        detail.setMaximumHeight(0)
        effect = QGraphicsOpacityEffect(detail)
        effect.setOpacity(0.0)
        detail.setGraphicsEffect(effect)
        self.detail = detail
        return detail

    def render_card(self, card: dict[str, Any]) -> int:
        """Show the full card and return the height it needs."""

        assert self.view is not None
        self.card = card
        self.view.setHtml(card_html(card, {"mode": "recognize"}, reveal=True))
        width = max(400, self.page.list_widget.width() - 100)
        self.view.document().setTextWidth(width - 4)
        height = int(self.view.document().size().height()) + 8
        self.view.setFixedHeight(height)
        return height + 22

    def animate(self, open_: bool, height: int) -> None:
        from PySide6.QtCore import QEasingCurve, QParallelAnimationGroup, QPropertyAnimation

        assert self.detail is not None
        self.expanded = open_
        self.setProperty("expanded", "true" if open_ else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.chevron.setText("⌄" if open_ else "›")
        group = QParallelAnimationGroup(self)
        size = QPropertyAnimation(self.detail, b"maximumHeight", group)
        size.setDuration(280)
        size.setEasingCurve(QEasingCurve.Type.OutCubic if open_ else QEasingCurve.Type.InOutCubic)
        size.setStartValue(self.detail.maximumHeight())
        size.setEndValue(height if open_ else 0)
        fade = QPropertyAnimation(self.detail.graphicsEffect(), b"opacity", group)
        fade.setDuration(240 if open_ else 160)
        fade.setStartValue(self.detail.graphicsEffect().opacity())
        fade.setEndValue(1.0 if open_ else 0.0)
        group.addAnimation(size)
        group.addAnimation(fade)
        if self.animation is not None:
            self.animation.stop()
        self.animation = group
        group.start()


class VocabularyLibraryPage(QWidget):
    """The English 词汇库: every collected word in one line; click to unfold its card."""

    PAGE_SIZE = 100

    def __init__(self, owner: Any) -> None:
        super().__init__()
        self.owner = owner
        self.service = review_service_for(owner)
        self.audio = AudioPlayer(getattr(getattr(owner, "english_service", None), "speech_service", None), self)
        self.rows: list[VocabularyRow] = []
        self.items: list[dict[str, Any]] = []
        self._query_task: _Task | None = None
        self._query_generation = 0
        self._pending_query = ("", "all")
        self._last_query: tuple[str, str] | None = None
        self._page_index = 0
        self._checked_keys: set[str] = set()
        self._expanded_keys: set[str] = set()
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.reload)
        self._batch_timer = QTimer(self)
        self._batch_timer.timeout.connect(self._append_batch)
        self.setStyleSheet(LIBRARY_CSS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        toolbar = QFrame()
        toolbar.setObjectName("glassPanel")
        toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(14, 10, 14, 10)
        bar.setSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("softInput")
        self.search.setPlaceholderText("搜索单词、中文或英文释义")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(240)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        bar.addWidget(self.search)
        self.filter = QComboBox()
        self.filter.setObjectName("softCombo")
        for label, value in (("全部词汇", "all"), ("不熟悉", "unfamiliar"), ("熟悉", "familiar")):
            self.filter.addItem(label, value)
        self.filter.currentIndexChanged.connect(lambda _index: self.reload())
        bar.addWidget(self.filter)
        bar.addStretch(1)
        actions = [
            ("设为熟悉", lambda: self.set_familiarity("familiar"), "secondaryButton"),
            ("设为不熟悉", lambda: self.set_familiarity("unfamiliar"), "secondaryButton"),
            ("移出词汇库", self.remove_checked, "dangerOutlineButton"),
            ("导出 TXT", lambda: self._export("txt"), "secondaryButton"),
            ("导出 PDF", lambda: self._export("pdf"), "secondaryButton"),
            ("刷新", self.reload, "secondaryButton"),
        ]
        for text, callback, kind in actions:
            button = QPushButton(text)
            button.setObjectName(kind)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(34)
            button.clicked.connect(callback)
            bar.addWidget(button)
        layout.addWidget(toolbar)

        self.count_label = QLabel("")
        self.audio.status_changed.connect(self.count_label.setText)
        self.count_label.setObjectName("pageNote")
        _font(self.count_label, 10)
        layout.addWidget(self.count_label)

        pages = QHBoxLayout()
        self.previous_page = QPushButton("上一页")
        self.next_page = QPushButton("下一页")
        self.page_label = QLabel("")
        for button in (self.previous_page, self.next_page):
            button.setObjectName("secondaryButton")
            button.setEnabled(False)
        self.previous_page.clicked.connect(lambda: self._show_page(self._page_index - 1))
        self.next_page.clicked.connect(lambda: self._show_page(self._page_index + 1))
        pages.addStretch(1)
        pages.addWidget(self.previous_page)
        pages.addWidget(self.page_label)
        pages.addWidget(self.next_page)
        layout.addLayout(pages)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet(f"QScrollArea {{ background: {PAPER}; border: 1px solid {HAIRLINE}; border-radius: 12px; }}")
        self.list_widget = QWidget()
        self.list_widget.setStyleSheet(f"background: {PAPER};")
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_widget)
        self.scroll.setMinimumHeight(520)
        layout.addWidget(self.scroll, 1)
        # A refresh restores the scroll position once the rebuilt rows give the
        # bar enough range; on Windows that range arrives after the restore runs.
        self._scroll_target: int | None = None
        self._scroll_restores = 0
        self._reader_scrolled = False
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._scroll_range_changed)
        bar.actionTriggered.connect(self._reader_scroll)
        self.reload()

    # -- data ------------------------------------------------------------------

    def reload(self) -> None:
        self._search_timer.stop()
        self._batch_timer.stop()
        self._query_generation += 1
        self._pending_query = (self.search.text(), str(self.filter.currentData() or "all"))
        self.previous_page.setEnabled(False)
        self.next_page.setEnabled(False)
        self.count_label.setText("正在读取词汇库…")
        if self._query_task is None:
            self._start_query()

    def _start_query(self) -> None:
        query, familiarity = self._pending_query
        service = self.service
        task = _Task(lambda: service.library_overview(query, familiarity))
        task.generation = self._query_generation
        self._query_task = task
        task.signals.finished.connect(self._query_finished)
        QThreadPool.globalInstance().start(task)

    def _query_finished(self, result: Any) -> None:
        task, self._query_task = self._query_task, None
        if not isValid(self):
            return
        if task.generation != self._query_generation:
            self._start_query()
            return
        if isinstance(result, dict) and result.get("error"):
            self.count_label.setText(f"词汇库读取失败：{result['error']}")
            self._update_page_buttons()
            return
        self.items = result
        scroll_position = self.scroll.verticalScrollBar().value() if self._last_query == self._pending_query else 0
        if self._last_query != self._pending_query:
            self._page_index = 0
        self._last_query = self._pending_query
        self._show_page(self._page_index, scroll_position=scroll_position)

    def _remember_rows(self) -> None:
        for row in self.rows:
            key = row.item["card_key"]
            (self._checked_keys.add if row.check.isChecked() else self._checked_keys.discard)(key)
            (self._expanded_keys.add if row.expanded else self._expanded_keys.discard)(key)

    def _show_page(self, index: int, *, scroll_position: int = 0) -> None:
        self._batch_timer.stop()
        self._remember_rows()
        keys = {item["card_key"] for item in self.items}
        self._checked_keys.intersection_update(keys)
        self._expanded_keys.intersection_update(keys)
        self._page_index = max(0, min(index, max(0, (len(self.items) - 1) // self.PAGE_SIZE)))
        while self.list_layout.count() > 1:
            widget = self.list_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.rows = []
        self._scroll_target = None
        self._reader_scrolled = False
        self._render_offset = self._page_index * self.PAGE_SIZE
        self._render_stop = min(len(self.items), self._render_offset + self.PAGE_SIZE)
        self._restore_scroll_position = scroll_position
        self._append_batch()
        self._update_page_buttons()
        self._update_count()

    def _restore_scroll(self) -> None:
        if not isValid(self):
            return
        if self._reader_scrolled:
            return  # the reader already moved this page; a queued restore must not undo that
        bar = self.scroll.verticalScrollBar()
        bar.setValue(self._restore_scroll_position)
        if bar.value() < self._restore_scroll_position:
            # The rows are in place but their height has not reached the bar yet.
            self._scroll_target = self._restore_scroll_position
            self._scroll_restores += 1
            restore = self._scroll_restores
            QTimer.singleShot(2000, lambda: self._scroll_restores == restore and self._drop_scroll_target())

    def _scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._scroll_target is not None and maximum >= self._scroll_target:
            self.scroll.verticalScrollBar().setValue(self._scroll_target)
            self._scroll_target = None

    def _reader_scroll(self, _action: int) -> None:
        self._reader_scrolled = True
        self._drop_scroll_target()

    def _drop_scroll_target(self) -> None:
        # The reader scrolled (or the layout never grew): stop restoring.
        if isValid(self):
            self._scroll_target = None

    def _update_page_buttons(self) -> None:
        count = max(1, (len(self.items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_label.setText(f"第 {self._page_index + 1} / {count} 页")
        self.previous_page.setEnabled(self._query_task is None and self._page_index > 0)
        self.next_page.setEnabled(self._query_task is None and self._page_index + 1 < count)

    def _set_checked(self, key: str, checked: bool) -> None:
        (self._checked_keys.add if checked else self._checked_keys.discard)(key)
        self._update_count()

    def _update_count(self) -> None:
        books = sum(1 for item in self.items if item["kind"] == "book")
        self.count_label.setText(
            f"{len(self.items)} 个词" + (f"（其中 {books} 个用原书卡片）" if books else "")
            + (f"　·　已勾选 {len(self._checked_keys)} 个词" if self._checked_keys else "")
            + "　·　点击一行展开词汇卡片；收藏的词都会进入背单词的“词汇库”单元"
            if self.items else "词汇库还没有词。阅读时选词后点“收藏到词汇库”，或在背单词卡片上双击单词收藏。"
        )

    def _append_batch(self) -> None:
        stop = min(self._render_stop, self._render_offset + 50)
        for item in self.items[self._render_offset:stop]:
            row = VocabularyRow(item, self)
            row.toggled.connect(self.toggle)
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
            self.rows.append(row)
            row.check.setChecked(item["card_key"] in self._checked_keys)
            row.check.toggled.connect(lambda checked, key=item["card_key"]: self._set_checked(key, checked))
            if item["card_key"] in self._expanded_keys:
                self.toggle(row)
            row.show()
        self._render_offset = stop
        if stop < self._render_stop:
            self._batch_timer.start(0)
        else:
            self._batch_timer.stop()
            QTimer.singleShot(0, self._restore_scroll)

    def toggle(self, row: VocabularyRow) -> None:
        if row.expanded:
            row.animate(False, 0)
            self._expanded_keys.discard(row.item["card_key"])
            return
        try:
            card = self.service.card(row.item["card_key"])
        except Exception as error:
            QMessageBox.warning(self, "卡片读取失败", str(error))
            return
        if row.detail is None:
            self.list_layout.insertWidget(self.list_layout.indexOf(row) + 1, row.build_detail())
        height = row.render_card(card)
        row.animate(True, height)
        self._expanded_keys.add(row.item["card_key"])
        if not self.service.card_is_ready(card):
            start_library_preparation(self.owner)

    # -- actions ------------------------------------------------------------------

    def play_word(self, item: dict[str, Any]) -> None:
        self.audio.play(item.get("audio_path") or self.service.examples.cached_audio(item["term"]), item["term"], mode="word")

    def card_link(self, row: VocabularyRow, url: QUrl) -> None:
        card = getattr(row, "card", None)
        if card is None:
            return
        target = url.toString()
        if target == "play:word":
            self.audio.play(card.get("audio_path", ""), card["term"], mode="word")
        elif target.startswith("play:example:"):
            example = card["examples"][int(target.rsplit(":", 1)[1])]
            self.audio.play(example.get("audio_path", ""), example.get("en", ""), mode="sentence")
        elif target.startswith("open:"):
            open_card_location(self.owner, card, int(target.split(":", 1)[1]))

    def checked_items(self) -> list[dict[str, Any]]:
        return [item for item in self.items if item["card_key"] in self._checked_keys]

    def set_familiarity(self, familiarity: str) -> None:
        items = self.checked_items()
        ids = sorted({entry_id for item in items for entry_id in item["entry_ids"]})
        if not ids:
            QMessageBox.information(self, "请勾选单词", "请先勾选要设置的单词（原书卡片需在词汇库里有对应词条）。")
            return
        try:
            self.service.manager.set_familiarity(familiarity, entry_ids=ids)
        except Exception as error:
            QMessageBox.critical(self, "设置失败", str(error))
            return
        self.reload()

    def remove_checked(self) -> None:
        items = self.checked_items()
        if not items:
            QMessageBox.information(self, "请勾选单词", "请先勾选要移出词汇库的单词。")
            return
        names = "、".join(item["term"] for item in items[:12]) + ("……" if len(items) > 12 else "")
        answer = QMessageBox.question(
            self, "移出词汇库",
            f"将 {len(items)} 个词移出词汇库：{names}\n\n书外单词会删除词条（先自动备份）；原书单词只取消收藏，原书卡片和背词进度保留。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.remove_from_library([item["card_key"] for item in items])
        except Exception as error:
            QMessageBox.critical(self, "移出失败", str(error))
            return
        self.reload()

    def _export(self, kind: str) -> None:
        try:
            result = self.service.export_library(kind, str(self.filter.currentData() or "all"))
            self.owner.open_path_with_feedback(Path(result["path"]))
        except Exception as error:
            QMessageBox.critical(self, "导出词汇库失败", str(error))


def build_vocabulary_library_page(owner: Any) -> QVBoxLayout:
    """The English workspace 词汇库 page (replaces the plain table)."""

    for stale in ("vocabulary_table", "vocabulary_search", "vocabulary_filter_combo"):
        if hasattr(owner, stale):
            delattr(owner, stale)  # the old table's refresh must not touch deleted widgets
    owner.english_vocabulary_encounter_table = None
    layout = QVBoxLayout()
    layout.setSpacing(12)
    title = QLabel("词汇库")
    title.setObjectName("pageTitle")
    _font(title, 22, QFont.Weight.DemiBold)
    layout.addWidget(title)
    page = VocabularyLibraryPage(owner)
    owner.vocabulary_library_page = page
    layout.addWidget(page, 1)
    return layout


def refresh_vocabulary_library_page(owner: Any) -> bool:
    """Reload the mounted English 词汇库 page; False when it is not on screen."""

    page = getattr(owner, "vocabulary_library_page", None)
    if page is None:
        return False
    try:
        page.reload()
    except RuntimeError:
        owner.vocabulary_library_page = None  # deleted with an earlier page
        return False
    return True
