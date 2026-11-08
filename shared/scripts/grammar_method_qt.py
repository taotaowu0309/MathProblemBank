"""The 文法 page of the English workspace: the lectures of 《旋元佑文法》.

The left column lists the book by part, chapter and section with the
learner's progress; the middle shows one section in the book's own forms —
example boxes, example sentences with their translations (which can be hidden
for self-testing), sentences analysed into labelled parts (each part
underlined in its colour with the book's label under it), sentences marked
wrong, pattern formulas, category pills, tables, the tense timelines of
chapter 9 (drawn from the book's coordinates) and the noun-phrase figure of
chapter 2.  At the end of a chapter a card links to the question bank's
grammar category, filtered to that chapter of 《文法解题》.  The right column
is the textbook-notes panel of the PDF reader.

All data comes from :class:`EnglishLearningService`; this module owns no
database logic.
"""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.method_book_qt import ACCENT, READ_GREEN, MethodBookView, _button, _clear
from shared.scripts.raydu_reading_parser import runs_to_text

PAGE_NAME = "文法"
MUTED = "#5b6b7a"
INK = "#263746"
ERROR_RED = "#d64541"
TIMELINE_RED = "#a51c24"
# The colour of an analysed part, by the kind of label the book prints under it.
PART_COLORS = (
    (re.compile(r"^S\b|^S（|主词"), "#1f6fd1"),
    (re.compile(r"^V\b|^V（|动词"), "#d64541"),
    (re.compile(r"^O\b|^O（|受词"), "#2e8b57"),
    (re.compile(r"^C\b|^C（|补语"), "#d9822b"),
    (re.compile(r"子句"), "#7b5aa6"),
    (re.compile(r"片语|构句"), "#138d90"),
)
OTHER_PART_COLOR = "#5b6b7a"
BOX_STYLE = """
QFrame#grammarExample {
    background: rgba(255, 255, 255, 205); border: 1px solid rgba(1, 126, 218, 46); border-radius: 12px;
}
QFrame#grammarExercise {
    background: rgba(255, 248, 235, 220); border: 1px solid rgba(217, 130, 43, 90); border-radius: 12px;
}
QLabel#grammarExerciseTag {
    background: #d9822b; color: white; border-radius: 10px; padding: 3px 14px;
}
QFrame#grammarPartBanner {
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0, stop: 0 #017EDA, stop: 1 #4aa8ec); border-radius: 14px;
}
QFrame#grammarAnswer { background: transparent; border: 0; border-left: 3px solid rgba(60, 70, 80, 90); }
QFrame#grammarQuote { background: rgba(240, 244, 248, 160); border: 0; border-radius: 6px; }
QLabel#grammarPill {
    background: rgba(1, 126, 218, 22); color: #017EDA; border: 1px solid rgba(1, 126, 218, 150);
    border-radius: 6px; padding: 1px 8px;
}
QWidget#grammarPart[hovered="true"] { background: rgba(1, 126, 218, 16); border-radius: 4px; }
"""


def _center() -> Any:
    from shared.scripts import problem_bank_center_qt

    return problem_bank_center_qt


def _escape(text: Any) -> str:
    return _center()._html_escape(str(text or ""))


def part_color(label: str) -> str:
    """The colour of an analysed part with this label (S, V, O, C, a clause, a phrase)."""

    text = str(label or "").strip()
    for pattern, color in PART_COLORS:
        if pattern.search(text):
            return color
    return OTHER_PART_COLOR


_GRAMMAR_KEYS = ("pat", "pill", "frame", "seg", "sid")


def runs_html(runs: Any) -> str:
    """Qt rich text for the grammar book's runs: the bank's styles plus the
    pattern formulas, category pills and boxed words of this book."""

    render = _center().english_question_runs_html
    parts: list[str] = []
    for run in runs or []:
        plain = {key: value for key, value in run.items() if key not in _GRAMMAR_KEYS}
        if "seg" in run:
            # An analysed part outside an analysis line: underline and a small label.
            plain["u"] = bool(run.get("u"))
        text = render([plain])
        if run.get("pat"):
            text = f'<span style="color: {ACCENT}; font-family: sans-serif; font-weight: 700;">{text}</span>'
        if run.get("pill"):
            text = (f'<span style="background-color: #e1effb; color: {ACCENT}; font-family: sans-serif; '
                    f'font-weight: 600;">&nbsp;{text}&nbsp;</span>')
        if run.get("frame"):
            text = f'<span style="background-color: #ffe9b3;">&nbsp;{text}&nbsp;</span>'
        parts.append(text)
    return "".join(parts)


def _font_size(size: float) -> float:
    return _center().ui_point_size(size)


