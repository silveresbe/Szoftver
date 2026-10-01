"""Config Loader (1.2, 5/5. pont): a factory.yaml indításkor validálva töltődik; hibás konfigurációval nem indul."""
from __future__ import annotations
import re
import os, yaml

class ConfigError(Exception):
    pass

PHASE_COMPONENTS = {  # 12.1: mi engedélyezett az adott fázisban
    0: {"supervisor", "message_bus", "tool_gateway", "llm_gateway", "rate_limiter", "sandbox_runner", "audit_chain", "kill_switch"},
    1: {"product_owner", "master_coder", "qa", "git_devops", "lane_classifier", "autofix", "ingress_filter_rules", "circuit_breaker"},
    2: {"security", "db_architect", "tech_writer", "context_retriever", "fix_cache", "review_cache", "ingress_classifier"},
    3: {"growth_manager", "ux_designer", "ops_watch", "iac", "local_llm", "fallback_providers", "microvm"},
}

def components_allowed(phase: int) -> set[str]:
    out: set[str] = set()
    for p in range(0, phase + 1):
        out |= PHASE_COMPONENTS.get(p, set())
    return out

REQUIRED = {
    "rollout": {"phase": int},
    "rate_limit": {"per_model": dict, "window_seconds": int, "safety_margin": float, "max_request_share": float, "max_wait_seconds": int},
    "sandbox": {"oci_runtime": str, "network": str, "image": str},
    "iteration_policy": {},
    "kill_switch": {},
    "state_store": {},
    "audit": {},
    "llm_gateway_modes": {"mode": str},
    "egress_redaction": {},
}

def validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise ConfigError("a konfig gyökere nem térkép")
    for section, fields in REQUIRED.items():
        if section not in cfg or not isinstance(cfg[section], dict):
            raise ConfigError(f"hiányzó szekció: {section}")
        for k, t in fields.items():
            if k not in cfg[section]:
                raise ConfigError(f"hiányzó mező: {section}.{k}")
            v = cfg[section][k]
            if t is float and isinstance(v, int) and not isinstance(v, bool):
                v = float(v)
            if not isinstance(v, t):
                raise ConfigError(f"rossz típus: {section}.{k} ({type(v).__name__}, várt: {t.__name__})")
    if cfg["llm_gateway_modes"]["mode"] not in ("live", "record", "replay", "mock"):
        raise ConfigError("llm_gateway_modes.mode: live|record|replay|mock")
    if cfg["sandbox"]["network"] != "none":
        raise ConfigError("sandbox.network csak 'none' lehet (1.1/3. alapelv)")
    if cfg["sandbox"].get("host_mounts"):
        raise ConfigError("sandbox.host_mounts tilos")
    if cfg["sandbox"]["oci_runtime"] not in ("runc", "runsc"):
        raise ConfigError("sandbox.oci_runtime: runc|runsc")
    if not (0 < cfg["rate_limit"]["safety_margin"] <= 1):
        raise ConfigError("rate_limit.safety_margin (0,1]")
    if not (0 < cfg["rate_limit"]["max_request_share"] <= 1):
        raise ConfigError("rate_limit.max_request_share (0,1]")
    if cfg["rollout"]["phase"] not in (0, 1, 2, 3):
        raise ConfigError("rollout.phase: 0..3")
    if cfg["llm_gateway_modes"]["mode"] == "live" and not cfg.get("egress_redaction", {}).get("data_egress_acknowledged"):
        raise ConfigError("live módhoz data_egress_acknowledged: true kell (12.6/5)")
    for m, q in cfg["rate_limit"]["per_model"].items():
        if "tpm" not in q or "rpm" not in q:
            raise ConfigError(f"rate_limit.per_model.{m}: tpm és rpm kötelező")
    return cfg

