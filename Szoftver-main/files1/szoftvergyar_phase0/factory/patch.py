"""Patch-alkalmazó (12.2/4, 2.2, 4.5): a Master Coder keresés-csere blokkjait alkalmazza a munkafán. KÓD, nem ágens.

Blokkformák (a séma szerinti JSON-ból):
  módosítás: {"path", "search", "replace"}   a `search` PONTOSAN EGYSZER szerepelhet a fájlban
  új fájl:   {"path", "content"}             csak nem létező fájlra (12.2/4: teljes fájl csak új fájlnál)
Törlés és átnevezés az első változatban nincs (a Coder ESCALATE-tel kéri).

Garanciák:
  * MINDENT VAGY SEMMIT: előbb minden blokk szárazon ellenőrzött; csak ha mind rendben, íródik lemezre bármi.
    Írás közbeni hibánál (pl. OSError) az addig írt fájlok visszaállnak, majd a kivétel továbbmegy.
  * A védett zóna KÓDSZINTEN zárt: minden útvonal a Tool Gatewayen (`repo_write`) megy át, így a `tests/acceptance/**`
    és a többi védett útvonal itt is tiltott (4.5). Ráadásul: `.git/` tiltott; a kis/nagybetű nem számít (fail-closed);
    abszolút útvonal, `..`, backslash, NUL és bármilyen szimbolikus link érintése tiltott.
  * Az auditba csak útvonal, hibakód és darabszám kerül, tartalom soha.
  * `DENIED` és `BAD_PATH` nem újrapróbálható: a Coder ESCALATE-et küld. Csak a `SEARCH_*` hiba újrapróbálható,
    EGYSZER (12.2/4), az `errors[*].context` (az érintett hely környezete) mellé.
A `context` repóból származó szöveg, vagyis ADAT: a hívó a Gateway előtt az Ingress Filteren engedje át
(a kimenő Redactor a Gatewayben fut)."""
from __future__ import annotations
import difflib, os, re, tempfile, shutil
from .tool_gateway import ToolDenied

DEFAULT_LIMITS = {"max_blocks": 50, "max_file_bytes": 1_000_000}
RETRYABLE = {"SEARCH_NOT_FOUND", "SEARCH_AMBIGUOUS"}
AGENT = "master_coder"


def _err(block, path, code, detail, context=None):
    e = {"block": block, "path": path, "code": code, "detail": detail}
    if context:
        e["context"] = context
    return e


# ---------- ellenőrzések ----------

def _shape(b, i, lim):
    """(kind, path, hiba). kind: 'edit' | 'new'."""
    if not isinstance(b, dict):
        return None, None, "a blokk nem objektum"
    path = b.get("path")
    keys = set(b)
    if keys == {"path", "search", "replace"}:
        kind = "edit"
    elif keys == {"path", "content"}:
        kind = "new"
    else:
        return None, path, "a blokk mezői nem 'path+search+replace' vagy 'path+content'"
    if not all(isinstance(b[k], str) for k in keys):
        return None, path, "minden mező szöveg kell legyen"
    if kind == "edit":
        if b["search"] == "":
            return None, path, "üres 'search'"
        if b["search"] == b["replace"]:
            return None, path, "'search' és 'replace' azonos (nincs változás)"
    return kind, path, None


def _norm(path):
    """(normalizált relatív útvonal, hiba). Windows és Unix útvonalakat is elfogad."""
    if not isinstance(path, str) or not path.strip():
        return None, "üres útvonal"
    if "\x00" in path or path != path.strip():
        return None, "tiltott karakter az útvonalban"
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None, "abszolút útvonal"
    if normalized.endswith("/"):
        return None, "könyvtár, nem fájl"
    p = normalized
    if p in (".", "..") or p.startswith("../"):
        return None, "kilépés a munkakönyvtárból"
    parts = p.split("/")
    for part in parts:
        if part == "..":
            return None, "kilépés a munkakönyvtárból"
    return p, None


def norm_path(path):
    """Nyilvános: (normalizált relatív útvonal, hiba) - ugyanazok a szabályok, mint az írásnál."""
    return _norm(path)


