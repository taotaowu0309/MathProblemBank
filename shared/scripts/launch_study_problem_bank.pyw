from __future__ import annotations

import ctypes
import traceback
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PYTHON_HOME = Path(sys.executable).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("TCL_LIBRARY", str(PYTHON_HOME / "tcl" / "tcl8.6"))
os.environ.setdefault("TK_LIBRARY", str(PYTHON_HOME / "tcl" / "tk8.6"))

from shared.scripts.application_paths import APP_PATHS
from shared.ui.windows_shell import apply_icon, configure_app_identity


LOG_PATH = APP_PATHS.log_dir / "launch_study_problem_bank_error.log"
STARTUP_TIMING_PATH = APP_PATHS.cache_dir / "last_startup_timing.json"
APP_ID = "MathProblemBank.ControlCenter"
APP_ICON_PATH = ROOT / "shared" / "ui" / "assets" / "icons" / "problem_bank_studio_icon.ico"
_WORKSPACE_CHOOSER_DIALOG: Any = None
_QT_APP: Any = None
WORKSPACE_OPTIONS = (
    {
        "key": "math",
        "name": "数学",
        "mark": "M",
        "description": "数学题库、学习项目与网课讲义",
        "enabled": True,
        "accent": "#3978d4",
    },
    {
        "key": "physics",
        "name": "物理",
        "mark": "P",
        "description": "物理题库、学习项目与课程资料",
        "enabled": True,
        "accent": "#7163c7",
    },
    {
        "key": "english",
        "name": "英语",
        "mark": "EN",
        "description": "旋元佑五书、广读、写作与主动语言练习",
        "enabled": True,
        "accent": "#2b8d7d",
    },
    {
        "key": "computer_science",
        "name": "计算机",
        "mark": "CS",
        "description": "计算机课程、代码与知识项目",
        "enabled": False,
        "accent": "#c17836",
    },
    {
        "key": "microelectronics",
        "name": "微电子科学与工程",
        "mark": "ME",
        "description": "专业课程、器件、电路与工程资料",
        "enabled": False,
        "accent": "#b9516d",
    },
)

configure_app_identity(APP_ID)


def prime_legacy_tk_dpi() -> None:
    """Prime the legacy PDF preview toolkit before Qt without showing a window."""
    if os.environ.get("STUDY_BANK_TK_DPI_PRIMED") == "1":
        return
    import tkinter as tk

    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
    finally:
        root.destroy()
    os.environ["STUDY_BANK_TK_DPI_PRIMED"] = "1"


def warm_startup_imports() -> None:
    """Use chooser dwell time to load the largest non-Qt startup dependency."""
    try:
        from shared.scripts import online_course_service  # noqa: F401
    except Exception:
        # The normal foreground import still reports a complete traceback.
        pass


