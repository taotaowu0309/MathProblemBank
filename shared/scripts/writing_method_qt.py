"""The 写作方法 page of the English workspace: the lectures of 《旋元佑英文写作》.

The left column lists the book by part, chapter and section with the
learner's progress; the middle shows one section as rich text in the book's
own boxes — wrong (red) / right (blue) / questionable (orange) examples,
solutions, model essays with a folded translation, outline trees,
transition-word tables and sentence patterns.  A chapter's closing exercises
are not shown here: a card links to the question bank's writing category,
filtered to that chapter.  The essay prompts the book sets in the running
text (考古题, 模拟题) are lecture examples and stay on this page.

All data comes from :class:`EnglishLearningService`; this module owns no
database logic.
"""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.method_book_qt import ACCENT, READ_GREEN, MethodBookView, _button, _clear

PAGE_NAME = "写作方法"
MUTED = "#5b6b7a"
# The book's box colours: left bar and a pale fill.
BOX_STYLES = {
    "wrong": ("#d64541", "rgba(214, 69, 65, 20)"),
    "correct": ("#1f6fd1", "rgba(31, 111, 209, 18)"),
    "question": ("#e08a1e", "rgba(224, 138, 30, 22)"),
    "solution": ("#2e8b57", "rgba(46, 139, 87, 18)"),
    "essay": ("#1f6fd1", "rgba(31, 111, 209, 14)"),
}
LEAF_KINDS = {
    "para", "heading", "title", "label", "translation", "tag", "phrase", "plain_example", "speaker",
    "source_line", "reading", "outline", "table", "patterns", "rule",
}


def _center() -> Any:
    from shared.scripts import problem_bank_center_qt

    return problem_bank_center_qt


def _escape(text: Any) -> str:
    return _center()._html_escape(str(text or ""))


def _runs(runs: Any) -> str:
    return _center().english_question_runs_html(runs)


def block_html(block: dict[str, Any]) -> str:
    """One leaf block of the book as Qt rich text."""

    kind = block.get("kind")
    if kind == "para":
        text = _runs(block.get("runs"))
        return f"<p style=\"margin: 0 0 8px 0;\">{'<b>' + text + '</b>' if block.get('bold') else text}</p>"
    if kind == "heading":
        # Sized in points: a named size ("large") is relative to the application font, not the 14 pt text.
        size = _center().ui_point_size(16)
        return (f'<p style="margin: 12px 0 6px 0;"><b style="color: {ACCENT}; font-size: {size:.1f}pt;">'
                f"{_escape(block.get('text'))}</b></p>")
    if kind == "title":
        return f'<p align="center" style="margin: 4px 0 8px 0;"><b>{_escape(block.get("text"))}</b></p>'
    if kind == "label":
        return ('<p style="margin: 10px 0 6px 0;"><span style="background-color: #5f6670; color: white; '
                f'font-family: sans-serif; font-weight: 700;">&nbsp;{_escape(block.get("text"))}&nbsp;</span></p>')
    if kind == "translation":
        return f'<p style="margin: 2px 0 8px 0; color: {MUTED};">{_runs(block.get("runs"))}</p>'
    if kind == "tag":
        return ('<p style="margin: 0 0 4px 0;"><span style="background-color: #eef1f4; font-family: sans-serif;">'
                f'&nbsp;{_escape(block.get("label"))}&nbsp;</span>&nbsp;&nbsp;{_runs(block.get("runs"))}</p>')
    if kind == "phrase":
        return f'<p style="margin: 10px 0 4px 0;"><b>{_runs(block.get("runs"))}</b></p>'
    if kind == "plain_example":
        return (f'<p style="margin: 0 0 6px 16px;"><span style="color: {ACCENT};">▪</span>&nbsp;'
                f'{_runs(block.get("en"))}<br><span style="color: {MUTED};">{_runs(block.get("zh"))}</span></p>')
    if kind == "speaker":
        return f'<p style="margin: 0 0 6px 0;"><b>{_runs(block.get("speaker"))}</b>&nbsp;&nbsp;{_runs(block.get("runs"))}</p>'
    if kind == "source_line":
        return f'<p align="right" style="margin: 0 0 6px 0; font-size: small;">{_runs(block.get("runs"))}</p>'
    if kind == "reading":
        return f'<p style="margin: 6px 0 8px 24px;">{_runs(block.get("runs"))}</p>'
    if kind == "rule":
        return f'<p style="margin: 4px 0; color: #9fb6cc;">{"┈" * 24}</p>'
    if kind == "outline":
        lines = []
        for line in block.get("lines") or []:
            if "heading" in line:
                lines.append(f'<p style="margin: 8px 0 3px 0;">{_runs(line["heading"])}</p>')
                continue
            indent = int(float(line.get("indent") or 0) * 15)
            mark = _runs(line.get("mark"))
            lines.append(f'<p style="margin: 0 0 3px {indent}px;"><span style="color: {ACCENT};">{mark}</span>'
                         f'&nbsp;{_runs(line.get("runs"))}</p>')
        return "".join(lines)
    if kind == "table":
        fill = ' bgcolor="#dcebf8"' if block.get("style") == "term" else ""
        rows = "".join(
            "<tr>" + "".join(f'<td{fill} style="padding: 4px 10px;">{_runs(cell)}</td>' for cell in row) + "</tr>"
            for row in block.get("rows") or []
        )
        return f'<table cellspacing="2" cellpadding="5" style="margin: 6px 0 10px 0;">{rows}</table>'
    if kind == "patterns":
        groups = []
        for group in block.get("groups") or []:
            groups.append("".join(
                f'<p style="margin: 0 0 4px 18px;"><span style="color: {ACCENT};">■</span>&nbsp;{_runs(item)}</p>'
                for item in group
            ))
        return '<p style="margin: 0 0 8px 0;"></p>'.join(groups)
    return ""


