"""Autofix (11.2/1. lépcső): formázás és biztonságosan javítható lint-szabályok, LLM nélkül, 0 token. A kapuk ELŐTT fut, nem iteráció.

Menete: szűrt másolat a sandboxba -> ott `ruff format` + `ruff check --fix` (csak SAFE javítás; `--unsafe-fixes` tilos) CSAK a patch által
érintett .py fájlokon -> a sandbox JSON-ban visszaadja a megváltozott fájlokat -> a gazda ellenőrzi és a patch-alkalmazón át írja (védett zóna,
szimbolikus link, atomicitás ugyanúgy, mint a Coder patchénél). Hiba, hiányzó vagy csonka kimenet = az autofix KIMARAD (a fa változatlan).

Korlát (Git/DevOps még nincs): a `chore(style)` commit és a „diffen kívüli sor” pontos számolása nincs. A feladat által létrehozott fájlok teljesen a
diffben vannak; a többi fájl MINDEN módosított sora „diffen kívülinek” számít (konzervatív). Az összeg `max_touched_lines_outside_diff` fölött: kimarad."""
from __future__ import annotations
import difflib, hashlib, json, os, tempfile
from . import patch as P
from .gates import stage_tree, DEFAULT_STAGE_MB

AGENT = "autofix"                    # kód, nem LLM-ágens: saját, minimális jogosultság a ToolGatewayben (repo_read, repo_write; a védett zóna rá is zárt)
BEGIN, END = "@@FSZ-AUTOFIX-BEGIN@@", "@@FSZ-AUTOFIX-END@@"
MAX_PAYLOAD = 900_000
# Javítható szabálycsoportok: a kemény F (pl. fölösleges import) és a puha stílus-csoportok; csak biztonságos javításokkal.
FIX_SELECT = "F,E,W,UP,SIM,I"
DEFAULTS = {"enabled": True, "only_files_in_diff": True, "max_touched_lines_outside_diff": 30}

# A sandboxban futó kód. Argumentumok: a javítandó fájlok relatív útvonalai. Kimenet: BEGIN + JSON + END.
DRIVER = r'''
import hashlib, json, subprocess, sys
files = sys.argv[1:]
def rd(p):
    with open(p, encoding="utf-8", newline="") as f: return f.read()
before = {p: rd(p) for p in files}
subprocess.run([sys.executable, "-m", "ruff", "format", "--no-cache", "--"] + files, capture_output=True)
subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "--fix", "--exit-zero", "--select", "@SELECT@", "--"] + files, capture_output=True)
changed = {}
for p in files:
    now = rd(p)
    if now != before[p]:
        changed[p] = {"sha_before": hashlib.sha256(before[p].encode("utf-8")).hexdigest(), "text": now}
payload = json.dumps(changed)
if len(payload) > @MAX@:
    print("@@FSZ-AUTOFIX-TOOBIG@@")
else:
    print("@@FSZ-AUTOFIX-BEGIN@@" + payload + "@@FSZ-AUTOFIX-END@@")
'''.replace("@SELECT@", FIX_SELECT).replace("@MAX@", str(MAX_PAYLOAD))


def touched_lines(old: str, new: str) -> int:
    """A módosult (törölt + beszúrt, sorokra vetített max) sorok száma."""
    a, b = old.splitlines(), new.splitlines()
    n = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            n += max(i2 - i1, j2 - j1)
    return n


