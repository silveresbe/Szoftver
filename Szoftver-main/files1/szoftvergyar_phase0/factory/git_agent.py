"""Git/DevOps ágens (2.5) – worktree, commit, merge, Auto-Rollback. Determinisztikus rész csak."""
from __future__ import annotations
import json, os, subprocess, re
from datetime import datetime

class GitError(Exception):
    pass

class Git:
    """Minimális Git wrapper: worktree, commit, merge, rollback."""
    
    def __init__(self, root: str, audit=None):
        self.root = root
        self.audit = audit
        self._verify_repo()
    
    def _verify_repo(self):
        try:
            subprocess.run(["git", "rev-parse", "--git-dir"], cwd=self.root, check=True, 
                          capture_output=True, timeout=5)
        except Exception as e:
            raise GitError(f"nem Git repo: {self.root}") from e
    
    def _run(self, args, check=True, timeout=30):
        try:
            r = subprocess.run(args, cwd=self.root, capture_output=True, text=True, 
                             check=check, timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            raise GitError(f"timeout: {' '.join(args)}") from e
        except Exception as e:
            raise GitError(f"git parancs hiba: {e}") from e
    
    def create_worktree(self, task_id: str, base_ref: str = "main") -> str:
        """Worktree létrehozása a feladat számára. Visszatér: worktree útvonal."""
        wt_name = f"wt-{task_id}"
        wt_path = os.path.join(self.root, ".worktrees", wt_name)
        try:
            self._run(["git", "worktree", "add", wt_path, base_ref])
            if self.audit:
                self.audit.append("git", "WORKTREE_CREATED", {"task_id": task_id, "path": wt_path})
            return wt_path
        except GitError as e:
            raise GitError(f"worktree létrehozás sikertelen {task_id}: {e}") from e
    
    def destroy_worktree(self, task_id: str):
        """Worktree törlése."""
        wt_name = f"wt-{task_id}"
        wt_path = os.path.join(self.root, ".worktrees", wt_name)
        if os.path.exists(wt_path):
            try:
                self._run(["git", "worktree", "remove", wt_path, "--force"])
                if self.audit:
                    self.audit.append("git", "WORKTREE_REMOVED", {"task_id": task_id})
            except GitError:
                pass
    
    def commit(self, worktree_path: str, message: str, author: str = "Szoftvergyár <factory@local>") -> str:
        """Commit a worktree-ben. Conventional Commits formátum ajánlott."""
        if not message.strip():
            raise GitError("üres commit üzenet")
        try:
            self._run(["git", "-C", worktree_path, "add", "-A"])
            rc, out, err = self._run(["git", "-C", worktree_path, "commit", 
                                     "-m", message, f"--author={author}"], check=False)
            if rc == 0:
                sha = out.strip().split()[-1].rstrip("]")
                return sha
            elif "nothing to commit" in err or "nothing to commit" in out:
                return "no-changes"
            else:
                raise GitError(f"commit hiba: {err}")
        except GitError as e:
            raise GitError(f"commit sikertelen: {e}") from e
    
    def get_last_verified(self, base_ref: str = "main") -> str | None:
        """Utolsó `verified/*` tag lekérése – Auto-Rollback célpontja."""
        try:
            rc, out, _ = self._run(["git", "tag", "-l", "verified/*", "--sort=-version:refname", "--merged", base_ref], check=False)
            if rc == 0 and out.strip():
                return out.strip().split()[0]
        except GitError:
            pass
        return None
    
    def reset_worktree(self, worktree_path: str, ref: str):
        """Hard reset a worktree-ben adott ref-re. Auto-Rollback."""
        try:
            self._run(["git", "-C", worktree_path, "reset", "--hard", ref])
            if self.audit:
                self.audit.append("git", "RESET_TO_REF", {"ref": ref})
        except GitError as e:
            raise GitError(f"reset sikertelen {ref}-re: {e}") from e
    
    def merge_branch(self, branch: str, target: str = "main", no_ff: bool = True) -> dict:
        """Merge pull request után. Visszatér: {status: 'OK'|'CONFLICT'|'FAILED', sha: sha|None, errors: []}."""
        try:
            merge_args = ["git", "merge", branch]
            if no_ff:
                merge_args.append("--no-ff")
            merge_args += ["-m", f"Merge {branch} into {target}"]
            
            # Előbb checkout target-re
            self._run(["git", "checkout", target])
            rc, out, err = self._run(merge_args, check=False)
            
            if rc == 0:
                return {"status": "OK", "sha": out.split()[-1] if out else None}
            elif "CONFLICT" in err or "conflict" in err:
                return {"status": "CONFLICT", "errors": err.split("\n")[:5]}
            else:
                return {"status": "FAILED", "errors": [err]}
        except GitError as e:
            return {"status": "FAILED", "errors": [str(e)]}
    
    def tag(self, ref: str, tag: str) -> bool:
        """Tag létrehozása verifikált commithez."""
        try:
            self._run(["git", "tag", tag, ref])
            if self.audit:
                self.audit.append("git", "TAG_CREATED", {"tag": tag, "ref": ref})
            return True
        except GitError:
            return False

class GitDevOps:
    """Git/DevOps ágens (mock/determinisztikus rész)."""
    
    def __init__(self, cfg, audit=None, sandbox=None):
        self.cfg = cfg
        self.audit = audit
        self.sandbox = sandbox
        self.git = None
    
    def initialize(self, root: str):
        """Git repo inicializálása."""
        try:
            self.git = Git(root, self.audit)
        except GitError as e:
            raise GitError(f"Git inicializálás sikertelen: {e}") from e
    
    def run(self, task_id: str, coder_output: dict) -> dict:
        """Coder outputjából worktree, commit, majd merge vagy rollback.
        Visszatér: {status: 'COMMITTED'|'CONFLICT'|'ROLLBACK'|'FAILED', ...}"""
        if not self.git:
            return {"status": "FAILED", "reason": "Git nincs inicializálva"}
        
        try:
            # 1. Worktree a feladatnak
            wt = self.git.create_worktree(task_id)
            
            # 2. Patch / módosítások a Codertől (már a worktree-ben vannak a `code_task`-ból)
            
            # 3. Commit
            commit_msg = self._format_commit_message(task_id, coder_output)
            sha = self.git.commit(wt, commit_msg)
            
            if sha == "no-changes":
                self.git.destroy_worktree(task_id)
                return {"status": "NO_CHANGES", "task_id": task_id}
            
            # 4. Merge main-be (pull request szimuláció)
            merge_result = self.git.merge_branch(f"wt-{task_id}", "main")
            
            if merge_result["status"] == "CONFLICT":
                # Auto-Rollback: últolsó verified-re
                last_verified = self.git.get_last_verified("main")
                if last_verified:
                    self.git.reset_worktree(wt, last_verified)
                    self.git.destroy_worktree(task_id)
                    if self.audit:
                        self.audit.append("git", "AUTO_ROLLBACK", {"task_id": task_id, "to": last_verified})
                    return {"status": "ROLLBACK", "to": last_verified, "reason": "merge conflict"}
                else:
                    self.git.destroy_worktree(task_id)
                    return {"status": "CONFLICT", "errors": merge_result["errors"]}
            
            if merge_result["status"] == "OK":
                sha = merge_result["sha"] or sha
                # Verified tag
                self.git.tag(sha, f"verified/{self._next_verified_number()}")
                self.git.destroy_worktree(task_id)
                if self.audit:
                    self.audit.append("git", "COMMITTED", {"task_id": task_id, "sha": sha})
                return {"status": "COMMITTED", "sha": sha, "task_id": task_id}
            
            self.git.destroy_worktree(task_id)
            return {"status": "FAILED", "errors": merge_result["errors"]}
        
        except GitError as e:
            self.git.destroy_worktree(task_id)
            return {"status": "FAILED", "reason": str(e)}
    
    def _format_commit_message(self, task_id: str, coder_output: dict) -> str:
        """Conventional Commits formátum."""
        files = coder_output.get("files", [])
        scope = self._infer_scope(files)
        
        msg = f"feat({scope}): {task_id}\n\n"
        if coder_output.get("summary"):
            msg += f"{coder_output['summary']}\n"
        msg += f"\nModified: {len(files)} file(s)\n"
        if coder_output.get("decision_log"):
            msg += f"Decision: {coder_output['decision_log'][:200]}\n"
        
        return msg.strip()
    
    def _infer_scope(self, files: list) -> str:
        """Scope a módosított fájlok alapján."""
        if not files:
            return "core"
        # Az első fájl könyvtár-szintje
        first = files[0].split("/")[0]
        return first or "core"
    
    def _next_verified_number(self) -> int:
        """Következő verified szám."""
        try:
            rc, out, _ = self.git._run(["git", "tag", "-l", "verified/*"], check=False)
            if rc == 0 and out.strip():
                tags = out.strip().split()
                nums = [int(t.split("/")[-1]) for t in tags if t.startswith("verified/")]
                return max(nums) + 1 if nums else 1
        except:
            pass
        return 1