def _initial(block: dict[str, Any]) -> str:
    text = "".join(str(run.get("t") or "") for run in block.get("runs") or [])
    match = re.search(r"[A-Za-z]", text)
    return match.group(0).upper() if match else ""


class WritingMethodView(MethodBookView):
    BOOK_TITLE = "旋元佑英文写作"
    NAV_TITLE = "写作方法目录"
    OWNER_SECTION_ATTRIBUTE = "writing_method_section_key"

    def _outline(self) -> dict[str, Any]:
        return self.service.writing_method_outline()

    def _section(self, key: str) -> dict[str, Any]:
        return self.service.writing_method_section(key)

    def _set_read(self, key: str, read: bool) -> dict[str, Any]:
        return self.service.mark_writing_method_section_read(key, read)

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
            larger = QFont(bold)
            larger.setPointSizeF(larger.pointSizeF() * 1.06)
            item.setFont(0, larger)
            self.tree.addTopLevelItem(item)
            part_items[part["part"]] = item
        current_item: QTreeWidgetItem | None = None
        for chapter in data.get("chapters") or []:
            keys = chapter["section_keys"]
            done = sum(1 for key in keys if by_key.get(key, {}).get("read"))
            number = f"{chapter['chapter']}  " if chapter["part"] < 4 else ""
            chapter_item = QTreeWidgetItem([f"{number}{chapter['title']}", "✓" if keys and done == len(keys) else ""])
            chapter_item.setData(0, Qt.ItemDataRole.UserRole, keys[0] if keys else "")
            chapter_item.setFont(0, bold)
            chapter_item.setForeground(1, QColor(READ_GREEN))
            tip = [f"已读 {done} / {len(keys)} 节"]
            if chapter["question_count"]:
                tip.append(f"写作练习 {chapter['question_count']} 题（在题库）")
            if chapter.get("source_page"):
                tip.append(f"原书第 {chapter['source_page']} 页")
            for column in (0, 1):
                chapter_item.setToolTip(column, " · ".join(tip))
            part_items[chapter["part"]].addChild(chapter_item)
            if len(keys) == 1 and by_key[keys[0]]["intro"]:
                # A chapter of one section (相关字组): the chapter item is the section.
                chapter_item.setData(0, Qt.ItemDataRole.UserRole, keys[0])
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
        """Show the sections of the current chapter only; the book has 180-odd."""
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
        part = " ".join(item for item in (section.get("part_title"), section.get("part_name")) if item)
        breadcrumb = QLabel("　›　".join(item for item in (self.BOOK_TITLE, part, section.get("chapter_title")) if item))
        breadcrumb.setObjectName("readingMethodBreadcrumb")
        center.set_font(breadcrumb, 10)
        title_box.addWidget(breadcrumb)
        title_text = str(section.get("full_title") or section.get("title") or "")
        title = QLabel(title_text)
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
        if section.get("source_page"):
            header.addWidget(_button("打开原书", lambda: self.open_book(),
                                     f"在内部阅读器中打开《{self.BOOK_TITLE}》第 {int(section['source_page'])} 页"),
                             0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._read_button(), 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.notes_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.section_layout.addWidget(header_card)

        blocks = section.get("blocks") or []
        self.phrase_labels: dict[str, QLabel] = {}
        phrases = [block for block in blocks if block.get("kind") == "phrase"]
        if len(phrases) > 30:
            self.section_layout.addWidget(self._phrase_index(phrases))
        lecture = center.GlassFrame("standardProblemCard")
        lecture_layout = QVBoxLayout(lecture)
        lecture_layout.setContentsMargins(22, 16, 22, 16)
        lecture_layout.setSpacing(9)
        for block in blocks:
            if block.get("kind") == "exercise_link":
                if lecture_layout.count():
                    self.section_layout.addWidget(lecture)
                    lookup.attach(lecture)
                    lecture = center.GlassFrame("standardProblemCard")
                    lecture_layout = QVBoxLayout(lecture)
                    lecture_layout.setContentsMargins(22, 16, 22, 16)
                    lecture_layout.setSpacing(9)
                self.section_layout.addWidget(self._exercise_card(block))
                continue
            self._add_blocks([block], lecture_layout, phrase_index=len(phrases) > 30)
        if lecture_layout.count():
            self.section_layout.addWidget(lecture)
            lookup.attach(lecture)
        self.section_layout.addLayout(self._step_row(section))
        self.section_layout.addStretch(1)
        self.section_scroll.verticalScrollBar().setValue(0)

    def _add_blocks(self, blocks: list[dict[str, Any]], layout: QVBoxLayout, *, phrase_index: bool = False) -> None:
        """Leaf blocks are merged into rich-text labels; boxes become framed panels."""
        pending: list[str] = []

        def flush() -> None:
            if pending:
                layout.addWidget(self.center.EnglishQuestionCard._rich_label("".join(pending), size=14))
                pending.clear()

        for block in blocks:
            kind = block.get("kind")
            if kind in LEAF_KINDS:
                if kind == "phrase" and phrase_index:
                    flush()
                    label = self.center.EnglishQuestionCard._rich_label(block_html(block), size=14)
                    layout.addWidget(label)
                    self.phrase_labels.setdefault(_initial(block), label)
                    continue
                pending.append(block_html(block))
                continue
            flush()
            if kind == "citation":
                inner = QWidget()
                inner_layout = QVBoxLayout(inner)
                inner_layout.setContentsMargins(26, 2, 0, 2)
                inner_layout.setSpacing(6)
                self._add_blocks(block.get("blocks") or [], inner_layout)
                layout.addWidget(inner)
            elif kind in {"example", "solution", "prompt"}:
                layout.addWidget(self._box(block.get("style") or kind, block.get("blocks") or []))
            elif kind == "essay":
                layout.addWidget(self._essay(block))
        flush()

    def _box(self, style: str, blocks: list[dict[str, Any]]) -> QFrame:
        frame = QFrame()
        frame.setObjectName("writingBox")
        if style == "prompt":
            frame.setStyleSheet("QFrame#writingBox { background: rgba(255,255,255,200); "
                                "border: 1px solid rgba(60,70,80,120); border-radius: 2px; }")
        else:
            bar, fill = BOX_STYLES[style]
            frame.setStyleSheet(f"QFrame#writingBox {{ background: {fill}; border: 0; "
                                f"border-left: 5px solid {bar}; border-radius: 6px; }}")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(6)
        self._add_blocks(blocks, inner)
        return frame

    def _essay(self, block: dict[str, Any]) -> QFrame:
        """A model essay; its translation is folded until asked for."""
        frame = self._box("essay", ([{"kind": "para", "runs": block["source"], "bold": True}] if block.get("source")
                                    else []) + list(block.get("blocks") or []))
        translation = list(block.get("translation") or [])
        if not translation:
            return frame
        layout = frame.layout()
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 4, 0, 0)
        holder_layout.setSpacing(6)
        heading = [{"kind": "rule"}] + ([{"kind": "para", "runs": [{"t": block["translation_title"]}], "bold": True}]
                                        if block.get("translation_title") else [])
        self._add_blocks(heading + translation, holder_layout)
        holder.hide()
        toggle = _button("显示译文", lambda: None, "展开或收起原书的中文译文")
        toggle.setCheckable(True)

        def flip(shown: bool) -> None:
            holder.setVisible(shown)
            toggle.setText("收起译文" if shown else "显示译文")

        toggle.toggled.connect(flip)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(toggle)
        layout.addLayout(row)
        layout.addWidget(holder)
        return frame

    def _exercise_card(self, block: dict[str, Any]) -> QFrame:
        card = self.center.GlassFrame("standardProblemCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(22, 14, 18, 14)
        count = int(block.get("count") or 0)
        label = QLabel(f"本章写作练习 {count} 题" if count else "本章写作练习")
        self.center.set_font(label, 13, QFont.Weight.DemiBold)
        label.setStyleSheet(f"color: {ACCENT};")
        row.addWidget(label)
        hint = QLabel("练习和原书答案在题库的“写作”类：先作答，再对照原书答案或范文。")
        hint.setObjectName("pageNote")
        hint.setWordWrap(True)
        row.addWidget(hint, 1)
        match = re.fullmatch(r"XYWR-(\d)-(\d{2})", str(block.get("chapter_key") or ""))
        number = int(match.group(1)) * 100 + int(match.group(2)) if match else 0
        row.addWidget(_button("去题库练习 →", lambda: self.owner.open_english_writing_exercises(number),
                              "切换到题库的写作类，只显示本章的写作练习"))
        return card

    def _phrase_index(self, phrases: list[dict[str, Any]]) -> QLabel:
        letters = sorted({_initial(block) for block in phrases} - {""})
        links = "&nbsp;&nbsp;".join(f'<a href="{letter}" style="color: {ACCENT}; text-decoration: none;">'
                                    f"<b>{letter}</b></a>" for letter in letters)
        label = QLabel(f"按首字母跳转：&nbsp;&nbsp;{links}")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setObjectName("pageNote")
        self.center.set_font(label, 12)
        label.linkActivated.connect(self._jump_to_phrase)
        return label

    def _jump_to_phrase(self, letter: str) -> None:
        label = getattr(self, "phrase_labels", {}).get(letter)
        if label is not None:
            bar = self.section_scroll.verticalScrollBar()
            bar.setValue(label.mapTo(self.section_scroll.widget(), label.rect().topLeft()).y() - 12)


def build_writing_method_page(owner: Any) -> QVBoxLayout:
    """The 写作方法 page (left navigation of the English workspace)."""

    layout = QVBoxLayout()
    layout.setContentsMargins(24, 22, 24, 28)
    layout.setSpacing(12)
    layout.addLayout(owner._english_page_heading(
        PAGE_NAME,
        "《旋元佑英文写作》的讲解：错误分析、章法结构、句法修辞与附录，按原书顺序学；"
        "范文译文可展开，双击英文单词查词。各章的写作练习在“题库”的写作类。",
    ))
    if owner.english_service is None:
        layout.addWidget(QLabel("英语学习服务不可用。"))
        return layout
    view = WritingMethodView(owner)
    owner.writing_method_view = view
    view.load()
    layout.addWidget(view, 1)
    return layout


__all__ = ["PAGE_NAME", "WritingMethodView", "block_html", "build_writing_method_page"]