def choose_workspace_qt() -> str | None:
    """Show the same stable Qt window stack used by the control center."""
    global _QT_APP, _WORKSPACE_CHOOSER_DIALOG
    prime_legacy_tk_dpi()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
    )

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    _QT_APP = app
    app.setApplicationName("学习题库管理中心")
    app.setOrganizationName("MathProblemBank")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))

    dialog = QDialog()
    _WORKSPACE_CHOOSER_DIALOG = dialog
    dialog.setObjectName("workspaceChooser")
    dialog.setWindowTitle("选择学习工程")
    dialog.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
    dialog.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, True)
    dialog.setModal(True)
    dialog.setFixedSize(680, 560)
    if APP_ICON_PATH.exists():
        dialog.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    dialog.setStyleSheet(
        """
        QDialog#workspaceChooser { background: #f7f9fc; }
        QLabel#chooserEyebrow { color: #3978d4; font-size: 12px; font-weight: 600; }
        QLabel#chooserTitle { color: #172033; font-size: 25px; font-weight: 600; }
        QLabel#chooserNote { color: #5d687a; font-size: 13px; }
        QFrame#workspaceCard {
            border: 1px solid #d3dce8; border-radius: 12px; background: white;
        }
        QFrame#workspaceCard[enabledCard="true"]:hover { border-color: #6699df; background: #f4f8ff; }
        QFrame#workspaceCard[enabledCard="false"] { background: #f1f3f6; border-color: #e0e4ea; }
        QLabel#workspaceMark { color: white; border-radius: 8px; font-size: 14px; font-weight: 700; }
        QLabel#workspaceName { color: #182235; font-size: 16px; font-weight: 600; }
        QLabel#workspaceDescription { color: #667286; font-size: 11px; }
        QLabel#workspaceState {
            color: #778293; background: #e3e7ed; border-radius: 7px;
            padding: 3px 8px; font-size: 11px;
        }
        QPushButton#workspaceOpenButton {
            min-height: 31px; border: 1px solid #c5d4e8; border-radius: 7px;
            background: #edf4ff; color: #285eaa; font-size: 12px; font-weight: 600;
        }
        QPushButton#workspaceOpenButton:hover { background: #deebff; border-color: #7ea6dc; }
        QPushButton#workspaceOpenButton:pressed { background: #cfdef3; }
        QPushButton#cancelButton {
            border: none; background: transparent; color: #6b7585; font-size: 12px;
        }
        QPushButton#cancelButton:hover { color: #263246; text-decoration: underline; }
        """
    )
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(32, 24, 32, 18)
    layout.setSpacing(6)
    eyebrow = QLabel("PROBLEM BANK STUDIO")
    eyebrow.setObjectName("chooserEyebrow")
    layout.addWidget(eyebrow)
    title = QLabel("请选择学习工程")
    title.setObjectName("chooserTitle")
    title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Weight.DemiBold))
    layout.addWidget(title)
    note = QLabel("选择本次要进入的工作空间；未启用的工程会在后续接入。")
    note.setObjectName("chooserNote")
    layout.addWidget(note)
    layout.addSpacing(12)
    card_grid = QGridLayout()
    card_grid.setHorizontalSpacing(12)
    card_grid.setVerticalSpacing(12)
    selection: dict[str, str | None] = {"value": None}

    def select(value: str) -> None:
        selection["value"] = value
        dialog.accept()

    for index, option in enumerate(WORKSPACE_OPTIONS):
        card = QFrame()
        card.setObjectName("workspaceCard")
        card.setProperty("enabledCard", bool(option["enabled"]))
        card.setFixedHeight(116)
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(12, 11, 12, 11)
        card_layout.setSpacing(11)
        mark = QLabel(str(option["mark"]))
        mark.setObjectName("workspaceMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(42, 42)
        mark.setStyleSheet(f"background: {option['accent']};")
        card_layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(3)
        name = QLabel(str(option["name"]))
        name.setObjectName("workspaceName")
        description = QLabel(str(option["description"]))
        description.setObjectName("workspaceDescription")
        description.setWordWrap(True)
        text_layout.addWidget(name)
        text_layout.addWidget(description)
        if option["enabled"]:
            open_button = QPushButton("进入工程")
            open_button.setObjectName("workspaceOpenButton")
            open_button.setCursor(Qt.CursorShape.PointingHandCursor)
            open_button.clicked.connect(
                lambda _checked=False, item=str(option["key"]): select(item)
            )
            text_layout.addWidget(open_button)
        else:
            state = QLabel("待接入")
            state.setObjectName("workspaceState")
            text_layout.addWidget(state)
        card_layout.addLayout(text_layout, 1)
        row = index // 2
        column = index % 2
        if index == len(WORKSPACE_OPTIONS) - 1 and len(WORKSPACE_OPTIONS) % 2:
            card_grid.addWidget(card, row, 0, 1, 2)
        else:
            card_grid.addWidget(card, row, column)
    layout.addLayout(card_grid)
    layout.addStretch(1)
    cancel = QPushButton("取消启动")
    cancel.setObjectName("cancelButton")
    cancel.setCursor(Qt.CursorShape.PointingHandCursor)
    cancel.clicked.connect(dialog.reject)
    layout.addWidget(cancel, 0, Qt.AlignmentFlag.AlignCenter)
    dialog.exec()
    _WORKSPACE_CHOOSER_DIALOG = None
    dialog.deleteLater()
    app.processEvents()
    return selection["value"]


def choose_workspace_tk() -> str | None:
    import tkinter as tk

    choice: dict[str, str | None] = {"value": None}
    root = tk.Tk()
    # Keep the native window hidden until its size, position, icon, and widgets
    # are ready.  Otherwise Windows may briefly paint the new Tk window at
    # (0, 0), leaving a black rectangle before the centered chooser appears.
    root.withdraw()
    root.title("选择学习工程")
    apply_icon(root, APP_ICON_PATH)
    root.resizable(False, False)
    root.geometry("320x150")
    root.update_idletasks()
    width = 320
    height = 150
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 2
    root.geometry(f"{width}x{height}+{x}+{y}")

    label = tk.Label(root, text="请选择要打开的学习工程", font=("Microsoft YaHei UI", 11))
    label.pack(pady=(22, 16))

    buttons = tk.Frame(root)
    buttons.pack()

    def select(value: str) -> None:
        choice["value"] = value
        # Hide synchronously before destroying the Tk to suppress Windows'
        # top-left shrink animation and stale compositor frame.
        root.withdraw()
        root.update_idletasks()
        root.destroy()

    math_button = tk.Button(buttons, text="数学", width=10, command=lambda: select("math"))
    physics_button = tk.Button(buttons, text="物理", width=10, command=lambda: select("physics"))
    math_button.pack(side=tk.LEFT, padx=8)
    physics_button.pack(side=tk.LEFT, padx=8)
    def close() -> None:
        choice["value"] = None
        root.withdraw()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.update_idletasks()
    root.deiconify()
    root.mainloop()
    return choice["value"]


def choose_workspace() -> str | None:
    warmup_done = threading.Event()

    def warm() -> None:
        try:
            warm_startup_imports()
        finally:
            warmup_done.set()

    threading.Thread(
        target=warm,
        name="problem-bank-startup-warmup",
        daemon=True,
    ).start()
    try:
        workspace = choose_workspace_qt()
    except Exception:
        workspace = choose_workspace_tk()
    if workspace is not None:
        # Usually the choice takes longer than the warmup. Bound instant clicks.
        warmup_done.wait(timeout=1.5)
    return workspace


if __name__ == "__main__":
    try:
        startup_started = time.perf_counter()
        skip_workspace_chooser = (
            APP_PATHS.public_release
            or os.environ.get("STUDY_BANK_SKIP_WORKSPACE_CHOOSER") == "1"
        )
        workspace = (
            (os.environ.get("STUDY_BANK_WORKSPACE", "").strip() or "math")
            if skip_workspace_chooser
            else choose_workspace()
        )
        if workspace is None:
            raise SystemExit(0)
        enabled_workspaces = {
            str(option["key"]) for option in WORKSPACE_OPTIONS if option["enabled"]
        }
        if workspace not in enabled_workspaces:
            raise RuntimeError(f"未知学习工程：{workspace!r}")
        os.environ["STUDY_BANK_WORKSPACE"] = workspace
        from shared.scripts.problem_bank_center_qt import main

        def record_startup_ready() -> None:
            import json
            elapsed = time.perf_counter() - startup_started
            STARTUP_TIMING_PATH.parent.mkdir(parents=True, exist_ok=True)
            STARTUP_TIMING_PATH.write_text(
                json.dumps(
                    {
                        "seconds_from_process_start_to_first_paint": round(elapsed, 3),
                        "workspace": workspace,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        main(record_startup_ready)
    except Exception:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        message = traceback.format_exc()
        LOG_PATH.write_text(message, encoding="utf-8")
        ctypes.windll.user32.MessageBoxW(
            None,
            f"学习题库管理中心启动失败，错误已写入：\n{LOG_PATH}\n\n{message[-1200:]}",
            "学习题库管理中心",
            0x10,
        )
