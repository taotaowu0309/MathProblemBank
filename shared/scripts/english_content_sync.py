"""Keep the English question bank complete on whichever machine starts.

The question sets parsed from the Ray Du books (《旋元佑文法解题》 grammar
exercises, 《旋元佑英文阅读》 Part 2 reading tests, 《旋元佑英文写作》 closing
writing exercises, the reading tests of 《旋元佑英文阅读通》, parsed from its PDF)
and the exam papers sent
through the import inbox are content, not user input, so nobody should have
to press an import button for them.  ``sync_english_question_sets`` brings the
bank up to date: each set is previewed against the database (by content
digest), and only a set whose questions are missing or changed is imported,
through the same backup / transaction / readback executors as before.  An
unchanged bank is not touched and no backup is made.

The control center runs it in a separate process when it starts, on the Mac
and on Windows alike (``python -m shared.scripts.english_content_sync --output
result.json``): PDF text extraction is mostly Python code, and in a thread it
would compete with the interface for the interpreter lock.  The database syncs
both ways, so whichever machine starts first after a change imports it and the
other one finds nothing to do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from shared.scripts.application_paths import APP_PATHS


def sync_english_question_sets(
    english_service: Any,
    *,
    inbox: Any = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Import every book question set and inbox exam paper that the bank lacks or has outdated."""

    from shared.scripts.import_inbox import ImportInbox

    def say(text: str) -> None:
        if progress is not None:
            progress(text)

    results: list[dict[str, Any]] = []
    books = () if APP_PATHS.public_release else (
        ("《旋元佑文法解题》", english_service.preview_raydu_grammar_question_bank,
         english_service.import_raydu_grammar_question_bank),
        ("《旋元佑英文阅读》实战题", english_service.preview_raydu_reading_question_bank,
         english_service.import_raydu_reading_question_bank),
        ("《旋元佑英文写作》写作练习", english_service.preview_raydu_writing_question_bank,
         english_service.import_raydu_writing_question_bank),
        ("《旋元佑英文阅读通》阅读测验", english_service.preview_raydu_highpoint_question_bank,
         english_service.import_raydu_highpoint_question_bank),
    )
    for title, preview, run_import in books:
        say(f"检查 {title}…")
        entry: dict[str, Any] = {"set": title}
        try:
            checked = preview()
        except FileNotFoundError as error:
            results.append({**entry, "status": "source_missing", "message": str(error)})
            continue
        except Exception as error:
            results.append({**entry, "status": "failed", "message": str(error)})
            continue
        entry.update({"question_count": checked["question_count"], "new": checked["new"], "updated": checked["updated"]})
        if checked.get("removed_question_codes"):
            # Questions the source no longer has are reported, never deleted.
            entry["removed_question_codes"] = checked["removed_question_codes"]
        if not checked["can_import"]:
            results.append({**entry, "status": "blocked", "message": "；".join(checked["issues"][:5])})
            continue
        # A set whose questions are unchanged can still have changed group texts.
        if not checked.get("needs_import", bool(checked["new"] or checked["updated"])):
            results.append({**entry, "status": "up_to_date"})
            continue
        say(f"导入 {title}…")
        try:
            imported = run_import()
        except Exception as error:
            results.append({**entry, "status": "failed", "message": str(error)})
            continue
        results.append({
            **entry,
            "status": "imported" if imported["readback_verified"] else "readback_mismatch",
            "written": imported.get("written"),
            "backup_path": imported.get("backup_path", ""),
        })

    inbox = inbox or ImportInbox()
    for package in inbox.exam_papers() if inbox.available() else []:
        entry = {"set": package["title"] or package["paper_code"], "paper_file": package["paper_file"]}
        if package["issues"]:
            results.append({**entry, "status": "blocked", "message": "；".join(package["issues"][:5])})
            continue
        try:
            checked = english_service.preview_exam_paper(paper_file=package["paper_file"])
        except Exception as error:
            results.append({**entry, "status": "failed", "message": str(error)})
            continue
        entry.update({"question_count": checked["question_count"], "new": checked["new"], "updated": checked["updated"]})
        entry.update({key: checked.get(key, []) for key in ("removed_question_codes", "removed_group_codes")})
        entry["updated_groups"] = checked.get("updated_groups", 0)
        if not checked["can_import"]:
            results.append({**entry, "status": "blocked", "message": "；".join(checked["issues"][:5])})
            continue
        if not checked.get("needs_import", bool(checked["new"] or checked["updated"])):
            removed = entry["removed_question_codes"] or entry["removed_group_codes"]
            results.append({**entry, "status": "source_removed" if removed else "up_to_date"})
            continue
        say(f"导入试卷 {entry['set']}…")
        try:
            imported = english_service.import_exam_paper(paper_file=package["paper_file"])
        except Exception as error:
            results.append({**entry, "status": "failed", "message": str(error)})
            continue
        results.append({
            **entry,
            "status": "imported" if imported["readback_verified"] else "readback_mismatch",
            "written": imported.get("stored"),
            "backup_path": imported.get("backup_path", ""),
        })
    return {
        "results": results,
        "imported": [item for item in results if item["status"] == "imported"],
        "problems": [item for item in results if item["status"] in {"failed", "blocked", "readback_mismatch", "source_removed"}
                     or item.get("removed_question_codes") or item.get("removed_group_codes")],
        "counts": english_service.question_bank_counts(),
    }


