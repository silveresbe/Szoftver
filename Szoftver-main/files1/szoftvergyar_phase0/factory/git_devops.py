"""Git/DevOps segéd (1. fázis): minimális, biztonságos git-műveletek a munkafán.

Ez nem LLM-ágens, hanem determinisztikus repo-helper. A cél, hogy a későbbi Supervisor
és a Git/DevOps komponens képes legyen csak a szükséges műveleteket elvégezni: branch,
checkout, status, add, commit, diff, worktree és rollback.
"""
from __future__ import annotations

import os
import subprocess
from typing import Iterable, Sequence


class GitDevOpsError(RuntimeError):
    """Deterministált repo-hiba, nem a Git által dobott nyers kivétel."""


def _run_git(repo_root: str, *args: str, check: bool = True) -> str:
    """Egy egyszerű, ellenőrzött git-hívás."""
    if not repo_root or not os.path.isdir(repo_root):
        raise GitDevOpsError(f"repo_root nem létezik: {repo_root!r}")
    p = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and p.returncode != 0:
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        detail = err or out or "git parancs sikertelen"
        raise GitDevOpsError(f"git {' '.join(args)}: {detail}")
    return (p.stdout or "").strip()


class GitDevOps:
    """Minimális Git helper a munkafához.

    Műveletek:
      - repo ellenőrzése / status
      - branch váltás / létrehozás
      - worktree létrehozás / tisztítás
      - diff / add / commit
      - restore / rollback
    """

    def __init__(self, repo_root: str, git_bin: str = "git"):
        self.repo_root = os.path.abspath(repo_root)
        self.git_bin = git_bin

    def _git(self, *args: str, check: bool = True) -> str:
        return _run_git(self.repo_root, *args, check=check)

    def ensure_repo(self) -> str:
        """A repo gyökérének ellenőrzése."""
        out = self._git("rev-parse", "--show-toplevel")
        if not out:
            raise GitDevOpsError("ez nem git-repo")
        return out

    def status(self, short: bool = True) -> dict:
        """Visszaadja a repo statusét strukturált formában."""
        self.ensure_repo()
        branch = self._git("branch", "--show-current")
        status = self._git("status", "--porcelain")
        files = [line[3:] for line in status.splitlines() if line.strip()] if status else []
        return {
            "branch": branch,
            "clean": not bool(files),
            "modified_files": files,
            "raw": status,
            "short": short,
        }

    def current_branch(self) -> str:
        return self._git("branch", "--show-current")

    def branch_exists(self, branch: str) -> bool:
        """True, ha a branch létezik."""
        try:
            self._git("rev-parse", "--verify", branch)
            return True
        except GitDevOpsError:
            return False

    def checkout(self, branch: str, *, create: bool = False) -> str:
        """Branch váltás, vagy új branch létrehozása."""
        self.ensure_repo()
        if create:
            return self._git("checkout", "-b", branch)
        return self._git("checkout", branch)

    def create_branch(self, branch: str, start_point: str = "HEAD") -> str:
        """Új branch létrehozása a megadott pontból."""
        self.ensure_repo()
        if self.branch_exists(branch):
            return self._git("checkout", branch)
        return self._git("checkout", "-b", branch, start_point)

    def add(self, files: Sequence[str] | None = None) -> str:
        """A megadott fájlok hozzáadása az indexhez, vagy minden módosított fájl."""
        self.ensure_repo()
        if files:
            return self._git("add", *list(files))
        return self._git("add", ".")

    def commit(self, message: str, *, files: Sequence[str] | None = None) -> str:
        """Commit készítése, a tárolt változtatásokból."""
        self.ensure_repo()
        if files:
            self.add(files)
        else:
            self.add()

        status = self.status()
        if status["clean"]:
            return ""

        return self._git("commit", "-m", message)

    def diff(self, *, cached: bool = False, branch: str | None = None) -> str:
        """Git diff a worktree vagy az indexben."""
        self.ensure_repo()
        if branch is not None:
            return self._git("diff", branch)
        if cached:
            return self._git("diff", "--cached")
        return self._git("diff")

    def restore(self, path: str | Iterable[str], *, staged: bool = False) -> str:
        """Visszaállítás a HEAD vagy a staginből. A rollback alapja."""
        self.ensure_repo()
        paths = [path] if isinstance(path, str) else list(path)
        if staged:
            self._git("restore", "--staged", *paths)
            return self._git("restore", *paths)
        return self._git("restore", *paths)

    def reset_hard(self, ref: str = "HEAD") -> str:
        """Visszaállítás a ref-re, teljes worktree + index rollback."""
        self.ensure_repo()
        return self._git("reset", "--hard", ref)

    def worktree_add(self, task_id: str, base_ref: str = "HEAD", *, path: str | None = None) -> str:
        """Új worktree létrehozása egy feladathoz."""
        self.ensure_repo()
        worktree_path = path or os.path.join(os.path.dirname(self.repo_root), f"{os.path.basename(self.repo_root)}-{task_id}")
        if os.path.exists(worktree_path):
            return worktree_path
        self._git("worktree", "add", "-f", worktree_path, base_ref)
        return worktree_path

    def worktree_remove(self, worktree_path: str, *, force: bool = False) -> str:
        """Worktree eltávolítása."""
        self.ensure_repo()
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(worktree_path)
        return self._git(*args)

    def log(self, n: int = 5) -> list[str]:
        """Utolsó n commit rövid összefoglalója."""
        out = self._git("log", "--oneline", "-n", str(n))
        return [line.strip() for line in out.splitlines() if line.strip()]


DEFAULT_GIT_DEVOPS = GitDevOps


if __name__ == "__main__":
    repo = os.getcwd()
    g = GitDevOps(repo)
    print(g.status())
