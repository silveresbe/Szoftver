"""Determinisztikus kapuk (2.4, 11.1, spec: 'kapuk az LLM előtt'): a Coder patchét a sandboxban futtatott, konfigurált
parancsok (szintaxis, teszt, később lint/típus) ellenőrzik. Nulla token. A Coder csak a hibasorokat kapja vissza.

Az ellenőrzendő fát ez a modul másolja külön, szűrt könyvtárba: a tiltott útvonalak (redactor.FORBIDDEN_GLOBS, .git),
a szimbolikus linkek és a túl nagy fa nem kerül a sandboxba. A kapuk mind KEMÉNYEK (a 'soha nem puhítható' osztály)."""
from __future__ import annotations
import hashlib, os, re, shutil, tempfile
from .redactor import FORBIDDEN_GLOBS

MAX_ERROR_LINES, MAX_LINE_CHARS = 30, 200
DEFAULT_STAGE_MB = 50
_ALWAYS_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
_FAIL_MARK = re.compile(r"(^|\s)(FAIL|ERROR|FAILED|Error|Traceback|SyntaxError|IndentationError|NameError|ImportError|ModuleNotFoundError|AssertionError|TypeError|ValueError)\b|\.py:\d+")
_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\d+(\.\d+)?\s*(ms|s|sec)\b|\b\d+\b|/tmp/[^\s:'\"]+|/work/")


class GatesConfigError(Exception):
    pass


class StageTooLarge(Exception):
    pass


_FINDING = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+): (?P<rule>[A-Z]+\d+) (?:\[\*\] )?(?P<message>.*)$")


def parse_findings(output: str) -> list[dict]:
    """ruff `--output-format=concise` sorai -> [{file, line, rule, message}], egyedi (rule, file, line) szerint."""
    seen, out = set(), []
    for l in (output or "").splitlines():
        m = _FINDING.match(l.strip())
        if not m:
            continue
        f = m["file"].replace("\\", "/")
        f = f[2:] if f.startswith("./") else f
        k = (m["rule"], f, int(m["line"]))
        if k not in seen:
            seen.add(k); out.append({"file": f, "line": int(m["line"]), "rule": m["rule"], "message": m["message"][:200]})
    return out


class GateToolsMissing(Exception):
    """A kapu eszköze (pl. ruff, mypy) nincs a sandbox képén: a kapu nem futtatható, emberi döntés kell. Hallgatólagos kihagyás nincs."""


# Előzetes ellenőrzés a sandboxban: a megadott Python-modulok mind megtalálhatók-e. Nem importálja őket, csak keresi.
_TOOLS_CHECK = ("import importlib.util,sys\n"
                "miss=[m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]\n"
                "print('missing: '+' '.join(miss)) if miss else None\n"
                "sys.exit(1 if miss else 0)")


def stage_tree(root: str, dest: str, max_bytes: int, extra_exclude=()) -> int:
    """Szűrt másolat a root-ból a dest-be. Visszatér: a másolt bájtok száma. Túl nagy fa -> StageTooLarge (semmi nem megy ki)."""
    total = 0
    extra = tuple(extra_exclude)
    for cur, dirs, files in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(cur, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirs):
            rel = f"{rel_dir}/{d}".lstrip("/")
            if d in _ALWAYS_SKIP_DIRS or FORBIDDEN_GLOBS.search(rel + "/") or any(rel == x.rstrip("/") or rel.startswith(x.rstrip("/") + "/") for x in extra):
                continue
            if os.path.islink(os.path.join(cur, d)):
                continue
            keep.append(d)
        dirs[:] = keep
        for f in sorted(files):
            rel = f"{rel_dir}/{f}".lstrip("/")
            src = os.path.join(cur, f)
            if os.path.islink(src) or FORBIDDEN_GLOBS.search(rel) or any(rel == x or rel.startswith(x.rstrip("/") + "/") for x in extra):
                continue
            size = os.path.getsize(src)
            total += size
            if total > max_bytes:
                raise StageTooLarge(f"a kapuk fája {max_bytes // (1024 * 1024)} MB fölött van")
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copyfile(src, out)
            shutil.copymode(src, out)
    return total


def error_lines(output: str) -> list[str]:
    """A kimenetből a hibasorok (rövidítve, legfeljebb MAX_ERROR_LINES). Ha nincs felismerhető jelölő: a kimenet vége."""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    picked = [l for l in lines if _FAIL_MARK.search(l)] or lines[-10:]
    seen, out = set(), []
    for l in picked:
        l = l[:MAX_LINE_CHARS]
        if l not in seen:
            seen.add(l); out.append(l)
    return out[:MAX_ERROR_LINES]