def summary_text(summary: dict[str, Any]) -> str:
    """A short Chinese report of one sync, for the control center's message."""

    names = {
        "imported": "已导入", "up_to_date": "已是最新", "source_missing": "找不到原书源码",
        "blocked": "有问题未导入", "failed": "导入失败", "readback_mismatch": "回读不一致",
        "source_removed": "源内容有移除，数据库记录保留",
    }
    lines = []
    for item in summary["results"]:
        detail = ""
        if item["status"] == "imported":
            detail = f"（新增 {item.get('new', 0)}，更新 {item.get('updated', 0)}）"
        elif item.get("message"):
            detail = f"：{item['message']}"
        if item.get("updated_groups"):
            detail += f"；更新文章组 {item['updated_groups']} 个"
        if item.get("removed_question_codes"):
            detail += "；源中已不存在的题目（保留学习记录）：" + "、".join(item["removed_question_codes"])
        if item.get("removed_group_codes"):
            detail += "；源中已不存在的文章组（保留）：" + "、".join(item["removed_group_codes"])
        lines.append(f"{item['set']}：{names.get(item['status'], item['status'])}{detail}")
    counts = summary.get("counts") or {}
    lines.append(
        f"题库现有：文法 {counts.get('grammar', 0)} 题，阅读 {counts.get('reading', 0)} 题，写作 {counts.get('writing', 0)} 题。"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run one sync and write the summary (or the error) as JSON to ``--output``."""

    import argparse
    import json
    import os
    import sys

    parser = argparse.ArgumentParser(description="补齐英语题库（原书题目与收件箱试卷）")
    parser.add_argument("--root", default="", help="English 数据目录（默认为项目的 English/）")
    parser.add_argument("--output", required=True, help="把结果写成 JSON 的文件")
    arguments = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from shared.scripts.english_learning_service import EnglishLearningService

    try:
        service = EnglishLearningService(arguments.root) if arguments.root else EnglishLearningService()
        summary = sync_english_question_sets(service)
        summary["summary_text"] = summary_text(summary)
    except Exception as error:  # Reported to the control center instead of a silent exit.
        summary = {"error": f"{type(error).__name__}: {error}"}
    output = Path(arguments.output)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(temporary, output)
    return 0 if "error" not in summary else 1


__all__ = ["main", "summary_text", "sync_english_question_sets"]


if __name__ == "__main__":
    raise SystemExit(main())
