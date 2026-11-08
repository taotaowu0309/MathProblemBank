"""The "背单词" button of the Windows Tk PDF reader.

The Tk reader runs inside the Qt control center process, so the button only
hands the current PDF and page to the shared Qt study window; the Tk side owns
no review logic or audio of its own.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any


def add_tk_reader_vocabulary_button(reader: Any, toolbar: Any, column: int) -> None:
    """Grid the button at ``column`` of the reader toolbar in the English workspace."""

    owner = getattr(reader, "owner", None)
    if str(getattr(owner, "workspace", "")) != "english":
        return

    def open_study() -> None:
        from shared.scripts.vocabulary_review_qt import open_study_for_reader

        open_study_for_reader(owner, getattr(reader, "pdf_path", None), int(getattr(reader, "page_index", 0)) + 1)

    button = ttk.Button(toolbar, text="背单词", command=open_study)
    button.grid(row=0, column=column, padx=(10, 0), sticky=tk.E)
    reader.vocabulary_review_button = button
