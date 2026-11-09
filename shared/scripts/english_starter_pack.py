"""Original sample content that makes a fresh public English workspace usable.

The public release ships no textbooks, so its English pages would otherwise
start empty.  On the first start this installs one self-written reading, one
small practice paper and a short word list through the normal import services.
The installed marker keeps it from returning after a learner removes it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS

STARTER_PACK_FILE = APP_PATHS.application_root / "shared" / "templates" / "english_starter" / "starter_pack.json"
STARTER_AUDIOBOOKS = STARTER_PACK_FILE.parent / "audiobook"
MARKER_NAME = "starter_pack_installed.json"


def _reading_pdf(text: str, destination: Path) -> Path:
    import fitz

    destination.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    try:
        page = document.new_page(width=595, height=842)
        title, _, body = text.partition("\n\n")
        page.insert_textbox(fitz.Rect(64, 64, 531, 110), title, fontsize=20, fontname="tibo")
        page.insert_textbox(fitz.Rect(64, 120, 531, 790), body, fontsize=13, fontname="tiro", lineheight=1.5)
        document.save(destination)
    finally:
        document.close()
    return destination


def install_starter_pack(service: Any, *, pack_file: Path = STARTER_PACK_FILE) -> dict[str, Any]:
    """Install the sample once; a later call only reports that it is already there."""

    from shared.scripts.vocabulary_manager import VocabularyManager

    data_dir = Path(service.database_path).parent
    marker = data_dir / MARKER_NAME
    if marker.is_file():
        return {"installed": False, "reason": "already_installed"}
    pack = json.loads(Path(pack_file).read_text(encoding="utf-8"))
    reading = pack["reading"]
    pdf = _reading_pdf(reading["text"], data_dir / "starter" / "a-small-reading-plan.pdf")
    material = service.import_material(
        pdf,
        title=reading["title"],
        author=reading["author"],
        role="extensive_reading",
        material_code=reading["material_code"],
    )
    paper = service.import_exam_paper(pack["paper"])
    words = VocabularyManager(workspace="english", root_dir=APP_PATHS.vocabulary_root).import_entries(pack["vocabulary"])
    from shared.scripts.audiobook_service import DEFAULT_AUDIOBOOK_ROOT

    for book in sorted(STARTER_AUDIOBOOKS.iterdir()) if STARTER_AUDIOBOOKS.is_dir() else []:
        target = DEFAULT_AUDIOBOOK_ROOT / book.name
        if book.is_dir() and not target.exists():
            shutil.copytree(book, target)
    result = {
        "version": pack["version"],
        "material_id": material.get("id") or material.get("material_id"),
        "questions": paper.get("stored"),
        "words": words.get("imported", words.get("inserted", len(pack["vocabulary"]))),
    }
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"installed": True, **result}


__all__ = ["MARKER_NAME", "STARTER_PACK_FILE", "install_starter_pack"]
