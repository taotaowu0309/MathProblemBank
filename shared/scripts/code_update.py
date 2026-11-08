"""Publish and receive program code through Git before the control center starts.

Both machines run this: Windows from LaunchStudyProblemBank.vbs, and every
launch of launch_study_problem_bank.pyw (the Mac .command and the Mac app).
It never blocks the launch. A problem leaves the current code in place and is
written to logs/code_update.log.

- Behind origin, with a clean worktree: fast-forward. A changed Python file
  that does not compile is rolled back with ``git reset --keep``.
- Ahead of origin, with a clean worktree: push. No force.
- Diverged, with a clean worktree: rebase local commits onto origin, compile,
  then push. A conflict aborts the rebase and keeps both sides. A compile
  failure restores the pre-rebase commits.
- Uncommitted work: not touched, not rebased, not pushed.

Only code is tracked by Git (see the allowlist block in .gitignore), so this
never touches databases, collections, PDFs or course data.

Manual use:
    python shared/scripts/code_update.py             # update now
    python shared/scripts/code_update.py --rollback  # return to the code before the last update
                                                     # and pause updates (logs/code_update.disabled)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = "origin"
FETCH_TIMEOUT_SECONDS = 25
LOCK_STALE_SECONDS = 600


class Updater:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.log_dir = root / "logs"
        self.log_path = self.log_dir / "code_update.log"
        self.state_path = self.log_dir / "code_update_last.json"
        self.lock_path = self.log_dir / "code_update.lock"
        self.git = find_git()

    def log(self, message: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")

    def run_git(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(
            [str(self.git), *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **kwargs,
        )

    def git_out(self, *args: str) -> str:
        result = self.run_git(*args)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    def acquire_lock(self) -> bool:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.lock_path.exists() and time.time() - self.lock_path.stat().st_mtime > LOCK_STALE_SECONDS:
                self.lock_path.unlink()
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True

    def release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:
            pass

    def worktree_dirty(self) -> bool:
        return bool(self.git_out("status", "--porcelain").strip())

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.run_git("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0

    def remember(self, previous: str, current: str, branch: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {"previous": previous, "current": current, "branch": branch, "at": datetime.now().isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )

    def update(self) -> str:
        if self.git is None:
            return "skip: git not found"
        branch = self.git_out("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            return "skip: detached HEAD"
        fetch = self.run_git("fetch", "--quiet", REMOTE, branch, timeout=FETCH_TIMEOUT_SECONDS)
        if fetch.returncode != 0:
            return f"skip: fetch failed: {fetch.stderr.strip()[:300]}"
        local = self.git_out("rev-parse", "HEAD")
        remote = self.git_out("rev-parse", "FETCH_HEAD")
        if local == remote:
            return f"up to date at {local[:10]}"
        if self.worktree_dirty():
            return (
                f"skip: uncommitted changes left untouched "
                f"(local {local[:10]}, {REMOTE}/{branch} {remote[:10]})"
            )
        if self.is_ancestor(local, remote):
            return self.fast_forward(branch, local, remote)
        if self.is_ancestor(remote, local):
            return self.push_commits(branch, remote, local)
        return self.rebase_and_push(branch, local, remote)

    def fast_forward(self, branch: str, local: str, remote: str) -> str:
        merge = self.run_git("merge", "--ff-only", "--quiet", remote)
        if merge.returncode != 0:
            return f"skip: fast-forward refused: {merge.stderr.strip()[:500]}"
        broken = self.compile_changed(local, remote)
        if broken:
            rollback = self.run_git("reset", "--keep", local)
            status = "rolled back" if rollback.returncode == 0 else f"ROLLBACK FAILED: {rollback.stderr.strip()}"
            return f"update {local[:10]}..{remote[:10]} does not compile ({'; '.join(broken)}); {status}"
        self.remember(local, remote, branch)
        return f"updated {local[:10]} -> {remote[:10]} on {branch}"

    def push_commits(self, branch: str, remote: str, local: str) -> str:
        push = self.run_git("push", "--quiet", REMOTE, f"HEAD:{branch}", timeout=FETCH_TIMEOUT_SECONDS)
        if push.returncode != 0:
            return (
                f"skip: push refused; local {local[:10]} kept, {REMOTE}/{branch} unchanged: "
                f"{push.stderr.strip()[:300]}"
            )
        return f"pushed {remote[:10]} -> {local[:10]} on {branch}"

    def rebase_and_push(self, branch: str, local: str, remote: str) -> str:
        rebase = self.run_git("rebase", remote)
        if rebase.returncode != 0:
            detail = (rebase.stderr or rebase.stdout).strip().replace("\n", " ")[:300]
            abort = self.run_git("rebase", "--abort")
            abort_note = "" if abort.returncode == 0 else f"; abort failed: {abort.stderr.strip()[:200]}"
            return (
                f"skip: rebase onto {remote[:10]} stopped ({detail or 'conflict'}); "
                f"local {local[:10]} and {REMOTE}/{branch} kept{abort_note}"
            )
        rebased = self.git_out("rev-parse", "HEAD")
        broken = self.compile_changed(remote, rebased)
        if broken:
            restore = self.run_git("reset", "--keep", local)
            status = "restored" if restore.returncode == 0 else f"RESTORE FAILED: {restore.stderr.strip()}"
            return (
                f"skip: rebase onto {remote[:10]} does not compile ({'; '.join(broken)}); "
                f"{status} {local[:10]}"
            )
        pushed = self.push_commits(branch, local, rebased)
        if pushed.startswith("skip:"):
            return pushed
        self.remember(local, rebased, branch)
        return f"rebased and pushed {local[:10]} -> {rebased[:10]} on {branch}"

    def compile_changed(self, old: str, new: str) -> list[str]:
        names = self.git_out("diff", "--name-only", "--diff-filter=AM", old, new, "--", "*.py", "*.pyw")
        broken = []
        for name in filter(None, names.splitlines()):
            path = self.root / name
            try:
                compile(path.read_bytes(), str(path), "exec")
            except (SyntaxError, ValueError, OSError) as exc:
                broken.append(f"{name}: {exc}")
        return broken

    def rollback(self) -> str:
        if self.git is None:
            return "rollback impossible: git not found"
        if not self.state_path.is_file():
            return "rollback impossible: no recorded update"
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        head = self.git_out("rev-parse", "HEAD")
        if head != state["current"]:
            return f"rollback refused: HEAD {head[:10]} is not the last update {state['current'][:10]}"
        result = self.run_git("reset", "--keep", state["previous"])
        if result.returncode != 0:
            return f"rollback failed: {result.stderr.strip()}"
        # Stay on the old code until the user deletes this marker.
        (self.log_dir / "code_update.disabled").write_text(
            f"rolled back from {state['current']}; delete this file to resume automatic updates\n",
            encoding="utf-8",
        )
        return f"rolled back {state['current'][:10]} -> {state['previous'][:10]}; automatic updates paused (delete logs/code_update.disabled to resume)"


def find_git() -> Path | None:
    found = shutil.which("git")
    if found:
        return Path(found)
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            for relative in (r"Git\cmd\git.exe", r"Programs\Git\cmd\git.exe"):
                candidate = Path(base) / relative
                if candidate.is_file():
                    return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rollback", action="store_true", help="undo the last recorded update")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    updater = Updater(args.root.resolve())
    if (updater.log_dir / "code_update.disabled").exists() and not args.rollback:
        return 0
    if not updater.acquire_lock():
        updater.log("skip: another update is running")
        return 0
    try:
        message = updater.rollback() if args.rollback else updater.update()
    except Exception as exc:  # never block the launch
        message = f"error: {type(exc).__name__}: {exc}"
    finally:
        updater.release_lock()
    updater.log(message)
    if sys.stdout is not None:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