def _is_safe_path(expected, real_root):
    """Ellenőrzi, hogy az expected útvonal a real_root alatt van és nem szimbolikus link.
    Windows alatt kezeli a case-insensitive fájlrendszert."""
    try:
        expected_abs = os.path.abspath(expected)
        root_abs = os.path.abspath(real_root)
        if os.name == 'nt':
            expected_abs = expected_abs.lower()
            root_abs = root_abs.lower()
        if not (expected_abs.startswith(root_abs + os.sep) or expected_abs == root_abs):
            return False
        current = expected_abs
        while current != root_abs and current != os.path.dirname(current):
            if os.path.islink(current):
                return False
            current = os.path.dirname(current)
        return True
    except Exception:
        return False


def read_file(root, rel, limits=None):
    """Olvasás a munkafáról az ÍRÁSSAL azonos útvonalszabályokkal (abszolút/`..`/szimbolikus link/`.git` tiltott).
    Visszatér: (szöveg, None) | (None, (kód, részlet)). A tartalom ADAT: promptba az Ingress Filter után kerülhet."""
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    rel_n, e = _norm(rel)
    if e:
        return None, ("BAD_PATH", e)
    if any(part.lower() == ".git" for part in rel_n.split("/")):
        return None, ("DENIED", ".git tiltott")
    expected = os.path.join(os.path.realpath(root), rel_n.replace("/", os.sep))
    if not _is_safe_path(expected, os.path.realpath(root)):
        return None, ("BAD_PATH", "szimbolikus link vagy a munkakönyvtárból kilépő útvonal")
    if not os.path.lexists(expected):
        return None, ("NOT_FOUND", "a fájl nem létezik")
    return _read(expected, lim)


def _access(rel, tools, agent, audit, real_root):
    """None, ha írható; különben (kód, részlet)."""
    log = audit or getattr(tools, "audit", None)
    if any(part.lower() == ".git" for part in rel.split("/")):
        if log:
            log.append(agent, "TOOL_DENIED", {"tool": "repo_write", "path": rel, "via": "git_dir"})
        return "DENIED", ".git tiltott"
    if tools.is_protected(rel.lower()) and not tools.is_protected(rel):
        if log:
            log.append(agent, "TOOL_DENIED", {"tool": "repo_write", "path": rel, "via": "casefold"})
        return "DENIED", "védett zóna"
    try:
        tools.authorize(agent, "repo_write", rel)
    except ToolDenied:
        return "DENIED", "nincs jogosultság vagy védett zóna"
    expected = os.path.join(real_root, rel.replace("/", os.sep))
    if not _is_safe_path(expected, real_root):
        return "BAD_PATH", "szimbolikus link vagy a munkakönyvtárból kilépő útvonal"
    return None


