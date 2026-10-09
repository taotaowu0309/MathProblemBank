from __future__ import annotations

import base64
import html
import json
from pathlib import Path
import re
import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QDir, QObject, QProcess, QRunnable, QThreadPool, Qt, QUrl, Signal, Slot
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QPalette,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.crash_diagnostics import PipelineRunLogger, get_crash_diagnostics
from shared.scripts.translation_agent_prompts import build_second_layer_prompt
from shared.scripts.translation_project_service import TranslationProjectService


def _font(widget: QWidget, size: int, weight: QFont.Weight = QFont.Weight.Normal) -> None:
    font = widget.font()
    font.setPointSize(size)
    font.setWeight(weight)
    widget.setFont(font)


def _reusable_explorer_script(target: Path) -> str:
    """Build a hidden PowerShell command that reuses an Explorer folder tab."""

    target_text = str(target.resolve()).replace("'", "''")
    folder_text = str(target.resolve().parent).replace("'", "''")
    return rf"""
$ErrorActionPreference = 'Stop'
$targetPath = [System.IO.Path]::GetFullPath('{target_text}')
$folderPath = [System.IO.Path]::GetFullPath('{folder_text}').TrimEnd('\')
$shell = New-Object -ComObject Shell.Application
$existing = $null
foreach ($window in @($shell.Windows())) {{
    try {{
        $candidate = [System.IO.Path]::GetFullPath(
            [string]$window.Document.Folder.Self.Path
        ).TrimEnd('\')
        if ([System.StringComparer]::OrdinalIgnoreCase.Equals($candidate, $folderPath)) {{
            $existing = $window
            break
        }}
    }} catch {{}}
}}
if ($null -ne $existing) {{
    $item = $existing.Document.Folder.ParseName(
        [System.IO.Path]::GetFileName($targetPath)
    )
    if ($null -ne $item) {{
        $existing.Document.SelectItem($item, 29)
    }}
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class MathProblemBankExplorerActivator {{
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}}
'@
    $handle = [IntPtr][long]$existing.HWND
    [MathProblemBankExplorerActivator]::ShowWindowAsync($handle, 9) | Out-Null
    [MathProblemBankExplorerActivator]::SetForegroundWindow($handle) | Out-Null
}} else {{
    Start-Process -FilePath 'explorer.exe' -ArgumentList (
        '/select,"' + $targetPath + '"'
    )
}}
""".strip()


def _reveal_in_reusable_explorer(target: Path) -> bool:
    script = _reusable_explorer_script(target)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    launch_result = QProcess.startDetached(
        "powershell.exe",
        [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded,
        ],
    )
    return bool(launch_result[0]) if isinstance(launch_result, tuple) else bool(launch_result)


def _latex_math_fragment_html(value: str) -> str:
    math = str(value or "")
    command_replacements = {
        r"\mathbb{R}": "ℝ",
        r"\mathbb{C}": "ℂ",
        r"\ell": "ℓ",
        r"\mu": "μ",
        r"\alpha": "α",
        r"\beta": "β",
        r"\gamma": "γ",
        r"\lambda": "λ",
        r"\omega": "ω",
        r"\,": " ",
    }
    for command, symbol in command_replacements.items():
        math = math.replace(command, symbol)
    math = math.replace("*", "∗")
    escaped = html.escape(math)
    escaped = re.sub(
        r"\^\{([^{}]+)\}|\^([A-Za-z0-9∗+\-]+)",
        lambda match: f"<sup>{match.group(1) or match.group(2)}</sup>",
        escaped,
    )
    escaped = re.sub(
        r"_\{([^{}]+)\}|_([A-Za-z0-9]+)",
        lambda match: f"<sub>{match.group(1) or match.group(2)}</sub>",
        escaped,
    )
    return escaped


def _inline_latex_title_html(value: object) -> str:
    text = str(value or "")
    parts = re.split(r"(?<!\\)\$", text)
    if len(parts) % 2 == 0:
        return html.escape(text)
    rendered = [
        _latex_math_fragment_html(part) if index % 2 else html.escape(part)
        for index, part in enumerate(parts)
    ]
    return "<span style=\"white-space: pre-wrap\">" + "".join(rendered) + "</span>"


class _InlineLatexTitleDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index) -> None:  # type: ignore[no-untyped-def]
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        source_text = styled.text
        styled.text = ""
        style = styled.widget.style() if styled.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)

        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(styled.font)
        document.setHtml(_inline_latex_title_html(source_text))
        # Qt's style text sub-rect can be shorter than the row.  That clips the
        # bottom of the active row once its title becomes bold, especially at
        # Windows display scaling above 100 percent.
        text_rect = styled.rect.adjusted(6, 0, -4, 0)
        document.setTextWidth(float(text_rect.width()))
        context = QAbstractTextDocumentLayout.PaintContext()
        color_role = (
            QPalette.ColorRole.HighlightedText
            if styled.state & QStyle.StateFlag.State_Selected
            else QPalette.ColorRole.Text
        )
        context.palette.setColor(
            QPalette.ColorRole.Text,
            styled.palette.color(color_role),
        )
        painter.save()
        painter.translate(text_rect.left(), text_rect.top())
        painter.setClipRect(0, 0, text_rect.width(), text_rect.height())
        painter.translate(0, max(0.0, (text_rect.height() - document.size().height()) / 2.0))
        document.documentLayout().draw(painter, context)
        painter.restore()


class _TranslationWorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)
    progress = Signal(str)


class _TranslationStreamingWorker(QRunnable):
    def __init__(
        self,
        task: Callable[[Callable[[str], None]], Any],
        *,
        operation: str = "translation_worker",
        project_id: str = "",
        stage: str = "running",
        run_logger: PipelineRunLogger | None = None,
    ) -> None:
        super().__init__()
        self.task = task
        self.signals = _TranslationWorkerSignals()
        self.operation = operation
        self.project_id = project_id
        self.stage = stage
        self.run_logger = run_logger
        self.worker_id = f"{operation}-{id(self):x}"

    @Slot()
    def run(self) -> None:
        diagnostics = get_crash_diagnostics()
        failed = False
        diagnostics.register_worker(
            self.worker_id,
            operation=self.operation,
            project_id=self.project_id,
            stage=self.stage,
            thread_name=threading.current_thread().name,
        )
        try:
            with diagnostics.operation_context(
                current_operation=self.operation,
                active_project_id=self.project_id,
                active_pipeline="translation",
                active_stage=self.stage,
                active_thread=threading.current_thread().name,
            ):
                result = self.task(self.signals.progress.emit)
                diagnostics.update_context(last_successful_step="worker_result_ready")
                self.signals.finished.emit(result)
        except BaseException as error:
            failed = True
            traceback_text = traceback.format_exc()
            if self.run_logger is not None:
                self.run_logger.fail(str(error), traceback_text)
            diagnostics.record_exception(
                type(error),
                error,
                error.__traceback__,
                kind="translation_background_worker_exception",
                fatal=False,
            )
            self.signals.failed.emit(
                {"message": str(error), "traceback": traceback_text}
            )
        finally:
            diagnostics.unregister_worker(
                self.worker_id,
                status="failed" if failed else "finished",
            )