def fingerprint(gate: str, errors) -> str:
    """Stabil ujjlenyomat: a kapu neve + a hibasorok számok, címek, időmérések nélkül (a Circuit Breaker ezt hasonlítja)."""
    norm = sorted({_VOLATILE.sub("#", e) for e in errors})
    return gate + ":" + hashlib.sha256("\n".join(norm).encode()).hexdigest()[:12]


class GateRunner:
    def __init__(self, cfg: dict, sandbox, audit=None, root: str = ".", kill=None):
        ci = cfg.get("ci_pipeline") or {}
        self.gates = ci.get("gates") or []
        st = ci.get("staging") or {}
        self.max_bytes = int(st.get("max_mb", DEFAULT_STAGE_MB)) * 1024 * 1024
        self.extra_exclude = tuple(st.get("exclude") or ())
        self.sandbox, self.audit, self.root, self.kill = sandbox, audit, root, kill
        self.required = sorted({m for g in self.gates for m in (g.get("requires") or [])})
        self._tools_ok_for = None          # melyik képre igazolt már az eszközkészlet
        if not self.gates:
            raise GatesConfigError("nincs kapu konfigurálva (ci_pipeline.gates): kapu nélkül nincs ellenőrzés")

    def _preflight(self, task_id: str) -> None:
        """Ha van kapu `requires` mezővel: egyszer (képenként) megnézi, hogy az eszközök megvannak-e. Hiány = GateToolsMissing."""
        if not self.required:
            return
        image = (getattr(self.sandbox, "c", None) or {}).get("image")
        if self._tools_ok_for is not None and self._tools_ok_for == image:
            return
        if self.kill: self.kill.check()
        rc, out = self.sandbox.run(["python", "-c", _TOOLS_CHECK] + self.required)
        if rc != 0:
            missing = " ".join(error_lines(out)[:1])[:200] or f"rc={rc}"
            if self.audit:
                self.audit.append("gates", "GATE_TOOLS_MISSING", {"task_id": task_id, "required": self.required, "rc": rc})
            raise GateToolsMissing(f"a kapuk eszközei hiányoznak a sandbox képéről ({missing}); építsd meg a kapu-képet (docker/build_gates_image.sh)")
        self._tools_ok_for = image if image is not None else "?"

    def run(self, task_id: str) -> dict:
        """Sorban futtatja a kapukat; az első bukónál megáll (a Coder egy hibacsoportot kap). SandboxRefused/Killed felszáll.
        Visszatér: {ok, gate, errors[], fingerprint, feedback, results[{name, rc}], soft[{file,line,rule,message}]}.
        A `kind: soft` kapu (csak ruff stílus-szabálycsoportok, config-ellenőrzött) találatai a `soft` listába kerülnek, NEM állítják meg a futást."""
        results, soft = [], []
        self._preflight(task_id)
        with tempfile.TemporaryDirectory(prefix="fsz-stage-") as stage:
            stage_tree(self.root, stage, self.max_bytes, self.extra_exclude)
            for g in self.gates:
                if self.kill: self.kill.check()
                rc, out = self.sandbox.run(list(g["argv"]), worktree=stage)
                results.append({"name": g["name"], "rc": rc})
                if g.get("kind") == "soft" and rc == 1:          # puha kapu (11.2): a találatok nem állítják meg a futást
                    found = parse_findings(out)
                    if found:
                        soft.extend(found)
                        continue                                  # felismerhetetlen kimenet = nem puha találat, kemény hibaként lejjebb
                if rc != 0:
                    errs = error_lines(out) if rc != 124 else ["időtúllépés a sandboxban"]
                    fp = fingerprint(g["name"], errs)
                    if self.audit:   # csak név, rc, darabszám és ujjlenyomat: a kimenet szövege nem kerül a naplóba
                        self.audit.append("gates", "GATE_FAILED", {"task_id": task_id, "gate": g["name"], "rc": rc, "errors": len(errs), "fingerprint": fp})
                    return {"ok": False, "gate": g["name"], "errors": errs, "fingerprint": fp, "results": results,
                            "feedback": f"A(z) '{g['name']}' kapu elbukott (rc={rc}). Hibasorok:\n" + "\n".join(errs)}
        if self.audit:
            self.audit.append("gates", "GATES_GREEN", {"task_id": task_id, "gates": [r["name"] for r in results], "soft_findings": len(soft)})
        return {"ok": True, "gate": None, "errors": [], "fingerprint": "", "feedback": "", "results": results, "soft": soft}
