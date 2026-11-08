from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import tkinter as tk


DEFAULT_APP_ID = "MathProblemBank.Toolbox"


def configure_app_identity(app_id: str = DEFAULT_APP_ID) -> None:
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def configure_process(app_id: str = DEFAULT_APP_ID) -> None:
    if sys.platform != "win32":
        return

    configure_app_identity(app_id)

    try:
        awareness_context = ctypes.c_void_p(-4)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(awareness_context)
        return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ensure_icon(icon_path: Path, source_image: Path | None = None) -> Path | None:
    if icon_path.exists():
        return icon_path

    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError:
        return None

    icon_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if source_image is not None and source_image.exists():
            image = Image.open(source_image)
            image = ImageOps.exif_transpose(image).convert("RGB")
            image = ImageOps.fit(
                image,
                (256, 256),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.38),
            )
        else:
            image = Image.new("RGB", (256, 256), "#102234")
            draw = ImageDraw.Draw(image)
            for y in range(256):
                ratio = y / 255
                red = round(18 + 34 * ratio)
                green = round(42 + 66 * ratio)
                blue = round(62 + 86 * ratio)
                draw.line((0, y, 256, y), fill=(red, green, blue))
            draw.rounded_rectangle((16, 16, 240, 240), radius=48, outline="#D9E9F3", width=4)
            draw.rounded_rectangle((38, 42, 218, 214), radius=28, fill="#F7FAFC")
            draw.rounded_rectangle((54, 62, 202, 88), radius=10, fill="#2F6F92")
            draw.line((58, 126, 198, 126), fill="#D7E2EA", width=3)
            draw.line((58, 166, 198, 166), fill="#D7E2EA", width=3)
            draw.arc((62, 104, 154, 196), start=215, end=325, fill="#B98F64", width=8)
            draw.line((146, 104, 188, 196), fill="#2F6F92", width=8)
            draw.line((188, 104, 146, 196), fill="#2F6F92", width=8)
            try:
                font = ImageFont.truetype("seguisb.ttf", 38)
            except OSError:
                font = ImageFont.load_default()
            draw.text((70, 93), "MPB", fill="#17202B", font=font)

        image.save(
            icon_path,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        return icon_path
    except Exception:
        return None


def apply_icon(root: tk.Tk | tk.Toplevel, icon_path: Path | None) -> None:
    if icon_path is None or not icon_path.exists():
        return

    try:
        root.iconbitmap(str(icon_path))
    except tk.TclError:
        pass

    try:
        root.iconbitmap(default=str(icon_path))
    except tk.TclError:
        pass


def install_window_shell(
    root: tk.Tk | tk.Toplevel,
    *,
    app_id: str = DEFAULT_APP_ID,
    icon_path: Path | None = None,
    icon_source: Path | None = None,
) -> None:
    configure_process(app_id)

    if icon_path is not None:
        apply_icon(root, ensure_icon(icon_path, icon_source))

    if sys.platform == "win32":
        _apply_dwm_frame(root)

    root.bind("<Map>", lambda _event: root.after_idle(root.update_idletasks), add="+")
    root.bind("<Configure>", _debounced_idle_refresh(root), add="+")


def reveal_when_ready(root: tk.Tk | tk.Toplevel, delay_ms: int = 35) -> None:
    def reveal() -> None:
        try:
            root.update_idletasks()
            root.deiconify()
            root.lift()
        except tk.TclError:
            pass

    root.after(delay_ms, reveal)


def _debounced_idle_refresh(root: tk.Tk | tk.Toplevel):
    state = {"after_id": None}

    def refresh(_event: tk.Event) -> None:
        if state["after_id"] is not None:
            try:
                root.after_cancel(state["after_id"])
            except tk.TclError:
                pass
        state["after_id"] = root.after(40, root.update_idletasks)

    return refresh


def _apply_dwm_frame(root: tk.Tk | tk.Toplevel) -> None:
    try:
        hwnd = root.winfo_id()
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        value = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass
