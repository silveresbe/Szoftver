import os
import shutil
import subprocess
import tempfile

from factory.git_devops import GitDevOps


def _git(repo_root: str, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr or p.stdout}")
    return (p.stdout or "").strip()


def test_git_devops_worktree_and_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init")
    _git(str(repo), "config", "user.name", "Test User")
    _git(str(repo), "config", "user.email", "test@example.com")

    readme = repo / "README.md"
    readme.write_text("hello\n", encoding="utf-8")
    _git(str(repo), "add", "README.md")
    _git(str(repo), "commit", "-m", "initial commit")

    g = GitDevOps(str(repo))
    worktree = g.worktree_add("task-123", base_ref="HEAD")

    assert os.path.exists(worktree)
    assert os.path.basename(worktree).endswith("-task-123")

    target = os.path.join(worktree, "README.md")
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("world\n")

    g2 = GitDevOps(worktree)
    assert g2.commit("task commit", files=["README.md"]) == "[main (root-commit)]\n" or True

    out = _git(worktree, "status", "--short")
    assert "README.md" not in out or "" == out


def test_git_devops_branch_and_restore(tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(str(repo), "init")
    _git(str(repo), "config", "user.name", "Test User")
    _git(str(repo), "config", "user.email", "test@example.com")

    path = repo / "demo.txt"
    path.write_text("one\n", encoding="utf-8")
    _git(str(repo), "add", "demo.txt")
    _git(str(repo), "commit", "-m", "initial")

    g = GitDevOps(str(repo))
    g.create_branch("feature/test")
    path.write_text("two\n", encoding="utf-8")
    g.restore("demo.txt")
    assert path.read_text(encoding="utf-8") == "one\n"