def _read(full, lim):
    """(szöveg, hiba)."""
    if not os.path.isfile(full):
        return None, ("BAD_PATH", "nem szabályos fájl")
    if os.path.getsize(full) > lim["max_file_bytes"]:
        return None, ("TOO_LARGE", f"a fájl nagyobb, mint {lim['max_file_bytes']} bájt")
    with open(full, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, ("NOT_TEXT", "nem UTF-8 szöveg")
    if "\x00" in text:
        return None, ("NOT_TEXT", "bináris tartalom")
    return text, None


def _line_of(text, idx):
    return text.count("\n", 0, idx) + 1


def hunk_context(text, search, radius=5, max_chars=1200):
    """Az érintett hely valódi környezete (sorszámozva) az újrakéréshez: a `search` első nem üres sorához leginkább
    hasonló sor körül. Ha nincs elég hasonló sor, üres (a Coder ilyenkor `read_symbol`-t kérhet)."""
    lines = text.splitlines()
    s_lines = search.splitlines() or [search]
    key = next((l.strip() for l in s_lines if l.strip()), "")
    if not key or not lines:
        return ""
    best_i, best = -1, (0.0, 0)
    for i, l in enumerate(lines):
        l = l.strip()
        sm = difflib.SequenceMatcher(None, key, l)
        if sm.real_quick_ratio() < best[0] or sm.quick_ratio() < best[0]:
            continue
        r = sm.ratio()
        if r < best[0]:
            continue
        cand = (r, sm.find_longest_match(0, len(key), 0, len(l)).size)
        if cand > best:
            best_i, best = i, cand
    if best[0] < 0.6:
        return ""
    lo, hi = max(0, best_i - radius), min(len(lines), best_i + len(s_lines) + radius)
    numbered = [f"{n + 1}: {lines[n]}" for n in range(len(lines))] if len(lines) <= 5000 else None

    def render():
        return "\n".join((numbered[n] if numbered else f"{n + 1}: {lines[n]}") for n in range(lo, hi))
    out = render()
    while len(out) > max_chars and hi - lo > 1:
        if best_i - lo > hi - 1 - best_i:
            lo += 1
        else:
            hi -= 1
        out = render()
    return out if len(out) <= max_chars else out[:max_chars] + "…"


def _edit(text, search, replace):
    """(új szöveg, hiba). Hiba: (kód, részlet, context)."""
    if "\r\n" in text and text.count("\n") == text.count("\r\n") and "\r" not in search and "\r" not in replace:
        search, replace = search.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
    first = text.find(search)
    if first < 0:
        return None, ("SEARCH_NOT_FOUND", "a 'search' szöveg nem található a fájlban", hunk_context(text, search))
    second = text.find(search, first + 1)
    if second >= 0:
        lines, pos = [], first
        while pos >= 0 and len(lines) < 10:
            lines.append(_line_of(text, pos))
            pos = text.find(search, pos + 1)
        return None, ("SEARCH_AMBIGUOUS", "a 'search' több helyen szerepel; bővítsd egyedi környezettel",
                      "találatok sorai: " + ", ".join(map(str, lines)))
    return text[:first] + replace + text[first + len(search):], None


def _commit(real_root, plan):
    """plan: [(rel, orig|None, new)]. Hiba esetén visszaállít és továbbdob."""
    done, made_dirs = [], []
    tmp = None
    try:
        for rel, orig, new in plan:
            full = os.path.join(real_root, rel.replace("/", os.sep))
            d = os.path.dirname(full)
            if orig is None:
                missing = []
                while not os.path.isdir(d):
                    missing.append(d)
                    d = os.path.dirname(d)
                for m in reversed(missing):
                    os.mkdir(m)
                    made_dirs.append(m)
                d = os.path.dirname(full)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".patch-")
            with os.fdopen(fd, "wb") as f:
                f.write(new.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            if orig is None:
                os.chmod(tmp, 0o644)
            else:
                shutil.copymode(full, tmp)
            os.replace(tmp, full)
            tmp = None
            done.append((rel, orig))
    except BaseException:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        for rel, orig in reversed(done):
            full = os.path.join(real_root, rel.replace("/", os.sep))
            if orig is None:
                if os.path.exists(full):
                    os.remove(full)
            else:
                with open(full, "wb") as f:
                    f.write(orig.encode("utf-8"))
        for m in reversed(made_dirs):
            try:
                os.rmdir(m)
            except OSError:
                pass
        raise


def _stats(plan):
    add = rem = 0
    for _rel, orig, new in plan:
        for l in difflib.unified_diff((orig or "").splitlines(), new.splitlines(), lineterm="", n=0):
            if l.startswith("+") and not l.startswith("+++"):
                add += 1
            elif l.startswith("-") and not l.startswith("---"):
                rem += 1
    return {"files": len(plan), "added": add, "removed": rem}


def apply_patch(blocks, root, tools, agent=AGENT, audit=None, dry_run=False, limits=None):
    """Alkalmazza a blokkokat a `root` alatt. `tools` KÖTELEZŐ (ToolGateway): a védett zónát nem lehet kihagyni.
    Visszatér: {"ok", "applied": [útvonalak], "stats", "errors", "retryable"}. Kivétel: Killed (kill switch), OSError
    (írás közben, visszaállítás után), ValueError (a `root` nem könyvtár)."""
    if not os.path.isdir(root):
        raise ValueError(f"a munkakönyvtár nem létezik: {root}")
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    log = audit or getattr(tools, "audit", None)
    real_root = os.path.realpath(root)
    errors = []

    def reject():
        codes = sorted({e["code"] for e in errors})
        if log and not dry_run:
            log.append(agent, "PATCH_REJECTED", {"errors": [{"block": e["block"], "path": e["path"], "code": e["code"]} for e in errors[:20]]})
        return {"ok": False, "applied": [], "stats": None, "errors": errors,
                "retryable": bool(errors) and all(c in RETRYABLE for c in codes), "dry_run": dry_run}

    if not isinstance(blocks, list) or not blocks:
        errors.append(_err(None, None, "BAD_BLOCK", "a patch üres vagy nem lista"))
        return reject()
    if len(blocks) > lim["max_blocks"]:
        errors.append(_err(None, None, "BAD_BLOCK", f"legfeljebb {lim['max_blocks']} blokk engedett"))
        return reject()

    files, order, failed = {}, [], set()
    for i, b in enumerate(blocks):
        kind, path, e = _shape(b, i, lim)
        if e:
            errors.append(_err(i, path if isinstance(path, str) else None, "BAD_BLOCK", e))
            continue
        rel, e = _norm(path)
        if e:
            errors.append(_err(i, path, "BAD_PATH", e))
            continue
        if rel in failed:
            continue
        st = files.get(rel)
        if st is None:
            bad = _access(rel, tools, agent, audit, real_root)
            if bad:
                errors.append(_err(i, rel, *bad)); failed.add(rel); continue
            full = os.path.join(real_root, rel.replace("/", os.sep))
            if os.path.lexists(full):
                text, rerr = _read(full, lim)
                if rerr:
                    errors.append(_err(i, rel, *rerr)); failed.add(rel); continue
                st = {"orig": text, "new": text}
            else:
                st = {"orig": None, "new": None}
            files[rel] = st
            order.append(rel)
        if kind == "new":
            if st["new"] is not None:
                errors.append(_err(i, rel, "EXISTS", "a fájl már létezik (vagy ebben a patchben jön létre); módosításhoz 'search/replace'"))
                failed.add(rel); continue
            if len(b["content"].encode("utf-8")) > lim["max_file_bytes"]:
                errors.append(_err(i, rel, "TOO_LARGE", f"az új fájl nagyobb, mint {lim['max_file_bytes']} bájt"))
                failed.add(rel); continue
            st["new"] = b["content"]
        else:
            if st["new"] is None:
                errors.append(_err(i, rel, "NOT_FOUND", "a fájl nem létezik; új fájlhoz 'path+content' kell"))
                failed.add(rel); continue
            new, eerr = _edit(st["new"], b["search"], b["replace"])
            if eerr:
                errors.append(_err(i, rel, *eerr)); failed.add(rel); continue
            if len(new.encode("utf-8")) > lim["max_file_bytes"]:
                errors.append(_err(i, rel, "TOO_LARGE", f"a módosított fájl nagyobb, mint {lim['max_file_bytes']} bájt"))
                failed.add(rel); continue
            st["new"] = new

    if errors:
        return reject()

    plan = [(rel, files[rel]["orig"], files[rel]["new"]) for rel in order if files[rel]["new"] != files[rel]["orig"]]
    stats = _stats(plan)
    result = {"ok": True, "applied": [p[0] for p in plan], "stats": stats, "errors": [], "retryable": False, "dry_run": dry_run}
    if dry_run or not plan:
        return result
    kill = getattr(tools, "kill", None)
    if kill:
        kill.check()
    try:
        _commit(real_root, plan)
    except BaseException as ex:
        if log:
            log.append(agent, "PATCH_FAILED_ROLLED_BACK", {"error": type(ex).__name__, "files": len(plan)})
        raise
    if log:
        log.append(agent, "PATCH_APPLIED", {"files": result["applied"], "blocks": len(blocks), "added": stats["added"], "removed": stats["removed"]})
    return result


def retry_text(errors, max_errors=10):
    """Tömör újrakérési szöveg (12.2/5: id + hely + kód). Csak újrapróbálható hibáknál van értelme."""
    out = []
    for e in errors[:max_errors]:
        out.append(f"#{e['block']} {e['path']} {e['code']}: {e['detail']}")
        if e.get("context"):
            out.append(e["context"])
    return "\n".join(out)
