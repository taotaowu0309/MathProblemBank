"""The 总览 page of the English workspace.

What the learner does today and where each part of the route stands: today's
vocabulary review, the reading progress of the 文法 / 阅读方法 / 写作方法
pages, the question bank (answered, unresolved mistakes), the reading shelf
and the words marked for later lookup.  Every card opens its page.  The
figures come from :meth:`EnglishLearningService.learning_overview` (the same
data as the AI tool ``get_english_learning_overview``) and the vocabulary
review service; this module owns no database logic and writes nothing.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QFont
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

PAGE_NAME = "总览"
ROUTE_STYLE = """
QProgressBar#englishRouteProgress {
    background: rgba(1, 126, 218, 26); border: 0; border-radius: 2px; max-height: 4px;
}
QProgressBar#englishRouteProgress::chunk { background: #017EDA; border-radius: 2px; }
QLabel#englishRouteStep {
    background: rgba(1, 126, 218, 30); color: #017EDA; border-radius: 11px;
}
QLabel#englishRouteStatus { color: #017EDA; }
QPushButton#englishRecentButton {
    text-align: left; padding: 6px 12px; border-radius: 10px;
    background: rgba(255, 255, 255, 150); border: 1px solid rgba(255, 255, 255, 200);
}
QPushButton#englishRecentButton:hover { background: rgba(1, 126, 218, 22); }
"""
ROLE_NAMES = {
    "grammar": "文法", "grammar_exercises": "文法解题", "vocabulary": "字汇", "reading_training": "阅读",
    "writing": "写作", "extensive_reading": "广读",
}


def _center() -> Any:
    from shared.scripts import problem_bank_center_qt

    return problem_bank_center_qt


def _percent(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


class RouteCard(QWidget):
    """One stop of the learning route: its step number, page, live status and,
    for a page with a book, a thin progress bar.  Clicking opens the page."""

    clicked = Signal()

    def __init__(self, step: int, title: str, kind: str, status: str, note: str,
                 progress: tuple[int, int] | None = None, *, has_progress: bool = False) -> None:
        super().__init__()
        center = _center()
        frame = center.GlassFrame("actionCard")
        frame.setCursor(Qt.CursorShape.PointingHandCursor)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        row = QHBoxLayout(frame)
        row.setContentsMargins(15, 13, 15, 13)
        row.setSpacing(13)
        icon_shell = QLabel()
        icon_shell.setObjectName("actionIconShell")
        icon_shell.setFixedSize(36, 36)
        icon_layout = QHBoxLayout(icon_shell)
        icon_layout.setContentsMargins(9, 9, 9, 9)
        icon_layout.addWidget(center.LineIcon(kind, 18, center.THEME.accent))
        row.addWidget(icon_shell, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        number = QLabel(str(step))
        number.setObjectName("englishRouteStep")
        number.setFixedSize(22, 22)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center.set_font(number, 9, QFont.Weight.DemiBold)
        head.addWidget(number)
        title_label = QLabel(title)
        title_label.setObjectName("actionTitle")
        center.set_font(title_label, 11, QFont.Weight.DemiBold)
        head.addWidget(title_label)
        head.addStretch(1)
        self.status_label = QLabel(status)
        self.status_label.setObjectName("englishRouteStatus")
        center.set_font(self.status_label, 10, QFont.Weight.DemiBold)
        head.addWidget(self.status_label)
        copy.addLayout(head)
        note_label = QLabel(note)
        note_label.setObjectName("actionNote")
        note_label.setWordWrap(True)
        center.set_font(note_label, 9)
        copy.addWidget(note_label)
        self.bar = QProgressBar()
        self.bar.setObjectName("englishRouteProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        copy.addWidget(self.bar)
        self.bar.setVisible(has_progress or progress is not None)
        self.set_status(status, progress)
        row.addLayout(copy, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_status(self, status: str, progress: tuple[int, int] | None = None) -> None:
        self.status_label.setText(status)
        if progress is not None:
            self.bar.setRange(0, max(1, progress[1]))
            self.bar.setValue(progress[0])

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt virtual method
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _BookSignals(QObject):
    done = Signal(object)


class _BookTask(QRunnable):
    """Read the three books' outlines off the interface thread: a book whose
    cache is out of date (a new parser, a changed source) is parsed again."""

    def __init__(self, service: Any) -> None:
        super().__init__()
        self.service = service
        self.signals = _BookSignals()

    def run(self) -> None:
        try:
            result: Any = self.service.learning_overview()["methods"]
        except Exception as error:
            result = error
        self.signals.done.emit(result)


def _method_status(item: dict[str, Any]) -> tuple[str, tuple[int, int] | None]:
    if item.get("error") or item.get("total") == 0:
        return "原书不在本机", None
    if item.get("total") is None:
        return f"已读 {item['read']} 节", None
    return f"已读 {item['read']} / {item['total']} 节", (int(item["read"]), int(item["total"]))


def _pages_figure(methods: list[dict[str, Any]]) -> tuple[str, str]:
    read = sum(int(item["read"]) for item in methods)
    if any(item.get("total") is None for item in methods):
        return str(read), "正在读取三本书的小节数…"
    total = sum(int(item["total"] or 0) for item in methods)
    note = " · ".join(f"{item['page']} {item['read']}/{item['total']}" for item in methods if item["total"])
    return (f"{read} / {total}" if total else "—"), (note or "原书工程不在本机")


def _vocabulary_today(owner: Any) -> dict[str, Any]:
    try:
        from shared.scripts.vocabulary_review_qt import review_service_for

        return review_service_for(owner).today_summary()
    except Exception as error:
        return {"error": str(error)}


def _panel(title: str, note: str = "") -> tuple[Any, QVBoxLayout]:
    center = _center()
    panel = center.GlassFrame("glassPanel")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(10)
    head = QHBoxLayout()
    heading = QLabel(title)
    heading.setObjectName("sectionTitleSmall")
    center.set_font(heading, 12, QFont.Weight.DemiBold)
    head.addWidget(heading)
    if note:
        detail = QLabel(note)
        detail.setObjectName("pageNote")
        center.set_font(detail, 9)
        head.addSpacing(10)
        head.addWidget(detail)
    head.addStretch(1)
    layout.addLayout(head)
    return panel, layout


def build_english_overview_page(owner: Any) -> QVBoxLayout:
    """The 总览 page (the first page of the English workspace)."""

    center = _center()
    layout = QVBoxLayout()
    layout.setContentsMargins(22, 20, 22, 24)
    layout.setSpacing(16)
    heading_row = QHBoxLayout()
    heading_row.addLayout(owner._english_page_heading(
        "英语学习",
        "以旋元佑系列为主线：背字汇、学文法并做《文法解题》，再学阅读和写作方法，最后进入广读。"
        "下面是今天要做的事和各部分的进度，点卡片直接进入。",
    ), 1)
    heading_row.addWidget(owner._build_english_carousel_control(), 0, Qt.AlignmentFlag.AlignTop)
    layout.addLayout(heading_row)

    try:
        # The books are read in the background (below); everything else is quick.
        overview = owner.english_service.learning_overview(books=False)
    except Exception as error:
        failed = QLabel(f"读取学习进度失败：{error}")
        failed.setObjectName("pageNote")
        failed.setWordWrap(True)
        layout.addWidget(failed)
        layout.addStretch(1)
        return layout
    vocabulary = _vocabulary_today(owner)
    methods = {item["page"]: item for item in overview["methods"]}
    bank = overview["question_bank"]
    bank_total = sum(bank["counts"].values())
    materials = overview["materials"]

    # Today: four figures.
    grid_host = QWidget()
    grid = QGridLayout(grid_host)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    if "error" in vocabulary:
        words = center.MetricCard("今日背单词", "—", "背单词数据暂时读不到")
    else:
        due, new = int(vocabulary.get("due") or 0), int(vocabulary.get("new") or 0)
        reviewed = int(vocabulary.get("reviewed_today") or 0)
        words = center.MetricCard("今日背单词", str(due + new),
                                  f"待复习 {due} · 新词 {new}" + (f" · 今天已复习 {reviewed}" if reviewed else ""),
                                  "warm" if due else "normal")
    pages = center.MetricCard("学习页已读", *_pages_figure(overview["methods"]))
    answered = center.MetricCard("题库已做", str(bank["answered"]),
                                 f"共 {bank_total} 题 · 正确率 {_percent(bank['correct'], bank['attempts'])}",
                                 "success" if bank["answered"] else "normal")
    mistakes = center.MetricCard("未掌握的错题", str(bank["mistakes"]),
                                 "在题库的“错题本”里复习，答对后标为已掌握",
                                 "warm" if bank["mistakes"] else "normal")
    for index, card in enumerate((words, pages, answered, mistakes)):
        # One height for the row, whether the note takes one line or two.
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        grid.addWidget(card, 0, index)
    layout.addWidget(grid_host)

    # The route, in the order of the series.
    route, route_layout = _panel("学习路线", "按旋元佑系列的顺序，点卡片进入")
    route.setStyleSheet(ROUTE_STYLE)
    route_grid = QGridLayout()
    route_grid.setHorizontalSpacing(12)
    route_grid.setVerticalSpacing(12)

    method_cards: dict[str, RouteCard] = {}

    def method_card(step: int, page: str, note: str) -> tuple[RouteCard, str]:
        status, progress = _method_status(methods.get(page) or {"read": 0, "total": None})
        card = RouteCard(step, page, "markdown", status, note, progress, has_progress=True)
        method_cards[page] = card
        return card, page

    if "error" in vocabulary:
        words_status = ""
    else:
        words_status = f"今天 {int(vocabulary.get('due') or 0) + int(vocabulary.get('new') or 0)} 个"
    cards: list[tuple[RouteCard, str]] = [
        (RouteCard(1, "基础课程", "book", "",
                   "《旋元佑英文字彙》《旋元佑文法》《旋元佑文法解题》原书，在 PDF 阅读器中阅读、查词、记笔记。"),
         "基础课程"),
        (RouteCard(2, "背单词", "family", words_status,
                   "按原书单元背《英文字彙》，加上词汇库收藏的词；到期复习与新词按记忆曲线安排。"), "背单词"),
        method_card(3, "文法", "《旋元佑文法》全书：句子成分按原书标注，章末链接《文法解题》。"),
        (RouteCard(4, "题库", "source", f"已做 {bank['answered']} / {bank_total}",
                   f"文法 {bank['counts'].get('grammar', 0)} · 阅读 {bank['counts'].get('reading', 0)} · "
                   f"写作 {bank['counts'].get('writing', 0)} 题；错题 {bank['mistakes']}，收藏 {bank['favorites']}。",
                   (int(bank["answered"]), bank_total) if bank_total else None), "题库"),
        method_card(5, "阅读方法", "《旋元佑英文阅读》方法论：讲解与范例，范例可分屏练习。"),
        method_card(6, "写作方法", "《旋元佑英文写作》：错误分析、章法结构、句法修辞，章末练习在题库写作类。"),
        (RouteCard(7, "广读材料", "overview", f"书架 {materials['extensive']} 本",
                   "「广读学英文」视频导读和推荐书单；导入自己的书统一在 PDF 阅读器中读。"), "广读材料"),
        (RouteCard(8, "词汇库", "glossary",
                   f"待查词 {overview['deferred_lookups']}" if overview["deferred_lookups"] else "",
                   "阅读和做题时收藏的单词：查词、熟悉度和导出。"), "词汇库"),
    ]
    for index, (card, page) in enumerate(cards):
        card.clicked.connect(lambda destination=page: owner.show_page(destination))
        route_grid.addWidget(card, index // 2, index % 2)
    route_layout.addLayout(route_grid)
    layout.addWidget(route)

    # Recently opened materials and the last question-bank sync.
    recent = materials["recent"]
    sync_summary = getattr(owner, "english_content_sync_summary", None)
    if recent or sync_summary:
        recent_panel, recent_layout = _panel("最近打开")
        recent_panel.setStyleSheet(ROUTE_STYLE)
        row = QHBoxLayout()
        row.setSpacing(8)
        for item in recent:
            role = ROLE_NAMES.get(item["role"], "")
            button = QPushButton(f"{item['title']}" + (f"  ·  {role}" if role else ""))
            button.setObjectName("englishRecentButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            center.set_font(button, 10)
            button.clicked.connect(_opener(owner, int(item["id"])))
            row.addWidget(button)
        row.addStretch(1)
        recent_layout.addLayout(row)
        if sync_summary:
            from shared.scripts.english_content_sync import summary_text

            sync = QLabel("本次启动的题库同步：" + summary_text(sync_summary))
            sync.setObjectName("pageNote")
            sync.setWordWrap(True)
            center.set_font(sync, 9)
            recent_layout.addWidget(sync)
        layout.addWidget(recent_panel)
    layout.addStretch(1)
    _read_books_later(owner, pages, method_cards)
    return layout


def _read_books_later(owner: Any, pages: Any, method_cards: dict[str, RouteCard]) -> None:
    task = _BookTask(owner.english_service)
    task.setAutoDelete(False)

    def apply(result: Any) -> None:
        if getattr(owner, "_english_overview_task", None) is task:
            owner._english_overview_task = None
        if isinstance(result, Exception) or not isValid(pages):
            return
        pages.update_content(*_pages_figure(result))
        for item in result:
            card = method_cards.get(item["page"])
            if card is not None and isValid(card):
                status, progress = _method_status(item)
                card.set_status(status, progress)
                card.bar.setVisible(progress is not None)

    task.signals.done.connect(apply)
    owner._english_overview_task = task  # keeps the task and its signals alive until it reports
    QThreadPool.globalInstance().start(task)


def _opener(owner: Any, material_id: int) -> Callable[[], None]:
    return lambda: owner.open_english_material(material_id)


__all__ = ["PAGE_NAME", "RouteCard", "build_english_overview_page"]
