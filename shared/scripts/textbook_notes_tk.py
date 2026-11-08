"""Tk side panel for textbook LaTeX notes (the Windows PDF reader).

Mirrors :mod:`textbook_notes_qt`: notes linked to the bookmarks around the
current page appear in a split pane beside the PDF, or in a small floating
window.  Tk widgets cannot change parent, so the panel keeps its state in this
object and rebuilds its widgets in whichever container is active.  Storage,
linking and compilation all go through :class:`TextbookNotesService`.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from shared.scripts.textbook_notes_service import BOOKMARK_SEPARATOR, TextbookNotesService

try:
    import fitz  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - the reader already requires PyMuPDF
    fitz = None  # type: ignore[assignment]
try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - the reader already requires Pillow
    Image = ImageTk = None  # type: ignore[assignment]

_SERVICE: TextbookNotesService | None = None
_CHECKED = "☑ "
_UNCHECKED = "☐ "


def textbook_notes_service() -> TextbookNotesService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = TextbookNotesService()
    return _SERVICE


def _deepest_bookmark(outline: list[dict[str, Any]], page_number: int) -> dict[str, Any] | None:
    covering = [item for item in outline if item["page_start"] <= page_number <= item["page_end"]]
    if not covering:
        return None
    return min(covering, key=lambda item: (item["page_end"] - item["page_start"], -item["level"]))


def _open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class TkNoteEditorDialog:
    """Modal dialog: create a note for checked bookmarks, link an existing one, or edit TeX."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        outline: list[dict[str, Any]] | None = None,
        preselected: set[int] | None = None,
        existing_notes: list[dict[str, Any]] | None = None,
        title: str = "",
        latex: str = "",
        edit_mode: bool = False,
        copy_prompt: Callable[[list[int]], None] | None = None,
        show_example: Callable[[], None] | None = None,
    ) -> None:
        self.result: dict[str, Any] | None = None
        self.edit_mode = edit_mode
        self.existing_notes = existing_notes or []
        self.checked: set[int] = set(preselected or set())
        self.top = tk.Toplevel(parent)
        self.top.title("编辑笔记" if edit_mode else "为书签新建 / 链接笔记")
        self.top.geometry("820x680" if edit_mode else "1120x720")
        self.top.transient(parent.winfo_toplevel())
        self.top.columnconfigure(1, weight=1)
        self.top.rowconfigure(0, weight=1)

        self.tree: ttk.Treeview | None = None
        if not edit_mode:
            left = ttk.Frame(self.top, padding=8)
            left.grid(row=0, column=0, sticky=tk.NSEW)
            left.rowconfigure(1, weight=1)
            left.columnconfigure(0, weight=1)
            ttk.Label(left, text="点击勾选要链接的书签（一章或一节，可多选）").grid(row=0, column=0, sticky=tk.W)
            self.tree = ttk.Treeview(left, show="tree", selectmode="browse", height=30)
            self.tree.column("#0", width=380)
            self.tree.grid(row=1, column=0, sticky=tk.NSEW)
            scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
            scroll.grid(row=1, column=1, sticky=tk.NS)
            self.tree.configure(yscrollcommand=scroll.set)
            self.labels: dict[str, str] = {}
            parents: dict[int, str] = {}
            first_checked = ""
            for entry in outline or []:
                level = int(entry["level"])
                iid = str(entry["index"])
                self.labels[iid] = f"{entry['title']}   p.{entry['page_start']}–{entry['page_end']}"
                checked = int(entry["index"]) in self.checked
                self.tree.insert(
                    parents.get(level - 1, ""), tk.END, iid=iid,
                    text=(_CHECKED if checked else _UNCHECKED) + self.labels[iid],
                )
                parents[level] = iid
                for deeper in [key for key in parents if key > level]:
                    del parents[deeper]
                if checked and not first_checked:
                    first_checked = iid
            if first_checked:
                self.tree.see(first_checked)
            self.tree.bind("<Button-1>", self._toggle_check, add="+")

        right = ttk.Frame(self.top, padding=8)
        right.grid(row=0, column=1, sticky=tk.NSEW)
        right.columnconfigure(1, weight=1)
        right.rowconfigure(4, weight=1)
        self.mode_var = tk.StringVar(value="new")
        self.existing_combo: ttk.Combobox | None = None
        if not edit_mode:
            ttk.Radiobutton(right, text="新建笔记", value="new", variable=self.mode_var, command=self._sync_mode).grid(
                row=0, column=0, sticky=tk.W
            )
            link_radio = ttk.Radiobutton(
                right, text="链接到已有笔记", value="link", variable=self.mode_var, command=self._sync_mode
            )
            link_radio.grid(row=0, column=1, sticky=tk.W)
            self.existing_combo = ttk.Combobox(
                right, state="readonly",
                values=[f"{note['note_code']}  {note['title']}" for note in self.existing_notes],
            )
            if self.existing_notes:
                self.existing_combo.current(0)
            else:
                link_radio.state(["disabled"])
            self.existing_combo.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(4, 8))
        ttk.Label(right, text="标题").grid(row=2, column=0, sticky=tk.W)
        self.title_var = tk.StringVar(value=title)
        self.title_entry = ttk.Entry(right, textvariable=self.title_var)
        self.title_entry.grid(row=2, column=1, sticky=tk.EW, pady=4)
        ttk.Label(
            right, wraplength=640, justify=tk.LEFT,
            text=(
                "先点“生成 ChatGPT Work 任务”，写下这次想记的内容，把复制的一行任务发给 ChatGPT Work"
                "（这几页原文已经截成 PDF 放进任务）；它写好后再打开本窗口，草稿会自动填入。排版固定，只写正文。"
            ),
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(4, 4))
        text_frame = ttk.Frame(right)
        text_frame.grid(row=4, column=0, columnspan=2, sticky=tk.NSEW)
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.text = tk.Text(text_frame, wrap=tk.NONE, undo=True, font=("Consolas", 11))
        self.text.grid(row=0, column=0, sticky=tk.NSEW)
        text_scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text.yview)
        text_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.text.configure(yscrollcommand=text_scroll.set)
        self.text.insert("1.0", latex)
        buttons = ttk.Frame(right)
        buttons.grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        self.load_button = ttk.Button(buttons, text="从 .tex 文件载入…", command=self._load_file)
        self.load_button.pack(side=tk.LEFT)
        if copy_prompt is not None:
            ttk.Button(
                buttons, text="生成 ChatGPT Work 任务", command=lambda: copy_prompt(sorted(self.checked))
            ).pack(side=tk.LEFT, padx=(6, 0))
        if show_example is not None:
            ttk.Button(buttons, text="查看示例笔记", command=show_example).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="取消", command=self.top.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="保存并编译", command=self._accept).pack(side=tk.RIGHT, padx=6)
        self._sync_mode()
        self.top.grab_set()
        self.top.wait_window()

    def _toggle_check(self, event: tk.Event) -> str | None:
        assert self.tree is not None
        iid = self.tree.identify_row(event.y)
        element = str(self.tree.identify_element(event.x, event.y))
        if not iid or "indicator" in element.casefold():
            return None
        index = int(iid)
        if index in self.checked:
            self.checked.discard(index)
        else:
            self.checked.add(index)
        self.tree.item(iid, text=(_CHECKED if index in self.checked else _UNCHECKED) + self.labels[iid])
        return None

    def _sync_mode(self) -> None:
        creating = self.mode_var.get() == "new"
        state = tk.NORMAL if creating else tk.DISABLED
        self.title_entry.configure(state=state)
        self.text.configure(state=state)
        self.load_button.configure(state=state)
        if self.existing_combo is not None:
            self.existing_combo.configure(state="disabled" if creating else "readonly")

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.top, title="选择 LaTeX 笔记", filetypes=[("LaTeX", "*.tex"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            messagebox.showwarning("读取失败", str(error), parent=self.top)
            return
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", content)
        if not self.title_var.get().strip():
            self.title_var.set(Path(path).stem)

    def _accept(self) -> None:
        linking = not self.edit_mode and self.mode_var.get() == "link"
        if not self.edit_mode and not self.checked:
            messagebox.showinfo("请选择书签", "请至少勾选一个书签。", parent=self.top)
            return
        latex = self.text.get("1.0", tk.END).strip()
        if not linking and (not self.title_var.get().strip() or not latex):
            messagebox.showinfo("内容不完整", "请填写标题，并等 ChatGPT Work 写好草稿（或粘贴 LaTeX 内容）。", parent=self.top)
            return
        existing_id = None
        if linking and self.existing_combo is not None and self.existing_notes:
            existing_id = int(self.existing_notes[max(0, self.existing_combo.current())]["id"])
        self.result = {
            "linking": linking,
            "existing_note_id": existing_id,
            "indices": sorted(self.checked),
            "title": self.title_var.get().strip(),
            "latex": latex,
        }
        self.top.destroy()


class TkTextbookNotesPanel:
    """Notes for the bookmarks around the Tk reader's current page."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.service = textbook_notes_service()
        self.notes: list[dict[str, Any]] = []
        self.selected_note_id: int | None = None
        self.visible = False
        self.floating_window: tk.Toplevel | None = None
        self.frame: ttk.Frame | None = None
        self.whole_book_var = tk.BooleanVar(master=reader.window, value=False)
        self._outline_cache: tuple[str, list[dict[str, Any]]] | None = None
        self._last_key: tuple[Any, ...] | None = None
        self._refresh_after_id: str | None = None
        self._render_after_id: str | None = None
        self._note_photos: list[Any] = []
        self._rendered_key: tuple[Any, ...] | None = None
        self._results: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._polling = False
        self._example_pdf: Path | None = None

    # -- container management ----------------------------------------------------

    def is_floating(self) -> bool:
        return self.floating_window is not None

    def toggle(self) -> None:
        if self.is_floating():
            self.floating_window.lift()  # type: ignore[union-attr]
            return
        if self.visible:
            self._hide_pane()
        else:
            self._show_pane()
        self._update_reader_button()

    def _show_pane(self) -> None:
        self.frame = ttk.Frame(self.reader.paned, padding=(8, 8))
        self._build(self.frame)
        width = max(360, int(self.reader.paned.winfo_width() * 0.4))
        self.reader.paned.add(self.frame, minsize=320, width=width)
        self.visible = True
        self.refresh(force=True)

    def _hide_pane(self) -> None:
        if self.frame is not None:
            try:
                self.reader.paned.forget(self.frame)
            except tk.TclError:
                pass
            self.frame.destroy()
        self.frame = None
        self.visible = False

    def toggle_floating(self) -> None:
        if self.is_floating():
            self.dock()
        else:
            self.undock()

    def undock(self) -> None:
        self._hide_pane()
        window = tk.Toplevel(self.reader.window)
        window.title("教材笔记")
        window.geometry("560x780")
        window.transient(self.reader.window)
        window.protocol("WM_DELETE_WINDOW", self.close_floating)
        self.floating_window = window
        self.frame = ttk.Frame(window, padding=(8, 8))
        self.frame.pack(fill=tk.BOTH, expand=True)
        self._build(self.frame)
        self.visible = True
        self._update_reader_button()
        self.refresh(force=True)

    def dock(self) -> None:
        self._destroy_floating()
        self._show_pane()
        self._update_reader_button()

    def close_floating(self) -> None:
        self._destroy_floating()
        self.visible = False
        self._update_reader_button()

    def _destroy_floating(self) -> None:
        window = self.floating_window
        self.floating_window = None
        self.frame = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def destroy(self) -> None:
        self._destroy_floating()

    def _update_reader_button(self) -> None:
        button = getattr(self.reader, "toggle_notes_button", None)
        if button is not None:
            button.configure(text="收起笔记" if self.visible and not self.is_floating() else "笔记")

    # -- widgets -----------------------------------------------------------------

    def _build(self, container: ttk.Frame) -> None:
        container.columnconfigure(0, weight=1)
        container.rowconfigure(6, weight=1)
        top = ttk.Frame(container)
        top.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(top, text="教材笔记").pack(side=tk.LEFT)
        ttk.Button(top, text="停靠" if self.is_floating() else "小窗", command=self.toggle_floating).pack(side=tk.RIGHT)
        ttk.Checkbutton(
            top, text="整本书", variable=self.whole_book_var, command=lambda: self.refresh(force=True)
        ).pack(side=tk.RIGHT, padx=6)
        self.bookmark_var = tk.StringVar(master=container, value="")
        ttk.Label(container, textvariable=self.bookmark_var, wraplength=480, justify=tk.LEFT).grid(
            row=1, column=0, sticky=tk.EW, pady=(4, 4)
        )
        self.listbox = tk.Listbox(container, height=5, activestyle="none", exportselection=False)
        self.listbox.grid(row=2, column=0, sticky=tk.EW)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select, add="+")
        actions = ttk.Frame(container)
        actions.grid(row=3, column=0, sticky=tk.EW, pady=(6, 2))
        ttk.Button(actions, text="新建 / 链接…", command=self.open_link_dialog).pack(side=tk.LEFT)
        self.edit_button = ttk.Button(actions, text="编辑", command=self.edit_selected_note)
        self.edit_button.pack(side=tk.LEFT, padx=4)
        self.compile_button = ttk.Button(actions, text="编译", command=self.compile_selected_note)
        self.compile_button.pack(side=tk.LEFT)
        self.more_button = ttk.Button(actions, text="更多", command=self._show_more_menu)
        self.more_button.pack(side=tk.LEFT, padx=4)
        ttk.Button(actions, text="任务", command=lambda: self.copy_prompt()).pack(side=tk.LEFT)
        ttk.Button(actions, text="示例", command=self.show_example).pack(side=tk.LEFT, padx=4)
        self.status_var = tk.StringVar(master=container, value="")
        ttk.Label(container, textvariable=self.status_var, wraplength=480, justify=tk.LEFT).grid(
            row=4, column=0, sticky=tk.EW, pady=(2, 4)
        )
        view = ttk.Frame(container)
        view.grid(row=6, column=0, sticky=tk.NSEW)
        view.rowconfigure(0, weight=1)
        view.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(view, background="#e9edf1", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky=tk.NSEW)
        scroll = ttk.Scrollbar(view, orient=tk.VERTICAL, command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky=tk.NS)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", lambda _event: self._schedule_render(), add="+")
        self.canvas.bind("<MouseWheel>", self._on_wheel, add="+")
        self._rendered_key = None
        self._fill_list()

    def _on_wheel(self, event: tk.Event) -> str:
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")
        return "break"

    # -- data --------------------------------------------------------------------

    def schedule_refresh(self) -> None:
        if not self.visible or self.frame is None:
            return
        if self._refresh_after_id is not None:
            try:
                self.reader.window.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
        self._refresh_after_id = self.reader.window.after(250, self.refresh)

    def document_changed(self) -> None:
        self._outline_cache = None
        self._last_key = None
        self.schedule_refresh()

    def outline(self) -> list[dict[str, Any]]:
        pdf_path = getattr(self.reader, "pdf_path", None)
        if pdf_path is None:
            return []
        if self._outline_cache is None or self._outline_cache[0] != str(pdf_path):
            try:
                outline = self.service.pdf_outline(pdf_path)
            except Exception:
                outline = []
            self._outline_cache = (str(pdf_path), outline)
        return self._outline_cache[1]

    def _page_number(self) -> int:
        return int(getattr(self.reader, "page_index", 0)) + 1

    def selected_note(self) -> dict[str, Any] | None:
        return next((note for note in self.notes if int(note["id"]) == self.selected_note_id), None)

    def refresh(self, force: bool = False) -> None:
        self._refresh_after_id = None
        pdf_path = getattr(self.reader, "pdf_path", None)
        if pdf_path is None or not self.visible or self.frame is None:
            return
        page_number = self._page_number()
        whole_book = bool(self.whole_book_var.get())
        current = _deepest_bookmark(self.outline(), page_number)
        self.bookmark_var.set(
            f"第 {page_number} 页 · {current['path_title']}" if current else f"第 {page_number} 页 · 不在任何书签内"
        )
        key = (str(pdf_path), None if whole_book else page_number)
        if not force and key == self._last_key:
            return
        self._last_key = key
        self._example_pdf = None
        try:
            self.notes = self.service.notes_for_document(pdf_path, page_number=None if whole_book else page_number)
        except Exception as error:
            self.notes = []
            self.status_var.set(f"读取笔记失败：{error}")
        ids = [int(note["id"]) for note in self.notes]
        if self.selected_note_id not in ids:
            self.selected_note_id = ids[0] if ids else None
        self._fill_list()

    def _fill_list(self) -> None:
        self.listbox.delete(0, tk.END)
        for note in self.notes:
            bookmarks = [link["bookmark_title"].split(BOOKMARK_SEPARATOR)[-1] for link in note["links"]]
            mark = "" if note["pdf_current"] else ("  ⚠ 编译失败" if note["compile_status"] == "failed" else "  · 待编译")
            self.listbox.insert(tk.END, f"{note['title']}  —  {'、'.join(dict.fromkeys(bookmarks))}{mark}")
        ids = [int(note["id"]) for note in self.notes]
        if self.selected_note_id in ids:
            row = ids.index(int(self.selected_note_id))
            self.listbox.selection_set(row)
            self.listbox.see(row)
        self._show_selected_note()

    def _on_list_select(self, _event: tk.Event | None = None) -> None:
        selection = self.listbox.curselection()
        if selection and 0 <= int(selection[0]) < len(self.notes):
            self.selected_note_id = int(self.notes[int(selection[0])]["id"])
            self._example_pdf = None
            self._show_selected_note()

    def book_title(self) -> str:
        title = ""
        variable = getattr(self.reader, "problem_var", None)
        if variable is not None:
            title = str(variable.get() or "").strip()
        pdf_path = getattr(self.reader, "pdf_path", None)
        return title or (Path(pdf_path).stem if pdf_path else "")

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
        ideas = self._ask_ideas()
        if ideas is None:
            return
        pdf_path = getattr(self.reader, "pdf_path", None)
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
            messagebox.showwarning("生成任务失败", str(error), parent=self._dialog_parent())
            return
        window = self.reader.window
        window.clipboard_clear()
        window.clipboard_append(task["task_line"])
        target = "、".join(item["title"] for item in chosen) or "（未选书签）"
        if self.frame is not None:
            self.status_var.set(
                f"已复制 ChatGPT Work 任务（{target}，第 {pages or '?'} 页）。"
                "它写好后点“新建 / 链接…”，草稿会自动填入。"
            )

    def _ask_ideas(self) -> str | None:
        """Ask what the user wants to record; None when cancelled."""

        top = tk.Toplevel(self._dialog_parent())
        top.title("我的想法")
        top.geometry("560x340")
        top.transient(self._dialog_parent().winfo_toplevel())
        ttk.Label(
            top, wraplength=520, justify=tk.LEFT,
            text="这次想记什么？想弄清的问题、自己的理解、容易混的地方，可以很零碎；"
                 "ChatGPT Work 会按这些想法组织笔记。留空则在 ChatGPT Work 里再补。",
        ).pack(fill=tk.X, padx=10, pady=(10, 4))
        text = tk.Text(top, height=10, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=10)
        result: dict[str, str | None] = {"value": None}

        def accept() -> None:
            result["value"] = text.get("1.0", tk.END).strip()
            top.destroy()

        buttons = ttk.Frame(top)
        buttons.pack(fill=tk.X, padx=10, pady=8)
        ttk.Button(buttons, text="取消", command=top.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="生成并复制任务", command=accept).pack(side=tk.RIGHT, padx=6)
        text.focus_set()
        top.grab_set()
        top.wait_window()
        return result["value"]

    def show_example(self) -> None:
        if self.frame is not None:
            self.status_var.set("正在准备示例笔记…")

        def work() -> None:
            try:
                result: Any = {"ok": True, "path": str(self.service.example_pdf())}
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            self._results.put((self._example_ready, result))

        threading.Thread(target=work, name="textbook-note-compile", daemon=True).start()
        self._start_polling()

    def _example_ready(self, result: Any) -> None:
        if not (isinstance(result, dict) and result.get("ok")):
            error = result.get("error") if isinstance(result, dict) else result
            messagebox.showwarning("示例笔记编译失败", str(error), parent=self._dialog_parent())
            return
        if self.frame is None:
            return
        self._example_pdf = Path(result["path"])
        self.listbox.selection_clear(0, tk.END)
        self.edit_button.state(["disabled"])
        self.compile_button.state(["disabled"])
        self.status_var.set("正在显示【示例笔记】：所有笔记都使用这一固定排版，ChatGPT Work 任务里附有这份示例。")
        self._rendered_key = None
        self._schedule_render()

    def _show_selected_note(self) -> None:
        note = self.selected_note()
        state = ["!disabled"] if note is not None else ["disabled"]
        self.edit_button.state(state)
        self.compile_button.state(state)
        if note is None:
            if not self.status_var.get().startswith("读取笔记失败"):
                self.status_var.set(
                    "这本书还没有笔记。" if self.whole_book_var.get()
                    else "这一页所在的书签还没有笔记。先点“任务”交给 ChatGPT Work，它写好后点“新建 / 链接…”；"
                    "“示例”可查看标准排版。"
                )
        elif note.get("tex_consistent") is False:
            self.status_var.set("正文与保存记录不一致，请核对正文或恢复版本；下方 PDF 不是当前版本。")
        elif note["pdf_current"]:
            self.status_var.set(f"{note['note_code']} · 链接 {len(note['links'])} 个书签")
        elif note["compile_status"] == "failed":
            self.status_var.set("编译失败（下方为上一次成功的 PDF）：" + str(note["compile_error"]).splitlines()[0])
        else:
            self.status_var.set("TeX 已更新，PDF 尚未重新编译。")
        self._schedule_render()

    # -- rendering ---------------------------------------------------------------

    def _schedule_render(self) -> None:
        if self.frame is None:
            return
        if self._render_after_id is not None:
            try:
                self.reader.window.after_cancel(self._render_after_id)
            except tk.TclError:
                pass
        self._render_after_id = self.reader.window.after(120, self._render_note_pdf)

    def _render_note_pdf(self) -> None:
        self._render_after_id = None
        if self.frame is None or fitz is None or Image is None:
            return
        note = self.selected_note()
        pdf_file = self._example_pdf or (Path(note["pdf_file"]) if note else None)
        width = max(200, int(self.canvas.winfo_width()) - 16)
        stamp = pdf_file.stat().st_mtime if pdf_file is not None and pdf_file.is_file() else None
        key = (str(pdf_file), stamp, width)
        if key == self._rendered_key:
            return
        self._rendered_key = key
        self.canvas.delete("all")
        self._note_photos = []
        if pdf_file is None or stamp is None:
            self.canvas.configure(scrollregion=(0, 0, width, 0))
            return
        y = 8
        try:
            with fitz.open(str(pdf_file)) as document:
                for page in document:
                    scale = width / max(1.0, float(page.rect.width))
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                    photo = ImageTk.PhotoImage(image)
                    self._note_photos.append(photo)
                    self.canvas.create_image(8, y, image=photo, anchor=tk.NW)
                    y += pixmap.height + 10
        except Exception as error:
            self.status_var.set(f"笔记 PDF 显示失败：{error}")
        self.canvas.configure(scrollregion=(0, 0, width + 16, y))
        self.canvas.yview_moveto(0)

    # -- actions -----------------------------------------------------------------

    def _dialog_parent(self) -> tk.Misc:
        return self.floating_window or self.reader.window

    def open_link_dialog(self, preselected: set[int] | None = None) -> None:
        pdf_path = getattr(self.reader, "pdf_path", None)
        if pdf_path is None:
            return
        outline = self.outline()
        if not outline:
            messagebox.showinfo("没有书签", "这份 PDF 没有书签目录，无法按章节链接笔记。", parent=self._dialog_parent())
            return
        if preselected is None:
            current = _deepest_bookmark(outline, self._page_number())
            preselected = {int(current["index"])} if current else set()
        first = next((item for item in outline if item["index"] in preselected), None)
        task = self.service.textbook_note_task(pdf_path)
        dialog = TkNoteEditorDialog(
            self._dialog_parent(), outline=outline, preselected=preselected,
            existing_notes=self.service.list_notes(limit=500), title=first["title"] if first else "",
            latex=str(task["draft_source"]),
            copy_prompt=self.copy_prompt, show_example=self.show_example,
        )
        if dialog.result is None:
            return
        links = [
            {"pdf_path": str(pdf_path), "outline_index": index, "book_title": self.book_title()}
            for index in dialog.result["indices"]
        ]
        try:
            if dialog.result["linking"]:
                note = self.service.link_note(int(dialog.result["existing_note_id"]), links)
            else:
                note = self.service.create_note(
                    dialog.result["title"], dialog.result["latex"], links=links, source_kind="manual", compile_pdf=False,
                )
                if task["draft_exists"]:
                    self.service.archive_note_draft("textbook", task["task_key"])
        except Exception as error:
            messagebox.showwarning("保存失败", str(error), parent=self._dialog_parent())
            return
        self.selected_note_id = int(note["id"])
        self.refresh(force=True)
        if not dialog.result["linking"]:
            self.compile_note(int(note["id"]))

    def edit_selected_note(self) -> None:
        note = self.selected_note()
        if note is None:
            return
        try:
            content = self.service.read_note_tex(int(note["id"]))["content"]
        except Exception as error:
            messagebox.showwarning("读取失败", str(error), parent=self._dialog_parent())
            return
        dialog = TkNoteEditorDialog(
            self._dialog_parent(), title=str(note["title"]), latex=content, edit_mode=True,
            show_example=self.show_example,
        )
        if dialog.result is None:
            return
        try:
            result = self.service.update_note_tex(
                int(note["id"]), dialog.result["latex"], title=dialog.result["title"], compile_pdf=False
            )
        except Exception as error:
            messagebox.showwarning("保存失败", str(error), parent=self._dialog_parent())
            return
        self.refresh(force=True)
        if result.get("changed"):
            self.compile_note(int(note["id"]))

    def compile_selected_note(self) -> None:
        note = self.selected_note()
        if note is not None:
            self.compile_note(int(note["id"]))

    def compile_note(self, note_id: int) -> None:
        if self.frame is not None:
            self.status_var.set("正在编译笔记 PDF…")

        def work() -> None:
            try:
                result: Any = self.service.compile_note(note_id)
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            self._results.put((self._compile_finished, result))

        threading.Thread(target=work, name="textbook-note-compile", daemon=True).start()
        self._start_polling()

    def _compile_finished(self, result: Any) -> None:
        self._rendered_key = None
        self.refresh(force=True)
        if not (isinstance(result, dict) and result.get("ok")):
            error = result.get("error") if isinstance(result, dict) else result
            messagebox.showwarning("笔记编译失败", f"TeX 已保存，PDF 未替换。\n\n{error}", parent=self._dialog_parent())

    def _start_polling(self) -> None:
        if not self._polling:
            self._polling = True
            self.reader.window.after(200, self._poll_results)

    def _poll_results(self) -> None:
        try:
            while True:
                callback, value = self._results.get_nowait()
                callback(value)
        except queue.Empty:
            pass
        except tk.TclError:
            self._polling = False
            return
        if threading.active_count() > 1 and any(
            thread.name == "textbook-note-compile" for thread in threading.enumerate()
        ):
            self.reader.window.after(200, self._poll_results)
        else:
            self._polling = False
            if not self._results.empty():
                self._start_polling()

    def _show_more_menu(self) -> None:
        note = self.selected_note()
        menu = tk.Menu(self.more_button, tearoff=False)
        menu.add_command(
            label="打开 TeX 源文件",
            state=tk.NORMAL if note else tk.DISABLED,
            command=lambda: _open_file(Path(note["tex_file"])) if note else None,
        )
        jump_menu = tk.Menu(menu, tearoff=False)
        unlink_menu = tk.Menu(menu, tearoff=False)
        identity = None
        pdf_path = getattr(self.reader, "pdf_path", None)
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
            jump_menu.add_command(
                label=label, state=tk.NORMAL if same_book else tk.DISABLED,
                command=lambda page=int(link["page_start"]): self._jump(page),
            )
            unlink_menu.add_command(label=label, command=lambda link_id=int(link["id"]): self._unlink(link_id))
        has_links = bool(note and note["links"])
        menu.add_cascade(label="跳到链接的书签", menu=jump_menu, state=tk.NORMAL if has_links else tk.DISABLED)
        menu.add_cascade(label="移除书签链接", menu=unlink_menu, state=tk.NORMAL if has_links else tk.DISABLED)
        x = self.more_button.winfo_rootx()
        y = self.more_button.winfo_rooty() + self.more_button.winfo_height()
        menu.tk_popup(x, y)

    def _jump(self, page_number: int) -> None:
        jump = getattr(self.reader, "_jump_to_page_anchor", None)
        if callable(jump):
            self.reader.page_index = max(0, page_number - 1)
            jump(max(0, page_number - 1), 0.0)

    def _unlink(self, link_id: int) -> None:
        if not messagebox.askyesno(
            "移除链接", "只移除这个书签链接，笔记本身和文件都会保留。继续吗？", parent=self._dialog_parent()
        ):
            return
        try:
            self.service.unlink_note(link_id)
        except Exception as error:
            messagebox.showwarning("移除失败", str(error), parent=self._dialog_parent())
        self.refresh(force=True)


__all__ = ["TkNoteEditorDialog", "TkTextbookNotesPanel", "textbook_notes_service"]
