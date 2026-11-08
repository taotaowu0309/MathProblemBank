"""The 阅读方法 page of the English workspace: Part 1 of 《旋元佑英文阅读》.

The left column lists the methodology sections in book order with the
learner's progress; the middle shows one section — the book's lecture text
as rich text and its worked examples as the question bank's own passage and
question cards (answering, favourites, notes and word lookup all work as in
the bank); a worked example can be practised in the bank's split view.  The
right column is the textbook-notes panel of the PDF reader, linked to the
book's bookmarks, so a note written here is the same note the reader shows.
Below a question type's lecture, 《旋元佑英文阅读通》's teaching of the same
type follows, with a button to practise that book's questions of the type.

All data comes from :class:`EnglishLearningService`; this module owns no
database logic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSplitter,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.method_book_qt import ACCENT, READ_GREEN, MethodBookView, _button, _clear, _scroll

PAGE_NAME = "阅读方法"


class ReadingMethodView(MethodBookView):
    """Outline | section | notes for Part 1 of 《旋元佑英文阅读》; a worked
    example can also be practised in the bank's split view (stack page 1)."""

    BOOK_TITLE = "旋元佑英文阅读"
    NAV_TITLE = "方法论目录"
    OWNER_SECTION_ATTRIBUTE = "reading_method_section_key"

    def _add_pages(self) -> None:
        # Page 1: one worked example in the bank's split view.
        practice = QWidget()
        practice.setObjectName("readingMethodHost")
        practice_layout = QVBoxLayout(practice)
        practice_layout.setContentsMargins(0, 0, 0, 0)
        practice_layout.setSpacing(8)
        bar = QHBoxLayout()
        bar.addWidget(_button("← 返回讲解", lambda: self.stack.setCurrentIndex(0)))
        self.practice_title = QLabel()
        self.practice_title.setObjectName("pageNote")
        bar.addWidget(self.practice_title, 1)
        practice_layout.addLayout(bar)
        self.practice_split = QSplitter(Qt.Orientation.Horizontal)
        self.practice_split.setChildrenCollapsible(False)
        self.practice_split.setHandleWidth(12)
        self.practice_split.setStyleSheet("QSplitter::handle { background: transparent; }")
        self.practice_layouts: list[QVBoxLayout] = []
        for _side in ("passage", "questions"):
            host = QWidget()
            side = QVBoxLayout(host)
            side.setContentsMargins(0, 0, 4, 0)
            side.setSpacing(12)
            self.practice_split.addWidget(_scroll(host))
            self.practice_layouts.append(side)
        self.practice_split.setStretchFactor(0, 55)
        self.practice_split.setStretchFactor(1, 45)
        practice_layout.addWidget(self.practice_split, 1)
        self.stack.addWidget(practice)

    def _outline(self) -> dict[str, Any]:
        return self.service.reading_method_outline()

    def _section(self, key: str) -> dict[str, Any]:
        return self.service.reading_method_section(key)

    def _set_read(self, key: str, read: bool) -> dict[str, Any]:
        return self.service.mark_reading_method_section_read(key, read)

    def _fill_tree(self) -> None:
        sections = self.outline_data.get("sections") or []
        read_count = int(self.outline_data.get("read_count") or 0)
        self.progress_label.setText(
            f"已读 {read_count} / {len(sections)} 节"
        )
        self.progress_bar.setRange(0, max(1, len(sections)))
        self.progress_bar.setValue(read_count)
        self.tree.blockSignals(True)
        self.tree.clear()
        parents: dict[int, QTreeWidgetItem] = {}
        current_item: QTreeWidgetItem | None = None
        for section in sections:
            level = int(section["level"])
            number = f"{section['number']}  " if section.get("number") and level >= 3 else ""
            item = QTreeWidgetItem([f"{number}{section['title']}", ""])
            item.setData(0, Qt.ItemDataRole.UserRole, section["key"])
            if level <= 2:
                # Part headings (主流 / 特殊 / 托福 / 雅思) and the 高频 / 次高频 groups.
                font = QFont(self.tree.font())
                font.setWeight(QFont.Weight.DemiBold)
                if level == 1:
                    font.setPointSizeF(font.pointSizeF() * 1.06)
                item.setFont(0, font)
            if section["read"]:
                item.setText(1, "✓")
                item.setForeground(1, QColor(READ_GREEN))
            tip = [f"原书第 {section['source_page']} 页"] if section.get("source_page") else []
            if section["questions"]:
                tip.append(f"例题 {section['questions']} 道")
            tip.append("已读" if section["read"] else "未读")
            for column in (0, 1):
                item.setToolTip(column, " · ".join(tip))
            parent = next((parents[value] for value in range(level - 1, 0, -1) if value in parents), None)
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            parents[level] = item
            for deeper in [value for value in parents if value > level]:
                parents.pop(deeper)
            if section["key"] == self.section_key:
                current_item = item
        self.tree.expandAll()
        if current_item is not None:
            self.tree.setCurrentItem(current_item)
        self.tree.blockSignals(False)

    # -- the section ------------------------------------------------------------

    def _render_section(self) -> None:
        center = self.center
        section = self.section or {}
        # The notes button lives in the section header; keep it out of the old header's deletion.
        self.notes_button.setParent(None)
        _clear(self.section_layout)
        lookup = self.owner._english_question_word_lookup()
        lookup.close()
        header_card = QFrame()
        header_card.setObjectName("readingMethodHeader")
        header_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header = QHBoxLayout(header_card)
        header.setContentsMargins(24, 16, 18, 16)
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        crumbs = [part for part in ("旋元佑英文阅读 · 方法论", str(section.get("parent") or "")) if part]
        breadcrumb = QLabel("　›　".join(crumbs))
        breadcrumb.setObjectName("readingMethodBreadcrumb")
        center.set_font(breadcrumb, 10)
        title_box.addWidget(breadcrumb)
        title = QLabel(str(section.get("title") or ""))
        title.setStyleSheet(f"color: {ACCENT};")
        center.set_font(title, 20, QFont.Weight.DemiBold)
        title_box.addWidget(title)
        facts = []
        if section.get("source_page"):
            facts.append(f"原书第 {int(section['source_page'])} 页")
        questions = sum(len(example["questions"]) for example in section.get("examples") or [])
        if section.get("examples"):
            facts.append(f"范例 {len(section['examples'])} 篇" + (f" · 例题 {questions} 道" if questions else ""))
        if facts:
            detail = QLabel("　·　".join(facts))
            detail.setObjectName("pageNote")
            center.set_font(detail, 10)
            title_box.addWidget(detail)
        header.addLayout(title_box, 1)
        if section.get("source_page"):
            header.addWidget(_button("打开原书", lambda: self.open_book(),
                                     f"在内部阅读器中打开《旋元佑英文阅读》第 {int(section['source_page'])} 页"),
                             0, Qt.AlignmentFlag.AlignVCenter)
        read = bool(section.get("read"))
        self.read_button = _button("已读 ✓" if read else "标为已读", self.toggle_read,
                                   "记下这一节已经读完（学习进度）；再点一次取消")
        self.read_button.setObjectName("secondaryButton")
        self.read_button.setProperty("read", read)
        header.addWidget(self.read_button, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.notes_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.section_layout.addWidget(header_card)

        lecture = center.GlassFrame("standardProblemCard")
        lecture_layout = QVBoxLayout(lecture)
        lecture_layout.setContentsMargins(22, 16, 22, 16)
        lecture_layout.setSpacing(9)
        has_lecture = False
        examples = {example["group_code"]: example for example in section.get("examples") or []}
        example_codes = [example["group_code"] for example in section.get("examples") or []]
        for block in section.get("blocks") or []:
            kind = block.get("kind")
            if kind == "example":
                index = int(block.get("index") or 0)
                code = example_codes[index] if index < len(example_codes) else ""
                if has_lecture:
                    self.section_layout.addWidget(lecture)
                    lookup.attach(lecture)
                    lecture = center.GlassFrame("standardProblemCard")
                    lecture_layout = QVBoxLayout(lecture)
                    lecture_layout.setContentsMargins(22, 16, 22, 16)
                    lecture_layout.setSpacing(9)
                    has_lecture = False
                if code in examples:
                    self._add_example(examples[code], index + 1, len(example_codes))
                continue
            if kind == "overview":
                html_text = self._overview_html()
            else:
                html_text = center.english_reading_block_html(block)
            if html_text:
                lecture_layout.addWidget(center.EnglishQuestionCard._rich_label(html_text, size=14))
                has_lecture = True
        if has_lecture:
            self.section_layout.addWidget(lecture)
            lookup.attach(lecture)
        for supplement in section.get("supplements") or []:
            self._add_supplement(supplement)
        self.section_layout.addLayout(self._step_row(section))
        self.section_layout.addStretch(1)
        self.section_scroll.verticalScrollBar().setValue(0)

    def _add_supplement(self, supplement: dict[str, Any]) -> None:
        """《旋元佑英文阅读通》's teaching of the same question type, below the lecture,
        with its page in that book and its questions of this type in the bank."""
        center = self.center
        lookup = self.owner._english_question_word_lookup()
        row = QHBoxLayout()
        row.setContentsMargins(4, 8, 0, 0)
        label = QLabel(f"《{supplement['book_title']}》补充")
        label.setObjectName("readingMethodExampleTag")
        center.set_font(label, 11, QFont.Weight.DemiBold)
        row.addWidget(label)
        facts = [str(supplement.get("title") or "")]
        if supplement.get("source_page"):
            facts.append(f"原书第 {int(supplement['source_page'])} 页")
        hint = QLabel("　·　".join(part for part in facts if part))
        hint.setObjectName("pageNote")
        center.set_font(hint, 10)
        row.addWidget(hint)
        row.addStretch(1)
        if supplement.get("source_page"):
            row.addWidget(_button("打开原书", lambda: self._open_supplement_book(supplement),
                                  f"在内部阅读器中打开《{supplement['book_title']}》第 {int(supplement['source_page'])} 页"))
        count = int(supplement.get("practice_count") or 0)
        if count and supplement.get("topic"):
            row.addWidget(_button(
                f"练同类题 {count} 道",
                lambda: self.owner.open_english_reading_exercises(supplement["source"], supplement["topic"]),
                f"题库阅读类：《{supplement['book_title']}》中的“{supplement['topic']}”",
            ))
        self.section_layout.addLayout(row)
        card = center.GlassFrame("standardProblemCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 16, 22, 16)
        card_layout.setSpacing(9)
        for block in supplement.get("blocks") or []:
            html_text = center.english_reading_block_html(block)
            if html_text:
                card_layout.addWidget(center.EnglishQuestionCard._rich_label(html_text, size=14))
        self.section_layout.addWidget(card)
        lookup.attach(card)

    def _open_supplement_book(self, supplement: dict[str, Any]) -> None:
        pdf = Path(str(supplement.get("pdf_path") or ""))
        if not pdf.is_file():
            QMessageBox.information(self, "原书不在本机", f"没有找到《{supplement['book_title']}》的 PDF。")
            return
        try:
            preview = self.owner.pdf_preview_for_path(pdf)
            preview.show_pdf_location(pdf, page_index=max(0, int(supplement.get("source_page") or 1) - 1),
                                      anchor_y=0.0, title=f"《{supplement['book_title']}》")
        except Exception as error:
            QMessageBox.critical(self, "内部阅读器打开失败", str(error))

    def _overview_html(self) -> str:
        """The book's overview diagram (TikZ) as an indented list of the sections."""
        accent = self.center.ENGLISH_QUESTION_ACCENT
        lines = []
        for section in self.outline_data.get("sections") or []:
            if section["key"].endswith("overview"):
                continue
            indent = "&nbsp;" * 6 * max(0, int(section["level"]) - 1)
            number = f"{section['number']} " if section.get("number") and section["level"] >= 3 else ""
            weight = "font-weight: 700;" if section["level"] == 1 else ""
            lines.append(f'{indent}<span style="color: {accent}; {weight}">{number}{section["title"]}</span>')
        return "<br>".join(lines)

    def _add_example(self, example: dict[str, Any], number: int, total: int) -> None:
        center = self.center
        lookup = self.owner._english_question_word_lookup()
        row = QHBoxLayout()
        row.setContentsMargins(4, 8, 0, 0)
        label = QLabel("范例" + (f" {number}" if total > 1 else ""))
        label.setObjectName("readingMethodExampleTag")
        center.set_font(label, 11, QFont.Weight.DemiBold)
        row.addWidget(label)
        questions = len(example["questions"])
        hint = QLabel(f"原文与 {questions} 道例题，作答只用来自查" if questions else "原书范例（附题解）")
        hint.setObjectName("pageNote")
        center.set_font(hint, 10)
        row.addWidget(hint)
        row.addStretch(1)
        if example["questions"]:
            row.addWidget(_button("分屏练习", lambda: self._practice(example),
                                  "左边原文、右边例题，和题库里的阅读题一样"))
        self.section_layout.addLayout(row)
        group = example["group"]
        if (group.get("content") or {}).get("blocks") or (group.get("content") or {}).get("explanation"):
            passage = center.EnglishPassageCard(group)
            lookup.attach(passage)
            self.section_layout.addWidget(passage)
        for card in self._question_cards(example):
            self.section_layout.addWidget(card)

    def _question_cards(self, example: dict[str, Any]) -> list[Any]:
        """The example's questions as the bank's cards (answered as a self-check only)."""
        lookup = self.owner._english_question_word_lookup()
        cards = []
        previous_instruction: Any = None
        for row in example["questions"]:
            instruction = (row.get("content") or {}).get("instruction") or None
            card = self.center.EnglishQuestionCard(
                {**row, "group": example["group"]}, self.owner, show_instruction=instruction != previous_instruction,
            )
            previous_instruction = instruction
            lookup.attach(card)
            cards.append(card)
        return cards

    def _practice(self, example: dict[str, Any]) -> None:
        center = self.center
        lookup = self.owner._english_question_word_lookup()
        lookup.close()
        for side in self.practice_layouts:
            _clear(side)
        passage_layout, questions_layout = self.practice_layouts
        passage = center.EnglishPassageCard(example["group"], collapsible=False)
        lookup.attach(passage)
        passage_layout.addWidget(passage)
        passage_layout.addStretch(1)
        for card in self._question_cards(example):
            questions_layout.addWidget(card)
        questions_layout.addStretch(1)
        self.practice_title.setText(str((example["group"].get("content") or {}).get("title") or ""))
        width = max(2, self.practice_split.width())
        self.practice_split.setSizes([width * 55 // 100, width - width * 55 // 100])
        self.stack.setCurrentIndex(1)


def build_reading_method_page(owner: Any) -> QVBoxLayout:
    """The 阅读方法 page (left navigation of the English workspace)."""

    layout = QVBoxLayout()
    layout.setContentsMargins(24, 22, 24, 28)
    layout.setSpacing(12)
    layout.addLayout(owner._english_page_heading(
        PAGE_NAME,
        "《旋元佑英文阅读》第一部分：按原书顺序学各题型的解题方法，范例可作答自查或分屏练习；"
        "各题型下附《旋元佑英文阅读通》对同一题型的讲解。双击英文单词查词。两本书的实战题在“题库”的阅读类。",
    ))
    if owner.english_service is None:
        layout.addWidget(QLabel("英语学习服务不可用。"))
        return layout
    view = ReadingMethodView(owner)
    owner.reading_method_view = view
    view.load()
    layout.addWidget(view, 1)
    return layout


__all__ = ["PAGE_NAME", "ReadingMethodView", "build_reading_method_page"]
