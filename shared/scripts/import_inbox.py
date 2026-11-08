"""The import inbox: content packages prepared on the Mac.

Since 2026-10-28 subject data syncs both ways, so the Mac may import into its
own database directly; the inbox remains as a staging place for packages
whose source files (for an exam paper, its JSON and PDF) should be kept.  It
is the folder ``MathProblemBank-inbox`` beside the checkout, a separate
Syncthing folder, Send Only on the Mac and Receive Only on Windows.  The
start-up content sync imports a package on either machine through the same
executor as a manual import (preview, backup, transaction, readback); a
package is pending while its content differs from what the database holds,
so no separate log is needed and importing twice changes nothing.

See docs/MAC_WINDOWS_SYNC.zh-CN.md for the Syncthing setup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
# Beside the checkout on both machines, like EnglishProjects.
DEFAULT_INBOX_ROOT = ROOT_DIR.parent / "MathProblemBank-inbox"
EXAM_PAPER_DIR = "english_exam_papers"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ImportInbox:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_INBOX_ROOT

    def available(self) -> bool:
        return self.root.is_dir()

    @staticmethod
    def is_sender() -> bool:
        """The Mac sends packages into the inbox folder (see the sync guide)."""

        return sys.platform == "darwin"

    def send_exam_paper(self, paper_file: Path | str | None = None, *, paper: Any = None) -> dict[str, Any]:
        """Copy an exam paper (a JSON file, or the object an AI prepared) and its PDF into the inbox.

        The package lives in ``english_exam_papers/<paper_code>/``; the JSON's
        ``source_pdf`` is rewritten to the copied PDF's file name so it
        resolves beside the JSON on the receiving machine.
        """

        from shared.scripts.english_exam_paper import build_exam_rows, load_paper

        if not self.available():
            raise FileNotFoundError(f"收件箱文件夹不存在：{self.root}（按同步说明先建好并在 Syncthing 中共享）")
        paper = load_paper(paper, Path(str(paper_file)).expanduser().resolve() if paper_file else None)
        built = build_exam_rows(paper)
        if built["issues"]:
            raise ValueError("试卷有问题，未放入收件箱：\n" + "\n".join(built["issues"][:12]))
        package = self.root / EXAM_PAPER_DIR / built["paper_code"]
        package.mkdir(parents=True, exist_ok=True)
        pdf_name = ""
        pdf = Path(str(paper.get("source_pdf") or ""))
        if str(paper.get("source_pdf") or "").strip() and pdf.is_file():
            pdf_name = re.sub(r'[\\/:*?"<>|]+', "_", pdf.name)
            target_pdf = package / pdf_name
            if not target_pdf.is_file() or _sha256(target_pdf) != _sha256(pdf):
                temporary = target_pdf.with_name(target_pdf.name + ".tmp")
                shutil.copy2(pdf, temporary)
                os.replace(temporary, target_pdf)
        paper["source_pdf"] = pdf_name
        target_json = package / f"{built['paper_code']}.json"
        text = json.dumps(paper, ensure_ascii=False, indent=2)
        temporary = target_json.with_name(target_json.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target_json)
        written = target_json.read_text(encoding="utf-8") == text
        pdf_ok = not pdf_name or _sha256(package / pdf_name) == _sha256(pdf)
        return {
            "paper_code": built["paper_code"], "title": built["title"], "paper_label": built["paper_label"],
            "package": str(package), "paper_file": str(target_json),
            "pdf_file": str(package / pdf_name) if pdf_name else "",
            "readback_verified": written and pdf_ok,
        }

    def exam_papers(self) -> list[dict[str, Any]]:
        """Every exam-paper package in the inbox, newest paper first."""

        from shared.scripts.english_exam_paper import build_exam_rows, load_paper

        folder = self.root / EXAM_PAPER_DIR
        if not folder.is_dir():
            return []
        packages: list[dict[str, Any]] = []
        for paper_file in sorted(folder.glob("*/*.json")):
            try:
                built = build_exam_rows(load_paper(paper_file=paper_file))
                packages.append({
                    "paper_file": str(paper_file), "paper_code": built["paper_code"], "title": built["title"],
                    "paper_label": built["paper_label"], "year": built["year"],
                    "question_count": len(built["questions"]), "issues": built["issues"],
                })
            except Exception as error:  # A half-synced or broken file must not hide the others.
                packages.append({"paper_file": str(paper_file), "paper_code": paper_file.stem, "title": paper_file.name,
                                 "paper_label": "", "year": 0, "question_count": 0, "issues": [str(error)]})
        return sorted(packages, key=lambda item: (-int(item["year"] or 0), item["paper_code"]))

    def pending_exam_papers(self, english_service: Any) -> list[dict[str, Any]]:
        """Packages whose questions are not yet in this machine's question bank (or differ)."""

        pending: list[dict[str, Any]] = []
        for package in self.exam_papers():
            if package["issues"]:
                pending.append({**package, "new": 0, "updated": 0, "can_import": False})
                continue
            preview = english_service.preview_exam_paper(paper_file=package["paper_file"])
            if preview["new"] or preview["updated"]:
                pending.append({**package, "new": preview["new"], "updated": preview["updated"],
                                "can_import": preview["can_import"]})
        return pending


__all__ = ["DEFAULT_INBOX_ROOT", "EXAM_PAPER_DIR", "ImportInbox"]
