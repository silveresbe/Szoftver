"""Supervisor (2.1, 1.1/1,5,8; 4.1–4.4; 11.3; 12.1). 0. fázis: feladatgráf, iterációs plafon, Circuit Breaker,
haladásfigyelő, tokenkeret, fázis-kapu, összeomlás utáni folytatás. Az ágensek (PO, Coder, QA…) az 1. fázisban épülnek."""
from __future__ import annotations
import difflib, json
from . import messages as M
from .config import components_allowed
from .rate_limiter import DailyQuotaExhausted, WaitTooLong, RequestTooLarge
from . import splitter
from .sandbox import SandboxRefused
from .gates import StageTooLarge, GateToolsMissing, fingerprint as gate_fingerprint
from .lane_classifier import LaneClassifier
from .qa import QA

DEFAULT_CAPS = {"S1": {"iterations": 3, "tokens": 20000}, "S2": {"iterations": 5, "tokens": 60000}, "S3": {"iterations": 6, "tokens": 120000}}

class PhaseNotEnabled(Exception):
    pass

class Supervisor:
    def __init__(self, cfg, state, audit, kill=None, sandbox=None, metrics=None):
        self.cfg, self.state, self.audit, self.kill, self.sandbox = cfg, state, audit, kill, sandbox
        self.metrics = metrics
        self.phase = cfg["rollout"]["phase"]
        ip = cfg["iteration_policy"]
        self.caps = ip.get("caps") or DEFAULT_CAPS
        self.breaker_n = (cfg.get("circuit_breaker") or {}).get("same_fingerprint_iterations", 3)
        self.total_token_limit = (cfg.get("limits") or {}).get("total_token_limit")
        self.total_tokens = 0
        self.halted = False
        self.history: dict[str, list[dict]] = {}
        self.lane_classifier = LaneClassifier()

    # --- indulás ---
    def startup(self):
        n = self.audit.verify()
        self.audit.append("supervisor", "STARTUP", {"chain_entries": n, "phase": self.phase})
        if self.sandbox:
            self.sandbox.start()
        return self.recover()

    def recover(self):
        """Nyitott feladatok folytatása az utolsó checkpointról (6.4)."""
        out = []
        for t in self.state.open_tasks():
            out.append({"task_id": t["task_id"], "status": t["status"], "checkpoint": self.state.last_checkpoint(t["task_id"])})
        return out

    # --- fázis-kapu (12.1/1) ---
    def require_component(self, name: str):
        if name not in components_allowed(self.phase):
            self.audit.append("supervisor", "PHASE_BLOCK", {"component": name, "phase": self.phase})
            raise PhaseNotEnabled(f"{name} nincs engedélyezve a(z) {self.phase}. fázisban")

    # --- feladatgráf ---
    def add_task(self, task_id, deps=(), lane=None, data=None, task=None):
        if lane is None or lane not in self.caps:
            source = data or {}
            if task is None:
                for key in ("task", "description", "summary", "title", "text"):
                    value = source.get(key)
                    if value is not None:
                        task = value
                        break
            lane = self.lane_classifier.classify(task or source, default="S2")
        elif lane not in self.caps:
            raise ValueError(f"ismeretlen lane: {lane}")
        cap = self.caps[lane]
        if lane == "S3" and "security" not in components_allowed(self.phase):
            self.state.upsert_task(task_id, "WAITING_HUMAN", lane=lane, iteration_cap=cap["iterations"], token_cap=cap["tokens"], deps=list(deps), data=data or {})
            self.escalate(task_id, "S3 feladat a Security Officer bekötése előtt (12.1/2)")
            return "WAITING_HUMAN"
        self.state.upsert_task(task_id, "PENDING", lane=lane, iteration_cap=cap["iterations"], token_cap=cap["tokens"], deps=list(deps), data=data or {})
        self.audit.append("supervisor", "TASK_ADDED", {"task_id": task_id, "lane": lane, "deps": list(deps)})
        return "PENDING"

    def _has_cycle(self, graph):
        seen, stack = set(), set()
        def dfs(n):
            if n in stack: return True
            if n in seen: return False
            seen.add(n); stack.add(n)
            if any(dfs(d) for d in graph.get(n, [])): return True
            stack.discard(n); return False
        return any(dfs(n) for n in graph)

    def ready_tasks(self):
        """DAG: ami párhuzamosan futhat (minden függősége DONE)."""
        rows = self.state._x("SELECT * FROM tasks").fetchall()
        status = {r["task_id"]: r["status"] for r in rows}
        graph = {r["task_id"]: json.loads(r["deps"]) for r in rows}
        if self._has_cycle(graph):
            raise ValueError("a feladatgráf kört tartalmaz")
        return [t for t, d in graph.items() if status[t] == "PENDING" and all(status.get(x) == "DONE" for x in d)]

    def set_status(self, task_id, status):
        self.state.upsert_task(task_id, status)
        self.audit.append("supervisor", "STATUS", {"task_id": task_id, "status": status})
        if status == "DONE" and self.metrics:
            t = self.state.get_task(task_id)
            self.metrics.task_done(task_id, t["lane"] or "?", t["tokens_used"], t["iteration"])

    # --- eszkaláció / HALT ---
    def escalate(self, task_id, reason, options=("megoldás megadása", "spec/AC módosítása", "szál elvetése és visszaállítás")):
        gid = self.state.open_human_gate(task_id, reason)
        self.audit.append("supervisor", "ESCALATE_HUMAN", {"task_id": task_id, "reason": reason, "gate": gid})
        msg = M.make(task_id, "supervisor", "human", "ESCALATE_HUMAN", {"reason": reason, "options": list(options), "gate": gid})
        self.state.bus_put("human", task_id, M.dumps(msg))
        return gid

    def halt_thread(self, task_id, reason):
        self.state.upsert_task(task_id, "HALTED")
        self.audit.append("supervisor", "HALT", {"task_id": task_id, "reason": reason})
        return self.escalate(task_id, reason)

    def halt_all(self, reason):
        self.halted = True
        for t in self.state.open_tasks():
            self.state.checkpoint(t["task_id"], "halt_all", {"reason": reason})
            self.state.upsert_task(t["task_id"], "HALTED")
        if self.sandbox: self.sandbox.destroy_all()
        self.audit.append("supervisor", "HALT_ALL", {"reason": reason})

    def resolve_gate(self, gate_id, decision, task_id=None):
        """Emberi döntés után az iterációszámláló nullázódik (4.2/5)."""
        self.state.decide_gate(gate_id, decision)
        if task_id:
            self.state.set_task_fields(task_id, iteration=0)
            self.history.pop(task_id, None)
            if decision.strip().lower().startswith("szál elvetése"):
                self.state.upsert_task(task_id, "DISCARDED")
            else:
                self.state.update_task_data(task_id, human_decision=decision[:1000])
                self.state.upsert_task(task_id, "PENDING")
        self.audit.append("human", "GATE_DECIDED", {"gate": gate_id, "discarded": bool(task_id) and decision.strip().lower().startswith("szál elvetése")})

    # --- Product Owner kimenetének átvétele (2.1/24, 12.4/A) ---
    def accept_spec(self, task_id, result: dict) -> str:
        """A ProductOwner.run() eredményét dönti el. Visszatér: 'READY' | 'NEEDS_ANSWERS' | 'ESCALATED' | 'HALT'."""
        st = result["status"]
        if st == "SPEC_READY":
            qs = result.get("open_questions") or []
            if qs:
                self.state.upsert_task(task_id, "WAITING_HUMAN")
                self.escalate(task_id, ("Product Owner nyitott kérdései: " + " | ".join(qs))[:400],
                              options=("kérdések megválaszolása", "feltevések elfogadása", "szál elvetése"))
                return "NEEDS_ANSWERS"
            self.state.checkpoint(task_id, "SPEC_READY", {"path": result["path"], "ui": result["ui"]})
            self.state.update_task_data(task_id, spec_path=result["path"], ui=result["ui"])
            self.audit.append("supervisor", "SPEC_ACCEPTED", {"task_id": task_id, "path": result["path"], "ui": result["ui"]})
            return "READY"
        if st == "DOR_FAILED":
            self.state.upsert_task(task_id, "WAITING_HUMAN")
            self.escalate(task_id, ("DOR_FAILED a javítás után is: " + "; ".join(result["errors"][:5]))[:400],
                          options=("spec kézi javítása", "kérés pontosítása", "szál elvetése"))
            return "ESCALATED"
        if st in ("OUTPUT_FAILED", "HALTED"):
            if st == "OUTPUT_FAILED":
                self.halt_thread(task_id, "Product Owner: " + result["reason"])
            return "HALT"
        raise ValueError(f"ismeretlen Product Owner státusz: {st}")

    # --- Master Coder kimenetének átvétele (2.1, 2.2, 12.2/4) ---
    def accept_patch(self, task_id, result: dict) -> str:
        """A MasterCoder.run() eredményét dönti el. Visszatér: 'APPLIED' | 'ESCALATED' | 'HALT'."""
        st = result["status"]
        if st == "PATCH_APPLIED":
            self.state.checkpoint(task_id, "PATCH_APPLIED", {"files": result["files"], "created": result.get("created", []), "ac_uncovered": result.get("ac_uncovered", [])})
            self.state.upsert_task(task_id, "PATCH_APPLIED")
            self.audit.append("supervisor", "PATCH_ACCEPTED", {"task_id": task_id, "files": result["files"], "ac_uncovered": result.get("ac_uncovered", [])})
            return "APPLIED"
        if st in ("HALTED", "OUTPUT_FAILED"):
            if st == "OUTPUT_FAILED":
                self.halt_thread(task_id, "Master Coder: " + result["reason"])
            return "HALT"
        if st == "ESCALATE_HUMAN":
            reason = f"Master Coder eszkaláció ({result.get('kind', 'other')}): {result['reason']}"
            opts = ("megoldás megadása", "spec/AC módosítása", "szál elvetése")
        elif st == "PATCH_REJECTED":
            reason = "Patch elutasítva" + (" (újrakérés után is)" if result.get("retried") else "") + ": " + "; ".join(str(e) for e in result["errors"][:5])
            opts = ("útvonal/spec javítása", "kézi patch", "szál elvetése")
        elif st == "SPEC_INVALID":
            reason = "A spec nem felel meg a Coder kapujának: " + "; ".join(str(e) for e in result["errors"][:5])
            opts = ("spec kézi javítása", "kérés pontosítása a Product Ownerrel", "szál elvetése")
        elif st == "CONTEXT_INVALID":
            reason = "Kontextusfájl hiba: " + "; ".join(str(e) for e in result["errors"][:5])
            opts = ("fájllista javítása", "szál elvetése")
        elif st == "CONTEXT_TOO_LARGE":
            reason = f"A kérés nem fér a keretbe ({result['est_tokens']}/{result['budget']} token)"
            opts = ("kevesebb kontextusfájl", "spec bontása kisebb feladatokra", "szál elvetése")
        else:
            raise ValueError(f"ismeretlen Master Coder státusz: {st}")
        self.state.upsert_task(task_id, "WAITING_HUMAN")
        self.escalate(task_id, reason[:400], options=opts)
        return "ESCALATED"

    def code_task(self, task_id, coder, files=(), feedback=None) -> str:
        """PO -> Coder átadás: a READY specet (SPEC_READY checkpoint) átadja a Codernek."""
        if self.halted:
            return "HALT"
        cp = self.state.last_checkpoint(task_id)
        d = self.state.get_task_data(task_id)
        t = self.state.get_task(task_id)
        resume = bool(cp) and cp["step"] == "PAUSED_DAILY_LIMIT" and cp["data"].get("resume") == "code_task"
        ok_step = bool(cp) and (cp["step"] in ("SPEC_READY", "GATES_FAILED") or resume)
        if not ok_step or not d.get("spec_path") or not t or t["status"] not in ("PENDING", "PAUSED_DAILY_LIMIT"):
            self.audit.append("supervisor", "CODER_NOT_READY", {"task_id": task_id})
            return "NOT_READY"
        with open(d["spec_path"], encoding="utf-8") as f:
            spec = json.load(f)
        if cp["step"] in ("GATES_FAILED", "PAUSED_DAILY_LIMIT"):
            if feedback is None:
                feedback = cp["data"].get("feedback")
            if not files:
                files = tuple(cp["data"].get("files") or ())
        human = d.get("human_decision")
        if human:
            feedback = (feedback + "\n" if feedback else "") + "Emberi döntés: " + human
        try:
            res = coder.run(task_id, spec, files=files, iteration=self.state.get_task(task_id)["iteration"], feedback=feedback)
        except DailyQuotaExhausted:
            self.pause(task_id, "PAUSED_DAILY_LIMIT", {"resume": "code_task", "files": list(files), "feedback": feedback})
            return "PAUSED"
        except WaitTooLong as e:
            self.state.upsert_task(task_id, "WAITING_HUMAN")
            self.escalate(task_id, f"Túl hosszú várakozás a Coder hívásnál: {e}"[:400], options=("újrapróbálás később", "szál elvetése"))
            return "ESCALATED"
        if human:
            self.state.update_task_data(task_id, human_decision=None)
        return self.accept_patch(task_id, res)

    def review_task(self, task_id, spec, files=(), qa=None, test_results=None, feedback=None):
        """QA review bekötése a pipelineba."""
        if qa is None:
            return "QA_SKIPPED"
        patch = {}
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    patch[path] = f.read()
            except Exception:
                patch[path] = ""
        result = qa.run(task_id, spec, patch, test_results=test_results, feedback=feedback,
                        iteration=self.state.get_task(task_id)["iteration"])
        if result.get("verdict") == "REJECT":
            self.state.upsert_task(task_id, "WAITING_HUMAN")
            self.escalate(task_id, "QA review elutasította a patchet: " + "; ".join(
                str(x.get("message", "")) for x in result.get("findings", [])[:3] if x.get("message")
            )[:400], options=("javítás és új QA", "spec pontosítása", "szál elvetése"))
            return "ESCALATED"
        self.state.checkpoint(task_id, "QA_APPROVED", {"files": list(files), "findings": result.get("findings", [])})
        self.state.upsert_task(task_id, "QA_APPROVED")
        self.audit.append("supervisor", "QA_APPROVED", {"task_id": task_id, "confidence": result.get("confidence", 0)})
        return "APPROVED"

    # --- Coder -> kapuk -> javítási hurok (2.1, 4.2, 11.3) ---
    def drive(self, task_id, coder, gates, files=(), autofix=None, debt=None, qa=None, test_results=None) -> str:
        """READY spec -> Coder -> determinisztikus kapuk; bukásnál a hibasorok visszamennek a Codernek.
        Visszatér: 'GREEN' | 'GREEN_WITH_DEBT' | 'NOT_READY' | 'PAUSED' | 'ESCALATED' | 'HALT' | 'CAP'."""
        files, feedback, orig_model = list(files), None, getattr(coder, "model", None)
        created_all: set = set()
        try:
            while True:
                r = self.code_task(task_id, coder, files=tuple(files), feedback=feedback)
                if r != "APPLIED":
                    return r
                applied = list(self.state.last_checkpoint(task_id)["data"]["files"])
                files += [f for f in applied if f not in files]
                created_all |= set(self.state.last_checkpoint(task_id)["data"].get("created") or [])
                try:
                    if autofix is not None:
                        af = autofix.run(task_id, applied, created=created_all)
                        if af["status"] == "FIXED" and self.metrics: self.metrics.factory("autofix_files_fixed", len(af["files"]), task_id=task_id)
                    g = gates.run(task_id)
                except (SandboxRefused, StageTooLarge, GateToolsMissing) as e:
                    self.state.upsert_task(task_id, "WAITING_HUMAN")
                    self.escalate(task_id, f"A kapuk nem futtathatók: {e}"[:400], options=("sandbox/konfig javítása", "szál elvetése"))
                    return "ESCALATED"
                debt_n = 0
                if g["ok"] and g.get("soft"):
                    g, debt_n = self._soft_gate(task_id, g, debt)
                if g["ok"]:
                    if qa is not None:
                        spec_path = self.state.get_task_data(task_id).get("spec_path")
                        if spec_path:
                            with open(spec_path, "r", encoding="utf-8") as f:
                                spec = json.load(f)
                            qres = self.review_task(task_id, spec, files=files, qa=qa,
                                                    test_results=test_results, feedback=feedback)
                            if qres == "ESCALATED":
                                return "ESCALATED"
                    if debt_n:
                        self.state.checkpoint(task_id, "GATES_GREEN", {"files": files, "debt": debt_n})
                        self.state.upsert_task(task_id, "GATES_GREEN_WITH_DEBT")
                        self.audit.append("supervisor", "GATES_GREEN_WITH_DEBT", {"task_id": task_id, "files": files, "debt": debt_n})
                        return "GREEN_WITH_DEBT"
                    self.state.checkpoint(task_id, "GATES_GREEN", {"files": files})
                    self.state.upsert_task(task_id, "GATES_GREEN")
                    self.audit.append("supervisor", "GATES_GREEN", {"task_id": task_id, "files": files})
                    return "GREEN"
                self.state.checkpoint(task_id, "GATES_FAILED", {"feedback": g["feedback"][:4000], "gate": g["gate"], "files": files})
                rec = self.record_iteration(task_id, g["fingerprint"], errors=set(g["errors"]), files=tuple(applied))
                if rec in ("HALT", "CAP"):
                    return rec
                if rec == "NO_PROGRESS":
                    heavy = ((self.cfg.get("model_tiers") or {}).get("heavy") or {}).get("model")
                    if heavy and getattr(coder, "model", None) != heavy:
                        coder.model = heavy
                        self.audit.append("supervisor", "TIER_ESCALATED", {"task_id": task_id, "to": heavy})
                self.state.upsert_task(task_id, "PENDING")
                feedback = g["feedback"]
        finally:
            if orig_model is not None:
                coder.model = orig_model

    def _soft_gate(self, task_id, g, debt):
        """Puha kapu (11.2). A kapuk zöldek, de puha (stílus) találat maradt."""
        sg = (self.cfg.get("ci_pipeline") or {}).get("soft_gate") or {}
        soft = g["soft"]
        lines = [f"{f['file']}:{f['line']}: {f['rule']} {f['message']}"[:200] for f in soft]
        it_now = self.state.get_task(task_id)["iteration"]
        until, maxret = int(sg.get("return_soft_to_coder_until_iteration", 1)), int(sg.get("max_soft_findings_returned", 5))
        def back(why, errs, head):
            fp = gate_fingerprint("soft_gate", errs)
            return {**g, "ok": False, "gate": "soft_gate", "errors": errs, "fingerprint": fp,
                    "feedback": f"{head}\n" + "\n".join(errs)}, 0
        if debt is None or not debt.fits(soft):
            self.audit.append("supervisor", "SOFT_GATE_CLOSED", {"task_id": task_id, "findings": len(soft), "register": debt is not None})
            return back("closed", lines[:30], "A puha kapu zárva (nincs hely az adósságlistában): a stílushibák most kötelezők. Hibasorok:")
        if it_now + 1 <= until and len(soft) <= maxret:
            self.audit.append("supervisor", "SOFT_RETURNED", {"task_id": task_id, "findings": len(soft)})
            return back("returned", lines, "Puha (stílus) találatok maradtak; javítsd őket, a feladat többi része változatlan. Hibasorok:")
        res = debt.add(task_id, soft)
        if self.metrics: self.metrics.factory("soft_debt_created", res["added"], task_id=task_id)
        return {**g, "soft": []}, len(soft)

    def run_pipeline(self, task_id, request, po, coder, gates, files=(), lane="S2", autofix=None, debt=None, qa=None, test_results=None) -> str:
        """Kérés -> Product Owner -> Coder -> kapuk -> QA."""
        if self.state.get_task(task_id) is None:
            if self.add_task(task_id, lane=lane) == "WAITING_HUMAN":
                return "ESCALATED"
        r = self.accept_spec(task_id, po.run(task_id, request))
        if r != "READY":
            return r
        return self.drive(task_id, coder, gates, files, autofix=autofix, debt=debt, qa=qa, test_results=test_results)

    # --- iteráció, Circuit Breaker, haladásfigyelés (4.2, 11.3) ---
    def record_iteration(self, task_id, fingerprint: str, errors: set | None = None, diff: str = "", files: tuple = (), counts: bool = True):
        """Visszatér: 'CONTINUE' | 'HALT' | 'NO_PROGRESS' | 'CAP'."""
        if self.kill: self.kill.check()
        if not counts:
            return "CONTINUE"
        t = self.state.get_task(task_id)
        it = t["iteration"] + 1
        self.state.set_task_fields(task_id, iteration=it)
        h = self.history.setdefault(task_id, [])
        h.append({"fp": fingerprint, "errors": set(errors or ()), "diff": diff, "files": tuple(files)})
        last = [x["fp"] for x in h[-self.breaker_n:]]
        if len(last) == self.breaker_n and len(set(last)) == 1:
            self.halt_thread(task_id, f"Circuit Breaker: ugyanaz a hiba-ujjlenyomat {self.breaker_n}× ({fingerprint})")
            return "HALT"
        sig = self._no_progress(h)
        if sig:
            self.audit.append("supervisor", "NO_PROGRESS", {"task_id": task_id, "signal": sig})
            if self.metrics: self.metrics.factory("no_progress_events", 1, task_id=task_id)
            if not self.state.get_task_data(task_id).get("tier_escalated"):
                self.state.update_task_data(task_id, tier_escalated=True)
                return "NO_PROGRESS"
            self.escalate(task_id, f"NO_PROGRESS ({sig}) tierlépés után is")
            return "HALT"
        if it >= t["iteration_cap"]:
            self.escalate(task_id, f"iterációs plafon betelt ({it}/{t['iteration_cap']})")
            self.state.upsert_task(task_id, "HALTED")
            return "CAP"
        return "CONTINUE"

    @staticmethod
    def _no_progress(h):
        if len(h) >= 3 and len(h[-1]["errors"]) >= len(h[-2]["errors"]) >= len(h[-3]["errors"]) and h[-1]["errors"]:
            return "error_set_not_shrinking_2_rounds"
        if len(h) >= 2 and h[-1]["diff"] and h[-2]["diff"] and difflib.SequenceMatcher(None, h[-1]["diff"], h[-2]["diff"]).ratio() > 0.8:
            return "diff_similarity_over_0.8"
        if len(h) >= 3 and h[-1]["files"] and h[-1]["files"] == h[-3]["files"] != h[-2]["files"]:
            return "file_ping_pong"
        return None

    # --- token- és költségkeret (4.4) ---
    def add_tokens(self, task_id, tokens: int):
        t = self.state.get_task(task_id)
        used = t["tokens_used"] + tokens
        self.state.set_task_fields(task_id, tokens_used=used)
        self.total_tokens += tokens
        res = "OK"
        if used >= t["token_cap"]:
            self.halt_thread(task_id, f"feladat-tokenplafon ({used}/{t['token_cap']})"); res = "TASK_CAP"
        elif used >= 0.8 * t["token_cap"]:
            self.audit.append("supervisor", "ALERT", {"task_id": task_id, "what": "task_token_cap_80"}); res = "WARN"
        if self.total_token_limit:
            if self.total_tokens >= self.total_token_limit:
                self.halt_all("total_token_limit elérve"); res = "HALT_ALL"
            elif self.total_tokens >= 0.8 * self.total_token_limit:
                self.audit.append("supervisor", "ALERT", {"what": "total_token_limit_80"})
        return res

    def pause(self, task_id, status, checkpoint: dict):
        """PAUSED_RATE_LIMIT / PAUSED_DAILY_LIMIT: checkpoint, folytatás onnan (1.7)."""
        self.state.checkpoint(task_id, status, checkpoint)
        self.state.upsert_task(task_id, status)
        self.audit.append("supervisor", "PAUSED", {"task_id": task_id, "status": status})

    # --- túl nagy kérés bontása (1.7 / 4.4-5b) ---
    def request_budget(self, model: str) -> int:
        """Egy kérés legfeljebb ennyi tokenes lehet."""
        r = self.cfg["rate_limit"]
        return int(r["per_model"][model]["tpm"] * r["safety_margin"] * r["max_request_share"])

    def split_request(self, items, model: str, overhead_tokens: int = 500, max_output_tokens: int = 1000):
        """items: [(név, szöveg)] -> lépések, mindegyik belefér a kérésméret-keretbe."""
        budget = self.request_budget(model) - overhead_tokens - max_output_tokens
        return splitter.plan_steps(list(items), budget)

    def run_steps(self, task_id: str, steps, fn):
        """A lépések egymás után futnak; minden lépés után checkpoint (eredménnyel)."""
        cp = self.state.last_checkpoint(task_id)
        results, start = [], 0
        if cp and cp["step"] == "SPLIT_PROGRESS" and cp["data"].get("total") == len(steps):
            results, start = cp["data"]["results"], cp["data"]["next"]
        for i in range(start, len(steps)):
            if self.kill: self.kill.check()
            try:
                out = fn(steps[i])
            except DailyQuotaExhausted:
                self.pause(task_id, "PAUSED_DAILY_LIMIT", {"total": len(steps), "next": i, "results": results})
                self.state.checkpoint(task_id, "SPLIT_PROGRESS", {"total": len(steps), "next": i, "results": results})
                return "PAUSED_DAILY_LIMIT", results
            except WaitTooLong as e:
                self.state.checkpoint(task_id, "SPLIT_PROGRESS", {"total": len(steps), "next": i, "results": results})
                self.escalate(task_id, f"rate limit várakozás túl hosszú ({e.wait:.0f}s), a lépés: {i + 1}/{len(steps)}")
                return "ESCALATED", results
            results.append(out)
            self.state.checkpoint(task_id, "SPLIT_PROGRESS", {"total": len(steps), "next": i + 1, "results": results})
        return "DONE", results
