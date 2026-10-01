"""Git worktree + branch + rollback wrapper a Supervisor és a Git/DevOps komponenshez.

Ez a wrapper a repo környezetben zárt, determinisztikus műveleteket ad a feladatokhoz:
- task-specifikus worktree
- branch létrehozás / váltás
- commit / status / diff / rollback
"""
from __future__ import annotations

import os
import subprocess
from typing import Iterable, Sequence


class GitError(Exception):
    """Determinista git-hiba, a nyers stderr helyett."""


class Git:
    """Minimális Git wrapper: worktree, commit, merge, rollback."""

    def __init__(self, root: str, audit=None):
        self.root = os.path.abspath(root)
        self.audit = audit

    def _run(self, *args: str, check: bool = True) -> str:
        p = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=False)
        if check and p.returncode != 0:
            detail = (p.stderr or p.stdout or "git hiba").strip() or "git hiba"
            raise GitError(detail)
        return (p.stdout or "").strip()

    def _verify_repo(self):
        if not os.path.isdir(self.root):
            raise GitError(f"repo root nem létezik: {self.root!r}")
        try:
            self._run("rev-parse", "--show-toplevel")
        except GitError as exc:
            raise GitError(f"ez nem git repo: {self.root!r}") from exc

    def status(self) -> dict:
        """Visszaadja a repo állapotát strukturált formában."""
        self._verify_repo()
        branch = self._run("branch", "--show-current")
        raw = self._run("status", "--porcelain")
        modified = [line[3:] for line in raw.splitlines() if line.strip()] if raw else []
        return {"branch": branch, "clean": not bool(modified), "modified_files": modified, "raw": raw}

    def create_worktree(self, task_id: str, base_ref: str = "main") -> str:
        """Munkafájlt hoz létre a feladathoz.

        A munkafájl neve: {repo_name}-{task_id}
        """
        self._verify_repo()
        repo_name = os.path.basename(self.root.rstrip("/"))
        worktree_root = os.path.join(os.path.dirname(self.root), f"{repo_name}-{task_id}")
        if os.path.exists(worktree_root):
            return worktree_root
        self._run("worktree", "add", "-f", worktree_root, base_ref)
        return worktree_root

    def create_branch(self, name: str, start_point: str = "HEAD") -> str:
        self._verify_repo()
        try:
            self._run("rev-parse", "--verify", name)
            return self._run("checkout", name)
        except GitError:
            return self._run("checkout", "-b", name, start_point)

    def checkout(self, name: str) -> str:
        self._verify_repo()
        return self._run("checkout", name)

    def add(self, files: Sequence[str] | None = None) -> str:
        self._verify_repo()
        if files:
            return self._run("add", *list(files))
        return self._run("add", ".")

    def commit(self, message: str, *, files: Sequence[str] | None = None) -> str:
        self._verify_repo()
        if files:
            self.add(files)
        else:
            self.add()
        if not self.status()["modified_files"]:
            return ""
        return self._run("commit", "-m", message)

    def diff(self, *, cached: bool = False) -> str:
        self._verify_repo()
        if cached:
            return self._run("diff", "--cached")
        return self._run("diff")

    def restore(self, path: str | Iterable[str], *, staged: bool = False) -> str:
        self._verify_repo()
        paths = [path] if isinstance(path, str) else list(path)
        if staged:
            self._run("restore", "--staged", *paths)
        return self._run("restore", *paths)

    def rollback(self, path: str | Iterable[str] | None = None) -> str:
        """Rollback a worktree vagy a konkrét fájlok módosításaihoz."""
        self._verify_repo()
        if path is None:
            return self._run("reset", "--hard", "HEAD")
        paths = [path] if isinstance(path, str) else list(path)
        self._run("restore", *paths)
        return "OK"

    def log(self, n: int = 5) -> list[str]:
        self._verify_repo()
        out = self._run("log", "--oneline", "-n", str(n))
        return [line.strip() for line in out.splitlines() if line.strip()]


DEFAULT_GIT = Git


if __name__ == "__main__":
    g = Git(os.getcwd())
    print(g.status())
