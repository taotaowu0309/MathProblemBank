from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.scripts.study_project_service import load_subjects

TIMESTAMP_PATTERN = re.compile(
    r"_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$"
)


@dataclass(frozen=True)
class BackupItem:
    path: Path
    category: str
    timestamp: float
    is_directory: bool


def is_latest_alias(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith("_latest.db") or name in {
        "math_analysis_latest.db",
        "higher_algebra_latest.db",
    }


def classify_backup(path: Path) -> str | None:
    if path.is_file() and path.suffix.lower() == ".db":
        if is_latest_alias(path):
            return None
        return "database"

    if path.is_dir() and path.name.startswith(
        "chapters_before_export_"
    ):
        return "chapters"

    return None


def collect_items(backup_dir: Path) -> list[BackupItem]:
    items: list[BackupItem] = []

    if not backup_dir.exists():
        return items

    for path in backup_dir.iterdir():
        category = classify_backup(path)

        if category is None:
            continue

        try:
            timestamp = path.stat().st_mtime
        except OSError:
            continue

        items.append(
            BackupItem(
                path=path,
                category=category,
                timestamp=timestamp,
                is_directory=path.is_dir(),
            )
        )

    return items


def split_keep_delete(
    items: Iterable[BackupItem],
    keep_db: int,
    keep_chapters: int,
) -> tuple[list[BackupItem], list[BackupItem]]:
    grouped: dict[str, list[BackupItem]] = defaultdict(list)

    for item in items:
        grouped[item.category].append(item)

    keep: list[BackupItem] = []
    delete: list[BackupItem] = []

    limits = {
        "database": keep_db,
        "chapters": keep_chapters,
    }

    for category, category_items in grouped.items():
        category_items.sort(
            key=lambda item: item.timestamp,
            reverse=True,
        )
        limit = limits.get(category, 0)
        keep.extend(category_items[:limit])
        delete.extend(category_items[limit:])

    return keep, delete


def human_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0

    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass

    return total


def format_bytes(size: int) -> str:
    value = float(size)

    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{size} B"


def delete_item(item: BackupItem) -> None:
    if item.is_directory:
        shutil.rmtree(item.path)
    else:
        item.path.unlink()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "清理当前工作区题库旧备份。默认只预览，不真正删除。"
        )
    )

    parser.add_argument(
        "--subject",
        default="all",
        help="学科名称；all=当前工作区全部学科。兼容 math/algebra 旧别名。",
    )
    parser.add_argument(
        "--keep-db",
        type=int,
        default=5,
        help=(
            "每门课保留最近多少个带时间戳的数据库备份。"
            "latest.db 别名始终保留。默认 5。"
        ),
    )
    parser.add_argument(
        "--keep-chapters",
        type=int,
        default=3,
        help=(
            "每门课保留最近多少个章节目录备份。默认 3。"
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正执行删除；不加时只预览。",
    )

    arguments = parser.parse_args()

    if arguments.keep_db < 1:
        parser.error("--keep-db 必须至少为 1。")

    if arguments.keep_chapters < 1:
        parser.error("--keep-chapters 必须至少为 1。")

    return arguments


def subject_backup_dirs() -> dict[str, Path]:
    subjects = load_subjects()
    return {name: cfg["backups"] for name, cfg in subjects.items()}


def selected_subjects(selection: str, backup_dirs: dict[str, Path]) -> list[str]:
    if selection == "all":
        return list(backup_dirs)
    aliases = {
        "math": "数学分析",
        "algebra": "高等代数",
    }
    resolved = aliases.get(selection, selection)
    return [resolved] if resolved in backup_dirs else []


def main() -> None:
    arguments = parse_arguments()
    backup_dirs = subject_backup_dirs()

    subject_keys = selected_subjects(arguments.subject, backup_dirs)
    if not subject_keys:
        print("当前工作区没有匹配的备份目录。")
        return

    total_delete_size = 0
    all_delete: list[BackupItem] = []

    for subject_key in subject_keys:
        backup_dir = backup_dirs[subject_key]
        items = collect_items(backup_dir)
        keep, delete = split_keep_delete(
            items,
            arguments.keep_db,
            arguments.keep_chapters,
        )

        print()
        print(f"[{subject_key}] {backup_dir}")
        print(
            f"保留数据库备份：最近 {arguments.keep_db} 个"
        )
        print(
            f"保留章节目录备份：最近 {arguments.keep_chapters} 个"
        )

        if keep:
            print("将保留：")
            for item in sorted(
                keep,
                key=lambda value: value.timestamp,
                reverse=True,
            ):
                print(f"  {item.path.name}")
        else:
            print("没有可保留的时间戳备份。")

        if delete:
            print("将删除：")
            for item in sorted(
                delete,
                key=lambda value: value.timestamp,
            ):
                size = human_size(item.path)
                total_delete_size += size
                all_delete.append(item)
                print(
                    f"  {item.path.name}"
                    f"  ({format_bytes(size)})"
                )
        else:
            print("没有需要删除的旧备份。")

        latest_aliases = [
            path.name
            for path in backup_dir.glob("*_latest.db")
        ]

        if latest_aliases:
            print(
                "latest.db 别名始终保留："
                + "，".join(latest_aliases)
            )

    print()
    print(
        "预计释放空间："
        + format_bytes(total_delete_size)
    )

    if not arguments.execute:
        print("当前只是预览，没有删除任何文件。")
        print("确认列表无误后，加 --execute 执行。")
        return

    if not all_delete:
        print("没有旧备份需要删除。")
        return

    confirmation = input(
        "请输入 DELETE OLD BACKUPS 确认："
    ).strip()

    if confirmation != "DELETE OLD BACKUPS":
        print("确认文字不匹配，操作已取消。")
        return

    deleted_count = 0

    for item in all_delete:
        delete_item(item)
        deleted_count += 1

    print()
    print(f"清理完成，共删除 {deleted_count} 个旧备份项目。")
    print(
        "每门课最近的数据库备份、章节备份和 latest.db 均已保留。"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"备份清理失败：{error}")
        sys.exit(1)
