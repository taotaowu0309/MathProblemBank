"""Small platform adapters shared by the existing desktop entry points."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def configure_runtime_environment() -> None:
    """Finder does not inherit a login shell's TeX or developer-tool PATH."""
    if sys.platform != "darwin":
        return
    root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("MKTEXTFM", "0")
    os.environ.setdefault("MPLCONFIGDIR", str(root / "shared" / "ui" / "cache" / "matplotlib"))
    candidates = [
        root / ".venv" / "bin",
        root / "online_courses" / "runtime" / "media_tools",
        root / "runtimes" / "bin",
        root / "runtimes" / "node-v24.22.0-darwin-arm64" / "bin",
        Path("/Library/TeX/texbin"),
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path.home() / ".elan" / "bin",
    ]
    existing = os.environ.get("PATH", os.defpath).split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(
        [str(path) for path in candidates if path.is_dir()] + existing
    ))


def venv_executable(root: Path, name: str = "python") -> Path:
    if os.name == "nt":
        return Path(root) / "Scripts" / f"{name}.exe"
    return Path(root) / "bin" / name


def open_chatgpt() -> None:
    """Bind Chrome explicitly to the existing chatgpt profile, without cookies."""
    if sys.platform == "darwin":
        chrome_root = Path.home() / "Library/Application Support/Google/Chrome"
        try:
            state = json.loads((chrome_root / "Local State").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("无法识别 Chrome 的 chatgpt Profile，请先在 Chrome 中确认该 Profile。") from error
        profiles = state.get("profile", {}).get("info_cache", {})
        matches = [key for key, value in profiles.items()
                   if value.get("name") == "chatgpt" and (chrome_root / key).is_dir()]
        matches = [key for key in matches if Path(key).name == key and key not in {".", ".."}]
        if len(matches) != 1:
            raise RuntimeError("无法唯一绑定现有 Chrome chatgpt Profile；未打开其他浏览器。")
        subprocess.run([
            "/usr/bin/open", "-n", "-a", "Google Chrome", "--args",
            f"--profile-directory={matches[0]}", "--new-window", "https://chatgpt.com/",
        ], check=True)
        return
    if os.name == "nt":
        os.startfile("https://chatgpt.com/")
        return
    raise RuntimeError("当前系统尚未配置 Chrome chatgpt Profile。")


def ui_point_size(size: float) -> float:
    """Keep Windows-designed point sizes: Qt maps 1 pt to 1 px on macOS but 4/3 px on Windows."""
    return size * 96 / 72 if sys.platform == "darwin" else size


def sans_serif_font() -> str:
    """Locate the font used by local OCR acceptance fixtures on this system."""
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).is_file():
            return candidate
    return "arial.ttf"


def uses_portable_latex_fonts() -> bool:
    """macOS lacks the Windows font aliases; the public release cannot assume Chinese Windows fonts."""
    from shared.scripts.application_paths import APP_PATHS

    return sys.platform == "darwin" or APP_PATHS.public_release


def portable_latex_source(source: str) -> str:
    """Use TeX-distributed font files when the system fonts cannot be relied on."""
    if not uses_portable_latex_fonts():
        return source
    source = source.replace("UTF8,scheme=plain", "UTF8,scheme=plain,fontset=fandol")
    source = source.replace(r"\documentclass[UTF8,12pt]{ctexart}", r"\documentclass[UTF8,fontset=fandol,12pt]{ctexart}")
    for family, stem in (
        ("TeX Gyre Termes", "texgyretermes"),
        ("TeX Gyre Heros", "texgyreheros"),
        ("TeX Gyre Cursor", "texgyrecursor"),
    ):
        source = source.replace("{" + family + "}", "{" + stem + "-regular.otf}")
        for command in ("setmainfont", "setsansfont", "setmonofont"):
            declaration = "\\" + command + "{" + stem + "-regular.otf}"
            source = source.replace(declaration, declaration + "[BoldFont=" + stem + "-bold.otf,ItalicFont=" + stem + "-italic.otf,BoldItalicFont=" + stem + "-bolditalic.otf]")
    return source.replace("{TeX Gyre Termes Math}", "{texgyretermes-math.otf}")
