"""Mentés és visszaállítási próba (12.5): kód, nem ágens. SQLite online backup, N példány megőrzése.
Nulla token, nulla hálózat."""
from __future__ import annotations
import glob, os, shutil, sqlite3, time


class BackupError(Exception):
    pass


def sqlite_online_backup(src: str, dst: str) -> None:
    """Konzisztens másolat futó (WAL) adatbázisról is: a sqlite3 beépített backup API-ja."""
    if not os.path.exists(src):
        raise BackupError(f"a forrás nem létezik: {src}")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close(); s.close()
    check = sqlite3.connect(dst)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError(f"a mentés sérült: {dst}")
    finally:
        check.close()


def run_backup(base: str, cfg: dict, audit=None, tag: str | None = None, now: float | None = None) -> str:
    """Egy mentési példány: <backup.path>/<időbélyeg>[_tag]/ alatt az adatbázisok (online backup) és a könyvtárak másolata.
    A régi példányokat a `keep` szám fölött törli. Visszaadja a példány útvonalát."""
    b = cfg["backup"]
    root = os.path.join(base, b["path"].lstrip("/"))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now or time.time()))
    dest = os.path.join(root, stamp + (f"_{tag}" if tag else ""))
    if os.path.exists(dest):
        raise BackupError(f"a példány már létezik: {dest}")
    os.makedirs(dest)
    done, missing = [], []
    for t in b["targets"]:
        src = os.path.join(base, t.lstrip("/"))
        if not os.path.exists(src):
            missing.append(t)                          # pl. graph.db a 0. fázisban még nincs; a hiány látszik, nem csendes
            continue
        out = os.path.join(dest, t.strip("/").replace("/", "__"))
        if os.path.isdir(src):
            shutil.copytree(src, out)
        elif src.endswith(".db"):
            sqlite_online_backup(src, out)
        else:
            shutil.copy2(src, out)
        done.append(t)
    if not done:
        shutil.rmtree(dest, ignore_errors=True)
        raise BackupError("nincs mit menteni (egyik cél sem létezik)")
    _prune(root, int(b.get("keep", 7)))
    if audit:
        audit.append("backup", "BACKUP_DONE", {"path": dest, "targets": done, "missing": missing, "tag": tag})
    run_backup.last_missing = missing
    return dest


def _prune(root: str, keep: int) -> None:
    inst = sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))
    for old in inst[:-keep] if keep > 0 else inst:
        shutil.rmtree(old, ignore_errors=True)


def list_backups(base: str, cfg: dict) -> list[str]:
    root = os.path.join(base, cfg["backup"]["path"].lstrip("/"))
    return sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))


def restore_drill(backup_dir: str, audit=None) -> dict:
    """Visszaállítási próba: a mentést ideiglenes helyre másolja, megnyitja, és ellenőrzi, hogy a feladatok és
    checkpointok olvashatók (az újraindítás alapfeltétele). A teljes 'újraindul és folytatja' lépés a Supervisor
    recover() hívása a visszaállított State-en (lásd tests)."""
    import tempfile
    from .state import State
    src = os.path.join(backup_dir, "state__factory.db")
    if not os.path.exists(src):
        raise BackupError(f"a mentésben nincs factory.db: {backup_dir}")
    tmp = tempfile.mkdtemp(prefix="fsz-restore-")
    try:
        dbp = os.path.join(tmp, "factory.db")
        shutil.copy2(src, dbp)
        st = State(dbp)
        try:
            tasks = st.open_tasks()
            cps = {t["task_id"]: st.last_checkpoint(t["task_id"]) for t in tasks}
        finally:
            st.close()
        res = {"ok": True, "open_tasks": len(tasks), "with_checkpoint": sum(1 for c in cps.values() if c)}
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if audit:
        audit.append("backup", "RESTORE_DRILL", {"backup": backup_dir, **res})
    return res
