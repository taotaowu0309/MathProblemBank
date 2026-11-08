"""Keep the installed edge-tts new enough for Microsoft's current handshake.

edge-tts 7.2.8 and earlier send a v1 Sec-MS-GEC token. Since October 2026 the
read-aloud service answers that handshake with HTTP 403, so a word with no
cached recording stays silent while an already downloaded example MP3 still
plays. 7.3.1 sends the v2 token. This upgrades the package for the current
interpreter when it is older; a current install is left alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

MINIMUM = (7, 3, 1)
_SPEC = "edge-tts>=7.3.1,<8"
_UPGRADE_LOCK = threading.Lock()
_upgrade_result: str | None = None


def edge_tts_version() -> tuple[int, int, int]:
    try:
        from importlib.metadata import version

        raw = version("edge-tts")
    except Exception:
        return (0, 0, 0)
    numbers: list[int] = []
    for part in raw.split("."):
        digits = ""
        for character in part:
            if character.isdigit():
                digits += character
            else:
                break
        if not digits:
            break
        numbers.append(int(digits))
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def ensure_edge_tts() -> str:
    """Return '' when edge-tts can speak, otherwise a short reason it cannot."""

    global _upgrade_result
    if edge_tts_version() >= MINIMUM:
        return ""
    if os.environ.get("MPB_SKIP_EDGE_TTS_UPGRADE") == "1":
        found = ".".join(str(part) for part in edge_tts_version())
        return f"edge-tts {found} cannot complete the current online handshake; {_SPEC} is required"
    with _UPGRADE_LOCK:
        if edge_tts_version() >= MINIMUM:
            return ""
        if _upgrade_result is None:
            try:
                _upgrade_result = _install_edge_tts()
            except (OSError, subprocess.SubprocessError) as error:
                _upgrade_result = f"语音组件安装失败：{error}"
        return _upgrade_result


def _install_edge_tts() -> str:
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", _SPEC],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        **kwargs,
    )
    for name in list(sys.modules):
        if name == "edge_tts" or name.startswith("edge_tts."):
            sys.modules.pop(name, None)
    if edge_tts_version() >= MINIMUM:
        return ""
    detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:300]
    return detail or f"could not install {_SPEC}"