# --- 2. réteg: mélyebb validáció, gyengítés-tiltás, fázis-kapu, elgépelés-védelem (HANDOFF 11. hiány) ---
# A spec 3. fejezetének összes felső szintű szekciója; ami nincs itt, az elgépelés (pl. "rate_limt") és hiba.
KNOWN_SECTIONS = {
    "factory", "tech_stack_lock", "llm_providers", "model_tiers", "agents", "independence_policy", "limits", "rate_limit",
    "context_management", "eval_harness", "test_quality", "state_store", "tool_permissions", "observability", "kill_switch",
    "memory_hygiene", "circuit_breaker", "auto_rollback", "human_gates", "approval_policy", "ci_pipeline", "sandbox", "memory",
    "parallelism", "audit", "scope_control", "prompt_modules", "agent_growth", "maintenance", "context_retriever", "ops_watch",
    "tech_debt", "infrastructure_as_code", "human_gate_classes", "orchestrator", "risk_lanes", "iteration_policy", "fix_cache",
    "review_cache", "ingress_filter", "rollout", "structured_output", "llm_gateway_modes", "test_first", "audit_integrity",
    "backup", "egress_redaction", "local_llm", "fallback_providers", "dependency_audit",
}
# Fázishoz kötött szekciók (12.1): `enabled: true` csak a megadott fázistól.
PHASE_GATED_SECTIONS = {
    "fix_cache": 2, "review_cache": 2, "context_retriever": 2,
    "agent_growth": 3, "maintenance": 3, "ops_watch": 3, "infrastructure_as_code": 3, "local_llm": 3, "fallback_providers": 3,
}
# A sandbox-önteszt kötelező probái: egyiket sem lehet a konfigból elhagyni (a gyengítés emberi döntés, 4.5).
# A tests/ ellenőrzi, hogy ez egyezik a sandbox.PROBES kulcsaival.
REQUIRED_PROBES = ("write_rootfs", "outbound_network", "read_host_path", "mount_syscall", "exceed_pids_limit")

def _num(v, allow_none=False, positive=True):
    if v is None:
        return allow_none
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (v > 0 if positive else v >= 0)

def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0

# --- 11.2: autofix és puha kapu; a kemény/puha szabálylisták L0-ban élnek, a konfigból NEM gyengíthetők ---
SOFT_RULE_PREFIXES = ("E", "W", "C90", "N", "D", "SIM", "UP", "I")           # 11.2 puha csoportok; az E9 (szintaxis) KEMÉNY marad
NEVER_SOFT = ("tests", "coverage", "mutation_score", "security_scan_block", "type_errors", "hard_lint", "secrets", "license", "tech_stack_lock")
_FORBIDDEN_SOFT_FLAGS = ("--fix", "--unsafe-fixes", "--exit-zero", "--ignore", "--extend-ignore", "--per-file-ignores", "--exit-non-zero-on-fix")


def _check_soft_gate(g: dict, ci: dict) -> None:
    n, av = g["name"], g["argv"]
    sg = ci.get("soft_gate")
    if not isinstance(sg, dict) or sg.get("enabled") is not True:
        raise ConfigError(f"ci_pipeline.gates.{n}: puha kapuhoz ci_pipeline.soft_gate.enabled: true kell")
    if av[:4] != ["python", "-m", "ruff", "check"]:
        raise ConfigError(f"ci_pipeline.gates.{n}: puha kapu csak 'python -m ruff check' lehet (teszt, típus, biztonság nem puhítható)")
    bad = [a for a in av if a.split("=")[0] in _FORBIDDEN_SOFT_FLAGS]
    if bad:
        raise ConfigError(f"ci_pipeline.gates.{n}: puha kapuban tiltott kapcsoló: {bad[0]}")
    sel = None
    for i, a in enumerate(av):
        if a == "--select" and i + 1 < len(av):
            sel = av[i + 1]
        elif a.startswith("--select="):
            sel = a.split("=", 1)[1]
    if not sel:
        raise ConfigError(f"ci_pipeline.gates.{n}: puha kapuhoz --select kell (csak a puha szabálycsoportok)")
    for code in sel.split(","):
        if not code or code.startswith("E9") or not code.startswith(SOFT_RULE_PREFIXES) or code in ("ALL",):
            raise ConfigError(f"ci_pipeline.gates.{n}: a(z) '{code}' szabály nem puhítható (kemény csoport)")


