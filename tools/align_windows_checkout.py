"""One-time alignment of the Windows checkout with the Mac-developed code branch.

Background: the repository used to track subject databases, collections and
PDFs. The Mac commit that introduced the code-sync allowlist (.gitignore) stops
tracking them. A plain `git pull` of that commit on Windows would delete those
files from disk, so this script moves the Windows checkout over without ever
touching user data:

1. fetch origin/<branch> and check it carries the allowlist marker;
2. back up every code file that the target would overwrite (outside the project);
3. `git reset --mixed` to the target: HEAD and index move, the working tree is untouched,
   so the formerly tracked data files simply become untracked/ignored;
4. `git checkout -- .` restores only the (code-only) tracked files;
5. verify every formerly tracked data file is still on disk with the same size.

Default is a dry run that prints the plan. Add --apply to execute.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# >>> MPB code-sync allowlist >>>"


def git(root: Path, *args: str, check: bool = True, binary: bool = False):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=600)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.decode('utf-8', 'replace')}")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def ls_tree(root: Path, rev: str) -> list[str]:
    out = git(root, "ls-tree", "-r", "-z", "--name-only", rev, binary=True)
    return [item.decode("utf-8") for item in out.split(b"\0") if item]


def normalize(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = (args.backup_dir or root.parent / f"{root.name}-code-backup-{stamp}").resolve()
    if backup_dir == root or root in backup_dir.parents:
        raise SystemExit("backup directory must be outside the project")

    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        raise SystemExit("detached HEAD; check out the shared branch first")
    git(root, "fetch", "origin", branch)
    head = git(root, "rev-parse", "HEAD").strip()
    target = git(root, "rev-parse", "FETCH_HEAD").strip()
    if MARKER not in git(root, "show", f"{target}:.gitignore"):
        raise SystemExit(f"origin/{branch} {target[:10]} has no code-sync allowlist yet; nothing to do")
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, target], cwd=root).returncode != 0:
        raise SystemExit(f"local HEAD {head[:10]} has commits not on origin/{branch}; stop and reconcile by hand")

    target_files = ls_tree(root, target)
    target_set = set(target_files)
    data_files = [name for name in ls_tree(root, head) if name not in target_set]
    data_on_disk = {name: (root / name).stat().st_size for name in data_files if (root / name).is_file()}

    overwritten, created, same = [], [], 0
    for name in target_files:
        path = root / name
        if not path.is_file():
            created.append(name)
            continue
        blob = git(root, "cat-file", "blob", f"{target}:{name}", binary=True)
        if normalize(path.read_bytes()) == normalize(blob):
            same += 1
        else:
            overwritten.append(name)

    print(f"branch            : {branch}")
    print(f"local HEAD        : {head}")
    print(f"target            : {target}")
    print(f"code files        : {len(target_files)} (same {same}, overwritten {len(overwritten)}, created {len(created)})")
    print(f"data files kept   : {len(data_on_disk)} on disk become untracked (not modified)")
    for name in overwritten:
        print(f"  overwrite: {name}")
    for name in created:
        print(f"  create   : {name}")
    if not args.apply:
        print(f"\nDry run. Backup would go to {backup_dir}\nRe-run with --apply to execute.")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"branch": branch, "previous_head": head, "target": target, "files": {}}
    for name in overwritten:
        source = root / name
        dest = backup_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        manifest["files"][name] = hashlib.sha256(source.read_bytes()).hexdigest()
    (backup_dir / "status_before.txt").write_bytes(git(root, "status", "--porcelain", binary=True))
    (backup_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"backup written: {backup_dir}")

    git(root, "reset", "--mixed", "-q", target)
    git(root, "checkout", "--", ".")

    missing = [name for name, size in data_on_disk.items() if not (root / name).is_file() or (root / name).stat().st_size != size]
    dirty = git(root, "status", "--porcelain", "--untracked-files=no").strip()
    if missing:
        print("DATA CHECK FAILED, files missing or changed:\n" + "\n".join(missing[:50]))
        return 1
    if dirty:
        print("warning: tracked files still differ after checkout:\n" + dirty)
    print(f"done: HEAD {head[:10]} -> {target[:10]}; {len(data_on_disk)} data files intact; code backup in {backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
