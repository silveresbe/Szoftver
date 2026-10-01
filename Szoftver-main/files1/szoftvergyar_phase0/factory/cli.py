"""Parancssor: factory check | selftest-mock | stop --now | verify-audit | status"""
from __future__ import annotations
import argparse, os, sys
from . import config as C
from .audit import AuditLog, AuditChainBroken
from .state import State
from .killswitch import KillSwitch

def main(argv=None):
    ap = argparse.ArgumentParser("factory")
    ap.add_argument("--config", default="factory.yaml")
    ap.add_argument("cmd", choices=["check", "verify-audit", "stop", "clear-stop", "status", "backup", "restore-drill", "metrics", "sandbox-selftest"])
    ap.add_argument("--now", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = C.load(a.config)
    except C.ConfigError as e:
        print("KONFIGHIBA:", e); return 2
    base = os.path.dirname(os.path.abspath(a.config))
    p = lambda rel: os.path.join(base, rel)
    if a.cmd == "check":
        print("konfig rendben; fázis:", cfg["rollout"]["phase"], "mód:", cfg["llm_gateway_modes"]["mode"]); return 0
    if a.cmd == "verify-audit":
        try:
            print("lánc rendben, bejegyzések:", AuditLog(p(cfg["audit"]["path"])).verify()); return 0
        except AuditChainBroken as e:
            print("AUDIT_CHAIN_BROKEN:", e); return 3
    if a.cmd == "sandbox-selftest":
        # EMBERI lépés (HANDOFF 1.): valódi Dockerrel/gVisorral futtatandó; a 0. fázis egyetlen nyitott kilépési feltétele.
        from .sandbox import SandboxRunner, SandboxRefused
        au = AuditLog(p(cfg["audit"]["path"]))
        try:
            sb = SandboxRunner(cfg, audit=au); sb.start()
        except SandboxRefused as e:
            print("SANDBOX MEGTAGADVA:", e); return 4
        print("sandbox önteszt: MINDEN PROBA BLOCKED, runtime:", cfg["sandbox"]["oci_runtime"]); return 0
    if a.cmd == "metrics":
        import json
        from .metrics import Metrics
        print(json.dumps(Metrics(p("audit/metrics.jsonl")).summary(), ensure_ascii=False, indent=2)); return 0
    if a.cmd in ("backup", "restore-drill"):
        from . import backup as B
        au = AuditLog(p(cfg["audit"]["path"]))
        try:
            if a.cmd == "backup":
                d = B.run_backup(base, cfg, au); print("mentés kész:", d)
                if B.run_backup.last_missing: print("FIGYELEM, hiányzó célok (nem mentve):", B.run_backup.last_missing)
                return 0
            lst = B.list_backups(base, cfg)
            if not lst:
                print("nincs mentés"); return 5
            r = B.restore_drill(lst[-1], au); print(r); return 0 if r["ok"] else 5
        except B.BackupError as e:
            print("MENTÉSI HIBA:", e); return 5
    ks = KillSwitch(p(os.path.dirname(cfg["kill_switch"].get("file", "state/KILL")) or "state"))
    if a.cmd == "stop":
        ks.trip("factory stop" + (" --now" if a.now else "")); print("kill switch bekapcsolva"); return 0
    if a.cmd == "clear-stop":
        ks.clear(); print("kill switch törölve"); return 0
    if a.cmd == "status":
        st = State(p(cfg["state_store"]["path"]))
        for t in st.open_tasks():
            print(t["task_id"], t["status"], t["lane"], f"{t['iteration']}/{t['iteration_cap']}", f"tok {t['tokens_used']}/{t['token_cap']}")
        print("függő emberi kapuk:", len(st.pending_gates())); return 0

if __name__ == "__main__":
    sys.exit(main())
