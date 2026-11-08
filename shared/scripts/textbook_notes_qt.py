"""Qt side panel showing LaTeX notes linked to the bookmarks of the open PDF.

The panel lives in the Qt PDF reader's splitter (split view) and can float as
a small tool window above the reader.  :class:`QuestionNotesDialog` shows the
notes of one question-bank question with the same editor and PDF view.  All
storage, linking and compilation go through :class:`TextbookNotesService`;
this module only presents them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QBuffer, QIODevice, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from shiboken6 import isValid
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.textbook_notes_service import BOOKMARK_SEPARATOR, TextbookNotesService

_SERVICE: TextbookNotesService | None = None


def textbook_notes_service() -> TextbookNotesService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = TextbookNotesService()
    return _SERVICE


class _TaskSignals(QObject):
    finished = Signal(object)


class _Task(QRunnable):
    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = _TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        try:
            self.signals.finished.emit(result)
        except RuntimeError:
            # The reader (and this panel) was closed while the task ran.
            pass


def _deepest_bookmark(outline: list[dict[str, Any]], page_number: int) -> dict[str, Any] | None:
    covering = [item for item in outline if item["page_start"] <= page_number <= item["page_end"]]
    if not covering:
        return None
    return min(covering, key=lambda item: (item["page_end"] - item["page_start"], -item["level"]))


class NoteEditorDialog(QDialog):
    """Create a note for chosen bookmarks, link an existing note, or edit TeX."""

    def __init__(
        self,
        parent: QWidget,
        *,
        outline: list[dict[str, Any]] | None = None,
        preselected: set[int] | None = None,
        existing_notes: list[dict[str, Any]] | None = None,
        title: str = "",
        latex: str = "",
        edit_mode: bool = False,
        copy_prompt: Callable[[list[int]], None] | None = None,
        show_example: Callable[[], None] | None = None,
        target_label: str = "",
    ) -> None:
        super().__init__(parent)
        # With target_label the note is for one question: no bookmark tree.
        self.question_mode = bool(target_label) and not edit_mode
        self.setWindowTitle(
            "编辑笔记" if edit_mode else ("为题目新建 / 链接笔记" if self.question_mode else "为书签新建 / 链接笔记")
        )
        self.resize(1080 if not edit_mode and not self.question_mode else 820, 700)
        self.edit_mode = edit_mode
        root = QHBoxLayout(self)
        self.tree: QTreeWidget | None = None
        if not edit_mode and not self.question_mode:
            left = QVBoxLayout()
            left.addWidget(QLabel("勾选要链接的书签（一章或一节，可多选）"))
            self.tree = QTreeWidget()
            self.tree.setHeaderHidden(True)
            parents: dict[int, QTreeWidgetItem] = {}
            first_checked: QTreeWidgetItem | None = None
            for entry in outline or []:
                item = QTreeWidgetItem([f"{entry['title']}   p.{entry['page_start']}–{entry['page_end']}"])
                item.setData(0, Qt.ItemDataRole.UserRole, int(entry["index"]))
                item.setToolTip(0, entry["path_title"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = int(entry["index"]) in (preselected or set())
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                parent = parents.get(int(entry["level"]) - 1)
                if parent is not None:
                    parent.addChild(item)
                else:
                    self.tree.addTopLevelItem(item)
                parents[int(entry["level"])] = item
                for level in [key for key in parents if key > int(entry["level"])]:
                    del parents[level]
                if checked and first_checked is None:
                    first_checked = item
            self.tree.collapseAll()
            if first_checked is not None:
                self.tree.scrollToItem(first_checked)
                parent_item = first_checked.parent()
                while parent_item is not None:
                    parent_item.setExpanded(True)
                    parent_item = parent_item.parent()
            left.addWidget(self.tree, 1)
            left_host = QWidget()
            left_host.setLayout(left)
            left_host.setMinimumWidth(340)
            root.addWidget(left_host, 2)

        right = QVBoxLayout()
        if self.question_mode:
            target = QLabel(f"链接到：{target_label}")
            target.setWordWrap(True)
            right.addWidget(target)
        self.new_radio = QRadioButton("新建笔记")
        self.link_radio = QRadioButton("链接到已有笔记")
        self.existing_combo = QComboBox()
        if not edit_mode:
            group = QButtonGroup(self)
            group.addButton(self.new_radio)
            group.addButton(self.link_radio)
            mode_row = QHBoxLayout()
            mode_row.addWidget(self.new_radio)
            mode_row.addWidget(self.link_radio)
            mode_row.addStretch(1)
            right.addLayout(mode_row)
            for note in existing_notes or []:
                self.existing_combo.addItem(f"{note['note_code']}  {note['title']}", int(note["id"]))
            self.link_radio.setEnabled(bool(existing_notes))
            right.addWidget(self.existing_combo)
        self.new_radio.setChecked(True)
        form = QFormLayout()
        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText(
            "例如：deny 后面不加 of" if self.question_mode else "例如：基本句型 · 五大句型与补语"
        )
        form.addRow("标题", self.title_edit)
        right.addLayout(form)
        hint = QLabel(
            "先点“生成 ChatGPT Work 任务”，写下你的想法，把复制的一行任务发给 ChatGPT Work"
            "（题目、答案和原书解析已经在任务里）；它写好后再打开本窗口，草稿会自动填入。排版固定，只写正文。"
            if self.question_mode else
            "先点“生成 ChatGPT Work 任务”，写下这次想记的内容，把复制的一行任务发给 ChatGPT Work"
            "（这几页原文已经截成 PDF 放进任务）；它写好后再打开本窗口，草稿会自动填入。排版固定，只写正文。"
        )
        hint.setWordWrap(True)
        right.addWidget(hint)
        guide_row = QHBoxLayout()
        if copy_prompt is not None:
            prompt_button = QPushButton("生成 ChatGPT Work 任务")
            prompt_button.clicked.connect(lambda: copy_prompt(self.checked_indices()))
            guide_row.addWidget(prompt_button)
        if show_example is not None:
            example_button = QPushButton("查看示例笔记")
            example_button.clicked.connect(show_example)
            guide_row.addWidget(example_button)
        guide_row.addStretch(1)
        right.addLayout(guide_row)
        self.latex_edit = QTextEdit()
        self.latex_edit.setAcceptRichText(False)
        self.latex_edit.setPlainText(latex)
        self.latex_edit.setPlaceholderText("\\section{...}\n\n\\eg{English sentence}{中文}")
        right.addWidget(self.latex_edit, 1)
        bottom = QHBoxLayout()
        self.load_button = QPushButton("从 .tex 文件载入…")
        self.load_button.clicked.connect(self._load_file)
        bottom.addWidget(self.load_button)
        bottom.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并编译")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        right.addLayout(bottom)
        right_host = QWidget()
        right_host.setLayout(right)
        root.addWidget(right_host, 3)
        self.new_radio.toggled.connect(self._sync_mode)
        self._sync_mode()

    def _sync_mode(self) -> None:
        creating = self.new_radio.isChecked()
        self.existing_combo.setEnabled(not creating)
        for widget in (self.title_edit, self.latex_edit, self.load_button):
            widget.setEnabled(creating)

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 LaTeX 笔记", "", "LaTeX (*.tex);;All files (*)")
        if not path:
            return
        try:
            self.latex_edit.setPlainText(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as error:
            QMessageBox.warning(self, "读取失败", str(error))
            return
        if not self.title_edit.text().strip():
            self.title_edit.setText(Path(path).stem)

    def checked_indices(self) -> list[int]:
        if self.tree is None:
            return []
        result: list[int] = []
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value() is not None:
            item = iterator.value()
            if item.checkState(0) == Qt.CheckState.Checked:
                result.append(int(item.data(0, Qt.ItemDataRole.UserRole)))
            iterator += 1
        return result

    def linking_existing(self) -> bool:
        return not self.edit_mode and self.link_radio.isChecked()

    def _accept(self) -> None:
        if not self.edit_mode and not self.question_mode and not self.checked_indices():
            QMessageBox.information(self, "请选择书签", "请至少勾选一个书签。")
            return
        if not self.linking_existing() and (
            not self.title_edit.text().strip() or not self.latex_edit.toPlainText().strip()
        ):
            QMessageBox.information(self, "内容不完整", "请填写标题，并等 ChatGPT Work 写好草稿（或粘贴 LaTeX 内容）。")
            return
        self.accept()


class _FloatingNotesWindow(QWidget):
    def __init__(self, panel: "TextbookNotesPanel") -> None:
        super().__init__(panel.reader, Qt.WindowType.Tool)
        self.panel = panel
        self.setWindowTitle("教材笔记")
        self.resize(560, 760)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.panel.dock(show=False)
        super().closeEvent(event)


class TextbookNotesPanel(QWidget):
    """Notes for the bookmarks around the reader's current page."""

    def __init__(self, reader: Any, splitter: QSplitter) -> None:
        super().__init__()
        self.reader = reader
        self.splitter = splitter
        self.service = textbook_notes_service()
        self._notes: list[dict[str, Any]] = []
        self._outline_cache: tuple[str, list[dict[str, Any]]] | None = None
        self._last_key: tuple[Any, ...] | None = None
        self._loaded_note_pdf: tuple[str, float] | None = None
        self._floating: _FloatingNotesWindow | None = None
        self._tasks: set[_Task] = set()
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)
        top = QHBoxLayout()
        heading = QLabel("教材笔记")
        heading.setObjectName("sectionTitleSmall")
        top.addWidget(heading)
        top.addStretch(1)
        self.whole_book_check = QCheckBox("整本书")
        self.whole_book_check.setToolTip("显示这本书的全部笔记，而不只是当前页所在书签的笔记")
        self.whole_book_check.toggled.connect(lambda _checked: self.refresh(force=True))
        top.addWidget(self.whole_book_check)
        self.float_button = QPushButton("小窗")
        self.float_button.setToolTip("把笔记弹出为浮动小窗；再次点击停靠回分屏")
        self.float_button.clicked.connect(self.toggle_floating)
        top.addWidget(self.float_button)
        layout.addLayout(top)

        self.bookmark_label = QLabel("")
        self.bookmark_label.setObjectName("cardNote")
        self.bookmark_label.setWordWrap(True)
        layout.addWidget(self.bookmark_label)

        self.note_list = QListWidget()
        self.note_list.setMaximumHeight(96)
        self.note_list.currentItemChanged.connect(lambda _current, _previous: self._show_selected_note())
        layout.addWidget(self.note_list)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.new_button = QPushButton("新建 / 链接…")
        self.new_button.clicked.connect(lambda: self.open_link_dialog())
        self.edit_button = QPushButton("编辑")
        self.edit_button.clicked.connect(self.edit_selected_note)
        self.compile_button = QPushButton("编译")
        self.compile_button.clicked.connect(self.compile_selected_note)
        self.more_button = QPushButton("更多")
        self.more_button.clicked.connect(self._show_more_menu)
        self.prompt_button = QPushButton("任务")
        self.prompt_button.setToolTip("生成 ChatGPT Work 笔记任务：自动填入书名、书签、页码并截取这几页原文，复制一行任务")
        self.prompt_button.clicked.connect(lambda: self.copy_prompt())
        self.example_button = QPushButton("示例")
        self.example_button.setToolTip("查看示例笔记（固定排版的标准样子）")
        self.example_button.clicked.connect(self.show_example)
        for button in (
            self.new_button, self.edit_button, self.compile_button, self.more_button,
            self.prompt_button, self.example_button,
        ):
            button.setObjectName("secondaryButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        for button in (self.whole_book_check, self.float_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.float_button.setObjectName("secondaryButton")
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setObjectName("cardNote")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.note_document = QPdfDocument(self)
        self._note_pdf_buffer = QBuffer(self)
        self.note_view = QPdfView()
        self.note_view.setDocument(self.note_document)
        self.note_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.note_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        layout.addWidget(self.note_view, 1)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self.refresh)

    # -- state ------------------------------------------------------------------

    def is_floating(self) -> bool:
        return self._floating is not None

    def is_shown(self) -> bool:
        return self.is_floating() or self.isVisible()

    def schedule_refresh(self) -> None:
        if self.is_shown():
            self._refresh_timer.start()

    def document_changed(self) -> None:
        self._outline_cache = None
        self._last_key = None
        self.schedule_refresh()

    def _pdf_path(self) -> Path | None:
        path = getattr(self.reader, "pdf_path", None)
        return Path(path) if path else None

    def _page_number(self) -> int:
        return int(self.reader.current_page()) + 1

    def outline(self) -> list[dict[str, Any]]:
        pdf_path = self._pdf_path()
        if pdf_path is None:
            return []
        if self._outline_cache is None or self._outline_cache[0] != str(pdf_path):
            try:
                outline = self.service.pdf_outline(pdf_path)
            except Exception:
                outline = []
            self._outline_cache = (str(pdf_path), outline)
        return self._outline_cache[1]

    def book_title(self) -> str:
        title = str(self.reader.windowTitle() or "").strip()
        pdf_path = self._pdf_path()
        return title if title and title != "PDF 定位" else (pdf_path.stem if pdf_path else "")

    def copy_prompt(self, indices: list[int] | None = None) -> None:
        outline = self.outline()
        if indices:
            chosen = [item for item in outline if item["index"] in set(indices)]
        else:
            current = _deepest_bookmark(outline, self._page_number())
            chosen = [current] if current else []
        pages = ""
        if chosen:
            start = min(item["page_start"] for item in chosen)
            end = max(item["page_end"] for item in chosen)
            pages = f"{start}–{end}" if end > start else str(start)
        ideas, accepted = QInputDialog.getMultiLineText(
            self.window(), "我的想法",
            "这次想记什么？想弄清的问题、自己的理解、容易混的地方，可以很零碎；\n"
            "ChatGPT Work 会按这些想法组织笔记。留空则在 ChatGPT Work 里再补。",
        )
        if not accepted:
            return
        pdf_path = self._pdf_path()
        if pdf_path is None:
            return
        try:
            task = self.service.prepare_textbook_note_task(
                pdf_path,
                book=self.book_title(),
                bookmarks=[item["path_title"] for item in chosen],
                page_start=min((item["page_start"] for item in chosen), default=0),
                page_end=max((item["page_end"] for item in chosen), default=0),
                ideas=ideas,
            )
        except Exception as error:
            QMessageBox.warning(self, "生成任务失败", str(error))
            return
        QApplication.clipboard().setText(task["task_line"])
        target = "、".join(item["title"] for item in chosen) or "（未选书签）"
        self.status_label.setText(
            f"已复制 ChatGPT Work 任务（{target}，第 {pages or '?'} 页）。"
            "它写好后点“新建 / 链接…”，草稿会自动填入。"
        )

    def show_example(self) -> None:
        self.note_list.blockSignals(True)
        self.note_list.clearSelection()
        self.note_list.setCurrentRow(-1)
        self.note_list.blockSignals(False)
        self.status_label.setText("正在准备示例笔记…")
        task = _Task(lambda: {"ok": True, "path": str(self.service.example_pdf())})
        task.setAutoDelete(False)
        self._tasks.add(task)

        def finished(result: Any) -> None:
            if not isValid(self):
                return
            self._tasks.discard(task)
            if not (isinstance(result, dict) and result.get("ok")):
                error = result.get("error") if isinstance(result, dict) else result
                self.status_label.setText("示例笔记编译失败。")
                QMessageBox.warning(self, "示例笔记编译失败", str(error))
                return
            self._load_note_pdf(Path(result["path"]))
            self._loaded_note_pdf = None
            self.edit_button.setEnabled(False)
            self.compile_button.setEnabled(False)
            self.status_label.setText("正在显示【示例笔记】：所有笔记都使用这一固定排版，ChatGPT Work 任务里附有这份示例。")

        task.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(task)

    def selected_note(self) -> dict[str, Any] | None:
        item = self.note_list.currentItem()
        if item is None:
            return None
        note_id = int(item.data(Qt.ItemDataRole.UserRole))
        return next((note for note in self._notes if int(note["id"]) == note_id), None)

    def refresh(self, force: bool = False) -> None:
        pdf_path = self._pdf_path()
        if pdf_path is None or not self.is_shown():
            return
        page_number = self._page_number()
        whole_book = self.whole_book_check.isChecked()
        current = _deepest_bookmark(self.outline(), page_number)
        self.bookmark_label.setText(
            f"第 {page_number} 页 · {current['path_title']}" if current else f"第 {page_number} 页 · 不在任何书签内"
        )
        key = (str(pdf_path), None if whole_book else page_number)
        if not force and key == self._last_key:
            return
        self._last_key = key
        try:
            notes = self.service.notes_for_document(pdf_path, page_number=None if whole_book else page_number)
        except Exception as error:
            notes = []
            self.status_label.setText(f"读取笔记失败：{error}")
        previous = self.selected_note()
        self._notes = notes
        self.note_list.blockSignals(True)
        self.note_list.clear()
        for note in notes:
            bookmarks = [link["bookmark_title"].split(BOOKMARK_SEPARATOR)[-1] for link in note["links"]]
            mark = "" if note["pdf_current"] else ("  ⚠ 编译失败" if note["compile_status"] == "failed" else "  · 待编译")
            item = QListWidgetItem(f"{note['title']}  —  {'、'.join(dict.fromkeys(bookmarks))}{mark}")
            item.setData(Qt.ItemDataRole.UserRole, int(note["id"]))
            item.setToolTip("\n".join(link["bookmark_title"] for link in note["links"]))
            self.note_list.addItem(item)
        self.note_list.blockSignals(False)
        if notes:
            ids = [int(note["id"]) for note in notes]
            row = ids.index(int(previous["id"])) if previous and int(previous["id"]) in ids else 0
            self.note_list.setCurrentRow(row)
        self._show_selected_note()

    def _load_note_pdf(self, pdf_file: Path) -> None:
        # Keep the preview in memory: an open QFile can prevent atomic PDF
        # replacement on Windows while the user recompiles the same note.
        self.note_document.close()
        self._note_pdf_buffer.close()
        self._note_pdf_buffer.setData(pdf_file.read_bytes())
        self._note_pdf_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.note_document.load(self._note_pdf_buffer)

    def _show_selected_note(self) -> None:
        note = self.selected_note()
        has_note = note is not None
        self.edit_button.setEnabled(has_note)
        self.compile_button.setEnabled(has_note)
        if note is None:
            self.note_document.close()
            self._loaded_note_pdf = None
            if not self.status_label.text().startswith("读取笔记失败"):
                self.status_label.setText(
                    "这一页所在的书签还没有笔记。先点“任务”交给 ChatGPT Work，它写好后点“新建 / 链接…”；"
                    "“示例”可查看标准排版。"
                    if not self.whole_book_check.isChecked() else "这本书还没有笔记。"
                )
            return
        pdf_file = Path(note["pdf_file"])
        if note.get("tex_consistent") is False:
            self.status_label.setText("正文与保存记录不一致，请核对正文或恢复版本；下方 PDF 不是当前版本。")
        elif note["pdf_current"]:
            self.status_label.setText(f"{note['note_code']} · 链接 {len(note['links'])} 个书签")
        elif note["compile_status"] == "failed":
            self.status_label.setText("编译失败（下方为上一次成功的 PDF）：" + str(note["compile_error"]).splitlines()[0])
            self.status_label.setToolTip(str(note["compile_error"]))
        else:
            self.status_label.setText("TeX 已更新，PDF 尚未重新编译。")
        if pdf_file.is_file():
            stamp = (str(pdf_file), pdf_file.stat().st_mtime)
            if stamp != self._loaded_note_pdf:
                self._load_note_pdf(pdf_file)
                self._loaded_note_pdf = stamp
        else:
            self.note_document.close()
            self._loaded_note_pdf = None

    # -- actions ----------------------------------------------------------------

    def open_link_dialog(self, preselected: set[int] | None = None) -> None:
        pdf_path = self._pdf_path()
        if pdf_path is None:
            return
        outline = self.outline()
        if not outline:
            QMessageBox.information(self, "没有书签", "这份 PDF 没有书签目录，无法按章节链接笔记。")
            return
        if preselected is None:
            current = _deepest_bookmark(outline, self._page_number())
            preselected = {int(current["index"])} if current else set()
        first = next((item for item in outline if item["index"] in preselected), None)
        task = self.service.textbook_note_task(pdf_path)
        dialog = NoteEditorDialog(
            self.window(), outline=outline, preselected=preselected,
            existing_notes=self.service.list_notes(limit=500), title=first["title"] if first else "",
            latex=str(task["draft_source"]),
            copy_prompt=self.copy_prompt, show_example=self.show_example,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        links = [
            {"pdf_path": str(pdf_path), "outline_index": index, "book_title": self.book_title()}
            for index in dialog.checked_indices()
        ]
        try:
            if dialog.linking_existing():
                note = self.service.link_note(int(dialog.existing_combo.currentData()), links)
            else:
                note = self.service.create_note(
                    dialog.title_edit.text(), dialog.latex_edit.toPlainText(),
                    links=links, source_kind="manual", compile_pdf=False,
                )
                if task["draft_exists"]:
                    self.service.archive_note_draft("textbook", task["task_key"])
        except Exception as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.refresh(force=True)
        if not dialog.linking_existing():
            self.compile_note(int(note["id"]))

    def edit_selected_note(self) -> None:
        note = self.selected_note()
        if note is None:
            return
        try:
            content = self.service.read_note_tex(int(note["id"]))["content"]
        except Exception as error:
            QMessageBox.warning(self, "读取失败", str(error))
            return
        dialog = NoteEditorDialog(
            self.window(), title=str(note["title"]), latex=content, edit_mode=True, show_example=self.show_example,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = self.service.update_note_tex(
                int(note["id"]), dialog.latex_edit.toPlainText(), title=dialog.title_edit.text(), compile_pdf=False,
            )
        except Exception as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.refresh(force=True)
        if result.get("changed"):
            self.compile_note(int(note["id"]))

    def compile_selected_note(self) -> None:
        note = self.selected_note()
        if note is not None:
            self.compile_note(int(note["id"]))

    def compile_note(self, note_id: int) -> None:
        self.status_label.setText("正在编译笔记 PDF…")
        task = _Task(lambda: self.service.compile_note(note_id))
        task.setAutoDelete(False)
        self._tasks.add(task)

        def finished(result: Any) -> None:
            if not isValid(self):
                return
            self._tasks.discard(task)
            self._loaded_note_pdf = None
            self.refresh(force=True)
            if not (isinstance(result, dict) and result.get("ok")):
                error = result.get("error") if isinstance(result, dict) else result
                QMessageBox.warning(self, "笔记编译失败", f"TeX 已保存，PDF 未替换。\n\n{error}")

        task.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(task)

    def _show_more_menu(self) -> None:
        menu = QMenu(self)
        note = self.selected_note()
        open_tex = menu.addAction("打开 TeX 源文件")
        open_tex.setEnabled(note is not None)
        jump_menu = menu.addMenu("跳到链接的书签")
        unlink_menu = menu.addMenu("移除书签链接")
        pdf_path = self._pdf_path()
        identity = None
        if note is not None and pdf_path is not None:
            try:
                identity = self.service.document_identity(pdf_path)
            except Exception:
                identity = None
        for link in (note or {}).get("links", []):
            same_book = identity is not None and (
                link["document_sha256"] == identity["sha256"]
                or (link["document_name"] == identity["name"] and int(link["document_page_count"]) == identity["page_count"])
            )
            label = link["bookmark_title"] if same_book else f"{link['document_name']} · {link['bookmark_title']}"
            jump = jump_menu.addAction(label)
            jump.setEnabled(same_book)
            jump.triggered.connect(lambda _checked=False, page=int(link["page_start"]): self.reader.jump_to_page(page - 1))
            unlink = unlink_menu.addAction(label)
            unlink.triggered.connect(lambda _checked=False, link_id=int(link["id"]): self._unlink(link_id))
        jump_menu.setEnabled(bool(jump_menu.actions()))
        unlink_menu.setEnabled(bool(unlink_menu.actions()))
        chosen = menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))
        if chosen is open_tex and note is not None:
            open_path = getattr(getattr(self.reader, "owner", None), "open_path_with_feedback", None)
            if callable(open_path):
                open_path(Path(note["tex_file"]))

    def _unlink(self, link_id: int) -> None:
        answer = QMessageBox.question(self, "移除链接", "只移除这个书签链接，笔记本身和文件都会保留。继续吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.unlink_note(link_id)
        except Exception as error:
            QMessageBox.warning(self, "移除失败", str(error))
        self.refresh(force=True)

    # -- docking ----------------------------------------------------------------

    def toggle_floating(self) -> None:
        if self.is_floating():
            self.dock(show=True)
        else:
            self.undock()

    def undock(self) -> None:
        window = _FloatingNotesWindow(self)
        self._floating = window
        window.layout().addWidget(self)
        self.show()
        self.float_button.setText("停靠")
        window.show()
        window.raise_()
        notify = getattr(self.reader, "notes_panel_visibility_changed", None)
        if callable(notify):
            notify()
        self.refresh(force=True)

    def dock(self, *, show: bool) -> None:
        window = self._floating
        self._floating = None
        self.splitter.addWidget(self)
        self.setVisible(show)
        self.float_button.setText("小窗")
        if window is not None:
            window.hide()
            window.deleteLater()
        notify = getattr(self.reader, "notes_panel_visibility_changed", None)
        if callable(notify):
            notify()
        if show:
            self.refresh(force=True)


def copy_question_prompt(
    parent: QWidget, service: TextbookNotesService, *, question_key: str, book: str, question: str,
) -> bool:
    """Ask for the user's ideas, write the question's ChatGPT Work task (question,
    ideas, format rules and the complete example) and copy its one-line task."""

    ideas, accepted = QInputDialog.getMultiLineText(
        parent, "我的想法",
        "这道题想记什么？为什么选错了、哪里没看懂、想弄清什么，可以很零碎；\n"
        "ChatGPT Work 会按这些想法组织笔记。题目、答案和原书解析会自动带上。",
    )
    if not accepted:
        return False
    try:
        task = service.prepare_question_note_task(question_key, book=book, question=question, ideas=ideas)
    except Exception as error:
        QMessageBox.warning(parent, "生成任务失败", str(error))
        return False
    QApplication.clipboard().setText(task["task_line"])
    return True


class QuestionNotesDialog(QDialog):
    """The notes of one question-bank question: list, PDF view, create/edit/compile."""

    def __init__(
        self,
        parent: QWidget,
        *,
        question_link: dict[str, Any],
        prompt_text: str,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._closed = False
        self.service = textbook_notes_service()
        self.question_link = dict(question_link)
        self.prompt_text = prompt_text
        self.on_changed = on_changed
        self._notes: list[dict[str, Any]] = []
        self._tasks: set[_Task] = set()
        self._loaded_note_pdf: tuple[str, float] | None = None
        self.setWindowTitle(f"题目笔记 · {self.question_link['question_title']}")
        self.resize(760, 860)

        layout = QVBoxLayout(self)
        heading = QLabel(f"{self.question_link['book_title']} · {self.question_link['question_title']}")
        heading.setObjectName("sectionTitleSmall")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.note_list = QListWidget()
        self.note_list.setMaximumHeight(96)
        self.note_list.currentItemChanged.connect(lambda _current, _previous: self._show_selected_note())
        layout.addWidget(self.note_list)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.prompt_button = QPushButton("任务")
        self.prompt_button.setToolTip("写下你的想法，生成含题目、写作规则和完整示例的 ChatGPT Work 任务，并复制一行任务")
        self.prompt_button.clicked.connect(self.copy_prompt)
        self.new_button = QPushButton("新建 / 链接…")
        self.new_button.clicked.connect(self.open_link_dialog)
        self.edit_button = QPushButton("编辑")
        self.edit_button.clicked.connect(self.edit_selected_note)
        self.compile_button = QPushButton("编译")
        self.compile_button.clicked.connect(self.compile_selected_note)
        self.unlink_button = QPushButton("移除链接")
        self.unlink_button.setToolTip("只移除这道题和笔记的链接，笔记本身保留")
        self.unlink_button.clicked.connect(self.unlink_selected_note)
        self.example_button = QPushButton("示例")
        self.example_button.setToolTip("查看题目笔记示例（固定排版的标准样子）")
        self.example_button.clicked.connect(self.show_example)
        for button in (
            self.prompt_button, self.new_button, self.edit_button, self.compile_button,
            self.unlink_button, self.example_button,
        ):
            button.setObjectName("secondaryButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.status_label = QLabel("")
        self.status_label.setObjectName("cardNote")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.note_document = QPdfDocument(self)
        self._note_pdf_buffer = QBuffer(self)
        self.note_view = QPdfView()
        self.note_view.setDocument(self.note_document)
        self.note_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.note_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        layout.addWidget(self.note_view, 1)
        self.refresh()

    def copy_prompt(self) -> None:
        if copy_question_prompt(
            self, self.service, question_key=self.question_link["question_key"],
            book=self.question_link["book_title"], question=self.prompt_text,
        ):
            self.status_label.setText("已复制 ChatGPT Work 任务。它写好后点“新建 / 链接…”，草稿会自动填入。")

    def selected_note(self) -> dict[str, Any] | None:
        item = self.note_list.currentItem()
        if item is None:
            return None
        note_id = int(item.data(Qt.ItemDataRole.UserRole))
        return next((note for note in self._notes if int(note["id"]) == note_id), None)

    def refresh(self) -> None:
        try:
            notes = self.service.notes_for_question(self.question_link["question_key"])
        except Exception as error:
            notes = []
            self.status_label.setText(f"读取笔记失败：{error}")
        previous = self.selected_note()
        self._notes = notes
        self.note_list.blockSignals(True)
        self.note_list.clear()
        for note in notes:
            mark = "" if note["pdf_current"] else ("  ⚠ 编译失败" if note["compile_status"] == "failed" else "  · 待编译")
            item = QListWidgetItem(f"{note['note_code']}  {note['title']}{mark}")
            item.setData(Qt.ItemDataRole.UserRole, int(note["id"]))
            self.note_list.addItem(item)
        self.note_list.blockSignals(False)
        if notes:
            ids = [int(note["id"]) for note in notes]
            self.note_list.setCurrentRow(ids.index(int(previous["id"])) if previous and int(previous["id"]) in ids else 0)
        self._show_selected_note()

    def _load_note_pdf(self, pdf_file: Path) -> None:
        # In memory, so recompiling can replace the PDF file on Windows.
        self.note_document.close()
        self._note_pdf_buffer.close()
        self._note_pdf_buffer.setData(pdf_file.read_bytes())
        self._note_pdf_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.note_document.load(self._note_pdf_buffer)

    def _show_selected_note(self) -> None:
        note = self.selected_note()
        for button in (self.edit_button, self.compile_button, self.unlink_button):
            button.setEnabled(note is not None)
        if note is None:
            self.note_document.close()
            self._loaded_note_pdf = None
            if not self.status_label.text().startswith("读取笔记失败"):
                self.status_label.setText(
                    "这道题还没有笔记。先点“任务”写下想法交给 ChatGPT Work，它写好后点“新建 / 链接…”；"
                    "“示例”可查看标准排版。"
                )
            return
        if note.get("tex_consistent") is False:
            self.status_label.setText("正文与保存记录不一致，请核对正文或恢复版本；下方 PDF 不是当前版本。")
        elif note["pdf_current"]:
            self.status_label.setText(f"{note['note_code']} · 链接 {len(note['question_links'])} 道题、{len(note['links'])} 个书签")
        elif note["compile_status"] == "failed":
            self.status_label.setText("编译失败（下方为上一次成功的 PDF）：" + str(note["compile_error"]).splitlines()[0])
            self.status_label.setToolTip(str(note["compile_error"]))
        else:
            self.status_label.setText("TeX 已更新，PDF 尚未重新编译。")
        pdf_file = Path(note["pdf_file"])
        if pdf_file.is_file():
            stamp = (str(pdf_file), pdf_file.stat().st_mtime)
            if stamp != self._loaded_note_pdf:
                self._load_note_pdf(pdf_file)
                self._loaded_note_pdf = stamp
        else:
            self.note_document.close()
            self._loaded_note_pdf = None

    def _changed(self) -> None:
        self.refresh()
        if self.on_changed is not None:
            self.on_changed()

    def open_link_dialog(self) -> None:
        task = self.service.note_task("question", self.question_link["question_key"])
        dialog = NoteEditorDialog(
            self, existing_notes=self.service.list_notes(limit=500),
            target_label=self.question_link["question_title"], latex=str(task["draft_source"]),
            copy_prompt=lambda _indices: self.copy_prompt(), show_example=self.show_example,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if dialog.linking_existing():
                note = self.service.link_note(
                    int(dialog.existing_combo.currentData()), question_links=[self.question_link],
                )
            else:
                note = self.service.create_note(
                    dialog.title_edit.text(), dialog.latex_edit.toPlainText(),
                    question_links=[self.question_link], source_kind="manual", compile_pdf=False,
                )
                if task["draft_exists"]:
                    self.service.archive_note_draft("question", self.question_link["question_key"])
        except Exception as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self._changed()
        if not dialog.linking_existing():
            self.compile_note(int(note["id"]))

    def edit_selected_note(self) -> None:
        note = self.selected_note()
        if note is None:
            return
        try:
            content = self.service.read_note_tex(int(note["id"]))["content"]
        except Exception as error:
            QMessageBox.warning(self, "读取失败", str(error))
            return
        dialog = NoteEditorDialog(
            self, title=str(note["title"]), latex=content, edit_mode=True, show_example=self.show_example,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = self.service.update_note_tex(
                int(note["id"]), dialog.latex_edit.toPlainText(), title=dialog.title_edit.text(), compile_pdf=False,
            )
        except Exception as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self._changed()
        if result.get("changed"):
            self.compile_note(int(note["id"]))

    def compile_selected_note(self) -> None:
        note = self.selected_note()
        if note is not None:
            self.compile_note(int(note["id"]))

    def compile_note(self, note_id: int) -> None:
        self.status_label.setText("正在编译笔记 PDF…")
        task = _Task(lambda: self.service.compile_note(note_id))
        task.setAutoDelete(False)
        self._tasks.add(task)

        def finished(result: Any) -> None:
            self._tasks.discard(task)
            if self._closed or not isValid(self):
                return
            self._loaded_note_pdf = None
            self._changed()
            if not (isinstance(result, dict) and result.get("ok")):
                error = result.get("error") if isinstance(result, dict) else result
                QMessageBox.warning(self, "笔记编译失败", f"TeX 已保存，PDF 未替换。\n\n{error}")

        task.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(task)

    def unlink_selected_note(self) -> None:
        note = self.selected_note()
        if note is None:
            return
        link = next(
            (row for row in note["question_links"] if row["question_key"] == self.question_link["question_key"]), None,
        )
        if link is None:
            return
        answer = QMessageBox.question(self, "移除链接", "只移除这道题和笔记的链接，笔记本身和文件都会保留。继续吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.unlink_question(int(link["id"]))
        except Exception as error:
            QMessageBox.warning(self, "移除失败", str(error))
        self._changed()

    def show_example(self) -> None:
        self.note_list.blockSignals(True)
        self.note_list.clearSelection()
        self.note_list.setCurrentRow(-1)
        self.note_list.blockSignals(False)
        self.status_label.setText("正在准备示例笔记…")
        task = _Task(lambda: {"ok": True, "path": str(self.service.question_example_pdf())})
        task.setAutoDelete(False)
        self._tasks.add(task)

        def finished(result: Any) -> None:
            self._tasks.discard(task)
            if self._closed or not isValid(self):
                return
            if not (isinstance(result, dict) and result.get("ok")):
                error = result.get("error") if isinstance(result, dict) else result
                self.status_label.setText("示例笔记编译失败。")
                QMessageBox.warning(self, "示例笔记编译失败", str(error))
                return
            self._load_note_pdf(Path(result["path"]))
            self._loaded_note_pdf = None
            for button in (self.edit_button, self.compile_button, self.unlink_button):
                button.setEnabled(False)
            self.status_label.setText("正在显示【题目笔记示例】：所有笔记都使用这一固定排版。点“提示词”可复制含此示例的写作提示词。")

        task.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(task)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._closed = True
        self.note_document.close()
        super().closeEvent(event)


__all__ = [
    "NoteEditorDialog",
    "QuestionNotesDialog",
    "TextbookNotesPanel",
    "copy_question_prompt",
    "textbook_notes_service",
]