def parse_payload(out: str):
    """A sandbox kimenetéből a {path: {sha_before, text}} szótár; None, ha hiányzik vagy csonka (fail-safe)."""
    if not out or BEGIN not in out or END not in out:
        return None
    body = out[out.rindex(BEGIN) + len(BEGIN): out.rindex(END)]
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Autofixer:
    def __init__(self, cfg: dict, sandbox, tools, audit=None, root: str = ".", kill=None):
        ci = cfg.get("ci_pipeline") or {}
        self.c = {**DEFAULTS, **(ci.get("autofix") or {})}
        st = ci.get("staging") or {}
        self.max_bytes = int(st.get("max_mb", DEFAULT_STAGE_MB)) * 1024 * 1024
        self.extra_exclude = tuple(st.get("exclude") or ())
        self.sandbox, self.tools, self.audit, self.root, self.kill = sandbox, tools, audit, root, kill

    def _skip(self, task_id, why, **extra):
        if self.audit:
            self.audit.append("autofix", "AUTOFIX_SKIPPED", {"task_id": task_id, "why": why, **extra})
        return {"status": "SKIPPED", "why": why, "files": [], "lines": 0}

    def run(self, task_id: str, files, created=()) -> dict:
        """files: a patch által érintett útvonalak; created: a feladat által létrehozott fájlok (teljesen a diffben vannak).
        Visszatér: {status: NOOP|FIXED|SKIPPED|DISABLED, why, files[], lines}. Kivétel: Killed, SandboxRefused (a hívó kezeli)."""
        if not self.c.get("enabled", True):
            return {"status": "DISABLED", "why": "disabled", "files": [], "lines": 0}
        if self.kill: self.kill.check()
        created = set(created)
        targets = []
        for rel in dict.fromkeys(files):
            n = os.path.normpath(rel).replace("\\", "/")
            if not n.endswith(".py") or n.startswith("../") or os.path.isabs(n) or self.tools.is_protected(n):
                continue
            full = os.path.join(self.root, n)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            targets.append(n)
        if not targets:
            return {"status": "NOOP", "why": "no_python_files", "files": [], "lines": 0}
        with tempfile.TemporaryDirectory(prefix="fsz-autofix-") as stage:
            stage_tree(self.root, stage, self.max_bytes, self.extra_exclude)
            rc, out = self.sandbox.run(["python", "-c", DRIVER] + targets, worktree=stage)
        if rc != 0:
            return self._skip(task_id, "sandbox_rc", rc=rc)
        if "@@FSZ-AUTOFIX-TOOBIG@@" in (out or ""):
            return self._skip(task_id, "payload_too_big")
        changed = parse_payload(out)
        if changed is None:
            return self._skip(task_id, "no_or_truncated_output")
        if any(rel not in targets or not isinstance(rec, dict) or not isinstance(rec.get("text"), str) for rel, rec in changed.items()):
            return self._skip(task_id, "unexpected_path")                  # a sandbox nem nevezhet meg más fájlt; semmi nem íródik
        blocks, lines_outside, lines_total, names = [], 0, 0, []
        for rel, rec in sorted(changed.items()):
            with open(os.path.join(self.root, rel), encoding="utf-8", newline="") as f:
                cur = f.read()
            if hashlib.sha256(cur.encode("utf-8")).hexdigest() != rec.get("sha_before"):
                return self._skip(task_id, "file_changed_meanwhile")
            n = touched_lines(cur, rec["text"])
            lines_total += n
            if rel not in created:
                lines_outside += n
            if cur:                                                           # üres fájl nem keresési minta; a formázó üres fájlt nem bővít
                blocks.append({"path": rel, "search": cur, "replace": rec["text"]})
                names.append(rel)
        if not blocks:
            return {"status": "NOOP", "why": "nothing_to_fix", "files": [], "lines": 0}
        limit = int(self.c["max_touched_lines_outside_diff"])
        if lines_outside > limit:
            return self._skip(task_id, "too_wide", lines_outside=lines_outside, limit=limit)
        res = P.apply_patch(blocks, self.root, self.tools, agent=AGENT, audit=self.audit)
        if not res["ok"]:
            return self._skip(task_id, "apply_failed", codes=sorted({e["code"] for e in res["errors"]}))
        if self.audit:
            self.audit.append("autofix", "AUTOFIX_APPLIED", {"task_id": task_id, "files": names, "lines": lines_total, "lines_outside_diff": lines_outside})
        return {"status": "FIXED", "why": "", "files": names, "lines": lines_total}