def _wrap(html_text: str, *, size: float = 14, color: str = INK, line_height: int = 145) -> str:
    serif = _center().ENGLISH_QUESTION_SERIF
    # Qt does not inherit line-height into nested paragraphs: without it on each
    # <p>, only the first paragraph of a merged label gets the leading.
    html_text = html_text.replace('<p style="', f'<p style="line-height: {line_height}%; ')
    return (f'<div style="font-family: {serif}; font-size: {_font_size(size):.1f}pt; color: {color}; '
            f'line-height: {line_height}%;">{html_text}</div>')


def _rich(html_text: str, *, size: float = 14, color: str = INK, wrap: bool = True, line_height: int = 145) -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setText(_wrap(html_text, size=size, color=color, line_height=line_height))
    return label


# ---------------------------------------------------------------------------
# Analysed sentences

class _FlowLayout(QLayout):
    """Words and analysed parts left to right, wrapping like text; items in a
    row share their top edge, so the English text of the row stays on one line."""

    def __init__(self, parent: QWidget | None = None, *, row_gap: int = 4) -> None:
        super().__init__(parent)
        self._items: list[Any] = []
        self._row_gap = row_gap
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: Any) -> None:  # noqa: N802 - Qt virtual method
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Any | None:  # noqa: N802 - Qt virtual method
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Any | None:  # noqa: N802 - Qt virtual method
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt virtual method
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt virtual method
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt virtual method
        return self._arrange(QRect(0, 0, max(1, width), 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt virtual method
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        width = sum(item.sizeHint().width() for item in self._items)
        return QSize(min(width, 720), self.heightForWidth(min(width, 720) or 1))

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt virtual method
        width = max((item.minimumSize().width() for item in self._items), default=0)
        height = max((item.minimumSize().height() for item in self._items), default=0)
        return QSize(width, height)

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        x, y, row_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            width = min(hint.width(), rect.width())
            if x > rect.x() and x + width > rect.right() + 1:
                x, y = rect.x(), y + row_height + self._row_gap
                row_height = 0
            if apply:
                item.setGeometry(QRect(x, y, width, hint.height()))
            x += width
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y()


class _Part(QWidget):
    """One analysed part: its English, a line in the part's colour (when the
    book underlines it) and the book's label under it."""

    def __init__(self, runs: list[dict[str, Any]], label: str, *, underline: bool, trailing_space: bool,
                 size: float) -> None:
        super().__init__()
        self.setObjectName("grammarPart")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        color = part_color(label)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        outer.addLayout(layout)
        # The coloured line under the part replaces the book's underline.
        # Single line height: the extra leading of running text would sit between
        # the words and their line.
        self.text = _rich(runs_html([{key: value for key, value in run.items() if key != "u"} for run in runs]),
                          size=size, wrap=False, line_height=100)
        layout.addWidget(self.text)
        if trailing_space:
            outer.addSpacing(round(QFontMetricsF(_label_font(size)).horizontalAdvance(" ")))
        line = QFrame()
        line.setFixedHeight(2)
        line.setStyleSheet(f"background: {color if underline else 'transparent'}; border: 0;")
        layout.addWidget(line)
        self.label = QLabel(label)
        self.label.setStyleSheet(f"color: {color}; font-family: sans-serif;")
        font = self.label.font()
        font.setPointSizeF(_font_size(9.5))
        font.setWeight(QFont.Weight.DemiBold)
        self.label.setFont(font)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.label)
        self.setToolTip(label)

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt virtual method
        self.setProperty("hovered", True)
        self.style().polish(self)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt virtual method
        self.setProperty("hovered", False)
        self.style().polish(self)
        super().leaveEvent(event)


def _tokens(runs: list[dict[str, Any]]) -> list[tuple[str, Any, bool]]:
    """An analysis line as ("part", (runs, label, underlined), space after) and
    ("word", runs, space after) tokens, so plain text wraps word by word."""

    tokens: list[tuple[str, Any, bool]] = []

    def mark_space() -> None:
        if tokens:
            kind, value, _space = tokens[-1]
            tokens[-1] = (kind, value, True)

    index = 0
    while index < len(runs):
        run = runs[index]
        if "sid" in run:
            group = [run]
            while index + 1 < len(runs) and runs[index + 1].get("sid") == run["sid"]:
                index += 1
                group.append(runs[index])
            tokens.append(("part", (group, str(run.get("seg") or ""), bool(run.get("u"))), False))
        elif run.get("br") or run.get("line"):
            mark_space()
        elif "t" in run:
            for piece in re.findall(r"\S+|\s+", str(run["t"])):
                if piece.isspace():
                    mark_space()
                elif tokens and tokens[-1][0] == "word" and not tokens[-1][2]:
                    # Two styles inside one word (wh + ose): keep them together.
                    previous = tokens[-1][1]
                    tokens[-1] = ("word", [*previous, {**run, "t": piece}], False)
                else:
                    tokens.append(("word", [{**run, "t": piece}], False))
        index += 1
    return tokens


class AnalysisLine(QWidget):
    """An English sentence analysed into labelled parts, wrapping like text."""

    def __init__(self, runs: list[dict[str, Any]], *, size: float = 14, prefix_html: str = "") -> None:
        super().__init__()
        flow = _FlowLayout(self, row_gap=8)
        size_policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        size_policy.setHeightForWidth(True)
        self.setSizePolicy(size_policy)
        if prefix_html:
            flow.addWidget(_rich(prefix_html, size=size, wrap=False, line_height=100))
        self.parts: list[_Part] = []
        for kind, value, space in _tokens(runs):
            if kind == "part":
                group, label, underline = value
                part = _Part(group, label, underline=underline, trailing_space=space, size=size)
                self.parts.append(part)
                flow.addWidget(part)
            else:
                html_text = runs_html(value) + ("&nbsp;" if space else "")
                word = _rich(html_text, size=size, wrap=False, line_height=100)
                word.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                flow.addWidget(word)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt virtual method
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt virtual method
        return self.layout().heightForWidth(width)


# ---------------------------------------------------------------------------
# Figures

def _label_font(size: float = 12.5) -> QFont:
    font = QFont()
    font.setFamilies(["Times New Roman", "Songti SC", "STSong", "SimSun"])
    font.setPointSizeF(_font_size(size))
    return font


class _Figure(QWidget):
    """A drawing in the book's coordinates, scaled to the column width."""

    MAX_UNIT = 52.0

    def __init__(self) -> None:
        super().__init__()
        size_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        size_policy.setHeightForWidth(True)
        self.setSizePolicy(size_policy)
        self.x_range = (0.0, 10.0)
        self.y_range = (-1.0, 1.0)

    def unit(self, width: int) -> float:
        span = self.x_range[1] - self.x_range[0]
        return max(14.0, min(self.MAX_UNIT, (width - 16) / span))

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt virtual method
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt virtual method
        return int((self.y_range[1] - self.y_range[0]) * self.unit(width)) + 12

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(560, self.heightForWidth(560))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(240, self.heightForWidth(240))

    def point(self, x: float, y: float) -> QPointF:
        unit = self.unit(self.width())
        left = (self.width() - (self.x_range[1] - self.x_range[0]) * unit) / 2
        return QPointF(left + (x - self.x_range[0]) * unit, 6 + (self.y_range[1] - y) * unit)


def _arrow_head(painter: QPainter, start: QPointF, end: QPointF, size: float) -> None:
    direction = end - start
    length = (direction.x() ** 2 + direction.y() ** 2) ** 0.5 or 1.0
    unit = QPointF(direction.x() / length, direction.y() / length)
    normal = QPointF(-unit.y(), unit.x())
    back = end - unit * size
    painter.drawPolygon(QPolygonF([end, back + normal * (size * 0.45), back - normal * (size * 0.45)]))


class TimelineView(_Figure):
    """A tense timeline of chapter 9: the axis, time brackets, verbs, now and
    the perfect-aspect arrows, at the positions the book gives."""

    def __init__(self, block: dict[str, Any]) -> None:
        super().__init__()
        self.items = list(block.get("items") or [])
        ys = [0.0]
        for item in self.items:
            if item["type"] == "label":
                ys.append(float(item["y"]))
            elif item["type"] == "line":
                ys.extend(float(point[1]) for point in item["points"])
        self.x_range = (-0.3, float(block.get("width") or 10.2) + 0.3)
        self.y_range = (min(ys) - 0.42, max(ys) + 0.42)
        self.setToolTip(" · ".join(runs_to_text(item["runs"]) for item in self.items if item.get("runs")))

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt virtual method
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        unit = self.unit(self.width())
        red = QColor(TIMELINE_RED)
        for item in self.items:
            if item["type"] != "line":
                continue
            points = [self.point(float(x), float(y)) for x, y in item["points"]]
            painter.setPen(QPen(red, 1.3))
            for start, end in zip(points, points[1:]):
                painter.drawLine(start, end)
            if item.get("arrow"):
                painter.setBrush(red)
                _arrow_head(painter, points[-2], points[-1], max(6.0, unit * 0.16))
                painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(INK), 1.4))
        for item in self.items:
            if item["type"] == "cross":
                center = self.point(float(item["x"]), 0.0)
                arm = max(3.5, unit * 0.09)
                painter.drawLine(center + QPointF(-arm, -arm), center + QPointF(arm, arm))
                painter.drawLine(center + QPointF(-arm, arm), center + QPointF(arm, -arm))
        font = _label_font(12.5)
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        for item in self.items:
            if item["type"] != "label":
                continue
            text = runs_to_text(item["runs"])
            center = self.point(float(item["x"]), float(item["y"]))
            width, height = metrics.horizontalAdvance(text), metrics.height()
            box = QRectF(center.x() - width / 2, center.y() - height / 2, width, height)
            if item.get("box"):
                frame = box.adjusted(-7, -2, 7, 2)
                painter.setPen(QPen(red, 1.1))
                painter.setBrush(QColor("white"))
                painter.drawRect(frame)
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QColor(INK))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


