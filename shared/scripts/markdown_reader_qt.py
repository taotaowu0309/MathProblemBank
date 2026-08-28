from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QKeySequence, QPainter, QShortcut, QTextCursor, QTextFormat
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception:
    QWebEngineSettings = None
    QWebEngineView = None

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.markdown_renderer import MarkdownHeading, MarkdownRenderResult, compile_markdown, pygments_stylesheet


ROOT_DIR = Path(__file__).resolve().parents[2]
MARKDOWN_ASSET_DIR = ROOT_DIR / "shared" / "ui" / "assets" / "markdown"
DEFAULT_MARKDOWN_DRAFT_PATH = APP_PATHS.cache_dir / "markdown_reader_draft.md"


def read_markdown_draft(path: str | Path) -> str:
    target = Path(path)
    return target.read_text(encoding="utf-8") if target.is_file() else ""


def write_markdown_draft(path: str | Path, source: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(str(source), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


_PREVIEW_SCRIPT = r"""
let markdownBridge = null;
let sourceDrivenUntil = 0;
let scrollTimer = null;
let sourceHighlightTimer = null;

function sourceElements() {
    return Array.from(document.querySelectorAll('[data-source-start]'));
}

function sourceElementForLine(line) {
    const matches = sourceElements().filter((element) => {
        const start = Number(element.dataset.sourceStart || 0);
        const end = Number(element.dataset.sourceEnd || start);
        return start <= line && line <= end;
    });
    if (matches.length) {
        matches.sort((left, right) => {
            const leftSpan = Number(left.dataset.sourceEnd || left.dataset.sourceStart) - Number(left.dataset.sourceStart);
            const rightSpan = Number(right.dataset.sourceEnd || right.dataset.sourceStart) - Number(right.dataset.sourceStart);
            return leftSpan - rightSpan;
        });
        return matches[0];
    }
    let nearest = null;
    let distance = Number.MAX_SAFE_INTEGER;
    sourceElements().forEach((element) => {
        const current = Math.abs(Number(element.dataset.sourceStart || 0) - line);
        if (current < distance) {
            nearest = element;
            distance = current;
        }
    });
    return nearest;
}

function clearSourceHighlight() {
    document.querySelectorAll('.source-active').forEach((element) => element.classList.remove('source-active'));
    sourceHighlightTimer = null;
}

window.revealSourceLine = function(line, center) {
    const target = sourceElementForLine(Number(line));
    if (!target) return;
    if (center) {
        clearTimeout(sourceHighlightTimer);
        clearSourceHighlight();
        target.classList.add('source-active');
        sourceHighlightTimer = setTimeout(clearSourceHighlight, 5000);
    }
    sourceDrivenUntil = Date.now() + 450;
    target.scrollIntoView({block: center ? 'center' : 'nearest', behavior: 'auto'});
};

function visibleSourceLine() {
    const candidates = sourceElements().filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.bottom >= 16 && rect.top <= window.innerHeight - 16;
    });
    if (!candidates.length) return 1;
    candidates.sort((left, right) => {
        const a = Math.abs(left.getBoundingClientRect().top - 24);
        const b = Math.abs(right.getBoundingClientRect().top - 24);
        return a - b;
    });
    return Number(candidates[0].dataset.sourceStart || 1);
}

function renderMath() {
    const nodes = Array.from(document.querySelectorAll('.math-source'));
    let errors = 0;
    nodes.forEach((element) => {
        const source = element.textContent;
        if (typeof katex === 'undefined') {
            element.classList.add('math-error');
            errors += 1;
            return;
        }
        try {
            katex.render(source, element, {
                displayMode: element.dataset.display === '1',
                output: 'htmlAndMathml',
                strict: 'warn',
                throwOnError: false,
                trust: false
            });
            if (element.querySelector('.katex-error')) errors += 1;
        } catch (_error) {
            element.textContent = source;
            element.classList.add('math-error');
            errors += 1;
        }
    });
    if (markdownBridge) markdownBridge.renderCompleted(nodes.length, errors);
}

document.addEventListener('click', (event) => {
    const anchor = event.target.closest('a[href]');
    if (anchor) {
        const href = anchor.getAttribute('href') || '';
        if (!href.startsWith('#')) {
            event.preventDefault();
            if (markdownBridge) markdownBridge.openExternalUrl(anchor.href);
            return;
        }
    }
    const target = event.target.closest('[data-source-start]');
    if (target && markdownBridge) {
        markdownBridge.previewSourceRequested(Number(target.dataset.sourceStart || 1));
    }
});

window.addEventListener('scroll', () => {
    if (!markdownBridge || Date.now() < sourceDrivenUntil) return;
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
        if (Date.now() >= sourceDrivenUntil) markdownBridge.previewScrollChanged(visibleSourceLine());
    }, 120);
}, {passive: true});

new QWebChannel(qt.webChannelTransport, (channel) => {
    markdownBridge = channel.objects.markdownBridge;
    renderMath();
});
"""


_DOCUMENT_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="katex/katex.min.css">
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { scroll-behavior: auto; }
body {
    margin: 0 auto;
    max-width: 920px;
    padding: 32px 40px 96px;
    color: #17212b;
    background: #ffffff;
    font-family: "Segoe UI", "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif;
    font-size: 16px;
    line-height: 1.75;
    overflow-wrap: anywhere;
}
h1, h2, h3, h4, h5, h6 { color: #111827; line-height: 1.3; margin: 1.35em 0 0.55em; }
h1 { font-size: 2em; border-bottom: 1px solid #d8e0e8; padding-bottom: 0.28em; }
h2 { font-size: 1.55em; border-bottom: 1px solid #e4e9ef; padding-bottom: 0.24em; }
h3 { font-size: 1.28em; }
p, ul, ol, blockquote, pre, table { margin-top: 0.75em; margin-bottom: 0.75em; }
a { color: #006d77; text-decoration-thickness: 1px; text-underline-offset: 2px; }
blockquote { margin-left: 0; padding: 0.2em 1em; color: #52606d; border-left: 4px solid #8fb8b5; }
hr { border: 0; border-top: 1px solid #d8e0e8; margin: 2em 0; }
pre { padding: 14px 16px; overflow: auto; background: #f4f6f8; border: 1px solid #dce3e9; border-radius: 6px; line-height: 1.5; }
code, kbd { font-family: "Cascadia Mono", Consolas, monospace; font-size: 0.92em; }
:not(pre) > code { padding: 0.12em 0.34em; background: #eef2f5; border-radius: 4px; }
kbd { padding: 0.1em 0.34em; border: 1px solid #c8d0d8; border-bottom-width: 2px; border-radius: 4px; background: #f8fafb; }
table { width: 100%; border-collapse: collapse; display: block; overflow-x: auto; }
th, td { padding: 8px 12px; border: 1px solid #cfd8e1; text-align: left; }
th { background: #f1f5f6; font-weight: 650; }
tr:nth-child(even) td { background: #fafbfc; }
img { max-width: 100%; height: auto; }
.task-list { list-style: none; padding-left: 1.5em; }
.task-list-checkbox { width: 1em; height: 1em; margin: 0 0.45em 0 -1.35em; accent-color: #006d77; }
.math-source { font-family: "Cambria Math", serif; }
.math-display { display: block; overflow-x: auto; overflow-y: hidden; padding: 0.35em 0; }
.math-error { color: #b42318; border-bottom: 1px dotted #b42318; }
.source-block { border-radius: 3px; transition: background-color 90ms ease, box-shadow 90ms ease; }
.source-block:hover { box-shadow: inset 3px 0 0 #8fb8b5; }
.source-active { background: #eaf5f3 !important; box-shadow: inset 3px 0 0 #007b73 !important; }
@media (max-width: 720px) { body { padding: 24px 20px 72px; } }
__PYGMENTS__
</style>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script src="katex/katex.min.js"></script>
</head>
<body>
__BODY__
<script>__SCRIPT__</script>
</body>
</html>
"""


def build_markdown_document(result: MarkdownRenderResult) -> str:
    return (
        _DOCUMENT_TEMPLATE.replace("__PYGMENTS__", pygments_stylesheet())
        .replace("__BODY__", result.html)
        .replace("__SCRIPT__", _PREVIEW_SCRIPT)
    )


def build_markdown_outline_document(headings: tuple[MarkdownHeading, ...]) -> str:
    roots: list[dict[str, Any]] = []
    parents: list[tuple[int, dict[str, Any]]] = []
    for heading in headings:
        node: dict[str, Any] = {"heading": heading, "children": []}
        while parents and parents[-1][0] >= heading.level:
            parents.pop()
        if parents:
            parents[-1][1]["children"].append(node)
        else:
            roots.append(node)
        parents.append((heading.level, node))

    def render_nodes(nodes: list[dict[str, Any]]) -> str:
        parts = ["<ul>"]
        for node in nodes:
            heading = node["heading"]
            children = node["children"]
            item_class = "outline-item has-children" if children else "outline-item"
            parts.append(
                f'<li class="{item_class}">'
                f'<div class="outline-row" data-source-line="{heading.source_line}" '
                f'title="{html.escape(heading.title, quote=True)}">'
                '<button class="outline-toggle" type="button" title="展开或收起子目录" '
                'aria-label="展开或收起子目录"></button>'
                f'<span class="outline-label">{heading.content_html}</span>'
                "</div>"
            )
            if children:
                parts.append(render_nodes(children))
            parts.append("</li>")
        parts.append("</ul>")
        return "".join(parts)

    body = render_nodes(roots) if roots else '<div class="outline-empty">当前文档没有标题</div>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="katex/katex.min.css">
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; min-height: 100%; background: #ffffff; color: #243442; }}
body {{ padding: 8px 6px 18px; font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif; font-size: 14px; }}
ul {{ list-style: none; margin: 0; padding: 0 0 0 16px; }}
body > ul {{ padding-left: 0; }}
.outline-item.collapsed > ul {{ display: none; }}
.outline-row {{ display: flex; align-items: flex-start; min-height: 32px; padding: 5px 6px 5px 2px; border-radius: 4px; cursor: pointer; line-height: 1.55; }}
.outline-row:hover {{ background: #eef5f4; }}
.outline-row.selected {{ background: #d8ece9; color: #17212b; }}
.outline-toggle {{ flex: 0 0 22px; width: 22px; height: 22px; padding: 0; border: 0; background: transparent; color: #657582; cursor: pointer; }}
.outline-toggle::before {{ content: "›"; display: block; font-size: 22px; line-height: 20px; transform: rotate(90deg); transform-origin: center; }}
.outline-item.collapsed > .outline-row .outline-toggle::before {{ transform: rotate(0deg); }}
.outline-item:not(.has-children) > .outline-row .outline-toggle {{ visibility: hidden; }}
.outline-label {{ min-width: 0; overflow-wrap: anywhere; }}
.outline-label p {{ display: inline; margin: 0; }}
.outline-label code {{ padding: 1px 3px; border-radius: 3px; background: #eef2f5; font-family: "Cascadia Mono", Consolas, monospace; }}
.outline-label a {{ color: inherit; text-decoration: none; pointer-events: none; }}
.outline-label .katex {{ font-size: 1em; }}
.outline-empty {{ padding: 18px 12px; color: #657582; text-align: center; }}
</style>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script src="katex/katex.min.js"></script>
</head>
<body>
{body}
<script>
let outlineBridge = null;
function renderOutlineMath() {{
    document.querySelectorAll('.math-source').forEach((element) => {{
        try {{
            katex.render(element.textContent, element, {{
                displayMode: false,
                output: 'htmlAndMathml',
                strict: 'warn',
                throwOnError: false,
                trust: false
            }});
        }} catch (_error) {{}}
    }});
}}
document.addEventListener('click', (event) => {{
    const toggle = event.target.closest('.outline-toggle');
    if (toggle) {{
        event.preventDefault();
        event.stopPropagation();
        toggle.closest('.outline-item')?.classList.toggle('collapsed');
        return;
    }}
    const row = event.target.closest('.outline-row');
    if (!row) return;
    event.preventDefault();
    document.querySelectorAll('.outline-row.selected').forEach((item) => item.classList.remove('selected'));
    row.classList.add('selected');
    if (outlineBridge) outlineBridge.headingRequested(Number(row.dataset.sourceLine || 1));
}});
renderOutlineMath();
new QWebChannel(qt.webChannelTransport, (channel) => {{
    outlineBridge = channel.objects.outlineBridge;
}});
</script>
</body>
</html>"""


class _LineNumberArea(QWidget):
    def __init__(self, editor: "MarkdownSourceEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event: Any) -> None:  # type: ignore[override]
        self.editor.paint_line_numbers(event)


class MarkdownSourceEditor(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.line_number_area = _LineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.transient_highlight_timer = QTimer(self)
        self.transient_highlight_timer.setSingleShot(True)
        self.transient_highlight_timer.setInterval(5000)
        self.transient_highlight_timer.timeout.connect(self.clear_transient_highlight)
        self.update_line_number_area_width()
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _count: int = 0) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width()

    def resizeEvent(self, event: Any) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        contents = self.contentsRect()
        self.line_number_area.setGeometry(QRect(contents.left(), contents.top(), self.line_number_area_width(), contents.height()))

    def paint_line_numbers(self, event: Any) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#f1f4f6"))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                color = "#3f4f5f" if number == self.textCursor().blockNumber() else "#8996a3"
                painter.setPen(QColor(color))
                painter.drawText(0, top, self.line_number_area.width() - 7, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def highlight_line_temporarily(self, line: int) -> None:
        block = self.document().findBlockByLineNumber(max(0, int(line) - 1))
        if not block.isValid():
            return
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor("#edf6f5"))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = QTextCursor(block)
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])
        self.transient_highlight_timer.start()
        self.line_number_area.update()

    def clear_transient_highlight(self) -> None:
        self.setExtraSelections([])
        self.line_number_area.update()

    def current_line(self) -> int:
        return self.textCursor().blockNumber() + 1

    def first_visible_line(self) -> int:
        return self.firstVisibleBlock().blockNumber() + 1

    def reveal_line(self, line: int, *, focus: bool) -> None:
        block = self.document().findBlockByLineNumber(max(0, int(line) - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()
        if focus:
            self.highlight_line_temporarily(line)
            self.setFocus(Qt.FocusReason.OtherFocusReason)


class MarkdownPreviewBridge(QObject):
    source_requested = Signal(int, bool)
    render_completed = Signal(int, int)

    @Slot(int)
    def previewSourceRequested(self, line: int) -> None:
        self.source_requested.emit(max(1, int(line)), True)

    @Slot(int)
    def previewScrollChanged(self, line: int) -> None:
        self.source_requested.emit(max(1, int(line)), False)

    @Slot(str)
    def openExternalUrl(self, url: str) -> None:
        target = QUrl(str(url or ""))
        if target.scheme().lower() in {"http", "https", "mailto"}:
            QDesktopServices.openUrl(target)

    @Slot(int, int)
    def renderCompleted(self, math_count: int, error_count: int) -> None:
        self.render_completed.emit(max(0, int(math_count)), max(0, int(error_count)))


class MarkdownOutlineBridge(QObject):
    heading_requested = Signal(int)

    @Slot(int)
    def headingRequested(self, source_line: int) -> None:
        self.heading_requested.emit(max(1, int(source_line)))


class MarkdownReaderPage(QWidget):
    def __init__(self, parent: QWidget | None = None, *, draft_path: str | Path | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("markdownReaderPage")
        self.setMinimumHeight(600)
        self.draft_path = Path(draft_path) if draft_path is not None else DEFAULT_MARKDOWN_DRAFT_PATH
        self._last_result: MarkdownRenderResult | None = None
        self._preview_loaded = False
        self._syncing_from_preview = False
        self.outline_visible = False

        self.compile_timer = QTimer(self)
        self.compile_timer.setSingleShot(True)
        self.compile_timer.setInterval(420)
        self.compile_timer.timeout.connect(self.compile_preview)

        self.draft_save_timer = QTimer(self)
        self.draft_save_timer.setSingleShot(True)
        self.draft_save_timer.setInterval(700)
        self.draft_save_timer.timeout.connect(self.save_draft)

        self.preview_highlight_timer = QTimer(self)
        self.preview_highlight_timer.setSingleShot(True)
        self.preview_highlight_timer.setInterval(5000)
        self.preview_highlight_timer.timeout.connect(self.clear_preview_highlight)

        self.editor = MarkdownSourceEditor()
        self.editor.setObjectName("markdownSource")
        self.editor.setPlaceholderText("Markdown")
        self.editor.textChanged.connect(self.schedule_compile)
        self.editor.textChanged.connect(self.schedule_draft_save)
        self.editor.cursorPositionChanged.connect(self.sync_cursor_to_preview)
        self.editor.verticalScrollBar().valueChanged.connect(self.sync_scroll_to_preview)

        self.web_view: Any | None = None
        self.bridge: MarkdownPreviewBridge | None = None
        preview_widget = self._build_preview()

        self.outline_web_view: Any | None = None
        self.outline_bridge: MarkdownOutlineBridge | None = None
        outline_widget = self._build_outline()
        self.outline_pane = self._pane("文档目录", outline_widget)
        self.outline_pane.setMinimumWidth(220)
        self.outline_pane.setMaximumWidth(360)
        self.outline_pane.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("markdownSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._pane("Markdown 源码", self.editor))
        self.splitter.addWidget(self.outline_pane)
        self.splitter.addWidget(self._pane("编译预览", preview_widget))
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes([620, 0, 760])
        root.addWidget(self.splitter, 1)

        self.status_label = QLabel("CommonMark + GFM + 数学扩展")
        self.status_label.setObjectName("markdownStatus")
        self.line_label = QLabel("1:1")
        self.line_label.setObjectName("markdownStatus")
        status_bar = QHBoxLayout()
        status_bar.setContentsMargins(14, 7, 14, 7)
        status_bar.addWidget(self.status_label)
        status_bar.addStretch(1)
        status_bar.addWidget(self.line_label)
        status_frame = QFrame()
        status_frame.setObjectName("markdownStatusBar")
        status_frame.setLayout(status_bar)
        root.addWidget(status_frame)

        self.editor.cursorPositionChanged.connect(self.update_line_status)
        self._apply_style()
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.compile_preview)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.copy_source)
        self.load_draft()
        QTimer.singleShot(0, self.compile_preview)

    def _build_toolbar(self) -> QWidget:
        toolbar = QFrame()
        toolbar.setObjectName("markdownToolbar")
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        for label, callback, primary in (
            ("粘贴", self.paste_source, False),
            ("复制源码", self.copy_source, False),
            ("清空", self.clear_source, False),
            ("编译预览", self.compile_preview, True),
        ):
            button = QPushButton(label)
            button.setObjectName("markdownPrimaryButton" if primary else "markdownButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(34)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.outline_toggle_button = QPushButton("打开目录")
        self.outline_toggle_button.setObjectName("markdownButton")
        self.outline_toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.outline_toggle_button.setFixedHeight(34)
        self.outline_toggle_button.setToolTip("显示或隐藏由 Markdown 标题生成的文档目录")
        self.outline_toggle_button.clicked.connect(self.toggle_outline)
        layout.addWidget(self.outline_toggle_button)
        layout.addSpacing(10)
        self.auto_compile = QCheckBox("自动编译")
        self.auto_compile.setChecked(True)
        self.sync_enabled = QCheckBox("双向定位")
        self.sync_enabled.setChecked(True)
        layout.addWidget(self.auto_compile)
        layout.addWidget(self.sync_enabled)
        layout.addStretch(1)
        dialect = QLabel("CommonMark · GFM · KaTeX")
        dialect.setObjectName("markdownDialect")
        layout.addWidget(dialect)
        return toolbar

    @staticmethod
    def _pane(title: str, content: QWidget) -> QWidget:
        pane = QFrame()
        pane.setObjectName("markdownPane")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        label = QLabel(title)
        label.setObjectName("markdownPaneTitle")
        layout.addWidget(label)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(content, 1)
        return pane

    def _build_preview(self) -> QWidget:
        if QWebEngineView is None or QWebEngineSettings is None:
            label = QLabel("Qt WebEngine 不可用，无法显示 Markdown 预览。")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("markdownPreviewError")
            return label
        self.web_view = QWebEngineView()
        self.web_view.setObjectName("markdownPreview")
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.bridge = MarkdownPreviewBridge(self.web_view)
        self.bridge.source_requested.connect(self.sync_preview_to_source)
        self.bridge.render_completed.connect(self.on_render_completed)
        channel = QWebChannel(self.web_view.page())
        channel.registerObject("markdownBridge", self.bridge)
        self.web_view.page().setWebChannel(channel)
        self.web_view._markdown_channel = channel
        self.web_view.loadFinished.connect(self.on_preview_loaded)
        return self.web_view

    def _build_outline(self) -> QWidget:
        if QWebEngineView is None or QWebEngineSettings is None:
            label = QLabel("Qt WebEngine 不可用，无法显示 Markdown 目录。")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("markdownPreviewError")
            return label
        self.outline_web_view = QWebEngineView()
        self.outline_web_view.setObjectName("markdownOutline")
        settings = self.outline_web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.outline_bridge = MarkdownOutlineBridge(self.outline_web_view)
        self.outline_bridge.heading_requested.connect(self.jump_to_outline_source_line)
        channel = QWebChannel(self.outline_web_view.page())
        channel.registerObject("outlineBridge", self.outline_bridge)
        self.outline_web_view.page().setWebChannel(channel)
        self.outline_web_view._markdown_outline_channel = channel
        return self.outline_web_view

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#markdownReaderPage { background: #e8edf1; color: #17212b; }
            QFrame#markdownToolbar { background: #f8fafb; border-bottom: 1px solid #cfd8e1; }
            QPushButton#markdownButton, QPushButton#markdownPrimaryButton {
                border: 1px solid #bcc8d2; border-radius: 5px; padding: 0 13px;
                background: #ffffff; color: #243442; font-weight: 600;
            }
            QPushButton#markdownButton:hover { background: #eef3f5; border-color: #98a8b5; }
            QPushButton#markdownPrimaryButton { background: #006d77; border-color: #006d77; color: #ffffff; }
            QPushButton#markdownPrimaryButton:hover { background: #005f67; }
            QCheckBox { spacing: 7px; color: #334552; }
            QLabel#markdownDialect { color: #657582; font-size: 12px; }
            QFrame#markdownPane { background: #ffffff; border: 0; }
            QLabel#markdownPaneTitle { padding: 8px 12px; background: #edf2f4; border-bottom: 1px solid #cfd8e1; color: #425463; font-weight: 650; }
            QPlainTextEdit#markdownSource { background: #ffffff; color: #17212b; border: 0; padding: 10px 12px; selection-background-color: #b9ded9; }
            QSplitter#markdownSplitter::handle { background: #c8d2da; width: 4px; }
            QFrame#markdownStatusBar { background: #f8fafb; border-top: 1px solid #cfd8e1; }
            QLabel#markdownStatus { color: #596b78; font-size: 12px; }
            QLabel#markdownPreviewError { color: #b42318; background: #ffffff; }
            """
        )

    def schedule_compile(self) -> None:
        if self.auto_compile.isChecked():
            self.compile_timer.start()
        self.update_line_status()

    def schedule_draft_save(self) -> None:
        self.draft_save_timer.start()

    def load_draft(self) -> None:
        try:
            source = read_markdown_draft(self.draft_path)
        except OSError as error:
            self.status_label.setText(f"读取 Markdown 草稿失败：{error}")
            return
        if source:
            self.editor.setPlainText(source)

    def save_draft(self) -> None:
        self.draft_save_timer.stop()
        try:
            write_markdown_draft(self.draft_path, self.editor.toPlainText())
        except OSError as error:
            self.status_label.setText(f"保存 Markdown 草稿失败：{error}")

    def compile_preview(self) -> None:
        self.compile_timer.stop()
        if self.web_view is None:
            return
        try:
            result = compile_markdown(self.editor.toPlainText())
            document = build_markdown_document(result)
        except Exception as error:
            self.status_label.setText(f"编译失败：{error}")
            fallback = f"<html><body style='font-family:Segoe UI;padding:24px;color:#b42318'>{html.escape(str(error))}</body></html>"
            self.web_view.setHtml(fallback)
            return
        if not (MARKDOWN_ASSET_DIR / "katex" / "katex.min.js").is_file():
            self.status_label.setText("缺少离线 KaTeX 资源，无法编译数学公式。")
            return
        self._last_result = result
        self.save_draft()
        self.load_outline(result)
        self._preview_loaded = False
        base = QUrl.fromLocalFile(str(MARKDOWN_ASSET_DIR.resolve()) + os.sep)
        self.web_view.setHtml(document, base)
        warning_text = f" · {len(result.warnings)} 条安全提示" if result.warnings else ""
        self.status_label.setText(
            f"已编译 · {result.source_lines} 行 · {result.source_blocks} 个定位块 · "
            f"{len(result.headings)} 个标题 · {result.math_fragments} 个公式{warning_text}"
        )

    def on_preview_loaded(self, ok: bool) -> None:
        self._preview_loaded = bool(ok)
        if not ok:
            self.status_label.setText("Markdown 预览载入失败。")
            return
        self.sync_cursor_to_preview()

    def on_render_completed(self, math_count: int, error_count: int) -> None:
        if self._last_result is None:
            return
        suffix = f" · 公式错误 {error_count}" if error_count else ""
        warning_text = f" · 安全提示 {len(self._last_result.warnings)}" if self._last_result.warnings else ""
        self.status_label.setText(
            f"已编译 · {self._last_result.source_lines} 行 · {self._last_result.source_blocks} 个定位块 · "
            f"{len(self._last_result.headings)} 个标题 · {math_count} 个公式{suffix}{warning_text}"
        )

    def toggle_outline(self) -> None:
        self.outline_visible = not self.outline_visible
        self.outline_pane.setVisible(self.outline_visible)
        self.outline_toggle_button.setText("收起目录" if self.outline_visible else "打开目录")
        if self.outline_visible:
            total = max(900, sum(self.splitter.sizes()))
            outline_width = max(220, min(300, total // 5))
            content_width = max(680, total - outline_width)
            source_width = max(320, int(content_width * 0.45))
            self.splitter.setSizes([source_width, outline_width, content_width - source_width])

    def load_outline(self, result: MarkdownRenderResult) -> None:
        if self.outline_web_view is None:
            return
        document = build_markdown_outline_document(result.headings)
        base = QUrl.fromLocalFile(str(MARKDOWN_ASSET_DIR.resolve()) + os.sep)
        self.outline_web_view.setHtml(document, base)

    @Slot(int)
    def jump_to_outline_source_line(self, source_line: int) -> None:
        self._run_preview_script(max(1, int(source_line)), True)

    def paste_source(self) -> None:
        self.editor.paste()

    def copy_source(self) -> None:
        QApplication.clipboard().setText(self.editor.toPlainText())
        self.status_label.setText("Markdown 源码已复制。")

    def clear_source(self) -> None:
        if not self.editor.toPlainText():
            return
        answer = QMessageBox.question(self, "清空 Markdown", "清空当前 Markdown 源码？")
        if answer == QMessageBox.StandardButton.Yes:
            self.editor.clear()

    def update_line_status(self) -> None:
        cursor = self.editor.textCursor()
        self.line_label.setText(f"{cursor.blockNumber() + 1}:{cursor.positionInBlock() + 1}")

    def _run_preview_script(self, line: int, center: bool) -> None:
        if self.web_view is None or not self._preview_loaded or not self.sync_enabled.isChecked():
            return
        self.web_view.page().runJavaScript(f"window.revealSourceLine({max(1, int(line))}, {str(bool(center)).lower()});")
        if center:
            self.preview_highlight_timer.start()

    def clear_preview_highlight(self) -> None:
        if self.web_view is not None and self._preview_loaded:
            self.web_view.page().runJavaScript("window.clearSourceHighlight();")

    def sync_cursor_to_preview(self) -> None:
        if not self._syncing_from_preview:
            self._run_preview_script(self.editor.current_line(), True)

    def sync_scroll_to_preview(self, _value: int) -> None:
        if not self._syncing_from_preview:
            self._run_preview_script(self.editor.first_visible_line(), False)

    @Slot(int, bool)
    def sync_preview_to_source(self, line: int, focus: bool) -> None:
        if not self.sync_enabled.isChecked():
            return
        self._syncing_from_preview = True
        try:
            self.editor.reveal_line(line, focus=focus)
        finally:
            self._syncing_from_preview = False

    def closeEvent(self, event: Any) -> None:  # type: ignore[override]
        self.save_draft()
        super().closeEvent(event)
