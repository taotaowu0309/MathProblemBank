from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.scripts.study_project_service import load_subjects



def check_database(database_path: Path) -> None:
    if not database_path.exists():
        raise FileNotFoundError(
            f"找不到数据库文件：{database_path}"
        )

    with sqlite3.connect(database_path) as connection:
        result = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()

    if result is None or result[0] != "ok":
        raise RuntimeError(
            f"数据库完整性检查失败：{database_path}"
        )


def create_backup(
    database_path: Path,
    backup_directory: Path,
) -> Path:
    check_database(database_path)

    backup_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    backup_path = (
        backup_directory
        / f"{database_path.stem}_{timestamp}.db"
    )

    with sqlite3.connect(database_path) as source_connection:
        with sqlite3.connect(backup_path) as backup_connection:
            source_connection.backup(backup_connection)

    check_database(backup_path)

    return backup_path


def remove_old_backups(
    backup_directory: Path,
    database_stem: str,
    keep_count: int = 30,
) -> None:
    backup_files = sorted(
        backup_directory.glob(
            f"{database_stem}_*.db"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for old_backup in backup_files[keep_count:]:
        old_backup.unlink()


def copy_latest_backup(
    backup_path: Path,
    backup_directory: Path,
    database_stem: str,
) -> Path:
    latest_path = (
        backup_directory
        / f"{database_stem}_latest.db"
    )

    shutil.copy2(
        backup_path,
        latest_path,
    )

    return latest_path


def main() -> None:
    print("开始备份题库数据库。")
    print()

    subjects = load_subjects()
    if not subjects:
        print("当前工作区没有可备份的学科。")
        return

    for subject_name, configuration in subjects.items():
        source_path = configuration["db"]
        backup_directory = configuration["backups"]

        backup_path = create_backup(
            database_path=source_path,
            backup_directory=backup_directory,
        )

        latest_path = copy_latest_backup(
            backup_path=backup_path,
            backup_directory=backup_directory,
            database_stem=source_path.stem,
        )

        remove_old_backups(
            backup_directory=backup_directory,
            database_stem=source_path.stem,
            keep_count=30,
        )

        print(f"{subject_name} 备份完成：")
        print(f"历史备份：{backup_path}")
        print(f"最新备份：{latest_path}")
        print()

    print("全部数据库备份完成。")


if __name__ == "__main__":
    main()