class DiagramView(_Figure):
    """The nested noun phrase of chapter 2: labelled nodes (pills or words) and
    the bracket lines drawn from the nodes' edges."""

    MAX_UNIT = 66.0

    def __init__(self, block: dict[str, Any]) -> None:
        super().__init__()
        self.nodes = list(block.get("nodes") or [])
        self.paths = list(block.get("paths") or [])
        xs = [float(node["x"]) for node in self.nodes] or [0.0]
        ys = [float(node["y"]) for node in self.nodes] or [0.0]
        self.x_range = (min(xs) - 1.6, max(xs) + 1.6)
        self.y_range = (min(ys) - 0.45, max(ys) + 0.45)

    def _node_rects(self, metrics: QFontMetricsF) -> dict[str, QRectF]:
        rects: dict[str, QRectF] = {}
        for index, node in enumerate(self.nodes):
            text = runs_to_text(node["runs"])
            pill = any(run.get("pill") for run in node["runs"])
            width = metrics.horizontalAdvance(text) + (18 if pill else 6)
            height = metrics.height() + (6 if pill else 2)
            center = self.point(float(node["x"]), float(node["y"]))
            rect = QRectF(center.x() - width / 2, center.y() - height / 2, width, height)
            rects[node["name"] or f"#{index}"] = rect
        return rects

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt virtual method
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        unit = self.unit(self.width())
        font = _label_font(12.5)
        metrics = QFontMetricsF(font)
        rects = self._node_rects(metrics)
        painter.setPen(QPen(QColor(ACCENT), 1.2))
        for path in self.paths:
            points: list[tuple[QPointF, bool]] = []
            for point in path:
                if point["node"]:
                    rect = rects[point["node"]]
                    base = {
                        "west": QPointF(rect.left(), rect.center().y()),
                        "east": QPointF(rect.right(), rect.center().y()),
                        "north": QPointF(rect.center().x(), rect.top()),
                        "south": QPointF(rect.center().x(), rect.bottom()),
                    }.get(point["anchor"], rect.center())
                    location = base + QPointF(float(point["x"]) * unit, -float(point["y"]) * unit)
                else:
                    location = self.point(float(point["x"]), float(point["y"]))
                points.append((location, bool(point["draw"])))
            for (start, _drawn), (end, drawn) in zip(points, points[1:]):
                if drawn:
                    painter.drawLine(start, end)
        for index, node in enumerate(self.nodes):
            rect = rects[node["name"] or f"#{index}"]
            text = runs_to_text(node["runs"])
            pill = any(run.get("pill") for run in node["runs"])
            if pill:
                shape = QPainterPath()
                shape.addRoundedRect(rect, 6, 6)
                painter.setPen(QPen(QColor(ACCENT), 1.0))
                painter.setBrush(QColor(225, 239, 251))
                painter.drawPath(shape)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                bold = QFont(font)
                bold.setFamilies(["PingFang SC", "Microsoft YaHei", "sans-serif"])
                bold.setWeight(QFont.Weight.DemiBold)
                painter.setFont(bold)
                painter.setPen(QColor(ACCENT))
            else:
                painter.setFont(font)
                painter.setPen(QColor(INK))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