def _check_autofix_and_soft_gate(ci: dict) -> None:
    af = ci.get("autofix")
    if af is not None:
        if not isinstance(af, dict):
            raise ConfigError("ci_pipeline.autofix térkép kell")
        if "enabled" in af and not isinstance(af["enabled"], bool):
            raise ConfigError("ci_pipeline.autofix.enabled bool kell")
        if "max_touched_lines_outside_diff" in af and not _pos_int(af["max_touched_lines_outside_diff"]):
            raise ConfigError("ci_pipeline.autofix.max_touched_lines_outside_diff pozitív egész kell")
        if af.get("only_files_in_diff", True) is not True:
            raise ConfigError("ci_pipeline.autofix.only_files_in_diff csak true lehet (11.2)")
        if af.get("counts_as_iteration", False) is not False:
            raise ConfigError("ci_pipeline.autofix.counts_as_iteration csak false lehet (11.3)")
    sg = ci.get("soft_gate")
    if sg is not None:
        if not isinstance(sg, dict):
            raise ConfigError("ci_pipeline.soft_gate térkép kell")
        if "enabled" in sg and not isinstance(sg["enabled"], bool):
            raise ConfigError("ci_pipeline.soft_gate.enabled bool kell")
        for k in ("return_soft_to_coder_until_iteration", "max_soft_findings_returned"):
            if k in sg and (isinstance(sg[k], bool) or not isinstance(sg[k], int) or sg[k] < 0):
                raise ConfigError(f"ci_pipeline.soft_gate.{k} nemnegatív egész kell")
        if sg.get("result_status", "GREEN_WITH_DEBT") != "GREEN_WITH_DEBT":
            raise ConfigError("ci_pipeline.soft_gate.result_status csak GREEN_WITH_DEBT lehet")
        if "never_soft" in sg:
            ns = sg["never_soft"]
            if not isinstance(ns, list) or not set(NEVER_SOFT) <= set(ns):
                raise ConfigError("ci_pipeline.soft_gate.never_soft nem szűkíthető (11.2)")
        if sg.get("debt_created_by", "ci_code_not_agent") != "ci_code_not_agent":
            raise ConfigError("ci_pipeline.soft_gate.debt_created_by csak ci_code_not_agent lehet")
        di = sg.get("debt_intake")
        if di is not None:
            if not isinstance(di, dict):
                raise ConfigError("ci_pipeline.soft_gate.debt_intake térkép kell")
            reg = di.get("register", "state/tech_debt.jsonl")
            if not isinstance(reg, str) or not reg or ".." in reg.split("/"):
                raise ConfigError("ci_pipeline.soft_gate.debt_intake.register relatív útvonal kell")
            if "max_open_items" in di and not _pos_int(di["max_open_items"]):
                raise ConfigError("ci_pipeline.soft_gate.debt_intake.max_open_items pozitív egész kell")
            if di.get("on_full", "close_soft_gate") != "close_soft_gate":
                raise ConfigError("ci_pipeline.soft_gate.debt_intake.on_full csak close_soft_gate lehet")
            if di.get("origin", "soft_gate") != "soft_gate" or di.get("dedupe_by", ["rule", "file"]) != ["rule", "file"]:
                raise ConfigError("ci_pipeline.soft_gate.debt_intake: origin=soft_gate, dedupe_by=[rule, file]")



