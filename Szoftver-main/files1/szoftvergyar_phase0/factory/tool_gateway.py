"""Tool Gateway (6.5): eszközjogosultság KÓDSZINTEN, default deny. A védett zóna (protected_paths) az ágenseknek nem írható (4.5)."""
from __future__ import annotations
import fnmatch, os

class ToolDenied(Exception):
    pass

PERMISSIONS = {
    "supervisor":     {"state_read", "send_message", "repo_read"},
    "product_owner":  {"repo_read", "spec_write"},
    "db_architect":   {"repo_read", "schema_write"},
    "master_coder":   {"repo_read", "repo_write", "sandbox_run", "propose_change"},
    "security":       {"repo_read", "sast", "dep_audit", "sandbox_run"},
    "qa":             {"repo_read", "sandbox_run", "tests_write"},
    "git_devops":     {"repo_read", "commit", "branch", "tag", "rollback", "pipeline", "infra_write"},
    "tech_writer":    {"repo_read", "docs_write"},
    "ux_designer":    {"repo_read", "design_write"},
    "autofix":        {"repo_read", "repo_write"},      # kód (11.2), nem ágens: LLM nélküli formázás; a védett zóna rá is zárt
}
WRITE_TOOLS = {"repo_write", "spec_write", "schema_write", "tests_write", "docs_write", "design_write", "infra_write"}
DEFAULT_PROTECTED = ["core/**", "factory/**", "factory.yaml", "state/**", "audit/**", "evals/baseline.json", "evals/recordings/**",
                     "secrets/**", ".env*", "modules/**", "tests/acceptance/**"]

class ToolGateway:
    def __init__(self, cfg: dict | None = None, audit=None, killswitch=None, permissions=None, protected=None):
        tp = (cfg or {}).get("tool_permissions") or {}
        self.perms = {k: set(v) for k, v in (permissions or PERMISSIONS).items()}
        for k, v in (tp.get("agents") or {}).items():
            self.perms[k] = set(v)
        self.protected = list(protected or (tp.get("protected_paths") or DEFAULT_PROTECTED))
        self.audit, self.kill = audit, killswitch

    def is_protected(self, path: str) -> bool:
        p = os.path.normpath(path).replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        if p.startswith("../") or os.path.isabs(p):   # kilépés a munkakönyvtárból = védettnek számít
            return True
        return any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.replace("**", "*")) for g in self.protected)

    def authorize(self, agent: str, tool: str, path: str | None = None):
        if self.kill: self.kill.check()               # minden lépés előtt (6.9)
        allowed = tool in self.perms.get(agent, set())     # default: deny
        if allowed and tool in WRITE_TOOLS and path is not None and self.is_protected(path):
            allowed = False
        if not allowed:
            if self.audit: self.audit.append(agent, "TOOL_DENIED", {"tool": tool, "path": path})
            raise ToolDenied(f"{agent} nem használhatja: {tool} {path or ''}".strip())
        return True