class TranslationProjectPage(QWidget):
    status_requested = Signal(str)
    markdown_open_requested = Signal(str, int)
    markdown_bundle_open_requested = Signal(object, str, int)
    pdf_open_requested = Signal(str, int, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        service: TranslationProjectService | None = None,
        confirm_formal_write: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service or TranslationProjectService()
        self.confirm_formal_write = confirm_formal_write
        self.current_project_code = ""
        self.current_target_id = ""
        self.material_confirmed_target_id = ""
        self.project_rows: dict[str, dict[str, Any]] = {}
        self.unit_rows: dict[str, dict[str, Any]] = {}
        self.log_history: list[str] = []
        self.thread_pool = QThreadPool.globalInstance()
        self._active_workers: set[QRunnable] = set()
        self._running_agent_sections: set[str] = set()
        self._running_package_targets: set[str] = set()
        self._running_mineru_projects: set[str] = set()
        self._running_latex_projects: set[str] = set()
        self.setObjectName("translationProjectPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addLayout(self._build_header())
        root.addWidget(self._build_workspace(), 1)
        self._create_management_dialogs()
        self.refresh_projects()
        self.append_process_log(
            "已就绪。新建译本后，日志会从原教材复制、内置 MinerU 运行、章级 PDF/节级 Markdown "
            "生成，一直记录到第二层校正版和最小单位材料包。"
        )

    def showEvent(self, event: Any) -> None:  # type: ignore[override]
        get_crash_diagnostics().update_context(
            current_operation="translation_workspace",
            active_view="translation_project",
            active_project_id=self.current_project_code,
            active_pipeline="translation",
            active_stage="idle",
        )
        super().showEvent(event)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        title_group = QVBoxLayout()
        title = QLabel("数学译本")
        title.setObjectName("pageTitle")
        _font(title, 24, QFont.Weight.DemiBold)
        note = QLabel(
            "原书 / MinerU 证据 → 节级轻量校正 Agent → 节或小节材料包 → ChatGPT 中文 LaTeX。"
            "原书 PDF 始终是最高事实源。"
        )
        note.setObjectName("pageNote")
        note.setWordWrap(True)
        _font(note, 10)
        title_group.addWidget(title)
        title_group.addWidget(note)
        header.addLayout(title_group, 1)

        self.project_combo = QComboBox()
        self.project_combo.setFixedHeight(38)
        self.project_combo.currentIndexChanged.connect(self._project_changed)
        header.addWidget(self.project_combo)
        for text, callback, primary in (
            ("新建译本", self.create_project, True),
            ("运行 MinerU", self.run_mineru, False),
            ("刷新", self.refresh_projects, False),
        ):
            button = QPushButton(text)
            button.setObjectName("primaryButton" if primary else "secondaryButton")
            button.setFixedHeight(38)
            button.clicked.connect(callback)
            header.addWidget(button)
            if text == "运行 MinerU":
                self.mineru_button = button
        return header

    def _resize_project_combo_to_current_text(self) -> None:
        text = self.project_combo.currentText() or "选择译本项目"
        content_width = self.project_combo.fontMetrics().horizontalAdvance(text)
        self.project_combo.setFixedWidth(max(126, min(420, content_width + 52)))

    def _build_workspace(self) -> QWidget:
        workspace = QFrame()
        workspace.setObjectName("glassPanel")
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        agent_header = QHBoxLayout()
        agent_title = QLabel("译本处理日志")
        agent_title.setObjectName("sectionTitle")
        _font(agent_title, 13, QFont.Weight.DemiBold)
        agent_header.addWidget(agent_title)
        agent_header.addStretch(1)
        copy_log = QPushButton("复制日志")
        copy_log.setObjectName("secondaryButton")
        copy_log.setFixedHeight(30)
        copy_log.clicked.connect(self.copy_process_log)
        agent_header.addWidget(copy_log)
        clear_log = QPushButton("清空当前显示")
        clear_log.setObjectName("secondaryButton")
        clear_log.setFixedHeight(30)
        clear_log.clicked.connect(self.clear_process_log)
        agent_header.addWidget(clear_log)
        layout.addLayout(agent_header)

        self.target_label = QLabel("尚未选择工作单元。")
        self.target_label.setObjectName("sectionTitle")
        self.target_label.setWordWrap(True)
        _font(self.target_label, 12, QFont.Weight.DemiBold)
        layout.addWidget(self.target_label)
        self.target_note = QLabel(
            "请先在“分集与材料”中选择当前节或小节。配置的导入最小单位只控制材料与 LaTeX 行；"
            "Agent Markdown 的完整章/节/小节层级请在“译本目录”中展开并跳转预览。"
        )
        self.target_note.setObjectName("pageNote")
        self.target_note.setWordWrap(True)
        layout.addWidget(self.target_note)
        layout.addLayout(self._build_view_action_row())
        self.activity_log = QTextBrowser()
        self.activity_log.setObjectName("softText")
        self.activity_log.setReadOnly(True)
        self.activity_log.setOpenExternalLinks(False)
        layout.addWidget(self.activity_log, 1)
        layout.addLayout(self._build_pipeline_action_row())
        return workspace

    def append_process_log(self, message: str) -> None:
        clean = str(message or "").rstrip()
        if not clean:
            return
        get_crash_diagnostics().log_event(
            level="INFO",
            logger="ui.translation",
            event="translation_ui_log",
            message=clean,
            project_id=self.current_project_code,
            operation="translation_workspace",
        )
        self.log_history.append(clean)
        if hasattr(self, "activity_log"):
            cursor = self.activity_log.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            prefix = "\n" if not self.activity_log.document().isEmpty() else ""
            cursor.insertText(prefix + clean)
            self.activity_log.setTextCursor(cursor)
            self.activity_log.verticalScrollBar().setValue(
                self.activity_log.verticalScrollBar().maximum()
            )

    def copy_process_log(self) -> None:
        QApplication.clipboard().setText("\n".join(self.log_history))
        self.status_requested.emit("数学译本处理日志已复制。")

    def clear_process_log(self) -> None:
        self.log_history.clear()
        self.activity_log.clear()

    def _action_button(
        self,
        text: str,
        callback: Callable[[], None],
        *,
        primary: bool = False,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("primaryButton" if primary else "secondaryButton")
        button.setFixedHeight(36)
        _font(button, 9, QFont.Weight.DemiBold)
        button.clicked.connect(callback)
        return button

    def _build_view_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        self.target_buttons: list[QPushButton] = []
        self.project_buttons: list[QPushButton] = []
        for text, callback, needs_target in (
            ("查看原书", self.reveal_original_pdf, False),
            ("查看 Agent 校正版", self.open_corrected_markdown, True),
            ("分集与材料", self.show_materials_dialog, False),
            ("译本目录", self.show_outline_dialog, False),
            ("TeX 精修", self.show_tex_refinement_dialog, False),
        ):
            button = self._action_button(text, callback)
            button.setProperty("translationActionRow", "view")
            row.addWidget(button)
            (self.target_buttons if needs_target else self.project_buttons).append(button)
        row.addStretch(1)
        return row

    def _build_pipeline_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        self.second_layer_button = self._action_button(
            "运行第二层 Agent", self.run_second_layer_agent, primary=True
        )
        definitions = (
            (self.second_layer_button, True),
            (self._action_button("生成材料包", self.generate_package, primary=True), True),
            (self._action_button("导入 ChatGPT LaTeX", self.import_chatgpt_latex, primary=True), True),
            (self._action_button("查看已导入 TeX", self.open_latex), True),
            (self._action_button("定位到 PDF", self.locate_formal_pdf), True),
            (self._action_button("打开 PDF 位置", self.reveal_formal_pdf), False),
        )
        for button, needs_target in definitions:
            button.setProperty("translationActionRow", "pipeline")
            row.addWidget(button)
            (self.target_buttons if needs_target else self.project_buttons).append(button)
        row.addStretch(1)
        return row

    def _create_management_dialogs(self) -> None:
        self.materials_dialog = QDialog(self)
        self.materials_dialog.setWindowTitle("译本节与材料")
        self.materials_dialog.setModal(False)
        self.materials_dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.materials_dialog.resize(1180, 650)
        materials_layout = QVBoxLayout(self.materials_dialog)
        materials_title = QLabel("导入最小单位与材料")
        materials_title.setObjectName("sectionTitle")
        _font(materials_title, 13, QFont.Weight.DemiBold)
        materials_layout.addWidget(materials_title)
        minimum_row = QHBoxLayout()
        minimum_note = QLabel(
            "这里只列出实际导入行：节模式不显示章和小节；小节模式逐一显示小节。"
        )
        minimum_note.setObjectName("pageNote")
        minimum_note.setWordWrap(True)
        minimum_row.addWidget(minimum_note, 1)
        minimum_row.addWidget(QLabel("导入最小单位"))
        self.granularity_combo = QComboBox()
        self.granularity_combo.addItem("节（默认）", "section")
        self.granularity_combo.addItem("小节", "subsection")
        self.granularity_combo.currentIndexChanged.connect(
            self._on_material_granularity_changed
        )
        minimum_row.addWidget(self.granularity_combo)
        materials_layout.addLayout(minimum_row)
        self.materials_table = QTableWidget()
        self.materials_table.setObjectName("softTable")
        self.materials_table.setStyleSheet(
            "QTableWidget::item:selected {"
            " background-color: #2F8FEA; color: white;"
            "}"
            "QTableWidget::item:selected:!active {"
            " background-color: #2F8FEA; color: white;"
            "}"
        )
        self.materials_table.setColumnCount(9)
        self.materials_table.setHorizontalHeaderLabels(
            ["编号", "标题", "单位", "MinerU", "Agent", "图片", "材料包", "LaTeX", "编译"]
        )
        self.materials_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.materials_table.setItemDelegateForColumn(
            1, _InlineLatexTitleDelegate(self.materials_table)
        )
        for column in (0, 2, 3, 4, 5, 6, 7, 8):
            self.materials_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.materials_table.verticalHeader().setVisible(False)
        self.materials_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.materials_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.materials_table.itemSelectionChanged.connect(
            self._on_material_selection_changed
        )
        materials_layout.addWidget(self.materials_table, 1)
        material_buttons = QHBoxLayout()
        material_definitions = [
            ("选中本单位", self.select_highlighted_material, True),
            ("查看 Agent 校正版", lambda: self._run_for_highlighted(self.open_corrected_markdown), False),
            ("运行 / 重新运行 Agent", lambda: self._run_for_highlighted(self.run_second_layer_agent), False),
            ("生成材料包", lambda: self._run_for_highlighted(self.generate_package), False),
            ("打开材料包位置", lambda: self._run_for_highlighted(self.reveal_material_package), False),
            ("ChatGPT 编写 / 导入", lambda: self._run_for_highlighted(self.import_chatgpt_latex), True),
            ("定位到 PDF", lambda: self._run_for_highlighted(self.locate_formal_pdf), False),
        ]
        self.material_action_buttons: list[QPushButton] = []
        for text, callback, primary in material_definitions:
            button = QPushButton(text)
            button.setObjectName("primaryButton" if primary else "secondaryButton")
            button.setFixedHeight(34)
            _font(button, 8, QFont.Weight.DemiBold)
            button.clicked.connect(callback)
            button.setEnabled(False)
            if text == "选中本单位":
                self.material_select_button = button
            else:
                self.material_action_buttons.append(button)
            material_buttons.addWidget(button)
        material_buttons.addStretch(1)
        materials_layout.addLayout(material_buttons)

        self.outline_dialog = QDialog(self)
        self.outline_dialog.setWindowTitle("数学译本目录")
        self.outline_dialog.setModal(False)
        self.outline_dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.outline_dialog.resize(980, 700)
        outline_layout = QVBoxLayout(self.outline_dialog)
        outline_title = QLabel("Agent 校正版的实际目录")
        outline_title.setObjectName("sectionTitle")
        _font(outline_title, 13, QFont.Weight.DemiBold)
        outline_layout.addWidget(outline_title)
        outline_note = QLabel(
            "此窗口始终显示 Chapter / Section / Subsection 完整层级，与导入最小单位无关。"
            "双击节或小节，会在 Markdown 阅读器中打开对应 Agent 校正版并定位到标题。"
        )
        outline_note.setObjectName("pageNote")
        outline_note.setWordWrap(True)
        outline_layout.addWidget(outline_note)
        self.outline_tree = QTreeWidget()
        self.outline_tree.setObjectName("softTree")
        self.outline_tree.setColumnCount(5)
        self.outline_tree.setHeaderLabels(["类型", "编号", "标题", "原书页", "Agent 定位"])
        for column in (0, 1, 3, 4):
            self.outline_tree.header().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.outline_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.outline_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.outline_tree.itemDoubleClicked.connect(self.preview_selected_outline_item)
        outline_layout.addWidget(self.outline_tree, 1)
        outline_buttons = QHBoxLayout()
        for text, callback in (
            ("展开全部", self.outline_tree.expandAll),
            ("折叠到章", self._collapse_outline_to_chapters),
            ("预览 Agent 校正版", self.preview_selected_outline_item),
            ("刷新", self.refresh_outline),
        ):
            button = QPushButton(text)
            button.setObjectName("secondaryButton")
            button.setFixedHeight(34)
            button.clicked.connect(callback)
            outline_buttons.addWidget(button)
        outline_buttons.addStretch(1)
        outline_layout.addLayout(outline_buttons)

        self.tex_refinement_dialog = QDialog(self)
        self.tex_refinement_dialog.setWindowTitle("数学译本 TeX 精修选择")
        self.tex_refinement_dialog.setModal(False)
        self.tex_refinement_dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.tex_refinement_dialog.resize(980, 650)
        refine_layout = QVBoxLayout(self.tex_refinement_dialog)
        refine_title = QLabel("按实际最小精度选择 TeX")
        refine_title.setObjectName("sectionTitle")
        _font(refine_title, 13, QFont.Weight.DemiBold)
        refine_layout.addWidget(refine_title)
        refine_note = QLabel(
            "此列表不读取“导入最小单位”设置，而是检查已导入正式 TeX 的真实 sectioning 命令。"
            "若一个节级文件内写有多个 subsection，只列出并允许精修这些 subsection。"
        )
        refine_note.setObjectName("pageNote")
        refine_note.setWordWrap(True)
        refine_layout.addWidget(refine_note)
        self.tex_refinement_table = QTableWidget()
        self.tex_refinement_table.setObjectName("softTable")
        self.tex_refinement_table.setColumnCount(5)
        self.tex_refinement_table.setHorizontalHeaderLabels(
            ["编号", "标题", "实际最小单位", "正式源文件", "源码范围"]
        )
        self.tex_refinement_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.tex_refinement_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        for column in (0, 2, 4):
            self.tex_refinement_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.tex_refinement_table.verticalHeader().setVisible(False)
        self.tex_refinement_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tex_refinement_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tex_refinement_table.itemDoubleClicked.connect(
            lambda _item: self.open_selected_tex_refinement()
        )
        refine_layout.addWidget(self.tex_refinement_table, 1)
        refine_buttons = QHBoxLayout()
        enter_refine = QPushButton("进入 TeX 精修与预览")
        enter_refine.setObjectName("primaryButton")
        enter_refine.setFixedHeight(36)
        enter_refine.clicked.connect(self.open_selected_tex_refinement)
        refresh_refine = QPushButton("刷新实际精度")
        refresh_refine.setObjectName("secondaryButton")
        refresh_refine.setFixedHeight(36)
        refresh_refine.clicked.connect(self.refresh_tex_refinement_units)
        refine_buttons.addWidget(enter_refine)
        refine_buttons.addWidget(refresh_refine)
        refine_buttons.addStretch(1)
        refine_layout.addLayout(refine_buttons)

    def _confirm(self, title: str, message: str) -> bool:
        if self.confirm_formal_write is not None:
            return bool(self.confirm_formal_write(title, message))
        return (
            QMessageBox.question(self, title, message)
            == QMessageBox.StandardButton.Yes
        )

    def create_project(self) -> None:
        title, accepted = QInputDialog.getText(self, "新建数学译本", "原书书名：")
        if not accepted or not title.strip():
            return
        author, accepted = QInputDialog.getText(self, "新建数学译本", "原作者（可留空）：")
        if not accepted:
            return
        source_pdf, _selected_filter = QFileDialog.getOpenFileName(
            self, "选择原书 PDF", "", "PDF (*.pdf)"
        )
        if not source_pdf:
            return
        if not self._confirm(
            "创建译本项目",
            "程序会把原书 PDF 复制到新的正式译本项目目录，并锁定当前第二层 Skill revision。继续吗？",
        ):
            return
        try:
            project = self.service.create_project(
                title,
                author,
                source_pdf,
                emit=self.append_process_log,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.append_process_log(f"[失败] 创建译本项目：{error}")
            QMessageBox.warning(self, "创建译本失败", str(error))
            return
        self.current_project_code = str(project["code"])
        self.refresh_projects()
        saved_source = self.service._project_dir(self.current_project_code) / str(
            project.get("source_pdf") or "source/original.pdf"
        )
        self.append_process_log(f"[新建译本] 原书已保存到固定工程路径：{saved_source}")
        self.status_requested.emit(f"已创建译本项目 {self.current_project_code}。")

    def run_mineru(self) -> None:
        if not self.current_project_code:
            QMessageBox.information(self, "没有译本项目", "请先新建或选择一个译本项目。")
            return
        project_code = self.current_project_code
        if project_code in self._running_mineru_projects:
            QMessageBox.information(self, "MinerU 正在运行", "当前译本的第一层已经在处理中。")
            return
        project = self.project_rows.get(project_code) or self.service.project(project_code)
        rerun = bool(project.get("mineru"))
        if rerun and not self._confirm(
            "重新运行 MinerU",
            "将复用已完成的 32 页检查点，并重建章级 PDF、节级 Markdown、公式和高清截图。继续吗？",
        ):
            return
        action = "重新运行" if rerun else "运行"
        diagnostics = get_crash_diagnostics()
        run_logger = diagnostics.start_pipeline_run(
            pipeline="translation",
            project_id=project_code,
            project_name=str(project.get("title") or ""),
            operation="mineru_first_layer",
        )
        diagnostics.update_context(
            current_operation="mineru_first_layer",
            active_project_id=project_code,
            active_project_name=str(project.get("title") or ""),
            active_pipeline="translation",
            active_stage="starting",
            active_file=str(
                self.service._project_dir(project_code)
                / str(project.get("source_pdf") or "source/original.pdf")
            ),
        )
        self.append_process_log(
            f"[第一层] {action}题库内置 MinerU：{project_code} {project.get('title') or ''}"
        )
        self.append_process_log(f"[第一层持久日志] {run_logger.log_path}")

        def task(ui_emit: Callable[[str], None]) -> dict[str, Any]:
            def persist_and_emit(message: str) -> None:
                run_logger.emit(message)
                ui_emit(message)

            return self.service.run_mineru(
                project_code,
                emit=persist_and_emit,
            )

        worker = _TranslationStreamingWorker(
            task,
            operation="mineru_first_layer",
            project_id=project_code,
            stage="mineru_processing",
            run_logger=run_logger,
        )
        worker.setAutoDelete(False)
        self._active_workers.add(worker)
        self._running_mineru_projects.add(project_code)
        self._update_target_controls()

        def release() -> None:
            self._active_workers.discard(worker)
            self._running_mineru_projects.discard(project_code)

        def finished(_result: object) -> None:
            diagnostics.update_context(
                current_operation="mineru_first_layer_completion_callback",
                active_project_id=project_code,
                active_pipeline="translation",
                active_stage="ui_completion_callback",
                last_successful_step="project_write_complete",
            )
            run_logger.emit(
                "后台结果已送达主线程，开始释放 worker 并刷新译本项目控件。",
                event="ui_completion_callback_started",
                stage="ui_completion_callback",
            )
            try:
                release()
                self.current_project_code = project_code
                run_logger.emit(
                    "开始刷新项目下拉框、分集表格与目录树。",
                    event="ui_refresh_started",
                    stage="ui_refresh",
                )
                self.refresh_projects()
                run_logger.emit(
                    "项目下拉框、分集表格与目录树刷新完成。",
                    event="ui_refresh_finished",
                    stage="ui_refresh_complete",
                )
                self.append_process_log(
                    "[第一层完成] MinerU、拆章、节级 Markdown 和高清截图已全部生成。"
                )
                self.status_requested.emit("译本第一层已完成；目录与分集材料状态已刷新。")
                run_logger.finish("第一层后台处理与主线程 UI 刷新均已完成。")
                diagnostics.update_context(
                    active_stage="idle",
                    last_successful_step="mineru_ui_refresh_complete",
                )
            except BaseException as error:
                release()
                traceback_text = traceback.format_exc()
                run_logger.fail(
                    f"第一层产物已写入，但完成后的 UI 刷新失败：{error}",
                    traceback_text,
                )
                diagnostics.record_exception(
                    type(error),
                    error,
                    error.__traceback__,
                    kind="translation_mineru_completion_callback_exception",
                    fatal=False,
                )
                self.append_process_log(
                    f"[第一层 UI 刷新失败] 第一层产物已经保存；无需重跑 MinerU。{error}"
                )
                self._update_target_controls()
                QMessageBox.critical(
                    self,
                    "第一层已完成，但界面刷新失败",
                    "MinerU 与正式产物已经保存，不需要重新运行。"
                    f"\n\n界面刷新错误：{error}\n\n持久日志：{run_logger.log_path}",
                )

        def failed(failure: object) -> None:
            data = failure if isinstance(failure, dict) else {"message": str(failure)}
            message = str(data.get("message") or "未知错误")
            release()
            self._update_target_controls()
            self.append_process_log(f"[第一层失败] {message}")
            QMessageBox.warning(
                self,
                "MinerU 第一层失败",
                f"{message}\n\n持久日志：{run_logger.log_path}",
            )

        worker.signals.progress.connect(self.append_process_log)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        try:
            self.thread_pool.start(worker)
        except Exception:
            release()
            self._update_target_controls()
            raise

    def refresh_projects(self) -> None:
        diagnostics = get_crash_diagnostics()
        diagnostics.update_context(
            current_operation="translation_refresh_projects",
            active_project_id=self.current_project_code,
            active_pipeline="translation",
            active_stage="ui_refresh_project_list",
        )
        projects = self.service.list_projects()
        self.project_rows = {
            str(project.get("code") or ""): project
            for project in projects
            if str(project.get("code") or "")
        }
        selected = self.current_project_code
        previous_blocked = self.project_combo.blockSignals(True)
        try:
            self.project_combo.clear()
            self.project_combo.addItem("选择译本项目", "")
            for project in projects:
                code = str(project.get("code") or "")
                title = str(project.get("title") or "").strip() or "未命名译本"
                self.project_combo.addItem(title, code)
            target_index = self.project_combo.findData(selected)
            self.project_combo.setCurrentIndex(target_index if target_index >= 0 else 0)
        finally:
            self.project_combo.blockSignals(previous_blocked)
        self._resize_project_combo_to_current_text()
        self.current_project_code = str(self.project_combo.currentData() or "")
        self.refresh_outline()
        diagnostics.update_context(
            active_project_id=self.current_project_code,
            active_stage="ui_refresh_complete",
            last_successful_step="translation_project_widgets_refreshed",
        )

    def _project_changed(self) -> None:
        self._resize_project_combo_to_current_text()
        self.current_project_code = str(self.project_combo.currentData() or "")
        self.current_target_id = ""
        self.material_confirmed_target_id = ""
        get_crash_diagnostics().update_context(
            current_operation="translation_project_selected",
            active_project_id=self.current_project_code,
            active_pipeline="translation",
            active_stage="idle",
        )
        self.refresh_outline()

    def refresh_outline(self) -> None:
        if not hasattr(self, "materials_table") or not hasattr(self, "outline_tree"):
            return
        expanded_ids = self._expanded_outline_ids()
        self.materials_table.setRowCount(0)
        self.outline_tree.clear()
        self.unit_rows = {}
        if not self.current_project_code:
            self.current_target_id = ""
            self.material_confirmed_target_id = ""
            self._update_target_controls()
            self._on_material_selection_changed()
            return
        granularity = str(self.granularity_combo.currentData() or "section")
        try:
            units = self.service.list_units(self.current_project_code, granularity)
            outline = self.service.outline_nodes(self.current_project_code)
        except (OSError, RuntimeError, ValueError) as error:
            self.outline_tree.addTopLevelItem(
                QTreeWidgetItem(["错误", "", f"读取失败：{error}"])
            )
            return
        self.materials_table.setRowCount(len(units))
        active_row = -1
        for row_index, row in enumerate(units):
            target_id = str(row["target_id"])
            self.unit_rows[target_id] = row
            if target_id == self.current_target_id:
                active_row = row_index
            values = [
                str(row.get("number") or ""),
                str(row.get("title") or ""),
                "小节" if row.get("target_kind") == "subsection" else "节",
                str(row.get("mineru_status") or ""),
                str(row.get("agent_status") or ""),
                str(row.get("image_status") or ""),
                str(row.get("package_status") or ""),
                str(row.get("latex_status") or ""),
                str(row.get("compile_status") or ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, target_id)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.materials_table.setItem(row_index, column, item)
        if active_row < 0 and self.current_target_id:
            self.current_target_id = ""
            self.material_confirmed_target_id = ""
        elif active_row >= 0:
            active_item = self.materials_table.item(active_row, 1)
            if active_item is not None:
                self.materials_table.scrollToItem(active_item)
        self._update_active_material_row_style()
        self.materials_table.clearSelection()
        self.materials_table.setCurrentCell(-1, -1)

        for chapter in outline:
            chapter_item = QTreeWidgetItem(
                [
                    "Chapter",
                    str(chapter.get("number") or ""),
                    str(chapter.get("title") or ""),
                    self._page_range_text(chapter),
                    "—",
                ]
            )
            chapter_item.setData(0, Qt.ItemDataRole.UserRole, chapter)
            self.outline_tree.addTopLevelItem(chapter_item)
            for section in chapter.get("children") or []:
                section_item = self._outline_tree_item(section)
                chapter_item.addChild(section_item)
                for subsection in section.get("children") or []:
                    section_item.addChild(self._outline_tree_item(subsection))
        self._restore_outline_expansion(expanded_ids)
        self._update_target_controls()
        self._on_material_selection_changed()

    @staticmethod
    def _page_range_text(row: dict[str, Any]) -> str:
        start = int(row.get("page_start") or 1)
        end = int(row.get("page_end") or start)
        return str(start) if start == end else f"{start}–{end}"

    def _outline_tree_item(self, row: dict[str, Any]) -> QTreeWidgetItem:
        line = int(row.get("source_line") or 0)
        status = f"第 {line} 行" if row.get("agent_markdown") and line else (
            "已校正，未匹配标题" if row.get("agent_markdown") else "Agent 未运行"
        )
        item = QTreeWidgetItem(
            [
                "Subsection" if row.get("kind") == "subsection" else "Section",
                str(row.get("number") or ""),
                str(row.get("title") or ""),
                self._page_range_text(row),
                status,
            ]
        )
        item.setData(0, Qt.ItemDataRole.UserRole, row)
        if row.get("preview_only"):
            item.setToolTip(2, "此小节来自 Agent Markdown 的实际标题，仅用于预览定位；不会新增导入单位。")
        return item

    def _expanded_outline_ids(self) -> set[str]:
        if not hasattr(self, "outline_tree"):
            return set()
        expanded: set[str] = set()
        root = self.outline_tree.invisibleRootItem()
        stack = [root.child(index) for index in range(root.childCount())]
        while stack:
            item = stack.pop()
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if item.isExpanded() and isinstance(data, dict):
                expanded.add(str(data.get("id") or ""))
            stack.extend(item.child(index) for index in range(item.childCount()))
        return expanded

    def _restore_outline_expansion(self, expanded_ids: set[str]) -> None:
        root = self.outline_tree.invisibleRootItem()
        for index in range(root.childCount()):
            chapter = root.child(index)
            data = chapter.data(0, Qt.ItemDataRole.UserRole)
            chapter_id = str(data.get("id") or "") if isinstance(data, dict) else ""
            chapter.setExpanded(not expanded_ids or chapter_id in expanded_ids)
            for child_index in range(chapter.childCount()):
                section = chapter.child(child_index)
                section_data = section.data(0, Qt.ItemDataRole.UserRole)
                section_id = (
                    str(section_data.get("id") or "")
                    if isinstance(section_data, dict)
                    else ""
                )
                section.setExpanded(section_id in expanded_ids)

    def _collapse_outline_to_chapters(self) -> None:
        self.outline_tree.collapseAll()
        root = self.outline_tree.invisibleRootItem()
        for index in range(root.childCount()):
            root.child(index).setExpanded(True)

    def _highlighted_material_id(self) -> str:
        selected = self.materials_table.selectionModel().selectedRows()
        row_index = int(selected[0].row()) if selected else self.materials_table.currentRow()
        if row_index < 0:
            return ""
        item = self.materials_table.item(row_index, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _on_material_selection_changed(self) -> None:
        highlighted_target_id = self._highlighted_material_id()
        highlighted = bool(highlighted_target_id)
        if highlighted:
            # A blue row is only a pending choice. Every new highlight, even
            # the currently active row, requires the explicit first button.
            self.material_confirmed_target_id = ""
        active = (
            self.current_target_id in self.unit_rows
            and self.material_confirmed_target_id == self.current_target_id
            and not highlighted
        )
        project_busy = self.current_project_code in self._running_latex_projects
        select_button = getattr(self, "material_select_button", None)
        if select_button is not None:
            select_button.setEnabled(highlighted)
        for button in getattr(self, "material_action_buttons", []):
            button.setEnabled(active and not project_busy)

    def _on_material_granularity_changed(self, *_args: Any) -> None:
        self.material_confirmed_target_id = ""
        self.refresh_outline()

    def _update_active_material_row_style(self) -> None:
        active_target_id = (
            self.current_target_id if self.current_target_id in self.unit_rows else ""
        )
        for row_index in range(self.materials_table.rowCount()):
            row_item = self.materials_table.item(row_index, 0)
            row_target_id = (
                str(row_item.data(Qt.ItemDataRole.UserRole) or "")
                if row_item is not None
                else ""
            )
            is_active = bool(active_target_id and row_target_id == active_target_id)
            row = self.unit_rows.get(row_target_id) or {}
            unit = "小节" if row.get("target_kind") == "subsection" else "节"
            for column in range(self.materials_table.columnCount()):
                item = self.materials_table.item(row_index, column)
                if item is None:
                    continue
                item.setBackground(QBrush(QColor("#DDF5E5")) if is_active else QBrush())
                item.setForeground(QBrush(QColor("#14532D")) if is_active else QBrush())
                font = item.font()
                font.setBold(is_active)
                item.setFont(font)
                item.setToolTip(
                    f"当前工作{unit}；材料、Agent、LaTeX 和 PDF 操作均以本行为目标。"
                    if is_active
                    else ""
                )

    def select_highlighted_material(self) -> bool:
        target_id = self._highlighted_material_id()
        if not target_id or target_id not in self.unit_rows:
            QMessageBox.information(self, "尚未选择", "请先单击一个节或小节。")
            return False
        self.current_target_id = target_id
        self._update_target_controls()
        self._update_active_material_row_style()
        self.materials_table.clearSelection()
        self.materials_table.setCurrentCell(-1, -1)
        self.material_confirmed_target_id = target_id
        self._on_material_selection_changed()
        row = self.unit_rows[target_id]
        unit = "小节" if row.get("target_kind") == "subsection" else "节"
        self.status_requested.emit(f"已选中当前{unit}：{row['number']}  {row['title']}。")
        return True

    def _run_for_highlighted(self, callback: Callable[[], None]) -> None:
        if self._highlighted_material_id():
            QMessageBox.information(
                self,
                "尚未确认工作单元",
                "蓝色行只表示高亮。请先点击最左侧的“选中本单位”。",
            )
            return
        if (
            self.current_target_id not in self.unit_rows
            or self.material_confirmed_target_id != self.current_target_id
        ):
            QMessageBox.information(
                self,
                "尚未确认工作单元",
                "请先高亮一个节或小节，再点击最左侧的“选中本单位”。",
            )
            return
        callback()

    def preview_selected_outline_item(self, *_args: Any) -> None:
        items = self.outline_tree.selectedItems()
        if not items:
            QMessageBox.information(self, "尚未选择", "请先选择一个节或小节目录项。")
            return
        row = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(row, dict) or row.get("kind") == "chapter":
            items[0].setExpanded(not items[0].isExpanded())
            return
        path = Path(str(row.get("agent_markdown") or ""))
        if not path.is_file():
            QMessageBox.information(
                self,
                "Agent 校正版尚不存在",
                "当前节还没有 Agent corrected.md，无法按实际标题跳转预览。",
            )
            return
        self._request_markdown_document(path, int(row.get("source_line") or 1))

    def show_materials_dialog(self) -> None:
        # Closing this auxiliary window must not forget the explicitly confirmed
        # green work unit.  A blue row, project change, or granularity change
        # still clears the confirmation through their dedicated handlers.
        self.refresh_outline()
        self.materials_dialog.show()
        self.materials_dialog.raise_()
        self.materials_dialog.activateWindow()

    def show_outline_dialog(self) -> None:
        self.refresh_outline()
        self.outline_dialog.show()
        self.outline_dialog.raise_()
        self.outline_dialog.activateWindow()

    def refresh_tex_refinement_units(self) -> None:
        self.tex_refinement_table.setRowCount(0)
        self.refinement_rows: dict[str, dict[str, Any]] = {}
        if not self.current_project_code:
            return
        try:
            rows = self.service.latex_refinement_units(self.current_project_code)
        except (OSError, RuntimeError, ValueError) as error:
            self.append_process_log(f"[失败] 读取 TeX 实际最小精度：{error}")
            return
        self.tex_refinement_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            refinement_id = str(row["refinement_id"])
            self.refinement_rows[refinement_id] = row
            values = [
                str(row.get("number") or ""),
                str(row.get("title") or ""),
                str(row.get("unit_kind") or ""),
                str(row.get("relative_path") or ""),
                f"{int(row.get('start_line') or 1)}–{int(row.get('end_line') or 1)}",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, refinement_id)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.tex_refinement_table.setItem(row_index, column, item)

    def show_tex_refinement_dialog(self) -> None:
        if not self.current_project_code:
            QMessageBox.information(self, "尚未选择译本", "请先选择一个译本项目。")
            return
        self.refresh_tex_refinement_units()
        self.tex_refinement_dialog.show()
        self.tex_refinement_dialog.raise_()
        self.tex_refinement_dialog.activateWindow()

    def _selected_refinement_id(self) -> str:
        selected = self.tex_refinement_table.selectionModel().selectedRows()
        row_index = (
            int(selected[0].row())
            if selected
            else self.tex_refinement_table.currentRow()
        )
        if row_index < 0:
            return ""
        item = self.tex_refinement_table.item(row_index, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def open_selected_tex_refinement(self) -> None:
        refinement_id = self._selected_refinement_id()
        if not refinement_id:
            QMessageBox.information(self, "尚未选择", "请先选择一个实际最小 TeX 单位。")
            return
        try:
            payload = self.service.latex_refinement_payload(
                self.current_project_code,
                refinement_id,
            )
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(self, "无法打开 TeX 精修", str(error))
            return
        self._open_tex_refinement_workbench(payload)

    def _open_tex_refinement_workbench(self, payload: dict[str, Any]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(
            f"数学译本 TeX 精修 - {payload['number']} {payload['title']}"
        )
        dialog.resize(1460, 900)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QFrame()
        left.setObjectName("glassPanel")
        left_layout = QVBoxLayout(left)
        left_title = QLabel(
            f"{payload['number']} {payload['title']} - 实际最小单位 TeX"
        )
        left_title.setObjectName("sectionTitle")
        _font(left_title, 12, QFont.Weight.DemiBold)
        left_layout.addWidget(left_title)
        editor = QTextEdit()
        editor.setObjectName("softText")
        editor.setAcceptRichText(False)
        editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        editor.setPlainText(str(payload["latex_source"]))
        left_layout.addWidget(editor, 1)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("glassPanel")
        right_layout = QVBoxLayout(right)
        right_title = QLabel("当前实际最小单位 PDF 预览")
        right_title.setObjectName("sectionTitle")
        _font(right_title, 12, QFont.Weight.DemiBold)
        right_layout.addWidget(right_title)
        pdf_document = QPdfDocument(dialog)
        pdf_view = QPdfView()
        pdf_view.setDocument(pdf_document)
        pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        right_layout.addWidget(pdf_view, 1)
        compile_log = QTextEdit()
        compile_log.setObjectName("softText")
        compile_log.setReadOnly(True)
        compile_log.setAcceptRichText(False)
        compile_log.setMaximumHeight(160)
        right_layout.addWidget(compile_log)
        splitter.addWidget(right)
        splitter.setSizes([650, 790])
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        reload_button = QPushButton("重新载入已保存稿")
        compile_button = QPushButton("编译预览")
        save_button = QPushButton("保存实际最小单位精修稿")
        formal_pdf_button = QPushButton("定位正式 PDF")
        close_button = QPushButton("关闭")
        for button, primary in (
            (reload_button, False),
            (compile_button, False),
            (save_button, True),
            (formal_pdf_button, False),
            (close_button, False),
        ):
            button.setObjectName("primaryButton" if primary else "secondaryButton")
            button.setFixedHeight(36)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        def append_preview_log(message: str) -> None:
            clean = str(message or "").rstrip()
            if not clean:
                return
            compile_log.append(clean)
            compile_log.verticalScrollBar().setValue(
                compile_log.verticalScrollBar().maximum()
            )
            self.append_process_log(f"[TeX 精修] {clean}")

        def compile_preview() -> None:
            compile_log.clear()
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                result = self.service.compile_latex_refinement_preview(
                    self.current_project_code,
                    str(payload["refinement_id"]),
                    editor.toPlainText(),
                    emit=append_preview_log,
                )
                status = pdf_document.load(str(result["pdf_path"]))
                if status != QPdfDocument.Error.None_:
                    raise RuntimeError(f"PDF 预览加载失败：{status}")
                pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
                pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            except (OSError, RuntimeError, ValueError) as error:
                append_preview_log(str(error))
                QMessageBox.warning(dialog, "TeX 预览编译失败", str(error))
            finally:
                QApplication.restoreOverrideCursor()

        def reload_saved() -> None:
            try:
                current = self.service.latex_refinement_payload(
                    self.current_project_code,
                    str(payload["refinement_id"]),
                )
                editor.setPlainText(str(current["latex_source"]))
                compile_log.setPlainText("已重新载入正式保存稿。")
            except (OSError, RuntimeError, ValueError) as error:
                QMessageBox.warning(dialog, "重新载入失败", str(error))

        def save_refinement() -> None:
            if not self._confirm(
                "保存 TeX 精修稿",
                f"只替换实际最小单位 {payload['number']} 的源码范围；原文件会先备份。继续吗？",
            ):
                return
            try:
                result = self.service.save_latex_refinement(
                    self.current_project_code,
                    str(payload["refinement_id"]),
                    editor.toPlainText(),
                    emit=append_preview_log,
                )
            except (OSError, RuntimeError, ValueError) as error:
                QMessageBox.critical(dialog, "保存 TeX 精修稿失败", str(error))
                return
            self.append_process_log(f"[TeX 精修] 已保存：{result['source_path']}")
            self.refresh_outline()
            self.refresh_tex_refinement_units()
            dialog.accept()

        def locate_refinement_pdf() -> None:
            try:
                location = self.service.formal_pdf_location_for_refinement(
                    self.current_project_code,
                    str(payload["refinement_id"]),
                )
            except (OSError, RuntimeError, ValueError) as error:
                QMessageBox.information(dialog, "无法定位正式 PDF", str(error))
                return
            self.pdf_open_requested.emit(
                str(location["pdf_path"]),
                int(location["page_index"]),
                f"{payload['number']}  {payload['title']}".strip(),
            )

        reload_button.clicked.connect(reload_saved)
        compile_button.clicked.connect(compile_preview)
        save_button.clicked.connect(save_refinement)
        formal_pdf_button.clicked.connect(locate_refinement_pdf)
        close_button.clicked.connect(dialog.accept)
        dialog.exec()

    def _update_target_controls(self) -> None:
        row = self.unit_rows.get(self.current_target_id)
        enabled = row is not None
        target_busy = bool(
            row is not None
            and (
                str(row.get("section_id") or "") in self._running_agent_sections
                or self.current_target_id in self._running_package_targets
                or self.current_project_code in self._running_latex_projects
            )
        )
        for button in self.target_buttons:
            button.setEnabled(enabled and not target_busy)
        for button in self.project_buttons:
            button.setEnabled(bool(self.current_project_code))
        if hasattr(self, "mineru_button"):
            project = self.project_rows.get(self.current_project_code) or {}
            running_mineru = self.current_project_code in self._running_mineru_projects
            self.mineru_button.setText(
                "重新运行 MinerU" if project.get("mineru") else "运行 MinerU"
            )
            self.mineru_button.setEnabled(bool(self.current_project_code) and not running_mineru)
        if hasattr(self, "second_layer_button"):
            self.second_layer_button.setText(
                "重新运行第二层 Agent"
                if row is not None and str(row.get("agent_status") or "") == "完成"
                else "运行第二层 Agent"
            )
            if row is not None and str(row.get("section_id") or "") in self._running_agent_sections:
                self.second_layer_button.setEnabled(False)
        if row is None:
            self.target_label.setText("尚未选择工作单元。")
            return
        unit = "小节" if row["target_kind"] == "subsection" else "节"
        self.target_label.setText(
            f"当前{unit}：{row['number']}  {row['title']} · "
            f"MinerU {row['mineru_status']} · Agent {row['agent_status']} · "
            f"图片 {row['image_status']} · 材料包 {row['package_status']} · LaTeX {row['latex_status']}"
        )

    def _paths(self) -> dict[str, Path] | None:
        if not self.current_project_code or not self.current_target_id:
            QMessageBox.information(self, "尚未选择", "请在“分集与材料”中选择一个节或小节。")
            return None
        try:
            return self.service.target_paths(self.current_project_code, self.current_target_id)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, "读取工作单元失败", str(error))
            return None

    @staticmethod
    def _open_path(path: Path) -> bool:
        if not path.exists():
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))))

    def _open_required(self, key: str, title: str) -> None:
        paths = self._paths()
        if paths is None:
            return
        path = paths[key]
        if not self._open_path(path):
            QMessageBox.information(self, title, f"文件尚不存在：\n{path}")

    def reveal_original_pdf(self) -> None:
        if not self.current_project_code:
            QMessageBox.information(self, "尚未选择译本", "请先选择一个译本项目。")
            return
        try:
            project = self.service.project(self.current_project_code)
            project_dir = self.service._project_dir(self.current_project_code)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, "读取原书路径失败", str(error))
            return
        source = (project_dir / str(project.get("source_pdf") or "")).resolve()
        if not source.is_file():
            QMessageBox.information(
                self,
                "工程原书不存在",
                f"译本工程内保存的原书副本不存在：\n{source}",
            )
            return
        launch_result = QProcess.startDetached(
            "explorer.exe",
            [f"/select,{QDir.toNativeSeparators(str(source))}"],
        )
        started = bool(launch_result[0]) if isinstance(launch_result, tuple) else bool(launch_result)
        if not started:
            QMessageBox.warning(self, "打开文件资源管理器失败", f"无法选中原书：\n{source}")
            return
        self.append_process_log(f"[查看原书] 已在文件资源管理器中选中：{source}")
        self.status_requested.emit(f"已在文件资源管理器中选中原书：{source.name}")

    def open_chapter_pdf(self) -> None:
        self._open_required("chapter_pdf", "章级 PDF")

    def open_images(self) -> None:
        self._open_required("images_dir", "图片目录")

    def reveal_material_package(self) -> None:
        paths = self._paths()
        if paths is None:
            return
        package = paths["package"].resolve()
        if not package.is_file():
            QMessageBox.information(
                self,
                "材料包尚未生成",
                f"当前工作单元还没有材料压缩包：\n{package}",
            )
            return
        if not _reveal_in_reusable_explorer(package):
            QMessageBox.warning(
                self,
                "打开文件资源管理器失败",
                f"无法在文件资源管理器中选中材料包：\n{package}",
            )
            return
        self.append_process_log(
            f"[材料包位置] 已在文件资源管理器中选中：{package}"
        )
        self.status_requested.emit(
            f"已在文件资源管理器中选中材料包：{package.name}"
        )

    def open_latex(self) -> None:
        self._open_required("latex", "LaTeX")

    def reveal_formal_pdf(self) -> None:
        if not self.current_project_code:
            QMessageBox.information(self, "尚未选择译本", "请先选择一个译本项目。")
            return
        try:
            pdf_path = self.service.formal_pdf_path(self.current_project_code).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, "读取正式译本 PDF 失败", str(error))
            return
        if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
            QMessageBox.information(
                self,
                "正式译本 PDF 尚未生成",
                f"当前译本还没有可用的正式 PDF：\n{pdf_path}",
            )
            return
        if not _reveal_in_reusable_explorer(pdf_path):
            QMessageBox.warning(
                self,
                "打开文件资源管理器失败",
                f"无法在文件资源管理器中选中正式译本 PDF：\n{pdf_path}",
            )
            return
        self.append_process_log(
            f"[正式 PDF 位置] 已在文件资源管理器中选中：{pdf_path}"
        )
        self.status_requested.emit(
            f"已在文件资源管理器中选中正式译本 PDF：{pdf_path.name}"
        )

    def locate_formal_pdf(self) -> None:
        if not self.current_project_code:
            QMessageBox.information(self, "尚未选择译本", "请先选择一个译本项目。")
            return
        if not self.current_target_id:
            QMessageBox.information(
                self,
                "尚未选择工作单元",
                "请先在“分集与材料”中选中一个节或小节，再定位正式 PDF。",
            )
            return
        try:
            location = self.service.formal_pdf_location(
                self.current_project_code,
                self.current_target_id,
            )
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.information(
                self,
                "无法定位正式译本 PDF",
                f"{error}\n\n“定位到 PDF”不会退回原书或章级证据 PDF。",
            )
            return
        title = f"{location['number']}  {location['title']}".strip()
        self.pdf_open_requested.emit(
            str(location["pdf_path"]),
            int(location["page_index"]),
            title,
        )

    def _target_agent_source_line(self) -> int:
        if not self.current_project_code or not self.current_target_id:
            return 1
        try:
            outline = self.service.outline_nodes(self.current_project_code)
        except (OSError, RuntimeError, ValueError):
            return 1
        for chapter in outline:
            for section in chapter.get("children") or []:
                if str(section.get("id") or "") == self.current_target_id:
                    return max(1, int(section.get("source_line") or 1))
                for subsection in section.get("children") or []:
                    if str(subsection.get("id") or "") == self.current_target_id:
                        return max(1, int(subsection.get("source_line") or 1))
        return 1

    def _open_markdown_key(
        self,
        key: str,
        title: str,
        *,
        source_line: int = 1,
    ) -> None:
        paths = self._paths()
        if paths is None:
            return
        path = paths[key]
        if not path.is_file():
            QMessageBox.information(self, title, f"文件尚不存在：\n{path}")
            return
        self._request_markdown_document(path, source_line)

    def _request_markdown_document(
        self,
        path: str | Path,
        source_line: int = 1,
    ) -> None:
        target = Path(path).resolve()
        if not target.is_file():
            QMessageBox.information(self, "Markdown", f"文件尚不存在：\n{target}")
            return
        self.markdown_open_requested.emit(str(target), max(1, int(source_line)))

    def open_mineru_markdown(self) -> None:
        self._open_markdown_key("mineru_markdown", "MinerU Markdown")

    def prepare_second_layer(self) -> None:
        paths = self._paths()
        if paths is None:
            return
        row = self.unit_rows.get(self.current_target_id)
        if row is None:
            return
        try:
            project = self.service.project(self.current_project_code)
            chapter, section = self.service._find_section(
                project, str(row["section_id"])
            )
            raw_path = paths["mineru_markdown"]
            manifest_path = paths["images_dir"] / "manifest.json"
            if not raw_path.is_file() or not manifest_path.is_file():
                raise RuntimeError("当前节缺少 MinerU Markdown 或图片清单。")
            raw_markdown = raw_path.read_text(encoding="utf-8")
            reference_index = self.service._translation_reference_index(
                project,
                self.service._project_dir(self.current_project_code),
            )
            bundle = build_second_layer_prompt(
                project,
                chapter,
                section,
                raw_markdown,
                json.loads(manifest_path.read_text(encoding="utf-8")),
                self.service._agent_reference_candidates(
                    reference_index,
                    raw_markdown,
                    str(row["section_id"]),
                ),
                self.service._deterministic_reference_mentions(
                    raw_markdown,
                    reference_index,
                ),
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            self.append_process_log(f"[失败] 准备第二层 Agent：{error}")
            QMessageBox.warning(self, "准备第二层 Agent 失败", str(error))
            return
        self.append_process_log(
            f"[第二层 Agent] 已生成完整 section 提示词：{row['number']} {row['title']}；"
            f"稳定前缀 revision={bundle.upstream_revision}，sha256={bundle.stable_prefix_sha256}"
        )
        self.append_process_log(
            "[第二层 Agent] 当前界面只负责准备锁定提示词和附件清单；"
            "模型返回后请用“导入 Agent 校正版”写回 corrected.md/report.md。"
        )
        dialog = QDialog(self)
        dialog.setWindowTitle(f"第二层 Agent 提示词 - {row['number']} {row['title']}")
        dialog.resize(1100, 760)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "稳定前缀应放在 provider 缓存区；当前节任务与附件清单如下。此处不执行模型调用。"
        )
        note.setObjectName("pageNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(bundle.task_prompt)
        layout.addWidget(editor, 1)
        buttons = QHBoxLayout()
        copy_button = QPushButton("复制任务提示词")
        copy_button.setObjectName("primaryButton")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(bundle.task_prompt))
        close_button = QPushButton("关闭")
        close_button.setObjectName("secondaryButton")
        close_button.clicked.connect(dialog.accept)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        dialog.exec()

    def open_corrected_markdown(self) -> None:
        paths = self._paths()
        if paths is None:
            return
        corrected = paths["corrected_markdown"]
        if not corrected.is_file():
            QMessageBox.information(
                self,
                "Agent 校正版尚不存在",
                "请先运行第二层 Agent。运行完成后会自动保存 corrected.md 和 report.md。",
            )
            return
        image_items: list[dict[str, str]] = []
        row = self.unit_rows.get(self.current_target_id) or {}
        manifest_path = paths["images_dir"] / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                project = self.service.project(self.current_project_code)
                _chapter, section = self.service._find_section(
                    project, str(row.get("section_id") or "")
                )
                figure_ids = set(section.get("figure_ids") or [])
                audit_paths: set[str] = set()
                for item in manifest.get("figures") or []:
                    if str(item.get("figure_id") or "") not in figure_ids:
                        continue
                    image_path = str(item.get("path") or "").strip()
                    if image_path:
                        image_items.append(
                            {
                                "label": " ".join(
                                    str(value)
                                    for value in (
                                        item.get("captions") or [item.get("figure_id")]
                                    )
                                    if str(value).strip()
                                ),
                                "path": image_path,
                            }
                        )
                    audit_path = str(item.get("audit_overlay") or "").strip()
                    if audit_path and audit_path not in audit_paths:
                        audit_paths.add(audit_path)
                        image_items.append(
                            {
                                "label": (
                                    f"原书第 {int(item.get('page_number') or 0)} 页 · "
                                    "MinerU layout audit"
                                ),
                                "path": audit_path,
                            }
                        )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                image_items = []
        documents = {
            "agent": {
                "label": "Agent 校正版",
                "path": str(corrected.resolve()),
                "kind": "markdown",
            },
            "mineru": {
                "label": "MinerU 原始版",
                "path": str(paths["mineru_markdown"].resolve()),
                "kind": "markdown",
            },
            "images": {
                "label": "图片",
                "path": str(paths["project_dir"].resolve()),
                "kind": "image_gallery",
                "items": image_items,
            },
        }
        if paths["agent_report"].is_file():
            documents["report"] = {
                "label": "Agent 报告",
                "path": str(paths["agent_report"].resolve()),
                "kind": "markdown",
            }
        self.markdown_bundle_open_requested.emit(
            documents,
            "agent",
            self._target_agent_source_line(),
        )

    def run_second_layer_agent(self) -> None:
        row = self.unit_rows.get(self.current_target_id)
        if row is None:
            return
        project_code = self.current_project_code
        section_id = str(row["section_id"])
        if section_id in self._running_agent_sections:
            QMessageBox.information(self, "第二层 Agent 正在运行", "当前 section 已经在处理中。")
            return
        rerun = str(row.get("agent_status") or "") == "完成"
        action = "重新运行" if rerun else "运行"
        if not self._confirm(
            f"{action}第二层 Agent",
            f"将使用当前 AI 助手 API 配置处理完整 section {row['section_number']}。"
            "这是一次付费模型调用；返回后会自动备份并写入 corrected.md/report.md。继续吗？",
        ):
            return
        project = self.project_rows.get(project_code) or self.service.project(project_code)
        diagnostics = get_crash_diagnostics()
        run_logger = diagnostics.start_pipeline_run(
            pipeline="translation",
            project_id=project_code,
            project_name=str(project.get("title") or ""),
            operation="second_layer_agent",
        )
        self.append_process_log(
            f"[第二层 Agent] {action}完整 section：{row['section_number']} {row['section_title']}"
        )
        self.append_process_log(f"[第二层持久日志] {run_logger.log_path}")

        def task(ui_emit: Callable[[str], None]) -> dict[str, Any]:
            def persist_and_emit(message: str) -> None:
                run_logger.emit(message)
                ui_emit(message)

            return self.service.run_second_layer_agent(
                project_code,
                section_id,
                emit=persist_and_emit,
            )

        worker = _TranslationStreamingWorker(
            task,
            operation="second_layer_agent",
            project_id=project_code,
            stage="agent_processing",
            run_logger=run_logger,
        )
        worker.setAutoDelete(False)
        self._active_workers.add(worker)
        self._running_agent_sections.add(section_id)
        for button in self.target_buttons:
            button.setEnabled(False)

        def release() -> None:
            self._active_workers.discard(worker)
            self._running_agent_sections.discard(section_id)
            self.refresh_outline()

        def finished(result: object) -> None:
            try:
                release()
                data = result if isinstance(result, dict) else {}
                self.append_process_log(
                    f"[第二层 Agent 完成] corrected.md 已写后回读："
                    f"{data.get('corrected_markdown') or ''}"
                )
                self.status_requested.emit(
                    f"第二层 Agent 已完成 section {row['section_number']}。"
                )
                if self.current_project_code == project_code:
                    self.open_corrected_markdown()
                run_logger.finish("第二层 Agent 与完成后界面刷新均已完成。")
            except BaseException as error:
                traceback_text = traceback.format_exc()
                run_logger.fail(f"第二层完成回调失败：{error}", traceback_text)
                diagnostics.record_exception(
                    type(error),
                    error,
                    error.__traceback__,
                    kind="translation_second_layer_completion_callback_exception",
                    fatal=False,
                )
                self.append_process_log(f"[第二层完成回调失败] {error}")
                QMessageBox.warning(self, "第二层界面刷新失败", str(error))

        def failed(failure: object) -> None:
            data = failure if isinstance(failure, dict) else {"message": str(failure)}
            message = str(data.get("message") or "未知错误")
            release()
            self.append_process_log(f"[第二层 Agent 失败] {message}")
            QMessageBox.warning(
                self,
                "第二层 Agent 失败",
                f"{message}\n\n持久日志：{run_logger.log_path}",
            )

        worker.signals.progress.connect(self.append_process_log)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.thread_pool.start(worker)

    def import_agent_result(self) -> None:
        row = self.unit_rows.get(self.current_target_id)
        if row is None:
            return
        corrected_path, _selected = QFileDialog.getOpenFileName(
            self, "选择 Agent 输出的 corrected.md", "", "Markdown (*.md)"
        )
        if not corrected_path:
            return
        corrected_file = Path(corrected_path)
        report_file = corrected_file.with_name("report.md")
        try:
            corrected = corrected_file.read_text(encoding="utf-8")
            report = report_file.read_text(encoding="utf-8") if report_file.is_file() else ""
        except OSError as error:
            QMessageBox.warning(self, "读取 Agent 结果失败", str(error))
            return
        if not self._confirm(
            "导入 Agent 校正版",
            f"将把完整 {row['section_number']} section 校正版写入正式译本项目；"
            "若已有版本会先保留备份。继续吗？",
        ):
            return
        try:
            result = self.service.save_agent_result(
                self.current_project_code,
                str(row["section_id"]),
                corrected,
                report,
                emit=self.append_process_log,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.append_process_log(f"[失败] 导入第二层 Agent 校正版：{error}")
            QMessageBox.warning(self, "导入 Agent 结果失败", str(error))
            return
        self.refresh_outline()
        self._request_markdown_document(
            result["corrected_markdown"], self._target_agent_source_line()
        )
        self.status_requested.emit("Agent 完整节级校正版已导入；小节模式不会重复调用 Agent。")

    def generate_package(self) -> None:
        if not self.current_target_id:
            return
        project_code = self.current_project_code
        target_id = self.current_target_id
        row = self.unit_rows.get(target_id)
        if row is None:
            return
        if target_id in self._running_package_targets:
            QMessageBox.information(self, "材料包正在生成", "当前工作单元已经在处理中。")
            return
        try:
            preflight = self.service.material_package_preflight(project_code, target_id)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, "材料包前置检查失败", str(error))
            return
        requires_agent = bool(preflight.get("requires_second_layer"))
        section_id = str(preflight.get("section_id") or row.get("section_id") or "")
        if section_id in self._running_agent_sections:
            QMessageBox.information(self, "第二层 Agent 正在运行", "当前 section 已经在处理中。")
            return
        if requires_agent and not self._confirm(
            "运行第二层 Agent 并生成材料包",
            f"{preflight.get('reason') or '当前第二层产物需要刷新'}。\n\n"
            f"“生成材料包”将先使用当前 AI 助手 API 配置处理完整 section "
            f"{preflight.get('section_number') or ''}，由 Agent 检查并写入全部英文标签，"
            "随后自动继续生成所选材料包。这是一次付费模型调用；继续吗？",
        ):
            return
        project = self.project_rows.get(project_code) or self.service.project(project_code)
        diagnostics = get_crash_diagnostics()
        run_logger = diagnostics.start_pipeline_run(
            pipeline="translation",
            project_id=project_code,
            project_name=str(project.get("title") or ""),
            operation="material_package_pipeline",
        )
        self.append_process_log(
            f"[材料包] {'自动运行第二层 Agent 后生成' if requires_agent else '直接生成'}："
            f"{row['number']} {row['title']}"
        )
        self.append_process_log(f"[材料包持久日志] {run_logger.log_path}")

        def task(ui_emit: Callable[[str], None]) -> Path:
            def persist_and_emit(message: str) -> None:
                run_logger.emit(message)
                ui_emit(message)

            return self.service.generate_material_package(
                project_code,
                target_id,
                emit=persist_and_emit,
            )

        worker = _TranslationStreamingWorker(
            task,
            operation="material_package_pipeline",
            project_id=project_code,
            stage="agent_and_package" if requires_agent else "package_generation",
            run_logger=run_logger,
        )
        worker.setAutoDelete(False)
        self._active_workers.add(worker)
        self._running_package_targets.add(target_id)
        if requires_agent:
            self._running_agent_sections.add(section_id)
        self._update_target_controls()

        def release() -> None:
            self._active_workers.discard(worker)
            self._running_package_targets.discard(target_id)
            if requires_agent:
                self._running_agent_sections.discard(section_id)
            self.refresh_outline()

        def finished(result: object) -> None:
            package = Path(result)
            release()
            self.append_process_log(f"[材料包完成] 已写后回读：{package}")
            self.status_requested.emit(f"ChatGPT 材料包已生成：{package.name}")
            run_logger.finish("第二层标签审计（如需）与材料包生成均已完成。")

        def failed(failure: object) -> None:
            data = failure if isinstance(failure, dict) else {"message": str(failure)}
            message = str(data.get("message") or "未知错误")
            release()
            self.append_process_log(f"[失败] 生成最小单位材料包：{message}")
            QMessageBox.warning(
                self,
                "生成材料包失败",
                f"{message}\n\n持久日志：{run_logger.log_path}",
            )

        worker.signals.progress.connect(self.append_process_log)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.thread_pool.start(worker)

    def import_chatgpt_latex(self) -> None:
        row = self.unit_rows.get(self.current_target_id)
        if row is None:
            return
        if self.current_project_code in self._running_latex_projects:
            QMessageBox.information(
                self,
                "正式译本正在编译",
                "当前译本已有一个导入/编译任务正在后台运行，请等待处理日志显示完成。",
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"导入 {row['number']} ChatGPT LaTeX")
        dialog.resize(980, 700)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            f"只粘贴 {row['number']} 的 LaTeX 正文。"
            + (
                "当前为小节模式；导入会保存为独立、可寻址的小节文件，不覆盖兄弟小节。"
                if row["target_kind"] == "subsection"
                else "当前为节模式；导入范围是完整 section（含其所有 subsections）。"
            )
        )
        note.setWordWrap(True)
        note.setObjectName("pageNote")
        layout.addWidget(note)
        editor = QTextEdit()
        editor.setAcceptRichText(False)
        editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(editor, 1)
        buttons = QHBoxLayout()
        import_button = QPushButton("导入")
        import_button.setObjectName("primaryButton")
        close_button = QPushButton("取消")
        close_button.setObjectName("secondaryButton")
        buttons.addWidget(import_button)
        buttons.addWidget(close_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        def do_import() -> None:
            source = editor.toPlainText().strip()
            if not source:
                QMessageBox.information(dialog, "没有 LaTeX", "请先粘贴 ChatGPT 返回的正文。")
                return
            project_code = str(self.current_project_code)
            target_id = str(self.current_target_id)
            row_snapshot = dict(row)
            if project_code in self._running_latex_projects:
                QMessageBox.information(
                    dialog,
                    "正式译本正在编译",
                    "当前译本已有一个导入/编译任务正在后台运行。",
                )
                return
            if not self._confirm(
                "导入 ChatGPT LaTeX",
                f"只写入当前 {row_snapshot['target_kind']} {row_snapshot['number']}；已有文件会先备份。"
                "导入后将从头编译截至当前的全部译本 TeX。继续吗？",
            ):
                return

            project = self.project_rows.get(project_code) or self.service.project(project_code)
            diagnostics = get_crash_diagnostics()
            run_logger = diagnostics.start_pipeline_run(
                pipeline="translation",
                project_id=project_code,
                project_name=str(project.get("title") or ""),
                operation="translation_formal_compile",
            )
            self.append_process_log(
                f"[LaTeX 后台导入] {row_snapshot['number']} {row_snapshot['title']}；"
                "保存后将从头编译截至当前的正式译本。"
            )
            self.append_process_log(f"[LaTeX 编译持久日志] {run_logger.log_path}")

            def task(ui_emit: Callable[[str], None]) -> dict[str, Any]:
                def persist_and_emit(message: str) -> None:
                    run_logger.emit(message)
                    ui_emit(message)

                return self.service.import_latex_and_compile(
                    project_code,
                    target_id,
                    source,
                    emit=persist_and_emit,
                )

            worker = _TranslationStreamingWorker(
                task,
                operation="translation_formal_compile",
                project_id=project_code,
                stage="latex_import_and_compile",
                run_logger=run_logger,
            )
            worker.setAutoDelete(False)
            self._active_workers.add(worker)
            self._running_latex_projects.add(project_code)
            self._update_target_controls()
            self._on_material_selection_changed()

            def release() -> None:
                self._active_workers.discard(worker)
                self._running_latex_projects.discard(project_code)
                self.refresh_outline()
                self._update_target_controls()
                self._on_material_selection_changed()

            def finished(result: object) -> None:
                data = result if isinstance(result, dict) else {}
                release()
                self.append_process_log(
                    f"[LaTeX] 已导入并从头编译实际目标 "
                    f"{row_snapshot['number']} {row_snapshot['title']}："
                    f"{data.get('latex_path') or ''} -> {data.get('pdf_path') or ''}"
                )
                self.status_requested.emit(
                    f"已导入并编译 {row_snapshot['number']}："
                    f"{Path(str(data.get('pdf_path') or '')).name}"
                )
                run_logger.finish("ChatGPT LaTeX 已导入，正式译本已从头编译并写后回读。")
                QMessageBox.information(
                    self,
                    "导入并编译成功",
                    f"{row_snapshot['number']} 已导入。\n\n"
                    f"已从头编译 {len(data.get('source_paths') or [])} 个已导入最小单位，"
                    f"正式译本共 {int(data.get('page_count') or 0)} 页。\n\n"
                    f"PDF：{data.get('pdf_path') or ''}\n"
                    f"日志：{data.get('log_path') or ''}",
                )

            def failed(failure: object) -> None:
                data = failure if isinstance(failure, dict) else {"message": str(failure)}
                message = str(data.get("message") or "未知错误")
                release()
                self.append_process_log(f"[失败] 导入 ChatGPT LaTeX：{message}")
                QMessageBox.critical(self, "导入后编译失败", message)

            worker.signals.progress.connect(self.append_process_log)
            worker.signals.finished.connect(finished)
            worker.signals.failed.connect(failed)
            try:
                self.thread_pool.start(worker)
            except Exception as error:
                release()
                run_logger.fail(f"无法启动 LaTeX 后台编译任务：{error}", traceback.format_exc())
                self.append_process_log(f"[失败] 无法启动 LaTeX 后台编译任务：{error}")
                QMessageBox.critical(dialog, "无法启动后台编译", str(error))
                return
            dialog.accept()
            self.status_requested.emit(
                f"{row_snapshot['number']} 已开始后台导入并编译；可在处理日志查看进度。"
            )

        import_button.clicked.connect(do_import)
        close_button.clicked.connect(dialog.reject)
        dialog.exec()

    def open_adapter_skill(self) -> None:
        path = APP_PATHS.application_root / "shared" / "skills" / "mathtranslations_source_repair" / "SKILL.md"
        self._request_markdown_document(path)

    def open_upstream_skill(self) -> None:
        path = APP_PATHS.application_root / "shared" / "skills" / "vendor" / "mathtranslations" / "SKILL.md"
        self._request_markdown_document(path)


__all__ = ["TranslationProjectPage"]