def validate_deep(cfg: dict) -> dict:
    """A validate() UTÁN fut (a kötelező szekciók megvannak). Minden hiba ConfigError."""
    phase = cfg["rollout"]["phase"]
    unknown = sorted(set(cfg) - KNOWN_SECTIONS)
    if unknown:
        raise ConfigError(f"ismeretlen szekció (elgépelés?): {', '.join(unknown)}")
    for name, min_phase in PHASE_GATED_SECTIONS.items():
        sec = cfg.get(name)
        if isinstance(sec, dict) and sec.get("enabled") and phase < min_phase:
            raise ConfigError(f"{name}.enabled: true csak a(z) {min_phase}. fázistól engedélyezett (jelenlegi: {phase}; 12.1)")
    lim = cfg.get("limits") or {}
    if "total_token_limit" in lim and not _pos_int(lim["total_token_limit"]):
        raise ConfigError("limits.total_token_limit pozitív egész kell")
    rl = cfg["rate_limit"]
    if not _pos_int(rl["window_seconds"]) or not _pos_int(rl["max_wait_seconds"]):
        raise ConfigError("rate_limit.window_seconds és max_wait_seconds pozitív egész kell")
    if "max_concurrent_calls" in rl and not _pos_int(rl["max_concurrent_calls"]):
        raise ConfigError("rate_limit.max_concurrent_calls pozitív egész kell (0 = minden hívás örökre várna)")
    if "starvation_aging_seconds" in rl and not _pos_int(rl["starvation_aging_seconds"]):
        raise ConfigError("rate_limit.starvation_aging_seconds pozitív egész kell")
    po = rl.get("priority_order", [])
    if not isinstance(po, list) or not all(isinstance(a, str) for a in po) or len(set(po)) != len(po):
        raise ConfigError("rate_limit.priority_order egyedi ágensnevek listája kell")
    for m, q in rl["per_model"].items():
        if not isinstance(q, dict):
            raise ConfigError(f"rate_limit.per_model.{m} térkép kell")
        if not _num(q["tpm"]) or not _num(q["rpm"]):
            raise ConfigError(f"rate_limit.per_model.{m}: tpm és rpm pozitív szám kell")
        for k in ("rpd", "tpd"):
            if k in q and not _num(q[k], allow_none=True):
                raise ConfigError(f"rate_limit.per_model.{m}.{k}: pozitív szám vagy null")
    caps = (cfg["iteration_policy"] or {}).get("caps")
    if caps is not None:
        if not isinstance(caps, dict) or set(caps) != {"S1", "S2", "S3"}:
            raise ConfigError("iteration_policy.caps: pontosan S1, S2, S3 kell")
        for lane, c in caps.items():
            if not isinstance(c, dict) or not _pos_int(c.get("iterations")) or not _pos_int(c.get("tokens")):
                raise ConfigError(f"iteration_policy.caps.{lane}: iterations és tokens pozitív egész kell")
    cb = cfg.get("circuit_breaker") or {}
    if "same_fingerprint_iterations" in cb and (not _pos_int(cb["same_fingerprint_iterations"]) or cb["same_fingerprint_iterations"] < 2):
        raise ConfigError("circuit_breaker.same_fingerprint_iterations legalább 2 kell")
    for sec, key in (("state_store", "path"), ("audit", "path")):
        if key in cfg[sec] and not (isinstance(cfg[sec][key], str) and cfg[sec][key]):
            raise ConfigError(f"{sec}.{key} nem üres szöveg kell")
    # --- sandbox: a gyengítés tilos (4.5); ezek nem "beállítások", hanem invariánsok ---
    sb = cfg["sandbox"]
    if sb.get("run_as_user") in ("root", "0", 0):
        raise ConfigError("sandbox.run_as_user nem lehet root")
    if sb.get("read_only_rootfs") is False:
        raise ConfigError("sandbox.read_only_rootfs nem kapcsolható ki")
    if sb.get("on_runtime_unavailable", "halt_and_ask_human") != "halt_and_ask_human":
        raise ConfigError("sandbox.on_runtime_unavailable csak halt_and_ask_human lehet (csendes visszalépés nincs, 4.6)")
    if "timeout_seconds" in sb and not _pos_int(sb["timeout_seconds"]):
        raise ConfigError("sandbox.timeout_seconds pozitív egész kell")
    hard = sb.get("hardening") or {}
    if "pids_limit" in hard and not _pos_int(hard["pids_limit"]):
        raise ConfigError("sandbox.hardening.pids_limit pozitív egész kell")
    st = sb.get("selftest") or {}
    if st.get("on_start") is False:
        raise ConfigError("sandbox.selftest.on_start nem kapcsolható ki (önteszt az első ágens-hívás előtt)")
    if st.get("on_fail", "refuse_to_start") != "refuse_to_start":
        raise ConfigError("sandbox.selftest.on_fail csak refuse_to_start lehet")
    if "must_fail" in st:
        missing = [n for n in REQUIRED_PROBES if n not in (st["must_fail"] or [])]
        if missing:
            raise ConfigError(f"sandbox.selftest.must_fail: kötelező próba(k) hiányoznak: {', '.join(missing)}")
        extra = [n for n in st["must_fail"] if n not in REQUIRED_PROBES]
        if extra:
            raise ConfigError(f"sandbox.selftest.must_fail: ismeretlen próba(k): {', '.join(extra)}")
    mt = cfg.get("model_tiers")
    if mt is not None:
        if not isinstance(mt, dict):
            raise ConfigError("model_tiers térkép kell")
        for tier in ("light", "standard", "heavy"):
            t = mt.get(tier)
            if not isinstance(t, dict) or not isinstance(t.get("model"), str) or not t["model"]:
                raise ConfigError(f"model_tiers.{tier}.model kell")
            if t["model"] not in rl["per_model"]:
                raise ConfigError(f"model_tiers.{tier}: a(z) {t['model']} modellnek nincs rate_limit.per_model bejegyzése (kvóta nélkül nem hívható)")
    ag = cfg.get("agents")
    if ag is not None:
        if not isinstance(ag, dict):
            raise ConfigError("agents térkép kell")
        for name, a in ag.items():
            if not isinstance(a, dict):
                raise ConfigError(f"agents.{name} térkép kell")
            if a.get("tier") is not None and a["tier"] not in ("light", "standard", "heavy"):
                raise ConfigError(f"agents.{name}.tier: light | standard | heavy")
            if "max_tokens_per_task" in a and not _pos_int(a["max_tokens_per_task"]):
                raise ConfigError(f"agents.{name}.max_tokens_per_task pozitív egész kell")
            if "temperature" in a and (not _num(a["temperature"], positive=False) or a["temperature"] > 2):
                raise ConfigError(f"agents.{name}.temperature 0 és 2 közötti szám kell")
    ci = cfg.get("ci_pipeline")
    if ci is not None:
        if not isinstance(ci, dict):
            raise ConfigError("ci_pipeline térkép kell")
        if "gates" in ci:
            gs = ci["gates"]
            if not isinstance(gs, list) or not gs:
                raise ConfigError("ci_pipeline.gates nem üres lista kell")
            names = set()
            for g in gs:
                if not isinstance(g, dict) or not isinstance(g.get("name"), str) or not g["name"].strip():
                    raise ConfigError("ci_pipeline.gates[]: name kell")
                if g["name"] in names:
                    raise ConfigError(f"ci_pipeline.gates: ismétlődő név: {g['name']}")
                names.add(g["name"])
                av = g.get("argv")
                if not isinstance(av, list) or not av or not all(isinstance(x, str) and x for x in av):
                    raise ConfigError(f"ci_pipeline.gates.{g['name']}.argv nem üres szöveglista kell (shell nincs)")
                if av[0] in ("sh", "bash", "dash", "zsh") and "-c" in av:
                    raise ConfigError(f"ci_pipeline.gates.{g['name']}: shell -c nem használható kapuként")
                kind = g.get("kind", "hard")
                if kind not in ("hard", "soft"):
                    raise ConfigError(f"ci_pipeline.gates.{g['name']}.kind: hard | soft")
                if kind == "soft":
                    _check_soft_gate(g, ci)
                rq = g.get("requires")
                if rq is not None and not (isinstance(rq, list) and rq and all(isinstance(x, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", x) for x in rq)):
                    raise ConfigError(f"ci_pipeline.gates.{g['name']}.requires nem üres modulnév-lista kell")
                if g.get("hard", True) is not True:
                    raise ConfigError(f"ci_pipeline.gates.{g['name']}: a kapu kemény, nem puhítható (never_soft)")
        st = ci.get("staging")
        if st is not None:
            if not isinstance(st, dict) or ("max_mb" in st and not _pos_int(st["max_mb"])):
                raise ConfigError("ci_pipeline.staging.max_mb pozitív egész kell")
            if "exclude" in st and not (isinstance(st["exclude"], list) and all(isinstance(x, str) for x in st["exclude"])):
                raise ConfigError("ci_pipeline.staging.exclude szöveglista kell")
        _check_autofix_and_soft_gate(ci)
    bk = cfg.get("backup")
    if bk is not None:
        if not isinstance(bk, dict):
            raise ConfigError("backup térkép kell")
        if not _pos_int(bk.get("keep", 7)):
            raise ConfigError("backup.keep pozitív egész kell")
        if bk.get("method", "sqlite_online_backup") != "sqlite_online_backup":
            raise ConfigError("backup.method csak sqlite_online_backup lehet")
        tg = bk.get("targets")
        if not isinstance(tg, list) or not tg or not all(isinstance(t, str) and t for t in tg):
            raise ConfigError("backup.targets nem üres útvonallista kell")
        if not isinstance(bk.get("path"), str) or not bk["path"]:
            raise ConfigError("backup.path kell")
        for t in tg + [bk["path"]]:
            if ".." in t.replace("\\", "/").split("/"):
                raise ConfigError(f"backup: a '..' útvonalelem tilos: {t}")
    return cfg

def load(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"nincs konfig: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            cfg = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"YAML hiba: {e}")
    return validate_deep(validate(cfg))