# ---------------------------------------------------------------------------
# The page

def _pill(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("grammarPill")
    font = label.font()
    font.setPointSizeF(_font_size(11))
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return label


def _section_number(key: str) -> str:
    match = re.fullmatch(r"XYGM-(\w+)-(\d{2})", str(key or ""))
    if match is None:
        return ""
    chapter = str(int(match.group(1))) if match.group(1).isdigit() else match.group(1)
    return f"{chapter}.{int(match.group(2))}"


class GrammarMethodView(MethodBookView):
    BOOK_TITLE = "旋元佑文法"
    NAV_TITLE = "文法目录"
    OWNER_SECTION_ATTRIBUTE = "grammar_method_section_key"

    def __init__(self, owner: Any) -> None:
        super().__init__(owner)
        self.setStyleSheet(self.styleSheet() + BOX_STYLE)
        self.translations: list[QWidget] = []
        self.hide_translations = bool(getattr(owner, "grammar_method_hide_translations", False))

    def _outline(self) -> dict[str, Any]:
        return self.service.grammar_method_outline()

    def _section(self, key: str) -> dict[str, Any]:
        return self.service.grammar_method_section(key)

    def _set_read(self, key: str, read: bool) -> dict[str, Any]:
        return self.service.mark_grammar_method_section_read(key, read)

    # -- the outline --------------------------------------------------------------

    def _fill_tree(self) -> None:
        data = self.outline_data
        sections = data.get("sections") or []
        by_key = {section["key"]: section for section in sections}
        read_count = int(data.get("read_count") or 0)
        self.progress_label.setText(f"已读 {read_count} / {len(sections)} 节")
        self.progress_bar.setRange(0, max(1, len(sections)))
        self.progress_bar.setValue(read_count)
        self.tree.blockSignals(True)
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setWeight(QFont.Weight.DemiBold)
        part_items: dict[int, QTreeWidgetItem] = {}
        for part in data.get("parts") or []:
            first = next((chapter["section_keys"][0] for chapter in data.get("chapters") or []
                          if chapter["part"] == part["part"] and chapter["section_keys"]), "")
            item = QTreeWidgetItem([part["title"], ""])
            item.setData(0, Qt.ItemDataRole.UserRole, first)
            if part.get("abstract"):
                item.setToolTip(0, part["abstract"])
            larger = QFont(bold)
            larger.setPointSizeF(larger.pointSizeF() * 1.06)
            item.setFont(0, larger)
            self.tree.addTopLevelItem(item)
            part_items[part["part"]] = item
        current_item: QTreeWidgetItem | None = None
        for chapter in data.get("chapters") or []:
            keys = chapter["section_keys"]
            done = sum(1 for key in keys if by_key.get(key, {}).get("read"))
            number = "" if chapter.get("appendix") or chapter.get("front") else f"{chapter['chapter']}  "
            chapter_item = QTreeWidgetItem([f"{number}{chapter['title']}", "✓" if keys and done == len(keys) else ""])
            chapter_item.setData(0, Qt.ItemDataRole.UserRole, keys[0] if keys else "")
            chapter_item.setFont(0, bold)
            chapter_item.setForeground(1, QColor(READ_GREEN))
            tip = [f"已读 {done} / {len(keys)} 节"]
            if chapter.get("exercise_count"):
                tip.append(f"《文法解题》{chapter['exercise_count']} 题（在题库）")
            if chapter.get("source_page"):
                tip.append(f"原书第 {chapter['source_page']} 页")
            for column in (0, 1):
                chapter_item.setToolTip(column, " · ".join(tip))
            part_items[chapter["part"]].addChild(chapter_item)
            if len(keys) == 1 and by_key[keys[0]]["intro"]:
                # A chapter of one section (总序, 前言): the chapter item is the section.
                chapter_item.setText(1, "✓" if by_key[keys[0]]["read"] else "")
                if keys[0] == self.section_key:
                    current_item = chapter_item
                continue
            for key in keys:
                section = by_key[key]
                item = QTreeWidgetItem([section["title"], "✓" if section["read"] else ""])
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                item.setForeground(1, QColor(READ_GREEN))
                tip = [f"原书第 {section['source_page']} 页"] if section.get("source_page") else []
                tip.append("已读" if section["read"] else "未读")
                for column in (0, 1):
                    item.setToolTip(column, " · ".join(tip))
                chapter_item.addChild(item)
                if key == self.section_key:
                    current_item = item
        for part in part_items.values():
            part.setExpanded(True)
        if current_item is not None:
            self._expand_only(current_item)
            self.tree.setCurrentItem(current_item)
        self.tree.blockSignals(False)

    def _expand_only(self, item: QTreeWidgetItem) -> None:
        """Show the sections of the current chapter only; the book has 175."""
        chapter = item if item.parent() is not None and item.parent().parent() is None else item.parent()
        for index in range(self.tree.topLevelItemCount()):
            part = self.tree.topLevelItem(index)
            for position in range(part.childCount()):
                child = part.child(position)
                child.setExpanded(child is chapter)

    def _select_tree_item(self, key: str) -> None:
        iterator = [self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        while iterator:
            item = iterator.pop(0)
            children = [item.child(index) for index in range(item.childCount())]
            # A part or chapter item carries its first section's key; prefer the section itself.
            if item.data(0, Qt.ItemDataRole.UserRole) == key and not any(
                    child.data(0, Qt.ItemDataRole.UserRole) == key for child in children):
                self.tree.blockSignals(True)
                self._expand_only(item)
                if self.tree.currentItem() is not item:
                    self.tree.setCurrentItem(item)
                self.tree.blockSignals(False)
                return
            iterator.extend(children)

    # -- the section ----------------------------------------------------------------

    def _render_section(self) -> None:
        center = self.center
        section = self.section or {}
        self.notes_button.setParent(None)
        _clear(self.section_layout)
        self.translations = []
        lookup = self.owner._english_question_word_lookup()
        lookup.close()
        self.section_layout.addWidget(self._header(section))
        if section.get("part_abstract"):
            self.section_layout.addWidget(self._part_banner(section))

        blocks = list(section.get("blocks") or [])
        answers_at = section.get("answers_at")
        lesson = blocks if answers_at is None else blocks[:answers_at]
        lecture = center.GlassFrame("standardProblemCard")
        lecture_layout = QVBoxLayout(lecture)
        lecture_layout.setContentsMargins(22, 16, 22, 18)
        lecture_layout.setSpacing(10)
        self._add_blocks(lesson, lecture_layout)
        if answers_at is not None:
            lecture_layout.addWidget(self._folded(blocks[answers_at:], "解答"))
        self.section_layout.addWidget(lecture)
        lookup.attach(lecture)
        count = int(section.get("chapter_exercise_count") or 0)
        if section.get("chapter_last") and count:
            self.section_layout.addWidget(self._exercise_card(section, count))
        if section.get("appendix"):
            self.section_layout.addWidget(self._reading_card())
        self.section_layout.addLayout(self._step_row(section))
        self.section_layout.addStretch(1)
        self._apply_translation_visibility()
        self.section_scroll.verticalScrollBar().setValue(0)

    def _header(self, section: dict[str, Any]) -> QFrame:
        center = self.center
        header_card = QFrame()
        header_card.setObjectName("readingMethodHeader")
        header_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header = QHBoxLayout(header_card)
        header.setContentsMargins(24, 16, 18, 16)
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        part = " ".join(item for item in (section.get("part_title"), section.get("part_name")) if item)
        breadcrumb = QLabel("　›　".join(item for item in (self.BOOK_TITLE, part, section.get("chapter_title")) if item))
        breadcrumb.setObjectName("readingMethodBreadcrumb")
        center.set_font(breadcrumb, 10)
        title_box.addWidget(breadcrumb)
        number = "" if section.get("key", "").endswith("-00") else _section_number(section.get("key", ""))
        title = QLabel(" ".join(item for item in (number, str(section.get("title") or "")) if item))
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {ACCENT};")
        center.set_font(title, 20, QFont.Weight.DemiBold)
        title_box.addWidget(title)
        if section.get("source_page"):
            detail = QLabel(f"原书第 {int(section['source_page'])} 页")
            detail.setObjectName("pageNote")
            center.set_font(detail, 10)
            title_box.addWidget(detail)
        header.addLayout(title_box, 1)
        self.translation_button = _button("", self._toggle_translations, "隐藏或显示例句的中文译文，先看英文自测")
        self.translation_button.setCheckable(True)
        self.translation_button.setChecked(self.hide_translations)
        self.translation_button.setText("显示译文" if self.hide_translations else "隐藏译文")
        header.addWidget(self.translation_button, 0, Qt.AlignmentFlag.AlignVCenter)
        if section.get("source_page"):
            header.addWidget(_button("打开原书", lambda: self.open_book(),
                                     f"在内部阅读器中打开《{self.BOOK_TITLE}》第 {int(section['source_page'])} 页"),
                             0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._read_button(), 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.notes_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return header_card

    def _part_banner(self, section: dict[str, Any]) -> QFrame:
        """The part's title page: its name and the one-line abstract the book prints there."""
        banner = QFrame()
        banner.setObjectName("grammarPartBanner")
        banner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(banner)
        row.setContentsMargins(22, 12, 22, 12)
        row.setSpacing(14)
        part = " ".join(item for item in (section.get("part_title"), section.get("part_name")) if item)
        name = QLabel(part)
        name.setStyleSheet("color: white;")
        self.center.set_font(name, 13, QFont.Weight.DemiBold)
        row.addWidget(name)
        abstract = QLabel(str(section.get("part_abstract") or ""))
        abstract.setWordWrap(True)
        abstract.setStyleSheet("color: white;")
        self.center.set_font(abstract, 12)
        row.addWidget(abstract, 1)
        return banner

    def _toggle_translations(self) -> None:
        self.hide_translations = self.translation_button.isChecked()
        self.owner.grammar_method_hide_translations = self.hide_translations
        self.translation_button.setText("显示译文" if self.hide_translations else "隐藏译文")
        self._apply_translation_visibility()

    def _apply_translation_visibility(self) -> None:
        for widget in self.translations:
            widget.setVisible(not self.hide_translations)

    def _add_blocks(self, blocks: list[dict[str, Any]], layout: QVBoxLayout) -> None:
        """Running text is merged into rich-text labels; everything else is a widget."""
        pending: list[str] = []

        def flush() -> None:
            if pending:
                layout.addWidget(_rich("".join(pending)))
                pending.clear()

        for block in blocks:
            kind = block.get("kind")
            if kind == "para":
                pending.append(f'<p style="margin: 0 0 8px 0;">{runs_html(block.get("runs"))}</p>')
                continue
            if kind == "term":
                note = runs_html(block.get("note"))
                pending.append(
                    f'<p style="margin: 12px 0 4px 0;"><span style="color: {ACCENT}; font-family: sans-serif; '
                    f'font-weight: 700; font-size: {_font_size(14.5):.1f}pt;">{runs_html(block.get("runs"))}</span>'
                    + (f'&nbsp;&nbsp;<span style="color: {MUTED};">{note}</span>' if note else "") + "</p>"
                )
                continue
            flush()
            widget = self._block_widget(block)
            if widget is not None:
                layout.addWidget(widget)
        flush()

    def _block_widget(self, block: dict[str, Any]) -> QWidget | None:
        kind = block.get("kind")
        if kind == "sentence":
            return self._sentence(block)
        if kind == "analysis":
            return AnalysisLine(block.get("runs") or [])
        if kind == "translation":
            label = _rich(runs_html(block.get("runs")), size=12.5, color=MUTED)
            self.translations.append(label)
            return label
        if kind == "centered":
            label = _rich(runs_html(block.get("runs")))
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            return label
        if kind in {"example", "exercise"}:
            return self._box(block)
        if kind == "answer":
            return self._framed("grammarAnswer", block.get("blocks") or [], (14, 4, 0, 4))
        if kind == "quote":
            return self._framed("grammarQuote", block.get("blocks") or [], (14, 8, 14, 8))
        if kind == "list":
            return self._list(block)
        if kind == "table":
            return self._table(block)
        if kind == "timeline":
            return TimelineView(block)
        if kind == "diagram":
            return DiagramView(block)
        return None

    def _sentence(self, block: dict[str, Any]) -> QWidget:
        """One example sentence: a bullet (or * for a wrong one), the English —
        analysed or plain — and the translation and notes under it."""
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)
        error = str(block.get("error") or "")
        bullet = QLabel("*" if error else "▪")
        bullet.setStyleSheet(f"color: {ERROR_RED if error else ACCENT}; font-weight: 700;")
        bullet.setFixedWidth(16)
        bullet.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.center.set_font(bullet, 14)
        row.addWidget(bullet, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        if block.get("title"):
            column.addWidget(_rich(f"<b>{_escape(block['title'])}</b>"))
        marked = False
        for line in block.get("lines") or []:
            runs = line.get("runs") or []
            if line.get("role") == "zh":
                label = _rich(runs_html(runs), size=12.5, color=MUTED)
                self.translations.append(label)
                column.addWidget(label)
                continue
            if any("sid" in run for run in runs):
                column.addWidget(AnalysisLine(runs))
            else:
                column.addWidget(_rich(runs_html(runs)))
            if error and not marked:
                # As in the book, the verdict (【误】, 【不佳】) is the line under the sentence.
                column.addWidget(_rich(f'<span style="color: {ERROR_RED}; font-family: sans-serif; '
                                       f'font-weight: 700;">{_escape(error)}</span>', size=11.5, wrap=False))
                marked = True
        row.addLayout(column, 1)
        return row_widget

    def _box(self, block: dict[str, Any]) -> QFrame:
        """An example box (with the book's Example number) or the exercise box."""
        exercise = block.get("kind") == "exercise"
        frame = QFrame()
        frame.setObjectName("grammarExercise" if exercise else "grammarExample")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(6)
        # The book's own label, numbered by the parser through the chapter.
        text = " ".join(item for item in ("Exercise" if exercise else "Example", str(block.get("number") or ""))
                        if item)
        if block.get("title"):
            text += f" · {block['title']}"
        tag = QLabel(text)
        tag.setObjectName("grammarExerciseTag" if exercise else "readingMethodExampleTag")
        self.center.set_font(tag, 10, QFont.Weight.DemiBold)
        tag.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addWidget(tag, 0, Qt.AlignmentFlag.AlignLeft)
        self._add_blocks(block.get("blocks") or [], layout)
        return frame

    def _framed(self, name: str, blocks: list[dict[str, Any]], margins: tuple[int, int, int, int]) -> QFrame:
        frame = QFrame()
        frame.setObjectName(name)
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(*margins)
        layout.setSpacing(4)
        self._add_blocks(blocks, layout)
        return frame

    def _list(self, block: dict[str, Any]) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.setSpacing(6)
        for number, item in enumerate(block.get("items") or [], start=int(block.get("start") or 1)):
            row = QHBoxLayout()
            row.setSpacing(6)
            mark = QLabel(f"{number}." if block.get("ordered") else "•")
            mark.setStyleSheet(f"color: {ACCENT}; font-family: sans-serif; font-weight: 700;")
            self.center.set_font(mark, 13)
            mark.setFixedWidth(26)
            mark.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            column = QVBoxLayout()
            column.setSpacing(4)
            self._add_blocks(item, column)
            row.addLayout(column, 1)
            layout.addLayout(row)
        return holder

    def _table(self, block: dict[str, Any]) -> QWidget:
        """A table of the book; the text between columns (the + of a formula) sits
        in its own column, and a cell holding only a category label is a pill."""
        holder = QWidget()
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(0, 4, 0, 4)
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        aligns = list(block.get("align") or [])
        joins = list(block.get("joins") or [])
        alignment = {"l": Qt.AlignmentFlag.AlignLeft, "c": Qt.AlignmentFlag.AlignHCenter,
                     "r": Qt.AlignmentFlag.AlignRight}
        for row_index, row in enumerate(block.get("rows") or []):
            head = bool(block.get("head")) and row_index == 0
            for column_index, cell in enumerate(row):
                grid_column = column_index * 2
                runs = cell or []
                if runs and all(run.get("pill") for run in runs if "t" in run):
                    widget: QWidget = _pill(runs_to_text(runs))
                elif any("sid" in run for run in runs):
                    widget = AnalysisLine(runs)
                else:
                    html_text = runs_html(runs)
                    if head:
                        html_text = f'<span style="color: {ACCENT}; font-family: sans-serif;"><b>{html_text}</b></span>'
                    long_text = len(runs_to_text(runs)) > 24
                    widget = _rich(html_text, wrap=long_text and column_index == len(row) - 1)
                align = alignment.get(aligns[column_index] if column_index < len(aligns) else "l",
                                      Qt.AlignmentFlag.AlignLeft)
                if isinstance(widget, QLabel) and widget.wordWrap():
                    # A cell given an alignment gets its one-line size hint, which
                    # cuts off a wrapped second line; the label aligns its own text.
                    widget.setAlignment(align | Qt.AlignmentFlag.AlignVCenter)
                    grid.addWidget(widget, row_index, grid_column)
                else:
                    grid.addWidget(widget, row_index, grid_column, align | Qt.AlignmentFlag.AlignVCenter)
                following = row[column_index + 1] if column_index + 1 < len(row) else []
                if column_index < len(joins) and joins[column_index] and runs_to_text(following or []):
                    join = _rich(f'<b style="color: {ACCENT};">{_escape(joins[column_index])}</b>', wrap=False)
                    grid.addWidget(join, row_index, grid_column + 1, Qt.AlignmentFlag.AlignCenter)
        if block.get("centered"):
            outer.addStretch(1)
            outer.addWidget(grid_host)
            outer.addStretch(1)
        else:
            grid.setColumnStretch(max(0, (len(aligns) - 1) * 2), 1)
            outer.addWidget(grid_host, 1)
        return holder

    def _folded(self, blocks: list[dict[str, Any]], title: str) -> QWidget:
        """Blocks behind a button: the solutions of an exercise section."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        self._add_blocks(blocks, content_layout)
        content.hide()
        toggle = _button(f"显示{title}", lambda: None, f"先做练习，再展开原书的{title}")
        toggle.setCheckable(True)

        def flip(shown: bool) -> None:
            content.setVisible(shown)
            toggle.setText(("收起" if shown else "显示") + title)

        toggle.toggled.connect(flip)
        row = QHBoxLayout()
        row.addWidget(toggle)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(content)
        return holder

    def _exercise_card(self, section: dict[str, Any], count: int) -> QFrame:
        card = self.center.GlassFrame("standardProblemCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(22, 14, 18, 14)
        label = QLabel(f"本章《文法解题》{count} 题")
        self.center.set_font(label, 13, QFont.Weight.DemiBold)
        label.setStyleSheet(f"color: {ACCENT};")
        row.addWidget(label)
        hint = QLabel(f"读完第 {section.get('chapter')} 章，到题库的“文法”类做对应章节的《文法解题》，附原书详解。")
        hint.setObjectName("pageNote")
        hint.setWordWrap(True)
        row.addWidget(hint, 1)
        chapter = int(section.get("chapter") or 0)
        row.addWidget(_button("去题库练习 →", lambda: self.owner.open_english_grammar_exercises(chapter),
                              "切换到题库的文法类，只显示《文法解题》本章的题目"))
        return card

    def _reading_card(self) -> QFrame:
        card = self.center.GlassFrame("standardProblemCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(22, 14, 18, 14)
        hint = QLabel("这篇附录在旋元佑五本书中相同，“广读材料”页有配套的「广读学英文」视频导读和推荐书单。")
        hint.setObjectName("pageNote")
        hint.setWordWrap(True)
        row.addWidget(hint, 1)
        row.addWidget(_button("去广读材料 →", lambda: self.owner.show_page("广读材料")))
        return card


def build_grammar_method_page(owner: Any) -> QVBoxLayout:
    """The 文法 page (left navigation of the English workspace)."""

    layout = QVBoxLayout()
    layout.setContentsMargins(24, 22, 24, 28)
    layout.setSpacing(12)
    layout.addLayout(owner._english_page_heading(
        PAGE_NAME,
        "《旋元佑文法》全书：总序与前言、单句、复合句、减化子句与附录，按原书顺序学；句子成分按原书标注，"
        "译文可隐藏自测，双击英文单词查词。各章练习是题库“文法”类中的《文法解题》。",
    ))
    if owner.english_service is None:
        layout.addWidget(QLabel("英语学习服务不可用。"))
        return layout
    view = GrammarMethodView(owner)
    owner.grammar_method_view = view
    view.load()
    layout.addWidget(view, 1)
    return layout


__all__ = [
    "AnalysisLine",
    "DiagramView",
    "GrammarMethodView",
    "PAGE_NAME",
    "TimelineView",
    "build_grammar_method_page",
    "part_color",
    "runs_html",
]
